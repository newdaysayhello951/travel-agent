"""对照架构：只改变编排方式，复用同一校验、修正、配置和运行预算。"""
from langchain.agents import create_agent
from langchain_core.tools import StructuredTool

from agents.planner import TripPlanner
from config import CONFIG
from prompts import PLANNER_AGENT_PROMPT
from reliability import model_reliability, tool_reliability


DELEGATES = ('search_hotel','search_attraction','query_weather')


def delegate(agent):
    async def ask(query: str) -> str:
        result=await agent.ainvoke({'messages':[{'role':'user','content':query}]},
                                  config={'recursion_limit':CONFIG.recursion_limit})
        return result['messages'][-1].content
    return ask


class MultiTripPlanner(TripPlanner):
    """总控+领域子Agent；子调用不创建新ExecutionBudget或新的任务时限。"""

    async def build(self):
        if self._agent is not None:
            return
        groups={name:await self.mcp.get_tools_for(name) for name in ('poi','weather','route')}
        if not any(groups.values()):
            raise RuntimeError('No travel MCP tools available')
        children=[]
        for name,domain,task in [('search_hotel','poi','酒店位置及候选住宿'),
                                  ('search_attraction','poi','景点与用户偏好'),
                                  ('query_weather','weather','工具实际提供日期内的天气')]:
            child=create_agent(model=self.llm,tools=groups[domain],
                system_prompt=f'你只负责查询{task}。必须实际调用提供的工具，保留真实名称、POI标识、地址及经纬度。未知价格、评分、开放时间明确说明未知。日期超出预报范围则说明无法预测。只总结事实与缺项，不生成完整旅行行程，不编造工具结果。',
                middleware=[tool_reliability(CONFIG.tool_timeout,CONFIG.tool_retries,CONFIG.max_tool_calls),
                            model_reliability(CONFIG.max_model_calls,CONFIG.max_tool_calls,purpose='facts')])
            children.append(StructuredTool.from_function(coroutine=delegate(child),name=name,description=f'查询{task}，query为城市、日期和偏好等完整自然语言需求。'))
        prompt=PLANNER_AGENT_PROMPT
        start=prompt.index('## 你可以调用的工具')
        end=prompt.index('## 信息真实性')
        prompt=prompt[:start]+'''## 你可以调用的工具
- search_hotel：酒店领域查询，参数query
- search_attraction：景点领域查询，参数query
- query_weather：天气领域查询，参数query
- maps_direction_*：直接调用高德路线工具，参数遵循实际schema

'''+prompt[end:]
        prompt=prompt.replace('maps_weather','query_weather').replace('maps_text_search','search_attraction').replace('maps_search_detail','search_attraction')
        prompt=prompt.replace('2. 用 search_attraction 找酒店','2. 用 search_hotel 找酒店')
        prompt=prompt.replace('酒店和景点均直接使用 search_attraction 搜索，必要时用 search_attraction 查详情。','酒店调用search_hotel，景点调用search_attraction；子工具返回事实和坐标，路线由你查询。')
        self._agent=create_agent(model=self.llm,tools=[*children,*groups['route']],system_prompt=prompt,
            middleware=[tool_reliability(CONFIG.tool_timeout,CONFIG.tool_retries,CONFIG.max_tool_calls,delegated_names=DELEGATES),
                        model_reliability(CONFIG.max_model_calls,CONFIG.max_tool_calls)])


def create_planner(llm, architecture='single'):
    if architecture=='single':return TripPlanner(llm)
    if architecture=='multi':return MultiTripPlanner(llm)
    raise ValueError('architecture必须为single或multi')
