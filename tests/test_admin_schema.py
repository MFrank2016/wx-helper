"""插件配置声明的解析、读值、校验与写回验证。

夹具一律造在 `.runtime/tests` 下的临时目录里，不碰仓库真实的插件目录与 `bot_config.json`。
"""
import json
import tempfile
import unittest
from pathlib import Path

from wxbot_framework.admin.schema import (SaveError, load_declaration, read_values,
                                          validate_values, write_values)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"

# 一份仿 bga_plugin 的声明：plugin_file 来源，键用点路径表达嵌套。
BGA_SECTIONS = [
    {"title": "账号", "fields": [
        {"key": "account.email", "label": "邮箱", "type": "text", "default": "",
         "help": "BGA 登录邮箱"},
        {"key": "account.password", "label": "密码", "type": "password", "default": "",
         "help": "BGA 登录密码，页面里做掩码显示"},
    ]},
    {"title": "查询", "fields": [
        {"key": "groups", "label": "群与桌游", "type": "map", "default": {},
         "help": "群 ID → 桌游短名"},
        {"key": "default_game", "label": "默认桌游", "type": "enum", "default": "yatzy",
         "choices": ["yatzy", "splendor"]},
        {"key": "ranking_depth", "label": "排名深度", "type": "int", "default": 1000,
         "range": [1, 10000], "help": "取前多少名，1–10000"},
        {"key": "cache_seconds", "label": "缓存秒数", "type": "int", "default": 300,
         "range": [0, 3600]},
        {"key": "request_interval_seconds", "label": "请求间隔", "type": "float",
         "default": 1.0, "range": [0.5, 60]},
        {"key": "daily_snapshot_hour", "label": "每日快照小时", "type": "int",
         "default": 4, "range": [0, 23]},
    ]},
]

# 覆盖全部字段类型的一份 demo 声明。
DEMO_SECTIONS = [
    {"title": "全部类型", "fields": [
        {"key": "name", "label": "名称", "type": "text", "default": "demo",
         "help": "非空字符串"},
        {"key": "secret", "label": "密钥", "type": "password", "default": ""},
        {"key": "count", "label": "计数", "type": "int", "default": 3, "range": [1, 10]},
        {"key": "ratio", "label": "比例", "type": "float", "default": 1.0, "range": [0.5, 2]},
        {"key": "on", "label": "开关", "type": "bool", "default": True},
        {"key": "mode", "label": "模式", "type": "enum", "default": "fast",
         "choices": ["fast", "slow"]},
        {"key": "pairs", "label": "对照表", "type": "map", "default": {}},
        {"key": "extra", "label": "自定义", "type": "json", "default": None},
    ]},
]


class AdminSchemaTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    def make_plugin(self, name, *, config=None, files=None, manifest=True):
        """造一个临时插件目录；config 为 None 表示 plugin.json 不带 config 段。"""
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        if manifest:
            manifest_data = {"api": "v1", "description": name}
            if config is not None:
                manifest_data["config"] = config
            self.write(directory / "plugin.json", manifest_data)
        for filename, content in (files or {}).items():
            self.write(directory / filename, content)
        return directory

    def bga_config(self):
        return {"source": "plugin_file", "file": "bga_config.json",
                "sections": BGA_SECTIONS, "pages": [{"key": "ranking", "title": "排行榜"}]}

    def echo_config(self):
        return {"source": "bot_config", "sections": [
            {"title": "基础", "fields": [
                {"key": "prefix", "label": "前缀", "type": "text", "default": "echo ",
                 "help": "触发命令用的前缀，非空字符串"}]}]}

    def test_missing_config_section_returns_none(self):
        directory = self.make_plugin("legacy")
        self.assertIsNone(load_declaration(directory))

    def test_unknown_field_type_is_rejected(self):
        directory = self.make_plugin("demo", config={
            "source": "bot_config",
            "sections": [{"title": "怪分组", "fields": [
                {"key": "weird", "label": "怪字段", "type": "colour", "default": "red"}]}]})
        with self.assertRaises(ValueError) as caught:
            load_declaration(directory)
        message = str(caught.exception)
        self.assertIn("demo", message)
        self.assertIn("weird", message)

    def test_source_must_be_one_of_two(self):
        bad = self.make_plugin("demo", config={"source": "elsewhere", "sections": []})
        with self.assertRaises(ValueError):
            load_declaration(bad)
        self.assertEqual(
            load_declaration(self.make_plugin("a", config={"source": "bot_config", "sections": []})).source,
            "bot_config")
        self.assertEqual(
            load_declaration(self.make_plugin(
                "b", config={"source": "plugin_file", "file": "x.json", "sections": []})).source,
            "plugin_file")

    def test_plugin_file_source_requires_file_name(self):
        directory = self.make_plugin("demo", config={"source": "plugin_file", "sections": []})
        with self.assertRaises(ValueError):
            load_declaration(directory)

    def test_read_values_returns_defaults_for_missing_keys(self):
        directory = self.make_plugin("bga_plugin", config=self.bga_config(),
                                     files={"bga_config.json": {}})
        decl = load_declaration(directory)
        self.assertEqual(decl.pages, ({"key": "ranking", "title": "排行榜"},))
        values = read_values(decl, plugin_dir=directory, bot_config={})
        self.assertEqual(values["ranking_depth"], 1000)
        self.assertEqual(values["cache_seconds"], 300)
        self.assertEqual(values["request_interval_seconds"], 1.0)
        self.assertEqual(values["daily_snapshot_hour"], 4)

    def test_read_values_uses_dotted_path_for_nested_keys(self):
        directory = self.make_plugin("bga_plugin", config=self.bga_config(),
                                     files={"bga_config.json": {"account": {"email": "a@b"}}})
        values = read_values(load_declaration(directory), plugin_dir=directory, bot_config={})
        self.assertEqual(values["account.email"], "a@b")

    def test_validate_rejects_out_of_range_int(self):
        directory = self.make_plugin("bga_plugin", config=self.bga_config(),
                                     files={"bga_config.json": {}})
        decl = load_declaration(directory)
        with self.assertRaisesRegex(SaveError, "1–10000"):
            validate_values(decl, {"ranking_depth": 0})
        with self.assertRaisesRegex(SaveError, "1–10000"):
            validate_values(decl, {"ranking_depth": 10001})
        self.assertEqual(validate_values(decl, {"ranking_depth": 1}), {"ranking_depth": 1})

    def test_validate_rejects_wrong_type_and_keeps_bool_distinct(self):
        directory = self.make_plugin("demo", config={"source": "bot_config", "sections": DEMO_SECTIONS})
        decl = load_declaration(directory)
        with self.assertRaises(SaveError):
            validate_values(decl, {"count": "abc"})
        with self.assertRaises(SaveError):
            validate_values(decl, {"count": True})  # 布尔值不算整数
        self.assertEqual(validate_values(decl, {"count": 5}), {"count": 5})
        self.assertEqual(validate_values(decl, {"on": True}), {"on": True})

    def test_validate_enum_checks_choices(self):
        directory = self.make_plugin("bga_plugin", config=self.bga_config(),
                                     files={"bga_config.json": {}})
        decl = load_declaration(directory)
        self.assertEqual(validate_values(decl, {"default_game": "splendor"}),
                         {"default_game": "splendor"})
        with self.assertRaises(SaveError):
            validate_values(decl, {"default_game": "chess"})

    def test_validate_map_requires_string_keys_and_values(self):
        directory = self.make_plugin("bga_plugin", config=self.bga_config(),
                                     files={"bga_config.json": {}})
        decl = load_declaration(directory)
        self.assertEqual(validate_values(decl, {"groups": {"1@chatroom": "yatzy"}}),
                         {"groups": {"1@chatroom": "yatzy"}})
        for bad in ({"": "yatzy"}, {"1@chatroom": ""}, {"1@chatroom": 1}, {"1@chatroom": None}, []):
            with self.subTest(groups=bad):
                with self.assertRaises(SaveError):
                    validate_values(decl, {"groups": bad})
        self.assertEqual(validate_values(decl, {"groups": {}}), {"groups": {}})

    def test_write_values_to_plugin_file_is_atomic_and_keeps_unknown_keys(self):
        original = {
            "account": {"email": "old@b", "password": "旧密码"},
            "groups": {}, "default_game": "yatzy", "ranking_depth": 500, "cache_seconds": 300,
            "request_interval_seconds": 1.0, "daily_snapshot_hour": 4,
            "database_file": "bga.sqlite3", "future_key": {"x": 1},
        }
        directory = self.make_plugin("bga_plugin", config=self.bga_config(),
                                     files={"bga_config.json": original})
        decl = load_declaration(directory)
        result = write_values(decl, {"ranking_depth": 2000, "account.email": "new@b"},
                              plugin_dir=directory, bot_config_path=self.root / "bot_config.json")
        self.assertEqual(result["ranking_depth"], 2000)
        self.assertEqual(result["account"]["email"], "new@b")
        # 声明没覆盖到的键原样保留。
        self.assertEqual(result["future_key"], {"x": 1})
        self.assertEqual(result["database_file"], "bga.sqlite3")
        self.assertEqual(result["account"]["password"], "旧密码")
        target = directory / "bga_config.json"
        text = target.read_text(encoding="utf-8")
        self.assertNotIn("\r\n", text)
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(text, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        self.assertEqual([name for name in sorted(p.name for p in directory.iterdir())
                          if name.startswith(".")], [])

    def test_write_values_to_bot_config_preserves_enabled(self):
        bot_config_path = self.root / "bot_config.json"
        self.write(bot_config_path, {"backend": "native_text",
                                     "plugins": {"echo": {"enabled": True, "prefix": "旧的 "}}})
        directory = self.make_plugin("echo", config=self.echo_config())
        decl = load_declaration(directory)
        result = write_values(decl, {"prefix": "新 "}, plugin_dir=directory,
                              bot_config_path=bot_config_path)
        self.assertTrue(result["plugins"]["echo"]["enabled"])
        self.assertEqual(result["plugins"]["echo"]["prefix"], "新 ")
        stored = json.loads(bot_config_path.read_text(encoding="utf-8-sig"))
        self.assertTrue(stored["plugins"]["echo"]["enabled"])
        self.assertEqual(stored["plugins"]["echo"]["prefix"], "新 ")
        self.assertEqual(stored["backend"], "native_text")

    def test_write_values_creates_missing_nested_object(self):
        bot_config_path = self.root / "bot_config.json"
        self.write(bot_config_path, {"backend": "native_text", "plugins": {}})
        directory = self.make_plugin("echo", config=self.echo_config())
        decl = load_declaration(directory)
        result = write_values(decl, {"prefix": "echo "}, plugin_dir=directory,
                              bot_config_path=bot_config_path)
        self.assertEqual(result["plugins"]["echo"]["prefix"], "echo ")
        stored = json.loads(bot_config_path.read_text(encoding="utf-8-sig"))
        self.assertEqual(stored["plugins"]["echo"]["prefix"], "echo ")

    def test_save_error_leaves_file_untouched(self):
        directory = self.make_plugin("bga_plugin", config=self.bga_config(),
                                     files={"bga_config.json": {"ranking_depth": 1000}})
        target = directory / "bga_config.json"
        before = target.read_bytes()
        decl = load_declaration(directory)
        with self.assertRaises(SaveError):
            write_values(decl, {"ranking_depth": 0}, plugin_dir=directory,
                         bot_config_path=self.root / "bot_config.json")
        self.assertEqual(target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
