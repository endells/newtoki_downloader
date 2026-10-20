import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
import test_ff as ff_fixture
from parser import SiteError

class ManualHistory(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    run_job=fixtures.EngineTests.run_job

    def analyze(self):
        self.engine.start('analyze',['123'],self.config)
        self.engine.thread.join(5)
        self.assertFalse(self.engine.thread.is_alive())
        return {e['id']:e['have'] for e in self.engine.snapshot()['analysis']['episodes']}

    def test_analyze_checks_actual_files_and_repairs_missing_or_corrupt(self):
        self.run_job(['1001'])
        self.assertTrue(self.analyze()['1001'])
        row=self.engine.history.rows('123')[0];path=Path(row['path'])
        for damaged in (b'',b'changed content'):
            path.write_bytes(damaged)
            self.assertFalse(self.analyze()['1001'])
            self.assertEqual(self.run_job(['1001'])['completed'],1)
            self.assertTrue(self.engine.snapshot()['analysis']['episodes'][0]['have'])
        path.unlink()
        self.assertFalse(self.analyze()['1001'])
        self.assertEqual(self.run_job(['1001'])['completed'],1)

    def test_stale_manual_selection_skips_existing_without_requesting_body(self):
        self.run_job(['1001'])
        class NoBody(fixtures.FakeClient):
            def body(self,*args):raise AssertionError('existing body should not be requested')
        self.engine.factory=NoBody
        result=self.run_job(['1001'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['completed'],0)
        self.assertEqual(result['skipped'],1)
        self.assertTrue(result['analysis']['episodes'][0]['have'])

    def test_partial_failure_refreshes_already_saved_rows(self):
        class BrokenSecond(fixtures.FakeClient):
            def body(self,nid,item):
                if item['id']=='1002':raise SiteError('second episode failed')
                return fixtures.TEXT
        self.engine.factory=BrokenSecond
        result=self.run_job(['1001','1002'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual([e['have'] for e in result['analysis']['episodes']],[True,False,False])

class ManualUI(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node needed for manual UI regression')
    def test_saved_rows_disabled_range_all_and_finish_refresh(self):
        fixture=ff_fixture.FFTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        with fixture.app.test_request_context('/'):
            html=fixture.module.process_menu('manual',None)
        script=re.search(r'<script>(.*?)</script>',html,re.S).group(1)
        ids=re.findall(r'\bid="([^"]+)"',html)
        harness=r'''
const vm=require('vm');
class El {
 constructor(tag='div'){this.tag=tag;this.children=[];this.handlers={};this.style={setProperty(){}};this.dataset={};this.checked=false;this.disabled=false;this.value='';this.textContent='';}
 appendChild(x){this.children.push(x);return x;}
 replaceChildren(){this.children=[];}
 addEventListener(k,f){this.handlers[k]=f;}
}
const elements=Object.fromEntries(IDS.map(id=>[id,new El()]));
function inputs(){const all=[];function walk(n){if(n.tag==='input')all.push(n);n.children.forEach(walk);}walk(elements.episodes);return all;}
const document={getElementById:id=>elements[id]||null,createElement:tag=>new El(tag),querySelectorAll:q=>q.startsWith('#episodes input')?inputs().filter(n=>(!q.includes(':checked')||n.checked)&&(!q.includes(':not(:disabled)')||!n.disabled)):[]};
let state={status:'completed',message:'완료',analysis:{id:'123',title:'작품',pages:[1],episodes:[{id:'1001',no:1,title:'1화',have:true,locked:false},{id:'1002',no:2,title:'2화',have:false,locked:false},{id:'1003',no:3,title:'3화',have:false,locked:true}]}};
const timers=[],requests=[];
const context={document,URLSearchParams,console,setTimeout:f=>timers.push(f),setInterval:()=>{},fetch:async(url,options)=>{const command=options.body.get('command');requests.push({command,ids:options.body.get('arg2')});return {ok:true,json:async()=>({ret:'success',state,msg:'작업 시작'})};}};
vm.runInNewContext(SCRIPT,context);
(async()=>{
 await new Promise(setImmediate);
 function assert(v,m){if(!v)throw Error(m);}
 let boxes=inputs();assert(boxes.length===3,'3 rows expected');assert(boxes[0].disabled&&!boxes[0].checked,'saved row excluded');assert(boxes[1].checked&&!boxes[1].disabled,'missing selected');assert(boxes[2].disabled&&!boxes[2].checked,'locked excluded');
 await elements['select-none'].handlers.click();assert(inputs().every(x=>!x.checked),'none');
 await elements['select-missing'].handlers.click();assert(inputs()[1].checked&&!inputs()[0].checked,'all missing only');
 elements['range-from'].value='1';elements['range-to'].value='3';await elements['select-range'].handlers.click();assert(!inputs()[0].checked&&inputs()[1].checked&&!inputs()[2].checked,'range skips saved and locked');
 inputs()[0].checked=true;await elements.download.handlers.click();assert(requests.at(-1).ids==='["1002"]','download filters disabled saved row');
 state.analysis.episodes[1].have=true;await timers.shift()();await new Promise(setImmediate);assert(inputs()[1].disabled&&!inputs()[1].checked,'finished row refreshed');assert(elements.download.disabled,'no remaining selectable rows');
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('IDS',json.dumps(ids)).replace('SCRIPT',json.dumps(script))
        result=subprocess.run(['node'],input=harness,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
