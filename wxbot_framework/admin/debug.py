"""管理台调试页后端：喂一条假消息、跑一遍插件，收集命中、回复、报错与耗时。

调试**只走插件分发**：默认调 `PluginManager.dispatch`，指定单个插件时只投给该插件的队列；
不经过 `BotApplication._emit`，因此不触发白名单过滤、管理员拦截与人格切换，也不写成员库。

默认不真的发消息：假消息带 `Message.debug=True`，并在 `preview_scope` 里派发，发送收口据此把
「这次调试自己的回复」拦成预览（`kind="preview"`）。预览窗口必须覆盖「插件回调全部结束」的
整个窗口，所以顺序是：进预览 → 派发 → 等各插件队列排空 → 退预览。提前退出会把调试回复真的
发进会话；而窗口内并发到达的真实消息不带调试标记、也不在预览线程里，它们的回复照常发出，
不会被试跑吞掉。

`send_for_real=True` 时不进预览窗口（回复真的发出），但假消息自身仍带调试标记以标识来源。

结果汇聚靠 `Instruments.collect`：调试期间把每条记录同时收进本地列表，再按插件名归集。
这要求统计库已 attach（`Instruments.enabled`）；未 attach 时一条记录都收不到，此时直接报错，
不静默返回空结果。
"""
from __future__ import annotations

import re
import threading
import time

from ..sdk import Message
from .instrument import PREVIEW_PREFIX

# 结果里每个插件的状态。
REPLIED = "replied"      # 命中了并回复（调试模式下回复被拦成预览）
SILENT = "silent"        # 跑完了但没回复
ERROR = "error"          # 回调抛异常，或超时仍未跑完
SKIPPED = "skipped"      # 没有产生任何处理记录：未在运行、队列已满被丢弃等

TIMEOUT_ERROR = "超时"
DISABLED_ERROR = "管理台未启用，无法调试"
BUSY_ERROR = "已有调试在进行，请稍后再试"

# plugin 记录的摘要末段是 `_elapsed_text(ms)`（例如 `1.5ms`）；取不到就记 0.0。
_MS_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]+)?)ms\s*$")


def _reply_text(record):
    """从发送/预览记录里取回复正文；预览记录的摘要带 [预览] 前缀。"""
    summary = record.summary or ""
    if summary.startswith(PREVIEW_PREFIX):
        return summary[len(PREVIEW_PREFIX):]
    return summary


def _callback_ms(summary):
    """插件耗时只有 plugin 记录的摘要里带着，按格式取回；取不到记 0.0。"""
    match = _MS_PATTERN.search(summary or "")
    return float(match.group(1)) if match else 0.0


class Debugger:
    """把一条假消息跑一遍插件并把结果汇聚成网页要的字典。"""

    def __init__(self, plugins, instruments, *, timeout=30.0):
        self.plugins = plugins
        self.instruments = instruments
        self.timeout = max(0.0, float(timeout))
        # 同一时刻只放一个调试请求通过：并发进来的直接报错，免得两个请求的记录互相串台
        # （collect 的收集器是共享的），也免得共享预览窗口互相干扰。
        self._run_lock = threading.Lock()

    def run(self, *, plugin="", chat_id, sender_id="", text, send_for_real=False):
        """跑一次调试；返回各插件的命中、回复、报错、耗时与整体汇总。"""
        if not self.instruments.enabled:
            raise RuntimeError(DISABLED_ERROR)
        if not self._run_lock.acquire(timeout=self.timeout):
            raise RuntimeError(BUSY_ERROR)
        try:
            # run 级开关：`debug_preview` 为真时「带调试标记的消息」其回复一律拦成预览，
            # 不依赖预览窗口是否还开着——调试超时先返回后，慢插件迟到的回复也不会真的发出。
            with self.instruments.debug_scope(preview=not send_for_real):
                return self._run(plugin, chat_id, sender_id, text, send_for_real)
        finally:
            self._run_lock.release()

    def _run(self, plugin, chat_id, sender_id, text, send_for_real):
        started = time.monotonic()
        message = Message(text=text, chat_id=chat_id, sender_id=sender_id, type=1,
                          is_group=chat_id.endswith("@chatroom"), debug=True)
        targets, dispatched = self._targets(plugin)
        collected = []
        # 收集器只做 list.append，出错不会影响消息路径。
        with self.instruments.collect(collected):
            if send_for_real:
                self._dispatch(plugin, message, targets)
                timed_out = self._wait(targets)
            else:
                with self.instruments.preview_scope():
                    self._dispatch(plugin, message, targets)
                    # 必须等回调全部结束再退出预览，否则调试回复会被真的发出去。
                    timed_out = self._wait(targets)
        return self._summarize(targets, collected, timed_out, dispatched, started, send_for_real)

    def _targets(self, plugin):
        """本次要跑哪些插件，以及实际投出去的条数。"""
        if plugin:
            # 指定单个插件时不调 dispatch（那会发给所有插件），只投给它的队列。
            return [self._running_record(plugin)], 1
        records = list(self.plugins.records.values())
        return records, sum(1 for record in records if record.active)

    def _running_record(self, name):
        record = self.plugins.records.get(name)
        if record is None:
            raise ValueError(f"未找到插件：{name}")
        if not self.plugins.is_active(name):
            raise ValueError(f"插件未在运行：{name}")
        return record

    def _dispatch(self, plugin, message, targets):
        if plugin:
            targets[0].enqueue(message)
        else:
            self.plugins.dispatch(message)

    def _wait(self, targets):
        """等每个插件的队列排空；超时返回还没跑完的插件名（不杀线程）。"""
        pending = [record for record in targets if record.active]
        if not pending:
            return set()
        done = threading.Event()

        def waiter():
            for record in pending:
                record.queue.join()
            done.set()

        threading.Thread(target=waiter, name="AdminDebugWait", daemon=True).start()
        if done.wait(self.timeout):
            return set()
        # 超时只把仍在处理的插件标出来：回调线程随后自己跑完，结果照常返回。
        return {record.spec.name for record in pending if record.queue.unfinished_tasks}

    def _summarize(self, targets, collected, timed_out, dispatched, started, send_for_real):
        # 预览模式下调试回复一定被拦成 preview；此时的 send 只可能来自预览窗口内并发到达的
        # 真实消息，不能算作这次调试的回复。真发模式下预览不存在，只认 send。
        send_kind = "send" if send_for_real else "preview"
        entries = [self._entry(record, collected, timed_out, send_kind) for record in targets]
        return {"plugins": entries,
                "dispatched": dispatched,
                "replied": sum(1 for entry in entries if entry["state"] == REPLIED),
                "ms": (time.monotonic() - started) * 1000,
                "sent_for_real": bool(send_for_real)}

    def _entry(self, record, collected, timed_out, send_kind):
        """按插件名归集记录：一条假消息在该插件上留下的发送/预览、处理与异常记录。"""
        name = record.spec.name
        plugin_records = [item for item in collected if item.kind == "plugin" and item.plugin == name]
        errors = [item for item in collected if item.kind == "error" and item.plugin == name]
        sends = [item for item in collected if item.kind == send_kind and item.plugin == name]
        entry = {"name": name, "state": SKIPPED, "reply": "", "ms": 0.0, "error": ""}
        if errors:
            entry["state"] = ERROR
            entry["error"] = errors[-1].summary
        elif sends:
            entry["state"] = REPLIED
            entry["reply"] = _reply_text(sends[-1])
        elif plugin_records:
            entry["state"] = SILENT
        elif name in timed_out:
            entry["state"] = ERROR
            entry["error"] = TIMEOUT_ERROR
        if plugin_records:
            entry["ms"] = _callback_ms(plugin_records[-1].summary)
        return entry
