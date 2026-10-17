# 定时任务框架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 框架统一提供定时任务能力——插件在加载时注册任务，框架负责到点判断、启停与可见性，管理台里能看全部任务、改时间、启停、手动跑一次。

**Architecture:** 新增 `wxbot_framework/scheduler.py`，承载"表达式解析 + 任务注册表 + 到期判断 + 运行状态"四件事。任务回调不做独立线程，而是投递到**所属插件已有的线程**上、与其消息回调共用同一把回调锁，因此同一插件不会一边回消息一边跑任务。任务的时间与开关存在 `bot_config.json` 既有 `plugins.<插件>` 段下新增的 `tasks` 子对象里，写入沿用 `ConfigStore` 的原子替换。管理台新增一个核心页面与 `/api/tasks` 四个接口，页面直接读进程内调度器的状态，不新建存储。

**Tech Stack:** Python 3.13；仅标准库（`datetime` + 固定 UTC+8 `timezone(timedelta(hours=8))`，不引入 `zoneinfo`；`threading`/`queue`/`logging`）。管理台前端是纯 HTML/CSS/JS、无构建。测试沿用仓库现有 `unittest`。

**Spec:** `docs/superpowers/specs/2026-10-16-task-scheduler-design.md`

## Global Constraints

- 时区固定北京时间（UTC+8），不逐任务配置；所有表达式按此解释。
- cron 为 5 字段 `分 时 日 月 周`，支持 `*` `,` `-` `/`；周字段 0–7，`0` 与 `7` 都是周日。**日 与 周 同时被限定（都不是 `*`）时取"或"，否则取"与"**（POSIX cron 语义）。
- 语义化写法集合（解析时忽略空格）：`每天 HH:MM`、`工作日 HH:MM`、`每周一…每周日 HH:MM`、`每月 D 日 HH:MM`、`每 N 分钟`（N ≤ 59）、`每 N 小时`。`HH:MM` 必须是真实时刻（00–23 / 00–59），`N` 必须为正整数。
- 触发粒度是分钟：只有当前分钟匹配才触发，**错过的时刻不补跑**。
- 任务名规则同 `validate_name`：`^[a-z][a-z0-9_]{0,63}$`。
- 任务回调签名 `callback()`，无参数。
- 不新增任何第三方依赖（`requirements.txt` 只有 `pefile`）。
- 微信正文 1–1023 个 UTF-8 字节，超限时发送会抛错。
- 配置写回必须走"先读整份、只改一个键、同目录原子替换"，与 `ConfigStore.set_enabled` 同一机制。
- 管理台与机器人同进程、只绑 `127.0.0.1`、不设登录；`/api/` 前缀统一转发；前端是无框架的哈希路由单页。
- 管理台页面不得新增统计维度、数据表或日志类型。
- 测试命令（仓库根目录）：`py -3 -m unittest discover -s tests -p "test_*.py"`（仓库文档写 `python -m unittest discover -s tests -p "test_*.py"`；本机 `python` 是 Windows 商店占位符，用 `py -3`）。
- 日志：框架侧用 `logging.getLogger("Plugins")`，任务运行结果用 `Plugin.<插件>`（即 `ctx.logger`）。

## Review Focus

这些是规格隐含、但容易被实现漏掉、且一次做错就会让用户觉得"这功能是坏的"的地方。每一条都已在下面的任务里安排了对应测试。

1. **手改配置文件写出非法时间**（大小写、漏空格、`每天 25:00`）→ 该任务不触发，但插件其余功能照常，且状态里能看见它错在哪。
2. **表达式永远不匹配**（如 `0 0 30 2 *`）→ 页面显示"下次触发：—"，不崩溃、不空转狂算。
3. **到点那一刻上一轮还在跑** → 跳过并计数，不排队堆积、不并发两份。
4. **微信断线时正好到点** → 该次记为失败并留痕，不重试、恢复后也不补跑。
5. **跨天/跨月边界**（`每月 31 日`、`每 2 小时`、`工作日` 跨周末）在北京时间下的判断与"下次触发"推算不出现差一天/差一小时。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `wxbot_framework/scheduler.py` | 表达式解析（`Schedule`/`parse_schedule`）、任务注册表与运行状态（`TaskState`/`TaskRegistrar`/`Scheduler`）、`TaskJob` 作业信封 | 新建 |
| `wxbot_framework/plugins.py` | `PluginRecord` 增加任务通道并在同一线程/锁上执行；`PluginManager` 增加 `enqueue_job`，在加载/停用/重载时登记与注销任务 | 修改 |
| `wxbot_framework/sdk.py` | `PluginContext` 暴露 `ctx.tasks` | 修改 |
| `wxbot_framework/config.py` | 校验 `plugins.<插件>.tasks.*`；新增任务开关与任务时间的原子写 | 修改 |
| `wxbot_framework/runtime.py` | 构造并绑定调度器、主循环 tick、控制台命令、把调度器注入管理台 | 修改 |
| `wxbot_framework/admin/api.py` | `/api/tasks` 四个接口 | 修改 |
| `wxbot_framework/admin/web/app.js` | 「定时任务」页与导航入口 | 修改 |
| `tests/test_scheduler.py` | 表达式解析 + 调度器行为（纯逻辑，注入时钟与假投递） | 新建 |
| `tests/test_plugin_tasks.py` | 插件线程上的任务执行 + 加载期注册与配置覆盖 | 新建 |
| `tests/test_config.py` | `tasks` 段校验与原子写 | 修改 |
| `tests/test_console.py` | 控制台 `tasks` / `enable` / `disable` / `run` | 修改 |
| `tests/test_admin_api.py` | `/api/tasks` 路由与错误码 | 修改 |
| `plugins/bga_plugin/bga/commands.py` | 把排行榜渲染入口改成按 `chat_id` 调用（供定时播报复用） | 修改（独立仓库） |
| `plugins/bga_plugin/__init__.py` | 注册 `daily_broadcast` 任务并实现播报 | 修改（独立仓库） |

---

### Task 1: 调度表达式解析

**Files:**
- Create: `wxbot_framework/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: 无（本任务只依赖标准库）
- Produces:
  - `BEIJING: timezone`（UTC+8）、`beijing_now() -> datetime`（带时区）
  - `class ScheduleError(ValueError)`
  - `@dataclass(frozen=True) class Schedule`：字段 `text: str`、`minutes: frozenset[int]`、`hours: frozenset[int]`、`days: frozenset[int]`、`months: frozenset[int]`、`weekdays: frozenset[int]`（**0=周一 … 6=周日，与 `datetime.weekday()` 一致**）、`day_restricted: bool`、`weekday_restricted: bool`；方法 `matches(self, moment: datetime) -> bool`、`next_due(self, after: datetime) -> datetime | None`
  - `parse_schedule(text: str) -> Schedule`

- [ ] **Step 1: Write the failing tests**

建 `tests/test_scheduler.py`，文件头加 `"""调度表达式解析与调度器行为；不加载真实插件、不碰网络。"""`。本任务只写解析相关用例：

```python
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
        for text in ("每天 25:00", "每天 21:0", "每 0 分钟", "每 60 分钟", "每 0 小时",
                     "* * * *", "1 2 3 4 5 6", "0 9 * * 8", "60 9 * * *", "每天", "", "  "):
            with self.subTest(text=text):
                with self.assertRaises(ScheduleError):
                    parse_schedule(text)

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m unittest discover -s tests -p "test_scheduler.py" -v`
Expected: FAIL —— `ImportError: cannot import name 'parse_schedule'`

- [ ] **Step 3: Implement `wxbot_framework/scheduler.py`（解析部分）**

要点：
- 先按语义化解析（用去空格后的字符串匹配 `每天`/`工作日`/`每周[一二三四五六日]`/`每月 N 日`/`每 N 分钟`/`每 N 小时`），解析不出再按空白切成 5 段当 cron；两者都失败抛 `ScheduleError`，消息里带上原始表达式与出错字段。
- 语义化写法直接映射成 cron 字段：`工作日` → 周 `1-5`；`每周一` → 周 `1`（周日 → `0`）；`每月 D 日` → 日 `D`；`每 N 分钟` → 分 `*/N`；`每 N 小时` → 时 `*/N`、分 `0`。
- cron 字段展开成整数集合，支持 `*`、`a-b`、`a,b`、`*/n`、`a-b/n`；越界即 `ScheduleError`。
- 周字段换算成内部 0=周一：cron `0` 或 `7` → 内部 `6`，cron `1..6` → 内部 `0..5`。
- `matches(moment)`：分/时/月三项必须命中；日与周按 Global Constraints 的"或/与"规则。
- `next_due(after)`：从 `after` 的下一分钟起，**先按天过滤**（日/月/周三项都不命中就跳到次日 00:00），命中日再逐分钟扫到当天 23:59；最多扫 366 天，扫完没有命中返回 `None`。返回值带 `BEIJING` 时区。

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m unittest discover -s tests -p "test_scheduler.py" -v`
Expected: PASS（10 个用例全绿）

- [ ] **Step 5: Commit**

```bash
git add wxbot_framework/scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): 调度表达式解析与下次触发推算"
```

---

### Task 2: 任务注册表与调度器

**Files:**
- Modify: `wxbot_framework/scheduler.py`（在 Task 1 内容之后追加）
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: Task 1 的 `Schedule` / `parse_schedule` / `ScheduleError` / `beijing_now`；`wxbot_framework/config.py` 的 `ConfigStore`（本任务只当"能 `load()` 出配置、能写配置的方法存在"用，Task 5 才实现新写方法，测试里用假 store）
- Produces:
  - `@dataclass(frozen=True) class TaskJob`：`plugin: str`、`name: str`
  - `@dataclass class TaskState`：`plugin`、`name`、`run: Callable[[], None] | None`、`default_schedule: str`、`schedule: Schedule | None`、`source: str`（`"code"`/`"config"`）、`enabled: bool`、`running: bool`、`skipped: int`、`runs: int`、`last_run_at: str | None`、`last_ok: bool | None`、`last_error: str`、`last_ms: float`、`config_error: str`、`last_fired_minute: str | None`
  - `class TaskRegistrar`：`__init__(self, scheduler, plugin)`、`register(self, name: str, schedule: str, run: Callable[[], None]) -> None`、`close(self) -> None`
  - `class Scheduler`：
    - `__init__(self, store, *, enqueue=None, now=None)`（`now` 返回带时区 `datetime`，默认 `beijing_now`）
    - `bind(self, enqueue: Callable[[str, TaskJob], bool], *, is_active: Callable[[str], bool] | None = None) -> None`（`is_active` 用来区分"插件未加载"与"代码没提供该任务"；为 `None` 时退回旧行为）
    - `refresh(self) -> None`
    - `register(self, plugin: str, name: str, schedule: str, run: Callable[[], None]) -> None`
    - `unregister_plugin(self, plugin: str) -> None`
    - `tick(self) -> None`
    - `run_now(self, plugin: str, name: str) -> bool`
    - `run_job(self, job: TaskJob) -> None`
    - `set_enabled(self, plugin: str, name: str, enabled: bool) -> dict`
    - `set_schedule(self, plugin: str, name: str, text: str) -> dict`
    - `status(self) -> list[dict]`
  - `set_enabled` / `set_schedule` 的准入：任务**已注册**、或该任务名**已在配置里声明**时都允许（写回配置不要求插件已加载，对应设计 §8.6）；两者都不满足才抛 `ValueError`。`run_now` 仍要求任务已注册（未加载的插件没有回调可执行），未注册抛 `ValueError`。
  - `status()` 每行字段：`plugin`、`name`、`schedule`（生效文本；未注册/未加载的行回显配置里声明的原文，没有则为 `""`）、`source`、`enabled`、`registered`、`state`（`运行中`/`待触发`/`已停用`/`配置错误`/`未加载`/`未注册`）、`running`、`skipped`、`runs`、`last_run_at`、`last_ok`、`last_error`、`last_ms`、`next_due`（ISO 字符串，算不出为 `""`）、`config_error`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_scheduler.py`。夹具：一个假 store（`load()` 返回注入的 dict，`set_task_enabled`/`set_task_schedule` 改写它并返回新 dict），一个假 `enqueue` 记录投递、可切换成拒绝：

```python
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
```

用例：

```python
class SchedulerTests(unittest.TestCase):
    def make(self, store=None, accept=True):
        # 第一个位置参数是 store：5 个用例以 self.make(_FakeStore({...})) 调用。
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m unittest discover -s tests -p "test_scheduler.py" -v`
Expected: FAIL —— `ImportError: cannot import name 'Scheduler'`

- [ ] **Step 3: Implement registry + scheduler in `wxbot_framework/scheduler.py`**

要点：
- 内部状态 `self._tasks: dict[tuple[str, str], TaskState]`，用 `threading.RLock` 保护；`tick()` / `run_job()` / `status()` / `register()` 都要持锁改状态，`run_job()` **在锁内不要回调插件代码**（先取状态与回调、置 `running`，再出锁执行）。
- `register()`：校验任务名（复用 `wxbot_framework.config.validate_name`）与默认表达式（`parse_schedule`，失败抛 `ValueError`）；同名重复注册抛 `ValueError`；随后按当前配置快照解析覆盖值。
- `_apply_config(state)`：读 `self._config["plugins"][plugin]["tasks"][name]`；有 `schedule` → `parse_schedule`，失败则 `state.schedule = None`、`state.config_error = str(error)`、`state.source = "config"`；没有 `schedule` → 用代码默认（`source="code"`，`config_error=""`）；`enabled` 缺省 `True`。
- `refresh()`：`self._config = self.store.load()`，然后对每个已注册任务重新 `_apply_config`。**读到非法 schedule 时只清掉该任务的 schedule，不删任务、不影响同插件的其它任务**（Review Focus 1）。
- `tick()`：取 `moment = self.now()`，按分钟键 `moment.strftime("%Y-%m-%d %H:%M")` 去重；对每个 `enabled and schedule is not None` 的任务：若 `running` → `skipped += 1`；否则若 `schedule.matches(moment)` 且 `last_fired_minute != minute_key` → 置 `last_fired_minute`、`running=True`、调 `enqueue(plugin, TaskJob(plugin, name))`，`enqueue` 返回假值时立刻 `running=False` 并 `skipped += 1`。`tick()` 整体包一层 `try/except Exception` 记 `logger.exception`，**绝不向上抛**（不能把主循环打挂）。
- `run_job(job)`：找到状态，记 `started = time.monotonic()`，调 `state.run()`；异常用 `state.run and ctx` 对应的 logger 记录 —— 用 `logging.getLogger(f"Plugin.{job.plugin}")` 的 `exception("定时任务 %s 执行失败", job.name)`；`finally` 里写 `last_ms`、`last_run_at`（ISO）、`last_ok`、`last_error`、`running=False`、`runs += 1`。任务不计入插件消息计数。
- `run_now()`：任务未注册抛 `ValueError(f"未找到任务：{plugin}/{name}")`；忽略 `enabled`，按 `tick()` 同样的方式投递（`running` 置真），投递被拒时返回 `False`。
- `set_enabled()` / `set_schedule()`：`set_schedule` 先 `parse_schedule` 校验再写；两者都调 `self.store.set_task_enabled/set_task_schedule`，把返回的新配置存进 `self._config`，对该任务 `_apply_config`，返回该任务的状态行。
- `status()`：行集合 = 已注册任务 ∪ 配置里声明过的任务名（`self._config.get("plugins", {})` 下每个插件的 `tasks` 键），按 `(plugin, name)` 排序；未注册的行 `registered=False`、`enabled` 取配置、其余计数为 0。`state` 取值：有 `config_error` → `配置错误`；插件没有存活记录（停用/未加载/根本不存在）→ `未加载`；插件有存活记录但该任务未注册 → `未注册`；已注册行按 `not enabled` → `已停用`、`running` → `运行中`、否则 `待触发`（设计 §8.6/§9.4 要求区分"未加载"与"未注册"）。`next_due` 由 `schedule.next_due(now)` 得到，`None` 就留空字符串。
- `unregister_plugin(plugin)`：删除该插件名下所有任务（停用/卸载时调用，幂等）。

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m unittest discover -s tests -p "test_scheduler.py" -v`
Expected: PASS（23 个用例全绿：11 个解析用例 + 12 个调度用例）

- [ ] **Step 5: Commit**

```bash
git add wxbot_framework/scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): 任务注册表、到期判断与运行状态"
```

---

### Task 3: 任务在插件自己的线程上执行

**Files:**
- Modify: `wxbot_framework/plugins.py`（`PluginRecord`、`PluginManager`）
- Test: `tests/test_plugin_tasks.py`

**Interfaces:**
- Consumes: Task 2 的 `Scheduler` / `TaskJob`
- Produces:
  - `plugins.JOB_QUEUE_SIZE = 8`（模块级常量）
  - `PluginRecord.__init__(self, spec, instance, context, module_name, queue_size, *, instruments=None, scheduler=None)`
  - `PluginRecord.enqueue_job(self, job) -> bool`（不活跃或队列满返回 `False`）
  - `PluginRecord.job_dropped: int`
  - `PluginManager.__init__(..., *, member_store=None, admins=None, instruments=None, scheduler=None)`
  - `PluginManager.enqueue_job(self, plugin: str, job) -> bool`

- [ ] **Step 1: Write the failing tests**

新建 `tests/test_plugin_tasks.py`，头注 `"""任务在插件线程上执行；直接构造 PluginRecord，不加载真实插件。"""`。夹具用一个最小 `spec`（`PluginSpec("demo", Path("x"), "v1", "", frozenset())`）、一个只有 `logger` 的假 context、一个记录回调顺序的假调度器：

```python
class _OrderScheduler:
    """记录进入/离开，用来验证任务回调与消息回调不重叠；与消息回调共用同一个 order 列表。"""
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
```

用例：

```python
class PluginJobChannelTests(unittest.TestCase):
    def make_record(self, order=None, scheduler=None, queue_size=8):
        spec = PluginSpec("demo", Path("demo/__init__.py"), "v1", "", frozenset())
        context = SimpleNamespace(logger=logging.getLogger("Plugin.demo"))
        instance = _BusyInstance(order if order is not None else [])
        return PluginRecord(spec, instance, context, "_wxbot_plugins.demo_x", queue_size,
                            scheduler=scheduler)

    def test_enqueue_job_is_rejected_when_inactive_or_full(self):
        record = self.make_record()
        # 任务队列容量是 JOB_QUEUE_SIZE，与消息队列的 queue_size 无关。
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
```

（`message_with_text` 是本文件的本地小工具：用 `Message(text=..., chat_id="room")` 构造。）

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m unittest discover -s tests -p "test_plugin_tasks.py" -v`
Expected: FAIL —— `AttributeError: 'PluginRecord' object has no attribute 'enqueue_job'`

- [ ] **Step 3: Implement the job channel**

要点：
- `PluginRecord` 增加 `self.jobs = queue.Queue(maxsize=JOB_QUEUE_SIZE)`、`self.job_dropped = 0`、`self.scheduler = scheduler`，以及 `enqueue_job()`（结构照抄 `enqueue()`：`state_lock` 下判 `active`、`put_nowait` 失败则 `job_dropped += 1` 并返回 `False`）。
- `_run()` 改成两路取活：每轮先 `self.jobs.get_nowait()`（有任务先做任务），没有就 `self.queue.get(timeout=0.2)`；取到任务时**同样在 `with self.callback_lock:` 里**执行，调 `self.scheduler.run_job(job)`，任务路径**不**碰 `handled`/`errors`/`instruments`（任务的成功失败由调度器自己记）；取到消息时保持现有逻辑不变（含 `plugin_scope` 与计数）。`finally: self.queue.task_done()` 只对消息路径调用。
- `PluginManager` 保存 `self.scheduler = scheduler`；`_prepare` 里把 `scheduler=self.scheduler` 传给 `PluginRecord`；新增 `enqueue_job(plugin, job) -> bool`：加锁取 `records.get(plugin)`，没有或不活跃返回 `False`，否则 `record.enqueue_job(job)`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m unittest discover -s tests -p "test_plugin_tasks.py" -v`
Expected: PASS

- [ ] **Step 5: Run the existing suite to confirm no regression**

Run: `py -3 -m unittest discover -s tests -p "test_*.py"`
Expected: 全绿（新增 3 个用例之外，原有用例不变）

- [ ] **Step 6: Commit**

```bash
git add wxbot_framework/plugins.py tests/test_plugin_tasks.py
git commit -m "feat(plugins): 任务在插件线程上串行执行"
```

---

### Task 4: `on_load` 期间注册与配置覆盖

**Files:**
- Modify: `wxbot_framework/sdk.py`（`PluginContext`）
- Modify: `wxbot_framework/plugins.py`（`_prepare`、`load_one`、`disable`、`reload`）
- Test: `tests/test_plugin_tasks.py`

**Interfaces:**
- Consumes: Task 2 的 `TaskRegistrar`、Task 3 的 `PluginManager.enqueue_job`
- Produces:
  - `PluginContext.__init__(..., tasks=None)`；`PluginContext.tasks` 属性（未注入时 `raise CapabilityError("框架未提供定时任务注册入口")`）
  - `PluginManager._prepare` 在 `on_load` 期间把 `TaskRegistrar` 交给插件，`on_load` 结束后关闭；关闭后再 `register` 抛 `RuntimeError("定时任务只能在 on_load 期间注册")`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_plugin_tasks.py`。夹具：临时插件目录（在 `ROOT/.runtime/tests/<temp>` 下写 `plugins/demo/__init__.py`）+ 临时 `bot_config.json`：

```python
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
```

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m unittest discover -s tests -p "test_plugin_tasks.py" -v`
Expected: FAIL —— `AttributeError: 'PluginContext' object has no attribute 'tasks'`

- [ ] **Step 3: Wire registration**

要点：
- `sdk.PluginContext.__init__` 加关键字参数 `tasks=None`，存 `self._tasks`；加属性 `tasks`（未注入时 `raise CapabilityError("框架未提供定时任务注册入口")`）。
- `PluginManager._prepare`：构造 `registrar = TaskRegistrar(self.scheduler, spec.name)`，传给 `PluginContext(tasks=registrar)`；`on_load` 调完后**在 `finally` 里** `registrar.close()`（`on_load` 抛异常也要关）。
- 加载成功后对比"配置里声明过的任务名"与"实际注册的任务名"，多出来的用 `logger.warning("插件 %s 配置里声明了未注册的任务：%s", name, ...)`（Review Focus 1 的可见性要求）。
- `load_one`：在 `_prepare` 之前调 `self.scheduler.unregister_plugin(name)`（重载即重新注册，幂等）。
- `disable`：在从 `records` 摘除之后调 `self.scheduler.unregister_plugin(name)`。
- `reload`：`self.config = self.store.load()` 之后立刻调 `self.scheduler.refresh()`，保证插件注册时看到的是新配置。
- `PluginManager.__init__` 中 `scheduler` 缺省为 `None` 时，测试会传；运行时由 Task 6 传真实调度器。若为 `None`，`ctx.tasks` 注入 `None`，插件调用时得到 `CapabilityError`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m unittest discover -s tests -p "test_plugin_tasks.py" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wxbot_framework/sdk.py wxbot_framework/plugins.py tests/test_plugin_tasks.py
git commit -m "feat(plugins): 插件在 on_load 中注册定时任务并接受配置覆盖"
```

---

### Task 5: 配置校验与任务开关/时间的原子写

**Files:**
- Modify: `wxbot_framework/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ConfigStore.load()` / `_save()`（既有）
- Produces:
  - `ConfigStore.set_task_enabled(self, plugin: str, task: str, enabled: bool) -> dict`
  - `ConfigStore.set_task_schedule(self, plugin: str, task: str, schedule: str) -> dict`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_config.py`（复用其 `VALID`、`load`、`reject` 夹具）：

```python
    def test_tasks_section_is_optional(self):
        data = dict(VALID, plugins={"demo": {"enabled": True}})
        self.assertEqual(self.load(data), data)

    def test_non_dict_tasks_is_rejected(self):
        for value in ([], "x", 1, True):
            with self.subTest(tasks=value):
                self.reject(dict(VALID, plugins={"demo": {"tasks": value}}),
                            "plugins.demo.tasks 必须是 JSON 对象")

    def test_bad_task_name_is_rejected(self):
        for name in ("Daily", "daily-broadcast", "", "1daily"):
            with self.subTest(name=name):
                self.reject(dict(VALID, plugins={"demo": {"tasks": {name: {}}}}),
                            "任务名不合法")

    def test_task_entry_must_be_object_with_valid_fields(self):
        self.reject(dict(VALID, plugins={"demo": {"tasks": {"daily": []}}}),
                    "plugins.demo.tasks.daily 必须是 JSON 对象")
        self.reject(dict(VALID, plugins={"demo": {"tasks": {"daily": {"enabled": "yes"}}}}),
                    "plugins.demo.tasks.daily.enabled 必须是布尔值")
        self.reject(dict(VALID, plugins={"demo": {"tasks": {"daily": {"schedule": ""}}}}),
                    "plugins.demo.tasks.daily.schedule 必须是非空字符串")
        self.reject(dict(VALID, plugins={"demo": {"tasks": {"daily": {"schedule": 9}}}}),
                    "plugins.demo.tasks.daily.schedule 必须是非空字符串")

    def test_set_task_enabled_and_schedule_only_touch_that_key(self):
        data = dict(VALID, admin={"port": 8787},
                    plugins={"demo": {"enabled": True, "prefix": "echo ",
                                      "tasks": {"daily": {"schedule": "每天 21:00"}}}})
        path = self.root / "bot_config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        store = ConfigStore(path)
        store.set_task_enabled("demo", "daily", False)
        store.set_task_schedule("demo", "daily", "每天 09:00")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["plugins"]["demo"]["tasks"]["daily"],
                         {"schedule": "每天 09:00", "enabled": False})
        self.assertEqual(saved["plugins"]["demo"]["prefix"], "echo ")
        self.assertEqual(saved["admin"], {"port": 8787})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m unittest discover -s tests -p "test_config.py" -v`
Expected: FAIL —— 前四个用例报「配置未被拒绝」，`set_task_enabled` 报 `AttributeError`

- [ ] **Step 3: Implement validation and writers in `wxbot_framework/config.py`**

要点：
- 在 `load()` 里 `plugins` 校验那段之后，对每个插件的 `options` 追加 `tasks` 校验（文案与现有风格一致）：`tasks` 必须是对象；任务名用 `validate_name`（失败时包成 `ValueError(f"plugins.{name}.tasks 的任务名不合法：{task!r}")`，保证既能被 `reject(..., "任务名不合法")` 匹配，也保留可读上下文）；每个任务必须是对象；`enabled` 若存在必须是 bool；`schedule` 若存在必须是非空 `str`。
- 新增两个方法，形制照抄 `set_enabled`：`validate_name(plugin)`、`validate_name(task)`、`data = self.load()`、按需 `setdefault`、写一个键、`return self._save(data)`。

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m unittest discover -s tests -p "test_config.py" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wxbot_framework/config.py tests/test_config.py
git commit -m "feat(config): 校验任务配置并提供任务开关/时间的原子写"
```

---

### Task 6: 主循环 tick 与控制台命令

**Files:**
- Modify: `wxbot_framework/runtime.py`
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: Task 2 的 `Scheduler`、Task 3 的 `PluginManager.enqueue_job`、Task 5 的 `ConfigStore.set_task_*`
- Produces: `BotApplication.scheduler`；控制台命令 `tasks`、`enable <插件> <任务>`、`disable <插件> <任务>`、`run <插件> <任务>`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_console.py`（复用其 `make_app`；给 `make_app` 增加 `scheduler=None` 关键字并挂到 `app.scheduler`，不影响现有调用）：

```python
    def test_tasks_command_logs_each_row(self):
        rows = [{"plugin": "demo", "name": "daily", "schedule": "每天 21:00", "state": "待触发",
                 "skipped": 0, "last_run_at": None, "last_ok": None, "last_error": "", "config_error": ""}]
        app = self.make_app(scheduler=Mock(status=Mock(return_value=rows)))
        info = self.logged(app, "tasks")
        rendered = " ".join(str(arg) for call in info.call_args_list for arg in call.args)
        self.assertIn("demo", rendered)
        self.assertIn("daily", rendered)

    def test_task_enable_and_disable_go_through_scheduler(self):
        scheduler = Mock()
        app = self.make_app(scheduler=scheduler)
        app.execute("disable demo daily")
        scheduler.set_enabled.assert_called_once_with("demo", "daily", False)
        app.execute("enable demo daily")
        scheduler.set_enabled.assert_called_with("demo", "daily", True)

    def test_task_command_with_bad_name_warns_without_traceback(self):
        scheduler = Mock()
        scheduler.set_enabled.side_effect = ValueError("未找到任务：demo/ghost")
        app = self.make_app(scheduler=scheduler)
        with patch.object(runtime.logger, "warning") as warning:
            app.execute("disable demo ghost")
        self.assertIn("未找到任务", warning.call_args.args[1] % warning.call_args.args[2:])

    def test_run_command_requires_registered_task(self):
        scheduler = Mock()
        scheduler.run_now.return_value = True
        app = self.make_app(scheduler=scheduler)
        app.execute("run demo daily")
        scheduler.run_now.assert_called_once_with("demo", "daily")

    def test_help_lists_task_commands(self):
        info = self.logged(self.make_app(), "help")
        self.assertIn("tasks", info.call_args.args[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m unittest discover -s tests -p "test_console.py" -v`
Expected: FAIL —— `tasks` 与 `run` 落到「未知命令」分支抛 `ValueError`

- [ ] **Step 3: Wire the scheduler into `BotApplication`**

要点：
- `__init__`：在构造 `PluginManager` 之前建 `self.scheduler = Scheduler(store)`；把 `scheduler=self.scheduler` 传给 `PluginManager`；构造完之后 `self.scheduler.bind(self.plugins.enqueue_job)`。
- `run()`：把现有 `if self.members is not None and now >= next_rollover:` 那一块改成 `if now >= next_rollover:` 内先做 `if self.members is not None: self.members.rollover()`，再 `self.scheduler.tick()`，最后 `next_rollover = now + 0.5`（保持 0.5 秒节拍，**且成员库未配置时也照常 tick**）。
- `execute()`：
  - `tasks`（1 个词）→ 逐行 `logger.info("%s/%s [%s] %s 上次=%s", ...)`，字段取 `scheduler.status()`；用 `row["..."]` 直接取（`status()` 保证这些键都在，包含 `last_run_at`），不要用 `.get()` 兜底。
  - `enable`/`disable`（3 个词）→ `self.scheduler.set_enabled(plugin, task, action == "enable")`，`ValueError` 记 `logger.warning("命令未执行：%s", error)`。
  - `run`（3 个词）→ `self.scheduler.run_now(plugin, task)`，同上处理 `ValueError`；投递被拒时记一条 warning（"任务未能投递"）。
  - `help` 文案追加 `tasks / enable 插件 任务 / disable 插件 任务 / run 插件 任务`。
- `start_admin()` **本任务不要动**：`AdminApi` 的 `scheduler` 参数是 Task 7 才加的，现在传会让 3 个管理台用例因 `TypeError` 变红（并被 `start_admin` 的 `except Exception` 吞成"管理台启动失败"）。接线放在 Task 7。

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m unittest discover -s tests -p "test_console.py" -v`
Expected: PASS

- [ ] **Step 5: Run the existing suite**

Run: `py -3 -m unittest discover -s tests -p "test_*.py"`
Expected: 全绿

- [ ] **Step 6: Commit**

```bash
git add wxbot_framework/runtime.py tests/test_console.py
git commit -m "feat(runtime): 主循环调度 tick 与任务控制台命令"
```

---

### Task 7: 管理台任务接口

**Files:**
- Modify: `wxbot_framework/admin/api.py`
- Modify: `wxbot_framework/runtime.py`（本任务给 `AdminApi.__init__` 加 `scheduler` 参数后，在 `start_admin()` 的 `AdminApi(...)` 里补传 `scheduler=self.scheduler`——Task 6 传不了，因为那时这个参数还不存在）
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: Task 2 的 `Scheduler.status/set_enabled/set_schedule/run_now`
- Produces: `AdminApi.__init__(..., scheduler=None)`；路由 `GET /api/tasks`、`POST /api/tasks/<插件>/<任务>/toggle|schedule|run`

- [ ] **Step 1: Write the failing tests**

追加到 `tests/test_admin_api.py`（沿用其假对象风格，新增一个 `_FakeScheduler`）；同时在该文件的 `setUp` 里构造 `self.scheduler = _FakeScheduler()`，并在构造 `self.api` 时传 `scheduler=self.scheduler`，下面的用例才拿得到它：

```python
class _FakeScheduler:
    def __init__(self):
        self.calls = []
        self.rows = [{"plugin": "demo", "name": "daily", "schedule": "每天 21:00",
                      "state": "待触发", "enabled": True, "registered": True}]
        self.error = None
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
        return True
```

用例（走 `api.handle(...)`）：

```python
    def test_get_tasks_returns_scheduler_rows(self):
        status, body = self.api.handle("GET", "/api/tasks", {}, {})
        self.assertEqual((status, body["tasks"]), (200, self.scheduler.status()))

    def test_post_task_toggle_passes_enabled_flag(self):
        status, body = self.api.handle("POST", "/api/tasks/demo/daily/toggle", {}, {"enabled": False})
        self.assertEqual(status, 200)
        self.assertIn(("set_enabled", "demo", "daily", False), self.scheduler.calls)

    def test_post_task_schedule_rejects_invalid_expression_as_400(self):
        self.scheduler.error = ValueError("每天 25:00 不是合法的时间表达式")
        status, body = self.api.handle("POST", "/api/tasks/demo/daily/schedule", {},
                                       {"schedule": "每天 25:00"})
        self.assertEqual(status, 400)
        self.assertIn("25:00", body["error"])

    def test_post_task_run_and_unknown_path(self):
        status, _ = self.api.handle("POST", "/api/tasks/demo/daily/run", {}, {})
        self.assertEqual((status, self.scheduler.calls[-1]), (200, ("run_now", "demo", "daily")))
        status, body = self.api.handle("POST", "/api/tasks/demo/daily/explode", {}, {})
        self.assertEqual(status, 400)
        self.assertIn("接口不存在", body["error"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `py -3 -m unittest discover -s tests -p "test_admin_api.py" -v`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'scheduler'`

- [ ] **Step 3: Implement routes in `wxbot_framework/admin/api.py`**

要点：
- `__init__` 增加 `scheduler=None`，存 `self.scheduler`。
- `wxbot_framework/runtime.py` 的 `start_admin()` 里给 `AdminApi(...)` 补传 `scheduler=self.scheduler`（这一行归本任务，Task 6 不能传）。
- `_route`：`GET` 分支加 `if path == "/api/tasks": return STATUS_OK, {"tasks": self._tasks()}`；`POST` 分支加 `if path.startswith("/api/tasks/"): return self._task_route(path, body)`。
- `_tasks()`：`self.scheduler` 为 `None` 时抛 `RuntimeError("调度器未启用")`，最终落到 **400**（既有 `_runtime_result` 只对含「已关闭」的文本给 503，400 与既有「管理台未启用」同口径），否则返回 `self.scheduler.status()`。
- `_task_route(path, body)`：把 `/api/tasks/<插件>/<任务>/<动作>` 切四段（不足四段或动作为空 → `STATUS_BAD_REQUEST, {"error": f"接口不存在：{path}"}`）；插件名与任务名走 `validate_name`；`toggle` 取 `body.get("enabled")`（非 bool → `ValueError`）、`schedule` 取 `body.get("schedule")`；动作不识别同样返回 400。返回值统一 `{"ok": True, ...行}`。
- 路径不做 URI 解码以外的特殊处理，与现有 `_split_route` 风格保持一致。

- [ ] **Step 4: Run tests to verify they pass**

Run: `py -3 -m unittest discover -s tests -p "test_admin_api.py" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add wxbot_framework/admin/api.py tests/test_admin_api.py
git commit -m "feat(admin): 新增定时任务接口"
```

---

### Task 8: 管理台「定时任务」页面

**Files:**
- Modify: `wxbot_framework/admin/web/app.js`
- Modify: `wxbot_framework/admin/web/style.css`（仅在需要新增类时）

**Interfaces:**
- Consumes: Task 7 的 `/api/tasks` 四个接口
- Produces: 路由 `#/tasks` 与页面渲染 `renderTasks(main)`

- [ ] **Step 1: 加路由、导航与渲染函数**

要点（三处改动 + 一个新函数，全部照 `app.js` 现有写法）：
- `currentRoute()`：在 `if (parts[0] === 'debug')` 之前加 `if (parts[0] === 'tasks') return { name: 'tasks' };`
- `buildNav()`：在 `nav.append(navLink('概览', ...))` 之后、`nav.append(el('div', { class: 'nav-group', text: '插件' }))` 之前加 `nav.append(navLink('定时任务', '#/tasks', target.name === 'tasks'));`
- `route()`：在 `if (target.name === 'debug')` 之前加 `if (target.name === 'tasks') return renderTasks(main);`
- 新写 `renderTasks(main)`（仿 `renderLogs`）：`pageTitle('定时任务', ...)`（真实签名是 `pageTitle(title, sub)`，照文件现有用法）→ `const data = await api('/api/tasks')` → `tableBox` 渲染表格，列为「插件 / 任务 / 生效时间 / 来源 / 状态 / 上次运行 / 跳过 / 下次触发 / 操作」。操作列每行三个控件：开关（`POST /api/tasks/<插件>/<任务>/toggle`，body `{"enabled": !current}`）、时间输入框 + 保存按钮（`POST .../schedule`，body `{"schedule": 值}`）、「跑一次」按钮（`POST .../run`，响应里 `dispatched !== true` 时提示"任务未能投递"）。任一请求失败时用 `errorBlock` 显示后端返回的 `error` 原文，成功后就地重取列表刷新。空列表时显示「还没有定时任务：插件还没有注册任何任务，或插件未加载。」。
- 「来源」列：`code` → `（代码默认）`，`config` → `（配置覆盖）`；状态为「配置错误」时把 `config_error` 原文显示出来，「上次运行」列附 `last_error` 原文。

- [ ] **Step 2: 手工核对（前端无自动化测试，这是本任务唯一的验证）**

1. 用临时配置启动：`py -3 main.py`（或仓库文档里的启动方式），确认日志出现 `管理台已启动：http://127.0.0.1:8787/`。
2. 浏览器打开该地址，左侧导航应出现「定时任务」，点开后表格有行（若当前没有插件注册任务，则出现上面那句空状态文案）。
3. 点某行「跑一次」→ 该行「上次运行」立即更新；把时间输入框改成 `每天 09:00` 保存 → 「生效时间」变为新值且来源标为「配置覆盖」；打开仓库根的 `bot_config.json`，确认 `plugins.<插件>.tasks.<任务>.schedule` 已写入该值，且同插件其它键未被改动。
4. 在同一个页面把时间改成 `每天 25:00` 保存 → 页面显示后端返回的错误原文，`bot_config.json` 保持不变。
5. 关闭开关后刷新页面 → 状态显示为「已停用」；控制台执行 `tasks` 看到的开关与页面一致。

- [ ] **Step 3: Commit**

```bash
git add wxbot_framework/admin/web/app.js wxbot_framework/admin/web/style.css
git commit -m "feat(admin): 新增定时任务页面"
```

---

### Task 9: BGA 每日排行榜播报（独立仓库 `plugins/bga_plugin`）

**Files:**
- Modify: `plugins/bga_plugin/bga/commands.py`
- Modify: `plugins/bga_plugin/__init__.py`
- 无自动化测试（该插件是独立 git 仓库，主仓库测试不覆盖它），验证见 Step 3

**Interfaces:**
- Consumes: 框架的 `ctx.tasks.register(name, schedule, run)`；本插件的 `deps.client` / `deps.store` / `deps.config`
- Produces:
  - `bga.commands.leaderboard_text(chat_id: str, deps) -> str`（与 `/bga排行榜` 同一条取数路径、同一 `RANKING_MAX` 与字节预算）
  - `Plugin._broadcast(self, ctx) -> None`；任务名 `daily_broadcast`，默认 `每天 21:00`

- [ ] **Step 1: 把排行榜渲染改成按 `chat_id` 可调用**

`commands.py`：把 `_ranking(text, message, deps)` 改成 `_ranking(chat_id, deps)`（函数体里原来用 `message.chat_id` 的两处——缓存键与 `config.game_for`——改用形参），`_dispatch` 里的调用改成 `_ranking(message.chat_id, deps)`；新增公开入口：

```python
def leaderboard_text(chat_id: str, deps) -> str:
    """定时播报入口：与 /bga排行榜 完全同一条取数路径与字节预算。"""
    return _ranking(chat_id, deps)
```

- [ ] **Step 2: 注册任务并实现播报**

`__init__.py`：文件顶部补 `import functools`。
- `on_load` 末尾（`self.worker.start()` 之后）注册：`ctx.tasks.register("daily_broadcast", "每天 21:00", functools.partial(self._broadcast, ctx))`。**注意**：配置缺失（`ConfigError` 分支）或初始化失败（`_release()` 分支）时直接 `return`，不注册任务——没有配置就没有可播报的内容。
- 实现 `_broadcast`：

```python
    def _broadcast(self, ctx):
        """逐个已配置的群播报该群所配桌游的当前排行榜；单群失败不影响其它群。"""
```

  规则：遍历 `sorted(self.deps.config.groups)`；每个群调 `leaderboard_text(chat_id, self.deps)`，用 `ctx.send_text(chat_id, clip(text))` 发出；**单群异常只记 `ctx.logger.exception` 并继续下一个群**；若一个群都没成功（或没有任何已配置的群）则抛出最后一个异常/`RuntimeError("没有可播报的群或全部失败")`，好让调度器把这次运行记成失败；成功时逐个 `ctx.logger.info("BGA 排行榜已播报：%s", chat_id)`。
- 播报**不**写快照、**不**改动 `SnapshotWorker`。

- [ ] **Step 3: 手工验证**

1. 启动机器人（`py -3 main.py`），控制台执行 `plugins` 确认 `bga_plugin` 处于运行、`tasks` 里能看到 `bga_plugin/daily_broadcast`（生效时间 `每天 21:00`，来源 `代码默认`）。
2. 控制台执行 `run bga_plugin daily_broadcast` → 日志出现「BGA 排行榜已播报：<群ID>」；对应微信群里收到与 `/bga排行榜` 同格式的文本（头条与条数上限一致）。
3. 控制台执行 `disable bga_plugin daily_broadcast` → `tasks` 里该任务显示「已停用」；`bot_config.json` 的 `plugins.bga_plugin.tasks.daily_broadcast.enabled` 为 `false`。**注意**：`plugins/bga_plugin/` 被主仓库 `.gitignore` 忽略，这条配置键写在主仓库的 `bot_config.json` 里，插件仓库自身不含它。
4. 在这台机器的插件仓库里提交这两处改动（独立仓库，与主仓库分开提交）。

---

## Self-Review

**1. Spec coverage**

| Spec 章节 | 落在哪个任务 |
|---|---|
| §2 目标 1/6（插件注册、在插件线程执行） | Task 3、Task 4 |
| §2 目标 2（cron + 语义化） | Task 1 |
| §2 目标 3/7（框架管配置与启停） | Task 4、Task 5、Task 7 |
| §2 目标 4（不补跑） | Task 2（分钟粒度去重） |
| §2 目标 5（控制台） | Task 6 |
| §2 目标 7（管理台页面） | Task 7、Task 8 |
| §7 配置结构与两类 schedule 出错处理 | Task 2、Task 4、Task 5 |
| §8.3 执行位置（插件线程串行） | Task 3 |
| §8.5 失败处理与未注册任务 warning | Task 2、Task 4 |
| §8.7 `status` 结构不变 | 未动 `status`（Task 6 只加新命令） |
| §9 管理台页面与四个接口 | Task 7、Task 8 |
| §10 BGA 播报 | Task 9 |
| §13 验证清单 1–13 | 覆盖：1/2 → Task 1+2；3 → Task 3；4 → Task 2；5/6 → Task 2+4；7 → Task 2；8 → Task 6；9/10 → Task 2+4；11 → Task 9 Step 3；12/13 → Task 7+8 |

**2. Step scan**：每个 Step 只要求一件事（写测试 / 跑测试 / 实现 / 跑测试 / 提交）；Task 8 是前端，没有测试框架，因此它的"可核对结果"落在 Step 2 的手工核对清单上，并明确标注这是本任务唯一验证。

**3. Type consistency**：`TaskJob(plugin, name)`、`TaskState` 字段名、`Scheduler` 九个方法、`PluginRecord.enqueue_job(job)`、`PluginManager.enqueue_job(plugin, job)`、`ConfigStore.set_task_enabled/set_task_schedule`、`AdminApi(scheduler=...)` 在后续任务的 Interfaces 里逐字一致。

**4. Review Focus**：五条各自有归属——1 落 Task 2 的 `test_invalid_config_schedule_keeps_task_unfired_and_visible` 与 Task 4 的 `test_invalid_config_schedule_keeps_plugin_loaded`；2 落 Task 1 的 `test_next_due_returns_none_for_impossible_expression`；3 落 Task 2 的 `test_tick_skips_while_previous_run_is_still_running`；4 落 Task 2 的 `test_run_job_records_success_and_failure`（失败被记录、不重试）；5 落 Task 1 的 `test_next_due_crosses_month_boundary`、`test_semantic_forms_match_their_cron_equivalents`（跨周末）与 `test_last_day_of_month_form_is_not_supported`。

**5. Proportion**：计划长度与规格相当；代码块只出现在测试与"签名决定不了"的地方（解析与 `next_due` 的算法要点、`_broadcast` 的失败规则），其余是签名与断言名。
