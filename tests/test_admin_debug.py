"""管理台调试页后端：喂一条假消息、只走插件分发，预览拦截并汇聚结果。

两个命门分别用假后端钉住：
- 调试回复不能被真的发进会话（预览拦截）；
- 预览窗口内并发到达的真实消息的回复必须照常发出（不能被试跑吞掉）。

调试还必须绕开 `BotApplication._emit`：不写成员库、不做管理员判定、不记「收到」。
"""
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from wxbot_framework.admin.debug import Debugger
from wxbot_framework.admin.instrument import TRACEBACK_LIMIT, TRACEBACK_OMITTED, Instruments
from wxbot_framework.admin.store import AdminStore
from wxbot_framework.backend import CAPABILITIES
from wxbot_framework.config import ConfigStore
from wxbot_framework.plugins import PluginManager
from wxbot_framework.sdk import Message, ServiceFacade

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
GROUP = "12345678@chatroom"
DIRECT = "wxid_direct"
SENDER = "wxid_sender"

# 回调里记下收到的消息，供用例核对假消息的字段与调试标记。
ECHO_SOURCE = """
class Plugin:
    def __init__(self):
        self.seen = []

    def on_message(self, message, ctx):
        self.seen.append(message)
        ctx.reply(message, "回显：" + message.text)
"""

SILENT_SOURCE = """
class Plugin:
    def __init__(self):
        self.seen = []

    def on_message(self, message, ctx):
        self.seen.append(message)
"""

BROKEN_SOURCE = """
class Plugin:
    def on_message(self, message, ctx):
        raise ValueError("插件坏了")
"""

SLOW_SOURCE = """
import time


class Plugin:
    def on_message(self, message, ctx):
        time.sleep(2)
        ctx.reply(message, "慢回复：" + message.text)
"""

LATE_SOURCE = """
import time


class Plugin:
    def on_message(self, message, ctx):
        time.sleep(0.6)
        ctx.reply(message, "迟到回复：" + message.text)
"""

GATED_SOURCE = """
import threading


class Plugin:
    # 用例用它把回调卡在预览窗口里：等真实消息发出后再放行。
    gate = threading.Event()

    def on_message(self, message, ctx):
        self.gate.wait(5)
        ctx.reply(message, "门后回复：" + message.text)
"""


def _wait_until(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("等待条件超时")


class _SendBackend:
    """只记录发送结果的内存后端；加锁便于跨线程断言。"""

    def __init__(self):
        self.session_id = "admin-session"
        self.sent = []
        self._lock = threading.Lock()

    def send_text(self, chat_id, text):
        with self._lock:
            self.sent.append((chat_id, text))
        return True

    def stop(self):
        return True


class _CountingCalls:
    """任何属性访问都返回计数函数：用来断言调试路径没有碰成员库与管理员判定。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def hook(*args, **kwargs):
            self.calls.append(name)

        return hook


class _DebugFixture(unittest.TestCase):
    """真实的 PluginManager + 假后端；调试对象由用例自己构造。"""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        (self.root / "plugins").mkdir()
        self.config_path = self.root / "bot_config.json"
        self.config = {"plugins": {}}
        self.write_config()
        self.store = ConfigStore(self.config_path)
        self.admin_store = AdminStore(self.root / "admin_data.sqlite3")
        self.admin_store.open()
        self.addCleanup(self.admin_store.close)
        self.instruments = Instruments(store=self.admin_store)
        self.backend = _SendBackend()
        self.service = ServiceFacade(text_delay={"enabled": False}, instruments=self.instruments)
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        # 调试换掉真实成员库与管理员判定：只允许计数，不允许被调用。
        self.members = _CountingCalls()
        self.admins = _CountingCalls()
        self.manager = PluginManager(self.root / "plugins", self.store, self.service, CAPABILITIES,
                                     member_store=self.members, admins=self.admins,
                                     instruments=self.instruments)
        self.addCleanup(self.manager.shutdown)
        # 插件回调里的异常堆栈仍走原来的文本日志，这里只关心记录，屏蔽日志噪音。
        self.previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, self.previous_logging)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")

    def add_plugin(self, name, source, *, enabled=True):
        directory = self.root / "plugins" / name
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text(source, encoding="utf-8")
        (directory / "plugin.json").write_text(
            json.dumps({"api": "v1", "concurrency": "serial"}), encoding="utf-8")
        self.config["plugins"][name] = {"enabled": enabled}
        self.write_config()

    def debugger(self, *, timeout=3.0):
        return Debugger(self.manager, self.instruments, timeout=timeout)

    def admin_kinds(self):
        """落盘后取出统计库里的记录类型，用来核对调试没有记「收到」。"""
        self.admin_store.flush()
        db = self.admin_store._connection()
        return [row["kind"] for row in db.execute("SELECT kind FROM message_log ORDER BY id")]

    def admin_rows(self, kind):
        self.admin_store.flush()
        db = self.admin_store._connection()
        return [dict(row) for row in db.execute(
            "SELECT kind,chat_id,plugin,summary FROM message_log WHERE kind=? ORDER BY id", (kind,))]


class AdminDebugTests(_DebugFixture):
    def test_synthetic_message_does_not_touch_member_store_or_admins(self):
        self.add_plugin("echo", ECHO_SOURCE)
        self.manager.reload()
        result = self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="你好")
        self.assertEqual(result["plugins"][0]["reply"], "回显：你好")
        # 只走插件分发：不写成员库、不做管理员判定、也不记「收到」。
        self.assertEqual(self.members.calls, [])
        self.assertEqual(self.admins.calls, [])
        self.assertNotIn("receive", self.admin_kinds())

    def test_debug_does_not_pollute_plugin_or_error_counts(self):
        """调试试跑只进内存收集器：库里不留 plugin/error 行，累计也不变。"""
        self.add_plugin("echo", ECHO_SOURCE)
        self.add_plugin("broken", BROKEN_SOURCE)
        self.manager.reload()
        before = self.admin_store._connection().execute(
            "SELECT COALESCE(SUM(value),0) FROM stats_total WHERE scope='plugin'").fetchone()[0]
        result = self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="你好")
        # 调试结果仍能列出命中插件与将回复的文本（collect 路径不受影响）。
        entries = {entry["name"]: entry for entry in result["plugins"]}
        self.assertEqual(entries["echo"]["reply"], "回显：你好")
        self.assertEqual(entries["broken"]["state"], "error")
        # 库里没有该插件名下的 plugin/error 行，plugin 维度的累计也不变。
        self.admin_store.flush()
        db = self.admin_store._connection()
        kinds = [row["kind"] for row in db.execute("SELECT kind FROM message_log")]
        self.assertNotIn("plugin", kinds)
        self.assertNotIn("error", kinds)
        after = db.execute(
            "SELECT COALESCE(SUM(value),0) FROM stats_total WHERE scope='plugin'").fetchone()[0]
        self.assertEqual(after, before)

    def test_preview_reply_is_captured_not_sent(self):
        self.add_plugin("echo", ECHO_SOURCE)
        self.manager.reload()
        result = self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="你好")
        entry = result["plugins"][0]
        self.assertEqual((entry["name"], entry["state"], entry["reply"]),
                         ("echo", "replied", "回显：你好"))
        self.assertEqual(entry["error"], "")
        self.assertGreaterEqual(entry["ms"], 0.0)
        self.assertEqual(result["replied"], 1)
        self.assertFalse(result["sent_for_real"])
        # 预览拦截：假后端一条都没收到，统计库只留一条预览记录。
        self.assertEqual(self.backend.sent, [])
        self.assertIn("preview", self.admin_kinds())
        self.assertNotIn("send", self.admin_kinds())
        self.assertFalse(self.instruments.previewing)
        # 假消息按群会话构造。
        self.assertTrue(self.manager.records["echo"].instance.seen[0].is_group)

    def test_silent_plugins_are_listed(self):
        for name in ("a1", "a2", "a3"):
            self.add_plugin(name, SILENT_SOURCE)
        self.manager.reload()
        result = self.debugger().run(chat_id=DIRECT, sender_id=SENDER, text="你好")
        self.assertEqual(result["dispatched"], 3)
        self.assertEqual(result["replied"], 0)
        self.assertEqual([entry["name"] for entry in result["plugins"]], ["a1", "a2", "a3"])
        self.assertEqual([entry["state"] for entry in result["plugins"]], ["silent"] * 3)
        self.assertTrue(all(entry["reply"] == "" for entry in result["plugins"]))
        self.assertEqual(self.backend.sent, [])

    def test_error_plugin_is_reported_with_traceback_summary(self):
        self.add_plugin("broken", BROKEN_SOURCE)
        self.manager.reload()
        result = self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="你好")
        entry = result["plugins"][0]
        self.assertEqual(entry["state"], "error")
        self.assertIn("ValueError", entry["error"])
        self.assertIn("插件坏了", entry["error"])
        self.assertEqual(entry["reply"], "")
        self.assertEqual(result["replied"], 0)
        self.assertEqual(self.backend.sent, [])

    def test_error_entry_carries_traceback_text(self):
        """报错的插件要在结果里带上堆栈文本，页面才谈得上「展开堆栈」。"""
        self.add_plugin("broken", BROKEN_SOURCE)
        self.add_plugin("echo", ECHO_SOURCE)
        self.manager.reload()
        result = self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="你好")
        entries = {entry["name"]: entry for entry in result["plugins"]}
        stack = entries["broken"]["traceback"]
        # 堆栈里有异常类型与消息，也有真正抛错的那一帧（插件文件里的 on_message）。
        self.assertIn("ValueError", stack)
        self.assertIn("插件坏了", stack)
        self.assertIn("on_message", stack)
        self.assertIn("broken", stack)
        self.assertLessEqual(len(stack), TRACEBACK_LIMIT)
        # 没报错的条目也带这个键，值是空串：前端不必判 undefined。
        self.assertEqual(entries["echo"]["traceback"], "")
        self.assertEqual(entries["echo"]["state"], "replied")

    def test_long_traceback_is_truncated(self):
        """超大堆栈不能原样塞进响应：裁剪到上限，并留下省略提示。"""
        self.add_plugin("loud", "class Plugin:\n"
                                "    def on_message(self, message, ctx):\n"
                                "        raise ValueError('长' * 6000)\n")
        self.manager.reload()
        result = self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="你好")
        stack = result["plugins"][0]["traceback"]
        self.assertLessEqual(len(stack), TRACEBACK_LIMIT)
        self.assertTrue(stack.startswith(TRACEBACK_OMITTED))
        # 异常行自己超长时也要留住它的开头：异常类型不能丢。
        self.assertIn("ValueError", stack)

    def test_deep_traceback_keeps_innermost_frame_and_exception_line(self):
        """深栈触发裁剪后，展开的人仍要看到最内层帧与末尾那行「异常类型: 消息」。

        用「一层一个函数」的调用链构造深栈：同一行反复递归的帧会被 traceback 折叠成
        一条「[Previous line repeated ...]」，那样构造不出真正超长的堆栈。
        """
        steps = 150
        lines = ["class Plugin:",
                 "    def on_message(self, message, ctx):",
                 "        step_0(message)",
                 ""]
        for index in range(steps):
            lines.append(f"def step_{index}(message):")
            if index + 1 < steps:
                lines.append(f"    step_{index + 1}(message)")
            else:
                lines.append("    raise ValueError('演示：深栈')")
            lines.append("")
        self.add_plugin("deep", "\n".join(lines))
        self.manager.reload()
        result = self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="你好")
        stack = result["plugins"][0]["traceback"]
        self.assertLessEqual(len(stack), TRACEBACK_LIMIT)
        self.assertTrue(stack.startswith(TRACEBACK_OMITTED))
        # 末尾的异常行与最内层那一帧都在。
        self.assertIn("ValueError: 演示：深栈", stack)
        self.assertIn(f"in step_{steps - 1}", stack)
        # 前段（框架调用插件那一帧、最外层的 step_0）被省掉了。
        self.assertNotIn("in step_0", stack)
        self.assertNotIn("_run", stack)

    def test_filtering_to_single_plugin_skips_others(self):
        self.add_plugin("echo", ECHO_SOURCE)
        self.add_plugin("quiet", SILENT_SOURCE)
        self.manager.reload()
        result = self.debugger().run(plugin="echo", chat_id=GROUP, sender_id=SENDER, text="你好")
        self.assertEqual([entry["name"] for entry in result["plugins"]], ["echo"])
        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(result["plugins"][0]["state"], "replied")
        # 只投给 echo 的队列：quiet 的回调一次都没进。
        self.assertEqual(self.manager.records["quiet"].instance.seen, [])
        self.assertEqual(self.backend.sent, [])

    def test_plugin_timeout_is_reported_not_hung(self):
        self.add_plugin("slow", SLOW_SOURCE)
        self.manager.reload()
        started = time.monotonic()
        result = self.debugger(timeout=0.2).run(chat_id=GROUP, sender_id=SENDER, text="你好")
        elapsed = time.monotonic() - started
        entry = result["plugins"][0]
        self.assertEqual(entry["state"], "error")
        self.assertIn("超时", entry["error"])
        self.assertEqual(result["replied"], 0)
        # 超时不挂住调用方；慢回调随后自己跑完，不杀线程。
        self.assertLess(elapsed, 2.0)
        self.assertLess(result["ms"], 2000)
        # 清理时等慢回调收尾，避免它落在其它用例的清理窗口里。
        self.addCleanup(self.manager.records["slow"].queue.join)

    def test_late_reply_after_timeout_is_still_previewed(self):
        """超时后预览窗口已关，但慢插件迟到的回复必须仍被拦成预览、绝不进会话。"""
        self.add_plugin("slow", LATE_SOURCE)
        self.manager.reload()
        result = self.debugger(timeout=0.2).run(chat_id=GROUP, sender_id=SENDER, text="你好")
        self.assertEqual(result["plugins"][0]["state"], "error")
        self.assertIn("超时", result["plugins"][0]["error"])
        # 等回调真的跑完：那时预览窗口早已退出。
        self.manager.records["slow"].queue.join()
        self.assertFalse(self.instruments.previewing)
        self.assertEqual(self.backend.sent, [])
        self.assertNotIn("send", self.admin_kinds())
        # 迟到的回复被记成 slow 名下的预览记录。
        previews = self.admin_rows("preview")
        self.assertEqual([row["plugin"] for row in previews], ["slow"])
        self.assertIn("迟到回复：你好", previews[0]["summary"])

    def test_concurrent_run_is_rejected_with_unified_message(self):
        """串行化必须跨 Debugger 实例生效（真发运行也不例外），且文案与窗口互斥一致。"""
        self.add_plugin("gated", GATED_SOURCE)
        self.manager.reload()
        gated = self.manager.records["gated"]
        self.addCleanup(gated.instance.gate.set)
        first = self.debugger()
        worker = threading.Thread(target=lambda: first.run(
            plugin="gated", chat_id=DIRECT, sender_id=SENDER, text="第一条", send_for_real=True))
        worker.start()
        _wait_until(lambda: gated.queue.unfinished_tasks == 1)
        # 真发运行不进预览窗口；另一个实例（Task 8 可能每请求新建）仍必须被同一条互斥挡住。
        second = Debugger(self.manager, self.instruments, timeout=0.2)
        with self.assertRaises(RuntimeError) as caught:
            second.run(chat_id=GROUP, sender_id=SENDER, text="第二条")
        gated.instance.gate.set()
        worker.join(6)
        self.assertFalse(worker.is_alive())
        with self.assertRaises(RuntimeError) as window:
            with self.instruments.preview_scope():
                with self.instruments.preview_scope():
                    pass
        # 上层只看到一套文案。
        self.assertEqual(str(caught.exception), "已有调试在进行")
        self.assertEqual(str(caught.exception), str(window.exception))
        # 被挡下的第二条没有派发：只有真发运行那一条发出，没有留下预览记录。
        self.assertEqual(self.backend.sent, [(DIRECT, "门后回复：第一条")])
        self.assertNotIn("preview", self.admin_kinds())

    def test_concurrent_real_run_does_not_unblock_preview(self):
        """真发运行不会解除预览运行里迟到回复的拦截：决定随消息携带，不共享全局开关。"""
        self.add_plugin("gated", GATED_SOURCE)
        self.add_plugin("slow", LATE_SOURCE)
        self.manager.reload()
        gated = self.manager.records["gated"]
        self.addCleanup(gated.instance.gate.set)
        real = self.debugger()
        preview = Debugger(self.manager, self.instruments, timeout=0.2)
        worker = threading.Thread(target=lambda: real.run(
            plugin="gated", chat_id=DIRECT, sender_id=SENDER, text="真发", send_for_real=True))
        worker.start()
        _wait_until(lambda: gated.queue.unfinished_tasks == 1)
        with self.assertRaises(RuntimeError) as caught:
            preview.run(plugin="slow", chat_id=GROUP, sender_id=SENDER, text="预览")
        self.assertEqual(str(caught.exception), "已有调试在进行")
        gated.instance.gate.set()
        worker.join(6)
        # 真发运行的回复真的发出，且只发到它自己的调试会话。
        self.assertEqual(self.backend.sent, [(DIRECT, "门后回复：真发")])
        # 真发运行结束后预览运行才真正跑：超时先返回，迟到的回复仍必须被拦成预览。
        result = preview.run(plugin="slow", chat_id=GROUP, sender_id=SENDER, text="预览")
        self.assertEqual(result["plugins"][0]["state"], "error")
        self.manager.records["slow"].queue.join()
        self.assertEqual(self.backend.sent, [(DIRECT, "门后回复：真发")])
        previews = [row for row in self.admin_rows("preview") if row["plugin"] == "slow"]
        self.assertEqual(len(previews), 1)
        self.assertIn("迟到回复：预览", previews[0]["summary"])

    def test_send_for_real_only_sends_debug_reply(self):
        self.add_plugin("echo", ECHO_SOURCE)
        self.manager.reload()
        result = self.debugger().run(chat_id=DIRECT, sender_id=SENDER, text="你好", send_for_real=True)
        self.assertTrue(result["sent_for_real"])
        self.assertEqual(result["plugins"][0]["state"], "replied")
        self.assertEqual(result["plugins"][0]["reply"], "回显：你好")
        # 真发模式不进预览窗口：恰好一条、发到调试会话。
        self.assertEqual(self.backend.sent, [(DIRECT, "回显：你好")])
        self.assertFalse(self.instruments.previewing)
        # 假消息自身仍带调试标记，便于标识来源。
        message = self.manager.records["echo"].instance.seen[0]
        self.assertTrue(message.debug)
        self.assertEqual((message.chat_id, message.sender_id, message.type), (DIRECT, SENDER, 1))
        self.assertFalse(message.is_group)

    def test_concurrent_real_message_is_not_swallowed(self):
        self.add_plugin("gated", GATED_SOURCE)
        self.add_plugin("other", ECHO_SOURCE)
        self.manager.reload()
        gated = self.manager.records["gated"]
        self.addCleanup(gated.instance.gate.set)
        results = {}
        worker = threading.Thread(target=lambda: results.update(
            self.debugger().run(chat_id=GROUP, sender_id=SENDER, text="调试消息")))
        worker.start()
        # 调试消息已进 gated 的队列、回调卡在门后：预览窗口正开着。
        _wait_until(lambda: gated.queue.unfinished_tasks == 1)
        # 预览窗口内，另一条真实消息（不带调试标记）进别的插件：它的回复必须真的发出。
        self.manager.records["other"].enqueue(
            Message(text="真实消息", chat_id=DIRECT, sender_id=SENDER))
        _wait_until(lambda: self.backend.sent)
        self.assertTrue(self.instruments.previewing)
        gated.instance.gate.set()
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.backend.sent, [(DIRECT, "回显：真实消息")])
        entries = {entry["name"]: entry for entry in results["plugins"]}
        # 调试回复只被拦成预览、并按插件归集到调试结果里。
        self.assertEqual((entries["gated"]["state"], entries["gated"]["reply"]),
                         ("replied", "门后回复：调试消息"))
        self.assertEqual(entries["other"]["reply"], "回显：调试消息")

    def test_unknown_plugin_name_is_rejected(self):
        self.add_plugin("echo", ECHO_SOURCE)
        self.add_plugin("off", ECHO_SOURCE, enabled=False)
        self.manager.reload()
        debugger = self.debugger()
        with self.assertRaises(ValueError):
            debugger.run(plugin="nope", chat_id=GROUP, sender_id=SENDER, text="你好")
        # 插件存在但没在运行，同样拒绝。
        with self.assertRaises(ValueError):
            debugger.run(plugin="off", chat_id=GROUP, sender_id=SENDER, text="你好")
        self.assertEqual(self.backend.sent, [])

    def test_unattached_instruments_is_rejected(self):
        self.add_plugin("echo", ECHO_SOURCE)
        self.manager.reload()
        debugger = Debugger(self.manager, Instruments(), timeout=1.0)
        with self.assertRaises(RuntimeError) as caught:
            debugger.run(chat_id=GROUP, sender_id=SENDER, text="你好")
        # 未 attach 统计库时收不到任何记录，明确报错而不是静默返回空结果。
        self.assertIn("管理台未启用", str(caught.exception))
        self.assertEqual(self.backend.sent, [])


if __name__ == "__main__":
    unittest.main()
