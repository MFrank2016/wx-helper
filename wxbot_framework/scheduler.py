"""调度表达式解析：中文语义化写法与 5 字段 cron，一律按北京时间解释。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

BEIJING = timezone(timedelta(hours=8))

_MINUTE_BOUNDS = (0, 59)
_HOUR_BOUNDS = (0, 23)
_DAY_BOUNDS = (1, 31)
_MONTH_BOUNDS = (1, 12)
_WEEKDAY_BOUNDS = (0, 7)

_WEEKDAY_CHARS = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "日": "0"}

_NUMBER = re.compile(r"[0-9]+")
_DAILY = re.compile(r"每天([0-9]{1,2}):([0-9]{2})")
_WORKDAY = re.compile(r"工作日([0-9]{1,2}):([0-9]{2})")
_DAY_OF_WEEK = re.compile(r"每周([一二三四五六日])([0-9]{1,2}):([0-9]{2})")
_DAY_OF_MONTH = re.compile(r"每月([0-9]{1,2})日([0-9]{1,2}):([0-9]{2})")
_MINUTE_STEP = re.compile(r"每([0-9]+)分钟")
_HOUR_STEP = re.compile(r"每([0-9]+)小时")

_MAX_SEARCH_DAYS = 366


def beijing_now() -> datetime:
    """当前北京时间。"""
    return datetime.now(BEIJING)


class ScheduleError(ValueError):
    """调度表达式无法解析。"""


@dataclass(frozen=True)
class Schedule:
    """一个调度表达式；`weekdays` 用 0=周一 … 6=周日，与 `datetime.weekday()` 一致。"""

    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    def matches(self, moment: datetime) -> bool:
        """`moment` 所在的分钟是否命中。"""
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        return self._matches_day(moment)

    def next_due(self, after: datetime) -> datetime | None:
        """`after` 之后第一个命中的分钟；一年内找不到返回 None。"""
        moment = after if after.tzinfo is not None else after.replace(tzinfo=BEIJING)
        probe = (moment + timedelta(minutes=1)).replace(second=0, microsecond=0)
        for _ in range(_MAX_SEARCH_DAYS):
            if self._matches_day(probe):
                first = probe.hour * 60 + probe.minute
                for offset in range(first, 24 * 60):
                    candidate = probe.replace(hour=offset // 60, minute=offset % 60)
                    if self.matches(candidate):
                        return candidate
            probe = (probe + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return None

    def _matches_day(self, moment: datetime) -> bool:
        on_day = moment.day in self.days
        on_weekday = moment.weekday() in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return on_day or on_weekday
        if self.day_restricted:
            return on_day
        if self.weekday_restricted:
            return on_weekday
        return True


def parse_schedule(text: str) -> Schedule:
    """解析中文语义化写法或 5 字段 cron（分 时 日 月 周）。"""
    fields = _semantic_fields(text)
    if fields is None:
        fields = text.split()
        if len(fields) != 5:
            raise ScheduleError(f"调度表达式 {text!r} 既不是语义化写法，也不是 5 字段 cron")
    minutes = _expand(fields[0], _MINUTE_BOUNDS, "分", text)
    hours = _expand(fields[1], _HOUR_BOUNDS, "时", text)
    days = _expand(fields[2], _DAY_BOUNDS, "日", text)
    months = _expand(fields[3], _MONTH_BOUNDS, "月", text)
    weekdays = {6 if value in (0, 7) else value - 1
                for value in _expand(fields[4], _WEEKDAY_BOUNDS, "周", text)}
    return Schedule(text=text, minutes=frozenset(minutes), hours=frozenset(hours),
                    days=frozenset(days), months=frozenset(months), weekdays=frozenset(weekdays),
                    day_restricted=fields[2] != "*", weekday_restricted=fields[4] != "*")


def _semantic_fields(text: str):
    """语义化写法翻译成 5 个 cron 字段；不是语义化写法时返回 None。"""
    compact = "".join(text.split())
    match = _DAILY.fullmatch(compact)
    if match:
        hour, minute = _clock(match.group(1), match.group(2), text)
        return (str(minute), str(hour), "*", "*", "*")
    match = _WORKDAY.fullmatch(compact)
    if match:
        hour, minute = _clock(match.group(1), match.group(2), text)
        return (str(minute), str(hour), "*", "*", "1-5")
    match = _DAY_OF_WEEK.fullmatch(compact)
    if match:
        hour, minute = _clock(match.group(2), match.group(3), text)
        return (str(minute), str(hour), "*", "*", _WEEKDAY_CHARS[match.group(1)])
    match = _DAY_OF_MONTH.fullmatch(compact)
    if match:
        hour, minute = _clock(match.group(2), match.group(3), text)
        return (str(minute), str(hour), match.group(1), "*", "*")
    match = _MINUTE_STEP.fullmatch(compact)
    if match:
        step = _positive(match.group(1), text)
        if step > 59:
            raise ScheduleError(f"调度表达式 {text!r} 的分钟间隔不能超过 59")
        return (f"*/{step}", "*", "*", "*", "*")
    match = _HOUR_STEP.fullmatch(compact)
    if match:
        return ("0", f"*/{_positive(match.group(1), text)}", "*", "*", "*")
    return None


def _clock(hour_text, minute_text, text):
    hour, minute = int(hour_text), int(minute_text)
    if not 0 <= hour <= 23:
        raise ScheduleError(f"调度表达式 {text!r} 的时字段越界：{hour}")
    if not 0 <= minute <= 59:
        raise ScheduleError(f"调度表达式 {text!r} 的分字段越界：{minute}")
    return hour, minute


def _positive(token, text):
    value = int(token)
    if value < 1:
        raise ScheduleError(f"调度表达式 {text!r} 的间隔必须是正整数：{token}")
    return value


def _expand(field, bounds, name, text):
    low, high = bounds
    values = set()
    for part in field.split(","):
        values.update(_expand_part(part, low, high, name, text))
    return values


def _expand_part(part, low, high, name, text):
    body, slash, step_text = part.partition("/")
    step = 1
    if slash:
        if not _NUMBER.fullmatch(step_text) or int(step_text) < 1:
            raise ScheduleError(f"调度表达式 {text!r} 的{name}字段步长非法：{part!r}")
        step = int(step_text)
    if body == "*":
        start, stop = low, high
    elif "-" in body:
        start_text, _, stop_text = body.partition("-")
        start = _number(start_text, low, high, name, text)
        stop = _number(stop_text, low, high, name, text)
        if start > stop:
            raise ScheduleError(f"调度表达式 {text!r} 的{name}字段区间颠倒：{part!r}")
    else:
        start = _number(body, low, high, name, text)
        stop = high if slash else start
    return set(range(start, stop + 1, step))


def _number(token, low, high, name, text):
    if not _NUMBER.fullmatch(token):
        raise ScheduleError(f"调度表达式 {text!r} 的{name}字段无法解析：{token!r}")
    value = int(token)
    if not low <= value <= high:
        raise ScheduleError(f"调度表达式 {text!r} 的{name}字段越界：{value}")
    return value
