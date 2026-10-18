import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from parser import parse_catalog, viewer_config, discover_legacy, validate_body, SiteError, Blocked, Locked, Pending, origin
from engine import Engine, collect_catalog, History, atomic_write, valid_record, safe_path

BASE = 'https://newtoki1.org'
TEXT = '테스트용으로 직접 작성한 문장입니다. 실제 작품의 본문은 사용하지 않습니다.\n\n두 번째 문단이며 순서와 줄바꿈을 검사합니다.'


def catalog(numbers, pages=(1,), title='테스트 작품'):
    rows = ''.join('<li class="list-item"><div class="wr-num">%s</div><div class="wr-subject"><a class="item-subject" href="/novel/123/%s">%s화</a></div></li>' % (n, 1000+n, n) for n in numbers)
    links = ''.join('<a href="/novel/123?epage=%s">%s</a>' % (p,p) for p in pages)
    return '<title>작품</title><div class="theme-detail-title-line">' + title + '</div><div class="serial-list"><ul>' + rows + '</ul></div>' + links


def viewer(gate=None, nid='123', eid='1001'):
    cfg = {'novelId':nid,'episodeId':eid,'episodeNumber':1,'title':'테스트 1화','paidGate':gate or {},'token':'DO-NOT-RETURN'}
    return '<script id="theme-novel-viewer-data" type="application/json">'+json.dumps(cfg)+'</script><div data-theme-novel-content>본문 불러오는 중...</div><aside>로그인 / 포인트</aside>'


class ParserTests(unittest.TestCase):
    def test_numeric_sort_not_lexicographic(self):
        class C:
            base=BASE
            def check(self): pass
            def html(self,path):
                return catalog([10,9],(1,2)) if 'epage' not in path else catalog([2,1],(1,2))
        data=collect_catalog(C(),'123')
        self.assertEqual([e['no'] for e in data['episodes']],[1,2,9,10])
        self.assertEqual(data['pages'],[1,2])

    def test_discover_pages_beyond_first_window(self):
        class C:
            base=BASE
            def check(self): pass
            def html(self,path):
                if 'epage=3' in path:return catalog([1],(2,3))
                if 'epage=2' in path:return catalog([2],(1,2,3))
                return catalog([3],(1,2))
        self.assertEqual(collect_catalog(C(),'123')['pages'],[1,2,3])

    def test_repeated_page_fails(self):
        class C:
            base=BASE
            def check(self):pass
            def html(self,path):return catalog([1],(1,2))
        with self.assertRaisesRegex(SiteError,'반복'):collect_catalog(C(),'123')

    def test_mismatched_episode_path(self):
        with self.assertRaises(SiteError):parse_catalog(catalog([1]).replace('/123/1001','/456/1001'),BASE+'/novel/123')

    def test_off_origin_link_rejected(self):
        with self.assertRaises(SiteError):parse_catalog(catalog([1]).replace('href="/novel','href="https://evil.example/novel'),BASE+'/novel/123')

    def test_comments_do_not_trigger_paywall(self):
        result=viewer_config(viewer(),'123','1001')
        self.assertEqual(result['no'],1)
        self.assertNotIn('token',result)

    def test_actual_paid_gate(self):
        with self.assertRaises(Locked):viewer_config(viewer({'locked':True,'cost':10}),'123','1001')

    def test_wrong_viewer_id(self):
        with self.assertRaises(SiteError):viewer_config(viewer(),'123','1002')

    def test_placeholder_not_download(self):
        with self.assertRaises(Pending):validate_body({'rendered':False,'message':'본문 불러오는 중...'})
        with self.assertRaises(Pending):validate_body({'rendered':True,'text':'본문 불러오는 중...'})

    def test_security_notice_not_download(self):
        with self.assertRaises(Blocked):validate_body({'rendered':False,'message':'본문 보안 검증에 실패했습니다.'})

    def test_rendered_body_and_images(self):
        self.assertEqual(validate_body({'rendered':True,'text':TEXT,'images':0}),TEXT)
        with self.assertRaises(SiteError):validate_body({'rendered':True,'text':TEXT,'images':1})

    def test_country_block(self):
        with self.assertRaises(Blocked):parse_catalog('<title>Access denied</title>KR_ONLY',BASE+'/novel/123')

    def test_legacy_only(self):
        html='<a href="https://toki34.com">신버전</a><a href="https://newtoki2.org">구버전 바로가기</a>'
        self.assertEqual(discover_legacy(html,BASE),'https://newtoki2.org')
        with self.assertRaises(SiteError):discover_legacy('<a href="https://toki34.com">바로가기</a>',BASE)
        with self.assertRaises(SiteError):discover_legacy(html+'<a href="https://newtoki3.org">구버전</a>',BASE)

    def test_origin_rejects_secrets_ports_and_other_hosts(self):
        for u in ['http://newtoki1.org','https://x:newtoki1.org@evil.example','https://newtoki1.org:444','https://newtoki1.org.evil.example']:
            with self.assertRaises(SiteError):origin(u)


class FakeClient:
    locked_at=None
    def __init__(self,base,*args):
        self.base=base;self.calls=[];self.keep_tab=False
    def check(self):pass
    def html(self,path):return catalog([3,2,1])
    def body(self,nid,item):
        self.calls.append(item['id'])
        if item['no']==self.locked_at:raise Locked('잠금 회차')
        return TEXT
    def close(self):pass


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config={'download_path':str(self.root/'books'),'base_url':BASE,'browser_url':'http://localhost:9222',
                     'request_delay':2,'timeout':10,'auto_domain':False,'max_per_title':1,'episode_digits':3,'make_epub':True}
        self.engine=Engine(self.root/'state.db',factory=FakeClient)

    def run_job(self,selected=None):
        self.engine.start('download',['123'],self.config,selected)
        self.engine.thread.join(5)
        self.assertFalse(self.engine.thread.is_alive())
        return self.engine.snapshot()

    def test_manual_not_capped_and_epub_xml(self):
        state=self.run_job(['1001','1002','1003'])
        self.assertEqual(state['completed'],3)
        self.assertEqual(state['status'],'completed')
        paths=list((self.root/'books').rglob('*.txt'));self.assertEqual(len(paths),3)
        self.assertTrue(paths[0].read_text().split('\n')[0])
        epub=next((self.root/'books').rglob('*.epub'))
        with zipfile.ZipFile(epub) as z:
            self.assertEqual(z.infolist()[0].filename,'mimetype')
            self.assertEqual(z.infolist()[0].compress_type,0)
            for name in z.namelist():
                if name.endswith(('.xml','.opf','.xhtml')):ET.fromstring(z.read(name))
        self.assertEqual(self.run_job(['1001'])['completed'],1)

    def test_auto_limit_and_history_skip(self):
        self.assertEqual(self.run_job()['completed'],1)
        state=self.run_job();self.assertEqual(state['completed'],1);self.assertEqual(state['skipped'],1)

    def test_zero_byte_repaired(self):
        self.run_job()
        path=next((self.root/'books').rglob('*.txt'));path.write_bytes(b'')
        self.assertEqual(self.run_job()['completed'],1)
        self.assertGreater(path.stat().st_size,30)

    def test_locked_stops_before_later_episode(self):
        class LockedClient(FakeClient):locked_at=2
        self.engine.factory=LockedClient
        state=self.run_job(['1001','1002','1003'])
        self.assertEqual(state['status'],'locked');self.assertEqual(state['completed'],1)
        self.assertEqual(len(list((self.root/'books').rglob('*.txt'))),1)
        self.assertEqual([r['status'] for r in self.engine.history.rows('123')],['completed','locked'])

    def test_pending_does_not_write_fake_body(self):
        class PendingClient(FakeClient):
            def body(self,*a):raise Pending('본문 대기')
        self.engine.factory=PendingClient
        state=self.run_job(['1001'])
        self.assertEqual(state['status'],'body_pending')
        self.assertEqual(list((self.root/'books').rglob('*.txt')),[])
        self.assertEqual(self.engine.history.rows('123'),[])

    def test_symlink_escape(self):
        outside=self.root/'outside';outside.mkdir()
        root=self.root/'books';root.mkdir();(root/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(SiteError):safe_path(root,'escape/file.txt')


if __name__=='__main__':unittest.main()
