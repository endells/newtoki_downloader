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


if __name__=='__main__':unittest.main()
