"""管理台只读查询：概览统计、按群/趋势/发言人聚合与日志过滤。

只读取 `AdminStore` 已落盘的数据，不写表。查询与落盘/清理共用 `AdminStore` 的同一条 sqlite
连接，故每个查询都在 `AdminStore._db_lock` 内执行（可重入锁，一次查询里多次取连接不会自锁）。
`day` 一律由调用方传入（北京日期 `YYYY-MM-DD`），便于测试与页面按日查看。
时间范围用 `beijing_day_bounds` 换算；`ts_utc` 是规范化 ISO UTC 字符串，范围比较直接用字符串比较。

日志有两个口径：`logs()` 是扁平的「一条记录一行」（既有行为不变），`messages()` 按消息关联标识
归集成「一条消息一行 + 该消息下的明细列表」，供日志页的默认视图用。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .store import beijing_day_bounds

# 今日「处理」「异常」在消息记录表里对应的类型。
HANDLED_KIND = "plugin"
ERROR_KIND = "error"
# LIKE 关键字里需要转义的通配符。
LIKE_ESCAPE = "\\"
# 归集视图里的分组：这两类算「插件处理条目」（`plugin_count` / `has_error` 只看它们），
# `ms` 也只有 `plugin` 行有值。
DETAIL_KINDS = ("plugin", "error")
# 这两类算「这条消息的回复」：`replied` / `reply` 由它们得出，预览也是回复。
SEND_KINDS = ("send", "preview")
# 一条消息展开的明细上限：旧库里所有无关联的历史行会挤在同一条「无关联」条目里，
# 不设上限一次可能返回上万条明细把响应撑爆；超出的行在 `logs()` 的扁平视图里照样查得到。
DETAIL_LIMIT = 200


def _like(pattern):
    """把关键字转成 LIKE 模式：转义 % 和 _，两侧补通配符。"""
    escaped = pattern.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
    escaped = escaped.replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return f"%{escaped}%"


def _quoted(values):
    """把本模块固定的记录类型拼成 SQL 的 IN 列表；只接模块内常量，不接外部输入。"""
    return ",".join(f"'{value}'" for value in values)


def _top_plugin(counts):
    """取今日调用次数最多的插件名；次数相同时按插件名取定序，保证结果稳定。"""
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _elapsed_ms(receive_ts, reply_ts):
    """回复耗时：该消息收到的时间 → 该消息下最后一条回复（send/preview）的时间，整数毫秒。

    只有同时存在收到行与回复行时才成立；缺任一端，或时间戳是旧库里的空值/畸形文本时返回 `None`，
    不拿别的字段凑数。这与明细里的 `ms`（单个插件的处理耗时）是两个口径，故并存而不替换。
    """
    if not receive_ts or not reply_ts:
        return None
    try:
        start = datetime.fromisoformat(receive_ts)
        end = datetime.fromisoformat(reply_ts)
    except (TypeError, ValueError):
        return None
    return int((end - start).total_seconds() * 1000)


class AdminQueries:
    """把统计库里的原始记录聚合给 API 层；全部只读，不产生任何写入。"""

    def __init__(self, store, name_resolver=None, chat_name_resolver=None, member_store=None):
        self._store = store
        self._name_resolver = name_resolver
        # 群名由框架侧在启动时刷新一次并持有快照，这里只按调用取用、不缓存也不落盘，
        # 所以机器人换群后管理台不会拿着过期名字；没注入（旧调用方式）时一律返回空串。
        self._chat_name_resolver = chat_name_resolver
        # 成员库（`MemberStore`）与统计库是两个库：群成员名单只存在成员库里。
        # 未注入时（旧调用方式或未启用成员目录）潜水名单接口报 400，其余查询不受影响。
        self._members = member_store

    def _db(self):
        # 复用统计库自己的连接；调用方必须持有 `AdminStore._db_lock`，
        # 与落盘 flush / prune 对同一条连接的语句执行串行。
        return self._store._connection()

    def summary(self, day):
        """概览：今日从消息记录表聚合，累计从 `stats_total` 读取。"""
        start, end = beijing_day_bounds(day)
        with self._store._db_lock:
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
        """各群今日收到/发出、累计收到、今日最活跃插件与最近一条记录时间，并带上群名。"""
        start, end = beijing_day_bounds(day)
        with self._store._db_lock:
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
                    # 群名同样只按调用取用；拿不到为空串，由页面回落到显示 chat_id。
                    "chat_name": self._chat_name_of(chat_id),
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
        result = []
        with self._store._db_lock:
            db = self._db()
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
        """今日发言最多的发送者；每人取「今日发言最多的群」作代表群，名字在该群里解析。

        先按「人」汇总排序、截取前 limit 个发送者，再单独取每人的代表群——分组不落到
        「(群, 人)」维度上，否则同一个人跨群发言会重复占位、把别人挤出榜单。代表群只决定
        名字从哪个群解析与 `chat_name`；`count` 仍是该发送者跨群的今日收到消息合计。
        """
        start, end = beijing_day_bounds(day)
        sql = """SELECT sender_id, COUNT(*) AS count FROM message_log
            WHERE kind='receive' AND allowed=1 AND ts_utc>=? AND ts_utc<?"""
        values = [start, end]
        if chat_id:
            sql += " AND chat_id=?"
            values.append(chat_id)
        sql += " GROUP BY sender_id ORDER BY count DESC, sender_id LIMIT ?"
        values.append(limit)
        with self._store._db_lock:
            db = self._db()
            rows = db.execute(sql, values).fetchall()
            rooms = self._talker_rooms(db, [row["sender_id"] for row in rows], start, end, chat_id)
        # 名字解析走成员库、另用一把锁，放在 `_db_lock` 外做，避免无谓地拖长数据库互斥。
        result = []
        for row in rows:
            room = rooms.get(row["sender_id"], "")
            result.append({"sender_id": row["sender_id"], "count": row["count"],
                           "name": self._resolve(row["sender_id"], room),
                           "chat_id": room,
                           "chat_name": self._chat_name_of(room)})
        return result

    def _talker_rooms(self, db, sender_ids, start, end, chat_id):
        """每个发送者的代表群：今日在该群收到最多；并列时取 chat_id 字典序最小的那个。

        `count` 降序 + `chat_id` 升序后，每个发送者取到的第一行就是代表群，用 `setdefault`
        保住第一行；传了 `chat_id` 时全部记录同一个群，代表群自然是它。
        """
        if not sender_ids:
            return {}
        placeholders = ",".join("?" * len(sender_ids))
        sql = f"""SELECT sender_id, chat_id, COUNT(*) AS count FROM message_log
            WHERE kind='receive' AND allowed=1 AND ts_utc>=? AND ts_utc<? AND sender_id IN ({placeholders})"""
        values = [start, end, *sender_ids]
        if chat_id:
            sql += " AND chat_id=?"
            values.append(chat_id)
        sql += " GROUP BY sender_id, chat_id ORDER BY sender_id, count DESC, chat_id ASC"
        rooms = {}
        for row in db.execute(sql, values).fetchall():
            rooms.setdefault(row["sender_id"], row["chat_id"])
        return rooms

    def _chat_name_of(self, chat_id):
        """会话显示名：拿到才返回，拿不到（没注入 resolver / 不是群 / 查不到）返回空串。

        不用 chat_id 冒充群名——页面侧已有「没有名字就显示 id」的回落逻辑；
        空会话（系统记录）本就没有归属，直接空串，连 resolver 都不必问。
        """
        if not chat_id or self._chat_name_resolver is None:
            return ""
        name = self._chat_name_resolver(chat_id)
        return name if isinstance(name, str) and name else ""

    def lurkers(self, chat_id=None):
        """该群「当前在群、从没发过言」的成员名单（潜水名单）。

        两个数据源都在成员库：`room_roster` 给当前在群人数与名单时间，
        `lurker_members` 排除机器人自己与历史上发过言的人（含已退群者不出现）。
        `chat_id` 缺省取已配置群里字典序最小的那个；传入未配置的群抛 `ValueError`
        （沿用 handle 的 400 映射）。`name` 可能为空串，原样返回由页面决定展示。
        """
        members = self._members
        if members is None:
            raise ValueError("成员库未启用，无法查询潜水名单")
        if not chat_id:
            groups = sorted(members.groups)
            if not groups:
                raise ValueError("没有配置群，无法查询潜水名单")
            chat_id = groups[0]
        elif chat_id not in members.groups:
            raise ValueError(f"未配置的群：{chat_id}")
        roster = members.room_roster(chat_id)
        rows = members.lurker_members(chat_id)
        # first_seen 升序、相同再按 wxid 升序；first_seen 缺失（旧库）当空串参与排序。
        rows.sort(key=lambda row: (row.get("first_seen") or "", row["wxid"]))
        return {
            "chat_id": chat_id,
            # 名单时间取该群当前成员里最新的 last_seen；一条名单都没有时为 null。
            "as_of": max((row["last_seen"] for row in roster if row.get("last_seen")), default=None),
            "roster_total": len(roster),
            "lurker_count": len(rows),
            "lurkers": [{"wxid": row["wxid"], "name": row["name"],
                         "first_seen": row["first_seen"], "last_seen": row["last_seen"]}
                        for row in rows],
        }

    def _resolve(self, sender_id, chat_id):
        if self._name_resolver is not None:
            name = self._name_resolver(sender_id, chat_id or "")
            if isinstance(name, str) and name:
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
        with self._store._db_lock:
            db = self._db()
            total = db.execute(f"SELECT COUNT(*) AS count FROM message_log{clause}", values).fetchone()["count"]
            rows = db.execute(f"SELECT * FROM message_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                              [*values, limit, offset]).fetchall()
            return {"rows": [dict(row) for row in rows], "total": total}

    def messages(self, *, kinds=None, keyword="", chat_id="", plugin="", since="", until="",
                 include_direct=False, include_system=False, limit=100, offset=0):
        """按消息标识归集：每条消息一行 + 该消息下的明细列表，供日志页默认视图用。

        与 `logs()` 的区别是口径按「一条消息」而不是「一条记录」：

        - 分组键是 `msg_key`（`runtime.new_message_key` 生成）。空串的行（旧库历史行、
          调试预览、定时任务里的发送等没有消息上下文的记录）合成一条「无关联」条目，
          明细照常列出，历史行不会因为拿不到标识就丢掉；
        - 行级过滤（群/私聊、会话、时间窗）先筛记录；组级过滤（插件、类型、关键字）只要
          组内有一条命中就保留整组——否则「按插件/关键字找到的消息」会被它自己的 receive 行筛掉；
        - 分页按消息条数计：`total` 是命中的消息条数，`offset` 也是按消息数走的。

        可见性按三类记录拆成两个开关（缺省都不开时与从前一致，只看群消息）：

        - 群消息（`is_group=1`）恒可见；
        - `include_direct` 只放真正的私聊消息（`is_group=0` 且 `chat_id` 非空）；
        - `include_system` 才放框架自身的系统记录（`chat_id` 为空，它们没有会话，会落进
          「无关联」条目）。
        """
        conditions, values = [], []
        # 两类记录语义不同（私聊 vs 无会话的系统记录），所以拆成两个独立开关而不是共用一个：
        # 私聊由 is_group/chat_id 判定，系统记录只能靠 chat_id 为空识别（其 is_group 取默认 0）。
        visible = ["is_group=1"]
        if include_direct:
            visible.append("(is_group=0 AND chat_id<>'')")
        if include_system:
            visible.append("chat_id=''")
        conditions.append(f"({' OR '.join(visible)})")
        if chat_id:
            conditions.append("chat_id=?")
            values.append(chat_id)
        if since:
            conditions.append("ts_utc>=?")
            values.append(since)
        if until:
            conditions.append("ts_utc<?")
            values.append(until)
        groups, group_values = [], []
        if plugin:
            groups.append("SUM(CASE WHEN plugin=? THEN 1 ELSE 0 END)>0")
            group_values.append(plugin)
        if kinds:
            groups.append(f"SUM(CASE WHEN kind IN ({','.join('?' * len(kinds))}) THEN 1 ELSE 0 END)>0")
            group_values.extend(kinds)
        if keyword:
            groups.append("SUM(CASE WHEN summary LIKE ? ESCAPE '\\' OR chat_id LIKE ? ESCAPE '\\' "
                          "THEN 1 ELSE 0 END)>0")
            pattern = _like(keyword)
            group_values.extend([pattern, pattern])
        clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        having = f" HAVING {' AND '.join(groups)}" if groups else ""
        with self._store._db_lock:
            db = self._db()
            total = db.execute(f"SELECT COUNT(*) AS count FROM "
                               f"(SELECT 1 FROM message_log{clause} GROUP BY msg_key{having})",
                               [*values, *group_values]).fetchone()["count"]
            # 消息层字段优先取该组的 receive 行；没有 receive 行的组（机器人自己的回显消息）
            # 退回组内记录的群与时间，`text` 留空。排序用组内最大 id，与 `logs()` 的时间倒序一致。
            # 「无关联」条目（`msg_key=''`）强制 `msg_id=0`：这样它组内每一行都留在明细里，
            # 历史行不会因为被当成锚点而看不到。
            rows = db.execute(f"""SELECT msg_key, MAX(id) AS sort_id,
                CASE WHEN msg_key='' THEN 0
                     ELSE COALESCE(MIN(CASE WHEN kind='receive' THEN id END),0) END AS msg_id,
                COALESCE(MIN(CASE WHEN kind='receive' THEN ts_utc END),MIN(ts_utc)) AS ts_utc,
                COALESCE(MIN(CASE WHEN kind='receive' THEN chat_id END),MAX(chat_id)) AS chat_id,
                COALESCE(MIN(CASE WHEN kind='receive' THEN sender_id END),'') AS sender_id,
                MAX(is_group) AS is_group,
                COALESCE(MIN(CASE WHEN kind='receive' THEN summary END),'') AS text,
                MIN(CASE WHEN kind='receive' THEN ts_utc END) AS receive_ts,
                MAX(CASE WHEN kind IN ({_quoted(SEND_KINDS)}) THEN ts_utc END) AS reply_ts,
                SUM(CASE WHEN kind IN ({_quoted(DETAIL_KINDS)}) THEN 1 ELSE 0 END) AS plugin_count,
                SUM(CASE WHEN kind='{ERROR_KIND}' THEN 1 ELSE 0 END) AS error_count,
                SUM(CASE WHEN kind IN ({_quoted(SEND_KINDS)}) THEN 1 ELSE 0 END) AS reply_count
            FROM message_log{clause} GROUP BY msg_key{having}
            ORDER BY sort_id DESC LIMIT ? OFFSET ?""",
                [*values, *group_values, limit, offset]).fetchall()
            # 明细查询也在这把锁里做（`_message_items` 复用同一个 db），可重入锁不会自锁。
            return {"rows": [self._message_row(db, row, conditions, values) for row in rows], "total": total}

    def _message_row(self, db, row, conditions, values):
        """补出一条归集行（消息层字段 + 明细列表）。"""
        items, truncated = self._message_items(db, row["msg_key"], row["msg_id"], conditions, values)
        reply = next((item for item in items if item["kind"] in SEND_KINDS), None)
        return {
            "msg_key": row["msg_key"],
            # 空标识的旧行合成一条「无关联」条目，页面据此显示成不挂在任何消息上的历史记录。
            "unlinked": not row["msg_key"],
            "msg_id": row["msg_id"],
            "ts_utc": row["ts_utc"],
            # 回复耗时：从收到到该消息最后一条回复（send/preview）的间隔，缺任一端为 None。
            "elapsed_ms": _elapsed_ms(row["receive_ts"], row["reply_ts"]),
            "chat_id": row["chat_id"],
            # 会话显示名：走注入的 chat_name_resolver（框架侧持有的群名快照）。
            # 拿不到时为「没数据」——保留空串，不拿 chat_id 冒充群名，由页面决定回落显示。
            "chat_name": self._chat_name_of(row["chat_id"]),
            "sender_id": row["sender_id"],
            "is_group": row["is_group"],
            "text": row["text"],
            "plugin_count": row["plugin_count"],
            "has_error": bool(row["error_count"]),
            "replied": bool(row["reply_count"]),
            # 回复正文取该消息下第一条发送/预览记录；`reply_kind` 让页面能标出「预览」。
            "reply": reply["summary"] if reply else "",
            "reply_kind": reply["kind"] if reply else "",
            "items_truncated": truncated,
            "items": items,
        }

    def _message_items(self, db, msg_key, msg_id, conditions, values):
        """取该消息下的明细（按记录顺序 id 升序）；返回 (明细列表, 是否被截断)。

        锚点那条 receive 记录不重复列出（正文已在消息层的 `text` 里）；「无关联」条目没有锚点
        （`msg_id=0`），它组内的每一行都列在这里，历史行一条不少。
        多取一条用来判断是否截断（`DETAIL_LIMIT`）。
        """
        sql = " WHERE msg_key=? AND id<>?"
        if conditions:
            sql += f" AND {' AND '.join(conditions)}"
        rows = db.execute(f"""SELECT id, ts_utc, kind, plugin, ms, summary FROM message_log{sql}
            ORDER BY id LIMIT ?""", [msg_key, msg_id, *values, DETAIL_LIMIT + 1]).fetchall()
        truncated = len(rows) > DETAIL_LIMIT
        items = [{"id": row["id"], "ts_utc": row["ts_utc"], "kind": row["kind"], "plugin": row["plugin"],
                  "ms": row["ms"], "summary": row["summary"], "reply": ""} for row in rows[:DETAIL_LIMIT]]
        # 插件/异常条目带上「这个插件的回复」：同一条消息下该插件名对应的第一条发送/预览正文。
        replies = {}
        for item in items:
            if item["kind"] in SEND_KINDS and item["plugin"] and item["plugin"] not in replies:
                replies[item["plugin"]] = item["summary"]
        for item in items:
            if item["kind"] in SEND_KINDS:
                item["reply"] = item["summary"]
            else:
                item["reply"] = replies.get(item["plugin"], "")
        return items, truncated

    def plugin_today(self, day):
        """今日各插件的处理数与异常数，供插件表与实时状态合并。"""
        start, end = beijing_day_bounds(day)
        with self._store._db_lock:
            rows = self._db().execute("""SELECT plugin, kind, COUNT(*) AS count FROM message_log
                WHERE plugin<>'' AND kind IN (?,?) AND ts_utc>=? AND ts_utc<?
                GROUP BY plugin, kind""",
                (HANDLED_KIND, ERROR_KIND, start, end)).fetchall()
            result = {}
            for row in rows:
                item = result.setdefault(row["plugin"], {"handled": 0, "errors": 0})
                item["handled" if row["kind"] == HANDLED_KIND else "errors"] += row["count"]
            return result
