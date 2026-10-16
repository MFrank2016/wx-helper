# 微信机器人管理台（admin）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给这个微信机器人框架加一个跟机器人同进程启动的本地网页管理台，能看统计、可视化配置插件、调试插件、检索日志。

**Architecture:** 在 `wxbot_framework/` 下新增 `admin/` 包，分成三层：数据层（独立 sqlite 库 + 只读聚合查询）、旁路记录层（埋点与预览拦截，未启用时是空实现）、HTTP 层（标准库 `http.server` + 静态页面）。消息路径只在三处加薄埋点：消息入口、插件回调、发送收口，且不改变分发顺序与回调语义。插件通过在 `plugin.json` 里声明配置与页面接入管理台。

**Tech Stack:** Python 3.13（仅标准库：`http.server`、`sqlite3`、`logging`、`threading`）；前端为纯 HTML/CSS/JS，无构建步骤；测试用仓库现有的 `unittest`。

**Spec:** `docs/superpowers/specs/2026-10-16-wxbot-admin-panel-design.md`

## Global Constraints

- 运行环境：Windows 10/11 x64、64 位 Python（仓库已验证 3.13）。
- **不新增任何第三方依赖**：`requirements.txt` 保持只有 `pefile==2023.2.7`；只允许标准库。
- 管理台与机器人**同进程、同生命周期**。收尾顺序：先停管理台服务（关监听、等在途请求、落盘）→ 取消待发送 → 停插件 → 断连接 → 关成员库。
- 管理台只绑 `127.0.0.1`，不设登录；端口默认 `8787`，被占用时顺延。
- 统计库固定在 `.runtime/admin_data.sqlite3`（独立于成员库）。
- 消息日志保留 7 天，且不超过 200000 行。
- `bot_config.json` 新增 `admin` 段：`enabled` 默认 `true`，`port` 默认 `8787`。
- **不改动消息判定与分发顺序，不阻塞插件回调**；埋点只在旁路记录，记录失败不得影响收发。
- 调试的假消息**不经过**白名单过滤、管理员拦截、人格切换和成员库写入；发送始终被拦成预览，除非显式打开开关。
- 插件配置里的密钥字段（`account.password`、`api_key` 等）不在接口里回显明文，未修改则不写回。
- 所有新增文案、注释、日志一律用中文，与仓库现有风格一致。

## Review Focus

以下五类输入或失败模式最容易咬到用的人，每条都要有对应测试固定住，测试放在拥有该代码的任务里：

1. **管理台未启用或库打不开时，消息路径必须与从前完全一致**：`admin.enabled=false`、库文件不可写、磁盘满，任何一种情况下收到消息都要照常分发，不抛异常、不多写日志噪音。
2. **记录失败不得冒泡到消息路径**：`AdminStore` 写入或落盘抛错时，收发的返回值与顺序不变；错误只记一条框架日志。
3. **调试期间真实消息仍在进来**：预览开关不能把真实回复也吞掉——真实消息的发送必须正常发出，而调试请求自己的发送被拦成预览。
4. **端口被占用**：8787 被别的程序占用时要能顺延到下一个可用端口并把实际地址写进日志，不能让机器人启动失败。
5. **插件声明放行了实际非法值**：声明允许但插件 `on_load` 拒绝时（例如 bga 配置非法只降级警告），管理台必须把「已降级 / 尚未配置」当一等状态显示，而不是静默显示成功。

---

### Task 1: 统计库（建表、缓冲写入、落盘、保留清理）

**Files:**
- Create: `wxbot_framework/admin/__init__.py`
- Create: `wxbot_framework/admin/store.py`
- Test: `tests/test_admin_store.py`

**Interfaces:**
- Consumes: 无（本任务不依赖其它任务）
- Produces:
  - `admin.store.Record`：`@dataclass(frozen=True)`，字段 `kind: str`（`receive`/`send`/`plugin`/`error`/`system`）、`chat_id: str = ""`、`sender_id: str = ""`、`plugin: str = ""`、`is_group: bool = False`、`allowed: bool = True`、`summary: str = ""`、`ts_utc: str = ""`（空则由 store 填当前 UTC）
  - `admin.store.SUMMARY_LIMIT = 500`
  - `admin.store.beijing_day_bounds(day: str) -> tuple[str, str]`：入参 `"2026-10-16"`，返回该北京日的 `[start, end)` UTC ISO 字符串
  - `admin.store.AdminStore(path, *, retention_days: int = 7, max_rows: int = 200000, flush_seconds: float = 1.0, now=None)`
  - `.open() -> None`、`.close() -> None`、`.append(record: Record) -> None`、`.bump(scope: str, scope_key: str, metric: str, amount: int = 1) -> None`、`.flush() -> None`、`.prune() -> int`、`.path`、`.last_error: str`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_store.py
class AdminStoreTests(unittest.TestCase):
    def test_append_then_flush_persists_row(self)      # 写入一条 receive，flush 后另开连接查 message_log 有 1 行且 summary 被截断到 500 字符
    def test_append_without_flush_is_buffered(self)    # append 后不 flush，另开连接查不到该行
    def test_bump_accumulates_and_survives_reopen(self) # bump('group', ROOM, 'received', 3) 两次，flush、close、重新 open 后 totals() 为 6
    def test_prune_removes_rows_older_than_retention(self)  # 手工插入 8 天前的行，prune() 返回 1 且该行消失
    def test_prune_removes_oldest_when_over_max_rows(self)  # max_rows=3 时插 5 行，prune() 只保留最新 3 行
    def test_append_never_raises_when_database_is_unwritable(self)  # 库路径指向不存在且不可建的目录时 append/flush 不抛异常，last_error 非空
    def test_beijing_day_bounds_is_utc_half_open(self)  # beijing_day_bounds("2026-10-16") == ("2026-10-15T16:00:00+00:00", "2026-10-16T16:00:00+00:00")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_store -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'wxbot_framework.admin'`

- [ ] **Step 3: 实现 `admin/store.py`**

建表 SQL（与成员库同样的显式 `CREATE TABLE IF NOT EXISTS` 风格）：

```sql
CREATE TABLE IF NOT EXISTS message_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, kind TEXT NOT NULL,
  chat_id TEXT NOT NULL DEFAULT '', sender_id TEXT NOT NULL DEFAULT '',
  plugin TEXT NOT NULL DEFAULT '', is_group INTEGER NOT NULL DEFAULT 0,
  allowed INTEGER NOT NULL DEFAULT 1, summary TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS log_by_kind ON message_log(kind, id);
CREATE INDEX IF NOT EXISTS log_by_chat ON message_log(chat_id, id);
CREATE INDEX IF NOT EXISTS log_by_plugin ON message_log(plugin, id);
CREATE TABLE IF NOT EXISTS stats_total(
  scope TEXT NOT NULL, scope_key TEXT NOT NULL, metric TEXT NOT NULL,
  value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, scope_key, metric));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

要点：`append`/`bump` 只往内存缓冲加（`bump` 累加到 `{(scope,scope_key,metric): amount}`）；`flush()` 在一个事务里批量 `executemany` 并把累计用 `ON CONFLICT ... DO UPDATE SET value = value + excluded.value` 合并；缓冲区达到 200 条时提前 flush；任何 `sqlite3.Error`/`OSError` 都吞掉、记 `last_error` 并写一条框架日志（`logging.getLogger("Admin")`），`close()` 必须先 flush 再关连接。`prune()` 先按 `ts_utc < beijing_day_bounds(今天-start) ` 删旧行，再按 `id` 保留最新 `max_rows` 行。`beijing_day_bounds` 用 `timezone(timedelta(hours=8))` 计算北京 0 点，与 `members.py:209` 的做法一致（不要引入 `zoneinfo`，Windows 上未必有 tz 数据）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_store -v`
Expected: PASS（7 个用例）

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/__init__.py wxbot_framework/admin/store.py tests/test_admin_store.py
git commit -m "feat(admin): 新增管理台统计库与消息记录表"
```

---

### Task 2: 统计与日志查询

**Files:**
- Create: `wxbot_framework/admin/queries.py`
- Test: `tests/test_admin_queries.py`

**Interfaces:**
- Consumes: `admin.store.AdminStore`、`admin.store.beijing_day_bounds`（Task 1）
- Produces:
  - `admin.queries.AdminQueries(store, *, name_resolver=None)`，`name_resolver(wxid: str, chat_id: str) -> str | None`
  - `.summary(day: str) -> dict`：`{"today": {"received": int, "sent": int, "handled": int, "errors": int, "received_group": int, "received_direct": int}, "total": {...同名键...}}`
  - `.groups(day: str, *, limit: int = 50) -> list[dict]`：`{"chat_id", "today_received", "today_sent", "total_received", "top_plugin", "last_ts"}`
  - `.trend(*, days: int = 7, day: str) -> list[dict]`：`{"day", "received", "sent"}`
  - `.talkers(day: str, *, chat_id: str | None = None, limit: int = 10) -> list[dict]`：`{"sender_id", "name", "count"}`
  - `.logs(*, kinds: list[str] | None = None, keyword: str = "", chat_id: str = "", plugin: str = "", since: str = "", until: str = "", limit: int = 100, offset: int = 0) -> dict`：`{"rows": [...], "total": int}`
  - `.plugin_today(day: str) -> dict[str, dict]`：插件名 → `{"handled", "errors"}`（供 API 层与 `PluginManager.status()` 合并）

- [ ] **Step 1: 写失败测试**

```python
class AdminQueriesTests(unittest.TestCase):
    def test_summary_counts_today_and_total_separately(self)  # 昨天插 2 条 receive、今天插 3 条，summary(今天)["today"]["received"]==3 且 ["total"]["received"]==5
    def test_summary_splits_group_and_direct(self)            # 群 2 条、私聊 1 条 → received_group==2、received_direct==1
    def test_summary_ignores_denied_messages(self)             # allowed=0 的 receive 不计入任何数字（Review Focus 5 之外的口径防线）
    def test_groups_lists_top_plugin_of_today(self)            # 同群今天的 plugin 记录里 idiom_chain 3 条、echo 1 条 → top_plugin=="idiom_chain"
    def test_trend_returns_seven_days_in_order(self)           # 返回 7 项、日期递增、今天在最后
    def test_talkers_uses_name_resolver(self)                  # name_resolver 返回"阿花"时 rows[0]["name"]=="阿花"；返回 None 时回落为 sender_id
    def test_logs_filter_by_kind_keyword_and_paging(self)      # kinds=["error"] 只回异常行；keyword 命中 summary；limit=1 时 total 仍为命中总数
    def test_logs_filter_by_plugin_and_chat(self)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_queries -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'wxbot_framework.admin.queries'`

- [ ] **Step 3: 实现 `admin/queries.py`**

要点：`day` 一律由调用方传入（便于测试），时间范围用 `beijing_day_bounds(day)`；`ts_utc` 是规范化 ISO UTC 字符串，范围比较直接用字符串比较即可。今日数字从 `message_log` 聚合，累计数字读 `stats_total`（`scope='global'`）；「今日处理/异常」从 `message_log` 里 `kind IN ('plugin','error')` 统计；`groups` 的 `total_received` 读 `stats_total` 的 `scope='group'`；`keyword` 用 `summary LIKE ?` 与 `chat_id LIKE ?` 的组合，转义 `%`/`_`；`logs` 先 `SELECT COUNT(*)` 再取分页。全部只读、不加锁（sqlite 允许并发读）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_queries -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/queries.py tests/test_admin_queries.py
git commit -m "feat(admin): 统计与日志查询"
```

---

### Task 3: 埋点上下文与记录（未启用时是空实现）

**Files:**
- Create: `wxbot_framework/admin/instrument.py`
- Test: `tests/test_admin_instrument.py`

**Interfaces:**
- Consumes: `admin.store.Record`、`admin.store.AdminStore`（Task 1）
- Produces:
  - `admin.instrument.Instruments(*, store=None)`；`.enabled: bool`（有 store 才为真）、`.attach(store) -> None`、`.detach() -> None`
  - `.record_receive(message, *, allowed: bool) -> None`
  - `.record_send(to_wxid: str, content: str, *, plugin: str = "", preview: bool = False) -> None`
  - `.record_plugin(name: str, *, handled: bool, ms: float, replied: bool) -> None`
  - `.record_error(name: str, error: BaseException) -> None`
  - `.record_system(level: str, name: str, text: str) -> None`
  - `.current_plugin: str`（线程局部，插件回调期间有效，其它线程为空串）
  - `@contextmanager .plugin_scope(name: str, *, replied=lambda: False)`（内部用可调用对象判断「这条消息有没有被回复过」，因为发送与判断都在回调内）
  - `@contextmanager .preview_scope()` + `.previewing: bool`
  - `@contextmanager .collect(records: list)`：调试期间临时把每条记录同时追加进给定列表（退出后恢复原状；正常运行时零开销）

- [ ] **Step 1: 写失败测试**

```python
class InstrumentsTests(unittest.TestCase):
    def test_disabled_instruments_drop_everything(self)      # store=None 时调用全部 record_* 不产生任何记录、不抛异常
    def test_enabled_instruments_write_expected_records(self) # attach(store) 后 record_receive/record_send/record_plugin/record_error 各产生一条，kind 与字段正确
    def test_current_plugin_is_thread_local(self)            # 主线程调 plugin_scope("echo") 时，另一线程读到 current_plugin == ""
    def test_record_send_marks_preview_rows(self)            # preview=True 的记录 kind=="send" 且 summary 前缀 "[预览] "
    def test_previewing_flag_is_scoped(self)                 # preview_scope 退出后 previewing 为 False，异常退出也复位
    def test_plugin_scope_reports_replied(self)              # scope 内触发一次 replied 标记，record_plugin 的 replied=True
    def test_collect_captures_records_and_stops_after_exit(self)  # collect 期间记录同时进列表；退出后列表不再增长（Task 7 的调试靠它汇聚）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_instrument -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 `admin/instrument.py`**

要点：所有 `record_*` 先判 `if not self.enabled: return`，再 `try: store.append(...) except Exception: 记日志不抛`。线程局部用 `threading.local()`；预览标志用一个全局 `bool` + `threading.Lock`，只允许同一时刻有一个调试请求（`preview_scope` 进入时若已在预览中则抛 `RuntimeError("已有调试在进行")`）。`plugin_scope` 用一个可变的 `replied` 标志对象传给调用方（Task 4 里发送成功时置位）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_instrument -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/instrument.py tests/test_admin_instrument.py
git commit -m "feat(admin): 埋点上下文与记录接口"
```

---

### Task 4: 消息路径接入埋点（行为必须不变）

**Files:**
- Modify: `wxbot_framework/runtime.py`（`BotApplication.__init__`、`_emit`）
- Modify: `wxbot_framework/plugins.py`（`PluginManager.__init__`、`PluginRecord.__init__`/`_run`）
- Modify: `wxbot_framework/sdk.py`（`ServiceFacade.__init__`、`send_text`、`LegacyService`）
- Test: `tests/test_admin_instrument.py`（追加用例）、`tests/test_framework.py`（追加用例）

**Interfaces:**
- Consumes: `admin.instrument.Instruments`（Task 3）
- Produces（后续任务依赖的构造签名）：
  - `ServiceFacade(*, text_delay=None, instruments=None)`
  - `PluginManager(directory, store, service, capabilities, queue_size=128, *, member_store=None, admins=None, instruments=None)`
  - `PluginManager.call_web(name: str, action: str, params: Mapping) -> dict`：在持有该插件的回调锁时调用插件的 `on_web(action, params)`；插件未实现 `on_web` 抛 `ValueError("插件未提供管理页面数据")`，插件不活跃时抛 `ValueError("插件未在运行")`（Task 8 的插件页接口靠它取数，Task 15 的 `on_web` 靠它被调用）
  - `BotApplication.instruments`（构造后即可用，默认空实现）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_instrument.py（追加）
class MessagePathInstrumentsTests(unittest.TestCase):
    def test_emit_records_receive_with_allowlist_flag(self)   # 白名单内的群消息 → 一条 receive 且 allowed=True；白名单外的 → allowed=False
    def test_emit_dispatch_order_and_counts_unchanged(self)   # 用假 service/plugins 跑 5 条消息，插件收到的条数/顺序与无 instruments 时逐一相同
    def test_record_failure_does_not_break_dispatch(self)     # store.append 抛异常时 _emit 仍完成分发（Review Focus 2）
    def test_disabled_admin_leaves_behavior_identical(self)    # admin_enabled=False 时 _emit 与 _run 不产生任何记录（Review Focus 1）
    def test_plugin_run_records_handled_and_replied(self)      # 回调里调 ctx.reply 成功后 record_plugin 的 replied=True；未调用则 False
    def test_plugin_error_still_recorded_and_swallowed(self)   # 回调抛错 → 一条 error 记录、errors 计数 +1、后续消息继续处理
    def test_send_preview_is_recorded_and_not_sent(self)       # instruments.preview_scope 内调 send_text → 返回 True、backend.send_text 未被调用、记录带 [预览]
    def test_real_send_not_swallowed_during_preview(self)      # 预览期间另一线程发真实消息 → 真的调用 backend.send_text（Review Focus 3）
    def test_call_web_invokes_plugin_hook(self)               # 插件实现了 on_web → 返回它的 dict
    def test_call_web_rejects_missing_hook_and_inactive_plugin(self)  # 未实现 on_web → ValueError；插件已停用 → ValueError
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_instrument -v`
Expected: FAIL（`ServiceFacade` 不接受 `instruments`；无 receive/plugin 记录）

- [ ] **Step 3: 实现**

- `runtime.BotApplication.__init__`：`self.instruments = Instruments()`；构造 `ServiceFacade(..., instruments=self.instruments)` 与 `PluginManager(..., instruments=self.instruments)`。
- `runtime._emit`：在方法最前面算 `allowed`（把现有 `allowed = self.config.get("chat_allowlist", [])` 提到前面），并 `self.instruments.record_receive(message, allowed=...)`；**其余判断逻辑一行不动**，也不因记录而提前返回。
- `plugins.PluginRecord._run`：用 `with self.instruments.plugin_scope(self.spec.name, replied=lambda: replied)` 包住现有回调调用；成功走 `record_plugin(handled=True, replied=...)`；异常走 `record_error`（现有 `logger.exception` 保留）。
- `sdk.ServiceFacade.send_text`：在拿到发送锁、准备真正发送之前插入预览分支——`if self._instruments is not None and self._instruments.previewing: self._instruments.record_send(to_wxid, content, plugin=self._instruments.current_plugin, preview=True); return True`；真实发送成功后 `record_send(..., preview=False)` 并把所属 scope 的 `replied` 置位。**不得改变锁的获取顺序与既有的会话校验顺序。**
- 插件命中判定只依赖「本次回调期间是否发生过成功发送」，不新增任何等待或同步。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_instrument -v` 与 `python -m unittest tests.test_framework -v`
Expected: 全部 PASS（含既有用例，行为无回归）

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/runtime.py wxbot_framework/plugins.py wxbot_framework/sdk.py tests/test_admin_instrument.py tests/test_framework.py
git commit -m "feat(admin): 在消息路径埋入收发与插件处理记录"
```

---

### Task 5: 插件配置声明（解析、读值、校验、写回）

**Files:**
- Create: `wxbot_framework/admin/schema.py`
- Test: `tests/test_admin_schema.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `admin.schema.SaveError(ValueError)`
  - `admin.schema.Field(key, label, type, default, help, range, choices)`（`@dataclass(frozen=True)`；`type ∈ {"text","password","int","float","bool","enum","map","json"}`）
  - `admin.schema.Section(title, fields)`
  - `admin.schema.Declaration(source, file, sections, pages)`；`source ∈ {"bot_config","plugin_file"}`；`pages: tuple[str, ...]`
  - `admin.schema.load_declaration(plugin_dir: Path) -> Declaration | None`
  - `admin.schema.read_values(decl, *, plugin_dir: Path, bot_config: Mapping) -> dict`（点路径键 → 当前值；缺失则用声明的默认值）
  - `admin.schema.validate_values(decl, values: Mapping) -> dict`（返回规范化后的值；失败抛 `SaveError`）
  - `admin.schema.write_values(decl, values: Mapping, *, plugin_dir: Path, bot_config_path: Path) -> dict`（原子写；返回写回后的完整配置对象）

- [ ] **Step 1: 写失败测试**

```python
class AdminSchemaTests(unittest.TestCase):
    def test_missing_config_section_returns_none(self)              # plugin.json 没有 config 段 → None（旧插件不受影响）
    def test_unknown_field_type_is_rejected(self)                   # type="colour" → ValueError，且错误信息含插件名与字段名
    def test_source_must_be_one_of_two(self)
    def test_plugin_file_source_requires_file_name(self)            # source=plugin_file 但没写 file → ValueError
    def test_read_values_returns_defaults_for_missing_keys(self)    # bga 声明读空配置 → ranking_depth==1000、cache_seconds==300
    def test_read_values_uses_dotted_path_for_nested_keys(self)     # account.email 从 {"account":{"email":"a@b"}} 读到 "a@b"
    def test_validate_rejects_out_of_range_int(self)                # ranking_depth=0 → SaveError，信息含 "1–10000"
    def test_validate_rejects_wrong_type_and_keeps_bool_distinct(self)  # "abc" 当 int → SaveError；bool 不当作 int
    def test_validate_enum_checks_choices(self)
    def test_validate_map_requires_string_keys_and_values(self)      # groups 传 {"": "yatzy"} → SaveError
    def test_write_values_to_plugin_file_is_atomic_and_keeps_unknown_keys(self)  # 写 bga_config.json 后未知键仍在、临时文件不残留、换行与缩进保持仓库风格
    def test_write_values_to_bot_config_preserves_enabled(self)      # 写 bot_config 的 plugins.echo.prefix 后 plugins.echo.enabled 仍为 True
    def test_write_values_creates_missing_nested_object(self)        # bot_config 里没有 plugins.echo 时能建出来
    def test_save_error_leaves_file_untouched(self)                  # 校验失败时磁盘上的文件字节与写入前一致
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_schema -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 `admin/schema.py`**

要点：声明的字段类型到 Python 类型的映射只此一处；`map` 用「键值表」表示（键与值都必须是字符串，`groups` 这种群→桌游）；点路径读写用共用的小工具 `_get_path`/`_set_path`；写文件复用 `ConfigStore._write` 的原子写法（同目录 `mkstemp` → `os.replace`，UTF-8、`\n`、`indent=2`、`ensure_ascii=False`），但不要改动 `config.py` 的现有方法——在 `schema.py` 内实现一份等价的 `_atomic_write(path, data)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_schema -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/schema.py tests/test_admin_schema.py
git commit -m "feat(admin): 插件配置声明的解析、校验与写回"
```

---

### Task 6: HTTP 服务（绑定、端口顺延、路由、静态资源）

**Files:**
- Create: `wxbot_framework/admin/server.py`
- Test: `tests/test_admin_server.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `admin.server.AdminServer(api, *, host="127.0.0.1", port=8787, static_dir=None, plugin_dir=None, port_retries=10)`
    （`static_dir` 是管理台自己的页面目录；`plugin_dir` 是 `plugins/` 根目录，用于插件自带的 `web/` 资源）
  - `.start() -> str`（返回形如 `http://127.0.0.1:8787/` 的实际地址；端口被占用则顺延，10 次都失败抛 `OSError`）
  - `.stop() -> None`（关监听、等在途请求结束，幂等）
  - `.port: int`、`.url: str`
  - `api` 契约（Task 7 实现）：`api.handle(method: str, path: str, query: dict, body: dict) -> tuple[int, dict]`

- [ ] **Step 1: 写失败测试**

```python
class AdminServerTests(unittest.TestCase):
    def test_start_returns_loopback_url_on_requested_port(self)
    def test_occupied_port_falls_through_to_next(self)      # 先占用 PORT，再 start → .port == PORT+1 且 .url 用实际端口（Review Focus 4）
    def test_get_static_file_is_served_with_utf8(self)      # static_dir 下放 index.html → GET / 返回 200 与文件内容
    def test_unknown_path_returns_404_json(self)
    def test_post_json_body_is_passed_to_api(self)          # POST /api/x 带 JSON → api.handle 收到解析后的 dict
    def test_bad_json_body_returns_400(self)
    def test_static_path_traversal_is_rejected(self)        # GET /../bot_config.json → 404，不读到库外文件
    def test_plugin_static_files_are_served_from_plugin_dir(self)  # plugin_dir 下 echo/web/panel.html → GET /plugin/echo/panel.html 返回 200
    def test_plugin_static_rejects_bad_plugin_name_and_traversal(self)  # /plugin/../bot_config.json 与 /plugin/Bad Name/x → 404
    def test_stop_is_idempotent_and_frees_port(self)        # stop 两次不报错；stop 后端口可再次绑定
    def test_head_request_and_missing_static_dir_do_not_crash(self)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_server -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 `admin/server.py`**

用 `http.server.ThreadingHTTPServer` + 自定义 `BaseHTTPRequestHandler`；路由前缀 `/api/` 交给 `api.handle`，其余走静态目录（默认 `Path(__file__).parent / "web"`）；只允许 `GET`/`POST`/`HEAD`；响应统一 JSON（`ensure_ascii=False`、UTF-8）；把 `log_message` 覆写为走 `logging.getLogger("Admin")` 的 DEBUG，避免污染控制台；路径解析后必须 `resolve()` 并确认仍在静态目录内。`stop()` 用 `shutdown()` + `server_close()` 并加锁保证幂等。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_server -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/server.py tests/test_admin_server.py
git commit -m "feat(admin): 本地 HTTP 服务与静态资源路由"
```

---

### Task 7: 调试（预览拦截与结果汇聚）

**Files:**
- Create: `wxbot_framework/admin/debug.py`
- Test: `tests/test_admin_debug.py`

**Interfaces:**
- Consumes: `admin.instrument.Instruments`（Task 3）、`PluginManager`（Task 4）
- Produces:
  - `admin.debug.Debugger(plugins, instruments, *, timeout=30.0)`
  - `.run(*, plugin: str = "", chat_id: str, sender_id: str = "", text: str, send_for_real: bool = False) -> dict`
    返回 `{"plugins": [{"name", "state": "replied"|"silent"|"error"|"skipped", "reply", "ms", "error"}], "dispatched": int, "replied": int, "ms": float, "sent_for_real": bool}`

- [ ] **Step 1: 写失败测试**

```python
class AdminDebugTests(unittest.TestCase):
    def test_synthetic_message_does_not_touch_member_store_or_admins(self)  # 用假 members/admins，run 后成员库与管理员判定记录为 0 次调用
    def test_preview_reply_is_captured_not_sent(self)        # 插件 ctx.reply → 结果里 reply 有文本、假 backend.send_text 未被调用
    def test_silent_plugins_are_listed(self)                 # 3 个插件都未命中 → dispatched==3、replied==0、每条 state=="silent"
    def test_error_plugin_is_reported_with_traceback_summary(self)
    def test_filtering_to_single_plugin_skips_others(self)   # plugin="echo" 时其它插件不出现在结果里
    def test_plugin_timeout_is_reported_not_hung(self)       # 插件睡 2 秒、timeout=0.2 → state=="error" 且总耗时 < 2 秒
    def test_send_for_real_only_sends_debug_reply(self)      # send_for_real=True 时假 backend 恰好收到 1 条、且是调试会话的地址
    def test_concurrent_real_message_is_not_swallowed(self)  # 预览期间另一线程发送真实消息 → 真实发送发生（Review Focus 3）
    def test_unknown_plugin_name_is_rejected(self)           # plugin="nope" → ValueError
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_debug -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 `admin/debug.py`**

要点：构造 `Message(text=..., chat_id=..., sender_id=..., type=1, is_group=chat_id.endswith("@chatroom"))`，**直接调用 `plugins.dispatch(message)`**，不进 `_emit`；在 `instruments.preview_scope()` 里跑（`send_for_real=True` 时不进预览，只把调试消息标记为「调试」以便结果归集）。结果汇聚方式：给 `Instruments` 加一个可选的「会话级收集器」（`collect(records_list)` 上下文，仅调试期间启用），按 `plugin` 归集记录；超时用「等所有插件队列 `task_done`」配合 `queue.Queue.join()` 加超时线程——超时后不再等待，把未完成的插件标成 `error: 超时`。**不要为了调试去 monkeypatch 插件内部。**

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_debug -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/debug.py tests/test_admin_debug.py
git commit -m "feat(admin): 插件调试与预览拦截"
```

---

### Task 8: API 组装

**Files:**
- Create: `wxbot_framework/admin/api.py`
- Test: `tests/test_admin_api.py`

**Interfaces:**
- Consumes: Task 1/2/5/6/7 的全部产物 + `PluginManager`
- Produces:
  - `admin.api.AdminApi(*, store, queries, plugins, instruments, debugger, plugin_dir: Path, bot_config_path: Path, config_loader)`
    （`config_loader() -> Mapping` 用于读当前 `bot_config.json`，注入以便测试）
  - `.handle(method, path, query, body) -> tuple[int, dict]`
  - 路由：`GET /api/stats/summary`、`GET /api/stats/groups`、`GET /api/stats/trend`、`GET /api/stats/talkers`、`GET /api/plugins`、`GET /api/plugins/<name>/config`、`POST /api/plugins/<name>/config`、`POST /api/plugins/<name>/toggle`、`POST /api/plugins/<name>/reload`、`GET /api/logs`、`POST /api/debug`、`GET /api/plugin/<name>/<action>`（插件自带页面的数据，转给 `PluginManager.call_web`）

- [ ] **Step 1: 写失败测试**

```python
class AdminApiTests(unittest.TestCase):
    def test_stats_summary_returns_today_and_total_cards(self)
    def test_plugins_list_merges_live_status_and_declaration(self)  # 每个插件含 active/enabled/handled/errors/dropped/error/has_config/pages；今日处理数取自 queries.plugin_today
    def test_plugin_page_data_goes_through_call_web(self)          # GET /api/plugin/bga_plugin/status → plugins.call_web("bga_plugin","status",{}) 被调用一次，结果原样返回
    def test_plugin_page_data_rejects_unknown_plugin_and_missing_hook(self)  # 未知插件或插件没实现 on_web → 400，带原因
    def test_plugin_config_returns_sections_with_values_and_masks_secrets(self)  # password 字段的 value 为 "" 且 masked=True
    def test_plugin_config_save_reloads_and_reports_load_error(self)  # 保存后调 plugins.reload(name)；load_errors 有值时响应里 degraded=True（Review Focus 5）
    def test_plugin_config_save_rejects_bad_value_with_400(self)      # 越界值 → 400，且文件未改动
    def test_plugin_without_declaration_reports_declared_false(self)  # 无 config 段 → 返回原始 JSON 视图标记
    def test_toggle_enable_disable_calls_plugin_manager(self)         # 断言 enable/disable 被调用一次，失败时状态码 400 且带原因
    def test_reload_without_name_reloads_all(self)
    def test_logs_endpoint_passes_filters_through(self)
    def test_debug_endpoint_requires_chat_id_and_text(self)           # 缺字段 → 400
    def test_unknown_route_returns_404(self)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_api -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现 `admin/api.py`**

要点：本文件只做「路由 + 把各处拼起来 + 把异常转成状态码」，不放业务算法；`CapabilityError`/`ValueError`/`SaveError` 一律 400 且带上错误原文（页面直接显示给用户）；`plugins.status()` 的实时数据与 `queries` 的今日统计按插件名合并；开关与重载都走 `PluginManager` 的公开方法，不自己操作 `records`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_api -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/api.py tests/test_admin_api.py
git commit -m "feat(admin): 管理台 API 路由"
```

---

### Task 9: 系统日志接入（框架警告也进日志页）

**Files:**
- Create: `wxbot_framework/admin/handler.py`
- Modify: `main.py`（`configure_logging`）
- Test: `tests/test_admin_handler.py`

**Interfaces:**
- Consumes: `admin.instrument.Instruments`（Task 3）
- Produces:
  - `admin.handler.AdminLogHandler(instruments)`：`logging.Handler` 子类，`level=logging.WARNING`，`emit()` 把 `record.levelname` 与格式化后的消息交给 `instruments.record_system`
  - `main.configure_logging(config, *, instruments=None)`

- [ ] **Step 1: 写失败测试**

```python
class AdminLogHandlerTests(unittest.TestCase):
    def test_warning_and_above_become_system_records(self)   # WARNING/ERROR 各一条 → 两条 kind=="system" 记录
    def test_info_is_ignored(self)
    def test_handler_failure_does_not_break_logging(self)    # instruments 抛异常时 logging 调用链不受影响、其它 handler 仍收到记录
    def test_configure_logging_attaches_handler_only_when_instruments_given(self)  # 用假 config 与临时 logs 目录断言 handler 数量
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_handler -v`
Expected: FAIL，模块不存在

- [ ] **Step 3: 实现**

要点：`main.configure_logging` 保持原有控制台 + 轮转文件两个 handler 不变，只在传入 `instruments` 且 `instruments.enabled` 时追加第三个 handler；`emit()` 自己处理格式化（不依赖 `setFormatter`），并且绝不抛异常；`main()` 里改成 `configure_logging(config, instruments=app.instruments)`（`app` 在 `main.py:54` 已构造）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_handler -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/admin/handler.py main.py tests/test_admin_handler.py
git commit -m "feat(admin): 框架警告与错误进入日志页"
```

---

### Task 10: 接进生命周期（启动、收尾、地址落日志）

**Files:**
- Modify: `wxbot_framework/runtime.py`（`__init__`、`run`、`stop`）
- Test: `tests/test_framework.py`（追加用例）

**Interfaces:**
- Consumes: Task 1/3/6/7/8
- Produces:
  - `BotApplication.admin`：`None` 或 `SimpleNamespace(store=..., server=..., api=..., url=str)`（未启用时为 `None`）
  - `.start_admin() -> str | None`、`.stop_admin() -> None`（便于测试直接调用）

- [ ] **Step 1: 写失败测试**

```python
class AdminLifecycleTests(unittest.TestCase):
    def test_admin_disabled_creates_no_store_and_no_server(self)   # admin.enabled=False → app.admin is None、库文件不存在（Review Focus 1）
    def test_start_admin_opens_store_attaches_instruments_and_returns_url(self)
    def test_start_admin_failure_is_logged_and_does_not_stop_bot(self)  # 库目录不可写 / 端口全占用 → 返回 None、记录一条 ERROR、app 照常运行
    def test_stop_admin_flushes_and_closes_store(self)             # stop 后缓冲区为空、连接已关、再次 stop 不报错
    def test_run_starts_admin_before_plugins_reload(self)          # 从 run() 的调用顺序断言「先起管理台、再起插件」以免漏掉开头的处理记录
    def test_stop_order_shuts_admin_down_first(self)               # 收尾顺序：管理台 → plugins.shutdown → 断连接 → members.close
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_framework -v`
Expected: FAIL（`BotApplication` 无 `admin`/`start_admin`）

- [ ] **Step 3: 实现**

要点：`__init__` 里按 `self.config.get("admin", {})` 决定 `self._admin_enabled`；`start_admin()` 负责建 `AdminStore` → `open()` → `instruments.attach(store)` → 建 `AdminQueries`/`Debugger`/`AdminApi` → `AdminServer.start()`，并把地址写进日志（`logger.info("管理台已启动：%s", url)`）；任何异常都吞掉并 `logger.exception("管理台启动失败；机器人继续运行")` 后返回 `None`；`stop_admin()` 先 `server.stop()`、再 `store.close()`、再 `instruments.detach()`，并保证幂等。`run()` 里在 `plugins.reload()` 之前调用 `start_admin()`，`stop()` 里在最前面调用 `stop_admin()`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_framework -v`
Expected: PASS（含既有用例）

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/runtime.py tests/test_framework.py
git commit -m "feat(admin): 管理台接入机器人启动与收尾"
```

---

### Task 11: 配置项（`admin` 段校验、示例、README）

**Files:**
- Modify: `wxbot_framework/config.py`
- Modify: `bot_config.example.json`
- Modify: `README.md`
- Test: `tests/test_config.py`（追加用例）

**Interfaces:**
- Consumes: 无
- Produces: `bot_config.json` 的 `admin` 段：`{"enabled": true, "port": 8787}`；`ConfigStore.load()` 对其做校验（`enabled` 必须是布尔；`port` 必须是 1–65535 的整数；整段缺省时按默认值补全到返回值里）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py（追加）
def test_admin_section_defaults_when_missing(self)        # 不带 admin 段 → 返回 {"enabled": True, "port": 8787}
def test_admin_enabled_must_be_bool(self)
def test_admin_port_range_is_enforced(self)               # 0、"8787"、70000 都拒绝，错误信息含 "admin.port"
def test_admin_unknown_keys_are_rejected(self)            # admin 段里的拼错键（如 "prot"）要报错，避免静默无效配置
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_config -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `ConfigStore.load()` 里按现有风格加一段校验（错误信息用中文、带字段名），并把默认值补进返回的 `data`（与 `log_level`、`console` 一致的做法：`data.get("admin", ...)`）；`bot_config.example.json` 加 `"admin": {"enabled": true, "port": 8787}`；README 的「目录」和「首次配置」附近补一节说明管理台地址、数据文件位置、日志保留 7 天、以及「日志页会记录消息正文，属于本机数据」。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_config -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/config.py bot_config.example.json README.md tests/test_config.py
git commit -m "feat(admin): admin 配置段与文档"
```

---

### Task 12: 控制台 `admin` 命令

**Files:**
- Modify: `wxbot_framework/runtime.py`（`execute`、`help` 文案）
- Test: `tests/test_console.py`（追加用例）

**Interfaces:**
- Consumes: `BotApplication.admin`（Task 10）
- Produces: 控制台命令 `admin`（打印管理台地址；未启用或启动失败时打印原因）；`help` 文案追加该项

- [ ] **Step 1: 写失败测试**

```python
# tests/test_console.py（追加）
def test_admin_command_logs_url(self)          # app.admin.url="http://127.0.0.1:8787/" → 日志里出现该地址
def test_admin_command_reports_disabled(self)  # app.admin is None → 提示「管理台未启用」
def test_admin_command_rejects_extra_arguments(self)   # "admin now" → ValueError（与现有命令的参数校验一致）
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_console -v`
Expected: FAIL

- [ ] **Step 3: 实现**

在 `execute` 的分支链条里加 `elif action == "admin" and len(words) == 1:`，并更新 `help` 的那行文案。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_console -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add wxbot_framework/runtime.py tests/test_console.py
git commit -m "feat(admin): 控制台 admin 命令"
```

---

### Task 13: 前端页面（四页 + ❓浮窗 + 日志配色）

**Files:**
- Create: `wxbot_framework/admin/web/index.html`
- Create: `wxbot_framework/admin/web/style.css`
- Create: `wxbot_framework/admin/web/app.js`

**Interfaces:**
- Consumes: Task 8 的 API 路由与响应结构
- Produces: 单页应用，左侧导航（概览 / 插件 / 调试 / 日志），四个页面按草图实现

- [ ] **Step 1: 搭页面骨架与样式**

单文件 `index.html` + `style.css` + `app.js`（原生 `fetch`，无框架、无构建）；左侧导航固定，右侧内容区；中文界面；配色与草图一致（收到 `#eef4ff`、发出 `#eefbf1`、插件处理 `#f5f0ff`、异常 `#fdeeee`、系统 `#f5f6f8`）。

版式以 `.superpowers/brainstorm/1161-1792147571/content/` 下的四张草图为准（`layout.html` 定导航结构、`plugin-config.html` 定配置页与 ❓ 浮窗、`log-page.html` 定日志页、`stats-debug.html` 与 `overview-and-plugin-page.html` 定统计页与插件自带页）；草图只定结构与信息层级，细节按 CSS 变量统一收口，不做响应式适配。

- [ ] **Step 2: 概览页**

今日四张卡片（收到 / 发出 / 插件处理 / 异常，异常卡片红底）→ 累计四张卡片 → 近 7 天趋势柱（蓝=收到、绿=发出）→ 今天谁在聊 → 各群表（今日收到/发出、累计收到、最活跃插件、最近一条）→ 各插件表（状态、处理、异常、丢弃、最近错误）。所有数字来自 `/api/stats/*`，空数据要有明确的空状态文案。

- [ ] **Step 3: 插件页（配置）**

左侧导航列出全部插件；选中后右侧显示：顶部启用开关 + 「保存并重载」按钮；分区（如「账号」「群与桌游」「抓取」）；每行「字段名 ❓ + 输入框」，默认值预填；点 ❓ 弹出浮窗显示该字段的 `help` 与 `range`；保存失败时在按钮旁显示后端返回的错误原文；保存成功但插件降级（`degraded=true`）时显示黄色警告条与最近错误。`password` 字段显示为掩码占位，留空表示不改。

- [ ] **Step 4: 调试页**

左侧表单（插件下拉、会话 ID、发送者、内容、跑一遍、真的发到微信、跑完自动重载），右侧结果（命中插件与将回复的文本、看过但没回的插件、异常与堆栈、耗时）；「真的发到微信」默认关闭且刷新后复位；页面上常驻提示「调试会真实调用外部服务，可能产生费用或触发限流，也会改动插件的内部状态」。

- [ ] **Step 5: 日志页**

左侧类型列（全部/收到/发出/插件处理/异常/系统，各带条数）+ 右侧日志流（整行底色、时间、类型、会话或插件、内容）；顶部搜索框与群/插件/时间过滤；「跟随最新」开关，暂停后可翻页。

- [ ] **Step 6: 手工核对**

Run: `python main.py --run-seconds=20`
Expected: 日志里出现「管理台已启动：http://127.0.0.1:8787/」；浏览器打开该地址，逐项核对：四个页面都能打开、导航切换无报错、❓ 浮窗有内容、日志页在无记录时有空状态、控制台 `admin` 命令打印同一地址、`quit` 后端口释放。（不连接微信也应能完成；未连微信时插件列表可能为空或有降级提示，那是预期。）

- [ ] **Step 7: 提交**

```bash
git add wxbot_framework/admin/web
git commit -m "feat(admin): 管理台前端页面"
```

---

### Task 14: 给现有插件补配置声明

**Files:**
- Modify: `plugins/echo/plugin.json`
- Modify: `plugins/idiom_chain/plugin.json`
- Modify: `plugins/chat_summary/plugin.json`
- Modify: `plugins/_template/plugin.json`（加注释性示例）
- Test: `tests/test_admin_schema.py`（追加用例）

**Interfaces:**
- Consumes: `admin.schema`（Task 5）
- Produces: 三个插件的 `config` 声明，与它们 `on_load` 里的实际校验逐条对应（`echo.prefix` 非空字符串；`idiom_chain.member_names` 为群→成员→昵称的对象；`chat_summary.chunk_chars` 为 4000–64000 整数、默认 16000）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_admin_schema.py（追加）
class RepositoryDeclarationsTests(unittest.TestCase):
    def test_every_plugin_declaration_parses(self)          # 遍历 plugins/*/plugin.json，能解析的声明都不抛异常
    def test_declared_ranges_match_plugin_validation(self)   # echo.prefix 空串被声明拒绝；chat_summary.chunk_chars 传入 1000 被声明拒绝（与 summary.py 的 4000 下限一致）
    def test_declared_bot_config_source_writes_expected_path(self)  # echo 的声明 source=="bot_config"、字段键为 "prefix"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_schema -v`
Expected: FAIL（三个插件还没有 `config` 段）

- [ ] **Step 3: 写声明**

按 `admin/schema.py` 的格式为三个插件各写一段（`source: "bot_config"`），字段的 `help` 用一句人话说明「是什么、默认多少、能填什么」，`range` 写清取值范围或类型。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_schema -v` 与 `python main.py --check`
Expected: PASS；`--check` 输出「配置与插件语法检查通过」

- [ ] **Step 5: 提交**

```bash
git add plugins/echo/plugin.json plugins/idiom_chain/plugin.json plugins/chat_summary/plugin.json plugins/_template/plugin.json tests/test_admin_schema.py
git commit -m "feat(admin): 为现有插件补配置声明"
```

---

### Task 15: bga_plugin 适配（独立 git 仓库，单独提交）

> 本任务的文件全部位于 `plugins/bga_plugin/`，那是**独立的 git 仓库**，提交要在该目录内单独做，不要混进主仓库的提交。

**Files:**
- Modify: `plugins/bga_plugin/plugin.json`
- Modify: `plugins/bga_plugin/__init__.py`（新增 `on_web`）
- Create: `plugins/bga_plugin/web/ranking.html`
- Test: `tests/test_admin_schema.py`（在主仓库追加一条跨仓库的声明一致性用例）

**Interfaces:**
- Consumes: `admin.schema`（Task 5）、`admin.server` 的 `/plugin/<name>/...` 与 `/api/plugin/<name>/<action>` 路由
- Produces:
  - `plugins/bga_plugin/plugin.json` 的 `config` 段：`source: "plugin_file"`、`file: "bga_config.json"`、`pages: [{"key": "ranking", "title": "排行榜"}]`、8 个字段（`account.email`、`account.password`、`groups`、`default_game`、`ranking_depth`、`cache_seconds`、`request_interval_seconds`、`daily_snapshot_hour`），默认值与范围照 README 那张表
  - `Plugin.on_web(action: str, params: dict) -> dict`，支持 `status`（登录状态、昨日快照条数、已绑定玩家数、趋势可算天数）、`groups`（各群当前桌游与上次抓取）、`ranking`（指定群的排行榜预览）

- [ ] **Step 1: 写失败测试（在主仓库）**

```python
# tests/test_admin_schema.py（追加）
class BgaDeclarationTests(unittest.TestCase):
    def test_bga_declaration_matches_its_own_validator(self)
        # 用 admin.schema 读 plugins/bga_plugin/plugin.json，对每项传入非法值断言被拒绝，
        # 且这些值与 plugins/bga_plugin/bga/config.py 中的范围一致（ranking_depth 1–10000、cache_seconds 0–3600、
        # request_interval_seconds 0.5–60、daily_snapshot_hour 0–23）
    def test_bga_declaration_reads_defaults_from_empty_config(self)  # 空配置 → 1000 / 300 / 1.0 / 4
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest tests.test_admin_schema -v`
Expected: FAIL（bga 的 `plugin.json` 还没有 `config` 段）

- [ ] **Step 3: 写声明与自有页面**

`plugin.json` 加 `config` 段（`account.password` 的 `type` 为 `password`）；`__init__.py` 加 `on_web`，只读地取插件已有的状态（登录态、`snapshot` 记录数、绑定数、`store` 里的游戏名），**不新增写操作、不发任何消息**；`web/ranking.html` 用 `fetch('/api/plugin/bga_plugin/status')` 渲染。若某项数据插件本身没有现成的取法，就在页面上如实显示「暂不可用」，不要为了展示新增抓取逻辑。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m unittest tests.test_admin_schema -v`（主仓库）与 `python main.py --check`
Expected: PASS

- [ ] **Step 5: 提交（两个仓库分别提交）**

```bash
git -C plugins/bga_plugin add plugin.json __init__.py web
git -C plugins/bga_plugin commit -m "feat(admin): 配置声明与管理台排行榜页"
git add tests/test_admin_schema.py
git commit -m "test(admin): 校验 bga 插件声明与其自身校验一致"
```

---

## 交付后手工验收

1. `python main.py --check` 通过。
2. `python -m unittest discover -s tests -p "test_*.py"` 全绿。
3. `python main.py` 启动后，浏览器打开日志里打印的地址：改一次 bga 的 `ranking_depth` 并保存，页面提示重载成功；用调试页预览 `/bga排行榜` 看到回复内容但不发群；在日志页按「插件处理」过滤能看到这次调试的记录。
4. 关掉管理台配置（`admin.enabled=false`）再启动：日志里没有管理台地址，`.runtime/admin_data.sqlite3` 不被创建，机器人收发正常。
5. `quit` 退出：端口释放（再启动能回到 8787），统计库文件已落盘。
