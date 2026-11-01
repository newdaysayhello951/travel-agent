"""真实LangChain图与中间件组合，模型/网络使用确定性替身。"""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool

from agents.planner import TripPlanner
from reliability import RunReport
from reliability import ExecutionBudget, current_budget, model_reliability, tool_reliability
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import SystemMessage
from test_reliability import plan
from schemas import TripRequest


class ScriptedModel(BaseChatModel):
    responses: list[AIMessage]
    observed_tool_ids: list[str] = []

    @property
    def _llm_type(self):return 'scripted-test-model'

    def bind_tools(self, tools, **kwargs):return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.observed_tool_ids=[m.tool_call_id for m in messages if isinstance(m,ToolMessage)]
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


class WorkflowIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_query_forces_final_without_changing_request(self):
        model=ScriptedModel(responses=[])
        request=ModelRequest(model=model,messages=[],tools=[{'type':'function'}],system_message=SystemMessage(content='Original rules'))
        token=current_budget.set(ExecutionBudget(cache_hits=3))
        try:
            handler=AsyncMock(return_value='done')
            result=await model_reliability(20).awrap_model_call(request,handler)
            changed=handler.call_args.args[0]
            self.assertEqual(changed.tools,[])
            self.assertEqual(changed.model_settings['tool_choice'],'none')
            self.assertIn('Original rules',changed.system_message.content)
            self.assertNotEqual(request.tools,[])
            self.assertEqual(result,'done')
            self.assertTrue(current_budget.get().finalization_forced)
            self.assertEqual(current_budget.get().model_calls,1)
        finally:current_budget.reset(token)

    async def test_model_budget_reserves_finalization_turn(self):
        request=ModelRequest(model=ScriptedModel(responses=[]),messages=[],tools=[{}])
        token=current_budget.set(ExecutionBudget(model_calls=18))
        try:
            handler=AsyncMock(return_value='done')
            await model_reliability(20).awrap_model_call(request,handler)
            self.assertEqual(handler.call_args.args[0].tools,[])
            self.assertEqual(current_budget.get().model_calls,19)
        finally:current_budget.reset(token)

    async def test_tool_budget_exhaustion_returns_error_then_finalizes(self):
        request=ModelRequest(model=ScriptedModel(responses=[]),messages=[],tools=[{}])
        token=current_budget.set(ExecutionBudget(attempts=24))
        try:
            tool_request=SimpleNamespace(tool_call={'id':'no-budget','name':'maps_weather','args':{}})
            handler=AsyncMock()
            result=await tool_reliability(1,0,24).awrap_tool_call(tool_request,handler)
            self.assertEqual(result.status,'error')
            handler.assert_not_awaited()
            model_handler=AsyncMock(return_value='final')
            await model_reliability(20,24).awrap_model_call(request,model_handler)
            self.assertEqual(model_handler.call_args.args[0].tools,[])
            self.assertEqual(current_budget.get().attempts,24)
        finally:current_budget.reset(token)

    async def test_noncompliant_model_cannot_restart_tool_loop(self):
        from unittest.mock import patch
        from reliability import ExecutionLimitError
        async def weather(city: str) -> str:return 'fixture forecast'
        tool=StructuredTool.from_function(coroutine=weather,name='maps_weather',description='Test weather')
        model=ScriptedModel(responses=[AIMessage(content='',tool_calls=[{'name':'maps_weather','args':{'city':'110000'},'id':str(i)}]) for i in range(5)])
        planner=TripPlanner(model);planner.mcp=SimpleNamespace(get_tools_for=AsyncMock(side_effect=[[tool],[],[]]))
        report=RunReport()
        with patch('agents.planner.CONFIG.max_tool_calls',1):
            with self.assertRaises(ExecutionLimitError) as caught:
                await planner.invoke('fixture',request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',stay_on_last_day=True),report=report)
        self.assertIn('收尾阶段',str(caught.exception))
        self.assertEqual(report.tool_attempts,1)
        self.assertEqual(report.model_calls,2)

    async def test_retry_cache_message_ids_and_model_count_in_real_graph(self):
        attempts=0
        async def weather(city: str) -> str:
            nonlocal attempts
            attempts+=1
            if attempts==1:raise TimeoutError('fixture timeout')
            return 'fixture forecast'
        tool=StructuredTool.from_function(coroutine=weather,name='maps_weather',description='Test weather')
        model=ScriptedModel(responses=[
            AIMessage(content='',tool_calls=[{'name':'maps_weather','args':{'city':'110000'},'id':'one'}]),
            AIMessage(content='',tool_calls=[{'name':'maps_weather','args':{'city':'110000'},'id':'two'}]),
            AIMessage(content=json.dumps(plan()))])
        planner=TripPlanner(model)
        planner.mcp=SimpleNamespace(get_tools_for=AsyncMock(side_effect=[[tool],[],[]]))
        report=RunReport()
        output=await planner.invoke('fixture',request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',stay_on_last_day=True),report=report)
        self.assertEqual(json.loads(output)['city'],'长沙')
        self.assertEqual(report.status,'valid_plan')
        self.assertEqual(report.model_calls,3)
        self.assertEqual(report.tool_attempts,2)
        self.assertEqual(report.cache_hits,1)
        self.assertEqual(model.observed_tool_ids,['one','two'])
