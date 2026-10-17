"""配置加载的拒绝矩阵与合法配置；不读取项目配置或原生模块。"""

import json
import tempfile
import unittest
from pathlib import Path

from wxbot_framework.config import ConfigStore

ROOT = Path(__file__).resolve().parent.parent

VALID = {
    "backend": "native_filehelper",
    "chat_allowlist": [],
    "wechat_account": "wxid_robot",
    "wechat_pid": None,
    "reconnect_seconds": 5,
    "plugin_queue_size": 128,
    "plugins": {},
    "log_level": "INFO",
    "console": True,
    "llm_config_file": "llm_config.json",
    "members_config_file": None,
}


class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.assertTrue(
            self.root.resolve().is_relative_to((ROOT / ".runtime/tests").resolve())
        )
        self.temporary.cleanup()

    def load(self, data):
        path = self.root / "bot_config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return ConfigStore(path).load()

    def reject(self, data, message):
        with self.assertRaisesRegex(ValueError, message):
            self.load(data)

    def test_valid_config_loads_and_returns_data(self):
        data = dict(VALID)
        self.assertEqual(self.load(data), data)
        data["backend"] = "native_text"
        data["wechat_account"] = None
        data["members_config_file"] = "members_config.json"
        data["chat_allowlist"] = [ROOM := "12345678@chatroom"]
        data["plugins"] = {"echo": {"enabled": True}}
        data["log_level"] = "info"
        self.assertEqual(self.load(data), data)

    def test_non_object_root_is_rejected(self):
        for value in ([], "x", 1, None, True):
            with self.subTest(root=value):
                self.reject(value, "配置文件必须是 JSON 对象")

    def test_invalid_backend_is_rejected(self):
        for value in ("native_unknown", None, 1, ""):
            data = dict(VALID, backend=value)
            with self.subTest(backend=value):
                self.reject(data, "backend 应为 native_filehelper 或 native_text")

    def test_bad_llm_config_file_is_rejected(self):
        for value in ("", "   ", "a\0b", 5, None):
            data = dict(VALID, llm_config_file=value)
            with self.subTest(llm_config_file=value):
                self.reject(data, "llm_config_file 应为大模型配置文件路径")

    def test_bad_members_config_file_is_rejected(self):
        for value in ("", "   ", "a\0b", 5):
            data = dict(VALID, members_config_file=value)
            with self.subTest(members_config_file=value):
                self.reject(data, "members_config_file 应为成员配置文件路径或 null")

    def test_invalid_wechat_account_is_rejected(self):
        for value in ("", "   ", "filehelper", "123@chatroom", "gh_public", "weixin"):
            data = dict(VALID, wechat_account=value)
            with self.subTest(wechat_account=value):
                self.reject(data, "读取结果不是个人微信账号 ID|目标应为微信 ID 或群 ID")

    def test_non_list_chat_allowlist_is_rejected(self):
        for value in ("x", {}, 1, True):
            data = dict(VALID, chat_allowlist=value)
            with self.subTest(chat_allowlist=value):
                self.reject(data, "chat_allowlist 应为微信 ID 或群 ID 列表")

    def test_invalid_chat_allowlist_entry_is_rejected(self):
        self.reject(
            dict(VALID, chat_allowlist=["not a wxid!"]), "目标应为微信 ID 或群 ID"
        )

    def test_bad_wechat_pid_is_rejected(self):
        for value in (0, -1, 3.5, "123", True):
            data = dict(VALID, wechat_pid=value)
            with self.subTest(wechat_pid=value):
                self.reject(data, "wechat_pid 应为正整数或 null")

    def test_bad_reconnect_seconds_is_rejected(self):
        for value in (0, 0.05, -1, "5", None, float("inf")):
            data = dict(VALID, reconnect_seconds=value)
            with self.subTest(reconnect_seconds=value):
                self.reject(data, "reconnect_seconds 应为至少 0.1 的有限数值")
        self.load(dict(VALID, reconnect_seconds=0.1))

    def test_bad_plugin_queue_size_is_rejected(self):
        for value in (0, 10001, 1.5, "128", True):
            data = dict(VALID, plugin_queue_size=value)
            with self.subTest(plugin_queue_size=value):
                self.reject(data, "plugin_queue_size 应为 1–10000 的整数")
        for value in (1, 10000):
            with self.subTest(plugin_queue_size=value):
                self.load(dict(VALID, plugin_queue_size=value))

    def test_non_dict_plugins_is_rejected(self):
        for value in ([], "x", None):
            data = dict(VALID, plugins=value)
            with self.subTest(plugins=value):
                self.reject(data, "plugins 必须是 JSON 对象")

    def test_bad_plugin_name_or_options_is_rejected(self):
        self.reject(dict(VALID, plugins={"Bad Name": {}}), "插件目录名不合法")
        self.reject(dict(VALID, plugins={"echo": []}), "plugins.echo 必须是 JSON 对象")
        self.reject(
            dict(VALID, plugins={"echo": {"enabled": "yes"}}),
            "plugins.echo.enabled 必须是布尔值",
        )

    def test_unsupported_log_level_is_rejected(self):
        for value in ("VERBOSE", "trace", "NOTSET", ""):
            data = dict(VALID, log_level=value)
            with self.subTest(log_level=value):
                self.reject(data, "log_level 不受支持")

    def test_non_bool_console_is_rejected(self):
        for value in (1, 0, "True", None):
            data = dict(VALID, console=value)
            with self.subTest(console=value):
                self.reject(data, "console 必须是布尔值")

    def test_admin_section_is_optional(self):
        data = dict(VALID)
        loaded = self.load(data)
        self.assertEqual(loaded, data)
        self.assertNotIn("admin", loaded)

    def test_non_dict_admin_is_rejected(self):
        for value in ([], "x", 1, True):
            data = dict(VALID, admin=value)
            with self.subTest(admin=value):
                self.reject(data, "admin 必须是 JSON 对象")

    def test_admin_enabled_must_be_bool(self):
        for value in (1, 0, "true", None):
            data = dict(VALID, admin={"enabled": value})
            with self.subTest(enabled=value):
                self.reject(data, "admin.enabled 必须是布尔值")
        self.assertFalse(self.load(dict(VALID, admin={"enabled": False}))["admin"]["enabled"])

    def test_admin_port_range_is_enforced(self):
        for value in (-1, 65536, 70000, 1.5, "8787", True):
            data = dict(VALID, admin={"port": value})
            with self.subTest(port=value):
                self.reject(data, "admin.port 应为 0–65535 的整数")
        # 0 合法：交给系统自动分配空闲端口，Task 10 的框架用例依赖该写法。
        for value in (0, 1, 65535, 8787):
            with self.subTest(port=value):
                self.assertEqual(
                    self.load(dict(VALID, admin={"port": value}))["admin"], {"port": value}
                )

    def test_admin_unknown_keys_are_rejected(self):
        for key in ("prot", "Enabled", "host"):
            data = dict(VALID, admin={key: 1})
            with self.subTest(key=key):
                self.reject(data, "admin 段存在未知配置项")


if __name__ == "__main__":
    unittest.main()
