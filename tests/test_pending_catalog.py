"""Synthetic fixture reproducing a disabled work-page link for a pending episode."""
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
from parser import parse_catalog, SiteError
from browser_client import BrowserClient, LOAD_NEWER_EXPRESSION
from engine import collect_catalog

BASE = 'https://toki35.com'


def catalog(numbers, total=None, newer=False, pending=(3,)):
    rows = []
    for no in numbers:
        unready = no in pending
        href = BASE + '/novel/123' + ('' if unready else '/' + str(1000+no))
        rows.append('<li class="novel-ep-row%s" data-ep="%s" data-episode-id="%s"><a class="novel-ep-link" href="%s" %s><span class="ne-num">%s화</span><span class="ne-title">테스트 %s</span></a></li>' %
                    (' novel-ep--not-ready' if unready else '', no, 1000+no, href, 'aria-disabled="true" tabindex="-1"' if unready else '', no, no))
    return ('<div class="novel-detail"><h1>테스트 작품</h1></div>'
            + ('<div class="novel-episode-head">에피소드 (<!-- -->%s<!-- -->화)</div>' % total if total else '')
            + '<ul class="novel-eps" data-novel-id="123">' + ''.join(rows) + '</ul>'
            + ('<button>더 최신 회차 보기</button>' if newer else ''))


class PendingCatalog(unittest.TestCase):
    def test_disabled_pending_row_retains_id_number_without_navigable_url(self):
        book = parse_catalog(catalog([1,2,3,4]), BASE+'/novel/123')
        item = next(e for e in book['episodes'] if e['no']==3)
        self.assertEqual((item['id'], item['pending'], item['url']), ('1003', True, ''))
        self.assertFalse(book['episodes'][0]['pending'])

    def test_incomplete_or_conflicting_markers_do_not_relax_link_validation(self):
        original = catalog([3])
        for html in [original.replace(' novel-ep--not-ready', ''), original.replace('aria-disabled="true"', ''),
                     original.replace('data-episode-id="1003"', 'data-episode-id="invalid"'),
                     original.replace('data-ep="3"', 'data-ep="99"'),
                     original.replace('href="'+BASE, 'href="https://example.com'),
                     original.replace('href="'+BASE+'/novel/123', 'href="'+BASE+'/novel/456')]:
            with self.subTest(html=html), self.assertRaises(SiteError):
                parse_catalog(html, BASE+'/novel/123')

    def test_overlapping_300_episode_windows_merge_all_430_with_pending_row(self):
        first = catalog(range(300,0,-1), total=430, newer=True)
        final = catalog(range(430,130,-1), total=430)
        c = BrowserClient(BASE, 'http://localhost:9222', delay=0)
        c.html = Mock(return_value=first)
        c.continue_html = Mock(return_value=final)
        c.evaluate = Mock(return_value=True)
        book = collect_catalog(c, '123', read_page=lambda path: c.wait_catalog(path, c.html(path)))
        self.assertEqual([e['no'] for e in book['episodes']], list(range(1,431)))
        self.assertEqual(sum(e['pending'] for e in book['episodes']), 1)
        self.assertFalse(book['has_more'])
        c.evaluate.assert_called_once_with(LOAD_NEWER_EXPRESSION)


class PendingCatalogEngine(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp

    def setup_client(self, pending=(3,)):
        class Client(fixtures.FakeClient):
            def html(self, path):return catalog([1,2,3,4], pending=pending)
        c = Client(BASE)
        self.engine.factory = lambda *args:c
        self.config['base_url'] = BASE
        return c

    def run_job(self, kind='download'):
        self.engine.start(kind, ['123'], self.config, ['1001','1002','1003','1004'] if kind=='download' else None)
        self.engine.thread.join(3)
        self.assertEqual(self.engine.snapshot()['status'], 'completed', self.engine.snapshot())

    def test_pending_is_missing_without_body_request_and_recovers_next_run(self):
        c = self.setup_client();self.run_job()
        self.assertEqual(c.calls, ['1001','1002','1004'])
        self.assertEqual(self.engine.snapshot()['skipped'], 1)
        row = next(r for r in self.engine.history.rows('123') if r['episode_id']=='1003')
        self.assertEqual(row['status'], 'missing')
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))), 3)
        self.assertTrue(self.engine.history.catalogs()[0]['episodes'][2]['pending'])
        c = self.setup_client(pending=());self.run_job()
        self.assertEqual(c.calls, ['1003'])
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))), 4)

    def test_analysis_preserves_preexisting_file_for_pending_episode(self):
        self.setup_client(pending=());self.run_job()
        before = next(r for r in self.engine.history.rows('123') if r['episode_id']=='1003')
        data = Path(before['path']).read_bytes()
        c = self.setup_client();self.run_job('analyze')
        item = next(e for e in self.engine.snapshot()['analysis']['episodes'] if e['no']==3)
        self.assertTrue(item['pending']);self.assertTrue(item['have'])
        self.assertEqual(Path(before['path']).read_bytes(), data)
        self.assertEqual(next(r for r in self.engine.history.rows('123') if r['episode_id']=='1003')['status'], 'completed')
        self.assertEqual(c.calls, [])


class PendingCatalogUI(unittest.TestCase):
    def test_pending_row_is_disabled_but_saved_file_still_shows_saved(self):
        import subprocess
        import shutil
        if not shutil.which('node'):self.skipTest('Node unavailable')
        source=(Path(__file__).resolve().parents[1]/'templates/newtoki_basic.html').read_text()
        cell=source[source.index('function cell('):source.index('async function recentBooks(')]
        draw=source[source.index('function draw('):source.index('const labels=')]
        script="""
const assert=require('assert');
function element(){return {children:[],textContent:'',dataset:{},style:{setProperty(){}},appendChild(n){this.children.push(n)},replaceChildren(){this.children=[]},addEventListener(){}};}
const nodes={};const $=id=>nodes[id]||(nodes[id]=element());
const document={createElement:element};let analysis;function count(){}
"""+cell+draw+"""
draw({title:'test',pages:[1],episodes:[
 {id:'1',no:1,title:'normal',pending:false,have:false,locked:false},
 {id:'2',no:2,title:'pending',pending:true,have:false,locked:false},
 {id:'3',no:3,title:'saved',pending:true,have:true,locked:false}
]});
const rows=$('episodes').children;
assert.equal(rows[0].children[0].children[0].checked,true);
assert.equal(rows[1].children[0].children[0].disabled,true);
assert.equal(rows[1].children[0].children[0].checked,false);
assert.equal(rows[1].children[3].textContent,'준비중 · 미저장');
assert.equal(rows[2].children[3].textContent,'저장됨');
assert($('book-title').textContent.includes('준비중 1'));
"""
        result=subprocess.run(['node','-e',script],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
