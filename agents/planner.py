"""
行程规划总控 Agent —— 持有子 Agent 作为工具，统一编排并流式输出最终行程。
"""
import re
from typing import AsyncIterator
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_core.language_models import BaseChatModel

from mcp_client import McpClientManager
from agents.specialist import SpecialistAgent
from prompts import (
    HOTEL_AGENT_PROMPT,
    ATTRACTION_AGENT_PROMPT,
    WEATHER_AGENT_PROMPT,
    PLANNER_AGENT_PROMPT,
)

# 工具名 → 用户友好的中文标签
# 路线工具在不同 MCP 服务商下命名不同（百炼托管版带 _by_address 后缀，
# 高德官方版不带），所以两种都登记，避免状态行显示成 "🔧 原始工具名"。
TOOL_LABELS = {
    "query_weather":     ("🌤️", "查询天气"),
    "search_hotel":      ("🏨", "搜索酒店"),
    "search_attraction": ("🏛️", "搜索景点"),
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
    """
    旅行规划总控智能体。

    架构:
      Planner (总控)
        ├── search_hotel      → HotelAgent      → MCP: maps_text_search
        ├── search_attraction → AttractionAgent → MCP: maps_text_search
        ├── query_weather     → WeatherAgent    → MCP: maps_weather
        └── maps_direction_*  → 直接 MCP 路线工具

    用法:
        planner = TripPlanner(llm, specialist_llm)
        await planner.build()

        # 非流式
        result = await planner.invoke("杭州3日游...")

        # 流式（逐 token 输出到控制台）
        async for token in planner.stream("杭州3日游..."):
            print(token, end="", flush=True)

    关于 specialist_llm:
        建议传入 CONFIG.create_llm(streaming=False) 得到的实例。
        子 Agent 只被当作「黑盒工具」整体调用，没人需要看它的中间 token；
        若它也开着流式，LangChain 会把子 Agent 的 token 事件一并上报给
        Planner 的 astream_events，导致子 Agent 的原始输出混进主输出流。
    """

    def __init__(
        self,
        llm: BaseChatModel,
        specialist_llm: BaseChatModel | None = None,
    ):
        self.llm = llm
        # [适配4] 子 Agent 专用 LLM；不传则退回复用总控 LLM（原版行为）
        self.specialist_llm = specialist_llm or llm
        self.mcp = McpClientManager()

        # 子 Agent（build 时初始化）
        self._hotel_agent: SpecialistAgent | None = None
        self._attraction_agent: SpecialistAgent | None = None
        self._weather_agent: SpecialistAgent | None = None

        # 顶层 Planner Agent
        self._agent = None

    # ==================== 构建 ====================

    async def build(self):
        """初始化所有子 Agent + 组装 Planner"""
        if self._agent is not None:
            return

        # 1. 按领域加载 MCP 工具
        poi_tools = await self.mcp.get_tools_for("poi")
        weather_tools = await self.mcp.get_tools_for("weather")
        route_tools = await self.mcp.get_tools_for("route")

        # 2. 创建子 Agent（用非流式 LLM，避免中间 token 泄漏）
        self._hotel_agent = SpecialistAgent(
            self.specialist_llm, "HotelAgent", HOTEL_AGENT_PROMPT, poi_tools
        )
        self._attraction_agent = SpecialistAgent(
            self.specialist_llm, "AttractionAgent", ATTRACTION_AGENT_PROMPT, poi_tools
        )
        self._weather_agent = SpecialistAgent(
            self.specialist_llm, "WeatherAgent", WEATHER_AGENT_PROMPT, weather_tools
        )
        await self._hotel_agent.build()
        await self._attraction_agent.build()
        await self._weather_agent.build()

        # 3. 将子 Agent 包装为 Tool
        @tool
        async def search_hotel(query: str) -> str:
            """搜索酒店。输入城市+偏好，返回酒店列表。"""
            return await self._hotel_agent.invoke(query)

        @tool
        async def search_attraction(query: str) -> str:
            """搜索景点。输入城市+类型偏好，返回景点列表。"""
            return await self._attraction_agent.invoke(query)

        @tool
        async def query_weather(query: str) -> str:
            """查询天气。输入城市+日期，返回天气概况。"""
            return await self._weather_agent.invoke(query)

        # 4. 组装 Planner：子 Agent 工具 + 路线 MCP 工具
        all_tools = [search_hotel, search_attraction, query_weather, *route_tools]

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
                    if emit.strip():
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
