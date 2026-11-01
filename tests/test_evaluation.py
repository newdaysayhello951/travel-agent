import copy
import json
import unittest

from evals.judge import judge, summary
from schemas import TripRequest, PlanValidationError, validate_plan_text, recalculate_budget
from test_reliability import plan


class EvaluationTests(unittest.TestCase):
    def test_parseable_json_is_not_success(self):
        case={'expectation':{'statuses':['valid_plan']}}
        result=judge(case,{'status':'valid_plan','output':'{"city":"长沙"}'})
        self.assertFalse(result['rule_pass'])

    def test_text_is_not_valid_itinerary(self):
        case={'expectation':{'statuses':['valid_plan']}}
        self.assertEqual(judge(case,{'status':'text','output':'规划完成'})['verdict'],'fail')

    def test_manual_review_not_counted_as_full_pass(self):
        case={'expectation':{'statuses':['valid_plan'],'manual_checks':['是否适合儿童']}}
        row={'status':'valid_plan','output':json.dumps(plan())}
        row['assessment']=judge(case,row)
        self.assertEqual(row['assessment']['verdict'],'manual_review')
        self.assertEqual(summary([row])['verdicts']['pass'],0)
        self.assertEqual(summary([row])['rule_pass'],1)

    def test_clarification_requires_content_and_no_tools(self):
        case={'expectation':{'statuses':['text'],'keyword_groups':[['目的地','城市']], 'max_tool_attempts':0}}
        self.assertTrue(judge(case,{'status':'text','output':'请告诉我目的地','run_report':{'tool_attempts':0}})['rule_pass'])
        self.assertFalse(judge(case,{'status':'text','output':'收到'})['rule_pass'])
        self.assertFalse(judge(case,{'status':'text','output':'请告诉我目的地','run_report':{'tool_attempts':1}})['rule_pass'])

    def test_weather_only_requires_actual_tool(self):
        case={'expectation':{'statuses':['text'],'min_tool_attempts':1}}
        self.assertFalse(judge(case,{'status':'text','output':'晴'})['rule_pass'])

    def test_new_constraints_not_silently_ignored(self):
        request=TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',max_attractions_per_day=1,allowed_transportation_modes=['public_transit'],stay_on_last_day=True)
        data=plan();data['days'][0]['attractions']*=2
        data['budget']['total_attractions']=80;data['budget']['total']=350
        with self.assertRaises(PlanValidationError) as caught:validate_plan_text(json.dumps(data),request)
        self.assertEqual({i['code'] for i in caught.exception.issues},{'too_many_attractions','transport_not_allowed'})
        data=plan();data['days'][0]['transportation_mode']='public_transit'
        self.assertIsNotNone(validate_plan_text(json.dumps(data),request))
        with self.assertRaises(ValueError):
            TripRequest(city='长沙',start_date='2026-11-10',end_date='2026-11-10',misspelled_budget=1)

    def test_estimate_label_is_added_without_changing_price(self):
        data=plan();data['overall_suggestions']='欢迎出行'
        text,changes=recalculate_budget(json.dumps(data))
        result=json.loads(text)
        self.assertIn('费用均为估算',result['overall_suggestions'])
        self.assertEqual(result['days'],data['days'])
        self.assertEqual(changes[0]['path'],'overall_suggestions')
