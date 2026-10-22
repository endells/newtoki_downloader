import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
import urllib.error
import socket
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_client import BrowserClient, BrowserConnectionError, NavigationPending, BODY_EXPRESSION
from parser import SiteError

class BrowserDiagnostics(unittest.TestCase):
    def test_discovery_reports_stage_address_and_actionable_reason_without_response_body(self):
        cases=[(urllib.error.URLError(ConnectionRefusedError(111,'Connection refused')),'연결 거부'),
               (urllib.error.URLError(socket.gaierror(-2,'not found')),'호스트 이름'),
               (urllib.error.URLError(TimeoutError('timeout')),'시간 초과'),
               (urllib.error.HTTPError('http://localhost',502,'Bad gateway',{},None),'HTTP 502'),
               (urllib.error.HTTPError('http://localhost',404,'Not Found',{},None),'HTTP 404')]
        for error, expected in cases:
            with self.subTest(expected=expected):
                c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9223')
                with patch('browser_client.urllib.request.build_opener') as opener:
                    opener.return_value.open.side_effect=error
                    with self.assertRaises(BrowserConnectionError) as caught:c.discovery('/json/version')
                self.assertIn(expected,str(caught.exception))
                self.assertIn('GET /json/version',str(caught.exception))
                self.assertIn('http://127.0.0.1:9223',str(caught.exception))

    def test_connection_check_is_readonly_and_rejects_wrong_port_content(self):
        c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9223')
        c.discovery=Mock(return_value={'Browser':'Chrome/123.0','webSocketDebuggerUrl':'ws://localhost/?token=do-not-show'})
        c.call=Mock();c.connect=Mock()
        self.assertEqual(c.check_connection(),'Chrome/123.0')
        c.discovery.assert_called_once_with('/json/version')
        c.call.assert_not_called();c.connect.assert_not_called()
        self.assertIsNone(c.target)
        c.discovery.return_value={'unrelated':'json'}
        with self.assertRaisesRegex(BrowserConnectionError,'JSON 응답'):c.check_connection()
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=b'<html>secret session token=hidden</html>'
        with patch('browser_client.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=response
            del c.discovery
            with self.assertRaises(BrowserConnectionError) as caught:c.discovery('/json/version')
        self.assertNotIn('hidden',str(caught.exception))
        self.assertIn('CDP JSON 응답 형식 오류',str(caught.exception))

    def test_javascript_error_details_are_preserved_and_redacted(self):
        c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222')
        c.call=Mock(return_value={'exceptionDetails':{'exception':{'description':"TypeError: token=secretvalue https://newtoki1.org/?token=private\n    at secret stack"}}})
        with self.assertRaisesRegex(SiteError,'TypeError') as caught:c.evaluate('1')
        self.assertNotIn('secretvalue',str(caught.exception))
        self.assertNotIn('private',str(caught.exception))
        self.assertNotIn('secret stack',str(caught.exception))
        self.assertIn('Runtime.evaluate',str(caught.exception))

    def test_transient_context_error_is_distinguished(self):
        c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222')
        class WS:
            def send(self,raw):self.ident=json.loads(raw)['id']
            def recv(self):return json.dumps({'id':self.ident,'error':{'code':-32000,'message':'Execution context was destroyed.'}})
        c.ws=WS()
        with self.assertRaises(NavigationPending):c.evaluate('1')

    def test_navigation_retries_only_context_loss_and_empty_document(self):
        c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222')
        c.connect=Mock();c.call=Mock(return_value={});c.stop=Mock();c.stop.is_set.return_value=False;c.stop.wait.return_value=False
        c.evaluate=Mock(side_effect=[NavigationPending('전환 중'),{'url':'https://newtoki1.org/novel/123','ready':'loading','html':''},{'url':'https://newtoki1.org/novel/123','ready':'complete','html':'<title>목록</title>'}])
        self.assertEqual(c.html('/novel/123'),'<title>목록</title>')
        self.assertEqual(c.evaluate.call_count,3)
        c.evaluate=Mock(side_effect=SiteError('TypeError: persistent'))
        with self.assertRaisesRegex(SiteError,'persistent'):c.html('/novel/123')
        self.assertEqual(c.evaluate.call_count,1)

    @unittest.skipUnless(shutil.which('node'),'Node only needed for JS regression test')
    def test_actual_null_dom_race_old_script_throws_new_script_survives(self):
        c=BrowserClient('https://newtoki1.org','http://127.0.0.1:9222')
        c.connect=Mock();c.call=Mock(return_value={})
        c.evaluate=Mock(return_value={'url':'https://newtoki1.org/novel/123','ready':'complete','html':'<title>x</title>'})
        c.html('/novel/123')
        expression=c.evaluate.call_args.args[0]
        js="const document={documentElement:null,body:null,readyState:'loading',title:'',querySelector:()=>null,getElementById:()=>null};const location={href:'https://newtoki1.org/novel/123'};let threw=false;try{document.documentElement.outerHTML;}catch(e){threw=true;}if(!threw)throw Error('old race not reproduced');const state="+expression+";if(state.html!=='')throw Error('empty document expected');const body="+BODY_EXPRESSION+";if(body.rendered)throw Error('not loaded');"
        subprocess.run(['node','-e',js],check=True,capture_output=True)
