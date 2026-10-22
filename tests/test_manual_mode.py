"""Exercise FF command dispatch without importing the NAS-only framework."""
import ast
import json
from pathlib import Path
import secrets
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import SiteError, novel_id
import test_core as fixtures
from engine import korean_day


def module_class():
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'mod_basic.py').read_text())
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ModuleBasic')
    methods = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in ('manual_runtime', 'process_command')]
    reduced = ast.Module(body=[ast.ClassDef(name='ModuleBasic', bases=[], keywords=[], body=methods, decorator_list=[])], type_ignores=[])
    scope = dict(json=json, secrets=secrets, session={'newtoki_csrf': 'test'}, jsonify=lambda **kw: kw,
                 novel_id=novel_id, SiteError=SiteError)
    exec(compile(ast.fix_missing_locations(reduced), 'mod_basic.py', 'exec'), scope)
    return scope['ModuleBasic']


class ManualMode(unittest.TestCase):
    def setUp(self):
        self.module = module_class()()
        self.original = dict(domain_mode='legacy_first', auto_domain=False, save_domain=Mock())
        self.module.runtime = Mock(side_effect=lambda: dict(self.original))
        self.module.record_event = Mock()
        self.engine = Mock()
        self.module._engine = lambda: self.engine
        self.req = Mock(form={'csrf_token': 'test'})

    def test_both_commands_pin_family_without_changing_global_settings(self):
        for mode in ('legacy_only', 'modern_only'):
            with self.subTest(mode=mode):
                self.module.process_command('analyze', '123', mode, '', self.req)
                analyze = self.engine.start.call_args.args
                self.module.process_command('download', '123', '["1002"]', mode, self.req)
                download = self.engine.start.call_args.args
                self.assertEqual((analyze[0], download[0]), ('analyze', 'download'))
                self.assertEqual(download[3], ['1002'])
                for conf in (analyze[2], download[2]):
                    self.assertEqual(conf['domain_mode'], mode)
                    self.assertEqual(conf['manual_mode'], mode)
                    self.assertTrue(conf['auto_domain'])
                    self.assertIsNone(conf['save_domain'])
        self.assertEqual(self.original['domain_mode'], 'legacy_first')
        self.assertFalse(self.original['auto_domain'])
        self.original['save_domain'].assert_not_called()

    def test_invalid_mode_does_not_start_job(self):
        result = self.module.process_command('analyze', '123', 'bad', '', self.req)
        self.assertEqual(result['ret'], 'error')
        self.engine.start.assert_not_called()

    def test_domain_check_and_older_page_keep_existing_configuration(self):
        for command in ('domain', 'analyze'):
            self.module.process_command(command, '123', '', '', self.req)
            self.assertEqual(self.engine.start.call_args.args[2], self.original)


class ManualModeEngine(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp

    def test_manual_legacy_ignores_same_day_modern_preference_and_keeps_daily_gate(self):
        from parser import DailyVerificationRequired
        import time
        import threading
        class Client(fixtures.FakeClient):
            def html(self, path):
                return fixtures.catalog([1])
            def body(self, nid, item):
                self.calls.append((self.base, item['id']))
                raise DailyVerificationRequired('일일 조회 인증이 필요합니다.')
        c = Client('https://toki35.com')
        self.engine.factory = lambda *args: c
        self.engine.history.query('INSERT INTO verified_domain_handoff VALUES (1,?,?)', (korean_day(), c.base))
        self.config.update(auto_domain=True, domain_mode='legacy_only', manual_mode='legacy_only')
        with patch('engine.fetch_announcement', return_value=('<a href="https://newtoki1.org">구버전 바로가기</a><a href="https://toki35.com">신버전 바로가기</a>', 'https://notice.example/')):
            self.engine.start('download', ['123'], self.config, ['1001'])
            self.addCleanup(lambda: (self.engine.cancel(), self.engine.thread.join(2)))
            deadline = time.monotonic()+3
            while time.monotonic()<deadline and self.engine.snapshot()['status']=='running':
                threading.Event().wait(.01)
            state = self.engine.snapshot()
            self.assertEqual(state['status'], 'verification_waiting', state)
            self.assertEqual(state['manual_mode'], 'legacy_only')
            self.assertEqual(c.calls, [(fixtures.BASE, '1001')])
            self.engine.cancel();self.engine.thread.join(2)


class ManualModeUI(unittest.TestCase):
    def test_mode_change_invalidates_list_and_commands_carry_selected_family(self):
        import subprocess
        import shutil
        if not shutil.which('node'):self.skipTest('Node unavailable')
        source=(Path(__file__).resolve().parents[1]/'templates/newtoki_basic.html').read_text()
        post=source[source.index('async function post('):source.index('function bind(')]
        poll=source[source.index('async function poll('):source.index('function fileScope(')]
        handlers='\n'.join(line for line in source.splitlines() if line.startswith(("if($('manual-mode'))", "bind('analyze'", "bind('download'")))
        script="""
const assert=require('assert');
const handlers={},elements={};
const $=id=>elements[id]||(elements[id]={value:'',textContent:'',replaceChildren(){}});
$('manual-mode').value='legacy_only';
let analysis=null,rendered='',running=false,manualNeedsAnalysis=false,draws=0;
const book={id:'123',episodes:[],pages:[1]};
let state={status:'completed',analysis:book,manual_mode:'legacy_only'};
const calls=[];const endpoint='/command',csrf='test',page='manual';
const msg=()=>{},count=()=>{},verification=()=>{},recentBooks=async()=>{},setTimeout=()=>{};
const document={querySelectorAll:()=>[{value:'1002'}]};
const labels={completed:'완료'};
function bind(id,fn){handlers[id]=fn;}
function draw(b){analysis=b;draws++;}
$('manual-mode').addEventListener=(name,fn)=>handlers.change=fn;
const fetch=async(url,options)=>{const a=Object.fromEntries(options.body);calls.push(a);
 if(a.command==='analyze')state={status:'completed',analysis:book,manual_mode:a.arg2};
 return {ok:true,json:async()=>({ret:'success',msg:'ok',state})};};
"""+post+poll+handlers+"""
(async()=>{
 await poll();assert.equal(draws,1);
 $('manual-mode').value='modern_only';handlers.change();assert.equal(analysis,null);
 await poll();assert.equal(draws,1);
 $('manual-mode').value='legacy_only';handlers.change();await poll();assert.equal(draws,1);
 for(const mode of ['legacy_only','modern_only']){
  $('manual-mode').value=mode;handlers.change();await handlers.analyze();
  assert.equal(calls.at(-1).arg2,mode);await poll();assert(analysis);
  await handlers.download();assert.equal(calls.at(-1).arg3,mode);
  assert.equal(calls.at(-1).arg2,'["1002"]');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
