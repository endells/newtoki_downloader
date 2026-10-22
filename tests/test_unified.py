"""Domain compatibility without rotating around access or usage restrictions."""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_client import BrowserClient, DomainUnavailable
from engine import Engine
from parser import AccessLimited, Blocked, Locked, SiteError, check_page, validate_body
from domain import domain_candidates
from test_core import catalog
import test_core as fixtures
from test_modern_catalog import modern


class Unified(unittest.TestCase):
    def test_legacy_redirect_keeps_work_and_page_and_stays_in_family(self):
        client=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222')
        client.accept_location('https://newtoki2.org/novel/123?epage=2','/novel/123?epage=2')
        self.assertEqual(client.base,'https://newtoki2.org')
        for target in ('https://newtoki3.org/novel/456?epage=2','https://newtoki3.org/novel/123?epage=1','https://toki35.com/novel/123?epage=2'):
            with self.assertRaises(SiteError):client.accept_location(target,'/novel/123?epage=2')

    def test_quota_not_misclassified_as_structure_failure(self):
        for message in ('하루 열람 제한을 초과했습니다', '일일 이용 한도를 초과했습니다', '접속 횟수 제한', 'Too many requests', 'Daily limit reached'):
            with self.subTest(message=message):
                with self.assertRaises(AccessLimited):check_page('<main>'+message+'</main>')
                with self.assertRaises(AccessLimited):check_page('<article class="novel-viewer"><div class="novel-loading">'+message+'</div></article>')
                with self.assertRaises(AccessLimited):validate_body({'message':message})
        check_page(catalog([1]).replace('</body>','<p>하루 열람 제한을 초과했습니다</p></body>'))

    def test_only_transport_navigation_error_is_eligible(self):
        for error, eligible in [('net::ERR_CONNECTION_RESET',True),('net::ERR_NAME_NOT_RESOLVED',True),('net::ERR_BLOCKED_BY_RESPONSE',False),('net::ERR_CERT_AUTHORITY_INVALID',False)]:
            client=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222')
            client.connect=Mock();client.call=Mock(return_value={'errorText':error})
            with self.assertRaises(SiteError) as raised:client.html('/novel/123')
            self.assertEqual(isinstance(raised.exception,DomainUnavailable),eligible)
            if error=='net::ERR_BLOCKED_BY_RESPONSE':
                self.assertIsInstance(raised.exception,Blocked)

    def test_candidates_exclude_failed_address_and_respect_mode(self):
        self.assertEqual(domain_candidates('https://newtoki1.org',[],[],exclude=('https://newtoki1.org',)),['https://toki35.com'])
        self.assertEqual(domain_candidates('https://toki35.com',[],[],'legacy_only'),['https://newtoki1.org'])
        with self.assertRaises(SiteError):domain_candidates('https://toki35.com',[],[],'invalid')

    def test_mid_download_transport_fallback_checks_episode_before_reading(self):
        for number, expected in ((1,None),(2,SiteError)):
            with self.subTest(number=number),tempfile.TemporaryDirectory() as tmp:
                engine=Engine(Path(tmp)/'history.db')
                client=Mock();client.base='https://newtoki1.org'
                client.body.side_effect=[DomainUnavailable('DNS 실패'),'안전한 테스트 본문']
                item={'id':'1001','no':1,'title':'테스트','locked':False}
                def resolve(*args,**kwargs):
                    self.assertEqual(kwargs['exclude'],('https://newtoki1.org',))
                    client.base='https://toki35.com'
                    return catalog([1])
                alternate={'episodes':[dict(item,no=number)]}
                with patch.object(engine,'resolve',side_effect=resolve),patch('engine.collect_catalog',return_value=alternate):
                    if expected:
                        with self.assertRaises(expected):engine.read_body(client,'123',item,config={'auto_domain':True})
                        self.assertEqual(client.body.call_count,1)
                    else:
                        self.assertEqual(engine.read_body(client,'123',item,config={'auto_domain':True}),'안전한 테스트 본문')
                        self.assertEqual(client.body.call_count,2)

    def test_generic_body_errors_and_limits_do_not_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(Path(tmp)/'history.db')
            client=Mock();client.base='https://newtoki1.org'
            with patch.object(engine,'resolve') as resolve:
                for exc in (AccessLimited('일일 제한'),Locked('로그인'),SiteError('본문 구조 변경')):
                    client.body.side_effect=exc
                    with self.assertRaises(type(exc)):engine.read_body(client,'123',{'id':'1001'},config={'auto_domain':True})
                resolve.assert_not_called()


class UnifiedJobs(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    run_job=fixtures.EngineTests.run_job

    def test_download_switch_keeps_saved_files_and_history_on_both_sites(self):
        calls=[]
        class Client(fixtures.FakeClient):
            def html(self,path):
                return modern([3,2,1]) if self.base.startswith('https://toki') else catalog([1,2,3])
            def body(self,nid,item):
                calls.append((self.base,item['id']))
                if self.base==fixtures.BASE and item['id']=='1002':
                    raise DomainUnavailable('연결 끊김')
                return fixtures.TEXT
        self.engine.factory=Client
        saved=[]
        self.config.update(auto_domain=True,save_domain=saved.append)
        with patch('engine.fetch_announcement',return_value=('<a href="https://toki35.com">신버전 바로가기</a>','https://notice.example/')):
            state=self.run_job(['1001','1002','1003'])
            self.assertEqual(state['status'],'completed',state)
            self.assertEqual(state['completed'],3)
            self.assertEqual(calls,[(fixtures.BASE,'1001'),(fixtures.BASE,'1002'),('https://toki35.com','1002'),('https://toki35.com','1003')])
            self.assertEqual(saved,['https://toki35.com'])
            self.config['base_url']='https://toki35.com'
            self.engine.start('analyze',['123'],self.config);self.engine.thread.join(5)
        state=self.engine.snapshot()
        self.assertEqual(state['status'],'completed',state)
        self.assertEqual(state['base_url'],fixtures.BASE)
        self.assertEqual([row['have'] for row in state['analysis']['episodes']],[True,True,True])

