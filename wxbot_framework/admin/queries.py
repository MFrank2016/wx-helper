"""管理台只读查询：概览统计、按群/趋势/发言人聚合与日志过滤。

只读取 `AdminStore` 已落盘的数据，不写表、不加锁（SQLite 允许并发读）。
`day` 一律由调用方传入（北京日期 `YYYY-MM-DD`），便于测试与页面按日查看。
时间范围用 `beijing_day_bounds` 换算；`ts_utc` 是规范化 ISO UTC 字符串，范围比较直接用字符串比较。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .store import beijing_day_bounds

# 今日「处理」「异常」在消息记录表里对应的类型。
HANDLED_KIND = "plugin"
ERROR_KIND = "error"
# LIKE 关键字里需要转义的通配符。
LIKE_ESCAPE = "\\"


def _like(pattern):
    """把关键字转成 LIKE 模式：转义 % 和 _，两侧补通配符。"""
    escaped = pattern.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
    escaped = escaped.replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return f"%{escaped}%"


def _top_plugin(counts):
    """取今日调用次数最多的插件名；次数相同时按插件名取定序，保证结果稳定。"""
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


class AdminQueries:
    """把统计库里的原始记录聚合给 API 层；全部只读，不产生任何写入。"""

    def __init__(self, store, *, name_resolver=None):
        self._store = store
        self._name_resolver = name_resolver

    def _db(self):
        # 复用统计库自己的连接（只读查询，不加统计库的锁）。
        return self._store._connection()

    def summary(self, day):
        """概览：今日从消息记录表聚合，累计从 `stats_total` 读取。"""
        start, end = beijing_day_bounds(day)
        row = self._db().execute("""SELECT
                COALESCE(SUM(CASE WHEN kind='receive' AND allowed=1 AND is_group=1 THEN 1 ELSE 0 END),0) AS received_group,
                COALESCE(SUM(CASE WHEN kind='receive' AND allowed=1 AND is_group=0 THEN 1 ELSE 0 END),0) AS received_direct,
                COALESCE(SUM(CASE WHEN kind='send' AND allowed=1 THEN 1 ELSE 0 END),0) AS sent,
                COALESCE(SUM(CASE WHEN kind=? THEN 1 ELSE 0 END),0) AS handled,
                COALESCE(SUM(CASE WHEN kind=? THEN 1 ELSE 0 END),0) AS errors
            FROM message_log WHERE ts_utc>=? AND ts_utc<?""",
            (HANDLED_KIND, ERROR_KIND, start, end)).fetchone()
        today = {
            "received": row["received_group"] + row["received_direct"],
            "sent": row["sent"],
            "handled": row["handled"],
            "errors": row["errors"],
            "received_group": row["received_group"],
            "received_direct": row["received_direct"],
        }
        return {"today": today, "total": self._totals()}

    def _totals(self):
        """累计：收到/发出读全局维度，处理/异常取插件维度之和，群/私聊由群维度与全局相减拆出。"""
        db = self._db()
        received = self._global(db, "received")
        received_group = self._scoped_sum(db, "group", "received")
        return {
            "received": received,
            "sent": self._global(db, "sent"),
            "handled": self._scoped_sum(db, "plugin", "handled"),
            "errors": self._scoped_sum(db, "plugin", "errors"),
            "received_group": received_group,
            "received_direct": max(received - received_group, 0),
        }

    def _global(self, db, metric):
        row = db.execute("SELECT value FROM stats_total WHERE scope='global' AND scope_key='' AND metric=?",
                         (metric,)).fetchone()
        return row["value"] if row else 0

    def _scoped_sum(self, db, scope, metric):
        row = db.execute("SELECT COALESCE(SUM(value),0) AS value FROM stats_total WHERE scope=? AND metric=?",
                         (scope, metric)).fetchone()
        return row["value"]

    def groups(self, day, *, limit=50):
        """各群今日收到/发出、累计收到、今日最活跃插件与最近一条记录时间。"""
        start, end = beijing_day_bounds(day)
        db = self._db()
        received = self._today_by_chat(db, "receive", start, end)
        sent = self._today_by_chat(db, "send", start, end)
        plugins = self._today_plugins(db, start, end)
        last = self._last_ts(db)
        total_received = {row["scope_key"]: row["value"] for row in db.execute(
            "SELECT scope_key, value FROM stats_total WHERE scope='group' AND metric='received'")}
        rows = []
        for chat_id in set(received) | set(sent) | set(plugins) | set(total_received):
            if not chat_id:
                continue
            rows.append({
                "chat_id": chat_id,
                "today_received": received.get(chat_id, 0),
                "today_sent": sent.get(chat_id, 0),
                "total_received": total_received.get(chat_id, 0),
                "top_plugin": _top_plugin(plugins.get(chat_id, {})),
                "last_ts": last.get(chat_id, ""),
            })
        rows.sort(key=lambda item: (item["today_received"], item["today_sent"], item["total_received"]), reverse=True)
        return rows[:limit]

    def _today_by_chat(self, db, kind, start, end):
        rows = db.execute("""SELECT chat_id, COUNT(*) AS count FROM message_log
            WHERE kind=? AND allowed=1 AND is_group=1 AND ts_utc>=? AND ts_utc<?
            GROUP BY chat_id""", (kind, start, end)).fetchall()
        return {row["chat_id"]: row["count"] for row in rows}

    def _today_plugins(self, db, start, end):
        rows = db.execute("""SELECT chat_id, plugin, COUNT(*) AS count FROM message_log
            WHERE kind=? AND plugin<>'' AND is_group=1 AND ts_utc>=? AND ts_utc<?
            GROUP BY chat_id, plugin""", (HANDLED_KIND, start, end)).fetchall()
        result = {}
        for row in rows:
            result.setdefault(row["chat_id"], {})[row["plugin"]] = row["count"]
        return result

    def _last_ts(self, db):
        # 「最近一条」取消息记录表里该群最新的一条时间，不受查询日期限制。
        rows = db.execute("""SELECT chat_id, MAX(ts_utc) AS ts FROM message_log
            WHERE is_group=1 AND chat_id<>'' GROUP BY chat_id""").fetchall()
        return {row["chat_id"]: row["ts"] for row in rows}

    def trend(self, *, days=7, day):
        """截至 day（含）往前 days 天，按北京日递增返回收到/发出数。"""
        base = datetime.strptime(day, "%Y-%m-%d")
        db = self._db()
        result = []
        for offset in range(days - 1, -1, -1):
            current = (base - timedelta(days=offset)).date().isoformat()
            start, end = beijing_day_bounds(current)
            rows = db.execute("""SELECT kind, COUNT(*) AS count FROM message_log
                WHERE allowed=1 AND kind IN ('receive','send') AND ts_utc>=? AND ts_utc<?
                GROUP BY kind""", (start, end)).fetchall()
            counts = {row["kind"]: row["count"] for row in rows}
            result.append({"day": current, "received": counts.get("receive", 0), "sent": counts.get("send", 0)})
        return result

    def talkers(self, day, *, chat_id=None, limit=10):
        """今日发言最多的发送者；名字交由 name_resolver 解析，解析不到就回落到 ID。"""
        start, end = beijing_day_bounds(day)
        sql = """SELECT sender_id, COUNT(*) AS count FROM message_log
            WHERE kind='receive' AND allowed=1 AND ts_utc>=? AND ts_utc<?"""
        values = [start, end]
        if chat_id:
            sql += " AND chat_id=?"
            values.append(chat_id)
        sql += " GROUP BY sender_id ORDER BY count DESC, sender_id LIMIT ?"
        values.append(limit)
        rows = self._db().execute(sql, values).fetchall()
        return [{"sender_id": row["sender_id"], "count": row["count"],
                 "name": self._resolve(row["sender_id"], chat_id)} for row in rows]

    def _resolve(self, sender_id, chat_id):
        if self._name_resolver is not None:
            name = self._name_resolver(sender_id, chat_id or "")
            if name:
                return name
        return sender_id

    def logs(self, *, kinds=None, keyword="", chat_id="", plugin="", since="", until="", limit=100, offset=0):
        """按类型/关键字/群/插件/时间过滤消息记录，返回命中总数与当前分页。"""
        conditions, values = [], []
        if kinds:
            conditions.append(f"kind IN ({','.join('?' * len(kinds))})")
            values.extend(kinds)
        if keyword:
            conditions.append("(summary LIKE ? ESCAPE '\\' OR chat_id LIKE ? ESCAPE '\\')")
            pattern = _like(keyword)
            values.extend([pattern, pattern])
        if chat_id:
            conditions.append("chat_id=?")
            values.append(chat_id)
        if plugin:
            conditions.append("plugin=?")
            values.append(plugin)
        if since:
            conditions.append("ts_utc>=?")
            values.append(since)
        if until:
            conditions.append("ts_utc<?")
            values.append(until)
        clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        db = self._db()
        total = db.execute(f"SELECT COUNT(*) AS count FROM message_log{clause}", values).fetchone()["count"]
        rows = db.execute(f"SELECT * FROM message_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                          [*values, limit, offset]).fetchall()
        return {"rows": [dict(row) for row in rows], "total": total}

    def plugin_today(self, day):
        """今日各插件的处理数与异常数，供插件表与实时状态合并。"""
        start, end = beijing_day_bounds(day)
        rows = self._db().execute("""SELECT plugin, kind, COUNT(*) AS count FROM message_log
            WHERE plugin<>'' AND kind IN (?,?) AND ts_utc>=? AND ts_utc<? GROUP BY plugin, kind""",
            (HANDLED_KIND, ERROR_KIND, start, end)).fetchall()
        result = {}
        for row in rows:
            item = result.setdefault(row["plugin"], {"handled": 0, "errors": 0})
            item["handled" if row["kind"] == HANDLED_KIND else "errors"] += row["count"]
        return result
