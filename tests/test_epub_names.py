import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import epub_filename, make_epub
from parser import Cancelled


class EpubNames(unittest.TestCase):
    def test_actual_range_and_last_filename_marker(self):
        book={'title':'작품'}
        for name, suffix in [('012_마지막 (완).txt',' 완'),('012_마지막 [완료].txt',' 완료'),('012_완벽한 결말.txt',''),('012_미완.txt','')]:
            rows=[{'no':12,'path':name},{'no':5,'path':'005_중간 (완).txt'}]
            self.assertEqual(epub_filename(book,rows),'작품 005~012'+suffix+'.epub')
        self.assertEqual(epub_filename(book,[{'no':1,'path':'001.txt'},{'no':1234,'path':'1234.txt'}]),'작품 001~1234.epub')

    def test_regeneration_replaces_only_plugin_editions_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);book={'id':'123','title':'작품'}
            def row(no,name):
                p=folder/name;p.write_text('직접 작성한 합본 테스트 문단',encoding='utf-8')
                return {'no':no,'title':'테스트','episode_id':str(1000+no),'path':str(p)}
            first=row(1,'001_시작.txt')
            old=make_epub(folder,book,[first])
            self.assertEqual(old.name,'작품 001~001.epub')
            legacy=folder/'작품.epub';old.rename(legacy)
            foreign=folder/'작품 001~099.epub';foreign.write_bytes(b'user file')
            last=row(2,'002_끝 (완).txt')
            new=make_epub(folder,book,[last,first])
            self.assertEqual(new.name,'작품 001~002 완.epub')
            self.assertFalse(legacy.exists())
            self.assertEqual(foreign.read_bytes(),b'user file')
            with patch('engine.atomic_write',side_effect=OSError('write failed')):
                with self.assertRaises(OSError):make_epub(folder,book,[first])
            self.assertTrue(new.exists())
            with self.assertRaises(Cancelled):make_epub(folder,book,[first],lambda:(_ for _ in ()).throw(Cancelled('중지')))
            self.assertTrue(new.exists())
