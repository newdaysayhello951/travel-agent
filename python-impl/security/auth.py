"""API Key 鉴权。

支持两种携带方式（任选其一）：
- `Authorization: Bearer <key>`
- `X-API-Key: <key>`

安全策略（见 config.Settings）：
- 配置了 `API_KEYS` → 强制校验；
- 未配置但只监听本机 → 放行，并在启动时打警告（保证本机演示可用）；
- 未配置且监听了非本机地址 → 启动直接失败，避免"裸奔对外"。
"""

from __future__ import annotations

import hashlib
import logging
import secrets

from fastapi import Header, HTTPException, status

from config import get_settings

logger = logging.getLogger(__name__)

ANONYMOUS = "anonymous"


def principal_id(api_key: str) -> str:
    """把 API Key 映射成不可逆的调用方标识，用于限流分桶与日志，避免明文入日志。"""
    return "key:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def is_authenticated(principal: str) -> bool:
    """判断调用方是否为已鉴权身份（ANONYMOUS 表示未启用鉴权的本机演示）。"""
    return principal != ANONYMOUS


def extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def require_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> str:
    """
    FastAPI 依赖：校验 API Key、按调用方限流，返回调用方标识。

    限流放在这里而不是各个接口里：**所有受鉴权保护的接口自动获得同一份配额**，
    不会出现"聊天接口有限流、工具接口没有"的缺口。

    未配置密钥且只监听本机时返回 ANONYMOUS（本机演示模式），同样受限流保护。
    """
    settings = get_settings()
    if not settings.auth_enforced:
        _enforce_rate_limit(ANONYMOUS)
        return ANONYMOUS

    provided = extract_key(authorization, x_api_key)
    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 API Key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 逐个常时比较，避免通过响应时间区分密钥前缀。
    for key in settings.api_key_list:
        if secrets.compare_digest(provided, key):
            principal = principal_id(key)
            _enforce_rate_limit(principal)
            return principal

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API Key 无效",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _enforce_rate_limit(principal: str) -> None:
    """超出配额时返回 429，并给出建议等待秒数"""
    from security.ratelimit import get_limiter

    allowed, wait_seconds = get_limiter().check(principal)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(max(1, int(wait_seconds + 0.999)))},
        )
