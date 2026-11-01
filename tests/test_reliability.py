import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reliability import ExecutionBudget, ExecutionLimitError, call_with_retry, current_budget
from schemas import PlanValidationError, TripRequest, validate_plan_text


def plan():
    return {'city':'长沙','start_date':'2026-11-10','end_date':'2026-11-10',
            'days':[{'date':'2026-11-10','day_index':0,'hotel':{'name':'酒店','estimated_cost':200},
                     'attractions':[{'name':'岳麓书院','ticket_price':40}],
                     'meals':[{'name':'餐饮','type':kind,'estimated_cost':20} for kind in ('breakfast','lunch','dinner')]}],
            'budget':{'total_attractions':40,'total_hotels':200,'total_meals':60,'total_transportation':10,'total':310},
            'weather_info':[],'overall_suggestions':'费用为估算'}


class SchemaTests(unittest.TestCase):
    def test_valid_plan_and_request(self):
        request = TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',required_attractions=['岳麓书院'])
        self.assertEqual(validate_plan_text(json.dumps(plan()), request)['budget']['total'],310)

    def test_bad_dates_budget_meals_and_weather_rejected(self):
        for mutate in [lambda p:p.update(end_date='2026-11-11'),
                       lambda p:p['budget'].update(total=0),
                       lambda p:p['days'][0]['hotel'].update(estimated_cost=-1),
                       lambda p:p['days'][0].update(meals=[]),
                       lambda p:p.update(weather_info=[{'date':'2026-11-20'}])]:
            with self.subTest(mutate=mutate):
                data=copy.deepcopy(plan());mutate(data)
                with self.assertRaises(PlanValidationError):validate_plan_text(json.dumps(data))

    def test_request_constraints_and_text(self):
        for extra in ({'budget_limit':100},{'required_attractions':['故宫']}):
            request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',**extra)
            with self.assertRaises(PlanValidationError):validate_plan_text(json.dumps(plan()),request)
        self.assertIsNone(validate_plan_text('请补充目的地'))
        with self.assertRaises(PlanValidationError):
            validate_plan_text('没有行程',TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10'))


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.token=current_budget.set(ExecutionBudget())
        self.request=SimpleNamespace(tool_call={'id':'test'})

    async def asyncTearDown(self):current_budget.reset(self.token)

    async def call(self,handler,**kwargs):
        return await call_with_retry(handler,self.request,timeout=.01,retries=1,max_calls=3,**kwargs)

    async def test_transient_retry_then_success(self):
        handler=AsyncMock(side_effect=[ConnectionError(), 'ok'])
        self.assertEqual(await self.call(handler),'ok')
        self.assertEqual(current_budget.get().attempts,2)

    async def test_timeout_returns_safe_tool_error(self):
        async def slow(request):await asyncio.sleep(1)
        result=await self.call(slow)
        self.assertEqual(result.status,'error')
        self.assertEqual(current_budget.get().attempts,2)

    async def test_non_transient_not_retried(self):
        handler=AsyncMock(side_effect=ValueError('bad args'))
        with self.assertRaises(ValueError):await self.call(handler)
        self.assertEqual(handler.await_count,1)

    async def test_budget_exhaustion_and_cancellation(self):
        current_budget.get().attempts=3
        with self.assertRaises(ExecutionLimitError):await self.call(AsyncMock())
        current_budget.get().attempts=0
        with self.assertRaises(asyncio.CancelledError):await self.call(AsyncMock(side_effect=asyncio.CancelledError()))

    async def test_parallel_calls_share_attempt_limit(self):
        handler=AsyncMock(return_value='ok')
        results=await asyncio.gather(*[call_with_retry(handler,self.request,timeout=1,retries=0,max_calls=1) for _ in range(2)],return_exceptions=True)
        self.assertEqual(handler.await_count,1)
        self.assertEqual(sum(isinstance(r,ExecutionLimitError) for r in results),1)

    async def test_overall_timeout_and_context_reset(self):
        from agents.planner import TripPlanner
        async def slow():await asyncio.sleep(1)
        planner=TripPlanner(object());planner.build=slow
        previous=current_budget.get()
        with patch('agents.planner.CONFIG.run_timeout',.01):
            with self.assertRaises(TimeoutError):await planner.invoke('test')
        self.assertIs(current_budget.get(),previous)

    async def test_stream_invalid_plan_and_recursion_not_success(self):
        from agents.planner import TripPlanner
        from langgraph.errors import GraphRecursionError
        async def invalid(*args, **kwargs):
            yield {'event':'on_chat_model_stream','data':{'chunk':SimpleNamespace(content=json.dumps({'city':'长沙','days':[]}))}}
        planner=TripPlanner(object())
        planner._agent=SimpleNamespace(astream_events=invalid)
        with self.assertRaises(PlanValidationError):
            async for _ in planner.stream('test'):pass
        self.assertIsNotNone(current_budget.get())
        async def recurse(*args, **kwargs):raise GraphRecursionError('limit')
        planner._agent=SimpleNamespace(ainvoke=recurse)
        with self.assertRaises(ExecutionLimitError):await planner.invoke('test')
