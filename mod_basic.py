import json
import math
import logging
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
from .telegram_feed import TelegramFeed
from .notifications import ActivityLog, DiscordNotifier
from .storage import browse_folders, check_storage
from .domain import announcement_url, DEFAULT_ANNOUNCEMENT


class ModuleBasic(PluginModuleBase):
    db_default = {
        'announcement_url': DEFAULT_ANNOUNCEMENT, 'base_url': 'https://newtoki1.org', 'browser_url': 'http://newtoki-browser:9223',
        'download_path': '', 'titles': '', 'request_delay': '3', 'timeout': '30',
        'episode_digits': '4', 'max_per_title': '10', 'make_epub': 'True',
        'save_cover': 'True', 'epub_line_height': '1.8', 'epub_paragraph_gap': '0.55',
        'auto_domain': 'True', 'basic_auto_start': 'False', 'basic_interval': '180',
    }

    def __init__(self, P):
        super().__init__(P, first_menu='setting', name='basic', scheduler_desc='북토끼 공개 회차 수집')
        self.engine = None
        self.settings_lock = threading.RLock()
        directory = Path(F.config['path_data']) / 'db'
        self.telegram = TelegramFeed(directory)
        self.telegram_lock = threading.Lock()
        self.activity = ActivityLog(directory / 'newtoki_downloader_activity.db')
        self.discord = DiscordNotifier(directory)

    def plugin_load(self):
        if not self.P.ModelSetting.get('download_path'):
            self.P.ModelSetting.set('download_path', os.path.join(F.config['path_data'], 'downloads', 'newtoki'))
        self._engine()
        self.sync_schedule(self.config())
        self.sync_telegram_schedule()
        if hasattr(self.P.logger, 'addHandler') and self.activity not in self.P.logger.handlers:
            self.P.logger.addHandler(self.activity)
        self.record_event('INFO', '북토끼 소설 다운 시작 · 로그 활성화')

    def record_event(self, level, message):
        from .notifications import redact
        message = redact(message)
        self.activity.add(level, message)
        self.P.logger.log(getattr(logging, level, logging.INFO), message, extra={'nt_activity_written': True})

    def _engine(self):
        if self.engine is None:
            self.engine = Engine(os.path.join(F.config['path_data'], 'db', 'newtoki_downloader_history.db'))
        return self.engine

    @staticmethod
    def validate(raw):
        conf = dict(raw)
        conf['base_url'] = origin(conf['base_url'])
        conf['announcement_url'] = announcement_url(conf.get('announcement_url', DEFAULT_ANNOUNCEMENT))
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
        for key, lo, hi in [('epub_line_height', 1.2, 3), ('epub_paragraph_gap', 0, 2)]:
            conf[key] = float(conf[key])
            if not math.isfinite(conf[key]) or not lo <= conf[key] <= hi:
                raise SiteError('EPUB 문단/줄 간격 범위를 확인하세요.')
        for key in ('make_epub', 'save_cover', 'auto_domain', 'basic_auto_start'):
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
        conf['on_finished'] = self.download_finished
        conf['on_event'] = self.record_event
        return conf

    def download_finished(self, report):
        state = report['state']
        self.activity.add('ERROR' if state.get('failed') or state['status'] in ('failed', 'verification_required', 'locked', 'body_pending') else 'INFO',
                          '%s: %s · 완료 %s · 실패 %s · 건너뜀 %s' % (report['source'], state['message'], state.get('completed', 0), state.get('failed', 0), state.get('skipped', 0)))
        for item in report['downloads']:
            self.activity.add('INFO', '%s [%s] / %s. %s 저장 완료' % (item['title'], item['novel_id'], item['no'], item['episode_title']))
        if state.get('domain_change'):
            change = state['domain_change']
            self.activity.add('INFO', '구버전 도메인 변경: ' + change['old'] + ' → ' + change['new'])
        if state.get('last_error'):
            self.activity.add('ERROR', state['last_error'])
        if state.get('last_warning'):
            self.activity.add('WARNING', state['last_warning'])
        try:
            if self.discord.notify(report):
                self.activity.add('INFO', 'Discord 다운로드 결과 알림 전송 완료')
        except SiteError as exc:
            self.activity.add('ERROR', str(exc))
            self._engine().update(last_warning=str(exc))

    def sync_telegram_schedule(self, previous_interval=None):
        job_id = self.get_scheduler_name() + '_telegram'
        conf = self.telegram.settings()
        enabled = conf['enabled']
        interval = conf['interval_minutes']
        if not enabled:
            if F.scheduler.is_include(job_id):
                F.scheduler.remove_job(job_id)
        elif (not F.scheduler.is_include(job_id)
              or (previous_interval is not None and previous_interval != interval)):
            if F.scheduler.is_include(job_id):
                F.scheduler.remove_job(job_id)
            job = Job(self.P.package_name, job_id, str(interval), self.telegram_poll, '북토끼 텔레그램 링크 수신')
            F.scheduler.add_job_instance(job, run=False)

    def telegram_append(self, ids):
        if not ids:
            return []
        with F.app.app_context(), self.settings_lock:
            current = title_ids(self.P.ModelSetting.get('titles') or '')
            added = [nid for nid in dict.fromkeys(ids) if nid not in current]
            if len(current) + len(added) > 100:
                raise SiteError('자동 수집 목록이 100개를 초과합니다. 목록 정리 후 다시 수신하세요.')
            if added:
                self.P.ModelSetting.set('titles', '\n'.join(current + added))
                self.activity.add('INFO', '텔레그램 자동 목록 추가: ' + ', '.join(added))
            return added

    def telegram_poll(self):
        import fcntl
        if not self.telegram_lock.acquire(blocking=False):
            return '다른 수신 작업이 진행 중입니다.'
        try:
            self.telegram.directory.mkdir(parents=True, exist_ok=True)
            with open(self.telegram.directory / 'newtoki_downloader_telegram.lock', 'a') as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return '다른 수신 작업이 진행 중입니다.'
                try:
                    return self.telegram.poll(self.telegram_append)
                except SiteError as exc:
                    self.activity.add('ERROR', str(exc))
                    state = self.telegram.state()
                    state['message'] = str(exc)
                    self.telegram.write_state(state)
                    return str(exc)
        finally:
            self.telegram_lock.release()

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
        if page == 'log':
            page = 'activity'
        if page not in ('setting', 'manual', 'history', 'integration', 'activity'):
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
            if command == 'activity':
                return jsonify(ret='success', rows=self.activity.rows())
            if command == 'integration_settings':
                return jsonify(ret='success', telegram=self.telegram.public_settings(), discord=self.discord.public_settings(), state=self.telegram.state()['message'])
            if command == 'discord_save':
                return jsonify(ret='success', config=self.discord.save(json.loads(arg1 or '{}')), msg='알림 설정 저장 완료')
            if command == 'discord_test':
                self.discord.test()
                return jsonify(ret='success', msg='테스트 알림 전송 완료')
            if command == 'telegram_save':
                with self.telegram_lock:
                    previous = self.telegram.settings()['interval_minutes']
                    result = self.telegram.save(json.loads(arg1 or '{}'))
                    self.sync_telegram_schedule(previous)
                return jsonify(ret='success', config=result, msg='텔레그램 설정 저장 완료. 다운로드는 시작하지 않습니다.')
            if command == 'telegram_check':
                return jsonify(ret='success', msg='연결 확인: ' + self.telegram.check(self.telegram.settings()))
            if command == 'telegram_poll':
                return jsonify(ret='success', msg=self.telegram_poll())
            if command == 'titles':
                ids = title_ids(self.config()['titles'])
                rows = engine.history.query('SELECT novel_id, COUNT(*) AS count FROM entries WHERE status=? GROUP BY novel_id', ('completed',))
                counts = {r['novel_id']: r['count'] for r in rows}
                return jsonify(ret='success', rows=[dict(id=nid, completed=counts.get(nid, 0), **engine.book_info(nid)) for nid in ids])
            if command == 'remove_auto':
                nid = novel_id(arg1)
                with self.settings_lock:
                    ids = title_ids(self.P.ModelSetting.get('titles') or '')
                    self.P.ModelSetting.set('titles', '\n'.join(x for x in ids if x != nid))
                return jsonify(ret='success', msg='자동 목록에서 제외했습니다. 저장 파일과 이력은 유지됩니다.')
            if command == 'run_title':
                engine.start('download', [novel_id(arg1)], self.runtime())
                return jsonify(ret='success', msg='작품 수집 시작')
            if command == 'browse_path':
                return jsonify(ret='success', folders=browse_folders(str(arg1 or '')))
            if command == 'check_path':
                return jsonify(ret='success', storage=check_storage(arg1))
            if command == 'status':
                return jsonify(ret='success', state=engine.snapshot())
            if command == 'history':
                return jsonify(ret='success', rows=engine.history.rows())
            if command == 'stop':
                engine.cancel()
                return jsonify(ret='success', msg='중지를 요청했습니다.')
            if command == 'save_connection':
                raw = json.loads(arg1 or '{}')
                if not isinstance(raw, dict):
                    raise SiteError('연결 설정 형식 오류')
                keys = ('base_url', 'browser_url', 'announcement_url', 'auto_domain')
                with self.settings_lock:
                    conf = self.config()
                    conf.update({k: raw[k] for k in keys if k in raw})
                    conf = self.validate(conf)
                    for key in keys:
                        self.P.ModelSetting.set(key, str(conf[key]))
                return jsonify(ret='success', msg='연결 / 도메인 설정 저장 완료. 다운로드는 시작하지 않았습니다.')
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
            self.record_event('ERROR', '명령 ' + str(command) + ' 실패: ' + str(exc))
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
            conf['source'] = '예약 수집'
            self._engine().start('download', title_ids(conf['titles']), conf)
        except Exception as exc:
            self.P.logger.warning('자동 수집 시작 실패: %s', type(exc).__name__)

    def plugin_unload(self):
        if hasattr(self.P.logger, 'removeHandler'):
            self.P.logger.removeHandler(self.activity)
        job_id = self.get_scheduler_name() + "_telegram"
        if F.scheduler.is_include(job_id):
            F.scheduler.remove_job(job_id)
        if self.engine:
            self.engine.cancel()
