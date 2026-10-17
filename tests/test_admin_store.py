"""管理台统计库的缓冲落盘、累计计数与保留清理验证，不启动 HTTP 服务。"""
import contextlib
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wxbot_framework.admin.store import (AdminStore, BUFFER_LIMIT, Record, SUMMARY_LIMIT,
                                         beijing_day_bounds)

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
ROOM = "12345678@chatroom"
SENDER = "wxid_sender"


@contextlib.contextmanager
def connect(path):
    """临时开一条连接；用完必须关掉，否则 Windows 上删不掉库文件。"""
    db = sqlite3.connect(path)
    try:
        yield db
    finally:
        db.close()


def totals(path):
    """另开只读连接读累计表，返回 {(scope, scope_key, metric): value}。"""
    with connect(path) as db:
        return {(scope, key, metric): value for scope, key, metric, value
                in db.execute("SELECT scope,scope_key,metric,value FROM stats_total")}


class _BlockingConnection:
    """executemany 会卡在 release 上，用来证明落盘进行中 append 不被阻塞。"""

    def __init__(self, entered, release):
        self._entered, self._release = entered, release

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, *args):
        self._entered.set()
        self._release.wait(5)


class AdminStoreTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.stores = []
        self.store = self.create_store()

    def create_store(self, name="admin_data.sqlite3", **options):
        store = AdminStore(self.root / name, **options)
        self.stores.append(store)
        store.open()
        return store

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def rows(self, store=None):
        """另开一条连接查消息记录，用来确认数据真的落到了盘上。"""
        with connect((store or self.store).path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM message_log ORDER BY id")]

    def insert(self, timestamp, summary, store=None, kind="receive"):
        """绕开缓冲直接写库，用于构造历史数据。"""
        with connect((store or self.store).path) as db:
            db.execute("INSERT INTO message_log(ts_utc,kind,summary) VALUES(?,?,?)", (timestamp, kind, summary))
            db.commit()

    def test_append_then_flush_persists_row(self):
        self.store.append(Record(kind="receive", chat_id=ROOM, sender_id=SENDER, is_group=True,
                                 summary="你好" * 400))
        self.store.flush()
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "receive")
        self.assertEqual(rows[0]["chat_id"], ROOM)
        self.assertEqual(rows[0]["sender_id"], SENDER)
        self.assertEqual(rows[0]["plugin"], "")
        self.assertEqual(rows[0]["is_group"], 1)
        self.assertEqual(rows[0]["allowed"], 1)
        self.assertEqual(len(rows[0]["summary"]), SUMMARY_LIMIT)
        self.assertTrue(datetime.fromisoformat(rows[0]["ts_utc"]))

    def test_append_without_flush_is_buffered(self):
        # 关掉定时落盘，确认 append 自己不会同步写库。
        store = self.create_store(flush_seconds=3600)
        store.append(Record(kind="receive", summary="尚未落盘"))
        self.assertEqual(self.rows(store), [])

    def test_bump_accumulates_and_survives_reopen(self):
        store = self.create_store(flush_seconds=3600)
        store.bump("group", ROOM, "received", 3)
        store.bump("group", ROOM, "received", 3)
        store.flush()
        store.close()
        reopened = self.create_store()
        self.assertEqual(totals(reopened.path)[("group", ROOM, "received")], 6)

    def test_buffer_limit_is_flushed_without_blocking_caller(self):
        # 周期设得很长，只有“缓冲满”这个信号能触发落盘。
        store = self.create_store(flush_seconds=3600)
        # 占住统计库自己的锁：定时线程被唤醒后也只能等我们放行，这段时间谁都写不进库。
        store._lock.acquire()
        try:
            for index in range(BUFFER_LIMIT):
                store.append(Record(kind="receive", summary=f"第{index}条"))
            # 第 200 条 append 返回后调用线程没有落盘，另开连接查不到这批数据。
            self.assertEqual(self.rows(store), [])
        finally:
            store._lock.release()
        # 放行后由定时线程把它落盘。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(self.rows(store)) < BUFFER_LIMIT:
            time.sleep(0.02)
        self.assertEqual(len(self.rows(store)), BUFFER_LIMIT)

    def test_flush_releases_lock_before_database_io(self):
        # 落盘卡在 executemany 里时，另一线程的 append 必须立刻返回：热路径不被落盘挡住。
        store = self.create_store(flush_seconds=3600)
        entered, release = threading.Event(), threading.Event()
        real = store._db
        store._db = _BlockingConnection(entered, release)
        try:
            store.append(Record(kind="receive", summary="先落盘的"))
            flusher = threading.Thread(target=store.flush, daemon=True)
            flusher.start()
            self.assertTrue(entered.wait(5))
            started = time.monotonic()
            store.append(Record(kind="receive", summary="落盘中追加的"))
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
        finally:
            release.set()
            flusher.join(5)
            store._db = real

    def test_prune_removes_rows_older_than_retention(self):
        now = datetime.now(timezone.utc)
        self.insert((now - timedelta(days=8)).isoformat(), "八天前")
        self.insert((now - timedelta(days=1)).isoformat(), "一天前")
        self.assertEqual(self.store.prune(), 1)
        self.assertEqual([row["summary"] for row in self.rows()], ["一天前"])

    def test_prune_removes_oldest_when_over_max_rows(self):
        store = self.create_store(max_rows=3)
        now = datetime.now(timezone.utc).isoformat()
        for index in range(1, 6):
            self.insert(now, f"第{index}条", store)
        self.assertEqual(store.prune(), 2)
        self.assertEqual([row["summary"] for row in self.rows(store)], ["第3条", "第4条", "第5条"])

    def test_append_never_raises_when_database_is_unwritable(self):
        # 同名位置先放一个文件，库目录就建不出来。
        (self.root / "blocked").write_text("占位文件", encoding="utf-8")
        store = self.create_store(name="blocked/admin_data.sqlite3")
        store.append(Record(kind="receive", summary="写不进去"))
        store.flush()
        self.assertTrue(store.last_error)
        self.assertFalse((self.root / "blocked/admin_data.sqlite3").exists())

    def test_beijing_day_bounds_is_utc_half_open(self):
        self.assertEqual(beijing_day_bounds("2026-10-16"),
                         ("2026-10-15T16:00:00+00:00", "2026-10-16T16:00:00+00:00"))
