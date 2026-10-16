"""管理台系统日志接入验证：框架 WARNING 及以上进入日志页的「系统」记录。

用例只挂到独立命名空间的 logger（`propagate=False`），避免写进真实日志、
也不把 handler 永久留在根 logger 上污染其它用例。
"""
import logging
import tempfile
import unittest
from pathlib import Path

from main import attach_admin_logging
from wxbot_framework.admin.handler import AdminLogHandler
from wxbot_framework.admin.instrument import Instruments
from wxbot_framework.admin.store import AdminStore

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"


class AdminLogHandlerTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.store = AdminStore(self.root / "admin_data.sqlite3")
        self.store.open()
        self.instruments = Instruments(store=self.store)
        # 独立命名空间、不向根传播：断言只面对本用例自己挂的 handler。
        self.logger = logging.getLogger("测试系统日志")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False
        self.handler = AdminLogHandler(self.instruments)
        self.logger.addHandler(self.handler)

    def tearDown(self):
        self.logger.removeHandler(self.handler)
        self.store.close()
        self.temporary.cleanup()

    def rows(self):
        """落盘后取出消息记录，只取断言需要的列。"""
        self.store.flush()
        db = self.store._connection()
        return [dict(row) for row in db.execute(
            "SELECT kind,plugin,summary FROM message_log ORDER BY id")]

    def test_warning_and_above_become_system_records(self):
        self.logger.warning("插件队列已满")
        self.logger.error("等待微信连接")
        rows = self.rows()
        self.assertEqual([row["kind"] for row in rows], ["system", "system"])
        self.assertIn("[WARNING]", rows[0]["summary"])
        self.assertIn("插件队列已满", rows[0]["summary"])
        self.assertIn("[ERROR]", rows[1]["summary"])
        self.assertIn("等待微信连接", rows[1]["summary"])

    def test_info_is_ignored(self):
        self.logger.info("普通信息不该进日志页")
        self.assertEqual(self.rows(), [])

    def test_handler_failure_does_not_break_logging(self):
        class BrokenInstruments:
            enabled = True

            def record_system(self, level, name, text):
                raise RuntimeError("埋点坏了")

        received = []

        class Recorder(logging.Handler):
            def emit(self, record):
                received.append(record.getMessage())

        logger = logging.getLogger("测试故障日志")
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        broken = AdminLogHandler(BrokenInstruments())
        recorder = Recorder()
        logger.addHandler(broken)
        logger.addHandler(recorder)
        try:
            logger.warning("照常记录")  # 埋点抛异常也不该阻断日志调用链
        finally:
            logger.removeHandler(broken)
            logger.removeHandler(recorder)
        self.assertEqual(received, ["照常记录"])

    def test_attach_admin_logging_adds_handler_when_enabled(self):
        root = logging.getLogger()
        before = list(root.handlers)
        previous_level = root.level
        added = []
        try:
            root.setLevel(logging.WARNING)
            attach_admin_logging(self.instruments)
            added = [handler for handler in root.handlers if handler not in before]
            self.assertEqual(len(added), 1)
            self.assertIsInstance(added[0], AdminLogHandler)
            logging.getLogger("框架启动").warning("插件队列已满")
        finally:
            for handler in added:
                root.removeHandler(handler)
            root.setLevel(previous_level)
        rows = self.rows()
        self.assertEqual([row["kind"] for row in rows], ["system"])
        self.assertIn("插件队列已满", rows[0]["summary"])

    def test_attach_admin_logging_is_noop_when_disabled_or_none(self):
        root = logging.getLogger()
        before = len(root.handlers)
        attach_admin_logging(None)
        attach_admin_logging(Instruments())  # 未启用（无 store）
        self.assertEqual(len(root.handlers), before)

    def test_attach_admin_logging_is_idempotent(self):
        root = logging.getLogger()
        before = list(root.handlers)
        added = []
        try:
            attach_admin_logging(self.instruments)
            attach_admin_logging(self.instruments)
            added = [handler for handler in root.handlers if handler not in before]
            self.assertEqual(len(added), 1)
        finally:
            for handler in added:
                root.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
