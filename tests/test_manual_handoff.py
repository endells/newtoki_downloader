import sys
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
from engine import Engine, korean_day
from parser import DailyVerificationRequired, Blocked, AccessLimited, Locked, Pending, check_page, validate_body

NOTICE='일일 조회 인증이 필요합니다. 일반 소설 뷰어에서 인증 후 다시 열어주세요.'
SOURCE='<a href="https://newtoki1.org">구버전 바로가기</a><a href="https://toki35.com">신버전 바로가기</a>'


class DailyNotice(unittest.TestCase):
    def test_explicit_notice_is_separate_from_hard_limits_and_other_gates(self):
        with self.assertRaises(DailyVerificationRequired):validate_body({'message':NOTICE})
        with self.assertRaises(DailyVerificationRequired):check_page('<div class="novel-loading">'+NOTICE+'</div>')
        with self.assertRaises(DailyVerificationRequired):check_page('<main>'+NOTICE+'</main>')
        for state, kind in [({'message':NOTICE,'locked':True},Locked),
                            ({'message':NOTICE+' 일일 이용 한도 초과'},AccessLimited),
                            ({'message':NOTICE,'title':'Just a moment...'},Blocked),
                            ({'message':'퍼즐 조각을 맞춰주세요'},Blocked)]:
            with self.assertRaises(kind) as error:validate_body(state)
            self.assertNotIsInstance(error.exception,DailyVerificationRequired)

    def test_korean_calendar_boundary(self):
        with patch('engine.now',return_value='2026-10-22T14:59:59+00:00'):self.assertEqual(korean_day(),'2026-10-22')
        with patch('engine.now',return_value='2026-10-22T15:00:00+00:00'):self.assertEqual(korean_day(),'2026-10-23')


class ManualHandoff(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp

    def wait(self,predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if predicate():return
            threading.Event().wait(.01)
        self.fail(str(self.engine.snapshot()))

    def begin(self, **options):
        outer=self
        class Client(fixtures.FakeClient):
            confirmed=False;pending=False;resumes=0
            def __init__(self,*args):super().__init__(*args);self.navigation=[]
            def html(self,path):
                self.navigation.append((self.base,path))
                if self.base.startswith('https://toki') and path.endswith('/1002'):
                    if options.get('destination_limit'):raise AccessLimited('새주소 이용 한도')
                    # Even when this ordinary navigation succeeds, the worker must pause.
                    return '<main>새주소 인증 화면</main>'
                result=fixtures.catalog([1,2,3],title='다른 작품' if options.get('wrong_book') and self.base.startswith('https://toki') else '테스트 작품')
                if options.get('wrong_episode') and self.base.startswith('https://toki'):
                    result=result.replace('<div class="wr-num">2</div>','<div class="wr-num">99</div>')
                return result
            def body(self,nid,item):
                self.calls.append((self.base,item['id']))
                if item['no']==2 and self.base==fixtures.BASE:
                    if options.get('source_limit'):raise AccessLimited('일일 이용 한도')
                    raise DailyVerificationRequired(NOTICE)
                return fixtures.TEXT
            def continue_body(self,nid,item):
                self.resumes+=1
                if not self.confirmed:raise Blocked('새주소에서 직접 인증 필요')
                if self.pending:raise Pending('아직 로딩 중')
                return fixtures.TEXT
        c=Client(fixtures.BASE);self.engine.factory=lambda *args:c
        self.saved=[]
        self.config.update(auto_domain=True,save_domain=self.saved.append,domain_mode=options.get('mode','legacy_first'))
        notice=patch('engine.fetch_announcement',return_value=(SOURCE,'https://notice.example/'))
        notice.start();self.addCleanup(notice.stop)
        day=patch('engine.korean_day',return_value='2026-10-22')
        day.start();self.addCleanup(day.stop)
        self.engine.start('download',['123'],self.config,['1001','1002','1003'])
        self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
        return c

    def test_manual_destination_auth_resumes_same_episode_then_persists_until_next_korean_day(self):
        c=self.begin()
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        state=self.engine.snapshot()
        self.assertEqual(state['completed'],1)
        self.assertEqual(state['verification_url'],'https://toki35.com/novel/123/1002')
        self.assertEqual(state['verification_context'],'modern_handoff')
        self.assertEqual(c.resumes,0)
        self.assertEqual(self.saved,[])
        self.assertEqual(self.engine.history.query('SELECT * FROM verified_domain_handoff'),[])
        before=list(c.navigation)
        self.engine.resume()
        self.wait(lambda:c.resumes==1 and self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(c.navigation,before)
        self.assertEqual(self.saved,[])
        c.confirmed=True;c.pending=True
        self.engine.resume()
        self.wait(lambda:c.resumes==2 and self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(c.navigation,before)
        c.pending=False
        self.engine.resume();self.engine.thread.join(3)
        state=self.engine.snapshot()
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual((state['completed'],state['failed']),(3,0))
        self.assertEqual(c.calls,[(fixtures.BASE,'1001'),(fixtures.BASE,'1002'),('https://toki35.com','1003')])
        self.assertEqual(self.saved,['https://toki35.com'])
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),3)
        # Preference survives creating a fresh worker, and is scoped to a Korean date.
        fresh=Engine(self.root/'state.db');c.navigation=[];c.base='https://toki35.com'
        fresh.resolve(c,'123',self.config)
        self.assertEqual(c.navigation[0][0],'https://toki35.com')
        c.navigation=[]
        with patch('engine.korean_day',return_value='2026-10-23'):fresh.resolve(c,'123',self.config)
        self.assertEqual(c.navigation[0][0],fixtures.BASE)

    def test_cancel_or_wrong_book_does_not_save_route_or_unverified_episode(self):
        c=self.begin(wrong_book=True)
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        c.confirmed=True;self.engine.resume();self.engine.thread.join(3)
        state=self.engine.snapshot()
        self.assertEqual(state['status'],'failed',state)
        self.assertEqual(self.saved,[])
        self.assertEqual(self.engine.history.query('SELECT * FROM verified_domain_handoff'),[])
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),1)

    def test_cancel_during_human_wait_preserves_prior_download(self):
        self.begin()
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.engine.cancel();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'canceled')
        self.assertEqual(self.saved,[])
        self.assertEqual(self.engine.history.query('SELECT * FROM verified_domain_handoff'),[])
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),1)

    def test_destination_catalog_episode_mismatch_does_not_commit_body(self):
        c=self.begin(wrong_episode=True)
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        c.confirmed=True;self.engine.resume();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'failed')
        self.assertEqual(self.saved,[])
        self.assertEqual(self.engine.history.query('SELECT * FROM verified_domain_handoff'),[])
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),1)

    def test_hard_source_limit_does_not_open_another_domain(self):
        c=self.begin(source_limit=True);self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'limit_reached')
        self.assertTrue(all(base==fixtures.BASE for base,path in c.navigation))
        self.assertEqual(self.saved,[])

    def test_destination_limit_does_not_rotate_or_save(self):
        c=self.begin(destination_limit=True);self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'limit_reached')
        self.assertEqual(sum(base.startswith('https://toki') for base,path in c.navigation),1)
        self.assertEqual(self.saved,[])

    def test_explicit_legacy_only_keeps_existing_manual_verification(self):
        c=self.begin(mode='legacy_only')
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.assertTrue(all(base==fixtures.BASE for base,path in c.navigation))
        self.assertEqual(self.engine.snapshot()['verification_url'],fixtures.BASE+'/novel/123/1002')
