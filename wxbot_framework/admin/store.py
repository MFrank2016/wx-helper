"""管理台统计库：消息记录、累计计数与保留清理，写入走内存缓冲后定时落盘。"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOGGER = logging.getLogger("Admin")

# 北京时间固定 +8，不用 zoneinfo：Windows 上未必带时区数据。
BEIJING = timezone(timedelta(hours=8))

SUMMARY_LIMIT = 500
# 缓冲满这么多条就唤醒定时线程尽快落盘；落盘一律在定时线程里做，不阻塞收发路径。
BUFFER_LIMIT = 200
# 库长期不可写时，未落盘的记录最多留这么多条，避免一直占内存。
PENDING_LIMIT = 10000
# 关闭时等定时线程退出的上限，秒。
JOIN_TIMEOUT = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT NOT NULL, kind TEXT NOT NULL,
  chat_id TEXT NOT NULL DEFAULT '', sender_id TEXT NOT NULL DEFAULT '',
  plugin TEXT NOT NULL DEFAULT '', is_group INTEGER NOT NULL DEFAULT 0,
  allowed INTEGER NOT NULL DEFAULT 1, summary TEXT NOT NULL DEFAULT '',
  msg_key TEXT NOT NULL DEFAULT '', ms INTEGER);
CREATE INDEX IF NOT EXISTS log_by_kind ON message_log(kind, id);
CREATE INDEX IF NOT EXISTS log_by_chat ON message_log(chat_id, id);
CREATE INDEX IF NOT EXISTS log_by_plugin ON message_log(plugin, id);
CREATE TABLE IF NOT EXISTS stats_total(
  scope TEXT NOT NULL, scope_key TEXT NOT NULL, metric TEXT NOT NULL,
  value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, scope_key, metric));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# 旧库升级：两个新列只能靠 `ALTER TABLE ... ADD COLUMN` 补，`CREATE TABLE IF NOT EXISTS`
# 遇到已存在的表什么都不做。先 `PRAGMA table_info` 看列在不在，补列幂等，重复启动无副作用；
# 加列不动已有数据，历史行该列为空（`msg_key=''`、`ms` 为 NULL）。
# - `msg_key`：消息关联标识（由 `runtime.new_message_key` 生成），同一条上游消息触发的
#   receive / plugin / error / send / preview 都写同一个值；空串表示无关联（旧行与没有
#   消息上下文的记录，如调试预览、定时任务里的发送）。
# - `ms`：插件处理耗时（整数毫秒）；只有 `kind='plugin'` 的行有值，异常等其余行留空。
MIGRATION_COLUMNS = (
    ("msg_key", "msg_key TEXT NOT NULL DEFAULT ''"),
    ("ms", "ms INTEGER"),
)
# 补列之后才能建这个索引：旧库首次启动时列还不存在，把它写进 SCHEMA 会让整段建表脚本失败。
MIGRATION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS log_by_msg ON message_log(msg_key, id)",
)

RECORD_COLUMNS = "ts_utc,kind,chat_id,sender_id,plugin,is_group,allowed,summary,msg_key,ms"
RECORD_PLACEHOLDERS = "?,?,?,?,?,?,?,?,?,?"


def beijing_day_bounds(day):
    """把北京日期（YYYY-MM-DD）换算成该日 [开始, 结束) 的 UTC ISO 字符串。"""
    date = datetime.strptime(day, "%Y-%m-%d")
    start = datetime(date.year, date.month, date.day, tzinfo=BEIJING)
    return (start.astimezone(timezone.utc).isoformat(),
            (start + timedelta(days=1)).astimezone(timezone.utc).isoformat())


def _migrate(db):
    """把已存在的库补到当前表结构：只加列与建索引，不重建表、不搬数据。"""
    existing = {row["name"] for row in db.execute("PRAGMA table_info(message_log)")}
    for column, ddl in MIGRATION_COLUMNS:
        if column not in existing:
            db.execute(f"ALTER TABLE message_log ADD COLUMN {ddl}")
    for statement in MIGRATION_INDEXES:
        db.execute(statement)


@dataclass(frozen=True)
class Record:
    kind: str
    chat_id: str = ""
    sender_id: str = ""
    plugin: str = ""
    is_group: bool = False
    allowed: bool = True
    summary: str = ""
    ts_utc: str = ""
    # 消息关联标识：同一条上游消息触发的记录共用一个值；空串表示无关联。
    msg_key: str = ""
    # 插件处理耗时（整数毫秒）；只有 `kind='plugin'` 的记录有值。
    ms: int | None = None
    # 插件异常时随记录带回来的格式化堆栈（截断），只供管理台调试页展示；
    # `AdminStore.append` 写的固定几列里没有它。
    traceback: str = ""


class AdminStore:
    """管理台自己的 sqlite 库；落盘失败只记 last_error 和一条日志，绝不向外抛。"""

    def __init__(self, path, *, retention_days=7, max_rows=200000, flush_seconds=1.0, now=None):
        self.path = Path(path).resolve()
        self.retention_days = retention_days
        self.max_rows = max_rows
        self.flush_seconds = flush_seconds
        self.last_error = ""
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        # 数据库访问互斥锁：这条 sqlite 连接被管理台的多个请求线程与落盘线程共用，
        # 语句执行必须串行，否则 sqlite3 会抛 SQLITE_MISUSE（bad parameter or other API misuse）。
        # 用可重入锁是因为一次查询会连续取用同一条连接（见 `admin/queries.py`）。
        # 锁序固定为「先 `_db_lock` 再 `_lock`」，任何地方都不允许反向获取。
        self._db_lock = threading.RLock()
        self._stop = threading.Event()
        self._flush_now = threading.Event()
        self._records = []
        self._totals = {}
        self._db = None
        self._timer = None
        self._closed = False
        self._pruned_day = ""
        self._logged_error = ""

    def open(self):
        """建库建表、清理一次过期记录，并启动定时落盘；打不开也照常返回。"""
        with self._db_lock:
            with self._lock:
                self._closed = False
                self._stop.clear()
                self._flush_now.clear()
            try:
                self._connection()
            except (sqlite3.Error, OSError) as error:
                self._fail("打开管理台统计库失败", error)
            self.prune()
            with self._lock:
                self._start_timer()

    def close(self):
        """先落盘再关连接，最后等定时线程退出；重复调用没有副作用。"""
        self._stop.set()
        # 同时唤醒定时线程，免得它还在长周期等待里白白等一个超时。
        self._flush_now.set()
        # 先 join 再取锁关连接：定时线程可能正在锁外的 executemany 里，
        # 直接关同一条连接会让那一批记录丢失，也违反 sqlite3 同连接不得并发使用的要求。
        timer = self._timer
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=JOIN_TIMEOUT)
        # 关连接前先串行化数据库访问；这里不持 `_lock`，`flush()` 自己按「先 `_db_lock` 再 `_lock`」取锁。
        with self._db_lock:
            self.flush()
            with self._lock:
                db, self._db = self._db, None
                self._closed = True
                self._timer = None
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error as error:
                    self._fail("关闭管理台统计库失败", error)

    def append(self, record):
        """一条消息记录只进内存缓冲；缓冲满 BUFFER_LIMIT 条时唤醒定时线程，本线程不写盘。"""
        with self._lock:
            self._records.append((
                record.ts_utc or self._now().astimezone(timezone.utc).isoformat(), record.kind, record.chat_id,
                record.sender_id, record.plugin, int(record.is_group), int(record.allowed),
                record.summary[:SUMMARY_LIMIT], record.msg_key or "",
                None if record.ms is None else int(record.ms)))
            if len(self._records) >= BUFFER_LIMIT:
                self._flush_now.set()
            self._trim()

    def bump(self, scope, scope_key, metric, amount=1):
        """累计计数先按维度累加到内存，落盘时与库里的值相加；本线程不写盘。"""
        with self._lock:
            if not amount:
                return
            key = (scope, scope_key, metric)
            self._totals[key] = self._totals.get(key, 0) + amount
            if len(self._totals) >= BUFFER_LIMIT:
                self._flush_now.set()

    def flush(self):
        """把缓冲里的记录和累计放进同一个事务；失败时数据留在缓冲里等下次重试。

        持 `_lock` 只做「换出缓冲」这几微秒的事，真正的 DB I/O 在锁外做，
        否则 `append`/`bump` 这条埋点热路径会排在一次落盘之后，拖慢消息处理。
        DB I/O 整段由 `_db_lock` 串行化（它不参与热路径，不会挡住 `append`）。
        """
        with self._db_lock:
            with self._lock:
                if self._closed:
                    return
                records, self._records = self._records, []
                totals, self._totals = self._totals, {}
                if not records and not totals:
                    return
            try:
                db = self._connection()
            except (sqlite3.Error, OSError) as error:
                with self._lock:
                    self._restore(records, totals)
                self._fail("管理台统计库落盘失败", error)
                return
            try:
                with db:
                    if records:
                        db.executemany(
                            f"INSERT INTO message_log({RECORD_COLUMNS}) VALUES({RECORD_PLACEHOLDERS})", records)
                    if totals:
                        db.executemany("""INSERT INTO stats_total(scope,scope_key,metric,value) VALUES(?,?,?,?)
                            ON CONFLICT(scope,scope_key,metric) DO UPDATE SET value=value+excluded.value""",
                            [(scope, key, metric, value) for (scope, key, metric), value in totals.items()])
            except (sqlite3.Error, OSError) as error:
                with self._lock:
                    self._restore(records, totals)
                self._fail("管理台统计库落盘失败", error)

    def prune(self):
        """先删超过保留天数的记录，再只留最新的 max_rows 条；返回删除行数。

        两条 DELETE 同样在 `_lock` 外做，别让清理持着埋点热路径要用的锁；
        它们由 `_db_lock` 与查询/落盘串行化。
        """
        with self._db_lock:
            with self._lock:
                if self._closed:
                    return 0
                today = self._now().astimezone(BEIJING).date()
                cutoff = beijing_day_bounds((today - timedelta(days=self.retention_days)).isoformat())[0]
            try:
                db = self._connection()
            except (sqlite3.Error, OSError) as error:
                self._fail("管理台统计库清理失败", error)
                return 0
            removed = 0
            try:
                with db:
                    removed = db.execute("DELETE FROM message_log WHERE ts_utc<?", (cutoff,)).rowcount
                    removed += db.execute("""DELETE FROM message_log WHERE id NOT IN
                        (SELECT id FROM message_log ORDER BY id DESC LIMIT ?)""", (self.max_rows,)).rowcount
            except (sqlite3.Error, OSError) as error:
                self._fail("管理台统计库清理失败", error)
                return 0
            self._pruned_day = today.isoformat()
            return removed

    def _connection(self):
        with self._db_lock:
            if not self._closed and self._db is not None:
                return self._db
            # 惰性建连接要在锁内做：并发首次调用会各建一条，后一条覆盖前一条，前一条就漏了没关。
            with self._lock:
                if self._closed:
                    raise RuntimeError("管理台统计库已关闭")
                if self._db is not None:
                    return self._db
                self.path.parent.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
                db.row_factory = sqlite3.Row
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    db.executescript(SCHEMA)
                    _migrate(db)
                except BaseException:
                    db.close()
                    raise
                self._db = db
                return db

    def _start_timer(self):
        if self._timer is not None or self.flush_seconds <= 0:
            return
        self._timer = threading.Thread(target=self._tick, name="AdminStoreFlush", daemon=True)
        self._timer.start()

    def _tick(self):
        # 落盘只在本线程执行：定时到点，或被“缓冲满”的唤醒信号打断。
        while not self._stop.is_set():
            self._flush_now.wait(self.flush_seconds)
            self._flush_now.clear()
            if self._stop.is_set():
                return
            try:
                self.flush()
                if self._now().astimezone(BEIJING).date().isoformat() != self._pruned_day:
                    self.prune()
            except Exception as error:  # 线程里的意外不能让定时落盘悄悄停掉
                self._fail("管理台统计库定时落盘异常", error)

    def _restore(self, records, totals):
        """落盘失败时把换出去的缓冲塞回去；调用方需持有 `_lock`。"""
        self._records[:0] = records
        self._trim()
        for key, value in totals.items():
            self._totals[key] = self._totals.get(key, 0) + value

    def _trim(self):
        # 库长期不可写或已经关闭时不能无限占内存，只留最近的记录。
        del self._records[:-PENDING_LIMIT]

    def _fail(self, message, error):
        # 同样的错误只写一条日志，避免库一直不可用时刷屏。
        self.last_error = f"{message}：{error}"
        if self.last_error != self._logged_error:
            self._logged_error = self.last_error
            LOGGER.warning("%s", self.last_error)
