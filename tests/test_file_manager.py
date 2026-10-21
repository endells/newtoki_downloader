import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_core as fixtures
from engine import History
from file_manager import manage_files, rebuild_epubs, selected_paths
from parser import Cancelled

class FileManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'books';self.root.mkdir()
        self.history=History(Path(self.tmp.name)/'history.db')
        self.folder=self.root/'작품 [123]';self.folder.mkdir()
        (self.folder/'metadata.json').write_text(json.dumps({'id':'123','title':'작품'}))
        self.path=self.folder/'0001_첫 화 [1001].txt';self.data='1. 첫 화\n\n테스트 본문입니다.'.encode()
        self.path.write_bytes(self.data)
        self.item={'id':'1001','no':1,'title':'첫 화'}
        self.history.record('123',self.item,'completed',self.path,hashlib.sha256(self.data).hexdigest())

    def test_preview_then_apply_preserves_contents_and_history_skip(self):
        report=manage_files(self.root,self.history)
        self.assertEqual(report['planned'],2)
        self.assertTrue(self.path.exists())
        (self.folder/'cover.jpg').write_bytes(b'cover fixture')
        (self.folder/'작품.epub').write_bytes(b'epub fixture')
        report=manage_files(self.root,self.history,True)
        new=self.root/'작품'/'0001_첫 화.txt'
        self.assertEqual(new.read_bytes(),self.data)
        self.assertFalse(self.folder.exists())
        self.assertTrue(self.history.valid('123',self.item,self.root))
        self.assertEqual(self.history.rows('123')[0]['path'],str(new))
        self.assertEqual((new.parent/'cover.jpg').read_bytes(),b'cover fixture')
        self.assertEqual((new.parent/'작품.epub').read_bytes(),b'epub fixture')
        self.assertEqual(report['files_renamed'],1);self.assertEqual(report['folders_renamed'],1)
        again=manage_files(self.root,self.history,True)
        self.assertEqual(again['files_renamed'],0);self.assertEqual(again['folders_renamed'],0)

    def test_selected_folder_scan_rebuild_and_rename_leave_other_folder_untouched(self):
        import zipfile
        other=self.root/'다른 작품 [456]';other.mkdir()
        (other/'metadata.json').write_text(json.dumps({'id':'456','title':'다른 작품'}))
        path=other/'0001_다른 화 [2001].txt';path.write_bytes(b'changed external file')
        self.history.record('456',dict(id='2001',no=1,title='다른 화'),'completed',path,'old-digest')
        (other/'다른 작품.epub').write_bytes(b'keep epub')
        chosen=selected_paths(self.root,[self.folder.name])
        report=manage_files(self.root,self.history,selected_folders=chosen)
        rebuild_epubs(self.root,self.history,{},report,selected_folders=chosen)
        self.assertEqual(report['epub_created'],1)
        with zipfile.ZipFile(self.folder/'작품.epub') as z:
            self.assertIn('OEBPS/chapter-1001.xhtml',z.namelist())
        self.assertEqual(self.history.rows('456')[0]['digest'],'old-digest')
        self.assertEqual((other/'다른 작품.epub').read_bytes(),b'keep epub')
        manage_files(self.root,self.history,True,selected_folders=chosen)
        self.assertTrue((self.root/'작품'/'0001_첫 화.txt').exists())
        self.assertTrue(path.exists());self.assertTrue(other.exists())

    def test_folder_scope_rejects_empty_traversal_and_symlinks(self):
        from parser import SiteError
        link=self.root/'링크';link.symlink_to(self.folder,target_is_directory=True)
        for names in ([],['..'],['../books'],[str(self.folder)],['링크'],['없는 폴더']):
            with self.subTest(names=names),self.assertRaises(SiteError):selected_paths(self.root,names)
        self.assertIsNone(selected_paths(self.root,None))

    def test_collisions_keep_existing_files_and_folders(self):
        plain=self.root/'작품';plain.mkdir();(plain/'marker').write_bytes(b'keep')
        target=self.folder/'0001_첫 화.txt';target.write_bytes(b'other contents')
        report=manage_files(self.root,self.history,True)
        self.assertEqual(report['conflicts'],2)
        self.assertEqual(self.path.read_bytes(),self.data)
        self.assertEqual(target.read_bytes(),b'other contents')
        self.assertEqual((plain/'marker').read_bytes(),b'keep')

    def test_corrupt_missing_and_untracked_files_not_renamed(self):
        self.path.write_bytes(b'')
        unknown=self.folder/'0002_다른 화 [1002].txt';unknown.write_bytes(b'unknown')
        report=manage_files(self.root,self.history,True)
        new=self.root/'작품'
        self.assertEqual((new/self.path.name).read_bytes(),b'')
        self.assertTrue((new/unknown.name).exists())
        self.assertEqual(report['files_renamed'],0)
        self.assertEqual(report['skipped'],1)

    def test_recover_interrupted_folder_rename_then_file_rename(self):
        plain=self.root/'작품';self.folder.rename(plain)
        before=manage_files(self.root,self.history)
        self.assertEqual(before['planned'],1)
        self.assertEqual(before['history_imported'],1)
        report=manage_files(self.root,self.history,True)
        self.assertEqual(report['history_repaired']+report['history_imported'],0)
        self.assertTrue(self.history.valid('123',self.item,self.root))
        row=self.history.rows('123')[0]
        # Simulate old DB path after a previous process already renamed the file.
        self.history.query('UPDATE entries SET path=?',(str(plain/self.path.name),))
        report=manage_files(self.root,self.history,True)
        self.assertEqual(report['history_repaired']+report['history_imported'],1)
        self.assertTrue(self.history.valid('123',self.item,self.root))

    def test_db_failure_rolls_back_folder_or_file_without_loss(self):
        original=self.history.query
        def broken(sql,args=()):
            if sql.startswith('UPDATE entries'):raise RuntimeError('db write failure')
            return original(sql,args)
        with patch.object(self.history,'query',side_effect=broken):
            with self.assertRaises(RuntimeError):manage_files(self.root,self.history,True)
        self.assertTrue(self.path.exists())
        # Keep folder collision so only file rename is attempted.
        (self.root/'작품').mkdir()
        with patch.object(self.history,'query',side_effect=broken):
            with self.assertRaises(RuntimeError):manage_files(self.root,self.history,True)
        self.assertTrue(self.path.exists())
        self.assertFalse((self.folder/'0001_첫 화.txt').exists())

    def test_cancellation_never_changes_files(self):
        def stop():raise Cancelled('중지')
        with self.assertRaises(Cancelled):manage_files(self.root,self.history,True,stop)
        self.assertTrue(self.path.exists())

    def test_external_paths_and_symlinks_not_touched(self):
        outside=Path(self.tmp.name)/'external';outside.mkdir()
        target=outside/'0002_다른 화 [1002].txt';target.write_bytes(b'keep')
        item={'id':'1002','no':2,'title':'다른 화'}
        self.history.record('123',item,'completed',target,hashlib.sha256(b'keep').hexdigest())
        link=self.folder/'0003_링크 [1003].txt';link.symlink_to(target)
        self.history.record('123',dict(item,id='1003',no=3),'completed',link,hashlib.sha256(b'keep').hexdigest())
        report=manage_files(self.root,self.history,True)
        self.assertEqual(target.read_bytes(),b'keep')
        self.assertFalse((outside/'0002_다른 화.txt').exists())

class NewNames(unittest.TestCase):
    setUp=fixtures.EngineTests.setUp
    run_job=fixtures.EngineTests.run_job
    def test_new_folder_and_episode_names_have_no_ids(self):
        self.run_job(['1001'])
        folder=next((self.root/'books').iterdir())
        self.assertEqual(folder.name,'테스트 작품')
        self.assertEqual(next(folder.glob('*.txt')).name,'001_1화.txt')
    def test_maintenance_runs_without_browser(self):
        self.run_job(['1001'])
        def fail(*args):raise AssertionError('maintenance must not open browser')
        self.engine.factory=fail
        self.engine.start('files_scan',[],self.config);self.engine.thread.join(5)
        self.assertEqual(self.engine.snapshot()['status'],'completed')
        self.assertIn('file_report',self.engine.snapshot())

    def test_epub_regeneration_reconciles_external_txt_offline_and_ignores_auto_epub_toggle(self):
        import zipfile
        self.run_job(['1001'])
        folder=next((self.root/'books').iterdir())
        (folder/'002_외부 회차.txt').write_text('외부에서 넣은 두 번째 본문',encoding='utf-8')
        (folder/'003_빈 회차.txt').write_bytes(b'')
        (folder/'cover.jpg').write_bytes(b'\xff\xd8\xfftest-cover')
        epub=folder/'테스트 작품.epub';epub.write_bytes(b'old epub')
        def fail(*args):raise AssertionError('must not browse')
        self.engine.factory=fail
        self.config.update(make_epub=False,epub_line_height=2.2,file_folders=[folder.name])
        before={p.name:p.read_bytes() for p in folder.glob('*.txt')}
        self.engine.start('files_epub',[],self.config);self.engine.thread.join(5)
        state=self.engine.snapshot();self.assertEqual(state['status'],'completed',state)
        self.assertEqual(state['file_report']['epub_created'],1)
        self.assertEqual(state['file_report']['epub_failed'],0)
        self.assertEqual(before,{p.name:p.read_bytes() for p in folder.glob('*.txt')})
        with zipfile.ZipFile(epub) as z:
            self.assertIn('OEBPS/chapter-1002.xhtml',z.namelist())
            self.assertNotIn('OEBPS/chapter-1003.xhtml',z.namelist())
            self.assertIn('OEBPS/cover.jpg',z.namelist())
            chapter=z.read('OEBPS/chapter-1002.xhtml').decode()
            self.assertIn('외부에서 넣은 두 번째 본문',chapter)
            self.assertIn('line-height:2.2',chapter)
        epub.write_bytes(b'keep ordinary scan')
        self.engine.start('files_scan',[],self.config);self.engine.thread.join(5)
        self.assertEqual(epub.read_bytes(),b'keep ordinary scan')

    def test_cancel_before_epub_replace_preserves_old_epub(self):
        self.run_job(['1001'])
        folder=next((self.root/'books').iterdir());epub=folder/'테스트 작품.epub'
        epub.write_bytes(b'old epub')
        report={'rows':[]};calls=[]
        def check():
            calls.append(1)
            if len(calls)==4:raise Cancelled('중지')
        with self.assertRaises(Cancelled):
            rebuild_epubs(self.root/'books',self.engine.history,{},report,check)
        self.assertEqual(epub.read_bytes(),b'old epub')
