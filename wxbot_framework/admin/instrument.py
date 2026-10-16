"""管理台埋点：把消息路径上的收发、插件处理、异常与系统日志旁路记进统计库。

这是热路径上的旁路代码：未 `attach` 统计库时，所有 `record_*` 都是空实现；
`attach` 之后也只往 `AdminStore` 的内存缓冲里追加（落盘由统计库的定时线程负责），
既不阻塞调用线程，也绝不把异常抛回消息路径。

插件回调期间用 `plugin_scope` 建立线程级的「当前插件 / 当前消息」上下文，
`record_plugin` / `record_error` 据此把所属群与群/私聊标记带进记录（`groups` 的
「最活跃插件」按群聚合要用）。因此 `record_plugin` / `record_error` 必须在该 scope 内调用。

预览拦截只针对「调试请求自己的发送」：`preview_scope` 会记下进入它的线程，
`plugin_scope` 会记下本线程正在处理的消息是否是调试假消息（`Message.debug`）。
发送收口用 `previewing_here` 判断，因此预览窗口内并发到达的真实消息（其它线程、
且消息不带调试标记）的回复照常发出，不会被调试试跑吞掉。

累计计数由本文件自增：除这里之外没有别的地方会写 `stats_total`。
`allowed=False` 的收到消息只落一行 `message_log`（`allowed=0`）、不自增任何累计，
以保证查询层「今日」（按 `allowed=1` 过滤）与「累计」（读 `stats_total`）口径一致；
调试预览的发送单列 `kind="preview"` 且同样不自增累计，免得试跑污染「发出」统计。
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from .store import Record

LOGGER = logging.getLogger("Admin")

# 预览记录的摘要前缀：日志列表里一眼能看出这条没有真的发出去。
PREVIEW_PREFIX = "[预览] "


def _elapsed_text(ms):
    """把耗时格式化成摘要里的文本；类型不对也不抛，只退回字符串。"""
    try:
        return f"{float(ms):.1f}ms"
    except (TypeError, ValueError):
        return str(ms)


class ReplyFlag:
    """插件回调期间共享的「这条消息有没有被回复过」标志。

    `mark()` 供发送收口在真实发送成功后置位；`replied` 同时参考调用方给的可调用口径
    （`probe`），两者取或——发送路径与回调自身都能表达「已回复」。
    """

    __slots__ = ("_marked", "_probe")

    def __init__(self, probe=None):
        self._marked = False
        self._probe = probe

    def mark(self):
        self._marked = True

    @property
    def replied(self):
        if self._probe is not None:
            return self._marked or bool(self._probe())
        return self._marked

    def __bool__(self):
        return self.replied


class Instruments:
    """消息路径与管理台统计库之间的旁路记录层；未启用时是空实现。"""

    def __init__(self, *, store=None):
        self._store = store
        self._state = threading.local()
        self._preview_lock = threading.Lock()
        self._previewing = False
        # 只有调试期间（collect 生效时）非空；正常运行时 `_record` 里是一次空循环。
        self._collectors = []

    @property
    def enabled(self):
        """是否已接入统计库；未接入时全部记录都是空实现。"""
        return self._store is not None

    def attach(self, store):
        self._store = store

    def detach(self):
        self._store = None

    @property
    def current_plugin(self):
        """当前线程正在回调的插件名；插件回调之外为空串。"""
        return getattr(self._state, "plugin", "")

    @property
    def previewing(self):
        """是否有调试请求在进行中（全局唯一）。"""
        with self._preview_lock:
            return self._previewing

    @property
    def previewing_here(self):
        """本线程的发送是否该被拦成预览。

        只有「调试请求自己的发送」为真：进入 `preview_scope` 的线程，
        或正在处理调试假消息（`Message.debug`）的插件回调线程。
        预览窗口内并发到达的真实消息不带调试标记、也不在该线程里，
        因此它的回复照常发出，不会被调试试跑吞掉。
        """
        if not self.previewing:
            return False
        return bool(getattr(self._state, "preview_scope", False)
                    or getattr(self._state, "debug_message", False))

    def mark_replied(self):
        """发送收口在真实发送成功后调用：把当前线程 scope 的回复标志置位。"""
        flag = getattr(self._state, "reply_flag", None)
        if flag is not None:
            flag.mark()

    @contextmanager
    def plugin_scope(self, name, *, message=None, replied=lambda: False):
        """进入插件回调上下文，产出可置位的回复标志。

        `message` 是本次回调处理的消息，写进线程局部供 `record_plugin` / `record_error`
        取所属群与群/私聊标记；`replied` 是调用方给的可调用口径（例如回调内某个局部变量）。
        消息带调试标记时同样记进线程局部，供 `previewing_here` 判断。
        """
        state = self._state
        previous = (getattr(state, "plugin", ""), getattr(state, "message", None),
                    getattr(state, "reply_flag", None), getattr(state, "debug_message", False))
        flag = ReplyFlag(replied)
        state.plugin = name
        state.message = message
        state.reply_flag = flag
        state.debug_message = bool(getattr(message, "debug", False))
        try:
            yield flag
        finally:
            state.plugin, state.message, state.reply_flag, state.debug_message = previous

    @contextmanager
    def preview_scope(self):
        """调试期间的预览开关；同一时刻只允许一个（否则抛 RuntimeError）。

        进入的线程自身被记进线程局部：它的发送按预览拦截，其它线程不受影响。
        """
        with self._preview_lock:
            if self._previewing:
                raise RuntimeError("已有调试在进行")
            self._previewing = True
        previous = getattr(self._state, "preview_scope", False)
        self._state.preview_scope = True
        try:
            yield
        finally:
            self._state.preview_scope = previous
            with self._preview_lock:
                self._previewing = False

    @contextmanager
    def collect(self, records):
        """调试期间把每条记录同时追加进给定列表；退出后恢复原状。"""
        self._collectors.append(records)
        try:
            yield records
        finally:
            for index in range(len(self._collectors) - 1, -1, -1):
                if self._collectors[index] is records:
                    del self._collectors[index]
                    break

    def record_receive(self, message, *, allowed):
        """记一条收到消息；未通过白名单的只落日志、不自增任何累计。"""
        if not self.enabled:
            return
        is_group = bool(getattr(message, "is_group", False))
        chat_id = getattr(message, "chat_id", "") or ""
        self._record(
            Record(kind="receive", chat_id=chat_id, sender_id=getattr(message, "sender_id", "") or "",
                   is_group=is_group, allowed=bool(allowed), summary=getattr(message, "text", "") or ""),
            self._received_bumps(allowed, chat_id, is_group))

    def record_send(self, to_wxid, content, *, plugin="", preview=False):
        """记一条发出消息；`preview=True` 表示这条在调试里被拦成预览、没有真的发出。

        预览单独记成 `kind="preview"`、不自增任何累计，免得调试页的试跑污染「发出」统计；
        真实发送仍写 `kind="send"` 并自增 `sent`。
        """
        if not self.enabled:
            return
        to_wxid = to_wxid or ""
        content = content or ""
        is_group = to_wxid.endswith("@chatroom")
        if preview:
            self._record(Record(kind="preview", chat_id=to_wxid, plugin=plugin or "",
                                is_group=is_group, summary=PREVIEW_PREFIX + content))
            return
        bumps = [("global", "", "sent")]
        if is_group:
            bumps.append(("group", to_wxid, "sent"))
        self._record(Record(kind="send", chat_id=to_wxid, plugin=plugin or "",
                            is_group=is_group, summary=content), bumps)

    def record_plugin(self, name, *, handled, ms, replied):
        """记一条插件处理结果；必须在 `plugin_scope` 内调用以带上所属群与群/私聊标记。"""
        if not self.enabled:
            return
        chat_id, is_group = self._current_target()
        summary = " · ".join(("已处理" if handled else "未处理",
                              "已回复" if replied else "未回复", _elapsed_text(ms)))
        self._record(Record(kind="plugin", chat_id=chat_id, plugin=name or "",
                            is_group=is_group, summary=summary), [("plugin", name or "", "handled")])

    def record_error(self, name, error):
        """记一条插件异常摘要；堆栈仍写在原来的文本日志里。"""
        if not self.enabled:
            return
        chat_id, is_group = self._current_target()
        summary = f"{type(error).__name__}: {error}"
        self._record(Record(kind="error", chat_id=chat_id, plugin=name or "",
                            is_group=is_group, summary=summary), [("plugin", name or "", "errors")])

    def record_system(self, level, name, text):
        """记一条框架日志（不归属任何插件、不自增累计）。"""
        if not self.enabled:
            return
        self._record(Record(kind="system", summary=f"[{level}] {name}：{text}"))

    def _received_bumps(self, allowed, chat_id, is_group):
        # 白名单外的消息不计入累计：与查询层「今日」按 allowed=1 过滤的口径保持一致。
        if not allowed:
            return ()
        bumps = [("global", "", "received")]
        if is_group and chat_id:
            bumps.append(("group", chat_id, "received"))
        return bumps

    def _current_target(self):
        """从线程局部的「当前消息」取所属群与群/私聊标记；没有上下文时按私聊空群处理。"""
        message = getattr(self._state, "message", None)
        if message is None:
            return "", False
        return getattr(message, "chat_id", "") or "", bool(getattr(message, "is_group", False))

    def _record(self, record, bumps=()):
        """把一条记录写进统计库与调试收集器；任何失败只记日志，绝不抛回消息路径。"""
        store = self._store
        if store is None:
            return
        try:
            store.append(record)
            for scope, scope_key, metric in bumps:
                store.bump(scope, scope_key, metric)
        except Exception as error:
            LOGGER.warning("管理台埋点记录失败：%s", error)
        for collector in self._collectors:
            collector.append(record)
