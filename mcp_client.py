"""
MCP 客户端管理器 —— 单例模式，全局共享高德地图 MCP 连接。
"""
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import BaseTool
from config import CONFIG


class McpClientManager:
    """
    高德地图 MCP 客户端单例。
    这是单例模式
    保证整个程序永远只有一个 McpClientManager
    保证缓存只创建一次，不重复请求
    保证客户端不重复初始化

    职责：
      1. 管理与阿里百炼 MCP 服务器的连接
      2. 按领域（poi/weather/route）分发工具子集
      3. 缓存已加载工具，避免重复请求

    用法：
      manager = McpClientManager()
      poi_tools = await manager.get_tools_for("poi")
      route_tools = await manager.get_tools_for("route")
    """

    _instance: "McpClientManager | None" = None

    def __new__(cls) -> "McpClientManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._client: MultiServerMCPClient | None = None
        self._tools_cache: dict[str, list[BaseTool]] = {}
        self._initialized = True

    # ==================== 连接管理 ====================

    async def _get_client(self) -> MultiServerMCPClient:
        """懒加载 MCP 客户端"""
        if self._client is None:
            server_cfg: dict = {
                "transport": CONFIG.mcp_transport,
                "url": CONFIG.mcp_url,
            }
            # [适配3] 不同服务商的鉴权方式不同：
            #   阿里百炼托管版 → Authorization: Bearer <DASHSCOPE_API_KEY>
            #   高德官方版     → key 已在 URL 查询串里，不需要额外请求头
            headers = CONFIG.mcp_headers()
            if headers:
                server_cfg["headers"] = headers
            self._client = MultiServerMCPClient({"amap-server": server_cfg})
        return self._client

    # ==================== 工具获取 ====================

    async def get_all_tools(self) -> list[BaseTool]:
        """获取 MCP 服务器暴露的全部工具"""
        if "all" not in self._tools_cache:
            client = await self._get_client()
            self._tools_cache["all"] = await client.get_tools()
            # for t in self._tools_cache["all"]:
            #     print(f"  ✓ {t.name}: {t.description[:60]}...")
        return self._tools_cache["all"]

    async def get_tools_for(self, domain: str) -> list[BaseTool]:
        """按领域获取工具子集。

        先按 config 里的精确名字匹配；一个都没匹配到时，
        再用 tool_domain_prefixes 做前缀兜底 —— 不同 MCP 服务商的命名有差异。
        """
        all_tools = await self.get_all_tools()

        target_names = set(CONFIG.tool_domains.get(domain, []))
        matched = [t for t in all_tools if t.name in target_names]
        if matched:
            return matched

        # [适配5] 精确匹配失败 → 前缀兜底
        prefixes = tuple(CONFIG.tool_domain_prefixes.get(domain, []))
        if prefixes:
            matched = [t for t in all_tools if t.name.startswith(prefixes)]
        if matched:
            print(
                f"[MCP] 领域 {domain!r} 未命中精确名字，前缀兜底匹配到: "
                f"{[t.name for t in matched]}"
            )
            return matched

        # 一个都没匹配到 → 明确报出来，
        # 避免出现「子 Agent 手里没有任何工具」这种静默故障
        available = ", ".join(sorted(t.name for t in all_tools))
        print(
            f"[MCP] 警告: 领域 {domain!r} 没有匹配到任何工具！"
            f"服务端实际提供: {available or '(空)'}"
        )
        return []

    # ==================== 生命周期 ====================

    async def close(self):
        """关闭 MCP 连接（如有需要）"""
        self._client = None
        self._tools_cache.clear()

    @classmethod
    def reset(cls):
        """重置单例（测试用）"""
        cls._instance = None
