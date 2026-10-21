import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_client import BrowserClient, endpoint
from parser import SiteError, Blocked, validate_body


class BrowserTests(unittest.TestCase):
    def test_cdp_event_before_result(self):
        class WS:
            def send(self,data):self.sent=json.loads(data)
            def recv(self):
                self.i=getattr(self,'i',0)+1
                return json.dumps({'method':'Page.loadEventFired'}) if self.i==1 else json.dumps({'id':self.sent['id'],'result':{'value':'ok'}})
        c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222');c.ws=WS()
        self.assertEqual(c.call('Page.enable'),{'value':'ok'})

    def test_public_cdp_rejected(self):
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',9222))]):
            with self.assertRaises(SiteError):endpoint('http://example.com:9222')

    def test_readonly_body_expression(self):
        from browser_client import BODY_EXPRESSION
        self.assertIn('shadowRoot',BODY_EXPRESSION)
        for forbidden in ('fetch(','.click(','.cookie','unlockApiPath','crypto.'):
            self.assertNotIn(forbidden,BODY_EXPRESSION)

    def test_challenge_after_reload(self):
        with self.assertRaises(Blocked):validate_body({'rendered':False,'title':'Just a moment...','message':'Checking your browser'})

    def test_confirmed_modern_catalog_read_without_navigation(self):
        from unittest.mock import Mock
        from test_core import catalog
        c=BrowserClient('https://toki35.com','http://127.0.0.1:9222')
        c.call=Mock(side_effect=AssertionError('must not navigate'))
        c.evaluate=Mock(return_value=dict(url='https://toki35.com/novel/123?epage=2',ready='complete',html=catalog([2])))
        self.assertEqual(c.continue_html('/novel/123?epage=2'),catalog([2]))
        c.call.assert_not_called()
        with self.assertRaises(Blocked):c.continue_html('/novel/123')
        c.evaluate.return_value=dict(url='https://toki35.com/novel/123',ready='complete',html='<title>Just a moment...</title>Verify you are human')
        with self.assertRaises(Blocked):c.continue_html('/novel/123')


    def test_modern_redirect_accepts_only_same_work_and_query(self):
        c=BrowserClient('https://toki34.com','http://127.0.0.1:9222')
        c.accept_location('https://toki35.com/novel/123?epage=2','/novel/123?epage=2')
        self.assertEqual(c.base,'https://toki35.com')
        for target in ('https://toki36.com/novel/456?epage=2','https://toki36.com/novel/123?epage=3','https://newtoki1.org/novel/123?epage=2','https://evil.example/novel/123?epage=2'):
            with self.subTest(target=target),self.assertRaises(SiteError):
                c.accept_location(target,'/novel/123?epage=2')
            self.assertEqual(c.base,'https://toki35.com')

    def test_catalog_wait_reads_rendered_dom_without_new_navigation(self):
        from unittest.mock import Mock
        from test_core import catalog
        c=BrowserClient('https://toki35.com','http://127.0.0.1:9222')
        c.continue_html=Mock(return_value=catalog([1]));c.stop.wait=Mock(return_value=False)
        self.assertEqual(c.wait_catalog('/novel/123','<title>loading</title>'),catalog([1]))
        c.continue_html.assert_called_once_with('/novel/123')

    def test_unknown_catalog_structure_keeps_tab_and_requests_html(self):
        c=BrowserClient('https://toki35.com','http://127.0.0.1:9222');c.timeout=1
        with patch('browser_client.time.monotonic',side_effect=[0,2]):
            with self.assertRaisesRegex(SiteError,'HTML 파일'):
                c.wait_catalog('/novel/123','<h1>unknown modern markup</h1>')
        self.assertTrue(c.keep_tab)

if __name__=='__main__':unittest.main()
