"""Sanitized structural fixtures based on the supplied modern catalog HTML."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from parser import parse_catalog, SiteError, Blocked
from browser_client import BrowserClient, LOAD_MORE_EXPRESSION
from engine import collect_catalog
import test_core as fixtures

BASE='https://toki35.com'

def modern(numbers, total=None, more=False, newer=False):
    total=len(numbers) if total is None else total
    rows=''.join('<div class="novel-ep-row" data-ep="%s" data-episode-id="%s"><a class="novel-ep-link" href="/novel/123/%s"><span class="ne-num">%s화</span><span class="ne-title">%s화</span><span class="ep-badge-up">UP</span></a></div>' % (n,1000+n,1000+n,n,n) for n in numbers)
    return '<section class="novel-detail"><div class="nd-thumb"><img src="https://cdn.example/cover.png"></div><div class="nd-info"><h1>테스트 작품</h1><div class="nd-meta"><span>테스트 작가</span><span>· %s화</span></div><p class="nd-desc">테스트 설명</p></div></section><section class="adm-card"><div class="novel-episode-head">에피소드 (%s화)</div><div class="novel-eps" data-novel-id="123">%s</div>%s%s</section>' % (total,total,rows,'<button>이전 회차 더 보기</button>' if more else '', '<button>더 최신 회차 보기</button>' if newer else '')

class ModernCatalog(unittest.TestCase):
    def test_metadata_numbers_and_more_are_read_without_badge_text(self):
        book=parse_catalog(modern([333,332],333,True),BASE+'/novel/123')
        self.assertEqual((book['title'],book['author']),('테스트 작품','테스트 작가'))
        self.assertEqual(book['description'],'테스트 설명')
        self.assertEqual(book['cover_url'],'https://cdn.example/cover.png')
        self.assertEqual([e['no'] for e in book['episodes']],[333,332])
        self.assertEqual(book['episodes'][0]['title'],'333화')
        self.assertTrue(book['has_more']);self.assertEqual(book['total_episodes'],333)
        self.assertFalse(parse_catalog(modern([1,2],2,True),BASE+'/novel/123')['has_more'])
        locked=modern([1]).replace('<span class="ne-title">','<i class="ne-lock"></i><span class="ne-title">')
        self.assertTrue(parse_catalog(locked,BASE+'/novel/123')['episodes'][0]['locked'])

    def test_ids_domain_numbers_and_duplicates_are_checked(self):
        html=modern([1])
        for invalid in (html.replace('data-novel-id="123"','data-novel-id="456"'),html.replace('data-ep="1"','data-ep="2"'),html.replace('data-episode-id="1001"','data-episode-id="1002"'),html.replace('href="/novel/123/1001"','href="https://evil.example/novel/123/1001"'),modern([1,1])):
            with self.assertRaises(SiteError):parse_catalog(invalid,BASE+'/novel/123')

    def test_partial_catalog_is_never_accepted_as_complete(self):
        c=fixtures.FakeClient(BASE)
        for html in (modern([3,2],3,True),modern([3,2],3,False)):
            c.html=lambda path:html
            with self.assertRaisesRegex(SiteError,'전체 회차'):collect_catalog(c,'123')

    def test_browser_more_uses_normal_button_and_reads_growing_dom(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222');c.stop.wait=Mock(return_value=False)
        c.evaluate=Mock(return_value=True)
        c.continue_html=Mock(side_effect=[modern([3,2,1],3,False)])
        html=c.wait_catalog('/novel/123',modern([3],3,True))
        self.assertEqual(len(parse_catalog(html,BASE+'/novel/123')['episodes']),3)
        c.evaluate.assert_called_once_with(LOAD_MORE_EXPRESSION)
        self.assertNotIn('fetch(',LOAD_MORE_EXPRESSION)
        self.assertIn('이전 회차 더 보기',LOAD_MORE_EXPRESSION)

    def test_security_during_more_is_not_retried_or_solved(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222');c.stop.wait=Mock(return_value=False)
        c.evaluate=Mock(return_value=True);c.continue_html=Mock(side_effect=Blocked('사람 확인'))
        with self.assertRaises(Blocked):c.wait_catalog('/novel/123',modern([3],3,True))
        self.assertEqual(c.evaluate.call_count,1)

    def test_333_episodes_accumulate_when_dom_is_capped_at_300(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222');c.stop.wait=Mock(return_value=False)
        c.evaluate=Mock(return_value=True)
        c.continue_html=Mock(side_effect=[modern(list(range(333,133,-1)),333,True),modern(list(range(333,33,-1)),333,True),modern(list(range(300,0,-1)),333,False,True)])
        html=c.wait_catalog('/novel/123',modern(list(range(333,233,-1)),333,True))
        book=parse_catalog(html,BASE+'/novel/123')
        self.assertEqual([e['no'] for e in book['episodes']],list(range(1,334)))
        self.assertEqual(c.evaluate.call_count,3)
        fake=fixtures.FakeClient(BASE);fake.html=lambda path:html
        self.assertEqual(len(collect_catalog(fake,'123')['episodes']),333)

    def test_accumulated_windows_survive_manual_security_confirmation(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222');c.stop.wait=Mock(return_value=False);c.evaluate=Mock(return_value=True)
        c.continue_html=Mock(side_effect=[modern([4,3],5,True,True),Blocked('사람 확인')])
        with self.assertRaises(Blocked):c.wait_catalog('/novel/123',modern([5,4],5,True))
        c.continue_html=Mock(return_value=modern([2,1],5,False,True))
        html=c.wait_catalog('/novel/123',modern([3,2],5,True,True))
        self.assertEqual([e['no'] for e in parse_catalog(html,BASE+'/novel/123')['episodes']],list(range(1,6)))

class ModernHistory(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    run_job=fixtures.EngineTests.run_job

    def test_legacy_download_remains_saved_when_modern_catalog_is_analyzed(self):
        self.run_job(['1001'])
        class C(fixtures.FakeClient):
            def html(self,path):return modern([3,2,1])
        self.engine.factory=C;self.config['base_url']=BASE
        self.engine.start('analyze',['123'],self.config);self.engine.thread.join(5)
        state=self.engine.snapshot();self.assertEqual(state['status'],'completed',state)
        self.assertEqual([e['no'] for e in state['analysis']['episodes']],[1,2,3])
        self.assertEqual([e['have'] for e in state['analysis']['episodes']],[True,False,False])
