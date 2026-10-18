import json
import math
import os
import secrets
import threading
from pathlib import Path
from urllib.parse import urlsplit

from flask import jsonify, render_template, session
from framework import F, Job
from plugin import PluginModuleBase
from .parser import SiteError, novel_id, title_ids, origin
from .engine import Engine


class ModuleBasic(PluginModuleBase):
    db_default = {
        'base_url': 'https://newtoki1.org', 'browser_url': 'http://newtoki-browser:9223',
        'download_path': '', 'titles': '', 'request_delay': '3', 'timeout': '30',
        'episode_digits': '4', 'max_per_title': '10', 'make_epub': 'True',
        'auto_domain': 'False', 'basic_auto_start': 'False', 'basic_interval': '180',
    }

    def __init__(self, P):
        super().__init__(P, first_menu='setting', name='basic', scheduler_desc='북토끼 공개 회차 수집')
        self.engine = None
        self.settings_lock = threading.RLock()

    def plugin_load(self):
        if not self.P.ModelSetting.get('download_path'):
            self.P.ModelSetting.set('download_path', os.path.join(F.config['path_data'], 'downloads', 'newtoki'))
        self._engine()
        self.sync_schedule(self.config())

    def _engine(self):
        if self.engine is None:
            self.engine = Engine(os.path.join(F.config['path_data'], 'db', 'newtoki_downloader_history.db'))
        return self.engine

    @staticmethod
    def validate(raw):
        conf = dict(raw)
        conf['base_url'] = origin(conf['base_url'])
        u = urlsplit(conf['browser_url'])
        if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.path not in ('', '/') or u.query or u.fragment:
            raise SiteError('브라우저 내부 주소 형식을 확인하세요.')
        path = str(conf['download_path']).strip()
        if not Path(path).is_absolute():
            raise SiteError('저장 폴더는 FF 컨테이너 안의 절대 경로여야 합니다.')
        conf['download_path'] = path
        conf['titles'] = '\n'.join(title_ids(conf['titles']))
        for key, lo, hi in [('episode_digits', 1, 10), ('max_per_title', 1, 1000), ('timeout', 10, 90), ('basic_interval', 10, 10080)]:
            conf[key] = int(conf[key])
            if not lo <= conf[key] <= hi:
                raise SiteError('%s 범위: %s~%s' % (key, lo, hi))
        conf['request_delay'] = float(conf['request_delay'])
        if not math.isfinite(conf['request_delay']) or not 2 <= conf['request_delay'] <= 60:
            raise SiteError('요청 간격은 2~60초입니다.')
        for key in ('make_epub', 'auto_domain', 'basic_auto_start'):
            conf[key] = conf[key] in (True, 'True', 'true', '1')
        return conf

    def config(self):
        raw = {k: self.P.ModelSetting.get(k) or v for k, v in self.db_default.items()}
        raw['download_path'] = raw['download_path'] or os.path.join(F.config['path_data'], 'downloads', 'newtoki')
        return self.validate(raw)

    def save_domain(self, value):
        with F.app.app_context(), self.settings_lock:
            old = self.P.ModelSetting.get('base_url')
            self.P.ModelSetting.set('base_url', value)
            self.P.logger.info('구버전 도메인 변경: %s -> %s', old, value)

    def runtime(self):
        conf = self.config()
        conf['save_domain'] = self.save_domain
        return conf

    def sync_schedule(self, conf, old_interval=None):
        name = self.get_scheduler_name()
        included = F.scheduler.is_include(name)
        if not conf['basic_auto_start']:
            if included:
                F.scheduler.remove_job(name)
            return
        if included and old_interval in (None, conf['basic_interval']):
            return
        if included:
            F.scheduler.remove_job(name)
        job = Job(self.P.package_name, name, str(conf['basic_interval']), self.scheduler_function, self.get_scheduler_desc())
        F.scheduler.add_job_instance(job, run=False)

    def process_menu(self, page, req):
        if page not in ('setting', 'manual', 'history'):
            page = 'setting'
        if 'newtoki_csrf' not in session:
            session['newtoki_csrf'] = secrets.token_urlsafe(32)
        return render_template('newtoki_basic.html', package_name=self.P.package_name, page=page,
                               config=self.config(), csrf_token=session['newtoki_csrf'])

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            expected = session.get('newtoki_csrf', '')
            if not expected or not secrets.compare_digest(expected, req.form.get('csrf_token', '')):
                return jsonify(ret='error', msg='페이지를 새로고침하세요.'), 403
            engine = self._engine()
            if command == 'status':
                return jsonify(ret='success', state=engine.snapshot())
            if command == 'history':
                return jsonify(ret='success', rows=engine.history.rows())
            if command == 'stop':
                engine.cancel()
                return jsonify(ret='success', msg='중지를 요청했습니다.')
            if command == 'save':
                raw = json.loads(arg1 or '{}')
                if not isinstance(raw, dict):
                    raise SiteError('설정 형식 오류')
                conf = self.validate({k: raw.get(k, self.config()[k]) for k in self.db_default})
                previous = self.config()['basic_interval']
                with self.settings_lock:
                    for k in self.db_default:
                        self.P.ModelSetting.set(k, str(conf[k]))
                self.sync_schedule(conf, previous)
                return jsonify(ret='success', msg='설정 저장 완료. 다운로드는 시작하지 않았습니다.')
            if command == 'add_auto':
                nid = novel_id(arg1)
                with self.settings_lock:
                    ids = title_ids(self.P.ModelSetting.get('titles') or '')
                    if nid not in ids:
                        if len(ids) >= 100:
                            raise SiteError('최대 100개까지 등록할 수 있습니다.')
                        ids.append(nid)
                    self.P.ModelSetting.set('titles', '\n'.join(ids))
                return jsonify(ret='success', msg='자동 수집 목록에 추가했습니다. 예약 실행 여부는 설정을 따릅니다.')
            if command in ('analyze', 'domain'):
                engine.start(command, [novel_id(arg1)], self.runtime())
            elif command == 'download':
                selected = json.loads(arg2 or '[]')
                if not isinstance(selected, list) or not selected or len(selected) > 10000 or any(not str(x).isdigit() for x in selected):
                    raise SiteError('받을 회차를 선택하세요.')
                engine.start('download', [novel_id(arg1)], self.runtime(), selected)
            elif command == 'run':
                conf = self.runtime()
                engine.start('download', title_ids(conf['titles']), conf)
            else:
                raise SiteError('지원하지 않는 명령입니다.')
            return jsonify(ret='success', msg='작업을 시작했습니다.')
        except (ValueError, TypeError, KeyError, SiteError) as exc:
            return jsonify(ret='error', msg=str(exc))
        except Exception:
            self.P.logger.exception('newtoki command failed')
            return jsonify(ret='error', msg='처리에 실패했습니다. FF 로그를 확인하세요.')

    def scheduler_function(self):
        try:
            # A challenge/locked state needs explicit user intervention; do not loop.
            if self._engine().snapshot()['status'] in ('verification_required', 'body_pending', 'locked', 'failed'):
                return
            conf = self.runtime()
            self._engine().start('download', title_ids(conf['titles']), conf)
        except Exception as exc:
            self.P.logger.warning('자동 수집 시작 실패: %s', type(exc).__name__)

    def plugin_unload(self):
        if self.engine:
            self.engine.cancel()
