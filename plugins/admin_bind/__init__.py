"""私聊一次性 token 自助绑定管理员：群聊不触发。"""
import time

# 窗口必须短暂：token 是明文，长时间等待会让旁人在本人离开后抢先输入。
PENDING_SECONDS = 120
MAX_FAILURES = 5
BLOCK_SECONDS = 600


class Plugin:
    def on_load(self, ctx):
        # sender wxid → (窗口截止时间, 失败次数, 冷却截止时间)；插件回调在同一线程串行执行，无需加锁。
        self.pending = {}

    def on_message(self, message, ctx):
        if message.type != 1 or message.is_group:
            return
        text = message.text.strip()
        now = time.monotonic()
        if text == "绑定管理员":
            if not ctx.admins.has_token():
                # 未配置 admin_token 时整条功能不启用，不开启窗口。
                return
            self._prune(now)
            deadline, failures, blocked = self.pending.get(message.sender_id, (0.0, 0, 0.0))
            if now < blocked:
                # token 短且走私聊，必须限次，否则可在线枚举。
                return
            self.pending[message.sender_id] = (now + PENDING_SECONDS, failures, 0.0)
            ctx.reply(message, "请输入管理员 token")
            return
        entry = self.pending.get(message.sender_id)
        if entry is None or now >= entry[0] or now < entry[2]:
            # 无窗口或已过期：普通私聊文本不能当 token 使用。
            return
        if ctx.admins.bind(text, message.sender_id):
            self.pending.pop(message.sender_id, None)
            ctx.reply(message, "已绑定为管理员。")
            return
        failures = entry[1] + 1
        if failures >= MAX_FAILURES:
            self.pending[message.sender_id] = (0.0, failures, now + BLOCK_SECONDS)
            ctx.reply(message, "token 错误次数过多，请 10 分钟后再试。")
        else:
            # 窗口一次性：失败后关闭窗口，只累计失败次数。
            self.pending[message.sender_id] = (0.0, failures, 0.0)
            ctx.reply(message, "token 不正确。")

    def _prune(self, now):
        """开新窗口时清掉窗口与冷却都结束、且没有失败历史的条目，避免 pending 只增不减。"""
        # 带失败次数的条目必须留着：删掉等于把计数清零，限次与冷却就形同虚设。
        for sender in [key for key, entry in self.pending.items()
                       if entry[1] == 0 and now >= entry[0] and now >= entry[2]]:
            del self.pending[sender]
