"""管理员账号与需管理员权限的命令：以配置文件为唯一来源，文件变更后立即生效。"""
from __future__ import annotations

import hmac
import logging
import threading

logger = logging.getLogger("AdminPolicy")


def command_of(text):
    """取消息的首个空白分隔字段作为命令名，与配置里的 admin_commands 同名比对。"""
    words = text.strip().split(maxsplit=1)
    return words[0] if words else ""


class AdminPolicy:
    """admins 为空表示未启用管理员限制；admin_commands 列出需要管理员权限的命令。"""

    def __init__(self, store=None):
        self._store = store
        self._lock = threading.RLock()
        self._stamp = None
        self._accounts = frozenset()
        self._commands = frozenset()
        self._token = ""
        self._error = ""
        self._refresh()

    def _refresh(self):
        """按文件 mtime+size 判断是否有变更；无变更时复用上一份快照，避免逐条消息读盘。"""
        if self._store is None:
            return
        try:
            info = self._store.path.stat()
        except OSError:
            return
        stamp = (info.st_mtime_ns, info.st_size)
        with self._lock:
            if stamp == self._stamp:
                return
        try:
            data = self._store.load()
        except (OSError, ValueError) as error:
            # 手工编辑可能留下半个文件；保留上一份可用快照，等下一次变更再读。
            if str(error) != self._error:
                self._error = str(error)
                logger.warning("管理员策略暂用上一份配置：%s", error)
            return
        with self._lock:
            self._stamp = stamp
            self._accounts = frozenset(data.get("admins", []))
            self._commands = frozenset(data.get("admin_commands", []))
            self._token = data.get("admin_token", "")
            self._error = ""

    def blocked(self, message):
        """配置了管理员、命令在 admin_commands 中、且发送者不是管理员时返回 True。"""
        self._refresh()
        with self._lock:
            if not self._accounts or not self._commands:
                return False
            return command_of(message.text) in self._commands and message.sender_id not in self._accounts

    def has_token(self):
        """是否配置了一次性 token；未配置时私聊自助绑定不启用。"""
        self._refresh()
        with self._lock:
            return bool(self._token)

    def bind(self, token, account):
        """校验 token 并把账号加入管理员，成功后作废 token；返回是否成功。"""
        if self._store is None or not token:
            return False
        self._refresh()
        with self._lock:
            expected = self._token
        if not expected or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return False
        data = self._store.bind_admin(account)
        with self._lock:
            # 落盘结果立即生效；同时作废本地快照时间戳，下次访问重新对齐文件。
            self._stamp = None
            self._accounts = frozenset(data.get("admins", []))
            self._commands = frozenset(data.get("admin_commands", []))
            self._token = data.get("admin_token", "")
        return True
