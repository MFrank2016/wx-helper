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
  allowed INTEGER NOT NULL DEFAULT 1, summary TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS log_by_kind ON message_log(kind, id);
CREATE INDEX IF NOT EXISTS log_by_chat ON message_log(chat_id, id);
CREATE INDEX IF NOT EXISTS log_by_plugin ON message_log(plugin, id);
CREATE TABLE IF NOT EXISTS stats_total(
  scope TEXT NOT NULL, scope_key TEXT NOT NULL, metric TEXT NOT NULL,
  value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(scope, scope_key, metric));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

RECORD_COLUMNS = "ts_utc,kind,chat_id,sender_id,plugin,is_group,allowed,summary"
RECORD_PLACEHOLDERS = "?,?,?,?,?,?,?,?"


def beijing_day_bounds(day):
    """把北京日期（YYYY-MM-DD）换算成该日 [开始, 结束) 的 UTC ISO 字符串。"""
    date = datetime.strptime(day, "%Y-%m-%d")
    start = datetime(date.year, date.month, date.day, tzinfo=BEIJING)
    return (start.astimezone(timezone.utc).isoformat(),
            (start + timedelta(days=1)).astimezone(timezone.utc).isoformat())


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
    # 插件异常时随记录带回来的格式化堆栈（截断），只供管理台调试页展示；
    # `AdminStore.append` 不落这一列，库表结构不变。
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
        with self._lock:
            self._closed = False
            self._stop.clear()
            self._flush_now.clear()
            try:
                self._connection()
            except (sqlite3.Error, OSError) as error:
                self._fail("打开管理台统计库失败", error)
            self.prune()
            self._start_timer()

    def close(self):
        """先落盘再关连接，最后等定时线程退出；重复调用没有副作用。"""
        self._stop.set()
        # 同时唤醒定时线程，免得它还在长周期等待里白白等一个超时。
        self._flush_now.set()
        with self._lock:
            self.flush()
            db, self._db = self._db, None
            if db is not None:
                try:
                    db.close()
                except sqlite3.Error as error:
                    self._fail("关闭管理台统计库失败", error)
            self._closed = True
            timer, self._timer = self._timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=JOIN_TIMEOUT)

    def append(self, record):
        """一条消息记录只进内存缓冲；缓冲满 BUFFER_LIMIT 条时唤醒定时线程，本线程不写盘。"""
        with self._lock:
            self._records.append((
                record.ts_utc or self._now().astimezone(timezone.utc).isoformat(), record.kind, record.chat_id,
                record.sender_id, record.plugin, int(record.is_group), int(record.allowed),
                record.summary[:SUMMARY_LIMIT]))
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
        """把缓冲里的记录和累计放进同一个事务；失败时数据留在缓冲里等下次重试。"""
        with self._lock:
            if self._closed:
                return
            records, self._records = self._records, []
            totals, self._totals = self._totals, {}
            if not records and not totals:
                return
            try:
                db = self._connection()
                with db:
                    if records:
                        db.executemany(
                            f"INSERT INTO message_log({RECORD_COLUMNS}) VALUES({RECORD_PLACEHOLDERS})", records)
                    if totals:
                        db.executemany("""INSERT INTO stats_total(scope,scope_key,metric,value) VALUES(?,?,?,?)
                            ON CONFLICT(scope,scope_key,metric) DO UPDATE SET value=value+excluded.value""",
                            [(scope, key, metric, value) for (scope, key, metric), value in totals.items()])
            except (sqlite3.Error, OSError) as error:
                self._records[:0] = records
                self._trim()
                for key, value in totals.items():
                    self._totals[key] = self._totals.get(key, 0) + value
                self._fail("管理台统计库落盘失败", error)

    def prune(self):
        """先删超过保留天数的记录，再只留最新的 max_rows 条；返回删除行数。"""
        with self._lock:
            if self._closed:
                return 0
            today = self._now().astimezone(BEIJING).date()
            cutoff = beijing_day_bounds((today - timedelta(days=self.retention_days)).isoformat())[0]
            removed = 0
            try:
                db = self._connection()
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

    def _trim(self):
        # 库长期不可写或已经关闭时不能无限占内存，只留最近的记录。
        del self._records[:-PENDING_LIMIT]

    def _fail(self, message, error):
        # 同样的错误只写一条日志，避免库一直不可用时刷屏。
        self.last_error = f"{message}：{error}"
        if self.last_error != self._logged_error:
            self._logged_error = self.last_error
            LOGGER.warning("%s", self.last_error)
