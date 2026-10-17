"""任务在插件线程上执行；on_load 注册、配置覆盖与加载期约束。"""
from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from wxbot_framework.config import ConfigStore
from wxbot_framework.plugins import JOB_QUEUE_SIZE, PluginManager, PluginRecord, PluginSpec
from wxbot_framework.scheduler import Scheduler, TaskJob
from wxbot_framework.sdk import Message

ROOT = Path(__file__).resolve().parent.parent


def message_with_text(text):
    return Message(text=text, chat_id="room")


class _OrderScheduler:
    """记录进入/离开，用来验证任务回调与消息回调不重叠。"""
    def __init__(self, order=None, delay=0.0):
        self.order = order if order is not None else []
        self.delay = delay
    def run_job(self, job):
        self.order.append(("job-enter", job.name))
        time.sleep(self.delay)
        self.order.append(("job-exit", job.name))


class _BusyInstance:
    def __init__(self, order):
        self.order = order
    def on_message(self, message, ctx):
        self.order.append(("msg-enter", message.text))
        time.sleep(0.05)
        self.order.append(("msg-exit", message.text))


class PluginJobChannelTests(unittest.TestCase):
    def make_record(self, order=None, scheduler=None, queue_size=8):
        spec = PluginSpec("demo", Path("demo/__init__.py"), "v1", "", frozenset())
        context = SimpleNamespace(logger=logging.getLogger("Plugin.demo"))
        instance = _BusyInstance(order if order is not None else [])
        return PluginRecord(spec, instance, context, "_wxbot_plugins.demo_x", queue_size,
                            scheduler=scheduler)

    def test_enqueue_job_is_rejected_when_inactive_or_full(self):
        record = self.make_record()
        for index in range(JOB_QUEUE_SIZE):
            self.assertTrue(record.enqueue_job(TaskJob("demo", f"job{index}")))
        self.assertFalse(record.enqueue_job(TaskJob("demo", "extra")))   # 队列满
        self.assertEqual(record.job_dropped, 1)
        record.active = False
        self.assertFalse(record.enqueue_job(TaskJob("demo", "inactive")))

    def test_jobs_and_messages_never_overlap(self):
        order = []
        scheduler = _OrderScheduler(order, delay=0.05)
        record = self.make_record(order=order, scheduler=scheduler)
        record.thread.start()
        try:
            record.enqueue_job(TaskJob("demo", "nightly"))
            record.enqueue(message_with_text("t1"))
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(order) < 4:
                time.sleep(0.01)
        finally:
            record.deactivate(force=True)
            record.join()
        # 顺序可以是任意一种，但 enter 与 exit 必须成对相邻，不出现交叉。
        self.assertEqual(order[0][0].split("-")[0], order[1][0].split("-")[0])
        self.assertEqual(order[2][0].split("-")[0], order[3][0].split("-")[0])

    def test_job_failure_does_not_touch_message_counters(self):
        scheduler = _OrderScheduler()
        record = self.make_record(scheduler=scheduler)
        record.thread.start()
        try:
            record.enqueue_job(TaskJob("demo", "a"))
            time.sleep(0.2)
        finally:
            record.deactivate(force=True)
            record.join()
        self.assertEqual((record.handled, record.errors), (0, 0))


PLUGIN_SOURCE = '''
import logging
class Plugin:
    def on_load(self, ctx):
        ctx.logger.info("loaded")
        ctx.tasks.register("daily", "每天 21:00", lambda: None)
    def on_message(self, message, ctx):
        pass
'''

NAUGHTY_SOURCE = '''
class Plugin:
    def on_load(self, ctx):
        self.ctx = ctx
    def on_message(self, message, ctx):
        self.ctx.tasks.register("late", "每天 21:00", lambda: None)
'''


class PluginRegistrationTests(unittest.TestCase):
    def make_manager(self, source=PLUGIN_SOURCE, config=None):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "plugins/demo").mkdir(parents=True)
        (root / "plugins/demo/__init__.py").write_text(source, encoding="utf-8")
        path = root / "bot_config.json"
        path.write_text(json.dumps(config or {"plugins": {"demo": {"enabled": True}}},
                                   ensure_ascii=False), encoding="utf-8")
        self.store = ConfigStore(path)
        self.scheduler = Scheduler(self.store)
        self.plugins = PluginManager(root / "plugins", self.store, Mock(), {"text.receive"},
                                     scheduler=self.scheduler)
        self.scheduler.bind(self.plugins.enqueue_job)
        return self.plugins

    def test_on_load_registration_shows_up_in_status(self):
        self.make_manager()
        self.plugins.reload()
        self.assertEqual([(row["plugin"], row["name"]) for row in self.scheduler.status()],
                         [("demo", "daily")])

    def test_config_schedule_override_wins_over_code_default(self):
        self.make_manager(config={"plugins": {"demo": {
            "enabled": True, "tasks": {"daily": {"schedule": "每天 09:00", "enabled": False}}}}})
        self.plugins.reload()
        row = self.scheduler.status()[0]
        self.assertEqual((row["schedule"], row["enabled"], row["source"]),
                         ("每天 09:00", False, "config"))

    def test_invalid_config_schedule_keeps_plugin_loaded(self):
        self.make_manager(config={"plugins": {"demo": {
            "enabled": True, "tasks": {"daily": {"schedule": "每天 25:00"}}}}})
        self.plugins.reload()                      # 插件其余功能不受影响
        self.assertEqual(self.scheduler.status()[0]["state"], "配置错误")
        self.assertTrue(self.plugins.is_active("demo"))
        self.assertEqual(self.plugins.load_errors, {})

    def test_config_declared_unknown_task_logs_warning(self):
        self.make_manager(config={"plugins": {"demo": {
            "enabled": True, "tasks": {"daily": {}, "ghost": {}}}}})
        with self.assertLogs("Plugins", level="WARNING") as logs:
            self.plugins.reload()
        self.assertIn("ghost", "\n".join(logs.output))
        self.assertEqual(sorted(row["name"] for row in self.scheduler.status()),
                         ["daily", "ghost"])

    def test_register_after_on_load_is_rejected(self):
        self.make_manager(source=NAUGHTY_SOURCE)
        self.plugins.reload()
        record = self.plugins.records["demo"]
        with self.assertRaisesRegex(RuntimeError, "只能在 on_load 期间注册"):
            record.instance.on_message(None, record.context)

    def test_disable_unregisters_tasks(self):
        self.make_manager()
        self.plugins.reload()
        self.plugins.disable("demo")
        self.assertEqual([row for row in self.scheduler.status() if row["registered"]], [])

    def test_manager_enqueue_job_rejects_unknown_plugin(self):
        self.make_manager()
        self.assertFalse(self.plugins.enqueue_job("demo", TaskJob("demo", "daily")))

    def test_manager_enqueue_job_rejects_inactive_plugin(self):
        self.make_manager()
        self.plugins.reload()
        record = self.plugins.records["demo"]
        record.deactivate()
        record.join()
        self.assertFalse(self.plugins.enqueue_job("demo", TaskJob("demo", "daily")))

    def test_manager_enqueue_job_rejects_when_scheduler_is_none(self):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "plugins").mkdir(parents=True)
        path = root / "bot_config.json"
        path.write_text(json.dumps({"plugins": {}}, ensure_ascii=False), encoding="utf-8")
        plugins = PluginManager(root / "plugins", ConfigStore(path), Mock(), {"text.receive"},
                                scheduler=None)
        self.assertFalse(plugins.enqueue_job("demo", TaskJob("demo", "daily")))


if __name__ == "__main__":
    unittest.main()
