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
        self.scheduler=framework.F.scheduler

    def test_menu_templates_render_and_escape(self):
        from flask import session
        self.settings.set('titles','123')
        for page in ('setting','manual','history'):
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


if __name__=='__main__':unittest.main()
