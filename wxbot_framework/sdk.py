"""消息模型和插件上下文；插件不需要接触 Hook 或 DLL。"""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from contextlib import nullcontext, contextmanager
from types import MappingProxyType
from typing import Mapping, Any

from .admins import AdminPolicy
from .text import validate_target, validate_mentions
from .pacing import TextSendDelay

logger = logging.getLogger("TextSend")


class CapabilityError(NotImplementedError):
    pass


@dataclass(frozen=True)
class Message:
    text: str
    chat_id: str
    sender_id: str = ""
    receiver_id: str = ""
    protocol: int = 11046
    type: int = 1
    is_group: bool = False
    is_self: bool = False
    sequence: int = 0
    session_id: str = ""
    received_at: float = field(default_factory=time.monotonic)
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)
    # 管理台调试页构造的假消息带此标记：只有它自己的发送会被拦成预览。
    debug: bool = False

    @classmethod
    def from_native(cls, protocol, data, session_id):
        snapshot = copy.deepcopy(data)
        room = snapshot.get("room_wxid") or ""
        return cls(text=snapshot.get("msg") or snapshot.get("content") or "",
                   chat_id=snapshot.get("chat_id") or room or snapshot.get("from_wxid") or "",
                   sender_id=snapshot.get("sender_id") or snapshot.get("native_sender") or snapshot.get("from_wxid") or "",
                   receiver_id=snapshot.get("native_receiver") or "", protocol=protocol,
                   is_group=bool(room), is_self=bool(snapshot.get("is_self", False)), sequence=snapshot.get("sequence", 0),
                   session_id=session_id, data=MappingProxyType(snapshot))


class ServiceFacade:
    """序列化发送与连接切换，避免卸载正在使用的原生桥接。"""

    def __init__(self, *, text_delay=None, instruments=None):
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._backend = None
        self._available = False
        self._cancel = threading.Event()
        self.text_delay = TextSendDelay.from_config(text_delay)
        # 管理台埋点；未接入时为空实现，收发行为与从前完全一致。
        self._instruments = instruments

    def bind(self, backend):
        with self._lock:
            if self._backend is not None:
                raise RuntimeError("已有连接，需先关闭")
            self._backend = backend
            self._available = True
            self._cancel = threading.Event()

    @property
    def connected(self):
        with self._lock:
            return self._backend is not None and self._available

    def send_text(self, to_wxid, content, *, expected_session=None, _mentions=(), delivery_guard=None):
        validate_target(to_wxid)
        if not isinstance(content, str):
            raise ValueError("文本正文必须是字符串")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > 1023 or b"\0" in encoded:
            raise ValueError("文本应为 1–1023 个 UTF-8 字节，且不能包含 NUL")
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话回复")
            if _mentions and not callable(getattr(self._backend, "send_at_text", None)):
                raise CapabilityError("当前后端尚未开放真实群艾特")
            backend, cancel = self._backend, self._cancel
        # 等待不占用连接锁，状态查询、断线清理和退出可以及时进行。
        # 发送锁覆盖等待及原生发送，多个插件不会同时等待后集中发送。
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
            seconds = self.text_delay.seconds_for(content)
            logger.debug("文本发送等待 %.3f 秒（%d 个字符）", seconds, len(content))
            if cancel.wait(seconds):
                raise RuntimeError("微信连接已结束，取消待发送文本")
            with self._lock:
                self._check_pending(backend, cancel)
                # 在文本节奏等待之后检查记录版本，清空后不发送旧总结。
            instruments = self._instruments
            if instruments is not None and instruments.previewing_here:
                # 调试请求自己的发送：记成预览并返回成功，不触碰后端锁、不调用原生发送。
                instruments.record_send(to_wxid, content, plugin=instruments.current_plugin, preview=True)
                return True
            # 原生发送不占用连接锁：状态查询、断线清理和退出不必等待在途发送。
            # 发送锁与 close 的在途检查提供卸载保护，等待与原生发送整体串行化。
            with delivery_guard() if delivery_guard is not None else nullcontext():
                if _mentions:
                    result = backend.send_at_text(to_wxid, content, _mentions)
                else:
                    result = backend.send_text(to_wxid, content)
            if result and instruments is not None:
                # 真实发送成功后才记发出并置位本次回调的「已回复」标志；不新增任何等待。
                instruments.record_send(to_wxid, content, plugin=instruments.current_plugin)
                instruments.mark_replied()
            return result

    def send_at_text(self, room_wxid, content, at_list, *, expected_session=None, display_names=None, delivery_guard=None):
        members = validate_mentions(room_wxid, at_list)
        if not isinstance(content, str) or not content:
            raise ValueError("文本正文必须是非空字符串")
        names = members if display_names is None else display_names
        if not isinstance(names, (list, tuple)) or len(names) != len(members) or any(
                not isinstance(name, str) or not name.strip() or len(name) > 64 or
                any(ch in name for ch in ("\n", "\r", "\0")) for name in names):
            raise ValueError("艾特显示昵称必须与成员列表一一对应，且每个昵称最多 64 字符")
        # 标签用于显示；真实通知始终使用 members 中的微信 ID。
        labels = " ".join("@" + name + "\u2005" for name in names)
        return self.send_text(room_wxid, labels + "\n" + content,
                              expected_session=expected_session, _mentions=members, delivery_guard=delivery_guard)

    def _check_pending(self, backend, cancel):
        if cancel.is_set() or not self._available or self._backend is not backend or self._cancel is not cancel:
            raise RuntimeError("微信连接已结束，取消待发送文本")

    def cancel_pending(self):
        with self._lock:
            self._available = False
            self._cancel.set()

    def send_payload(self, payload, *, expected_session=None):
        if payload.get("type") != 11237:
            raise CapabilityError("当前后端只支持文本发送 11237")
        data = payload["data"]
        return self.send_text(data["to_wxid"], data["content"], expected_session=expected_session)

    def close(self):
        with self._lock:
            self.cancel_pending()
            backend = self._backend
            if backend is None:
                return None
        # 发送锁覆盖原生发送全程：拿到即没有在途发送；超时说明仍在发送，延迟清理。
        if not self._send_lock.acquire(timeout=1.0):
            logger.warning("正在发送原生文本，连接清理延后到本次发送结束")
            return None
        try:
            with self._lock:
                if self._backend is not backend:
                    return None
                # 清理失败时仍持有此实例，下次可以继续清理，不能重新注入。
                result = backend.stop()
                self._backend = None
                return result
        finally:
            self._send_lock.release()


class LegacyService:
    def __init__(self, service, session_id):
        self._service, self._session_id = service, session_id

    def send_text(self, to_wxid, content):
        return self._service.send_text(to_wxid, content, expected_session=self._session_id)

    def send_payload(self, payload):
        return self._service.send_payload(payload, expected_session=self._session_id)


class PluginContext:
    def __init__(self, name, options, service, capabilities, *, llm_config_path=None, member_store=None, admins=None):
        self.name = name
        self.config = MappingProxyType(copy.deepcopy(options))
        self.logger = logging.getLogger(f"Plugin.{name}")
        self.capabilities = frozenset(capabilities)
        self._service = service
        self._llm_config_path = llm_config_path
        self._llm = None
        self._members = member_store
        self._admins = admins if admins is not None else AdminPolicy()

    @property
    def members(self):
        if self._members is None:
            raise CapabilityError("尚未配置框架成员目录")
        return self._members

    @property
    def admins(self):
        # 暴露共享注册表，供插件调用 bind / has_token 实现运行期自助绑定。
        return self._admins

    @property
    def llm(self):
        if self._llm is None:
            if self._llm_config_path is None:
                raise ValueError("尚未配置大模型文件")
            from .llm import LLMClient
            self._llm = LLMClient.from_file(self._llm_config_path)
        return self._llm

    def send_text(self, chat_id, text):
        return self._service.send_text(chat_id, text)

    def personality(self, message):
        from .personality import PersonalityState
        return self._members.personality(message.chat_id) if self._members is not None else PersonalityState(message.chat_id)

    def llm_for(self, message, *, personality=None):
        from .personality import PersonalityLLM
        state = personality if personality is not None else self.personality(message)
        return PersonalityLLM(self.llm, state, current=lambda: self._members is None or
                              self._members.personality_is_current(state))

    def personality_guard(self, state):
        return self._members.personality_delivery_guard(state) if self._members is not None else nullcontext()

    @contextmanager
    def response_guard(self, snapshot, personality):
        # 只在进入时校验快照与人格的当前版本，不在 yield 期间持有成员锁：
        # guard 内再调用 reply() 会先取发送锁，而另一线程正持发送锁等待成员锁，
        # 跨 yield 持锁会形成 ABBA 死锁。原生发送期间的保护交给 send 路径的 delivery_guard。
        if self._members is not None:
            with self.members.delivery_guard(snapshot):
                pass
        with self.personality_guard(personality):
            pass
        yield

    def reply(self, message, text, *, delivery_guard=None):
        return self._service.send_text(message.chat_id, text, expected_session=message.session_id,
                                       delivery_guard=delivery_guard)

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        if not message.is_group:
            raise ValueError("reply_at 仅用于群消息")
        if self._members is not None:
            member = self._members.find_by_wxid(message.sender_id, message.chat_id)
            if member is not None and (display_name is None or member.self_named):
                display_name = member.display_name
            elif display_name is None:
                display_name = self._members.wechat_name(message.sender_id)
        return self._service.send_at_text(message.chat_id, text, [message.sender_id],
                                         expected_session=message.session_id,
                                         display_names=[display_name] if display_name is not None else None,
                                         delivery_guard=delivery_guard)
