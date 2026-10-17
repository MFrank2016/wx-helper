"""管理台 API 路由与组装验证。

本文件只验证「路由 + 组装 + 异常转状态码」，用假对象钉住外部边界：
- 假 `queries` 记录收到的参数并返回固定字典；
- 假 `plugins` 记录 `reload`/`enable`/`disable`/`call_web` 调用；
- 假 `debugger` 记录 `run` 的入参。

夹具一律造在 `.runtime/tests` 下的临时目录里，不碰仓库真实的插件目录与 `bot_config.json`。
"""
import json
import tempfile
import unittest
from pathlib import Path

from wxbot_framework.admin.api import AdminApi
from wxbot_framework.sdk import CapabilityError

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"

TODAY = "2026-01-02"

# 一份仿 echo 的声明：bot_config 来源，含文本、密码与带范围的整数。
ECHO_MANIFEST = {
    "api": "v1",
    "description": "回显插件",
    "config": {
        "source": "bot_config",
        "pages": [{"key": "ranking", "title": "排行榜"}],
        "sections": [{"title": "基础", "fields": [
            {"key": "prefix", "label": "前缀", "type": "text", "default": "echo ",
             "allow_empty": False, "help": "触发命令用的前缀"},
            {"key": "secret", "label": "密钥", "type": "password", "default": "",
             "help": "登录密钥，页面里掩码显示"},
            {"key": "count", "label": "计数", "type": "int", "default": 3,
             "range": [1, 10], "help": "取前多少条，1–10"},
        ]}],
    },
}


class _FakeQueries:
    """记录每次调用的参数，返回固定结果；可切换成抛 RuntimeError 模拟库已关闭。"""

    def __init__(self):
        self.calls = []
        self.summary_result = {"today": {"received": 3, "sent": 1, "handled": 2, "errors": 0},
                               "total": {"received": 5, "sent": 2, "handled": 4, "errors": 1}}
        self.plugin_today_result = {}
        self.logs_result = {"rows": [{"id": 1}], "total": 1}
        self.totals = {}
        self.error = None

    def _enter(self, name, **kwargs):
        if self.error is not None:
            raise self.error
        self.calls.append((name, kwargs))

    def summary(self, day):
        self._enter("summary", day=day)
        return self.summary_result

    def groups(self, day, *, limit=50):
        self._enter("groups", day=day, limit=limit)
        return [{"chat_id": "群", "today_received": 1}]

    def trend(self, *, days=7, day):
        self._enter("trend", day=day, days=days)
        return [{"day": day, "received": 1, "sent": 0}]

    def talkers(self, day, *, chat_id=None, limit=10):
        self._enter("talkers", day=day, chat_id=chat_id, limit=limit)
        return [{"sender_id": "wxid_a", "count": 1, "name": "甲"}]

    def plugin_today(self, day):
        self._enter("plugin_today", day=day)
        return self.plugin_today_result

    def logs(self, **kwargs):
        self._enter("logs", **kwargs)
        if self.totals:
            kinds = kwargs.get("kinds")
            key = kinds[0] if kinds else "全部"
            return {"rows": [], "total": self.totals.get(key, 0)}
        return self.logs_result


class _FakePlugins:
    """假插件管理器：只记录公开方法的调用，支持注入错误与降级。"""

    def __init__(self):
        self.status_rows = []
        self.load_errors = {}
        self.calls = []
        self.web_result = {}
        self.web_error = None
        self.toggle_error = None

    def status(self):
        return [dict(row) for row in self.status_rows]

    def reload(self, name=None):
        self.calls.append(("reload", name))

    def enable(self, name):
        self.calls.append(("enable", name))
        if self.toggle_error is not None:
            raise self.toggle_error

    def disable(self, name):
        self.calls.append(("disable", name))
        if self.toggle_error is not None:
            raise self.toggle_error

    def call_web(self, name, action, params):
        self.calls.append(("call_web", name, action, params))
        if self.web_error is not None:
            raise self.web_error
        return self.web_result


class _FakeDebugger:
    def __init__(self):
        self.calls = []
        self.result = {"plugins": [], "dispatched": 0, "replied": 0, "ms": 1.0, "sent_for_real": False}
        self.error = None

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


class _FakeScheduler:
    """假调度器：只记录公开方法的调用，行由 `status()` 原样返回。"""

    def __init__(self):
        self.calls = []
        self.rows = [{"plugin": "demo", "name": "daily", "schedule": "每天 21:00",
                      "state": "待触发", "enabled": True, "registered": True}]
        self.error = None
        self.run_result = True

    def status(self):
        return [dict(row) for row in self.rows]

    def set_enabled(self, plugin, name, enabled):
        self.calls.append(("set_enabled", plugin, name, enabled))
        if self.error is not None:
            raise self.error
        return {"plugin": plugin, "name": name, "enabled": enabled}

    def set_schedule(self, plugin, name, text):
        self.calls.append(("set_schedule", plugin, name, text))
        if self.error is not None:
            raise self.error
        return {"plugin": plugin, "name": name, "schedule": text}

    def run_now(self, plugin, name):
        self.calls.append(("run_now", plugin, name))
        if self.error is not None:
            raise self.error
        return self.run_result


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.plugin_dir = self.root / "plugins"
        self.plugin_dir.mkdir()
        self.bot_config_path = self.root / "bot_config.json"
        self.bot_config = {"plugins": {}}
        self.write_bot_config()
        self.queries = _FakeQueries()
        self.plugins = _FakePlugins()
        self.debugger = _FakeDebugger()
        self.scheduler = _FakeScheduler()
        self.api = AdminApi(store=object(), queries=self.queries, plugins=self.plugins,
                            instruments=object(), debugger=self.debugger,
                            plugin_dir=self.plugin_dir, bot_config_path=self.bot_config_path,
                            config_loader=lambda: self.bot_config, today=lambda: TODAY,
                            scheduler=self.scheduler)

    def tearDown(self):
        self.temporary.cleanup()

    def write_bot_config(self):
        self.bot_config_path.write_text(json.dumps(self.bot_config, ensure_ascii=False),
                                        encoding="utf-8")

    def add_plugin(self, name, manifest):
        directory = self.plugin_dir / name
        directory.mkdir()
        (directory / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                               encoding="utf-8")

    def handle(self, method, path, query=None, body=None):
        return self.api.handle(method, path, query or {}, body or {})

    def calls_of(self, action):
        return [call for call in self.plugins.calls if call[0] == action]

    def task_calls_of(self, action):
        return [call for call in self.scheduler.calls if call[0] == action]

    def find_field(self, result, key):
        for section in result["sections"]:
            for field in section["fields"]:
                if field["key"] == key:
                    return field
        raise AssertionError(f"未找到字段：{key}")

    # --- 统计 ---

    def test_stats_summary_returns_today_and_total_cards(self):
        status, payload = self.handle("GET", "/api/stats/summary")
        self.assertEqual(status, 200)
        self.assertEqual(payload["today"], self.queries.summary_result["today"])
        self.assertEqual(payload["total"], self.queries.summary_result["total"])
        # 用注入的「今天」口径，各日查询不得各算一遍。
        self.assertEqual(self.queries.calls[0], ("summary", {"day": TODAY}))

    def test_head_is_handled_as_get(self):
        status, payload = self.handle("HEAD", "/api/stats/summary")
        self.assertEqual(status, 200)
        self.assertEqual(payload["today"], self.queries.summary_result["today"])

    def test_stats_summary_reports_closed_store_as_503(self):
        self.queries.error = RuntimeError("管理台统计库已关闭")
        status, payload = self.handle("GET", "/api/stats/summary")
        self.assertEqual(status, 503)
        self.assertIn("已关闭", payload["error"])

    # --- 插件列表 ---

    def test_plugins_list_merges_live_status_and_declaration(self):
        self.add_plugin("echo", ECHO_MANIFEST)
        self.plugins.status_rows = [{"name": "echo", "active": True, "enabled": True, "api": "v1",
                                     "missing_capabilities": [], "handled": 7, "errors": 1,
                                     "dropped": 0, "error": ""}]
        self.queries.plugin_today_result = {"echo": {"handled": 3, "errors": 2}}
        status, payload = self.handle("GET", "/api/plugins")
        self.assertEqual(status, 200)
        self.assertEqual(payload["day"], TODAY)
        row = payload["plugins"][0]
        self.assertEqual(row["name"], "echo")
        self.assertTrue(row["active"])
        self.assertTrue(row["enabled"])
        self.assertEqual(row["handled"], 7)
        self.assertEqual(row["errors"], 1)
        self.assertEqual(row["dropped"], 0)
        self.assertEqual(row["error"], "")
        # 今日计数来自 queries.plugin_today，与实时状态分开。
        self.assertEqual(row["today_handled"], 3)
        self.assertEqual(row["today_errors"], 2)
        self.assertTrue(row["declared"])
        self.assertTrue(row["has_config"])
        self.assertEqual(row["pages"], [{"key": "ranking", "title": "排行榜"}])
        self.assertFalse(row["degraded"])

    def test_plugins_list_marks_degraded_when_error_present(self):
        self.plugins.status_rows = [{"name": "broken", "active": False, "enabled": True, "api": "v1",
                                     "missing_capabilities": ["store.members"], "handled": 0,
                                     "errors": 0, "dropped": 0,
                                     "error": "缺少后端能力：store.members"}]
        status, payload = self.handle("GET", "/api/plugins")
        row = payload["plugins"][0]
        self.assertEqual(status, 200)
        self.assertTrue(row["degraded"])
        self.assertFalse(row["declared"])
        self.assertFalse(row["has_config"])

    def test_plugins_list_survives_broken_declaration(self):
        # 一个声明畸形的插件不能让整页打不开：它作为带 error 的条目照常列出，其余插件正常返回。
        directory = self.plugin_dir / "broken"
        directory.mkdir()
        (directory / "plugin.json").write_text("{ 这不是 JSON", encoding="utf-8")
        self.add_plugin("echo", ECHO_MANIFEST)
        self.plugins.status_rows = [
            {"name": "broken", "active": False, "enabled": True, "api": "v1",
             "missing_capabilities": [], "handled": 0, "errors": 0, "dropped": 0, "error": ""},
            {"name": "echo", "active": True, "enabled": True, "api": "v1",
             "missing_capabilities": [], "handled": 1, "errors": 0, "dropped": 0, "error": ""},
        ]
        status, payload = self.handle("GET", "/api/plugins")
        self.assertEqual(status, 200)
        rows = {row["name"]: row for row in payload["plugins"]}
        self.assertEqual(sorted(rows), ["broken", "echo"])
        self.assertFalse(rows["broken"]["declared"])
        self.assertTrue(rows["broken"]["degraded"])
        self.assertEqual(rows["broken"]["state"], "已降级")
        self.assertTrue(rows["broken"]["error"])
        self.assertTrue(rows["echo"]["declared"])

    def test_plugins_list_ignores_last_error_for_degraded_state(self):
        # 回调偶发的 last_error（status 的 error 列）不该让插件永久显示「已降级」。
        self.plugins.status_rows = [{"name": "echo", "active": True, "enabled": True, "api": "v1",
                                     "missing_capabilities": [], "handled": 1, "errors": 1,
                                     "dropped": 0, "error": "TimeoutError: 大模型超时"}]
        status, payload = self.handle("GET", "/api/plugins")
        row = payload["plugins"][0]
        self.assertEqual(status, 200)
        self.assertFalse(row["degraded"])
        self.assertEqual(row["state"], "运行中")
        # 「最近错误」列照旧显示回调异常摘要。
        self.assertEqual(row["error"], "TimeoutError: 大模型超时")

    def test_plugins_list_marks_degraded_from_load_errors(self):
        # 加载/初始化失败（load_errors）才判「已降级」。
        self.plugins.status_rows = [{"name": "echo", "active": False, "enabled": True, "api": "v1",
                                     "missing_capabilities": [], "handled": 0, "errors": 0,
                                     "dropped": 0, "error": ""}]
        self.plugins.load_errors = {"echo": "缺少后端能力：store.members"}
        status, payload = self.handle("GET", "/api/plugins")
        row = payload["plugins"][0]
        self.assertEqual(status, 200)
        self.assertTrue(row["degraded"])
        self.assertEqual(row["state"], "已降级")

    def test_plugins_list_includes_load_errors_absent_from_status(self):
        # 目录名不合法/plugin.json 坏掉的插件只在 load_errors 里，也要作为一行返回。
        self.plugins.status_rows = []
        self.plugins.load_errors = {"坏名字": "插件名不合法"}
        status, payload = self.handle("GET", "/api/plugins")
        self.assertEqual(status, 200)
        rows = {row["name"]: row for row in payload["plugins"]}
        self.assertIn("坏名字", rows)
        self.assertEqual(rows["坏名字"]["state"], "已降级")
        self.assertEqual(rows["坏名字"]["error"], "插件名不合法")
        self.assertFalse(rows["坏名字"]["enabled"])
        self.assertFalse(rows["坏名字"]["declared"])

    # --- 插件自带页面数据 ---

    def test_plugin_page_data_goes_through_call_web(self):
        self.plugins.web_result = {"login": True, "bound": 2}
        status, payload = self.handle("GET", "/api/plugin/bga_plugin/status", {"x": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"login": True, "bound": 2})
        self.assertEqual(self.calls_of("call_web"), [("call_web", "bga_plugin", "status", {"x": "1"})])

    def test_plugin_page_data_rejects_unknown_plugin_and_missing_hook(self):
        self.plugins.web_error = ValueError("插件未在运行")
        status, payload = self.handle("GET", "/api/plugin/nope/status")
        self.assertEqual(status, 400)
        self.assertIn("未在运行", payload["error"])
        self.plugins.web_error = ValueError("插件未提供管理页面数据")
        status, payload = self.handle("GET", "/api/plugin/echo/status")
        self.assertEqual(status, 400)
        self.assertIn("未提供管理页面数据", payload["error"])

    # --- 插件配置 ---

    def test_plugin_config_returns_sections_with_values_and_masks_secrets(self):
        self.add_plugin("echo", ECHO_MANIFEST)
        self.bot_config = {"plugins": {"echo": {"enabled": True, "prefix": "echo ",
                                                "secret": "s3cr3t", "count": 5}}}
        status, payload = self.handle("GET", "/api/plugins/echo/config")
        self.assertEqual(status, 200)
        self.assertTrue(payload["declared"])
        self.assertEqual(payload["source"], "bot_config")
        self.assertEqual(payload["pages"], [{"key": "ranking", "title": "排行榜"}])
        self.assertEqual(payload["sections"][0]["title"], "基础")
        prefix = self.find_field(payload, "prefix")
        self.assertEqual((prefix["value"], prefix["masked"]), ("echo ", False))
        self.assertEqual(prefix["allow_empty"], False)
        secret = self.find_field(payload, "secret")
        # 密码字段不回显：值为空串并带 masked 标记。
        self.assertEqual((secret["value"], secret["masked"]), ("", True))
        count = self.find_field(payload, "count")
        self.assertEqual((count["value"], count["range"]), (5, [1, 10]))

    def test_plugin_config_save_reloads_and_reports_load_error(self):
        self.add_plugin("echo", ECHO_MANIFEST)
        self.bot_config = {"plugins": {"echo": {"enabled": True, "prefix": "echo "}}}
        self.write_bot_config()
        self.plugins.load_errors = {"echo": "缺少后端能力：store.members"}
        status, payload = self.handle("POST", "/api/plugins/echo/config",
                                      body={"values": {"prefix": "hi "}})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        # 走 PluginManager 的公开 reload，不自己操作 records。
        self.assertEqual(self.calls_of("reload"), [("reload", "echo")])
        self.assertTrue(payload["degraded"])
        self.assertIn("缺少后端能力", payload["reason"])
        saved = json.loads(self.bot_config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["plugins"]["echo"]["prefix"], "hi ")
        # 未提交但已存在的键原样保留。
        self.assertTrue(saved["plugins"]["echo"]["enabled"])

    def test_plugin_config_save_keeps_secret_when_submitted_empty(self):
        self.add_plugin("echo", ECHO_MANIFEST)
        self.bot_config = {"plugins": {"echo": {"enabled": True, "secret": "s3cr3t"}}}
        self.write_bot_config()
        status, _ = self.handle("POST", "/api/plugins/echo/config",
                                body={"values": {"secret": "", "prefix": "hi "}})
        self.assertEqual(status, 200)
        saved = json.loads(self.bot_config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["plugins"]["echo"]["secret"], "s3cr3t")

    def test_plugin_config_save_rejects_bad_value_with_400(self):
        self.add_plugin("echo", ECHO_MANIFEST)
        self.bot_config = {"plugins": {"echo": {"enabled": True, "count": 3}}}
        self.write_bot_config()
        before = self.bot_config_path.read_bytes()
        status, payload = self.handle("POST", "/api/plugins/echo/config",
                                      body={"values": {"count": 999}})
        self.assertEqual(status, 400)
        self.assertIn("count", payload["error"])
        self.assertEqual(self.bot_config_path.read_bytes(), before)
        self.assertEqual(self.calls_of("reload"), [])

    def test_plugin_without_declaration_reports_declared_false(self):
        self.add_plugin("plain", {"api": "v1", "description": "无配置声明"})
        self.bot_config = {"plugins": {"plain": {"enabled": True, "foo": 1}}}
        status, payload = self.handle("GET", "/api/plugins/plain/config")
        self.assertEqual(status, 200)
        self.assertFalse(payload["declared"])
        self.assertEqual(payload["raw"], {"enabled": True, "foo": 1})
        self.assertEqual(payload["pages"], [])

    # --- 开关与重载 ---

    def test_toggle_enable_disable_calls_plugin_manager(self):
        status, payload = self.handle("POST", "/api/plugins/echo/toggle", body={"enabled": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["enabled"])
        self.assertEqual(self.calls_of("enable"), [("enable", "echo")])
        status, payload = self.handle("POST", "/api/plugins/echo/toggle", body={"enabled": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["enabled"])
        self.assertEqual(self.calls_of("disable"), [("disable", "echo")])

    def test_toggle_reports_failure_with_400(self):
        self.plugins.toggle_error = CapabilityError("缺少后端能力：text.receive")
        status, payload = self.handle("POST", "/api/plugins/echo/toggle", body={"enabled": True})
        self.assertEqual(status, 400)
        self.assertIn("缺少后端能力", payload["error"])

    def test_toggle_requires_boolean_enabled(self):
        status, payload = self.handle("POST", "/api/plugins/echo/toggle", body={})
        self.assertEqual(status, 400)
        self.assertIn("enabled", payload["error"])

    def test_reload_without_name_reloads_all(self):
        status, payload = self.handle("POST", "/api/plugins/reload")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.calls_of("reload"), [("reload", None)])

    def test_reload_by_name_calls_plugin_manager(self):
        status, payload = self.handle("POST", "/api/plugins/echo/reload")
        self.assertEqual(status, 200)
        self.assertEqual(self.calls_of("reload"), [("reload", "echo")])

    # --- 日志 ---

    def test_logs_endpoint_passes_filters_through(self):
        status, payload = self.handle("GET", "/api/logs", {
            "kinds": "receive,send", "keyword": "你好", "chat_id": "群1",
            "plugin": "echo", "since": "2026-01-02T00:00:00", "until": "2026-01-03T00:00:00",
            "limit": "20", "offset": "40"})
        self.assertEqual(status, 200)
        self.assertEqual(payload, self.queries.logs_result)
        self.assertEqual(self.queries.calls[-1], ("logs", {
            "kinds": ["receive", "send"], "keyword": "你好", "chat_id": "群1",
            "plugin": "echo", "since": "2026-01-02T00:00:00", "until": "2026-01-03T00:00:00",
            "limit": 20, "offset": 40}))

    def test_logs_endpoint_uses_defaults_without_filters(self):
        status, _ = self.handle("GET", "/api/logs")
        self.assertEqual(status, 200)
        self.assertEqual(self.queries.calls[-1], ("logs", {
            "kinds": None, "keyword": "", "chat_id": "", "plugin": "",
            "since": "", "until": "", "limit": 100, "offset": 0}))

    def test_logs_counts_returns_chinese_labels(self):
        self.queries.totals = {"全部": 1284, "receive": 902, "send": 361, "preview": 3,
                               "plugin": 312, "error": 4, "system": 21}
        status, payload = self.handle("GET", "/api/logs/counts")
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"], {
            "全部": 1284, "收到": 902, "发出": 361, "预览": 3,
            "插件处理": 312, "异常": 4, "系统": 21})
        # 每个类型一次轻量查询；「全部」不带 kinds 过滤。
        kinds_arg = [call[1]["kinds"] for call in self.queries.calls if call[0] == "logs"]
        self.assertEqual(kinds_arg, [None, ["receive"], ["send"], ["preview"],
                                     ["plugin"], ["error"], ["system"]])

    # --- 调试 ---

    def test_debug_endpoint_requires_chat_id_and_text(self):
        status, payload = self.handle("POST", "/api/debug", body={})
        self.assertEqual(status, 400)
        self.assertIn("chat_id", payload["error"])
        status, payload = self.handle("POST", "/api/debug", body={"chat_id": "群1"})
        self.assertEqual(status, 400)
        self.assertIn("text", payload["error"])
        self.assertEqual(self.debugger.calls, [])

    def test_debug_endpoint_runs_debugger_with_body(self):
        status, payload = self.handle("POST", "/api/debug", body={
            "plugin": "echo", "chat_id": "群1", "sender_id": "wxid_a",
            "text": "你好", "send_for_real": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload, self.debugger.result)
        self.assertEqual(self.debugger.calls, [{
            "plugin": "echo", "chat_id": "群1", "sender_id": "wxid_a",
            "text": "你好", "send_for_real": True}])

    def test_debug_endpoint_reports_busy_as_409(self):
        self.debugger.error = RuntimeError("已有调试在进行")
        status, payload = self.handle("POST", "/api/debug",
                                      body={"chat_id": "群1", "text": "你好"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "已有调试在进行")

    def test_debug_endpoint_reports_disabled_as_400(self):
        self.debugger.error = RuntimeError("管理台未启用，无法调试")
        status, payload = self.handle("POST", "/api/debug",
                                      body={"chat_id": "群1", "text": "你好"})
        self.assertEqual(status, 400)
        self.assertIn("无法调试", payload["error"])

    # --- 定时任务 ---

    def test_get_tasks_returns_scheduler_rows(self):
        status, payload = self.handle("GET", "/api/tasks")
        self.assertEqual((status, payload["tasks"]), (200, self.scheduler.status()))

    def test_get_tasks_keeps_config_error_column(self):
        # 手改配置文件写出非法时间时，状态行里的 config_error 要带给页面显示原因。
        self.scheduler.rows = [{"plugin": "demo", "name": "daily", "schedule": "",
                                "state": "待触发", "enabled": True, "registered": True,
                                "config_error": "每天 25:00 不是合法的时间表达式"}]
        status, payload = self.handle("GET", "/api/tasks")
        self.assertEqual(status, 200)
        self.assertIn("25:00", payload["tasks"][0]["config_error"])

    def test_get_tasks_reports_missing_scheduler(self):
        # 调度器未注入时按既有 RuntimeError 口径返回 400 原文（503 只给「已关闭」那类）。
        api = AdminApi(store=object(), queries=self.queries, plugins=self.plugins,
                       instruments=object(), debugger=self.debugger,
                       plugin_dir=self.plugin_dir, bot_config_path=self.bot_config_path,
                       config_loader=lambda: self.bot_config, today=lambda: TODAY)
        status, payload = api.handle("GET", "/api/tasks", {}, {})
        self.assertEqual(status, 400)
        self.assertIn("调度器未启用", payload["error"])

    def test_post_task_toggle_reports_missing_scheduler(self):
        # 未注入调度器时 POST 任务分支也要给干净 400，而不是 AttributeError 500。
        api = AdminApi(store=object(), queries=self.queries, plugins=self.plugins,
                       instruments=object(), debugger=self.debugger,
                       plugin_dir=self.plugin_dir, bot_config_path=self.bot_config_path,
                       config_loader=lambda: self.bot_config, today=lambda: TODAY)
        status, payload = api.handle("POST", "/api/tasks/demo/daily/toggle", {},
                                     {"enabled": False})
        self.assertEqual(status, 400)
        self.assertIn("调度器未启用", payload["error"])

    def test_post_task_toggle_passes_enabled_flag(self):
        status, _ = self.handle("POST", "/api/tasks/demo/daily/toggle", body={"enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(self.task_calls_of("set_enabled"), [("set_enabled", "demo", "daily", False)])

    def test_post_task_toggle_requires_boolean_enabled(self):
        status, payload = self.handle("POST", "/api/tasks/demo/daily/toggle", body={})
        self.assertEqual(status, 400)
        self.assertIn("enabled", payload["error"])
        self.assertEqual(self.scheduler.calls, [])

    def test_post_task_schedule_rejects_invalid_expression_as_400(self):
        self.scheduler.error = ValueError("每天 25:00 不是合法的时间表达式")
        status, payload = self.handle("POST", "/api/tasks/demo/daily/schedule",
                                      body={"schedule": "每天 25:00"})
        self.assertEqual(status, 400)
        self.assertIn("25:00", payload["error"])

    def test_post_task_schedule_passes_expression_through(self):
        status, _ = self.handle("POST", "/api/tasks/demo/daily/schedule",
                                body={"schedule": "工作日 09:00"})
        self.assertEqual(status, 200)
        self.assertEqual(self.task_calls_of("set_schedule"),
                         [("set_schedule", "demo", "daily", "工作日 09:00")])

    def test_post_task_run_and_unknown_path(self):
        status, payload = self.handle("POST", "/api/tasks/demo/daily/run")
        self.assertEqual((status, self.scheduler.calls[-1]), (200, ("run_now", "demo", "daily")))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["dispatched"])
        status, payload = self.handle("POST", "/api/tasks/demo/daily/explode")
        self.assertEqual(status, 400)
        self.assertIn("接口不存在", payload["error"])

    def test_post_task_run_reports_rejected_dispatch(self):
        # 投递被拒（如上一轮还在跑）不是失败状态码，但必须能与已投递区分。
        self.scheduler.run_result = False
        status, payload = self.handle("POST", "/api/tasks/demo/daily/run")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dispatched"])

    def test_post_task_rejects_bad_plugin_name_and_missing_segments(self):
        status, payload = self.handle("POST", "/api/tasks/Bad/daily/toggle", body={"enabled": True})
        self.assertEqual(status, 400)
        self.assertTrue(payload["error"])
        status, payload = self.handle("POST", "/api/tasks/demo", body={})
        self.assertEqual(status, 400)
        self.assertIn("接口不存在", payload["error"])

    # --- 未知路由 ---

    def test_unknown_route_returns_404(self):
        status, payload = self.handle("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertIn("接口不存在", payload["error"])
        status, payload = self.handle("DELETE", "/api/plugins")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
