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


if __name__=='__main__':unittest.main()
