import json
import unittest
from types import SimpleNamespace

from schemas import TripRequest,PlanValidationError
from storage import PlanStore,VersionConflict
from travel_service import TravelService
from test_reliability import plan


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store=PlanStore('sqlite://')
        self.response=plan();self.prompts=[]
        async def invoke(prompt,request,report):
            self.prompts.append(prompt);report.status='valid_plan'
            return json.dumps(self.response)
        self.service=TravelService(SimpleNamespace(invoke=invoke),self.store)
        self.request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',stay_on_last_day=True)

    def tearDown(self):self.store.close()

    async def test_create_then_revise_preserves_history_and_input(self):
        first=await self.service.create('喜欢历史',self.request)
        self.response['overall_suggestions']='新的安排，费用为估算'
        second=await self.service.revise(first.conversation_id,'调整游览顺序',expected_version=1,preserve_hotels=True)
        self.assertEqual(second.version,2)
        self.assertIn('喜欢历史',self.prompts[-1])
        self.assertIn('current_plan',self.prompts[-1])
        self.assertEqual(self.store.get_plan(first.conversation_id,1).plan['overall_suggestions'],'费用为估算')
        self.assertEqual(len(self.store.history(first.conversation_id)['messages']),4)
        await self.service.revise(first.conversation_id,'补充建议',expected_version=2)
        self.assertIn('调整游览顺序',self.prompts[-1])

    async def test_bad_revision_never_becomes_success_version(self):
        first=await self.service.create('fixture',self.request)
        self.response['budget']['total']=0
        with self.assertRaises(PlanValidationError):await self.service.revise(first.conversation_id,'fixture')
        self.assertEqual(self.store.get_plan(first.conversation_id).version,1)
        self.assertEqual(self.store.history(first.conversation_id)['runs'][-1]['status'],'validation_failed')

    async def test_hotel_preservation_and_stale_interface(self):
        first=await self.service.create('fixture',self.request)
        with self.assertRaises(VersionConflict):await self.service.revise(first.conversation_id,'change',expected_version=0)
        self.response['days'][0]['hotel']['name']='different'
        with self.assertRaises(PlanValidationError):await self.service.revise(first.conversation_id,'keep hotels',preserve_hotels=True)
        self.assertEqual(self.store.get_plan(first.conversation_id).version,1)

    async def test_unknown_constraint_is_rejected_before_model_call(self):
        first=await self.service.create('fixture',self.request);calls=len(self.prompts)
        with self.assertRaises(ValueError):await self.service.revise(first.conversation_id,'change',request_updates={'unknown':1})
        self.assertEqual(len(self.prompts),calls)

    async def test_stream_progress_is_not_stored_as_plan(self):
        async def stream(prompt,request,report):
            yield '\n查询地点...\n'
            report.status='valid_plan'
            yield json.dumps(self.response)
        self.service.planner.stream=stream
        progress=[]
        saved=await self.service.create('fixture',self.request,on_progress=progress.append)
        self.assertEqual(len(progress),2)
        self.assertEqual(saved.plan['city'],'长沙')
        self.assertNotIn('查询地点',self.store.history(saved.conversation_id)['messages'][-1]['content'])

    async def test_cancelled_revision_is_recorded_without_overwriting(self):
        import asyncio
        first=await self.service.create('fixture',self.request)
        async def cancel(*args,**kwargs):raise asyncio.CancelledError()
        self.service.planner.invoke=cancel
        with self.assertRaises(asyncio.CancelledError):await self.service.revise(first.conversation_id,'cancel')
        self.assertEqual(self.store.get_plan(first.conversation_id).version,1)
        self.assertEqual(self.store.history(first.conversation_id)['runs'][-1]['status'],'cancelled')


class ConstrainedRepairTests(unittest.TestCase):
    def test_only_explicit_excess_attractions_can_be_removed(self):
        from schemas import validate_repair_facts
        import copy
        before=plan();before['days'][0]['attractions'].append({'name':'另一景点','ticket_price':0})
        after=copy.deepcopy(before);after['days'][0]['attractions']=after['days'][0]['attractions'][:1]
        request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',max_attractions_per_day=1)
        validate_repair_facts(json.dumps(before),json.dumps(after),request)
        with self.assertRaises(PlanValidationError):validate_repair_facts(json.dumps(before),json.dumps(after))
        after['days'][0]['attractions'][0]['ticket_price']=1
        with self.assertRaises(PlanValidationError):validate_repair_facts(json.dumps(before),json.dumps(after),request)
        after=copy.deepcopy(before);after['days'][0]['meals'].pop()
        with self.assertRaises(PlanValidationError):validate_repair_facts(json.dumps(before),json.dumps(after),request)
