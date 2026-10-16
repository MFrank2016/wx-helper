"""配置读取与插件启停状态保存。"""
from __future__ import annotations

import json
import math
import re
import os
import tempfile
from pathlib import Path

PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EXAMPLE_ACCOUNT = "wxid_example_robot"


def account_is_unbound(account):
    return account is None or account == "" or account == EXAMPLE_ACCOUNT


def initialize_configs(directory):
    """只补齐缺失的本地配置，不覆盖已经填写的账号、群聊或密钥。"""
    directory = Path(directory)
    created = []
    for stem in ("bot_config", "members_config", "llm_config"):
        destination = directory / (stem + ".json")
        if destination.exists():
            continue
        template = (directory / (stem + ".example.json")).read_text(encoding="utf-8-sig")
        try:
            with destination.open("x", encoding="utf-8", newline="\n") as output:
                output.write(template)
        except FileExistsError:
            continue
        created.append(destination.name)
    return created


def validate_name(name):
    if not isinstance(name, str) or not PLUGIN_NAME.fullmatch(name):
        raise ValueError(f"插件目录名不合法：{name!r}")
    return name


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def load(self):
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置文件必须是 JSON 对象")
        if data.get("backend", "native_filehelper") not in ("native_filehelper", "native_text"):
            raise ValueError("backend 应为 native_filehelper 或 native_text")
        from .pacing import TextSendDelay
        TextSendDelay.from_config(data.get("text_send_delay"))
        llm_file = data.get("llm_config_file", "llm_config.json")
        if not isinstance(llm_file, str) or not llm_file.strip() or "\0" in llm_file:
            raise ValueError("llm_config_file 应为大模型配置文件路径")
        members_file = data.get("members_config_file")
        if members_file is not None and (not isinstance(members_file, str) or not members_file.strip() or "\0" in members_file):
            raise ValueError("members_config_file 应为成员配置文件路径或 null")
        if not account_is_unbound(data.get("wechat_account")):
            from .account import validate_account
            validate_account(data["wechat_account"])
        admins = data.get("admins", [])
        if not isinstance(admins, list):
            raise ValueError("admins 应为微信 ID 列表；空列表表示未启用管理员限制")
        from .account import validate_account
        for admin in admins:
            validate_account(admin)
        admin_token = data.get("admin_token", "")
        if not isinstance(admin_token, str) or (admin_token and (
                not 6 <= len(admin_token) <= 64 or any(ch.isspace() or ord(ch) < 32 for ch in admin_token))):
            raise ValueError("admin_token 应为 6–64 个不含空白和控制字符的字符；空字符串表示关闭该功能")
        commands = data.get("admin_commands", [])
        if not isinstance(commands, list) or any(
                not isinstance(command, str) or not command or len(command) > 64 or
                any(ch.isspace() or ord(ch) < 32 for ch in command) for command in commands):
            raise ValueError("admin_commands 应为命令名列表，每项为不超过 64 个不含空白和控制字符的字符")
        allowed = data.get("chat_allowlist", [])
        if not isinstance(allowed, list):
            raise ValueError("chat_allowlist 应为微信 ID 或群 ID 列表；空列表表示所有会话")
        from .text import validate_target
        for target in allowed:
            validate_target(target)
        pid = data.get("wechat_pid")
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise ValueError("wechat_pid 应为正整数或 null")
        interval = data.get("reconnect_seconds", 5)
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0.1:
            raise ValueError("reconnect_seconds 应为至少 0.1 的有限数值")
        size = data.get("plugin_queue_size", 128)
        if type(size) is not int or not 1 <= size <= 10000:
            raise ValueError("plugin_queue_size 应为 1–10000 的整数")
        if not isinstance(data.get("plugins", {}), dict):
            raise ValueError("plugins 必须是 JSON 对象")
        for name, options in data.get("plugins", {}).items():
            validate_name(name)
            if not isinstance(options, dict):
                raise ValueError(f"plugins.{name} 必须是 JSON 对象")
            if "enabled" in options and type(options["enabled"]) is not bool:
                raise ValueError(f"plugins.{name}.enabled 必须是布尔值")
        if data.get("log_level", "INFO").upper() not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError("log_level 不受支持")
        if type(data.get("console", True)) is not bool:
            raise ValueError("console 必须是布尔值")
        return data

    def set_enabled(self, name, enabled):
        validate_name(name)
        data = self.load()
        data.setdefault("plugins", {}).setdefault(name, {})["enabled"] = bool(enabled)
        self._save(data)
        return data

    def bind_admin(self, account):
        from .account import validate_account
        validate_account(account)
        data = self.load()
        admins = data.setdefault("admins", [])
        if account not in admins:
            admins.append(account)
        # 一次性 token：绑定成功后立即作废，避免被重复使用。
        data["admin_token"] = ""
        return self._save(data)

    def bind_account(self, account):
        from .account import validate_account
        validate_account(account)
        if account_is_unbound(account):
            raise ValueError("不能把示例账号保存为机器人绑定")
        # 重新读取，保留账号选择期间对其他配置所作的修改。
        data = self.load()
        current = data.get("wechat_account")
        if not account_is_unbound(current):
            if current != account:
                raise RuntimeError("配置中的机器人账号已被修改，请重新启动后核对绑定")
            return data
        data["wechat_account"] = account
        self._save(data)
        return data

    def _save(self, data):
        # 在同一目录完成替换，避免中断写入留下半份 JSON。
        descriptor, temporary = tempfile.mkstemp(prefix=".bot_config_", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
