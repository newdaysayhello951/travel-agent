import copy,json,unittest
from datetime import date,timedelta
from knowledge import KnowledgeStore,Document
from storage import PlanStore,ConversationNotFound
from fact_checks import check_plan
from test_reliability import plan


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.store=PlanStore('sqlite://',owner_id='alice');self.kb=KnowledgeStore(self.store)
        self.expiry=(date.today()+timedelta(days=7)).isoformat()
    def tearDown(self):self.store.close()
    def put(self,content='长沙博物馆需提前预约，雨天携带雨具。',**kwargs):
        return self.kb.put('参观须知','长沙','https://example.org/visit',content,self.expiry,**kwargs)
    def test_retrieval_is_scoped_expiring_and_update_replaces_chunks(self):
        identity=self.put();hits=self.kb.retrieve('长沙博物馆预约','长沙');self.assertTrue(hits)
        self.assertEqual(KnowledgeStore(self.store.for_owner('bobby')).retrieve('博物馆预约','长沙'),[])
        with self.assertRaises(ConversationNotFound):KnowledgeStore(self.store.for_owner('bobby')).put('x','长沙','https://example.org','x',self.expiry,document_id=identity)
        self.put('全新海洋展览资料',document_id=identity)
        self.assertEqual(self.kb.retrieve('博物馆预约','长沙'),[])
        with self.store.sessions.begin() as s:s.get(Document,identity).expires_on='2000-01-01'
        self.assertEqual(self.kb.retrieve('海洋展览','长沙'),[])
    def test_credential_urls_and_invalid_rules_rejected(self):
        with self.assertRaises(ValueError):self.kb.put('x','长沙','https://example.org?key=private','x',self.expiry)
        with self.assertRaises(ValueError):self.put(facts=[{'name':'x'}])


class FactTests(unittest.TestCase):
    def test_no_activities_are_not_treated_as_verified(self):
        data=plan();data['days'][0]['attractions']=[]
        self.assertEqual(check_plan(data,[])['status'],'needs_review')

    def test_free_entry_requires_separating_optional_charges(self):
        data=plan();rule=self.rule();rule['adult_ticket_price']=0
        result=check_plan(data,[rule])
        self.assertIn('free_entry_has_charge',[x['code'] for x in result['errors']])
        data['days'][0]['attractions'][0]['ticket_price_basis']='package'
        self.assertNotIn('free_entry_has_charge',[x['code'] for x in check_plan(data,[rule])['errors']])
    def rule(self):return {'name':'岳麓书院','valid_from':'2026-01-01','valid_until':'2026-12-31','opens':'09:00','closes':'17:00','last_entry':'16:00','adult_ticket_price':40,'source_url':'https://example.org/rules'}
    def test_closed_hours_price_and_reservation_are_not_claimed_pass(self):
        data=plan();a=data['days'][0]['attractions'][0]
        self.assertEqual(check_plan(data,[])['status'],'needs_review')
        a.update(start_time='16:30',end_time='18:00',ticket_price_basis='adult_per_person',ticket_price=60)
        rule=self.rule();rule['reservation_required']=True
        result=check_plan(data,[rule]);codes={x['code'] for x in result['errors']}
        self.assertTrue({'outside_open_hours','after_last_entry','price_mismatch'}<=codes)
        self.assertIn('reservation_required',[x['code'] for x in result['unverified']])
    def test_time_overlap_route_gap_and_expired_rules(self):
        data=plan();a=data['days'][0]['attractions'][0];a.update(start_time='09:00',end_time='11:00')
        data['days'][0]['attractions'].append({'name':'其他','ticket_price':0,'start_time':'11:10','end_time':'12:00','travel_minutes_from_previous':30})
        self.assertIn('insufficient_travel_time',[x['code'] for x in check_plan(data,[])['errors']])
        data['days'][0]['attractions'][1]['start_time']='10:00'
        self.assertIn('time_overlap',[x['code'] for x in check_plan(data,[])['errors']])
        rule=self.rule();rule['valid_until']='2026-01-02'
        self.assertIn('missing_or_conflicting_source',[x['code'] for x in check_plan(data,[rule])['unverified']])
