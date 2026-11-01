import json
import unittest

from intake import IntakeDecision, clarification
from schemas import TripPlan, PlanValidationError, recalculate_budget, validate_plan_text
from test_reliability import plan


class SchemaNormalizationTests(unittest.TestCase):
    def test_unknown_optional_stay_does_not_block_missing_city_question(self):
        decision=IntakeDecision.model_validate({'intent':'plan','city':None,'stay_on_last_day':None,'conflict':None,'message':None})
        self.assertIn('目的地',clarification(decision,'安排三天旅行'))

    def test_meal_aliases_normalize_without_inventing_food_or_prices(self):
        data=plan()
        for meal,label in zip(data['days'][0]['meals'],['早餐','LUNCH','晚饭']):meal['type']=label
        text,changes=recalculate_budget(json.dumps(data))
        result=validate_plan_text(text)
        self.assertEqual([m['type'] for m in result['days'][0]['meals']],['breakfast','lunch','dinner'])
        self.assertEqual([m['estimated_cost'] for m in result['days'][0]['meals']],[20,20,20])
        self.assertEqual(len(changes),3)

    def test_missing_meal_is_not_filled_or_treated_as_free(self):
        data=plan();data['days'][0]['meals'].pop()
        text,_=recalculate_budget(json.dumps(data))
        self.assertEqual(len(json.loads(text)['days'][0]['meals']),2)
        with self.assertRaises(PlanValidationError) as caught:validate_plan_text(text)
        self.assertIn('too_short',{i['code'] for i in caught.exception.issues})

    def test_schema_and_duplicate_meal_diagnostics(self):
        schema=TripPlan.model_json_schema()
        self.assertEqual(schema['$defs']['DayPlan']['properties']['meals']['minItems'],3)
        self.assertEqual(schema['$defs']['DayPlan']['properties']['meals']['maxItems'],3)
        data=plan();data['days'][0]['meals'][-1]['type']='lunch'
        with self.assertRaises(PlanValidationError) as caught:validate_plan_text(json.dumps(data))
        self.assertIn('实际餐别为breakfast,lunch,lunch',str(caught.exception))
