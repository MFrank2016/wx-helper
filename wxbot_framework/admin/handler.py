"""把框架自身的日志旁路进管理台「系统」类型：WARNING 及以上才进日志页。

框架里「插件队列已满」「等待微信连接」这类日志走的是标准 `logging`，不属于任何插件；
这里用 `AdminLogHandler` 把它们接进 `Instruments.record_system`，在日志页显示成「系统」。
只负责转发，是否真正落库由统计库决定（未接入统计库时 `record_system` 是空实现）。
"""
from __future__ import annotations

import logging


class AdminLogHandler(logging.Handler):
    """把 WARNING 及以上的日志记录转发给管理台埋点。

    `emit()` 自己取 `record.getMessage()`，不依赖外部 `setFormatter`；
    格式化或埋点失败都在这里兜住，绝不把异常抛回日志调用链。
    """

    def __init__(self, instruments):
        super().__init__(level=logging.WARNING)
        self._instruments = instruments

    def emit(self, record):
        try:
            self._instruments.record_system(
                record.levelname, record.name, record.getMessage())
        except Exception:  # 埋点只是旁路，失败不能影响正常日志
            pass
