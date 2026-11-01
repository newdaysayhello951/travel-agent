import json
import unittest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from api import create_app
from auth import AuthStore,AuthenticationError,Session,User
from storage import PlanStore,ConversationNotFound
from sqlalchemy import select
from test_reliability import plan


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.store=PlanStore('sqlite://');self.calls=0
        async def invoke(prompt,request,report):
            self.calls+=1;report.status='valid_plan';return json.dumps(plan())
        self.client=TestClient(create_app(self.store,lambda:SimpleNamespace(invoke=invoke)))
        self.headers={}
        for name in ('alice','bobby'):
            self.assertEqual(self.client.post('/auth/register',json={'username':name,'password':'fixture-password-123'}).status_code,201)
            r=self.client.post('/auth/login',json={'username':name,'password':'fixture-password-123'})
            self.headers[name]={'Authorization':'Bearer '+r.json()['access_token']}

    def tearDown(self):self.client.close();self.store.close()

    def create(self):
        r=self.client.post('/plans',headers=self.headers['alice'],json={'query':'fixture','request':{'city':'长沙','start_date':'2026-11-10','end_date':'2026-11-10','stay_on_last_day':True}})
        self.assertEqual(r.status_code,201,r.text);return r.json()['conversation_id']

    def test_ownership_all_routes_and_no_paid_work_on_unauthorized(self):
        cid=self.create();calls=self.calls
        self.assertEqual(self.client.get('/conversations').status_code,401)
        self.assertEqual(self.client.get('/conversations',headers=self.headers['bobby']).json(),[])
        for suffix in ('','/history','/versions/1'):
            self.assertEqual(self.client.get('/conversations/'+cid+suffix,headers=self.headers['bobby']).status_code,404)
        r=self.client.post('/conversations/'+cid+'/revisions',headers=self.headers['bobby'],json={'instruction':'change','expected_version':1})
        self.assertEqual(r.status_code,404);self.assertEqual(self.calls,calls)
        with self.assertRaises(ConversationNotFound):self.store.get_plan(cid)

    def test_revision_versions_conflict_and_logout(self):
        cid=self.create();headers=self.headers['alice'];body={'instruction':'change','expected_version':1}
        self.assertEqual(self.client.post(f'/conversations/{cid}/revisions',headers=headers,json=body).status_code,201)
        self.assertEqual(self.client.post(f'/conversations/{cid}/revisions',headers=headers,json=body).status_code,409)
        self.assertEqual(self.client.get(f'/conversations/{cid}/history',headers=headers).json()['versions'],[1,2])
        self.assertEqual(self.client.post('/auth/logout',headers=headers).status_code,204)
        self.assertEqual(self.client.get('/me',headers=headers).status_code,401)

    def test_hashes_expiry_and_validation_never_echo_password(self):
        with self.store.sessions.begin() as session:
            users=session.scalars(select(User)).all()
            self.assertTrue(all(u.password_hash.startswith('$argon2') for u in users))
            sessions=session.scalars(select(Session)).all()
            self.assertTrue(all(len(s.token_hash)==64 for s in sessions))
            for row in sessions:row.expires_at=0
        self.assertEqual(self.client.get('/me',headers=self.headers['alice']).status_code,401)
        r=self.client.post('/auth/login',json={'username':'alice','password':'secret'})
        self.assertEqual(r.status_code,422);self.assertNotIn('secret',r.text)

    def test_login_throttling(self):
        for i in range(11):r=self.client.post('/auth/login',json={'username':'nobody','password':'invalid-password'})
        self.assertEqual(r.status_code,429)

    def test_upstream_quota_is_safe_429(self):
        from reliability import PlanningServiceError
        async def limited(*args,**kwargs):
            error=RuntimeError('private-provider-body');error.status_code=429
            try:raise error
            except RuntimeError:raise PlanningServiceError('safe') from None
        with TestClient(create_app(self.store,lambda:SimpleNamespace(invoke=limited))) as client:
            r=client.post('/plans',headers=self.headers['alice'],json={'query':'fixture','request':{'city':'长沙','start_date':'2026-11-10','end_date':'2026-11-10'}})
        self.assertEqual(r.status_code,429)
        self.assertEqual(r.json()['code'],'upstream_quota_limited')
        self.assertNotIn('private-provider-body',r.text)
