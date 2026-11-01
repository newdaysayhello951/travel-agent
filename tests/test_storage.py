from pathlib import Path
import unittest
from uuid import uuid4

from reliability import RunReport
from storage import PlanStore,ConversationNotFound,VersionConflict


class StorageTests(unittest.TestCase):
    def setUp(self):self.store=PlanStore('sqlite://')
    def tearDown(self):self.store.close()

    def test_versions_are_append_only_and_stale_edits_do_not_overwrite(self):
        cid=self.store.create_conversation('fixture')
        self.assertIsNone(self.store.get_plan(cid))
        self.store.save_plan(cid,0,{'city':'A'},{},'first',RunReport())
        self.store.save_plan(cid,1,{'city':'B'},{},'second',RunReport())
        with self.assertRaises(VersionConflict):self.store.save_plan(cid,1,{'city':'wrong'},{},'stale',RunReport())
        self.assertEqual(self.store.get_plan(cid).plan['city'],'B')
        self.assertEqual(self.store.get_plan(cid,1).plan['city'],'A')
        self.assertEqual(self.store.history(cid)['versions'],[1,2])
        self.assertEqual(len(self.store.history(cid)['runs']),2)

    def test_failure_records_do_not_create_success_versions(self):
        cid=self.store.create_conversation('fixture')
        report=RunReport(status='validation_failed')
        self.store.record_outcome(cid,'query','not accepted',report)
        self.assertIsNone(self.store.get_plan(cid))
        self.assertEqual(self.store.history(cid)['runs'][0]['status'],'validation_failed')
        with self.assertRaises(ConversationNotFound):self.store.get_plan('missing')

    def test_restart_recovers_plan_from_file(self):
        path=Path(__file__).resolve().parents[1]/'.work'/f'store-test-{uuid4().hex}.db'
        store=PlanStore('sqlite:///'+str(path));cid=store.create_conversation('durable')
        store.save_plan(cid,0,{'city':'A'},{'city':'A'},'first',RunReport());store.close()
        reopened=PlanStore('sqlite:///'+str(path))
        try:self.assertEqual(reopened.get_plan(cid).version,1)
        finally:reopened.close()

    def test_failed_transaction_rolls_back_version_and_messages(self):
        from sqlalchemy.exc import IntegrityError
        cid=self.store.create_conversation('atomic');report=RunReport()
        self.store.save_plan(cid,0,{'city':'A'},{},'first',report)
        # 重复执行编号使提交失败，即使版本计数已UPDATE也必须整体回滚。
        with self.assertRaises(IntegrityError):self.store.save_plan(cid,1,{'city':'B'},{},'second',report)
        self.assertEqual(self.store.get_plan(cid).version,1)
        self.assertEqual(self.store.history(cid)['versions'],[1])
        self.assertEqual(len(self.store.history(cid)['messages']),2)

    def test_concurrent_writers_only_one_can_advance_version(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        path=Path(__file__).resolve().parents[1]/'.work'/f'concurrency-{uuid4().hex}.db'
        store=PlanStore('sqlite:///'+str(path));cid=store.create_conversation('race');barrier=Barrier(2)
        def write(city):
            barrier.wait(timeout=5)
            try:store.save_plan(cid,0,{'city':city},{},city,RunReport());return 'saved'
            except VersionConflict:return 'conflict'
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(write,['A','B']))
            self.assertCountEqual(results,['saved','conflict'])
            self.assertEqual(store.history(cid)['versions'],[1])
        finally:store.close()
