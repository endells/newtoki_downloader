"""Synthetic text only; structure follows the user-supplied modern viewer."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from parser import viewer_config, validate_body, SiteError, Blocked, Locked, Pending, check_page
from browser_client import BrowserClient, modern_dom_body
from test_core import TEXT

BASE='https://toki35.com'
URL=BASE+'/novel/123/1001'

def viewer(no=1,nid='123',eid='1001'):
    return '<link rel="canonical" href="https://alias.example/novel/%s/%s"><h1 class="ne-h1">%s화</h1><article class="novel-viewer"><div class="novel-tools">광고/설정</div><div style="--novel-font-size:16px"></div></article>' % (nid,eid,no)

def node(name,children=None,value=''):
    return dict(nodeName=name,nodeType=3 if name=='#text' else 1,nodeValue=value,children=children or [])

def closed_tree():
    return {'shadowRoots':[dict(shadowRootType='closed',**node('#document-fragment',[
        node('STYLE',[node('#text',value='do-not-include-css')]),
        node('P',[node('#text',value=TEXT.split('\n\n')[0])]),
        node('P',[node('#text',value=TEXT.split('\n\n')[1])]),
    ]))]}

class ModernViewer(unittest.TestCase):
    def test_title_only_heading_uses_catalog_number_after_exact_episode_id_check(self):
        html=viewer().replace('1화','언제 세상이 망한다고?')
        meta=viewer_config(html,'123','1001',expected_no=1)
        self.assertEqual((meta['no'],meta['number_source']),(1,'catalog'))
        with self.assertRaises(SiteError):viewer_config(html,'123','1002',expected_no=1)
        for invalid in (None,0,-1,True,'1'):
            with self.assertRaises(SiteError):viewer_config(html,'123','1001',expected_no=invalid)

    def test_title_only_episode_can_read_body_without_relaxing_url_checks(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222')
        c.evaluate=Mock(side_effect=[URL,URL,{'title':'테스트','message':'','locked':False},URL])
        c.call=Mock(side_effect=[{'root':{'nodeId':1}},{'nodeId':2},{'node':closed_tree()}])
        self.assertEqual(c.displayed_body('123',dict(id='1001',no=1),viewer().replace('1화','숫자 없는 회차 제목')),TEXT)

    def test_bare_number_heading_and_puzzle_gate_before_metadata(self):
        html=viewer(278,'57318','8376236').replace('278화','278')
        self.assertEqual(viewer_config(html,'57318','8376236')['no'],278)
        gated=html.replace('<div style="--novel-font-size:16px"></div>','<div class="novel-loading">퍼즐 조각을 맞춰주세요 봇 확인 후 컨텐츠 열람 가능합니다. 밀어서 맞추기</div>')
        with self.assertRaises(Blocked):viewer_config(gated,'57318','8376236')
        with self.assertRaises(Blocked):validate_body({'rendered':False,'message':'퍼즐 조각을 맞춰주세요 봇 확인 후 컨텐츠 열람 가능합니다.'})

    def test_metadata_uses_canonical_path_and_heading_not_hidden_session_state(self):
        meta=viewer_config(viewer(260,'57318','8018729'),'57318','8018729')
        self.assertEqual((meta['id'],meta['no'],meta['format']),('8018729',260,'modern'))
        with self.assertRaises(SiteError):viewer_config(viewer(nid='456'),'123','1001')
        with self.assertRaises(SiteError):viewer_config(viewer(eid='1002'),'123','1001')

    def test_closed_root_paragraphs_only_and_images_not_silently_dropped(self):
        tree=closed_tree();result=modern_dom_body(tree)
        self.assertEqual(validate_body(result),TEXT)
        self.assertNotIn('do-not-include',result['text'])
        tree['shadowRoots'][0]['children'].append(node('IMG'))
        with self.assertRaises(SiteError):validate_body(modern_dom_body(tree))
        with self.assertRaises(Pending):validate_body(modern_dom_body({}))

    def test_standard_dom_read_and_current_episode_validation(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222')
        c.evaluate=Mock(side_effect=[URL,URL,{'title':'테스트','message':'','locked':False},URL])
        c.call=Mock(side_effect=[{'root':{'nodeId':1}},{'nodeId':2},{'node':closed_tree()}])
        self.assertEqual(c.displayed_body('123',dict(id='1001',no=1),viewer()),TEXT)
        self.assertEqual([call.args[0] for call in c.call.call_args_list],['DOM.getDocument','DOM.querySelector','DOM.describeNode'])
        self.assertTrue(c.call.call_args_list[-1].args[1]['pierce'])
        c.evaluate=Mock(return_value=URL);c.call.reset_mock()
        with self.assertRaisesRegex(SiteError,'회차 번호'):c.displayed_body('123',dict(id='1001',no=2),viewer())
        c.call.assert_not_called()

    def test_security_and_purchase_notices_stop_before_dom_read(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222');c.call=Mock()
        for state,error in [({'message':'인증이 필요합니다'},Blocked),({'message':'구매 필요','locked':True},Locked)]:
            c.evaluate=Mock(return_value=state)
            with self.assertRaises(error):c.modern_body()
        c.call.assert_not_called()

    def test_navigation_during_dom_read_rejects_other_episode(self):
        c=BrowserClient(BASE,'http://127.0.0.1:9222')
        c.evaluate=Mock(side_effect=[URL,URL,{'title':'테스트','message':''},BASE+'/novel/123/1002'])
        c.call=Mock(side_effect=[{'root':{'nodeId':1}},{'nodeId':2},{'node':closed_tree()}])
        with self.assertRaises(Blocked):c.displayed_body('123',dict(id='1001',no=1),viewer())
