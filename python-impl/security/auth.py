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
    FastAPI 依赖：校验 API Key，返回调用方标识。

    未配置密钥且只监听本机时返回 ANONYMOUS（本机演示模式）。
    """
    settings = get_settings()
    if not settings.auth_enforced:
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
            return principal_id(key)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API Key 无效",
        headers={"WWW-Authenticate": "Bearer"},
    )
