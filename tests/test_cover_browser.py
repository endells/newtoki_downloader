import base64
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_client import BrowserClient
from parser import SiteError

class CoverBrowserTests(unittest.TestCase):
    def test_loaded_cover_resource_bytes_without_new_http_request(self):
        c=BrowserClient.__new__(BrowserClient)
        c.timeout=10;c.check=lambda:None;c.evaluate=lambda expression:True
        payload=b'\x89PNG\r\n\x1a\nfixture'
        calls=[]
        def call(method,params=None):
            calls.append(method)
            if method=='Page.getResourceTree':return {'frameTree':{'frame':{'id':'main'},'resources':[{'url':'https://cdn.example/cover.png','type':'Image'}]}}
            return {'base64Encoded':True,'content':base64.b64encode(payload).decode()}
        c.call=call
        self.assertEqual(c.cover('https://cdn.example/cover.png'),payload)
        self.assertEqual(calls,['Page.getResourceTree','Page.getResourceContent'])
        with self.assertRaises(SiteError):c.cover('http://cdn.example/cover.png')
