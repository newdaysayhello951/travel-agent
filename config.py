"""
配置中心 —— 统一管理环境变量、LLM 实例、MCP 连接参数。

本文件在原始版本基础上做了「提供方可切换」改造，改动点都用 [适配] 标注：

  [适配1] llm_provider 开关
          原版只支持「阿里百炼 DashScope」：一个 Key 同时驱动 qwen 模型 + 高德 MCP。
          改造后支持第二种：任意 OpenAI 兼容网关（DeepSeek / 校园网关 / vLLM ...）。
          默认仍是 dashscope，保证原版行为不变。

  [适配2] mcp_url 可配置
          高德官方自己也有 MCP Server，地址形如 https://mcp.amap.com/mcp?key=xxx，
          工具名与百炼托管版基本一致。这样即使没有百炼 Key 也能跑通。

  [适配3] mcp_headers()
          百炼托管的 MCP 用 Authorization: Bearer <key> 鉴权；
          高德官方 MCP 的 key 直接放在 URL 查询串里，不需要额外请求头。
"""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# ========== 修复 langchain_community ChatTongyi 流式 tool_calls 的 KeyError ==========
# 上游 bug: subtract_client_response 访问 prev_function["name"] / ["arguments"]
# 前没有检查 key 是否存在。流式首个 tool_call chunk 可能不含这些 key。
#
# [适配] 这段补丁只对 ChatTongyi 生效，所以仅在 llm_provider == "dashscope" 时才应用。


def _patched_subtract(self, resp, prev_resp):
    import json

    resp_copy = json.loads(json.dumps(resp))
    message = resp_copy["output"]["choices"][0]["message"]
    prev_message = json.loads(json.dumps(prev_resp))["output"]["choices"][0]["message"]

    message["content"] = message["content"].replace(
        prev_message.get("content", "") or "", ""
    )

    if message.get("tool_calls") and prev_message.get("tool_calls"):
        for index, tool_call in enumerate(message["tool_calls"]):
            function = tool_call["function"]
            prev_function = prev_message["tool_calls"][index]["function"]

            if "name" in function and "name" in prev_function:
                function["name"] = function["name"].replace(prev_function["name"], "")
            if "arguments" in function and "arguments" in prev_function:
                function["arguments"] = function["arguments"].replace(
                    prev_function["arguments"], ""
                )

    return resp_copy


# 提供方常量
PROVIDER_DASHSCOPE = "dashscope"           # 原版：阿里百炼
PROVIDER_OPENAI = "openai_compatible"      # 任意 OpenAI 兼容网关

_LLM_PROVIDER = os.getenv("LLM_PROVIDER", PROVIDER_DASHSCOPE)

if _LLM_PROVIDER == PROVIDER_DASHSCOPE:
    from langchain_community.chat_models.tongyi import ChatTongyi

    ChatTongyi.subtract_client_response = _patched_subtract
# ========== 修复结束 ==========


@dataclass
class Config:
    """全局配置，单例语义 —— 模块级 CONFIG 实例"""

    # ---------- [适配1] 大模型提供方 ----------
    # "dashscope"         → 阿里百炼 ChatTongyi（原版）
    # "openai_compatible" → 任意 OpenAI 兼容网关（DeepSeek / 校园网关 / vLLM ...）
    llm_provider: str = field(
        default_factory=lambda: os.getenv("LLM_PROVIDER", PROVIDER_DASHSCOPE)
    )

    # ---------- A. 阿里百炼 DashScope（原版路径） ----------
    api_key: str = field(
        default_factory=lambda: os.getenv("DASHSCOPE_API_KEY", "")
    )
    dashscope_model: str = "qwen3-max"

    # ---------- B. OpenAI 兼容网关（本机路径） ----------
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", ""))

    # 公共生成参数
    temperature: float = 0.7

    # ---------- [适配2] MCP 连接 ----------
    mcp_transport: str = "http"
    mcp_url: str = field(
        default_factory=lambda: os.getenv("AMAP_MCP_URL")
        or "https://dashscope.aliyuncs.com/api/v1/mcps/amap-maps/mcp"
    )

    # ---------- [适配6] 思考模式开关 ----------
    # 实测本机网关上的 Ali-dashscope/Qwen3.5-Flash 是「思考型」模型：
    # 每次回答前都先生成大量 reasoning token，且思考期间**不输出任何可见内容**。
    # 实测数据（.tools/bench.py）：
    #   "回复两个字：收到"           → 224 completion token，其中 218 是 reasoning
    #   流式「数到30」首 token 延迟   → 42.0 秒（就是等它思考完）
    #   同一个请求关掉思考            → 首 token 2.4 秒，总耗时 4.2 秒
    # 所以关掉思考能带来约 10 倍加速。默认关掉（false）；
    # 若某个模型不支持该参数，把它设回 true 即可。
    llm_enable_thinking: bool = field(
        default_factory=lambda: os.getenv("LLM_ENABLE_THINKING", "false").strip().lower()
        in ("1", "true", "yes", "on")
    )

    # 工具领域映射：把 MCP 暴露的工具按功能分组，让每个子 Agent 只拿到自己领域的工具
    tool_domains: dict = field(default_factory=lambda: {
        "poi":     ["maps_text_search", "maps_search_detail"],
        "weather": ["maps_weather"],
        "route":   [
            # 高德官方 MCP（mcp.amap.com）实际命名
            "maps_direction_walking",
            "maps_direction_driving",
            "maps_direction_transit_integrated",
            "maps_direction_bicycling",
            # 阿里百炼托管版命名（保留兼容）
            "maps_direction_walking_by_address",
            "maps_direction_driving_by_address",
            "maps_direction_transit_integrated_by_address",
        ],
    })

    # [适配5] 工具名兜底前缀。
    # 不同 MCP 服务商的工具命名并不完全一致，最典型的是路线工具：
    #   - 阿里百炼托管版：maps_direction_walking_by_address
    #   - 高德官方 MCP  ：maps_direction_walking
    # 只做精确匹配的话，换服务商后 route 领域会匹配到空列表，
    # Planner 会静默地拿不到任何路线工具（不报错，只是功能缺一块）。
    # 所以精确匹配失败时再用前缀兜底匹配一次。
    tool_domain_prefixes: dict = field(default_factory=lambda: {
        "poi":     ["maps_text_search", "maps_search_detail", "maps_around_search"],
        "weather": ["maps_weather"],
        "route":   ["maps_direction_"],
    })

    # 自动检查初始化
    def __post_init__(self):
        if self.llm_provider == PROVIDER_DASHSCOPE:
            if not self.api_key:
                raise ValueError(
                    "请配置 DASHSCOPE_API_KEY（或在 .env 里设置 "
                    "LLM_PROVIDER=openai_compatible 改用兼容网关）"
                )
        elif self.llm_provider == PROVIDER_OPENAI:
            missing = [
                name
                for name, value in (
                    ("LLM_BASE_URL", self.llm_base_url),
                    ("LLM_API_KEY", self.llm_api_key),
                    ("LLM_MODEL", self.llm_model),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"请配置 {'、'.join(missing)}")
        else:
            raise ValueError(
                f"未知的 llm_provider: {self.llm_provider!r}，"
                f"可选 {PROVIDER_DASHSCOPE!r} 或 {PROVIDER_OPENAI!r}"
            )

        if "在此填入" in self.mcp_url:
            raise ValueError("AMAP_MCP_URL 还是占位符，请在 .env 里填入真实的高德 Key")

    # 当前生效的模型名
    @property
    def model_name(self) -> str:
        if self.llm_provider == PROVIDER_DASHSCOPE:
            return self.dashscope_model
        return self.llm_model

    # [适配3] MCP 请求头：百炼托管版要 Bearer，高德官方版不要
    def mcp_headers(self) -> dict:
        if "dashscope.aliyuncs.com" in self.mcp_url:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    # 创建模型实例对象
    def create_llm(self, streaming: bool = True):
        """按提供方创建模型，页面使用流式，评估可使用非流式。"""
        if self.llm_provider == PROVIDER_DASHSCOPE:
            return ChatTongyi(
                model=self.dashscope_model,
                api_key=self.api_key,
                temperature=self.temperature,
                streaming=streaming,
            )

        # ---------- [适配1] OpenAI 兼容网关 ----------
        # 延迟导入：不走这条路径时不必加载 langchain_openai
        from langchain_openai import ChatOpenAI

        # [适配6] 关掉「思考模式」以大幅降低延迟，见字段处的实测数据。
        # 通过 extra_body 透传给上游（DashScope 的 Qwen3 系列认这个字段）。
        extra_body = {} if self.llm_enable_thinking else {"enable_thinking": False}

        return ChatOpenAI(
            model=self.llm_model,
            api_key=self.llm_api_key,
            base_url=self.llm_base_url,
            temperature=self.temperature,
            streaming=streaming,
            extra_body=extra_body or None,
        )


CONFIG = Config()
