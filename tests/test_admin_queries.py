"""管理台只读查询（概览统计、按群/趋势/发言人聚合、日志过滤）验证，不启动 HTTP 服务。"""
import contextlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wxbot_framework.admin.queries import AdminQueries
from wxbot_framework.admin.store import BEIJING, AdminStore

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"
GROUP = "12345678@chatroom"
DIRECT = "wxid_direct"
SENDER_A = "wxid_a"
SENDER_B = "wxid_b"


def day_at(day, hour=12):
    """北京日期 day 的 hour 点，转成 UTC ISO 字符串，用于构造落在日界内的记录。"""
    date = datetime.strptime(day, "%Y-%m-%d")
    return datetime(date.year, date.month, date.day, hour, tzinfo=BEIJING).astimezone(timezone.utc).isoformat()


def beijing_date(days_ago=0):
    """相对今天往前数 days_ago 天的北京日期字符串。"""
    return (datetime.now(BEIJING).date() - timedelta(days=days_ago)).isoformat()


class AdminQueriesTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.store = AdminStore(self.root / "admin_data.sqlite3")
        self.store.open()
        self.queries = AdminQueries(self.store)
        self.today = beijing_date()
        self.yesterday = beijing_date(1)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    @contextlib.contextmanager
    def _connect(self):
        """另开一条连接直接写库，构造历史数据；用完必须关掉，否则 Windows 上删不掉库文件。"""
        db = sqlite3.connect(self.store.path)
        try:
            yield db
        finally:
            db.close()

    def insert(self, kind, *, day=None, hour=12, chat_id="", sender_id="", plugin="", is_group=0,
               allowed=1, summary=""):
        with self._connect() as db:
            db.execute("""INSERT INTO message_log(ts_utc,kind,chat_id,sender_id,plugin,is_group,allowed,summary)
                VALUES(?,?,?,?,?,?,?,?)""",
                       (day_at(day or self.today, hour), kind, chat_id, sender_id, plugin, is_group, allowed, summary))
            db.commit()

    def bump(self, scope, scope_key, metric, amount=1):
        self.store.bump(scope, scope_key, metric, amount)
        self.store.flush()

    def test_summary_counts_today_and_total_separately(self):
        for _ in range(2):
            self.insert("receive", day=self.yesterday, chat_id=GROUP, is_group=1)
        for _ in range(3):
            self.insert("receive", chat_id=GROUP, is_group=1)
        self.bump("global", "", "received", 5)
        result = self.queries.summary(self.today)
        self.assertEqual(result["today"]["received"], 3)
        self.assertEqual(result["total"]["received"], 5)

    def test_summary_splits_group_and_direct(self):
        for _ in range(2):
            self.insert("receive", chat_id=GROUP, sender_id=SENDER_A, is_group=1)
        self.insert("receive", chat_id=DIRECT, sender_id=SENDER_B)
        self.bump("global", "", "received", 3)
        self.bump("group", GROUP, "received", 2)
        result = self.queries.summary(self.today)
        self.assertEqual(result["today"]["received_group"], 2)
        self.assertEqual(result["today"]["received_direct"], 1)
        # 累计的群/私聊拆分：群维度之和即群聊，全局减去群聊即私聊。
        self.assertEqual(result["total"]["received_group"], 2)
        self.assertEqual(result["total"]["received_direct"], 1)

    def test_summary_ignores_denied_messages(self):
        self.insert("receive", chat_id=GROUP, sender_id=SENDER_A, is_group=1, allowed=0)
        self.insert("receive", chat_id=DIRECT, sender_id=SENDER_B, allowed=0)
        self.insert("send", chat_id=GROUP, is_group=1, allowed=0)
        today = self.queries.summary(self.today)["today"]
        self.assertEqual(today["received"], 0)
        self.assertEqual(today["received_group"], 0)
        self.assertEqual(today["received_direct"], 0)
        self.assertEqual(today["sent"], 0)

    def test_groups_lists_top_plugin_of_today(self):
        for _ in range(3):
            self.insert("plugin", chat_id=GROUP, plugin="idiom_chain", is_group=1)
        self.insert("plugin", chat_id=GROUP, plugin="echo", is_group=1)
        self.insert("receive", chat_id=GROUP, sender_id=SENDER_A, is_group=1)
        self.bump("group", GROUP, "received", 7)
        rows = self.queries.groups(self.today)
        self.assertEqual(rows[0]["chat_id"], GROUP)
        self.assertEqual(rows[0]["today_received"], 1)
        self.assertEqual(rows[0]["today_sent"], 0)
        self.assertEqual(rows[0]["total_received"], 7)
        self.assertEqual(rows[0]["top_plugin"], "idiom_chain")
        self.assertTrue(rows[0]["last_ts"])

    def test_trend_returns_seven_days_in_order(self):
        for index in range(3):
            self.insert("receive", day=beijing_date(index), chat_id=GROUP, is_group=1)
            self.insert("send", day=beijing_date(index), chat_id=GROUP, is_group=1)
        self.insert("receive", day=beijing_date(9), chat_id=GROUP, is_group=1)  # 落在 7 天窗口之外
        rows = self.queries.trend(days=7, day=self.today)
        self.assertEqual(len(rows), 7)
        self.assertEqual([row["day"] for row in rows], [beijing_date(offset) for offset in range(6, -1, -1)])
        self.assertEqual(rows[-1], {"day": self.today, "received": 1, "sent": 1})
        self.assertEqual(rows[-2], {"day": self.yesterday, "received": 1, "sent": 1})
        self.assertEqual(rows[0]["received"], 0)

    def test_talkers_uses_name_resolver(self):
        self.insert("receive", chat_id=GROUP, sender_id=SENDER_A, is_group=1)
        self.insert("receive", chat_id=GROUP, sender_id=SENDER_A, is_group=1)
        self.insert("receive", chat_id=GROUP, sender_id=SENDER_B, is_group=1)
        queries = AdminQueries(self.store,
                               name_resolver=lambda wxid, chat_id: "阿花" if wxid == SENDER_A else None)
        rows = queries.talkers(self.today, chat_id=GROUP)
        self.assertEqual(rows[0]["sender_id"], SENDER_A)
        self.assertEqual(rows[0]["name"], "阿花")
        self.assertEqual(rows[0]["count"], 2)
        # 解析器返回 None 时回落到发送者 ID。
        self.assertEqual(rows[1]["sender_id"], SENDER_B)
        self.assertEqual(rows[1]["name"], SENDER_B)
        # 没有解析器时一律用发送者 ID。
        self.assertEqual(self.queries.talkers(self.today, chat_id=GROUP)[0]["name"], SENDER_A)

    def test_logs_filter_by_kind_keyword_and_paging(self):
        self.insert("error", chat_id=GROUP, plugin="echo", is_group=1, summary="插件抛异常了")
        self.insert("receive", chat_id=GROUP, sender_id=SENDER_A, is_group=1, summary="普通消息")
        self.insert("system", chat_id=GROUP, is_group=1, summary="另一个关键字")
        errors = self.queries.logs(kinds=["error"])
        self.assertEqual([row["kind"] for row in errors["rows"]], ["error"])
        self.assertEqual(errors["total"], 1)
        matched = self.queries.logs(keyword="关键字")
        self.assertEqual(matched["total"], 1)
        self.assertEqual(matched["rows"][0]["summary"], "另一个关键字")
        paged = self.queries.logs(limit=1)
        self.assertEqual(len(paged["rows"]), 1)
        self.assertEqual(paged["total"], 3)

    def test_logs_filter_by_plugin_and_chat(self):
        self.insert("plugin", chat_id=GROUP, plugin="idiom_chain", is_group=1, summary="接龙")
        self.insert("plugin", chat_id=DIRECT, plugin="echo", summary="回声")
        by_plugin = self.queries.logs(plugin="idiom_chain")
        self.assertEqual(by_plugin["total"], 1)
        self.assertEqual(by_plugin["rows"][0]["chat_id"], GROUP)
        by_chat = self.queries.logs(chat_id=DIRECT)
        self.assertEqual(by_chat["total"], 1)
        self.assertEqual(by_chat["rows"][0]["plugin"], "echo")

    def test_summary_and_plugin_today_count_handled_and_errors(self):
        self.insert("plugin", chat_id=GROUP, plugin="echo", is_group=1)
        self.insert("plugin", chat_id=GROUP, plugin="echo", is_group=1)
        self.insert("plugin", chat_id=DIRECT, plugin="idiom_chain")
        self.insert("error", chat_id=GROUP, plugin="echo", is_group=1)
        self.insert("error", chat_id=DIRECT, plugin="idiom_chain")
        self.bump("plugin", "echo", "handled", 2)
        self.bump("plugin", "idiom_chain", "handled", 1)
        self.bump("plugin", "echo", "errors", 1)
        self.bump("plugin", "idiom_chain", "errors", 1)
        summary = self.queries.summary(self.today)
        self.assertEqual(summary["today"]["handled"], 3)
        self.assertEqual(summary["today"]["errors"], 2)
        self.assertEqual(summary["total"]["handled"], 3)
        self.assertEqual(summary["total"]["errors"], 2)
        self.assertEqual(self.queries.plugin_today(self.today),
                         {"echo": {"handled": 2, "errors": 1}, "idiom_chain": {"handled": 1, "errors": 1}})
