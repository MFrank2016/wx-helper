"""按连接、会话及微信原始消息编号补发撤回内容，不依赖网页监控。"""
from collections import OrderedDict
import re
import time

from wxbot_framework.recall import message_id
from wxbot_framework.media_recall import is_file_attachment
from wxbot_framework.summary import split_text

# 原生消息编号偶尔与撤回通知里的 newmsgid 对不上；同群、同发送者、最近 5 分钟内
# 只有一条未撤回原文时，按这条原文兜底。无法唯一确定时宁可不发，避免补错消息。
FALLBACK_SECONDS = 300
_RECALL_SENDER = re.compile(r'^\s*["“]([^"”]{1,64})["”]\s*撤回了一条消息')


class Plugin:
    def on_load(self, ctx):
        self.originals = OrderedDict()
        self.recalled = OrderedDict()

    @staticmethod
    def _put(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > 1000:
            cache.popitem(last=False)

    @staticmethod
    def _sender_names(message, ctx):
        """缓存群昵称、微信昵称等别名；撤回提示里用的是群昵称。"""
        names = []
        try:
            members = ctx.members
            names.extend((members.group_nickname(message.sender_id, message.chat_id),
                          members.wechat_name(message.sender_id)))
        except Exception:
            pass
        names.append(message.data.get('sender_wechat_name'))
        return tuple(dict.fromkeys(
            name.strip()[:64] for name in names
            if isinstance(name, str) and name.strip()
        ))

    @staticmethod
    def _recall_sender(notice):
        if not isinstance(notice, str):
            return ''
        match = _RECALL_SENDER.match(notice)
        return match.group(1).strip() if match else ''

    def _fallback_entry(self, message, recall):
        cutoff = time.monotonic() - FALLBACK_SECONDS
        candidates = [
            (key, entry) for key, entry in self.originals.items()
            if key[0] == message.session_id and key[1] == message.chat_id
            and entry['at'] >= cutoff and self.recalled.get(key) is not True
        ]
        if not candidates:
            return None
        sender = self._recall_sender(recall.get('notice'))
        if sender:
            matched = [item for item in candidates
                       if any(name.casefold() == sender.casefold()
                              for name in item[1]['senders'])]
            return matched[0] if len(matched) == 1 else None
        if not message.is_group:
            return max(candidates, key=lambda item: item[1]['at'])
        return None

    def _reply(self, key, entry, message, ctx, *, original_key=None):
        if not entry or self.recalled.get(key) is not False:
            return
        content, delegate = entry['content'], entry['delegate']
        # 在发送前消费通知；重复系统回调或媒体下载完成不会再次补发。
        self.recalled[key] = True
        if original_key is not None and original_key != key:
            self.recalled[original_key] = True
        if delegate and ctx.plugin_is_active(delegate):
            return
        for part in split_text('撤回的消息是：' + content, byte_limit=1023):
            if ctx.reply(message, part) is False:
                break

    def on_message(self, message, ctx):
        if not message.session_id or message.data.get('bot_generated'):
            return
        recall = message.data.get('recall')
        if message.type in (10000, 10002) and isinstance(recall, dict):
            identifier = message_id(recall.get('message_id'))
            if not identifier or recall.get('chat_id') != message.chat_id:
                return
            key = (message.session_id, message.chat_id, identifier)
            if key in self.recalled:
                return
            self._put(self.recalled, key, False)
            entry = self.originals.get(key)
            original_key = key
            if entry is None:
                fallback = self._fallback_entry(message, recall)
                if fallback is not None:
                    original_key, entry = fallback
                    ctx.logger.info('防撤回缓存未命中，按同发送者最近原文兜底：撤回ID=%s 原文ID=%s',
                                    identifier, original_key[2])
                else:
                    ctx.logger.info('防撤回缓存未命中：撤回ID=%s 缓存条数=%s',
                                    identifier, len(self.originals))
            self._reply(key, entry, message, ctx, original_key=original_key)
            return
        identifier = message_id(message.data.get('native_message_id'))
        if not identifier:
            return
        if message.type == 1:
            content = message.text
        elif message.type == 49:
            if message.data.get('app_type') == 19:
                content = '[合并转发]'
            else:
                name = str(message.data.get('file_name') or '').replace('\\', '/').rsplit('/', 1)[-1]
                content = '[文件] ' + name if name else '[文件/分享]'
        else:
            content = {3:'[图片]', 34:'[语音]', 43:'[视频]', 47:'[表情]', 62:'[视频]'}.get(message.type, '')
        if not isinstance(content, str) or not content or '\0' in content:
            return
        key = (message.session_id, message.chat_id, identifier)
        delegate = ('anti_recall_image' if message.type == 3 else
                    'anti_recall_file' if is_file_attachment(message) else '')
        entry = {'content': content[:2000], 'delegate': delegate,
                 'senders': self._sender_names(message, ctx), 'at': time.monotonic()}
        self._put(self.originals, key, entry)
        self._reply(key, entry, message, ctx, original_key=key)

    def on_unload(self, ctx):
        self.originals.clear()
        self.recalled.clear()
