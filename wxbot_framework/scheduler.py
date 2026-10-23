"""调度表达式解析：中文语义化写法与 5 字段 cron，一律按北京时间解释。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import re
import threading
import time
from collections.abc import Callable

from .config import validate_name

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
    if not isinstance(text, str):
        raise ScheduleError(f"调度表达式必须是字符串，收到 {type(text).__name__}")
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
        step = _positive(match.group(1), text, 59, "分钟")
        return (f"*/{step}", "*", "*", "*", "*")
    match = _HOUR_STEP.fullmatch(compact)
    if match:
        step = _positive(match.group(1), text, 23, "小时")
        return ("0", f"*/{step}", "*", "*", "*")
    return None


def _clock(hour_text, minute_text, text):
    hour, minute = int(hour_text), int(minute_text)
    if not 0 <= hour <= 23:
        raise ScheduleError(f"调度表达式 {text!r} 的时字段越界：{hour}")
    if not 0 <= minute <= 59:
        raise ScheduleError(f"调度表达式 {text!r} 的分字段越界：{minute}")
    return hour, minute


def _positive(token, text, maximum, unit):
    value = int(token)
    if not 1 <= value <= maximum:
        raise ScheduleError(f"调度表达式 {text!r} 的{unit}间隔应在 1–{maximum} 之间：{token}")
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


_LOGGER = logging.getLogger("Plugins")


@dataclass(frozen=True)
class TaskJob:
    """一次待执行的任务投递。"""

    plugin: str
    name: str


@dataclass
class TaskState:
    """一个已注册任务的全部运行时状态。"""

    plugin: str
    name: str
    run: Callable[[], None] | None
    default_schedule: str
    schedule: Schedule | None = None
    source: str = "code"
    enabled: bool = True
    running: bool = False
    skipped: int = 0
    runs: int = 0
    last_run_at: str | None = None
    last_ok: bool | None = None
    last_error: str = ""
    last_ms: float = 0.0
    config_error: str = ""
    last_fired_minute: str | None = None
    last_skipped_minute: str | None = None
    callback: str = ""
    derived: bool = False


class TaskRegistrar:
    """插件加载时拿到的任务注册句柄；`close()` 关闭注册窗口，已注册的任务保留。"""

    def __init__(self, scheduler: Scheduler, plugin: str):
        self._scheduler = scheduler
        self._plugin = plugin
        self._closed = False

    def register(self, name: str, schedule: str, run: Callable[[], None]) -> None:
        if self._closed:
            raise RuntimeError("定时任务只能在 on_load 期间注册")
        self._scheduler.register(self._plugin, name, schedule, run)

    def close(self) -> None:
        """关闭注册窗口；任务注销由调用方按卸载路径（加载失败/停用）负责。"""
        self._closed = True


class Scheduler:
    """任务注册表与到期判断；到点只投递，不自己开线程执行。"""

    def __init__(self, store, *, enqueue=None, now=None):
        self.store = store
        self._enqueue = enqueue
        self._is_active = None
        self.now = now if now is not None else beijing_now
        self._tasks: dict[tuple[str, str], TaskState] = {}
        self._config: dict = {}
        self._lock = threading.RLock()

    def bind(self, enqueue: Callable[[str, TaskJob], bool], *, is_active=None) -> None:
        """注入投递函数；任务回调借所属插件既有的线程执行。

        `is_active` 是「该插件此刻是否有存活实例」的判定函数，供未注册任务行区分
        「未注册」（插件在跑、代码里没这个任务）与「未加载」（插件停用/未加载/不存在）；
        不传表示没有存活信息，未注册行一律按「未注册」呈现。
        """
        self._enqueue = enqueue
        self._is_active = is_active

    def refresh(self) -> None:
        """重新加载配置，并把覆盖值/停用状态套用到每个已注册任务。"""
        with self._lock:
            self._config = self.store.load()
            for state in self._tasks.values():
                self._apply_config(state)
            self._rebuild_derived()

    def register(self, plugin: str, name: str, schedule: str, run: Callable[[], None]) -> None:
        """注册一个任务；任务名与默认表达式不合法、或重名时抛 ValueError。"""
        validate_name(name)
        parse_schedule(schedule)
        key = (plugin, name)
        with self._lock:
            existing = self._tasks.get(key)
            if existing is not None and not existing.derived:
                raise ValueError(f"任务已注册：{plugin}/{name}")
            state = TaskState(plugin=plugin, name=name, run=run, default_schedule=schedule,
                              callback=name)
            self._apply_config(state)
            self._tasks[key] = state
            self._rebuild_derived(plugin)

    def unregister_plugin(self, plugin: str) -> None:
        """注销该插件的全部任务；重复调用无副作用。"""
        with self._lock:
            for key in [key for key in self._tasks if key[0] == plugin]:
                del self._tasks[key]

    def abandon(self, plugin: str, name: str) -> None:
        """任务已出队但不再执行时必须归还 running，否则永远不再触发。"""
        with self._lock:
            state = self._tasks.get((plugin, name))
            if state is not None:
                state.running = False

    def snapshot_plugin(self, plugin: str) -> dict:
        """该插件当前任务注册的浅快照，供热重载失败时还原旧实例的任务。"""
        with self._lock:
            return {key: state for key, state in self._tasks.items() if key[0] == plugin}

    def restore_plugin(self, plugin: str, snapshot: dict) -> None:
        """先清空该插件的全部任务，再把快照里的注册放回去。"""
        with self._lock:
            for key in [key for key in self._tasks if key[0] == plugin]:
                del self._tasks[key]
            self._tasks.update(snapshot)

    def _rebuild_derived(self, plugin: str | None = None) -> None:
        """按配置里的 callback 生成配置任务实例；callback 指向已注册的代码任务。"""
        with self._lock:
            plugins = {key[0] for key in self._tasks}
            plugins.update(key[0] for key in self._config_entries())
            if plugin is not None:
                plugins = {plugin}
            for current in sorted(plugins):
                desired: dict[str, tuple[str, TaskState]] = {}
                for key in self._config_entries():
                    if key[0] != current:
                        continue
                    name = key[1]
                    entry = self._task_config(current, name)
                    callback = entry.get("callback")
                    if not isinstance(callback, str) or not callback:
                        continue
                    existing = self._tasks.get(key)
                    if existing is not None and not existing.derived:
                        continue
                    base = self._tasks.get((current, callback))
                    if base is None or base.derived:
                        continue
                    desired[name] = (callback, base)
                for key in [key for key in self._tasks if key[0] == current and self._tasks[key].derived]:
                    if key[1] not in desired:
                        del self._tasks[key]
                for name, (callback, base) in desired.items():
                    key = (current, name)
                    state = self._tasks.get(key)
                    if state is not None and not state.derived:
                        continue
                    if state is None:
                        state = TaskState(plugin=current, name=name, run=base.run,
                                          default_schedule=base.default_schedule,
                                          callback=callback, derived=True)
                        self._tasks[key] = state
                    else:
                        state.run = base.run
                        state.default_schedule = base.default_schedule
                        state.callback = callback
                    self._apply_config(state)

    def tick(self) -> None:
        """把当前分钟到期的任务投递出去；同一分钟只触发一次，错过不补跑。"""
        try:
            moment = self.now()
            minute_key = moment.strftime("%Y-%m-%d %H:%M")
            with self._lock:
                for state in self._tasks.values():
                    if not state.enabled or state.schedule is None:
                        continue
                    if not state.schedule.matches(moment):
                        continue
                    if state.last_fired_minute == minute_key:
                        continue
                    if state.running:
                        self._note_skip(state, minute_key)
                        continue
                    state.running = True
                    if self._dispatch(state):
                        state.last_fired_minute = minute_key
                    else:
                        state.running = False
                        self._note_skip(state, minute_key)
        except Exception:
            _LOGGER.exception("定时任务调度失败")

    def run_now(self, plugin: str, name: str) -> bool:
        """立刻投递一次，忽略 enabled；未注册抛 ValueError，投递被拒返回 False。"""
        with self._lock:
            state = self._tasks.get((plugin, name))
            if state is None:
                raise ValueError(f"未找到任务：{plugin}/{name}")
            if state.running:
                return False
            state.running = True
            if not self._dispatch(state):
                state.running = False
                return False
            return True

    def run_job(self, job: TaskJob) -> None:
        """执行一次任务并记录结果；异常只记日志，不向外扩散。"""
        with self._lock:
            state = self._tasks.get((job.plugin, job.name))
            if state is None:
                return
            run = state.run
            state.running = True
        started = time.monotonic()
        error = None
        try:
            if run is not None:
                run()
        except Exception as exc:
            error = exc
            logging.getLogger(f"Plugin.{job.plugin}").exception("定时任务 %s 执行失败", job.name)
        finally:
            elapsed = (time.monotonic() - started) * 1000
            with self._lock:
                state.runs += 1
                state.running = False
                state.last_run_at = self.now().isoformat()
                state.last_ms = elapsed
                state.last_ok = error is None
                state.last_error = "" if error is None else f"{type(error).__name__}: {error}"

    def set_enabled(self, plugin: str, name: str, enabled: bool) -> dict:
        """写回启停开关并立刻生效；已注册或配置里声明过的任务都允许。"""
        with self._lock:
            state = self._tasks.get((plugin, name))
            self._require_known(plugin, name)
            self._config = self.store.set_task_enabled(plugin, name, enabled)
            if state is None:
                return self._declared_row(plugin, name)
            self._apply_config(state)
            return self._row(state, self.now())

    def set_schedule(self, plugin: str, name: str, text: str) -> dict:
        """先校验表达式，再写回配置；已注册或配置里声明过的任务都允许。"""
        parse_schedule(text)
        with self._lock:
            state = self._tasks.get((plugin, name))
            self._require_known(plugin, name)
            self._config = self.store.set_task_schedule(plugin, name, text)
            if state is None:
                return self._declared_row(plugin, name)
            self._apply_config(state)
            return self._row(state, self.now())

    def status(self) -> list[dict]:
        """已注册任务与配置里声明过的任务，按 (插件, 任务名) 排序。"""
        with self._lock:
            moment = self.now()
            rows = [self._row(state, moment) for _, state in sorted(self._tasks.items())]
            seen = set(self._tasks)
            for key in self._config_entries():
                if key in seen:
                    continue
                rows.append(self._declared_row(key[0], key[1]))
            rows.sort(key=lambda row: (row["plugin"], row["name"]))
            return rows

    def _dispatch(self, state: TaskState) -> bool:
        """投递一次；未注入投递函数或投递抛异常都当作被拒（调用方负责回滚 running）。"""
        if self._enqueue is None:
            return False
        try:
            return bool(self._enqueue(state.plugin, TaskJob(state.plugin, state.name)))
        except Exception:
            _LOGGER.exception("定时任务投递失败：%s/%s", state.plugin, state.name)
            return False

    def _note_skip(self, state: TaskState, minute_key: str) -> None:
        """同一分钟内只记一次跳过，避免快速 tick 把长任务的跳过次数刷爆。"""
        if state.last_skipped_minute != minute_key:
            state.last_skipped_minute = minute_key
            state.skipped += 1

    def _require_known(self, plugin: str, name: str) -> None:
        """准入：已注册，或该任务名已在配置里声明过（与声明的时间是否合法无关）。"""
        if (plugin, name) in self._tasks or name in self._tasks_of(plugin):
            return
        raise ValueError(f"未找到任务：{plugin}/{name}")

    def _declared_row(self, plugin: str, name: str) -> dict:
        """配置里声明过、代码未注册的任务行（status() 与改开关/改时间共用）。

        插件有存活实例（或没有存活信息）时是「未注册」——配置写了、代码里没有；
        插件停用/未加载/不存在时是「未加载」——代码好坏无从判断，别把人引向「代码坏了」。
        """
        entry = self._task_config(plugin, name)
        text = entry.get("schedule")
        schedule_text = ""
        config_error = ""
        if text is not None:
            try:
                parse_schedule(text)
            except ScheduleError as error:
                config_error = str(error)
            else:
                schedule_text = text
        callback = entry.get("callback")
        if not isinstance(callback, str) or not callback:
            callback = ""
        if callback and not config_error:
            config_error = f"回调任务未注册：{callback}"
        state = "未注册" if self._is_active is None or self._is_active(plugin) else "未加载"
        return {
            "plugin": plugin,
            "name": name,
            "callback": callback or name,
            "derived": bool(callback and callback != name),
            "schedule": schedule_text,
            "source": "config",
            "enabled": bool(entry.get("enabled", True)),
            "registered": False,
            "state": state,
            "running": False,
            "skipped": 0,
            "runs": 0,
            "last_run_at": None,
            "last_ok": None,
            "last_error": "",
            "last_ms": 0.0,
            "next_due": "",
            "config_error": config_error,
        }

    def _apply_config(self, state: TaskState) -> None:
        entry = self._task_config(state.plugin, state.name)
        text = entry.get("schedule")
        state.enabled = bool(entry.get("enabled", True))
        if text is None:
            state.source = "code"
            state.config_error = ""
            state.schedule = parse_schedule(state.default_schedule)
            return
        state.source = "config"
        try:
            state.schedule = parse_schedule(text)
        except ScheduleError as error:
            state.schedule = None
            state.config_error = str(error)
        else:
            state.config_error = ""

    def _task_config(self, plugin: str, name: str) -> dict:
        tasks = self._tasks_of(plugin)
        entry = tasks.get(name)
        return entry if isinstance(entry, dict) else {}

    def _tasks_of(self, plugin: str) -> dict:
        plugins = self._config.get("plugins") if isinstance(self._config, dict) else None
        node = plugins.get(plugin) if isinstance(plugins, dict) else None
        tasks = node.get("tasks") if isinstance(node, dict) else None
        return tasks if isinstance(tasks, dict) else {}

    def _config_entries(self):
        """配置里声明过的 (插件, 任务名)。"""
        plugins = self._config.get("plugins") if isinstance(self._config, dict) else None
        if not isinstance(plugins, dict):
            return
        for plugin in sorted(plugins):
            for name in sorted(self._tasks_of(plugin)):
                yield (plugin, name)

    def _row(self, state: TaskState, moment: datetime) -> dict:
        next_due = state.schedule.next_due(moment) if state.schedule is not None else None
        if state.config_error:
            label = "配置错误"
        elif not state.enabled:
            label = "已停用"
        elif state.running:
            label = "运行中"
        else:
            label = "待触发"
        return {
            "plugin": state.plugin,
            "name": state.name,
            "callback": state.callback or state.name,
            "derived": state.derived,
            "schedule": state.schedule.text if state.schedule is not None else "",
            "source": state.source,
            "enabled": state.enabled,
            "registered": True,
            "state": label,
            "running": state.running,
            "skipped": state.skipped,
            "runs": state.runs,
            "last_run_at": state.last_run_at,
            "last_ok": state.last_ok,
            "last_error": state.last_error,
            "last_ms": state.last_ms,
            "next_due": next_due.isoformat() if next_due is not None else "",
            "config_error": state.config_error,
        }
