"""调度表达式解析与调度器行为；不加载真实插件、不碰网络。"""
from __future__ import annotations

from datetime import datetime, timedelta
import unittest

from wxbot_framework.scheduler import BEIJING, ScheduleError, parse_schedule


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


if __name__ == "__main__":
    unittest.main()
