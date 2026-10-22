import sys
from pathlib import Path
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
from test_core import FakeClient, catalog, BASE
from parser import parse_catalog
from engine import make_epub, image_type, SiteError

class Features(unittest.TestCase):
    setUp = fixtures.EngineTests.setUp
    run_job = fixtures.EngineTests.run_job
    def test_cover_epub_and_layout(self):
        self.run_job(['1001'])
        folder=next((self.root/'books').iterdir())
        (folder/'cover.png').write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        book={'id':'123','title':'테스트','description':'소개 & 설명'}
        make_epub(folder,book,self.engine.history.rows('123'),config={'epub_line_height':2.2,'epub_paragraph_gap':.8})
        with zipfile.ZipFile(folder/'테스트 001~001.epub') as z:
            opf=ET.fromstring(z.read('OEBPS/content.opf'))
            self.assertIn(b'cover-image',z.read('OEBPS/content.opf'))
            self.assertIn(b'line-height:2.2',z.read('OEBPS/chapter-1001.xhtml'))
            self.assertEqual(z.read('OEBPS/cover.png'),(folder/'cover.png').read_bytes())
            for name in z.namelist():
                if name.endswith(('.xml','.opf','.xhtml')): ET.fromstring(z.read(name))

    def test_callback_failure_keeps_success_and_releases_lock(self):
        def fail(report):raise RuntimeError('notification unavailable')
        self.config['on_finished']=fail
        result=self.run_job(['1001'])
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['completed'],1)
        self.assertIn('last_warning',result)
        self.assertEqual(self.run_job(['1002'])['status'],'completed')

    def test_cover_parser_and_html_rejection(self):
        book=parse_catalog('<meta property="og:image" content="/cover.png">'+catalog([1]), BASE+'/novel/123')
        self.assertEqual(book['cover_url'],BASE+'/cover.png')
        with self.assertRaises(SiteError):image_type(b'<html>error</html>')

    def test_callback_includes_downloads_and_current_domain(self):
        reports=[];self.config['on_finished']=reports.append
        self.run_job(['1001'])
        self.assertEqual(reports[0]['downloads'][0]['no'],1)
        self.assertEqual(reports[0]['base_url'],BASE)
