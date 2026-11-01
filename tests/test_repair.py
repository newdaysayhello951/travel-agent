import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents.planner import TripPlanner
from reliability import ExecutionBudget, ExecutionLimitError, PlanningServiceError, RunReport, current_budget, transient_error, call_with_retry
from schemas import PlanValidationError, TripRequest, validate_plan_text
from test_reliability import plan


def response(data):
    return SimpleNamespace(content=json.dumps(data))


class RepairTests(unittest.IsolatedAsyncioTestCase):
    def planner(self, draft, repaired):
        llm = SimpleNamespace(ainvoke=AsyncMock(return_value=response(repaired)))
        planner = TripPlanner(llm)
        planner._agent = SimpleNamespace(ainvoke=AsyncMock(return_value={'messages':[response(draft)]}))
        return planner

    async def test_valid_initial_never_repairs(self):
        planner=self.planner(plan(),plan());report=RunReport()
        await planner.invoke('test',report=report)
        planner.llm.ainvoke.assert_not_awaited()
        self.assertEqual(report.status,'valid_plan')

    async def test_field_diagnostics_and_successful_repair(self):
        broken=plan();broken['budget']['total']=0;broken['budget']['total_hotels']=0
        with self.assertRaises(PlanValidationError) as caught:validate_plan_text(json.dumps(broken))
        self.assertEqual({i['path'] for i in caught.exception.issues},{'budget.total','budget.total_hotels'})
        broken['days'][0]['day_index']=2
        planner=self.planner(broken,plan());report=RunReport()
        result=await planner.invoke('test',report=report)
        self.assertEqual(json.loads(result)['budget']['total'],310)
        self.assertEqual([a['status'] for a in report.attempts],['invalid','valid_plan'])
        planner.llm.ainvoke.assert_awaited_once()
        self.assertEqual(report.model_calls,1)  # 模拟初始Agent不经过真实中间件
        self.assertIsNone(current_budget.get())

    async def test_repair_exhaustion_preserves_final_diagnostics(self):
        broken=plan();broken['days'][0]['day_index']=2
        planner=self.planner(broken,broken);report=RunReport()
        with self.assertRaises(PlanValidationError):await planner.invoke('test',report=report)
        self.assertEqual(report.status,'validation_failed')
        self.assertEqual(len(report.attempts),2)
        planner.llm.ainvoke.assert_awaited_once()

    async def test_no_time_reset_for_repair(self):
        broken=plan();broken['days'][0]['day_index']=2
        planner=self.planner(broken,plan());report=RunReport()
        async def slow(*a,**kw):await asyncio.sleep(.1)
        planner.llm.ainvoke=slow
        with patch('agents.planner.CONFIG.run_timeout',.01):
            with self.assertRaises(TimeoutError):await planner.invoke('test',report=report)
        self.assertEqual(report.status,'timeout')

    async def test_repair_uses_existing_model_budget(self):
        broken=plan();broken['days'][0]['day_index']=2
        planner=self.planner(broken,plan());report=RunReport()
        async def generate(*a,**kw):
            current_budget.get().model_calls=1
            return {'messages':[response(broken)]}
        planner._agent.ainvoke=generate
        with patch('agents.planner.CONFIG.max_model_calls',1):
            with self.assertRaises(ExecutionLimitError):await planner.invoke('test',report=report)
        planner.llm.ainvoke.assert_not_awaited()
        self.assertEqual(report.status,'limit_exceeded')

    async def test_stream_only_returns_valid_final_draft(self):
        broken=plan();broken['budget']['total']=0
        planner=self.planner(broken,plan());report=RunReport()
        async def events(*a,**kw):
            yield {'event':'on_chat_model_start'}
            yield {'event':'on_chat_model_stream','data':{'chunk':SimpleNamespace(content='我先查询')}}
            yield {'event':'on_tool_start','name':'maps_weather'}
            yield {'event':'on_chat_model_start'}
            yield {'event':'on_chat_model_stream','data':{'chunk':response(broken)}}
        planner._agent.astream_events=events
        chunks=[c async for c in planner.stream('test',report=report)]
        bodies=[c for c in chunks if not c.startswith('\n')]
        self.assertEqual(len(bodies),1)
        self.assertEqual(json.loads(bodies[0])['budget']['total'],310)
        self.assertNotIn('我先查询',''.join(chunks))

    async def test_service_exception_sanitized(self):
        planner=self.planner(plan(),plan());report=RunReport()
        planner._agent.ainvoke=AsyncMock(side_effect=RuntimeError('secret-key-in-url'))
        with self.assertRaises(PlanningServiceError) as caught:await planner.invoke('test',report=report)
        self.assertNotIn('secret-key',str(caught.exception))
        self.assertEqual(report.status,'failed')

    async def test_cancelled_stream_closes_execution_context(self):
        planner=self.planner(plan(),plan());report=RunReport()
        async def events(*a,**kw):
            yield {'event':'on_tool_start','name':'maps_weather'}
            await asyncio.sleep(1)
        planner._agent.astream_events=events
        stream=planner.stream('test',report=report)
        await anext(stream)
        await stream.aclose()
        self.assertEqual(report.status,'cancelled')
        self.assertIsNone(current_budget.get())

    def test_truncated_json_never_passes_as_plain_text(self):
        with self.assertRaises(PlanValidationError) as caught:validate_plan_text('{"city":"长沙"')
        self.assertEqual(caught.exception.issues[0]['code'],'invalid_json')

    async def test_arithmetic_normalization_does_not_call_model(self):
        broken=plan();broken['budget']['total_meals']=0;broken['budget']['total']=0
        planner=self.planner(broken,plan());report=RunReport()
        result=await planner.invoke('test',report=report)
        planner.llm.ainvoke.assert_not_awaited()
        self.assertEqual(json.loads(result)['budget']['total'],310)
        self.assertEqual(len(report.attempts[0]['normalizations']),2)

    async def test_normalization_cannot_hide_over_budget(self):
        broken=plan();broken['budget']['total']=0
        planner=self.planner(broken,plan());report=RunReport()
        request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',budget_limit=100)
        with patch('agents.planner.CONFIG.plan_repairs',0):
            with self.assertRaises(PlanValidationError) as caught:await planner.invoke('test',request=request,report=report)
        self.assertIn('budget_exceeded',{i['code'] for i in caught.exception.issues})
        self.assertEqual(report.status,'validation_failed')

    async def test_reports_and_budgets_isolated_between_concurrent_runs(self):
        planner=self.planner(plan(),plan())
        from reliability import reserve_model_call
        async def generate(*args,**kwargs):
            reserve_model_call(1)
            await asyncio.sleep(.01)
            return {'messages':[response(plan())]}
        planner._agent.ainvoke=generate
        reports=[RunReport(),RunReport()]
        await asyncio.gather(*[planner.invoke('test',report=r) for r in reports])
        self.assertTrue(all(r.model_calls==1 and r.status=='valid_plan' for r in reports))
        self.assertNotEqual(reports[0].run_id,reports[1].run_id)
        self.assertIsNone(current_budget.get())

    async def test_repair_must_not_change_prices_to_meet_budget(self):
        broken=plan();broken['days'][0]['day_index']=2
        revised=plan();revised['days'][0]['hotel']['estimated_cost']=1
        planner=self.planner(broken,revised)
        with self.assertRaises(PlanValidationError) as caught:await planner.invoke('test')
        self.assertIn('fact_changed',{i['code'] for i in caught.exception.issues})

    def test_one_complete_fence_allowed_multiple_fences_rejected(self):
        self.assertIsNotNone(validate_plan_text('行程如下：\n```json\n'+json.dumps(plan())+'\n```'))
        with self.assertRaises(PlanValidationError):validate_plan_text('```json\n{}\n```\n```json\n{}\n```')

    def test_departure_day_can_have_no_hotel_or_attraction(self):
        from schemas import recalculate_budget
        from render import format_plan_cli
        data=plan();data['days'][0]['hotel']=None;data['days'][0]['attractions']=[]
        text,changes=recalculate_budget(json.dumps(data))
        checked=validate_plan_text(text)
        self.assertEqual(checked['budget']['total_hotels'],0)
        self.assertIsNone(checked['days'][0]['hotel'])
        self.assertIsInstance(format_plan_cli(text),str)

    async def test_httpx_exception_group_retried_and_tool_error_sanitized(self):
        import httpx
        from langchain_core.messages import ToolMessage
        token=current_budget.set(ExecutionBudget())
        try:
            transient=ExceptionGroup('transport',[httpx.ConnectError('private-url')])
            self.assertTrue(transient_error(transient))
            self.assertFalse(transient_error(ExceptionGroup('mixed',[ValueError('bad'),transient])))
            handler=AsyncMock(side_effect=[transient,ToolMessage(content='private-url',status='error',tool_call_id='x')])
            value=await call_with_retry(handler,SimpleNamespace(tool_call={'id':'x'}),timeout=1,retries=1,max_calls=3)
            self.assertEqual(value.status,'error')
            self.assertNotIn('private-url',value.content)
            self.assertEqual(handler.await_count,2)
        finally:
            current_budget.reset(token)

    async def test_read_cache_is_per_run_and_rebinds_tool_call_id(self):
        from langchain_core.messages import ToolMessage
        token=current_budget.set(ExecutionBudget())
        try:
            handler=AsyncMock(return_value=ToolMessage(content='result',tool_call_id='one',id='old-message-id'))
            for call_id in ['one','two']:
                request=SimpleNamespace(tool_call={'id':call_id,'name':'maps_weather','args':{'city':'110000'}})
                value=await call_with_retry(handler,request,timeout=1,retries=0,max_calls=1)
                self.assertEqual(value.tool_call_id,call_id)
                if call_id == 'two':
                    self.assertIsNone(value.id)
            self.assertEqual(handler.await_count,1)
            self.assertEqual(current_budget.get().cache_hits,1)
        finally:current_budget.reset(token)
        token=current_budget.set(ExecutionBudget())
        try:
            await call_with_retry(handler,request,timeout=1,retries=0,max_calls=1)
            self.assertEqual(handler.await_count,2)
        finally:current_budget.reset(token)
