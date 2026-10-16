"""私聊一次性 token 自助绑定管理员：群聊不触发。"""
import time

# 窗口必须短暂：token 是明文，长时间等待会让旁人在本人离开后抢先输入。
PENDING_SECONDS = 120


class Plugin:
    def on_load(self, ctx):
        # sender wxid → 窗口截止时间；插件回调在同一线程串行执行，无需加锁。
        self.pending = {}

    def on_message(self, message, ctx):
        if message.type != 1 or message.is_group:
            return
        text = message.text.strip()
        if text == "绑定管理员":
            if not ctx.admins.has_token():
                # 未配置 admin_token 时整条功能不启用，不开启窗口。
                return
            self.pending[message.sender_id] = time.monotonic() + PENDING_SECONDS
            ctx.reply(message, "请输入管理员 token")
            return
        deadline = self.pending.pop(message.sender_id, None)
        if deadline is None or time.monotonic() >= deadline:
            # 无窗口或已过期：普通私聊文本不能当 token 使用。
            return
        if ctx.admins.bind(text, message.sender_id):
            ctx.reply(message, "已绑定为管理员。")
        else:
            ctx.reply(message, "token 不正确。")
