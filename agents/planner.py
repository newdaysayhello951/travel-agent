"""
单个旅行规划 Agent，直接调用高德 MCP 工具并流式输出行程。
"""
import re
from typing import AsyncIterator
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel

from mcp_client import McpClientManager
from prompts import PLANNER_AGENT_PROMPT

# 工具名 → 用户友好的中文标签
# 路线工具在不同 MCP 服务商下命名不同（百炼托管版带 _by_address 后缀，
# 高德官方版不带），所以两种都登记，避免状态行显示成 "🔧 原始工具名"。
TOOL_LABELS = {
    "maps_weather":     ("🌤️", "查询天气"),
    "maps_search_detail": ("📍", "查询地点详情"),
    "maps_text_search": ("🔎", "搜索景点或酒店"),
    "maps_direction_walking_by_address":             ("🚶", "规划步行路线"),
    "maps_direction_walking":                        ("🚶", "规划步行路线"),
    "maps_direction_driving_by_address":             ("🚗", "规划驾车路线"),
    "maps_direction_driving":                        ("🚗", "规划驾车路线"),
    "maps_direction_transit_integrated_by_address":  ("🚌", "规划公交路线"),
    "maps_direction_transit_integrated":             ("🚌", "规划公交路线"),
    "maps_direction_bicycling":                      ("🚲", "规划骑行路线"),
}

# 匹配子 Agent 内部泄漏的 [TOOL_CALL:...] 模式
# 注意 [^\]\n]* 不允许跨行，避免标记没闭合时把后面的 JSON 一起吃掉
_TOOL_CALL_START = "[TOOL_CALL"
_TOOL_CALL_PATTERN = re.compile(r"\[TOOL_CALL:[^\]\n]*\]?")


def _split_leak(pending: str) -> tuple[str, str]:
    """从流式缓冲里剔除 [TOOL_CALL:...] 噪声。

    返回 (可以安全输出的文本, 需要继续留在缓冲里的尾巴)。

    【为什么需要缓冲 —— 原始版本的 bug】
    流式输出是按 chunk 到达的，一个标记很容易被切开，例如
    '[TO' + 'OL_CALL:amap_maps_weather...' 分属两个 chunk。
    原始实现是「对每个 chunk 单独做一次正则替换」，
    于是跨 chunk 的标记永远匹配不到，噪声照样漏给用户。
    修法：先把完整标记替换掉，再把「可能是标记前缀」的尾巴扣住，
    等后续 chunk 补齐后再判断。
    """
    pending = _TOOL_CALL_PATTERN.sub("", pending)
    for k in range(1, min(len(_TOOL_CALL_START), len(pending)) + 1):
        if _TOOL_CALL_START.startswith(pending[-k:]):
            return pending[:-k], pending[-k:]
    return pending, ""


class TripPlanner:
    """单个旅行 Agent，直接使用按领域筛选的 MCP 工具。"""

    def __init__(self, llm: BaseChatModel):
        self.llm = llm
        self.mcp = McpClientManager()
        self._agent = None

    # ==================== 构建 ====================

    async def build(self):
        """加载并去重旅行工具，只创建一个 Agent。"""
        if self._agent is not None:
            return

        all_tools = []
        seen = set()
        for domain in ("poi", "weather", "route"):
            for tool in await self.mcp.get_tools_for(domain):
                if tool.name not in seen:
                    seen.add(tool.name)
                    all_tools.append(tool)
        if not all_tools:
            raise RuntimeError("No travel MCP tools available")

        self._agent = create_agent(
            model=self.llm,
            tools=all_tools,
            system_prompt=PLANNER_AGENT_PROMPT,
        )

    # ==================== 非流式调用 ====================

    async def invoke(self, user_input: str) -> str:
        """输入自然语言需求，返回完整旅行计划"""
        await self.build()
        result = await self._agent.ainvoke({
            "messages": [{"role": "user", "content": user_input}]
        })
        return result["messages"][-1].content

    # ==================== 流式调用 ====================

    async def stream(self, user_input: str) -> AsyncIterator[str]:
        """
        流式输出旅行计划。

        逐 token yield 文本 + 工具调用状态标记:
          - 普通 token: 直接 yield（过滤掉子 Agent 内部 TOOL_CALL 泄漏）
          - 工具开始:   yield "\\n{emoji} {label}...\\n"
          - 工具结束:   静默（避免刷屏）
        """
        await self.build()

        pending = ""
        async for event in self._agent.astream_events(
            {"messages": [{"role": "user", "content": user_input}]},
            version="v2",
        ):
            kind = event.get("event", "")

            if kind == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
                    # 先入缓冲再切分，跨 chunk 的 [TOOL_CALL:...] 才不会漏网
                    emit, pending = _split_leak(pending + content)
                    if emit:
                        yield emit

            elif kind == "on_tool_start":
                name = event.get("name", "unknown")
                emoji, label = TOOL_LABELS.get(name, ("🔧", name))
                yield f"\n{emoji} {label}...\n"

            elif kind == "on_tool_end":
                name = event.get("name", "unknown")
                if name in TOOL_LABELS:
                    pass  # 静默处理，避免刷屏

        # 收尾：把缓冲里剩余的内容吐出去
        if pending:
            yield pending
