"""插件配置声明的解析、读值、校验与写回验证。

夹具一律造在 `.runtime/tests` 下的临时目录里，不碰仓库真实的插件目录与 `bot_config.json`。
"""
import importlib.util
import json
import sys
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

    def test_text_allows_empty_by_default(self):
        directory = self.make_plugin("demo", config={"source": "bot_config", "sections": DEMO_SECTIONS})
        decl = load_declaration(directory)
        self.assertEqual(validate_values(decl, {"name": "", "secret": ""}),
                         {"name": "", "secret": ""})

    def test_text_normalizes_none_to_empty_string(self):
        directory = self.make_plugin("demo", config={"source": "bot_config", "sections": DEMO_SECTIONS})
        decl = load_declaration(directory)
        self.assertEqual(validate_values(decl, {"name": None, "secret": None}),
                         {"name": "", "secret": ""})

    def test_text_rejects_empty_only_when_allow_empty_false(self):
        directory = self.make_plugin("demo", config={
            "source": "bot_config",
            "sections": [{"title": "基础", "fields": [
                {"key": "prefix", "label": "前缀", "type": "text", "default": "echo ",
                 "allow_empty": False},
                {"key": "note", "label": "备注", "type": "text", "default": ""}]}]})
        decl = load_declaration(directory)
        with self.assertRaises(SaveError):
            validate_values(decl, {"prefix": ""})
        with self.assertRaises(SaveError):
            validate_values(decl, {"prefix": None})  # null 视同空值，同样被拒
        self.assertEqual(validate_values(decl, {"prefix": "echo "}), {"prefix": "echo "})
        # 同一份声明里没写 allow_empty 的字段仍接受空串
        self.assertEqual(validate_values(decl, {"note": ""}), {"note": ""})

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


class RepositoryDeclarationsTests(unittest.TestCase):
    """仓库里真实插件的配置声明必须可解析，且与插件 on_load 的实际校验一致。"""

    PLUGINS = ROOT / "plugins"

    def declaration(self, name):
        return load_declaration(self.PLUGINS / name)

    def test_every_plugin_declaration_parses(self):
        manifests = sorted(self.PLUGINS.glob("*/plugin.json"))
        self.assertTrue(manifests, "plugins 下应至少有一个 plugin.json")
        for manifest in manifests:
            with self.subTest(plugin=manifest.parent.name):
                # 没有 config 段返回 None，有 config 段则不应抛异常。
                load_declaration(manifest.parent)

    def test_declared_ranges_match_plugin_validation(self):
        # echo.prefix：on_load 要求非空字符串，声明应同样拒绝空串。
        echo = self.declaration("echo")
        with self.assertRaises(SaveError):
            validate_values(echo, {"prefix": ""})
        # chat_summary.chunk_chars：summary.py 限定 4000–64000，声明应拒绝 1000。
        chat_summary = self.declaration("chat_summary")
        with self.assertRaisesRegex(SaveError, "4000"):
            validate_values(chat_summary, {"chunk_chars": 1000})
        self.assertEqual(validate_values(chat_summary, {"chunk_chars": 16000}),
                         {"chunk_chars": 16000})

    def test_declared_bot_config_source_writes_expected_path(self):
        # 私有配置写在 bot_config.json 的 plugins.echo 下，字段键为 prefix。
        echo = self.declaration("echo")
        self.assertEqual(echo.source, "bot_config")
        keys = [field.key for section in echo.sections for field in section.fields]
        self.assertEqual(keys, ["prefix"])


BGA_PLUGIN = ROOT / "plugins" / "bga_plugin"

# 每项：(小于下界的值, 下界, 上界, 大于上界的值)。下界/上界必须两边都接受。
BGA_NUMERIC_BOUNDS = {
    "ranking_depth": (0, 1, 10000, 10001),
    "cache_seconds": (-1, 0, 3600, 3601),
    "request_interval_seconds": (0.4, 0.5, 60, 60.5),
    "daily_snapshot_hour": (-1, 0, 23, 24),
}

# 声明必须覆盖的 9 项；database_file 是插件目录下的 sqlite 文件名，故意不声明。
BGA_DECLARED_KEYS = [
    "account.email", "account.password", "groups", "default_game", "game_labels",
    "ranking_depth", "cache_seconds", "request_interval_seconds", "daily_snapshot_hour",
]


def load_bga_config_module():
    """直接按路径加载插件的 `bga/config.py`（它只依赖标准库，可独立导入）。"""
    name = "bga_plugin_config_under_test"
    spec = importlib.util.spec_from_file_location(name, BGA_PLUGIN / "bga" / "config.py")
    module = importlib.util.module_from_spec(spec)
    # dataclass 解析注解时要按模块名回查，先登记再执行。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BgaDeclarationTests(unittest.TestCase):
    """bga_plugin 的配置声明必须与它自己的 `bga/config.py` 同一口径。"""

    def declaration(self):
        return load_declaration(BGA_PLUGIN)

    def assert_config_accepts(self, module, values, expected):
        """把一份配置喂给插件自己的 load_config，断言它接受/拒绝。"""
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            path = Path(temporary) / "bga_config.json"
            path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            try:
                module.load_config(path)
            except module.ConfigError:
                accepted = False
            else:
                accepted = True
        self.assertEqual(accepted, expected, f"config.py 对 {values} 的判定不符预期")

    def test_bga_declaration_matches_its_own_validator(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        decl = self.declaration()
        self.assertEqual(decl.source, "plugin_file")
        self.assertEqual(decl.file, "bga_config.json")
        # 页面 key 与 web/ 下的文件名一致（管理台直接把它当路径用）。
        self.assertEqual(decl.pages, ({"key": "ranking.html", "title": "排行榜"},))
        keys = [field.key for section in decl.sections for field in section.fields]
        self.assertEqual(keys, BGA_DECLARED_KEYS)
        self.assertNotIn("database_file", keys)

        module = load_bga_config_module()
        for key, (below, low, high, above) in BGA_NUMERIC_BOUNDS.items():
            with self.subTest(field=key):
                # 声明：边界值接受、越界拒绝。
                self.assertEqual(validate_values(decl, {key: low}), {key: low})
                self.assertEqual(validate_values(decl, {key: high}), {key: high})
                with self.assertRaises(SaveError):
                    validate_values(decl, {key: below})
                with self.assertRaises(SaveError):
                    validate_values(decl, {key: above})
                # 插件自己的校验：同一组边界值给出一致的判定。
                self.assert_config_accepts(module, {key: low}, True)
                self.assert_config_accepts(module, {key: high}, True)
                self.assert_config_accepts(module, {key: below}, False)
                self.assert_config_accepts(module, {key: above}, False)

        # 字符串字段：非字符串一律拒绝；空串允许（匿名模式 / 未设置）。
        for key in ("account.email", "account.password"):
            with self.subTest(field=key):
                self.assertEqual(validate_values(decl, {key: ""}), {key: ""})
                self.assertEqual(validate_values(decl, {key: None}), {key: ""})
                with self.assertRaises(SaveError):
                    validate_values(decl, {key: 123})
        # default_game：null 归一成空串，与 config.py 把 null 视同「未设置」一致。
        self.assertEqual(validate_values(decl, {"default_game": None}), {"default_game": ""})
        with self.assertRaises(SaveError):
            validate_values(decl, {"default_game": 5})
        self.assert_config_accepts(module, {"default_game": None}, True)
        self.assert_config_accepts(module, {"default_game": 5}, False)

        # groups：键与值都必须是非空字符串。
        self.assertEqual(validate_values(decl, {"groups": {"1@chatroom": "yatzy"}}),
                         {"groups": {"1@chatroom": "yatzy"}})
        for bad in ({"": "yatzy"}, {"1@chatroom": ""}, {"1@chatroom": 1}, {"1@chatroom": None}, []):
            with self.subTest(groups=bad):
                with self.assertRaises(SaveError):
                    validate_values(decl, {"groups": bad})
        self.assert_config_accepts(module, {"groups": {"1@chatroom": 1}}, False)
        self.assert_config_accepts(module, {"groups": []}, False)

    def test_bga_declaration_reads_defaults_from_empty_config(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        decl = self.declaration()
        # 读值只按 plugin_dir 找配置文件：用临时目录里的空配置，绝不读插件目录里那份真配置。
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            directory = Path(temporary)
            (directory / "bga_config.json").write_text("{}", encoding="utf-8")
            values = read_values(decl, plugin_dir=directory, bot_config={})
        self.assertEqual(values["account.email"], "")
        self.assertEqual(values["account.password"], "")
        self.assertEqual(values["groups"], {})
        self.assertEqual(values["default_game"], "")
        self.assertEqual(values["ranking_depth"], 1000)
        self.assertEqual(values["cache_seconds"], 300)
        self.assertEqual(values["request_interval_seconds"], 1.0)
        self.assertEqual(values["daily_snapshot_hour"], 4)


if __name__ == "__main__":
    unittest.main()
