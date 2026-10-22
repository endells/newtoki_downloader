from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
from parser import Blocked, Pending, SiteError
from browser_client import BrowserClient

class PauseResume(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    run_job=fixtures.EngineTests.run_job
    def wait(self,predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if predicate():return
            threading.Event().wait(.01)
        self.fail('worker did not reach expected state: '+str(self.engine.snapshot()))

    def challenge_client(self):
        class C(fixtures.FakeClient):
            def __init__(self,*args):super().__init__(*args);self.normal=[];self.resumes=0;self.confirmed=False
            def body(self,nid,item):
                self.normal.append(item['id'])
                if item['no']==2:raise Blocked('본문 보안 검증에 실패했습니다.')
                return fixtures.TEXT
            def continue_body(self,nid,item):
                self.resumes+=1
                if not self.confirmed:raise Blocked('아직 보안 확인 필요')
                return fixtures.TEXT
        c=C(fixtures.BASE);self.engine.factory=lambda *args:c
        return c

    def test_puzzle_after_18_saves_waits_with_link_and_continues_without_failure(self):
        from parser import viewer_config
        class C(fixtures.FakeClient):
            confirmed=False
            def html(self,path):return fixtures.catalog(range(1,26))
            def body(self,nid,item):
                self.calls.append(item['id'])
                if item['no']==19:
                    viewer_config('<link rel="canonical" href="https://alias.example/novel/123/1019"><h1 class="ne-h1">19</h1><article class="novel-viewer"><div class="novel-loading">퍼즐 조각을 맞춰주세요 봇 확인 후 컨텐츠 열람 가능합니다.</div></article>',nid,item['id'])
                return fixtures.TEXT
            def continue_body(self,nid,item):
                if not self.confirmed:raise Blocked('밀어서 맞추기')
                return fixtures.TEXT
        c=C('https://toki35.com');self.engine.factory=lambda *args:c
        self.config['batch_size']=100
        self.engine.start('download',['123'],self.config,[str(1000+n) for n in range(1,26)])
        self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        state=self.engine.snapshot()
        self.assertEqual((state['completed'],state['failed']),(18,0))
        self.assertEqual(state['verification_url'],'https://toki35.com/novel/123/1019')
        self.assertEqual(len(c.calls),19)
        c.confirmed=True;self.engine.resume();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'completed')
        self.assertEqual(self.engine.snapshot()['completed'],25)
        self.assertEqual(self.engine.snapshot()['failed'],0)
        self.assertEqual(c.calls.count('1019'),1)

    def test_security_wait_never_auto_retries_and_explicit_resume_continues(self):
        c=self.challenge_client();alerts=[];self.config['on_verification']=alerts.append
        self.engine.start('download',['123'],self.config,['1001','1002','1003'])
        self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(c.normal,['1001','1002']);self.assertEqual(c.resumes,0)
        self.assertEqual(self.engine.snapshot()['completed'],1)
        self.assertTrue(next((self.root/'books').rglob('*.epub')).exists())
        self.wait(lambda:len(alerts)==1)
        with self.assertRaises(SiteError):self.engine.start('analyze',['123'],self.config)
        self.engine.resume()
        self.wait(lambda:c.resumes==1 and self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(c.normal,['1001','1002']);self.assertEqual(len(alerts),1)
        c.confirmed=True;self.engine.resume();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'completed')
        self.assertEqual(self.engine.snapshot()['completed'],3)
        self.assertEqual(c.normal,['1001','1002','1003'])
        self.assertFalse(c.keep_tab)

    def test_stop_during_security_wait_preserves_completed_files(self):
        self.challenge_client()
        self.engine.start('download',['123'],self.config,['1001','1002'])
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.engine.cancel();self.engine.thread.join(3)
        self.assertFalse(self.engine.thread.is_alive())
        self.assertEqual(self.engine.snapshot()['status'],'canceled')
        self.assertEqual(self.engine.snapshot()['completed'],1)
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),1)

    def test_pending_retries_once_and_persistent_pending_stops(self):
        class C(fixtures.FakeClient):
            count=0
            def body(self,*args):
                self.count+=1
                if self.count==1:raise Pending('본문 대기')
                return fixtures.TEXT
        c=C(fixtures.BASE);self.engine.factory=lambda *args:c
        with patch.object(self.engine.stop,'wait',return_value=False):
            self.assertEqual(self.run_job(['1001'])['completed'],1)
        self.assertEqual(c.count,2)
        class Always(C):
            def body(self,*args):self.count+=1;raise Pending('계속 본문 대기')
        c=Always(fixtures.BASE)
        with patch.object(self.engine.stop,'wait',return_value=False):
            state=self.run_job(['1002'])
        self.assertEqual(state['status'],'body_pending');self.assertEqual(c.count,2)

    def test_browser_resume_reads_same_tab_without_navigation(self):
        c=BrowserClient(fixtures.BASE,'http://127.0.0.1:9222')
        c.html=Mock(side_effect=AssertionError('no navigation'))
        c.call=Mock(side_effect=AssertionError('no commands beyond mocked evaluation'))
        c.evaluate=Mock(side_effect=[fixtures.BASE+'/novel/123/1001',fixtures.viewer(),fixtures.BASE+'/novel/123/1001',{'rendered':True,'text':fixtures.TEXT,'images':0}])
        self.assertEqual(c.continue_body('123',{'id':'1001','no':1}),fixtures.TEXT)
        c.html.assert_not_called();c.call.assert_not_called()
        c.evaluate=Mock(side_effect=[fixtures.BASE+'/novel/123/1001','<title>Just a moment...</title>Verify you are human'])
        with self.assertRaises(Blocked):c.continue_body('123',{'id':'1001','no':1})
        c.html.assert_not_called()

    def test_25_episodes_rest_after_each_10_not_after_final_episode(self):
        class C(fixtures.FakeClient):
            def html(self,path):return fixtures.catalog(range(1,26))
        c=C(fixtures.BASE);self.engine.factory=lambda *args:c
        self.config.update(batch_size=10,batch_pause=60)
        with patch.object(self.engine,'batch_rest') as rest:
            counts=[];rest.side_effect=lambda seconds:counts.append((seconds,len(c.calls)))
            state=self.run_job([str(1000+n) for n in range(1,26)])
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['completed'],25)
        self.assertEqual(counts,[(60,10),(60,20)])

    def test_modern_catalog_challenge_waits_and_resumes_same_tab_before_saving_domain(self):
        class C(fixtures.FakeClient):
            def __init__(self,*args):super().__init__(*args);self.opens=[];self.reads=0
            def html(self,path):
                self.opens.append((self.base,path))
                if len(self.opens)==1:raise Blocked('Cloudflare 사람 확인')
                return fixtures.catalog([1])
            def continue_html(self,path):
                self.reads+=1
                return fixtures.catalog([1])
        c=C(fixtures.BASE);self.engine.factory=lambda *args:c
        saved=[];alerts=[]
        self.config.update(auto_domain=True,domain_mode='modern_only',save_domain=saved.append,on_verification=alerts.append)
        with patch('engine.fetch_announcement',return_value=('<a href="https://toki35.com">신버전 바로가기</a><a href="https://newtoki2.org">구버전 바로가기</a>','https://notice.example/')):
            self.engine.start('analyze',['123'],self.config)
            self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
            self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
            self.assertEqual(saved,[]);self.assertEqual(c.opens,[('https://toki35.com','/novel/123')])
            self.assertEqual(c.reads,0)
            self.assertEqual(self.engine.snapshot()['verification_url'],'https://toki35.com/novel/123')
            self.engine.resume();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'completed',self.engine.snapshot())
        self.assertEqual(saved,['https://toki35.com']);self.assertEqual(c.reads,1)
        self.assertEqual(len(c.opens),1,'confirmed first catalog must not be navigated again')
        self.assertTrue(all(base=='https://toki35.com' for base,path in c.opens))

    def test_cancel_during_rest_sends_no_more_requests(self):
        c=fixtures.FakeClient(fixtures.BASE);self.engine.factory=lambda *args:c
        self.config.update(batch_size=1,batch_pause=60)
        self.engine.start('download',['123'],self.config,['1001','1002'])
        self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
        self.wait(lambda:self.engine.snapshot().get('phase')=='batch_rest')
        self.assertEqual(c.calls,['1001'])
        self.engine.cancel();self.engine.thread.join(2)
        self.assertFalse(self.engine.thread.is_alive())
        self.assertEqual(self.engine.snapshot()['status'],'canceled')
        self.assertEqual(c.calls,['1001'])
        self.assertEqual(self.engine.snapshot()['completed'],1)

    def test_pending_after_human_resume_never_navigates_again(self):
        c=self.challenge_client()
        c.continue_body=Mock(side_effect=[Pending('확인한 본문 로딩 중'),fixtures.TEXT])
        self.engine.start('download',['123'],self.config,['1002'])
        self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.engine.resume();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'completed')
        self.assertEqual(c.normal,['1002'])
        self.assertEqual(c.continue_body.call_count,2)
