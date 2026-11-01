import unittest
from preferences import update_preferences,check_preferences
from test_reliability import plan


class PreferenceTests(unittest.TestCase):
    def test_explicit_constraint_persists_and_can_be_explicitly_cancelled(self):
        state=update_preferences('重庆两日游，不吃辣')
        self.assertTrue(update_preferences('加一条雨天建议',state)['avoid_spicy'])
        self.assertFalse(update_preferences('取消不吃辣限制',state)['avoid_spicy'])
        self.assertTrue(update_preferences('不可以吃辣',state)['avoid_spicy'])
        self.assertTrue(update_preferences('同行的人可以吃辣',state)['avoid_spicy'])
        self.assertIsNone(update_preferences('不吃辣椒籽')['avoid_spicy'])

    def test_mild_spice_option_is_conflict_but_missing_word_is_not_proof(self):
        data=plan();state={'avoid_spicy':True}
        data['days'][0]['meals'][1]['name']='火锅（微辣或鸳鸯锅）'
        self.assertEqual(check_preferences(data,state)['errors'][0]['code'],'explicit_spice_conflict')
        data['days'][0]['meals'][1]['name']='清汤火锅'
        result=check_preferences(data,state)
        self.assertFalse(result['errors']);self.assertTrue(result['unverified'])
        data['days'][0]['meals'][1]['name']='定制餐（不要微辣）'
        self.assertFalse(check_preferences(data,state)['errors'])
