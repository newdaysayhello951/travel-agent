"""按统一配置创建模型实例。不复制、不输出密钥。"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from config import ROOT, get_settings

__all__ = ["ROOT", "create_llm"]


def create_llm() -> ChatOpenAI:
    """
    创建模型客户端。

    配置缺失时抛 RuntimeError，让服务在启动阶段失败，而不是等请求进来才返回 502。
    """
    settings = get_settings()

    missing = settings.missing_llm_fields()
    if missing:
        raise RuntimeError(
            "缺少必需的模型配置：" + "、".join(missing)
            + "。请检查根目录 .env（可参考 .env.example）"
        )

    # 默认关闭模型的思考模式，降低延迟与 token 消耗。
    extra_body = None if settings.llm_enable_thinking else {"enable_thinking": False}

    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        temperature=0,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        extra_body=extra_body,
    )
