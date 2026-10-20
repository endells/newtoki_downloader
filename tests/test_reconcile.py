import hashlib
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
from engine import Engine, make_epub
from file_manager import manage_files
from reconcile import leading_number, reconcile_book
from parser import Cancelled
import zipfile

class LocalFiles(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    run_job=fixtures.EngineTests.run_job
    def external(self,no=1,title='완전히 다른 외부 제목',data=None,legacy=False):
        folder=self.root/'books'/('테스트 작품 [123]' if legacy else '테스트 작품')
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/('%04d_%s.txt'%(no,title))
        path.write_bytes(data if data is not None else '외부에서 받은 첫 문단입니다.\n\n두 번째 본문 문단입니다.'.encode())
        return path
    def analyze(self):
        self.engine.start('analyze',['123'],self.config);self.engine.thread.join(5)
        result=self.engine.snapshot();self.assertEqual(result['status'],'completed',result)
        return {e['no']:e['have'] for e in result['analysis']['episodes']}

    def test_leading_number_formats_only(self):
        for name in ['0001_다른 제목.txt','1.다른제목.txt','001 제목.TXT','[0001] 제목.txt','0001화 제목.txt','0001.txt']:
            self.assertEqual(leading_number(name),1)
        for name in ['제목 0001.txt','1abc.txt','작품0001.txt','cover.jpg']:
            self.assertIsNone(leading_number(name))

    def test_external_no_history_is_saved_in_analysis_and_skipped_by_auto(self):
        path=self.external()
        self.assertTrue(self.analyze()[1])
        row=self.engine.history.rows('123')[0]
        self.assertEqual(row['status'],'completed');self.assertEqual(row['path'],str(path))
        self.assertEqual(self.engine.snapshot()['history_imported'],1)
        class Client(fixtures.FakeClient):
            def body(self,nid,item):
                if item['no']==1:raise AssertionError('external saved file must not download')
                return fixtures.TEXT
        self.engine.factory=Client
        result=self.run_job()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['completed'],1);self.assertEqual(result['skipped'],1)
        self.assertEqual(path.read_bytes().decode().split('\n')[0],'외부에서 받은 첫 문단입니다.')

    def test_auto_and_manual_import_without_prior_analyze(self):
        self.external()
        result=self.run_job(['1001'])
        self.assertEqual(result['completed'],0);self.assertEqual(result['skipped'],1)
        self.assertEqual(result['history_imported'],1)

    def test_new_zero_byte_file_is_missing_and_can_be_downloaded(self):
        self.external(title='1화',data=b'')
        self.assertFalse(self.analyze()[1])
        self.assertEqual(self.engine.history.rows('123')[0]['status'],'missing')
        result=self.run_job(['1001'])
        self.assertEqual(result['status'],'completed');self.assertEqual(result['completed'],1)

    def test_deleted_file_downgraded_and_external_replacement_restores_history(self):
        self.run_job(['1001'])
        old=Path(self.engine.history.rows('123')[0]['path']);old.unlink()
        self.assertFalse(self.analyze()[1])
        self.assertEqual(self.engine.history.rows('123')[0]['status'],'missing')
        new=self.external(title='교체한 파일')
        self.assertTrue(self.analyze()[1])
        self.assertEqual(self.engine.history.rows('123')[0]['path'],str(new))
        new.write_bytes(b'new externally supplied content')
        self.assertTrue(self.analyze()[1])
        self.assertEqual(self.engine.history.rows('123')[0]['digest'],hashlib.sha256(new.read_bytes()).hexdigest())

    def test_offline_existing_file_scan_imports_from_persistent_catalog(self):
        self.analyze()
        self.engine.history.query('DELETE FROM entries')
        path=self.external(no=2)
        reloaded=Engine(self.root/'state.db')
        report=manage_files(self.root/'books',reloaded.history)
        self.assertEqual(report['history_imported'],1)
        row=reloaded.history.rows('123')[0]
        self.assertEqual(row['no'],2);self.assertEqual(row['path'],str(path))
        self.assertTrue(path.exists())

    def test_duplicates_prefer_history_path_then_stable_filename(self):
        first=self.external(title='A')
        second=self.external(title='B')
        self.assertTrue(self.analyze()[1])
        self.assertEqual(self.engine.history.rows('123')[0]['path'],str(first))
        self.engine.history.query('UPDATE entries SET path=?',(str(second),))
        self.analyze()
        self.assertEqual(self.engine.history.rows('123')[0]['path'],str(second))
        self.assertTrue(self.engine.snapshot()['warnings'])

    def test_wrong_book_metadata_and_symlink_files_are_excluded(self):
        path=self.external()
        (path.parent/'metadata.json').write_text(json.dumps({'id':'999','title':'다른 작품'}))
        self.assertFalse(self.analyze()[1])
        (path.parent/'metadata.json').unlink();path.unlink()
        outside=self.root/'outside.txt';outside.write_text('outside')
        path.symlink_to(outside)
        self.assertFalse(self.analyze()[1])

    def test_legacy_folder_and_cached_number_mapping(self):
        self.external(legacy=True)
        self.assertTrue(self.analyze()[1])
        self.assertEqual(self.engine.history.rows('123')[0]['episode_id'],'1001')
        cache=self.engine.history.catalogs()[0]
        self.assertEqual(len(cache['episodes']),3)

    def test_external_cp949_epub_keeps_first_paragraph(self):
        first='첫 번째 외부 본문 문단입니다.'
        self.external(data=(first+'\n\n두 번째 문단입니다.').encode('cp949'))
        self.analyze()
        result=self.run_job(['1001'])
        self.assertEqual(result['status'],'completed')
        epub=next((self.root/'books').rglob('*.epub'))
        with zipfile.ZipFile(epub) as z:
            self.assertIn(first,z.read('OEBPS/chapter-1001.xhtml').decode())

    def test_offline_unmapped_work_explains_initial_analysis_needed(self):
        self.external()
        messages=[]
        report=manage_files(self.root/'books',self.engine.history,update=lambda **kw:messages.append(kw))
        self.assertEqual(report['needs_analysis'],1)
        self.assertEqual(report['history_imported'],0)
        self.assertTrue(any('한 번 분석' in m.get('last_warning','') for m in messages))

    def test_cancellation_does_not_promote_unverified_files(self):
        self.external()
        book={'id':'123','title':'테스트 작품','episodes':[{'id':'1001','no':1,'title':'1화'}]}
        def stop():raise Cancelled('중지')
        with self.assertRaises(Cancelled):reconcile_book(self.root/'books',self.engine.history,book,stop)
        self.assertEqual(self.engine.history.rows('123'),[])
