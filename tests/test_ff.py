"""FF interface smoke tests with a minimal framework stub; not a real FF install."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT.parent))
HAS_FLASK=importlib.util.find_spec('flask') is not None


@unittest.skipUnless(HAS_FLASK,'Flask/Jinja2 not installed; run this test inside FF or install test dependencies')
class FFTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        from jinja2 import ChoiceLoader,DictLoader,FileSystemLoader
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=Flask(__name__);self.app.secret_key='fixture-only'
        self.app.jinja_loader=ChoiceLoader([FileSystemLoader(str(ROOT/'templates')),DictLoader({'base.html':'{% block content %}{% endblock %}'})])
        class Scheduler:
            jobs={}
            def is_include(self,k):return k in self.jobs
            def remove_job(self,k):self.jobs.pop(k,None)
            def add_job_instance(self,j,run=True):self.jobs[j.name]=(j,run)
        class Job:
            def __init__(self,package,name,*a):self.name=name
        class Base:
            def __init__(self,P,**kw):self.P=P
            def get_scheduler_name(self):return 'newtoki-test'
            def get_scheduler_desc(self):return 'test'
        framework=types.ModuleType('framework')
        framework.F=types.SimpleNamespace(app=self.app,config={'path_data':self.temp.name},scheduler=Scheduler())
        framework.Job=Job
        plugin=types.ModuleType('plugin');plugin.PluginModuleBase=Base
        sys.modules['framework']=framework;sys.modules['plugin']=plugin
        sys.modules.pop('newtoki_downloader.mod_basic',None)
        from newtoki_downloader.mod_basic import ModuleBasic
        class Settings:
            data=dict(ModuleBasic.db_default)
            @classmethod
            def get(cls,key):return cls.data.get(key)
            @classmethod
            def set(cls,key,value):cls.data[key]=value
        self.settings=Settings
        self.module=ModuleBasic(types.SimpleNamespace(ModelSetting=Settings,package_name='newtoki_downloader',logger=self.app.logger))
        self.module.plugin_load()
        self.addCleanup(self.module.plugin_unload)
        self.scheduler=framework.F.scheduler

    def test_menu_templates_render_and_escape(self):
        from flask import session
        self.settings.set('titles','123')
        for page in ('setting','manual','history','integration','activity','files'):
            with self.app.test_request_context('/'):
                output=self.module.process_menu(page,None)
                self.assertIn('newtoki_downloader/ajax/basic/command',output)
                self.assertIn('csrf_token',output)
                self.assertNotIn('{% ',output)

    def test_save_never_runs_job(self):
        from flask import session,request
        with self.app.test_request_context('/',method='POST',data={'csrf_token':'abc'}):
            session['newtoki_csrf']='abc'
            conf=self.module.config();conf['basic_auto_start']=True
            response=self.module.process_command('save',json.dumps(conf),'','',request)
            self.assertEqual(response.get_json()['ret'],'success')
            self.assertIsNone(self.module.engine.thread)
            self.assertFalse(self.scheduler.jobs['newtoki-test'][1])

    def test_csrf_required(self):
        from flask import request
        with self.app.test_request_context('/',method='POST'):
            response,status=self.module.process_command('run','','','',request)
            self.assertEqual(status,403)
            self.assertIsNone(self.module.engine.thread)

    def test_scheduled_block_pauses(self):
        self.module.engine.update(status='verification_required')
        self.module.scheduler_function()
        self.assertIsNone(self.module.engine.thread)

    def send(self, command, data=''):
        from flask import session, request
        with self.app.test_request_context('/',method='POST',data={'csrf_token':'abc'}):
            session['newtoki_csrf']='abc'
            return self.module.process_command(command,data,'','',request).get_json()

    def test_integration_schedule_no_download(self):
        raw={'token':'123456:'+'A'*30,'channel':'-100123','enabled':True,'interval_minutes':5}
        self.assertEqual(self.send('telegram_save',json.dumps(raw))['ret'],'success')
        first=self.scheduler.jobs['newtoki-test_telegram']
        self.send('telegram_save',json.dumps(raw))
        self.assertIs(first,self.scheduler.jobs['newtoki-test_telegram'])
        raw['interval_minutes']=10
        self.send('telegram_save',json.dumps(raw))
        self.assertIsNot(first,self.scheduler.jobs['newtoki-test_telegram'])
        self.assertFalse(self.scheduler.jobs['newtoki-test_telegram'][1])
        self.assertIsNone(self.module.engine.thread)
        self.assertNotIn(raw['token'],json.dumps(self.send('integration_settings')))

    def test_title_removal_preserves_history_and_files(self):
        self.send('add_auto','123');self.send('add_auto','123')
        self.assertEqual(len(self.send('titles')['rows']),1)
        self.send('remove_auto','123')
        self.assertEqual(self.send('titles')['rows'],[])
        self.assertTrue(Path(self.module.engine.history.path).exists())

    def test_log_menu_alias_and_live_refresh(self):
        with self.app.test_request_context('/'):
            output=self.module.process_menu('log',None)
            self.assertIn('최근 로그',output)
            self.assertIn('setInterval(refresh,3000)',output)
        source=(ROOT/'setup.py').read_text()
        self.assertIn("'uri': 'basic/log'",source)

    def test_failed_analysis_records_details_without_downloading(self):
        from unittest.mock import Mock
        from newtoki_downloader.parser import SiteError
        class BrokenBrowser:
            base='https://newtoki1.org'
            def check(self):pass
            def html(self,path):raise SiteError('페이지 JavaScript 실행 실패: TypeError fixture')
            def close(self):pass
        self.module.engine.factory=Mock(return_value=BrokenBrowser())
        self.module.discord.notify=Mock(return_value=False)
        conf=self.module.runtime();conf['auto_domain']=False
        self.module.engine.start('analyze',['123'],conf)
        self.module.engine.thread.join(5)
        self.assertEqual(self.module.engine.snapshot()['status'],'failed')
        rows=self.send('activity')['rows']
        self.assertTrue(any('TypeError fixture' in r['message'] for r in rows))
        self.assertTrue(any('작품 분석 시작' in r['message'] for r in rows))
        report=self.module.discord.notify.call_args.args[0]
        self.assertEqual(report['source'],'작품 분석')
        self.assertEqual(report['downloads'],[])

    def test_logger_capture_no_duplicate_and_secret_masking(self):
        self.module.record_event('WARNING','marker tokenless')
        rows=self.module.activity.rows()
        self.assertEqual(sum('marker tokenless' in r['message'] for r in rows),1)
        self.module.P.logger.warning('capture external record')
        self.assertTrue(any('capture external record' in r['message'] for r in self.module.activity.rows()))
        self.module.record_event('ERROR','cookie: value-hidden')
        self.assertNotIn('value-hidden',self.module.activity.rows()[0]['message'])

    def test_connection_tab_save_preserves_download_settings(self):
        with self.app.test_request_context('/'):
            setting=self.module.process_menu('setting',None)
            integration=self.module.process_menu('integration',None)
        for key in ('base_url','browser_url','announcement_url','auto_domain','domain_mode','probe','domain'):
            self.assertNotIn('id="'+key+'"',setting)
            self.assertIn('id="'+key+'"',integration)
        self.settings.set('titles','123')
        previous_path=self.module.config()['download_path']
        response=self.send('save_connection',json.dumps({'base_url':'https://newtoki2.org','browser_url':'http://127.0.0.1:9223','announcement_url':'https://뉴토주소.com/','auto_domain':False,'titles':'999','basic_auto_start':True}))
        self.assertEqual(response['ret'],'success')
        self.assertEqual(self.module.config()['titles'],'123')
        self.assertEqual(self.module.config()['download_path'],previous_path)
        self.assertFalse(self.module.config()['basic_auto_start'])
        self.assertFalse(self.module.config()['auto_domain'])
        self.assertEqual(self.module.config()['base_url'],'https://newtoki2.org')
        self.assertIsNone(self.module.engine.thread)
        self.assertEqual(self.module.config()['domain_mode'],'legacy_first')
        self.assertEqual(self.send('save_connection',json.dumps({'domain_mode':'modern_only'}))['ret'],'success')
        self.assertEqual(self.module.config()['domain_mode'],'modern_only')
        self.assertEqual(self.send('save_connection',json.dumps({'domain_mode':'invalid'}))['ret'],'error')
        self.assertEqual(self.module.config()['domain_mode'],'modern_only')
        self.send('save',json.dumps({'titles':'456'}))
        self.assertEqual(self.module.config()['base_url'],'https://newtoki2.org')
        self.assertEqual(self.module.config()['browser_url'],'http://127.0.0.1:9223')
        self.assertFalse(self.module.config()['auto_domain'])

    def test_browser_connection_probe_uses_entered_address_without_changing_settings(self):
        from unittest.mock import patch
        old=self.module.config()['browser_url']
        with patch('newtoki_downloader.mod_basic.BrowserClient') as factory:
            factory.return_value.check_connection.return_value='Chrome/123'
            factory.return_value.address='http://127.0.0.1:9223'
            result=self.send('browser_check','http://127.0.0.1:9223')
            self.assertEqual(result['ret'],'success')
            self.assertIn('Chrome/123',result['msg'])
            self.assertEqual(factory.call_args.args[1],'http://127.0.0.1:9223')
            factory.return_value.check_connection.assert_called_once()
            factory.return_value.connect.assert_not_called()
        self.assertEqual(self.module.config()['browser_url'],old)
        self.assertIsNone(self.module.engine.thread)

    def test_manual_defaults_removed_and_resume_command(self):
        from unittest.mock import patch
        import re
        with self.app.test_request_context('/'):
            html=self.module.process_menu('manual',None)
        field=re.search(r'<input id="novel"[^>]*>',html).group(0)
        self.assertNotIn('value=',field)
        for field_id in ('range-from','range-to'):
            field=re.search(r'<input id="'+field_id+r'"[^>]*>',html).group(0)
            self.assertNotIn('value=',field)
        self.assertIn('id="select-range"',html)
        self.assertIn('id="resume"',html)
        with patch.object(self.module.engine,'resume') as resume:
            self.assertEqual(self.send('resume')['ret'],'success')
            resume.assert_called_once()

    def test_batch_rest_defaults_and_validation(self):
        conf=self.module.config()
        self.assertEqual((conf['batch_size'],conf['batch_pause']),(10,60))
        for key,value in [('batch_size','0'),('batch_size','101'),('batch_pause','9'),('batch_pause','3601')]:
            with self.assertRaises(Exception):
                self.module.validate(dict(self.settings.data,**{key:value}))

    def test_file_folder_listing_and_scope_routing(self):
        from unittest.mock import patch
        root=Path(self.module.config()['download_path']);root.mkdir(parents=True,exist_ok=True)
        folder=root/'테스트 작품';folder.mkdir()
        self.assertEqual(self.send('file_folders')['folders'],['테스트 작품'])
        with patch.object(self.module.engine,'start') as start:
            self.assertEqual(self.send('files_epub',json.dumps([folder.name]))['ret'],'success')
            args=start.call_args.args
            self.assertEqual(args[0],'files_epub');self.assertEqual(args[2]['file_folders'],[folder.name])
            start.reset_mock()
            self.assertEqual(self.send('files_epub','[]')['ret'],'error');start.assert_not_called()
        with self.app.test_request_context('/'):
            html=self.module.process_menu('files',None)
            for ident in ('files-scan','files-apply','files-epub','files-folders'):
                self.assertIn('id="'+ident+'"',html)


if __name__=='__main__':unittest.main()
