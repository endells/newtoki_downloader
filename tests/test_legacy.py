from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import Engine
from parser import SiteError

CATALOG='<div class="theme-detail-title-line">테스트 작품</div><div class="serial-list"><li class="list-item"><span class="wr-num">1</span><a class="item-subject" href="/novel/123/1001">1화</a></li></div>'

class Legacy(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.engine=Engine(Path(self.tmp.name)/'state.db')

    def test_only_legacy_candidates_are_visited(self):
        calls=[];saved=[]
        class Client:
            base='https://newtoki1.org'
            def check(self):pass
            def html(self,path):
                calls.append(self.base)
                if self.base=='https://newtoki1.org':raise SiteError('접속 실패')
                return CATALOG
        c=Client()
        with patch('engine.fetch_announcement',return_value=('<a href="https://toki35.com">신버전 바로가기</a><a href="https://newtoki2.org">구버전 바로가기</a>','https://notice.example/')):
            self.engine.resolve(c,'123',{'save_domain':saved.append})
        self.assertEqual(calls,['https://newtoki1.org','https://newtoki2.org'])
        self.assertEqual(saved,['https://newtoki2.org'])

    def test_legacy_ui_and_settings_are_independent(self):
        from test_ff import FFTests
        fixture=FFTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        conf=fixture.module.config()
        self.assertEqual(conf['base_url'],'https://newtoki1.org')
        self.assertTrue(conf['download_path'].endswith('/newtoki_legacy'))
        self.assertTrue(fixture.module.engine.history.path.endswith('newtoki_downloader_legacy_history.db'))
        with self.assertRaises(Exception):fixture.module.validate(dict(conf,base_url='https://toki35.com'))
        with fixture.app.test_request_context('/'):
            html=fixture.module.process_menu('manual',None)
        self.assertIn('/newtoki_downloader_legacy/ajax/basic/command',html)

    def test_legacy_redirect_never_accepts_modern_destination(self):
        from browser_client import BrowserClient
        c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222')
        c.accept_location('https://newtoki2.org/novel/123','/novel/123')
        self.assertEqual(c.base,'https://newtoki2.org')
        with self.assertRaises(SiteError):c.accept_location('https://toki35.com/novel/123','/novel/123')
