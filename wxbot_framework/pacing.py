"""文本发送前的可配置随机等待；仅控制节奏，不保证账号风控结果。"""
from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass

logger = logging.getLogger("Pacing")

# 阈值与默认值集中写在一处，配置缺项时回落到这里。
# 30 字符以内视为短回复：交互式命令的确认语（如「已绑定」）本就不需要拟人化等待，
# 而按长度分段累加会让长帮助文本（如 /bga帮助 约 172 字符）等十几秒。
DEFAULT_SHORT_TEXT_MAX_CHARS = 30
# 长文本只抽一次随机等待；默认区间 0.5–2.0 秒保留拟人化节奏，又不随时长线性放大。
DEFAULT_MIN_SECONDS = 0.5
DEFAULT_MAX_SECONDS = 2.0


@dataclass(frozen=True)
class TextSendDelay:
    enabled: bool = True
    short_text_max_chars: int = DEFAULT_SHORT_TEXT_MAX_CHARS
    min_seconds: float = DEFAULT_MIN_SECONDS
    max_seconds: float = DEFAULT_MAX_SECONDS

    @classmethod
    def from_config(cls, options=None):
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise ValueError("text_send_delay 必须是 JSON 对象")
        for key in options:
            if key not in ("enabled", "short_text_max_chars", "min_seconds", "max_seconds"):
                # 旧配置可能残留已删除的 characters_per_group；只提示，不打断已有配置。
                logger.warning("text_send_delay.%s 已废弃或未知，将被忽略", key)
        result = cls(enabled=options.get("enabled", True),
                     short_text_max_chars=options.get("short_text_max_chars", DEFAULT_SHORT_TEXT_MAX_CHARS),
                     min_seconds=options.get("min_seconds", DEFAULT_MIN_SECONDS),
                     max_seconds=options.get("max_seconds", DEFAULT_MAX_SECONDS))
        if type(result.enabled) is not bool:
            raise ValueError("text_send_delay.enabled 必须是布尔值")
        if type(result.short_text_max_chars) is not int or not 0 <= result.short_text_max_chars <= 10000:
            raise ValueError("text_send_delay.short_text_max_chars 应为 0–10000 的整数")
        for value in (result.min_seconds, result.max_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 60:
                raise ValueError("text_send_delay 的等待秒数应为 0–60 的有限数值")
        if result.min_seconds > result.max_seconds:
            raise ValueError("text_send_delay.min_seconds 不能大于 max_seconds")
        return result

    def seconds_for(self, text):
        if not self.enabled:
            return 0.0
        # 按 Unicode 字符计数；短回复直接跳过等待，不拖慢交互式问答。
        if len(text) <= self.short_text_max_chars:
            return 0.0
        # 长文本单次随机取值，不再按长度分段累加。
        return random.uniform(self.min_seconds, self.max_seconds)
