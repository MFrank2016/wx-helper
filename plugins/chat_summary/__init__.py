"""群命令：/总结 昵称，或 /总结群聊；每晚定时向所有已配置群播报群聊总结。"""
import functools
from datetime import datetime, timezone

from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.personality import personality_text
from wxbot_framework.summary import (ChatSummarizer, build_week_corpus, conversation_rows, daily_talkers,
                                     render_group_summary, split_text, week_since)

# 默认播报时间走框架既有的任务配置覆盖机制；改时间/开关由 bot_config.json 的
# plugins.chat_summary.tasks.daily_summary 决定，插件自己不读配置。
DAILY_SUMMARY_SCHEDULE = "每天 22:00"


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("聊天总结需要成员配置 history_enabled 为 true")
        self.summarizer = ChatSummarizer(ctx.llm, chunk_chars=ctx.config.get("chunk_chars", 16000))
        ctx.tasks.register("daily_summary", DAILY_SUMMARY_SCHEDULE,
                           functools.partial(self._broadcast, ctx))

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group or message.chat_id not in self.members.groups:
            return
        words = message.text.strip().split(maxsplit=1)
        if not words or words[0] not in ("/总结", "/总结群聊"):
            return
        group = words[0] == "/总结群聊"
        if len(words) != (1 if group else 2):
            ctx.reply(message, "用法：/总结群聊" if group else "用法：/总结 昵称")
            return
        try:
            snapshot = (self.members.today_group_snapshot(message.chat_id) if group else
                        self.members.today_snapshot(words[1], message.chat_id))
        except MemberError as error:
            ctx.reply(message, str(error) + ("。" if group else "。请本人先发送 /绑定 昵称。"))
            return
        rows = conversation_rows(snapshot)
        personality = ctx.personality(message)
        guard = lambda: ctx.response_delivery_guard(snapshot, personality)
        try:
            if not rows:
                name = "本群" if group else snapshot.member.name
                ctx.reply(message, f"{name}今天还没有可总结的聊天记录。", delivery_guard=guard)
                return
            if group:
                content = self._group_summary(ctx, snapshot, rows, personality)
            else:
                summarizer = ChatSummarizer(ctx.llm_for(message, personality=personality),
                                            chunk_chars=self.summarizer.chunk_chars)
                summary = summarizer.summarize(snapshot, rows,
                    current=lambda: self.members.snapshot_is_current(snapshot))
                title = f"{snapshot.member.name} 今日聊天（{snapshot.day}，{len(rows)}条）\n"
                content = personality_text(summary, personality.name) if personality.name == "嘴臭" else title + summary
            for part in split_text(content):
                if not ctx.reply(message, part, delivery_guard=guard):
                    raise RuntimeError("原生文本发送未成功")
            ctx.logger.info("聊天总结完成：room=%s scope=%s member=%s records=%d", message.chat_id,
                            "group" if group else "member", "本群" if group else snapshot.member.name, len(rows))
        except SnapshotExpired:
            ctx.logger.info("聊天总结已取消：记录或人格状态已变更，room=%s", message.chat_id)
        except LLMError as error:
            ctx.logger.warning("聊天总结失败：%s", error)
            try:
                ctx.reply(message, "暂时无法生成聊天总结，请稍后重试。", delivery_guard=guard)
            except SnapshotExpired:
                pass

    # ---- 群聊总结正文（手动 /总结群聊 与定时播报共用） ----

    def _group_summary(self, ctx, snapshot, rows, personality):
        """生成群聊总结并渲染成最终正文；人格只决定用词语气，结构/长度/底线不变。

        `ctx.llm` 是未包装人格的客户端：语气由 `ChatSummarizer(personality=...)` 里的总结
        专用包装注入，避免 PersonalityLLM 把人格的长度与排版要求设为最高优先级。
        生成期间沿用既有机制：快照或人格任一失效就取消（手动入口的发送守卫不变）。
        这里同时是手动 /总结群聊 与定时播报的公共路径，七天语料也在这条路径上统一取。
        """
        focus = daily_talkers(rows)
        week = self._week_corpus(snapshot, rows, focus)
        summarizer = ChatSummarizer(ctx.llm, chunk_chars=self.summarizer.chunk_chars,
                                    personality=personality)
        result = summarizer.summarize(snapshot, rows, focus=focus, week=week,
            current=lambda: self.members.snapshot_is_current(snapshot) and
                            self.members.personality_is_current(personality))
        return render_group_summary(snapshot, rows, result)

    def _week_corpus(self, snapshot, rows, focus):
        """各 focus 成员过去七天的发言，形如 {"P1": "语料文本"}；失败时退化为没有七天语料。

        窗口用带时区的 UTC 起点（现在往回 7×24 小时）；speaker 编号到 sender_id 的对应关系
        取自今天的 rows，因此不依赖 group_rows_since 的排序。没有 focus 时不去查库。
        """
        if not focus:
            return {}
        speakers = {}
        for row in rows:
            # 编号由快照按首次发言顺序分配，同一个人的最近一行最有代表性，直接覆盖。
            speakers[row.get("speaker")] = row.get("sender_id")
        since = week_since(datetime.now(timezone.utc))
        try:
            history = self.members.group_rows_since(snapshot.room, since)
        except MemberError:
            # 七天语料只是点评的加分项：取不到就退化成只依据今天写，不打断总结。
            return {}
        return build_week_corpus(focus, speakers, history)

    # ---- 定时播报（每晚 DAILY_SUMMARY_SCHEDULE） ----

    def _broadcast(self, ctx):
        """逐个已配置的群播报当天的群聊总结；单群失败不影响其它群。"""
        rooms = sorted(self.members.groups)
        if not rooms:
            raise RuntimeError("没有可播报的群或全部失败")
        failures = []
        for room in rooms:
            try:
                self._broadcast_group(ctx, room)
            except SnapshotExpired:
                ctx.logger.info("群聊总结播报已取消：记录或人格状态已变更，room=%s", room)
            except Exception as error:  # noqa: BLE001 - 单群失败不能让其它群一起哑掉
                ctx.logger.exception("群聊总结播报失败：%s", room)
                failures.append(error)
        # 一个群都没成功：把这次运行记成失败（配置问题应当看得见，不静默成功）。
        if failures and len(failures) == len(rooms):
            raise failures[-1]

    def _broadcast_group(self, ctx, room):
        snapshot = self.members.today_group_snapshot(room)
        rows = conversation_rows(snapshot)
        if not rows:
            ctx.logger.info("群聊总结播报跳过：当天没有可总结的记录，room=%s", room)
            return
        # 定时路径没有消息对象，本群当前人格直接按群号取。
        text = self._group_summary(ctx, snapshot, rows, self.members.personality(room))
        for part in split_text(text):
            if not ctx.send_text(room, part):
                # 原生发送未生效时返回 falsy 而不抛异常，与手动路径同口径记失败。
                raise RuntimeError("原生文本发送未成功")
        ctx.logger.info("群聊总结已播报：room=%s records=%d", room, len(rows))
