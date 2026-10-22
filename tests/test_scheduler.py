"""调度表达式解析与调度器行为；不加载真实插件、不碰网络。"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import unittest

from wxbot_framework.scheduler import BEIJING, ScheduleError, Scheduler, TaskJob, parse_schedule


class ParseScheduleTests(unittest.TestCase):
    def test_semantic_forms_match_their_cron_equivalents(self):
        pairs = [
            ("每天 21:00", "0 21 * * *"),
            ("工作日 09:00", "0 9 * * 1-5"),
            ("每周一 10:30", "30 10 * * 1"),
            ("每月 1 日 09:00", "0 9 1 * *"),
            ("每 30 分钟", "*/30 * * * *"),
            ("每 2 小时", "0 */2 * * *"),
        ]
        base = datetime(2026, 3, 2, 0, 0, tzinfo=BEIJING)   # 2026-03-02 是周一
        moments = [base + timedelta(minutes=i) for i in range(0, 60 * 24 * 8, 7)]
        for text, cron in pairs:
            with self.subTest(text=text):
                left, right = parse_schedule(text), parse_schedule(cron)
                self.assertEqual([m for m in moments if left.matches(m)],
                                 [m for m in moments if right.matches(m)])

    def test_semantic_forms_ignore_spaces(self):
        self.assertTrue(parse_schedule("每天21:00").matches(datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)))
        self.assertTrue(parse_schedule("每30分钟").matches(datetime(2026, 3, 2, 21, 30, tzinfo=BEIJING)))

    def test_last_day_of_month_form_is_not_supported(self):
        # 「每月 31 日」只在有 31 日的月份匹配，不自动顺延到月末。
        schedule = parse_schedule("每月 31 日 09:00")
        self.assertTrue(schedule.matches(datetime(2026, 1, 31, 9, 0, tzinfo=BEIJING)))
        self.assertFalse(schedule.matches(datetime(2026, 2, 28, 9, 0, tzinfo=BEIJING)))

    def test_day_and_weekday_are_ored_when_both_restricted(self):
        # POSIX：日与周同时被限定时取「或」。
        schedule = parse_schedule("0 9 1 * 1")
        self.assertTrue(schedule.matches(datetime(2026, 4, 1, 9, 0, tzinfo=BEIJING)))   # 1 日
        self.assertTrue(schedule.matches(datetime(2026, 3, 2, 9, 0, tzinfo=BEIJING)))   # 周一
        self.assertFalse(schedule.matches(datetime(2026, 3, 3, 9, 0, tzinfo=BEIJING)))  # 周二且非 1 日

    def test_weekday_field_accepts_zero_and_seven_as_sunday(self):
        for text in ("0 9 * * 0", "0 9 * * 7"):
            with self.subTest(text=text):
                self.assertTrue(parse_schedule(text).matches(
                    datetime(2026, 3, 1, 9, 0, tzinfo=BEIJING)))     # 周日

    def test_ranges_lists_and_steps_are_supported(self):
        # 日字段的 */2 从 1 号起每两天一次，所以用 3 号而不是 2 号。
        schedule = parse_schedule("0,30 9-11 */2 * *")
        self.assertTrue(schedule.matches(datetime(2026, 3, 3, 10, 30, tzinfo=BEIJING)))
        self.assertFalse(schedule.matches(datetime(2026, 3, 3, 12, 30, tzinfo=BEIJING)))
        self.assertFalse(schedule.matches(datetime(2026, 3, 4, 10, 30, tzinfo=BEIJING)))

    def test_invalid_expressions_raise_schedule_error(self):
        for text in ("每天 25:00", "每天 21:0", "每 0 分钟", "每 60 分钟", "每 0 小时", "每 25 小时",
                     "* * * *", "1 2 3 4 5 6", "0 9 * * 8", "60 9 * * *", "每天", "", "  "):
            with self.subTest(text=text):
                with self.assertRaises(ScheduleError):
                    parse_schedule(text)

    def test_non_string_input_raises_schedule_error(self):
        for value in (None, 5, ["每天 09:00"]):
            with self.subTest(value=value):
                with self.assertRaises(ScheduleError):
                    parse_schedule(value)

    def test_next_due_returns_next_matching_minute(self):
        schedule = parse_schedule("每天 21:00")
        self.assertEqual(schedule.next_due(datetime(2026, 3, 2, 20, 59, tzinfo=BEIJING)),
                         datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING))
        self.assertEqual(schedule.next_due(datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)),
                         datetime(2026, 3, 3, 21, 0, tzinfo=BEIJING))

    def test_next_due_crosses_month_boundary(self):
        schedule = parse_schedule("每月 1 日 09:00")
        self.assertEqual(schedule.next_due(datetime(2026, 2, 28, 23, 59, tzinfo=BEIJING)),
                         datetime(2026, 3, 1, 9, 0, tzinfo=BEIJING))

    def test_next_due_returns_none_for_impossible_expression(self):
        self.assertIsNone(parse_schedule("0 0 30 2 *").next_due(datetime(2026, 3, 2, tzinfo=BEIJING)))


class _FakeStore:
    def __init__(self, data=None):
        self.data = data if data is not None else {}
        self.writes = []
    def load(self):
        return copy.deepcopy(self.data)
    def set_task_enabled(self, plugin, task, enabled):
        self.data.setdefault("plugins", {}).setdefault(plugin, {}).setdefault("tasks", {}) \
            .setdefault(task, {})["enabled"] = bool(enabled)
        self.writes.append(("enabled", plugin, task, enabled))
        return self.load()
    def set_task_schedule(self, plugin, task, schedule):
        self.data.setdefault("plugins", {}).setdefault(plugin, {}).setdefault("tasks", {}) \
            .setdefault(task, {})["schedule"] = schedule
        self.writes.append(("schedule", plugin, task, schedule))
        return self.load()


class _FakeClock:
    def __init__(self, moment):
        self.moment = moment
    def __call__(self):
        return self.moment


class SchedulerTests(unittest.TestCase):
    def make(self, store=None, accept=True):
        self.store = store if store is not None else _FakeStore()
        self.clock = _FakeClock(datetime(2026, 3, 2, 20, 59, tzinfo=BEIJING))
        self.jobs = []
        self.accept = accept
        def enqueue(plugin, job):
            if not self.accept:
                return False
            self.jobs.append(job)
            return True
        self.scheduler = Scheduler(self.store, enqueue=enqueue, now=self.clock)
        self.scheduler.refresh()
        return self.scheduler

    def test_register_then_tick_fires_once_for_the_matching_minute(self):
        scheduler = self.make()
        scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        scheduler.tick()
        scheduler.tick()          # 同一分钟内第二次 tick 不重复触发
        self.assertEqual([job.name for job in self.jobs], ["daily"])

    def test_tick_skips_while_previous_run_is_still_running(self):
        scheduler = self.make()
        scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        scheduler.tick()
        self.clock.moment = datetime(2026, 3, 3, 21, 0, tzinfo=BEIJING)
        scheduler.tick()                    # 上一轮没结束（未被 run_job 收尾）
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(scheduler.status()[0]["skipped"], 1)

    def test_tick_counts_skip_when_enqueue_refuses(self):
        scheduler = self.make(accept=False)
        scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        scheduler.tick()
        self.assertEqual(scheduler.status()[0]["skipped"], 1)
        self.assertFalse(scheduler.status()[0]["running"])

    def test_tick_ignores_disabled_task(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {"daily": {"enabled": False}}}}}))
        self.scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        self.scheduler.tick()
        self.assertEqual(self.jobs, [])

    def test_config_schedule_overrides_code_default_and_marks_source(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {"daily": {"schedule": "每天 09:00"}}}}}))
        self.scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        row = self.scheduler.status()[0]
        self.assertEqual((row["schedule"], row["source"]), ("每天 09:00", "config"))

    def test_invalid_config_schedule_keeps_task_unfired_and_visible(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {"daily": {"schedule": "每天 25:00"}}}}}))
        self.scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        self.scheduler.tick()
        row = self.scheduler.status()[0]
        self.assertEqual(self.jobs, [])
        self.assertEqual(row["state"], "配置错误")
        self.assertIn("25:00", row["config_error"])

    def test_config_declared_task_without_code_is_listed_unregistered(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {"ghost": {"schedule": "每天 09:00"}}}}}))
        row = self.scheduler.status()[0]
        self.assertEqual((row["name"], row["registered"], row["state"]), ("ghost", False, "未注册"))

    def test_run_job_records_success_and_failure(self):
        scheduler = self.make()
        scheduler.register("demo", "ok", "每天 21:00", lambda: None)
        scheduler.register("demo", "bad", "每天 21:00", lambda: (_ for _ in ()).throw(ValueError("炸了")))
        scheduler.run_job(TaskJob("demo", "ok"))
        with self.assertLogs("Plugin.demo", level="ERROR"):
            scheduler.run_job(TaskJob("demo", "bad"))
        rows = {row["name"]: row for row in scheduler.status()}
        self.assertTrue(rows["ok"]["last_ok"])
        self.assertFalse(rows["bad"]["last_ok"])
        self.assertIn("炸了", rows["bad"]["last_error"])
        self.assertFalse(rows["bad"]["running"])

    def test_set_schedule_validates_before_writing(self):
        scheduler = self.make()
        scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        with self.assertRaises(ScheduleError):
            scheduler.set_schedule("demo", "daily", "每天 25:00")
        self.assertEqual(self.store.writes, [])
        self.assertEqual(scheduler.set_schedule("demo", "daily", "每天 09:00")["schedule"], "每天 09:00")
        self.assertEqual(self.store.writes, [("schedule", "demo", "daily", "每天 09:00")])

    def test_set_enabled_writes_and_takes_effect_immediately(self):
        scheduler = self.make()
        scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        scheduler.set_enabled("demo", "daily", False)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        scheduler.tick()
        self.assertEqual(self.jobs, [])

    def test_status_reports_next_due(self):
        scheduler = self.make()
        scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.assertEqual(scheduler.status()[0]["next_due"],
                         datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING).isoformat())

    def test_run_now_ignores_enabled_and_requires_registration(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {"daily": {"enabled": False}}}}}))
        self.scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.assertTrue(self.scheduler.run_now("demo", "daily"))
        with self.assertRaises(ValueError):
            self.scheduler.run_now("demo", "ghost")

    def test_set_enabled_allows_declared_but_unregistered_task(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {"ghost": {"enabled": True}}}}}))
        row = self.scheduler.set_enabled("demo", "ghost", False)
        self.assertEqual((row["plugin"], row["name"], row["registered"], row["enabled"]),
                         ("demo", "ghost", False, False))
        self.assertEqual(self.store.writes, [("enabled", "demo", "ghost", False)])

    def test_set_schedule_allows_declared_but_unregistered_task(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {"ghost": {"schedule": "每天 09:00"}}}}}))
        row = self.scheduler.set_schedule("demo", "ghost", "每天 10:00")
        self.assertEqual((row["registered"], row["schedule"]), (False, "每天 10:00"))
        self.assertEqual(self.store.writes, [("schedule", "demo", "ghost", "每天 10:00")])

    def test_set_enabled_rejects_task_absent_from_registry_and_config(self):
        self.make()
        with self.assertRaises(ValueError):
            self.scheduler.set_enabled("demo", "ghost", True)

    def test_set_schedule_rejects_task_absent_from_registry_and_config(self):
        self.make()
        with self.assertRaises(ValueError):
            self.scheduler.set_schedule("demo", "ghost", "每天 10:00")

    def test_tick_never_raises_and_unlatches_when_enqueue_raises(self):
        scheduler = self.make()

        def boom(plugin, job):
            raise RuntimeError("boom")

        scheduler.bind(boom)
        scheduler.register("demo", "daily", "每天 21:00", lambda: None)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        with self.assertLogs("Plugins", level="ERROR"):
            scheduler.tick()
        row = scheduler.status()[0]
        self.assertFalse(row["running"])
        self.assertEqual(row["skipped"], 1)

        def record(plugin, job):
            self.jobs.append(job)
            return True

        scheduler.bind(record)
        scheduler.tick()
        self.assertEqual([job.name for job in self.jobs], ["daily"])

    def test_status_unregistered_row_shows_declared_schedule_and_config_error(self):
        self.make(_FakeStore({"plugins": {"demo": {"tasks": {
            "ghost": {"schedule": "每天 25:00"},
            "ok_ghost": {"schedule": "每天 09:00"},
        }}}}))
        rows = {row["name"]: row for row in self.scheduler.status()}
        self.assertFalse(rows["ghost"]["registered"])
        self.assertIn("25:00", rows["ghost"]["config_error"])
        self.assertEqual(rows["ok_ghost"]["schedule"], "每天 09:00")

    def test_skipped_counts_once_per_due_minute_while_running(self):
        scheduler = self.make()
        scheduler.register("demo", "frequent", "每 1 分钟", lambda: None)
        self.clock.moment = datetime(2026, 3, 2, 21, 0, tzinfo=BEIJING)
        scheduler.tick()
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(scheduler.status()[0]["skipped"], 0)
        self.clock.moment = datetime(2026, 3, 2, 21, 1, tzinfo=BEIJING)
        scheduler.tick()
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(scheduler.status()[0]["skipped"], 1)
        self.assertEqual(len(self.jobs), 1)


if __name__ == "__main__":
    unittest.main()
