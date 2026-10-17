"""任务在插件线程上执行；直接构造 PluginRecord，不加载真实插件。"""
from __future__ import annotations

import logging
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from wxbot_framework.plugins import JOB_QUEUE_SIZE, PluginRecord, PluginSpec
from wxbot_framework.scheduler import TaskJob
from wxbot_framework.sdk import Message


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


if __name__ == "__main__":
    unittest.main()
