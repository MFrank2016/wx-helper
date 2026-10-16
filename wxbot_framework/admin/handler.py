"""把框架自身的日志旁路进管理台「系统」类型：WARNING 及以上才进日志页。

框架里「插件队列已满」「等待微信连接」这类日志走的是标准 `logging`，不属于任何插件；
这里用 `AdminLogHandler` 把它们接进 `Instruments.record_system`，在日志页显示成「系统」。
只负责转发，是否真正落库由统计库决定（未接入统计库时 `record_system` 是空实现）。
"""
from __future__ import annotations

import logging

# 管理台自己的 logger：`instrument.py` 用它报告「埋点写库失败」。
ADMIN_LOGGER_NAME = "Admin"


class _AdminSelfLogFilter(logging.Filter):
    """拦下管理台自身 logger 的记录，避免「记录系统出问题」再进记录系统。

    本 handler 挂在根 logger 上；`instrument.py` 写库失败时会用 `Admin` logger 打 WARNING，
    若不拦，这条 WARNING 会沿传播回到同一个 handler → 再次写库失败 → 再打 WARNING，
    递归放大（直到 emit 吞掉 RecursionError），库不可写时把每条日志放大成上百次尝试。
    """

    def filter(self, record):
        name = record.name
        return name != ADMIN_LOGGER_NAME and not name.startswith(ADMIN_LOGGER_NAME + ".")


class AdminLogHandler(logging.Handler):
    """把 WARNING 及以上的日志记录转发给管理台埋点。

    `emit()` 自己取 `record.getMessage()`，不依赖外部 `setFormatter`；
    格式化或埋点失败都在这里兜住，绝不把异常抛回日志调用链。
    """

    def __init__(self, instruments):
        super().__init__(level=logging.WARNING)
        self._instruments = instruments
        self.addFilter(_AdminSelfLogFilter())

    def emit(self, record):
        try:
            self._instruments.record_system(
                record.levelname, record.name, record.getMessage())
        except Exception:  # 埋点只是旁路，失败不能影响正常日志
            pass
