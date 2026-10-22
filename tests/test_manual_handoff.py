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


def modern_catalog(numbers=(1,2,3), title='테스트 작품'):
    rows=''.join('<div class="novel-ep-row" data-ep="%s" data-episode-id="%s"><a class="novel-ep-link" href="https://toki34.com/novel/123/%s"><span class="ne-num">%s화</span><span class="ne-title">%s화</span></a></div>' % (n,2000+n,2000+n,n,n) for n in numbers)
    return '<div class="novel-detail"><h1>'+title+'</h1></div><div class="novel-eps" data-novel-id="123">'+rows+'</div>'


class ManualHandoff(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp

    def wait(self,predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            if predicate():return
            threading.Event().wait(.01)
        self.fail(str(self.engine.snapshot()))

    def begin(self, **options):
        class Client(fixtures.FakeClient):
            catalog_ok=False;pending=False
            def __init__(self,*args):
                super().__init__(*args);self.navigation=[];self.confirmed=set();self.resumes=[];self.catalog_resumes=0
            def destination(self):
                return modern_catalog((1,99,3) if options.get('wrong_episode') else (1,2,3),
                                      '다른 작품' if options.get('wrong_book') else '테스트 작품')
            def html(self,path):
                self.navigation.append((self.base,path))
                if self.base.startswith('https://toki'):
                    if options.get('destination_limit'):raise AccessLimited('새주소 이용 한도')
                    if not self.catalog_ok:raise Blocked('목록 사람 확인 필요')
                    return self.destination()
                return fixtures.catalog([1,2,3])
            def continue_html(self,path):
                self.catalog_resumes+=1
                if not self.catalog_ok:raise Blocked('목록 사람 확인 필요')
                return self.destination()
            def body(self,nid,item):
                self.calls.append((self.base,item['id']))
                if item['no']==2 and self.base==fixtures.BASE:
                    if options.get('source_limit'):raise AccessLimited('일일 이용 한도')
                    raise DailyVerificationRequired(NOTICE)
                if self.base.startswith('https://toki') and options.get('body_gates') and item['no'] not in self.confirmed:
                    raise Blocked('회차 사람 확인 필요')
                return fixtures.TEXT
            def continue_body(self,nid,item):
                self.resumes.append(item['id'])
                if item['no'] not in self.confirmed:raise Blocked('회차 사람 확인 필요')
                if self.pending:raise Pending('확인한 본문 로딩 중')
                return fixtures.TEXT
        c=Client(fixtures.BASE);self.engine.factory=lambda *args:c
        self.saved=[]
        self.config.update(auto_domain=True,save_domain=self.saved.append,domain_mode=options.get('mode','legacy_first'))
        notice=patch('engine.fetch_announcement',return_value=(SOURCE,'https://notice.example/'))
        notice.start();self.addCleanup(notice.stop)
        day=patch('engine.korean_day',return_value='2026-10-22')
        day.start();self.addCleanup(day.stop)
        self.engine.start('download',['123'],self.config,options.get('selected',['1001','1002','1003']))
        self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
        return c

    def open_catalog(self,c):
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(self.engine.snapshot()['verification_url'],'https://toki35.com/novel/123')
        c.catalog_ok=True;self.engine.resume()

    def test_catalog_first_remaps_ids_and_pauses_at_each_episode(self):
        c=self.begin(body_gates=True)
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(self.engine.snapshot()['completed'],1)
        self.assertEqual(c.calls,[(fixtures.BASE,'1001'),(fixtures.BASE,'1002')])
        self.assertEqual(self.saved,[])
        navigation=list(c.navigation)
        self.engine.resume()
        self.wait(lambda:c.catalog_resumes==1 and self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(c.navigation,navigation)
        self.open_catalog(c)
        for no in (2,3):
            self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting' and self.engine.snapshot()['episode_no']==no)
            self.assertEqual(self.engine.snapshot()['verification_url'],'https://toki35.com/novel/123/%s'%(2000+no))
            before=list(c.calls)
            self.engine.resume()
            self.wait(lambda:str(2000+no) in c.resumes and self.engine.snapshot()['status']=='verification_waiting')
            self.assertEqual(c.calls,before)
            c.confirmed.add(no);self.engine.resume()
        self.engine.thread.join(3)
        state=self.engine.snapshot()
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual((state['completed'],state['failed']),(3,0))
        self.assertEqual(c.calls,[(fixtures.BASE,'1001'),(fixtures.BASE,'1002'),('https://toki35.com','2002'),('https://toki35.com','2003')])
        self.assertEqual(self.saved,['https://toki35.com'])
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),3)
        completed=[r['episode_id'] for r in self.engine.history.rows('123') if r['status']=='completed']
        self.assertEqual(sorted(completed),['2001','2002','2003'])
        import zipfile
        with zipfile.ZipFile(next((self.root/'books').rglob('*.epub'))) as z:
            chapters=[n for n in z.namelist() if n.startswith('OEBPS/chapter-')]
            self.assertEqual(len(chapters),3,z.namelist())
        fresh=Engine(self.root/'state.db');c.navigation=[]
        fresh.resolve(c,'123',self.config)
        self.assertEqual(c.navigation[0][0],'https://toki35.com')
        c.navigation=[]
        with patch('engine.korean_day',return_value='2026-10-23'):fresh.resolve(c,'123',self.config)
        self.assertEqual(c.navigation[0][0],fixtures.BASE)

    def test_resume_before_body_loaded_waits_again_without_navigation(self):
        c=self.begin(selected=['1001','1002'],body_gates=True)
        self.open_catalog(c)
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting' and self.engine.snapshot()['episode_no']==2)
        c.confirmed.add(2);c.pending=True
        before=list(c.calls)
        self.engine.resume()
        self.wait(lambda:len(c.resumes)>=2 and self.engine.snapshot()['status']=='verification_waiting')
        self.assertEqual(c.calls,before)
        c.pending=False;self.engine.resume();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'completed',self.engine.snapshot())
        self.assertEqual(c.calls,before)

    def test_selected_range_stays_selected_after_catalog_reload(self):
        c=self.begin(selected=['1001','1002']);self.open_catalog(c);self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'completed',self.engine.snapshot())
        self.assertEqual(c.calls[-1],('https://toki35.com','2002'))
        self.assertNotIn(('https://toki35.com','2003'),c.calls)
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),2)

    def test_file_added_during_catalog_wait_is_skipped_by_leading_number(self):
        c=self.begin()
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        folder=next((self.root/'books').rglob('*.txt')).parent
        external=folder/'002_다른곳에서 받은 제목.txt'
        external.write_text(fixtures.TEXT,encoding='utf-8')
        self.open_catalog(c);self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'completed',self.engine.snapshot())
        self.assertNotIn(('https://toki35.com','2002'),c.calls)
        self.assertEqual(c.calls[-1],('https://toki35.com','2003'))
        self.assertEqual(external.read_text(encoding='utf-8'),fixtures.TEXT)
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),3)

    def test_wrong_book_or_missing_number_stops_before_destination_body(self):
        for options in ({'wrong_book':True},{'wrong_episode':True}):
            with self.subTest(options=options):
                self.setUp()
                c=self.begin(**options);self.open_catalog(c);self.engine.thread.join(3)
                self.assertEqual(self.engine.snapshot()['status'],'failed',self.engine.snapshot())
                self.assertEqual(self.saved,[])
                self.assertEqual(self.engine.history.query('SELECT * FROM verified_domain_handoff'),[])
                self.assertTrue(all(base==fixtures.BASE for base,eid in c.calls))
                self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),1)

    def test_cancel_during_catalog_wait_preserves_prior_download(self):
        self.begin();self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.engine.cancel();self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'],'canceled')
        self.assertEqual(self.saved,[])
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
        c=self.begin(mode='legacy_only');self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.assertTrue(all(base==fixtures.BASE for base,path in c.navigation))
        self.assertEqual(self.engine.snapshot()['verification_url'],fixtures.BASE+'/novel/123/1002')

    def test_more_button_security_wait_preserves_collected_catalog_rows(self):
        from browser_client import BrowserClient
        from parser import parse_catalog
        from unittest.mock import Mock
        c=BrowserClient('https://toki35.com','http://localhost:9222',delay=0)
        def page(no,more=True):
            return modern_catalog((no,))+'<div class="novel-episode-head">목록 (3화)</div>'+('<button>이전 회차 더 보기</button>' if more else '')
        c.html=Mock(return_value=page(3))
        c.continue_html=Mock(side_effect=[Blocked('더보기 중 인증'),page(2),page(1,False)])
        c.evaluate=Mock(return_value=True)
        result=[]
        def work():
            result.append(self.engine.read_page(c,'/novel/123',catalog=True))
        self.engine.thread=threading.Thread(target=work)
        self.engine.thread.start()
        self.addCleanup(lambda:(self.engine.cancel(),self.engine.thread.join(2)))
        self.wait(lambda:self.engine.snapshot()['status']=='verification_waiting')
        self.engine.resume();self.engine.thread.join(3)
        self.assertFalse(self.engine.thread.is_alive())
        self.assertEqual([e['no'] for e in parse_catalog(result[0],c.base+'/novel/123')['episodes']],[1,2,3])
        c.html.assert_called_once()
        self.assertEqual(c.evaluate.call_count,2)


class ModernCatalogValidation(unittest.TestCase):
    def parse(self,html):
        from parser import parse_catalog
        return parse_catalog(html,'https://toki35.com/novel/123')

    def test_previous_modern_domain_links_use_current_origin(self):
        book=self.parse(modern_catalog())
        self.assertEqual(book['episodes'][1]['url'],'https://toki35.com/novel/123/2002')
        self.assertEqual(book['episodes'][1]['id'],'2002')

    def test_thousands_and_episode_suffix_are_supported(self):
        book=self.parse(modern_catalog((1000,)).replace('>1000화</span>','>1,000회</span>'))
        self.assertEqual(book['episodes'][0]['no'],1000)

    def test_wrong_work_number_and_unrelated_domain_still_rejected(self):
        from parser import SiteError
        for html,detail in [
            (modern_catalog().replace('/novel/123/','/novel/456/'),'경로'),
            (modern_catalog().replace('>2화</span>','>둘째</span>'),'번호 형식'),
            (modern_catalog().replace('https://toki34.com','https://newtoki1.org'),'도메인'),
            (modern_catalog().replace('https://toki34.com','https://example.com'),'도메인'),
            (modern_catalog().replace('data-ep="2"','data-ep="99"'),'표시 회차'),
        ]:
            with self.subTest(detail=detail),self.assertRaises(SiteError):self.parse(html)
