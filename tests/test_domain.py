import sys
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from domain import announcement_url, discover_candidates, fetch_announcement, DEFAULT_ANNOUNCEMENT
from engine import Engine
from parser import Blocked, Cancelled, SiteError, novel_id
from test_core import catalog

SOURCE='<a href="https://toki34.com">뉴토끼 바로가기</a><a href="https://newtoki2.org">구버전 바로가기</a><a href="https://toki33.com">과거주소</a>'

class Domains(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.engine=Engine(Path(self.tmp.name)/'history.db')
        self.saved=[];self.conf={'save_domain':self.saved.append,'announcement_url':DEFAULT_ANNOUNCEMENT,'domain_mode':'modern_only'}
        self.outcomes={};self.calls=[]
        outer=self
        class Client:
            base='https://newtoki1.org'
            def check(self):pass
            def html(self,path):
                outer.calls.append(self.base)
                result=outer.outcomes.get(self.base,catalog([1]))
                if isinstance(result,Exception):raise result
                return result
        self.client=Client()
        self.patcher=patch('engine.fetch_announcement',return_value=(SOURCE,DEFAULT_ANNOUNCEMENT))
        self.fetch=self.patcher.start();self.addCleanup(self.patcher.stop)

    def resolve(self):self.engine.resolve(self.client,'123',self.conf)

    def test_default_unified_mode_uses_legacy_first_even_after_modern_success(self):
        self.conf.pop('domain_mode')
        self.client.base='https://toki35.com'
        self.resolve()
        self.assertEqual(self.calls,['https://newtoki2.org'])
        self.assertEqual(self.saved,['https://newtoki2.org'])

    def test_legacy_failure_falls_back_to_modern(self):
        self.conf['domain_mode']='legacy_first'
        self.outcomes={k:SiteError('접속 실패') for k in ('https://newtoki1.org','https://newtoki2.org')}
        self.resolve()
        self.assertEqual(self.calls,['https://newtoki1.org','https://newtoki2.org','https://toki34.com'])
        self.assertEqual(self.saved,['https://toki34.com'])

    def test_legacy_only_does_not_visit_modern(self):
        self.conf['domain_mode']='legacy_only'
        self.outcomes={k:SiteError('접속 실패') for k in ('https://newtoki1.org','https://newtoki2.org')}
        with self.assertRaises(SiteError):self.resolve()
        self.assertEqual(self.calls,['https://newtoki1.org','https://newtoki2.org'])

    def test_browser_connection_failure_never_rotates_site_domains(self):
        from browser_client import BrowserConnectionError
        self.conf['domain_mode']='legacy_first'
        self.outcomes['https://newtoki1.org']=BrowserConnectionError('CDP 연결 거부')
        with self.assertRaisesRegex(BrowserConnectionError,'CDP 연결 거부'):self.resolve()
        self.assertEqual(self.calls,['https://newtoki1.org'])
        self.assertEqual(self.saved,[])
        self.assertEqual(self.client.base,'https://newtoki1.org')

    def test_limits_and_locks_never_switch_domains(self):
        from parser import AccessLimited, Locked
        self.conf['domain_mode']='legacy_first'
        for error in (AccessLimited('일일 한도'), Locked('로그인 필요'), Blocked('사람 확인')):
            with self.subTest(error=error.code):
                self.calls.clear()
                self.outcomes={'https://newtoki1.org':error}
                with patch.object(self.engine,'read_page',side_effect=lambda client,path,**kw:client.html(path)):
                    with self.assertRaises(type(error)):self.resolve()
                self.assertEqual(self.calls,['https://newtoki1.org'])
                self.assertEqual(self.saved,[])

    def test_legacy_structure_failure_can_use_modern_but_modern_tab_is_preserved(self):
        from browser_client import CatalogStructureError
        self.conf['domain_mode']='legacy_first'
        self.outcomes={k:CatalogStructureError('목록 구조 변경') for k in ('https://newtoki1.org','https://newtoki2.org')}
        with patch.object(self.engine,'read_page',side_effect=lambda client,path,**kw:client.html(path)):
            self.resolve()
        self.assertEqual(self.calls[-1],'https://toki34.com')

    def test_modern_first_without_visiting_legacy(self):
        self.resolve()
        self.assertEqual(self.calls,['https://toki34.com'])
        self.assertEqual(self.saved,['https://toki34.com'])
        self.assertEqual(self.engine.snapshot()['domain_change']['old'],'https://newtoki1.org')

    def test_saved_modern_precedes_stale_announced_address(self):
        self.client.base='https://toki35.com'
        self.outcomes['https://toki34.com']=SiteError('접속 실패')
        self.resolve()
        self.assertEqual(self.calls,['https://toki35.com'])
        self.assertEqual(self.saved,[])

    def test_legacy_errors_do_not_affect_modern_selection(self):
        self.outcomes={k:SiteError('접속 실패') for k in ('https://newtoki1.org','https://newtoki2.org')}
        self.resolve()
        self.assertEqual(self.calls,['https://toki34.com'])
        self.assertEqual(self.saved,['https://toki34.com'])

    def test_validated_redirect_destination_is_saved_instead_of_stale_announcement(self):
        def redirected(path):
            self.calls.append(self.client.base)
            self.client.base='https://toki35.com'
            return catalog([1])
        self.client.html=redirected
        self.resolve()
        self.assertEqual(self.saved,['https://toki35.com'])
        self.assertEqual(self.engine.snapshot()['base_url'],'https://toki35.com')
        self.assertEqual(self.engine.snapshot()['domain_change']['new'],'https://toki35.com')
        self.assertEqual(self.engine.snapshot()['domain_attempts'][-1]['url'],'https://toki35.com')

    def test_challenge_never_applied_and_previous_address_kept(self):
        self.outcomes={k:SiteError('접속 실패') for k in ('https://newtoki1.org','https://newtoki2.org')}
        self.outcomes['https://toki34.com']=Blocked('사람 확인 필요')
        with patch.object(self.engine,'read_page',side_effect=lambda client,path,**kw:client.html(path)):
            with self.assertRaisesRegex(Blocked,'사람 확인'):self.resolve()
        self.assertEqual(self.calls,['https://toki34.com'])
        self.assertEqual(self.client.base,'https://newtoki1.org')
        self.assertEqual(self.saved,[])
        self.assertEqual(self.engine.snapshot()['domain_attempts'][-1]['status'],'verification_required')

    def test_same_id_wrong_title_not_applied(self):
        self.engine.history.query('INSERT INTO books VALUES (?,?,?,?)',('123','테스트 작품','','now'))
        self.outcomes['https://toki34.com']=catalog([1],title='다른 작품')
        self.outcomes['https://toki35.com']=catalog([1],title='다른 작품')
        self.client.base='https://toki35.com'
        with self.assertRaises(SiteError):self.resolve()
        self.assertEqual(self.saved,[])
        self.assertEqual(len(self.calls),2)

    def test_announced_modern_is_used_if_saved_modern_is_unavailable(self):
        self.client.base='https://toki35.com'
        self.outcomes['https://toki35.com']=SiteError('접속 실패')
        self.resolve()
        self.assertEqual(self.calls,['https://toki35.com','https://toki34.com'])
        self.assertEqual(self.saved,['https://toki34.com'])

    def test_catalog_failure_reports_actual_redirect_destination(self):
        from browser_client import CatalogStructureError
        def failed_redirect(client,path,**kwargs):
            client.base='https://toki35.com'
            raise CatalogStructureError('현재 작업 탭을 유지합니다.')
        with patch.object(self.engine,'read_page',side_effect=failed_redirect):
            with self.assertRaises(SiteError):self.resolve()
        attempt=self.engine.snapshot()['domain_attempts'][0]
        self.assertEqual(attempt['url'],'https://toki35.com')
        self.assertEqual(attempt['requested_url'],'https://toki34.com')

    def test_cancel_does_not_try_other_domains(self):
        self.outcomes['https://toki34.com']=Cancelled('중지')
        with self.assertRaises(Cancelled):self.resolve()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.saved,[])

    def test_announcement_failure_keeps_working_current(self):
        self.fetch.side_effect=SiteError('안내 사이트 오류')
        self.client.base='https://toki35.com'
        self.resolve()
        self.assertEqual(self.calls,['https://toki35.com'])
        self.assertTrue(self.engine.snapshot()['warnings'])
        self.assertEqual(self.saved,[])

    def test_duplicate_candidates_only_checked_once(self):
        self.client.base='https://toki34.com'
        self.fetch.return_value=(SOURCE+SOURCE,DEFAULT_ANNOUNCEMENT)
        self.resolve()
        self.assertEqual(self.calls,['https://toki34.com'])

    def test_incompatible_modern_html_not_saved(self):
        self.outcomes={k:SiteError('실패') for k in ('https://newtoki1.org','https://newtoki2.org')}
        self.outcomes['https://toki34.com']='<title>다른 구조</title>'
        self.outcomes['https://toki35.com']='<title>다른 구조</title>'
        with self.assertRaises(SiteError):self.resolve()
        self.assertEqual(self.saved,[])

    def test_idn_url_and_custom_announcement(self):
        self.assertEqual(announcement_url('https://뉴토주소.com/'),DEFAULT_ANNOUNCEMENT)
        self.conf['announcement_url']='https://notice.example/latest'
        self.resolve()
        self.assertEqual(self.fetch.call_args.args[0],self.conf['announcement_url'])

    def test_public_https_only(self):
        for url in ['http://example.com/','https://127.0.0.1/','https://localhost/','https://user:secret@example.com/','https://foo.local/','https://example.com:444/']:
            with self.assertRaises(SiteError):announcement_url(url)
        with patch('domain.socket.getaddrinfo',return_value=[(2,1,6,'',('192.168.1.1',443))]):
            with self.assertRaisesRegex(SiteError,'공개 IP'):fetch_announcement('https://notice.example/')

    def test_candidates_ignore_ad_history_and_deduplicate(self):
        html=SOURCE+'<a href="https://evil.test">구버전 바로가기</a>'+SOURCE
        self.assertEqual(discover_candidates(html,DEFAULT_ANNOUNCEMENT),(['https://newtoki2.org'],['https://toki34.com']))
        self.assertEqual(novel_id('https://toki34.com/novel/123'),'123')

    def test_announcement_https_redirect_rechecked(self):
        import urllib.error
        import io
        from email.message import Message
        headers=Message();headers['Location']='https://127.0.0.1/'
        error=urllib.error.HTTPError('https://notice.example/',302,'Found',headers,io.BytesIO())
        with patch('domain.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]), patch('domain.urllib.request.build_opener') as build:
            build.return_value.open.side_effect=error
            with self.assertRaises(SiteError):fetch_announcement('https://notice.example/')
            self.assertEqual(build.return_value.open.call_count,1)
