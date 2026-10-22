from pathlib import Path
import sys
import unittest
import zipfile
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
from parser import EmptyBody, Pending, Blocked, Locked, AccessLimited, validate_body
from browser_client import BrowserClient


class EmptyBodyTests(unittest.TestCase):
    def test_explicit_empty_is_distinct_from_loading_and_access_gates(self):
        for state in ({'rendered':False,'message':'본문이 아직 준비되지 않았습니다.'},
                      {'rendered':True,'text':'본문이 아직 준비되지 않았습니다.'},
                      {'rendered':True,'text':''},
                      {'rendered':False,'empty':True,'images':0}):
            with self.subTest(state=state),self.assertRaises(EmptyBody):validate_body(state)
        with self.assertRaises(Pending) as error:validate_body({'rendered':False,'message':'로딩 중'})
        self.assertNotIsInstance(error.exception,EmptyBody)
        for state, error in [({'message':'퍼즐 조각을 맞춰주세요','empty':True},Blocked),
                             ({'message':'인증이 필요합니다','empty':True},Blocked),
                             ({'message':'일일 이용 한도 초과','empty':True},AccessLimited),
                             ({'locked':True,'empty':True},Locked)]:
            with self.assertRaises(error):validate_body(state)

    def test_browser_wait_preserves_explicit_empty_only_after_timeout(self):
        for state, empty in [({'rendered':False,'message':'본문이 아직 준비되지 않았습니다.'},True),
                             ({'rendered':False,'message':'로딩 중'},False)]:
            c=BrowserClient(fixtures.BASE,'http://127.0.0.1:9222')
            c.evaluate=Mock(side_effect=lambda expression:fixtures.BASE+'/novel/123/1001' if expression=='location.href' else state)
            c.stop.wait=Mock(return_value=False)
            with patch('browser_client.time.monotonic',side_effect=[0,0,100]):
                with self.assertRaises(Pending) as error:c.displayed_body('123',{'id':'1001','no':1},fixtures.viewer())
            self.assertEqual(isinstance(error.exception,EmptyBody),empty)


class EmptyBodyJobs(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    run_job=fixtures.EngineTests.run_job

    def test_empty_middle_episode_continues_without_file_and_can_be_downloaded_later(self):
        clients=[];available=False;reports=[]
        class Client(fixtures.FakeClient):
            def __init__(self,*args):super().__init__(*args);clients.append(self)
            def body(self,nid,item):
                self.calls.append(item['id'])
                if item['no']==2 and not available:raise EmptyBody('본문이 아직 준비되지 않았습니다.')
                return fixtures.TEXT
        self.engine.factory=Client;self.engine.stop.wait=Mock(return_value=False)
        self.config['on_finished']=reports.append
        state=self.run_job(['1001','1002','1003'])
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual((state['completed'],state['skipped'],state['failed']),(2,1,0))
        self.assertEqual(clients[0].calls,['1001','1002','1002','1003'])
        self.assertEqual(state['empty_episodes'][0]['no'],2)
        missing=next(row for row in self.engine.history.rows('123') if row['no']==2)
        self.assertEqual(missing['status'],'missing');self.assertEqual(missing['path'],'')
        folder=next((self.root/'books').iterdir())
        self.assertEqual(len(list(folder.glob('*.txt'))),2)
        with zipfile.ZipFile(next(folder.glob('*.epub'))) as z:
            self.assertIn('OEBPS/chapter-1003.xhtml',z.namelist())
            self.assertNotIn('OEBPS/chapter-1002.xhtml',z.namelist())
        available=True
        state=self.run_job(['1001','1002','1003'])
        self.assertEqual((state['completed'],state['skipped'],state['failed']),(1,2,0))
        self.assertNotIn('empty_episodes',state)
        self.assertEqual(len(list(folder.glob('*.txt'))),3)

    def test_normal_loading_timeout_still_stops_without_skipping(self):
        class Client(fixtures.FakeClient):
            def body(self,nid,item):raise Pending('로딩 대기')
        self.engine.factory=Client;self.engine.stop.wait=Mock(return_value=False)
        state=self.run_job(['1001','1002'])
        self.assertEqual(state['status'],'body_pending')
        self.assertEqual(state['skipped'],0)

    def test_temporary_empty_recovers_on_bounded_retry(self):
        counts={}
        class Client(fixtures.FakeClient):
            def body(self,nid,item):
                counts[item['id']]=counts.get(item['id'],0)+1
                if item['no']==2 and counts[item['id']]==1:raise EmptyBody('본문 없음')
                return fixtures.TEXT
        self.engine.factory=Client;self.engine.stop.wait=Mock(return_value=False)
        state=self.run_job(['1001','1002','1003'])
        self.assertEqual((state['completed'],state['skipped'],state['failed']),(3,0,0))
        self.assertEqual(counts['1002'],2)
        self.assertNotIn('empty_episodes',state)
