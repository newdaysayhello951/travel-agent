import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agents.planner import TripPlanner
from intake import IntakeDecision, clarification
from reliability import RunReport
from schemas import TripRequest, PlanValidationError, validate_plan_text
from test_reliability import plan


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_city_never_builds_or_calls_map_tools(self):
        llm=SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=json.dumps({'intent':'plan','city':None}))))
        planner=TripPlanner(llm)
        planner.build=AsyncMock()
        report=RunReport()
        text=await planner.invoke('安排三天旅行，喜欢博物馆',report=report)
        self.assertIn('目的地',text)
        planner.build.assert_not_awaited()
        self.assertEqual(report.tool_attempts,0)
        self.assertEqual(report.model_calls,1)
        self.assertEqual(report.status,'text')

    def test_invented_city_date_conflict_and_weather(self):
        self.assertIsNotNone(clarification(IntakeDecision(intent='plan',city='巴黎'),'安排旅行'))
        decision=IntakeDecision(intent='plan',city='北京',start_date='2026-11-12',end_date='2026-11-10')
        self.assertIn('冲突',clarification(decision,'北京旅行'))
        self.assertIsNone(clarification(IntakeDecision(intent='weather',city='北京'),'北京天气'))
        self.assertIsNone(clarification(IntakeDecision(intent='weather',city='北京市'),'北京天气'))

    def test_departure_charges_require_explicit_stay(self):
        data=plan()
        request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10')
        with self.assertRaises(PlanValidationError) as caught:validate_plan_text(json.dumps(data),request)
        self.assertIn('departure_charge',{i['code'] for i in caught.exception.issues})
        request.stay_on_last_day=True
        self.assertIsNotNone(validate_plan_text(json.dumps(data),request))

    async def test_malformed_intake_has_field_diagnostic_and_no_tools(self):
        llm=SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='{"city":null}')))
        planner=TripPlanner(llm);planner.build=AsyncMock();report=RunReport()
        with self.assertRaises(PlanValidationError) as caught:await planner.invoke('安排三天旅行',report=report)
        self.assertEqual(caught.exception.issues[0]['path'],'intake.intent')
        planner.build.assert_not_awaited()
        self.assertEqual(report.status,'validation_failed')
