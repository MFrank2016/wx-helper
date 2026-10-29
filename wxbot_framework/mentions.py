"""真实群艾特名单来自微信 msgsource，不凭可见昵称猜测目标。"""
import re
from xml.etree import ElementTree

from .account import validate_account
from .text import WXID


def parse_at_members(source):
    if not isinstance(source, str) or len(source) > 4095 or "<!DOCTYPE" in source.upper() or "<!ENTITY" in source.upper():
        return ()
    try:
        root = ElementTree.fromstring(source)
    except (ElementTree.ParseError, ValueError):
        return ()
    node = root if root.tag == "atuserlist" else root.find(".//atuserlist")
    if node is None or not node.text:
        return ()
    result = []
    for value in node.text.split(","):
        value = value.strip()
        try:
            validate_account(value)
        except ValueError:
            continue
        if value not in result:
            result.append(value)
    return tuple(result)


def _appmsg_node(content):
    if not isinstance(content, str) or not content or len(content) > 32768:
        return None
    if re.search(r'<!\s*(DOCTYPE|ENTITY)', content, re.I):
        return None
    value = content.strip()
    prefix, separator, body = value.partition(':\n')
    if separator and WXID.fullmatch(prefix):
        value = body.strip()
    try:
        root = ElementTree.fromstring(value)
    except (ElementTree.ParseError, ValueError):
        return None
    return root if root.tag == 'appmsg' else root.find('.//appmsg')


def appmsg_text(content):
    """提取 appmsg 的 title/des，供引用回复里的艾特文本使用。"""
    node = _appmsg_node(content)
    if node is None:
        return ''
    for tag in ('title', 'des'):
        child = node.find(tag)
        if child is None:
            continue
        text = ''.join(child.itertext()).strip()
        if text:
            return text[:2000]
    return ''


def appmsg_quote(content):
    """提取引用消息的原文和显示名；没有可读引用时返回空对象。"""
    node = _appmsg_node(content)
    if node is None:
        return {}
    refer = node.find('refermsg')
    if refer is None:
        return {}

    def scalar(tag):
        child = refer.find(tag)
        return ''.join(child.itertext()).strip() if child is not None else ''

    text = scalar('content')[:2000]
    if not text:
        return {}
    sender = scalar('displayname')[:64] or scalar('chatusr')[:64] or scalar('fromusr')[:64]
    return {'text': text, 'sender': sender}


def strip_at_labels(text):
    return re.sub(r"@[^@\r\n\u2005]+\u2005", "", text).strip()
