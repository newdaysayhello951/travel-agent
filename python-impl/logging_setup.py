"""日志初始化。

uvicorn 默认只配置自己的 logger，应用侧 `logger.info(...)` 会因为根 logger 级别是
WARNING 而**静默消失**——本模块负责统一配置，让应用日志真正可见。
"""

from __future__ import annotations

import logging
import sys

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_configured = False


def configure_logging(level: str = "INFO") -> None:
    """
    配置根 logger，让应用日志输出到标准输出。

    幂等：重复调用不会叠加 handler。
    """
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.addHandler(handler)

    # 这些库在 INFO 级别过于聒噪，压到 WARNING
    for noisy in ("httpx", "httpcore", "openai", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    logging.getLogger(__name__).debug("日志已初始化，级别=%s", level.upper())
