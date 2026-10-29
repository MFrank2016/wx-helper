"""按连接、会话及微信原始消息编号补发撤回内容，不依赖网页监控。"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import tempfile
import threading
import time

from wxbot_framework.recall import message_id
from wxbot_framework.media_recall import is_file_attachment
from wxbot_framework.summary import split_text
from .stickers import StickerCache, StickerError, parse_sticker


@dataclass
class Original:
    content: str
    delegate: str = ''
    sticker_key: str = ''
    pending: bool = False
    message: object = field(default=None, repr=False)
    senders: tuple = ()
    at: float = 0.0
    sticker_error: str = ''


# 原生消息编号偶尔与撤回通知里的 newmsgid 对不上；同群、同发送者、最近 5 分钟内
# 只有一条未撤回原文时，按这条原文兜底。无法唯一确定时宁可不发，避免补错消息。
FALLBACK_SECONDS = 300
_RECALL_SENDER = re.compile(r'^\s*["“]([^"”]{1,64})["”]\s*撤回了一条消息')


class Plugin:
    def on_load(self, ctx):
        self.originals = OrderedDict()
        self.recalled = OrderedDict()
        self._lock = threading.RLock()
        self._active = True
        self.stickers = StickerCache(lambda session: self._active and ctx._service.session_is_current(session),
                                     lambda key, entry, ready, reason='':
                                     self._sticker_ready(key, entry, ready, ctx, reason))

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
        with self._lock:
            candidates = [
                (key, entry) for key, entry in self.originals.items()
                if key[0] == message.session_id and key[1] == message.chat_id
                and entry.at >= cutoff and not entry.pending
                and self.recalled.get(key) is not True
            ]
        if not candidates:
            return None
        sender = self._recall_sender(recall.get('notice'))
        if sender:
            matched = [item for item in candidates
                       if any(name.casefold() == sender.casefold()
                              for name in item[1].senders)]
            return matched[0] if len(matched) == 1 else None
        return None

    def _reply(self, key, entry, message, ctx, *, original_key=None):
        with self._lock:
            if not self._active or not entry or entry.pending or self.recalled.get(key) is not False:
                return
            # 在发送前消费通知；重复系统回调或媒体下载完成不会再次补发。
            self.recalled[key] = True
            if original_key is not None and original_key != key:
                self.recalled[original_key] = True
            data = self.stickers.get(entry.sticker_key) if entry.sticker_key else None
        if entry.delegate and ctx.plugin_is_active(entry.delegate):
            return
        if data is not None:
            # 内存缓存不依赖微信文件是否被撤回清理；临时 GIF 只活到原生发送返回。
            try:
                with tempfile.TemporaryDirectory(prefix='helper-recall-sticker-send-') as temporary:
                    path = Path(temporary) / 'sticker.gif'
                    path.write_bytes(data)
                    if ctx.reply_emoticon(message, path, delivery_guard=self._delivery_guard) is False:
                        ctx.logger.warning('撤回表情包补发未获确认，不重复发送')
            except (OSError, ValueError, RuntimeError):
                ctx.logger.warning('撤回表情包补发未完成，不重复发送')
            return
        reason = entry.sticker_error or ('缓存不可用' if entry.sticker_key else '')
        if reason:
            ctx.logger.warning('撤回表情未能重发，退回文字：%s', reason)
        for part in split_text(self.format_recall(entry.content), byte_limit=1023):
            if ctx.reply(message, part, delivery_guard=self._delivery_guard) is False:
                break

    def format_recall(self, content):
        """content 是被撤回的原文，返回要补发的文字。"""
        return '撤回的消息是：' + content

    @contextmanager
    def _delivery_guard(self):
        with self._lock:
            if not self._active:
                raise RuntimeError('防撤回插件已停止')
            yield

    def _sticker_ready(self, key, entry, ready, ctx, reason=''):
        with self._lock:
            if not self._active or self.originals.get(key) is not entry:
                return
            entry.pending = False
            if not ready:
                detail = reason or '表情缓存不可用'
                if entry.sticker_key:
                    detail += f'（md5={entry.sticker_key}）'
                entry.sticker_key = ''
                entry.sticker_error = detail
            else:
                entry.sticker_error = ''
        # 仅在已经收到撤回通知时补发；普通收表情只缓存，不回复。
        self._reply(key, entry, entry.message, ctx)

    def on_message(self, message, ctx):
        if not message.session_id or message.data.get('bot_generated'):
            return
        recall = message.data.get('recall')
        if message.type in (10000, 10002) and isinstance(recall, dict):
            identifier = message_id(recall.get('message_id'))
            if not identifier or recall.get('chat_id') != message.chat_id:
                return
            key = (message.session_id, message.chat_id, identifier)
            with self._lock:
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
                    'anti_recall_file' if is_file_attachment(message) else
                    'anti_recall_voice' if message.type == 34 else '')
        entry = Original(content[:2000], delegate,
                         senders=self._sender_names(message, ctx),
                         at=time.monotonic())
        sticker = None
        if message.type == 47:
            if 'emoticon.send' not in ctx.capabilities:
                entry.sticker_error = '当前后端不支持表情发送'
            else:
                try:
                    sticker = parse_sticker(message.data.get('raw_content') or message.data.get('content') or
                                            message.data.get('msg') or '')
                except StickerError as error:
                    entry.sticker_error = '表情元数据解析失败：' + str(error)
                if sticker is not None:
                    entry.sticker_key, entry.pending = sticker.md5, True
                    entry.message = replace(message, data={}, text='')
        with self._lock:
            if not self._active or self.recalled.get(key) is True or (message.type == 47 and key in self.originals):
                return
            self._put(self.originals, key, entry)
        if sticker is not None and not self.stickers.submit(key, entry, sticker):
            entry.pending, entry.sticker_key = False, ''
            entry.sticker_error = '表情下载队列已满或缓存已停止'
        self._reply(key, entry, message, ctx, original_key=key)

    def on_unload(self, ctx):
        with self._lock:
            self._active = False
            self.originals.clear()
            self.recalled.clear()
        self.stickers.close()
