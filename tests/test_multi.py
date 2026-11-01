import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from agents.multi import MultiTripPlanner,create_planner
from agents.planner import TripPlanner
from reliability import RunReport
from schemas import TripRequest
from test_reliability import plan
from test_workflow_integration import ScriptedModel


class MultiTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_uses_same_model_and_actual_tool_budget(self):
        async def search(keywords: str) -> str:return 'fixture POI coordinates'
        poi=StructuredTool.from_function(coroutine=search,name='maps_text_search',description='Fixture POI')
        model=ScriptedModel(responses=[
            AIMessage(content='',tool_calls=[{'name':'search_attraction','args':{'query':'fixture'},'id':'delegate'}]),
            AIMessage(content='',tool_calls=[{'name':'maps_text_search','args':{'keywords':'fixture'},'id':'external'}]),
            AIMessage(content='fixture POI facts'),AIMessage(content=json.dumps(plan()))])
        planner=MultiTripPlanner(model)
        planner.mcp=SimpleNamespace(get_tools_for=AsyncMock(side_effect=[[poi],[],[]]))
        report=RunReport()
        text=await planner.invoke('fixture',request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',stay_on_last_day=True),report=report)
        self.assertEqual(json.loads(text)['city'],'长沙')
        self.assertEqual(report.model_calls,4)
        self.assertEqual(report.tool_attempts,1)  # 委派不是外部网络查询
        self.assertEqual(report.status,'valid_plan')

    def test_default_stays_single_and_unknown_architecture_rejected(self):
        self.assertIs(type(create_planner(object())),TripPlanner)
        with self.assertRaises(ValueError):create_planner(object(),'unknown')
