"""管理台埋点上下文与记录验证：未启用时为空实现，启用后写入记录并自增累计。"""
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path

from wxbot_framework.admin.instrument import Instruments
from wxbot_framework.admin.store import AdminStore
from wxbot_framework.backend import CAPABILITIES
from wxbot_framework.plugins import PluginRecord, PluginSpec
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
GROUP = "12345678@chatroom"
DIRECT = "wxid_direct"
SENDER = "wxid_sender"


class FakeMessage:
    """只为埋点提供 message.chat_id / sender_id / is_group / text 这几个字段。"""

    def __init__(self, text="", chat_id="", sender_id="", is_group=False):
        self.text, self.chat_id, self.sender_id, self.is_group = text, chat_id, sender_id, is_group


class InstrumentsTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.store = AdminStore(self.root / "admin_data.sqlite3")
        self.store.open()
        self.instruments = Instruments(store=self.store)
        self.group_message = FakeMessage(text="群里的消息", chat_id=GROUP, sender_id=SENDER, is_group=True)
        self.direct_message = FakeMessage(text="私聊消息", chat_id=DIRECT, sender_id=SENDER, is_group=False)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def rows(self):
        """落盘后按写入顺序取出消息记录，字段只取断言需要的列。"""
        self.store.flush()
        db = self.store._connection()
        return [dict(row) for row in db.execute(
            "SELECT ts_utc,kind,chat_id,sender_id,plugin,is_group,allowed,summary FROM message_log ORDER BY id")]

    def totals(self):
        """落盘后把累计计数读成 {(scope, scope_key, metric): value}。"""
        self.store.flush()
        db = self.store._connection()
        return {(row["scope"], row["scope_key"], row["metric"]): row["value"]
                for row in db.execute("SELECT scope,scope_key,metric,value FROM stats_total")}

    def test_disabled_instruments_drop_everything(self):
        instruments = Instruments()
        self.assertFalse(instruments.enabled)
        instruments.record_receive(self.group_message, allowed=True)
        instruments.record_send(GROUP, "你好")
        with instruments.plugin_scope("echo", message=self.group_message):
            instruments.record_plugin("echo", handled=True, ms=1.0, replied=True)
            instruments.record_error("echo", ValueError("坏了"))
        instruments.record_system("WARNING", "Bot", "测试日志")
        self.assertFalse(instruments.previewing)
        # 未 attach 统计库：不产生任何记录、也不自增任何累计。
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.totals(), {})

    def test_enabled_instruments_write_expected_records(self):
        self.assertTrue(self.instruments.enabled)
        self.instruments.record_receive(self.group_message, allowed=True)
        self.instruments.record_send(GROUP, "你好", plugin="echo")
        with self.instruments.plugin_scope("echo", message=self.group_message):
            self.instruments.record_plugin("echo", handled=True, ms=1.5, replied=False)
            self.instruments.record_error("echo", ValueError("坏了"))
        rows = self.rows()
        self.assertEqual([row["kind"] for row in rows], ["receive", "send", "plugin", "error"])
        receive, send, plugin, error = rows
        self.assertEqual((receive["chat_id"], receive["sender_id"], receive["is_group"], receive["allowed"]),
                         (GROUP, SENDER, 1, 1))
        self.assertEqual(receive["summary"], "群里的消息")
        self.assertEqual((send["chat_id"], send["plugin"], send["is_group"], send["allowed"], send["summary"]),
                         (GROUP, "echo", 1, 1, "你好"))
        self.assertEqual((plugin["chat_id"], plugin["plugin"], plugin["is_group"]), (GROUP, "echo", 1))
        self.assertEqual((error["chat_id"], error["plugin"], error["is_group"]), (GROUP, "echo", 1))
        self.assertIn("ValueError", error["summary"])
        self.assertTrue(all(row["ts_utc"] for row in rows))

    def test_current_plugin_is_thread_local(self):
        self.assertEqual(self.instruments.current_plugin, "")
        seen = []
        with self.instruments.plugin_scope("echo", message=self.group_message):
            self.assertEqual(self.instruments.current_plugin, "echo")
            thread = threading.Thread(target=lambda: seen.append(self.instruments.current_plugin))
            thread.start()
            thread.join()
        self.assertEqual(seen, [""])
        self.assertEqual(self.instruments.current_plugin, "")

    def test_record_send_marks_preview_rows(self):
        self.instruments.record_send(GROUP, "预览内容", preview=True)
        preview = self.rows()[0]
        # 预览单列一类，摘要保留 [预览] 前缀。
        self.assertEqual(preview["kind"], "preview")
        self.assertTrue(preview["summary"].startswith("[预览] "))
        self.assertIn("预览内容", preview["summary"])
        # 预览是调试试跑、没有真的发出：一条累计都不加。
        self.assertEqual(self.totals(), {})
        self.instruments.record_send(GROUP, "真实内容")
        sent = self.rows()[1]
        self.assertEqual(sent["kind"], "send")
        self.assertFalse(sent["summary"].startswith("[预览] "))
        self.assertEqual(self.totals(), {("global", "", "sent"): 1, ("group", GROUP, "sent"): 1})

    def test_previewing_flag_is_scoped(self):
        self.assertFalse(self.instruments.previewing)
        with self.instruments.preview_scope():
            self.assertTrue(self.instruments.previewing)
            # 同一时刻只允许一个调试请求。
            with self.assertRaises(RuntimeError):
                with self.instruments.preview_scope():
                    pass
        self.assertFalse(self.instruments.previewing)
        # 异常退出也要复位。
        with self.assertRaises(RuntimeError):
            with self.instruments.preview_scope():
                raise RuntimeError("回调出错")
        self.assertFalse(self.instruments.previewing)

    def test_plugin_scope_reports_replied(self):
        with self.instruments.plugin_scope("echo", message=self.group_message) as flag:
            self.assertFalse(flag.replied)
            self.instruments.mark_replied()
            self.assertTrue(flag.replied)
            self.instruments.record_plugin("echo", handled=True, ms=1.0, replied=flag.replied)
        self.assertIn("已回复", self.rows()[0]["summary"])
        # 没有成功发送过就是「未回复」。
        with self.instruments.plugin_scope("echo", message=self.group_message):
            self.instruments.record_plugin("echo", handled=True, ms=1.0, replied=False)
        self.assertIn("未回复", self.rows()[1]["summary"])
        # replied 也可由调用方给的可调用口径决定。
        with self.instruments.plugin_scope("echo", message=self.group_message, replied=lambda: True) as flag:
            self.assertTrue(flag.replied)

    def test_collect_captures_records_and_stops_after_exit(self):
        captured = []
        with self.instruments.collect(captured):
            self.instruments.record_receive(self.group_message, allowed=True)
            self.instruments.record_send(GROUP, "你好")
            self.assertEqual([record.kind for record in captured], ["receive", "send"])
            self.assertEqual(captured[0].chat_id, GROUP)
        self.instruments.record_receive(self.group_message, allowed=True)
        self.assertEqual(len(captured), 2)

    def test_record_receive_bumps_global_and_group_totals(self):
        self.instruments.record_receive(self.group_message, allowed=True)
        self.assertEqual(self.totals(), {("global", "", "received"): 1, ("group", GROUP, "received"): 1})
        # 私聊只 bump 全局维度。
        self.instruments.record_receive(self.direct_message, allowed=True)
        self.assertEqual(self.totals(), {("global", "", "received"): 2, ("group", GROUP, "received"): 1})

    def test_denied_receive_is_logged_but_not_counted(self):
        self.instruments.record_receive(self.group_message, allowed=False)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kind"], rows[0]["allowed"], rows[0]["is_group"]), ("receive", 0, 1))
        # 白名单外与「今日」的口径一致：一条累计都不加。
        self.assertEqual(self.totals(), {})

    def test_plugin_and_error_records_carry_is_group(self):
        with self.instruments.plugin_scope("echo", message=self.group_message):
            self.instruments.record_plugin("echo", handled=True, ms=1.0, replied=False)
            self.instruments.record_error("echo", ValueError("群里的异常"))
        with self.instruments.plugin_scope("echo", message=self.direct_message):
            self.instruments.record_plugin("echo", handled=True, ms=1.0, replied=False)
            self.instruments.record_error("echo", ValueError("私聊的异常"))
        rows = self.rows()
        self.assertEqual([(row["kind"], row["chat_id"], row["is_group"]) for row in rows],
                         [("plugin", GROUP, 1), ("error", GROUP, 1), ("plugin", DIRECT, 0), ("error", DIRECT, 0)])

    def test_record_send_and_plugin_results_bump_their_totals(self):
        self.instruments.record_send(GROUP, "群回复")
        self.instruments.record_send(DIRECT, "私聊回复")
        with self.instruments.plugin_scope("echo", message=self.group_message):
            self.instruments.record_plugin("echo", handled=True, ms=1.0, replied=False)
            self.instruments.record_error("echo", ValueError("坏了"))
        self.assertEqual(self.totals(), {
            ("global", "", "sent"): 2,
            ("group", GROUP, "sent"): 1,
            ("plugin", "echo", "handled"): 1,
            ("plugin", "echo", "errors"): 1,
        })


def _wait_until(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("等待插件处理完成超时")


class _SendBackend:
    """只记录发送结果的内存后端。"""

    def __init__(self):
        self.session_id = "admin-session"
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class _ProbePlugin:
    """可控回调：正文为「沉默」时不回复，否则回一条固定文本。"""

    def __init__(self, error=None):
        self.error = error

    def on_message(self, message, ctx):
        if self.error is not None:
            raise self.error
        if message.text != "沉默":
            ctx.reply(message, "已收到")


class MessagePathInstrumentsTests(unittest.TestCase):
    """消息路径接入埋点后的记录行为：发送收口、插件回调与预览拦截。"""

    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.store = AdminStore(self.root / "admin_data.sqlite3")
        self.store.open()
        # 清理按注册顺序倒序执行：先关库再删临时目录，Windows 上文件被占用会删不掉。
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.store.close)
        # 插件回调里的异常堆栈仍走原来的文本日志，这里只关心记录，屏蔽日志噪音。
        self.previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, self.previous_logging)
        self.instruments = Instruments(store=self.store)
        self.backend = _SendBackend()
        self.service = ServiceFacade(text_delay={"enabled": False}, instruments=self.instruments)
        self.service.bind(self.backend)

    def rows(self):
        self.store.flush()
        db = self.store._connection()
        return [dict(row) for row in db.execute(
            "SELECT kind,chat_id,sender_id,plugin,is_group,allowed,summary FROM message_log ORDER BY id")]

    def totals(self):
        self.store.flush()
        db = self.store._connection()
        return {(row["scope"], row["scope_key"], row["metric"]): row["value"]
                for row in db.execute("SELECT scope,scope_key,metric,value FROM stats_total")}

    def start_record(self, name="probe", error=None):
        """起一个真实的插件记录线程；它拿到的是接入了埋点的发送收口。"""
        spec = PluginSpec(name, self.root / f"{name}.py", "v1", "", frozenset())
        context = PluginContext(name, {}, self.service, CAPABILITIES)
        record = PluginRecord(spec, _ProbePlugin(error), context, f"_probe_{name}", 8,
                              instruments=self.instruments)
        self.addCleanup(record.join)
        self.addCleanup(record.deactivate, force=True)
        record.thread.start()
        return record

    def test_send_preview_is_recorded_and_not_sent(self):
        with self.instruments.preview_scope():
            self.assertTrue(self.service.send_text(GROUP, "预览内容"))
        # 预览不调后端、返回成功、记一条带 [预览] 前缀且不计累计的记录。
        self.assertEqual(self.backend.sent, [])
        rows = self.rows()
        self.assertEqual(rows[0]["kind"], "preview")
        self.assertTrue(rows[0]["summary"].startswith("[预览] "))
        self.assertIn("预览内容", rows[0]["summary"])
        self.assertEqual(self.totals(), {})

    def test_real_send_not_swallowed_during_preview(self):
        results = []

        def real_send():
            results.append(self.service.send_text(DIRECT, "真实回复"))

        with self.instruments.preview_scope():
            self.assertTrue(self.service.send_text(DIRECT, "预览"))
            worker = threading.Thread(target=real_send)
            worker.start()
            worker.join()
        # 预览期间另一线程的真实发送必须真的发出去。
        self.assertEqual(results, [True])
        self.assertEqual(self.backend.sent, [(DIRECT, "真实回复")])
        self.assertEqual([row["kind"] for row in self.rows()], ["preview", "send"])

    def test_plugin_run_records_handled_and_replied(self):
        record = self.start_record()
        record.enqueue(Message(text="回复", chat_id=GROUP, sender_id=SENDER, is_group=True))
        _wait_until(lambda: record.queue.unfinished_tasks == 0)
        record.enqueue(Message(text="沉默", chat_id=DIRECT, sender_id=SENDER))
        _wait_until(lambda: record.queue.unfinished_tasks == 0)
        plugins = [row for row in self.rows() if row["kind"] == "plugin"]
        self.assertEqual([(row["chat_id"], row["is_group"], row["plugin"]) for row in plugins],
                         [(GROUP, 1, "probe"), (DIRECT, 0, "probe")])
        # 回调里成功发送过 → 已回复；没发送 → 未回复。
        self.assertIn("已回复", plugins[0]["summary"])
        self.assertIn("未回复", plugins[1]["summary"])
        self.assertEqual(self.totals()[("plugin", "probe", "handled")], 2)
        sends = [row for row in self.rows() if row["kind"] == "send"]
        self.assertEqual([(row["chat_id"], row["plugin"]) for row in sends], [(GROUP, "probe")])

    def test_plugin_error_still_recorded_and_swallowed(self):
        record = self.start_record(error=ValueError("插件坏了"))
        for _ in range(2):
            record.enqueue(Message(text="回复", chat_id=GROUP, sender_id=SENDER, is_group=True))
            _wait_until(lambda: record.queue.unfinished_tasks == 0)
        errors = [row for row in self.rows() if row["kind"] == "error"]
        self.assertEqual(len(errors), 2)
        self.assertEqual((errors[0]["chat_id"], errors[0]["plugin"]), (GROUP, "probe"))
        self.assertIn("ValueError", errors[0]["summary"])
        self.assertEqual((record.handled, record.errors), (0, 2))
        self.assertEqual(self.totals()[("plugin", "probe", "errors")], 2)
        # 异常被吞掉：线程继续处理后续消息。
        self.assertTrue(record.thread.is_alive())

    def test_debug_message_reply_is_previewed_in_plugin_thread(self):
        debug_record = self.start_record("probe")
        real_record = self.start_record("other")
        with self.instruments.preview_scope():
            # 调试假消息带 debug 标记，插件线程里它自己的回复要被拦成预览。
            debug_record.enqueue(Message(text="回复", chat_id=GROUP, sender_id=SENDER, is_group=True,
                                         debug=True))
            # 预览窗口内并发到达的真实消息不带标记，回复必须真的发出。
            real_record.enqueue(Message(text="回复", chat_id=DIRECT, sender_id=SENDER))
            _wait_until(lambda: debug_record.queue.unfinished_tasks == 0)
            _wait_until(lambda: real_record.queue.unfinished_tasks == 0)
        self.assertEqual(self.backend.sent, [(DIRECT, "已收到")])
        previews = [row for row in self.rows() if row["kind"] == "preview"]
        self.assertEqual([(row["plugin"], row["chat_id"]) for row in previews], [("probe", GROUP)])
        self.assertIn("已收到", previews[0]["summary"])


if __name__ == "__main__":
    unittest.main()
