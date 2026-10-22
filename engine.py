import copy
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

try:
    from .parser import SiteError, Blocked, DailyVerificationRequired, Locked, AccessLimited, Pending, EmptyBody, Cancelled, parse_catalog, discover_legacy, origin, novel_id
    from .browser_client import BrowserClient, NoRedirect, CatalogStructureError, DomainUnavailable, BrowserConnectionError
    from .domain import DEFAULT_ANNOUNCEMENT, DEFAULT_BASE, DEFAULT_MODERN, fetch_announcement, discover_candidates, domain_candidates
except ImportError:
    from parser import SiteError, Blocked, DailyVerificationRequired, Locked, AccessLimited, Pending, EmptyBody, Cancelled, parse_catalog, discover_legacy, origin, novel_id
    from browser_client import BrowserClient, NoRedirect, CatalogStructureError, DomainUnavailable, BrowserConnectionError
    from domain import DEFAULT_ANNOUNCEMENT, DEFAULT_BASE, DEFAULT_MODERN, fetch_announcement, discover_candidates, domain_candidates

ANNOUNCEMENT = DEFAULT_ANNOUNCEMENT


class CatalogReloadRequired(SiteError):
    """Replace the remaining queue from a validated destination catalog."""
    def __init__(self, book, previous):
        super().__init__('새주소 목록으로 남은 회차를 다시 구성합니다.')
        self.book, self.previous = book, previous


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def korean_day():
    return datetime.fromisoformat(now()).astimezone(timezone(timedelta(hours=9))).date().isoformat()


def safe_name(text):
    text = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', str(text)).strip(' .')
    return text[:70].strip(' .') or '제목없음'


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.newtoki-', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, str(path))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def safe_path(root, name):
    root = Path(root).resolve()
    path = root / name
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise SiteError('저장 경로가 지정 폴더 밖으로 연결되어 있습니다.') from None
    return path


def collect_catalog(client, nid, update=lambda **kw: None, read_page=None, first_html=None):
    pending, visited, seen_pages, episodes = {1}, set(), set(), {}
    book = None
    while pending:
        client.check()
        page = min(pending)
        pending.remove(page)
        if page in visited:
            continue
        update(message='회차 목록 %s페이지 확인 중' % page)
        path = '/novel/' + nid + ('?epage=' + str(page) if page > 1 else '')
        html = first_html if page == 1 and first_html is not None else (read_page or client.html)(path)
        data = parse_catalog(html, client.base + path)
        if data.get('has_more') or (data.get('total_episodes') is not None and len(data['episodes']) != data['total_episodes']):
            raise SiteError('새 사이트의 전체 회차 목록 로딩이 끝나지 않았습니다. 일부 목록으로 진행하지 않습니다.')
        signature = tuple(e['id'] for e in data['episodes'])
        if signature in seen_pages:
            raise SiteError('목록 페이지가 반복됩니다. 일부 목록만으로 다운로드하지 않고 중단했습니다.')
        seen_pages.add(signature)
        visited.add(page)
        if book is None:
            book = {k: v for k, v in data.items() if k not in ('episodes', 'pages')}
        elif data['title'] != book['title']:
            raise SiteError('페이지 사이 작품 제목이 달라졌습니다.')
        for item in data['episodes']:
            previous = episodes.get(item['id'])
            if previous and previous != item:
                raise SiteError('목록이 수집 중 변경되었습니다. 다시 분석하세요.')
            episodes[item['id']] = item
        # Links can expose only a window of page numbers; discover at every page.
        pending.update(set(data['pages']) - visited)
        if len(visited) + len(pending) > 1000:
            raise SiteError('목록 페이지가 1000개를 넘습니다.')
    book['episodes'] = sorted(episodes.values(), key=lambda e: (e['no'], int(e['id'])))
    book['pages'] = sorted(visited)
    return book


class History:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.query('CREATE TABLE IF NOT EXISTS entries (novel_id TEXT, episode_id TEXT, no INTEGER, title TEXT, status TEXT, path TEXT, digest TEXT, error TEXT, updated TEXT, PRIMARY KEY(novel_id,episode_id))')
        self.query('CREATE TABLE IF NOT EXISTS catalogs (novel_id TEXT PRIMARY KEY, data TEXT, updated TEXT)')

    def query(self, sql, args=()):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                return [dict(r) for r in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    def rows(self, nid=None):
        if nid:
            return self.query('SELECT * FROM entries WHERE novel_id=? ORDER BY no,episode_id', (nid,))
        return self.query('SELECT * FROM entries ORDER BY updated DESC LIMIT 200')

    def cache_catalog(self, book):
        data = {k: v for k, v in book.items() if k != 'episodes'}
        data['episodes'] = [{k: e.get(k) for k in ('id', 'no', 'title', 'locked', 'pending')} for e in book['episodes']]
        self.query('INSERT OR REPLACE INTO catalogs VALUES (?,?,?)', (book['id'], json.dumps(data, ensure_ascii=False), now()))

    def catalogs(self):
        return [json.loads(r['data']) for r in self.query('SELECT data FROM catalogs')]

    def record(self, nid, item, status, path='', digest='', error=''):
        self.query('INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?,?,?,?)',
                   (nid, item['id'], item['no'], item['title'], status, str(path), digest, error, now()))

    def valid(self, nid, item, root):
        rows = self.query('SELECT * FROM entries WHERE novel_id=? AND episode_id=?', (nid, item['id']))
        return bool(rows and valid_record(rows[0], root))


def valid_record(row, root):
    try:
        path = Path(row['path']).resolve()
        path.relative_to(Path(root).resolve())
        return row['status'] == 'completed' and path.stat().st_size > 0 and hashlib.sha256(path.read_bytes()).hexdigest() == row['digest']
    except (ValueError, OSError):
        return False


def epub_filename(book, records):
    first = min(int(row['no']) for row in records)
    last = max(records, key=lambda row: int(row['no']))
    marker = re.search(r'(?:^|[\s_\-\[(【])(완료|완)[\s\])】.!_\-]*$', Path(last['path']).stem)
    suffix = ' %03d~%03d' % (first, int(last['no']))
    return safe_name(book['title']) + suffix + (' ' + marker[1] if marker else '') + '.epub'


def remove_previous_epubs(folder, book, target):
    """Replace only our own earlier generated editions, never unrelated user EPUBs."""
    import xml.etree.ElementTree as ET
    pattern = re.compile(re.escape(safe_name(book['title'])) + r'(?: [0-9]{3,}~[0-9]{3,}(?: (?:완료|완))?)?\.epub$')
    for path in Path(folder).glob('*.epub'):
        if path == target or path.is_symlink() or not pattern.fullmatch(path.name):
            continue
        try:
            with zipfile.ZipFile(path) as z:
                if z.getinfo('OEBPS/content.opf').file_size > 512 * 1024:
                    continue
                opf = ET.fromstring(z.read('OEBPS/content.opf'))
            identifier = opf.find('.//{http://purl.org/dc/elements/1.1/}identifier[@id="book-id"]')
            title = opf.find('.//{http://purl.org/dc/elements/1.1/}title')
            if identifier is not None and identifier.text == 'newtoki:' + str(book['id']) and title is not None and title.text == book['title']:
                path.unlink()
        except (OSError, KeyError, ValueError, zipfile.BadZipFile, ET.ParseError):
            continue


def make_epub(folder, book, records, check=lambda: None, config=None):
    config = config or {}
    records = sorted(records, key=lambda row: int(row['no']))
    if not records:
        return
    buffer = io.BytesIO()
    manifest, spine, nav = [], [], []
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        cover_meta = ''
        cover = cover_file(folder)
        if cover:
            path, mime = cover
            z.writestr('OEBPS/' + path.name, path.read_bytes())
            z.writestr('OEBPS/cover.xhtml', '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>표지</title></head><body style="margin:0;text-align:center"><img src="' + path.name + '" alt="표지" style="max-width:100%;max-height:100%"/></body></html>')
            manifest += ['<item id="cover-image" href="' + path.name + '" media-type="' + mime + '" properties="cover-image"/>', '<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>']
            spine.append('<itemref idref="cover-page"/>')
            cover_meta = '<meta name="cover" content="cover-image"/>'
        for index, row in enumerate(records):
            check()
            title = '%s. %s' % (row['no'], row['title'])
            raw = Path(row['path']).read_bytes()
            if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
                text = raw.decode('utf-16')
            else:
                try:
                    text = raw.decode('utf-8-sig')
                except UnicodeDecodeError:
                    text = raw.decode('cp949', errors='replace')
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            if '\n\n' in text and text.split('\n\n', 1)[0].strip() == title:
                text = text.split('\n\n', 1)[1]
            name = 'chapter-%s.xhtml' % row['episode_id']
            paragraphs = ''.join('<p>' + escape(p).replace('\n', '<br/>') + '</p>' for p in text.split('\n\n') if p.strip())
            xhtml = '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ko"><head><title>' + escape(title) + '</title><style>body{line-height:' + str(float(config.get('epub_line_height', 1.8))) + '}p{margin:0 0 ' + str(float(config.get('epub_paragraph_gap', .55))) + 'em}</style></head><body><h2>' + escape(title) + '</h2>' + paragraphs + '</body></html>'
            z.writestr('OEBPS/' + name, xhtml)
            manifest.append('<item id="c%s" href="%s" media-type="application/xhtml+xml"/>' % (index, name))
            spine.append('<itemref idref="c%s"/>' % index)
            nav.append('<li><a href="%s">%s</a></li>' % (name, escape(title)))
        z.writestr('OEBPS/nav.xhtml', '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>목차</title></head><body><nav epub:type="toc"><ol>' + ''.join(nav) + '</ol></nav></body></html>')
        z.writestr('OEBPS/content.opf', '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">newtoki:' + book['id'] + '</dc:identifier><dc:title>' + escape(book['title']) + '</dc:title><dc:creator>' + escape(book.get('author') or '') + '</dc:creator><dc:language>ko</dc:language><meta property="dcterms:modified">' + datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ') + '</meta>' + cover_meta + '<dc:description>' + escape(book.get('description', '')) + '</dc:description></metadata><manifest><item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>' + ''.join(manifest) + '</manifest><spine>' + ''.join(spine) + '</spine></package>')
    check()
    target = safe_path(folder, epub_filename(book, records))
    atomic_write(target, buffer.getvalue())
    remove_previous_epubs(folder, book, target)
    return target


def image_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg', 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png', 'image/png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp', 'image/webp'
    raise SiteError('표지 응답이 JPEG/PNG/WebP 이미지가 아닙니다.')


def cover_file(folder):
    for ext in ('jpg', 'png', 'webp'):
        path = safe_path(folder, 'cover.' + ext)
        if path.is_file():
            try:
                actual, mime = image_type(path.read_bytes())
                if actual == ext:
                    return path, mime
            except (OSError, SiteError):
                pass
    return None


class Engine:
    def __init__(self, db_path, factory=BrowserClient):
        self.history, self.factory = History(db_path), factory
        self.guard, self.stop = threading.Lock(), threading.Event()
        self.resume_signal = threading.Event()
        self.lock_path = str(db_path) + '.lock'
        self.thread = None
        self.on_event = None
        self.on_verification = None
        self.history.query('CREATE TABLE IF NOT EXISTS books (id TEXT PRIMARY KEY, title TEXT, author TEXT, updated TEXT)')
        self.history.query('CREATE TABLE IF NOT EXISTS verified_domain_handoff (id INTEGER PRIMARY KEY CHECK(id=1), day TEXT, base TEXT)')
        self.state = {'status': 'idle', 'message': '대기 중', 'completed': 0, 'skipped': 0, 'failed': 0}

    def book_info(self, nid):
        rows = self.history.query('SELECT title,author,updated FROM books WHERE id=?', (nid,))
        return rows[0] if rows else {'title': '분석 전', 'author': '', 'updated': ''}

    def recent_books(self):
        return self.history.query('SELECT id,title FROM books ORDER BY updated DESC,rowid DESC LIMIT 5')

    def update(self, **values):
        with self.guard:
            changed = {k: v for k, v in values.items() if self.state.get(k) != v}
            self.state.update(values)
        callback = self.on_event
        if callback:
            for key in ('message', 'last_error', 'last_warning'):
                if changed.get(key):
                    try:
                        callback('ERROR' if key == 'last_error' else 'WARNING' if key == 'last_warning' else 'INFO', str(changed[key]))
                    except Exception:
                        pass

    def snapshot(self):
        with self.guard:
            return copy.deepcopy(self.state)

    def stop_check(self):
        if self.stop.is_set():
            raise Cancelled('중지했습니다.')

    def cancel(self):
        self.stop.set()
        self.resume_signal.set()

    def resume(self):
        with self.guard:
            if self.state['status'] != 'verification_waiting' or not self.thread or not self.thread.is_alive():
                raise SiteError('현재 보안 확인 대기 중인 작업이 없습니다.')
            self.resume_signal.set()

    def read_page(self, client, path, catalog=False):
        resumed, announced = False, False
        while True:
            self.stop_check()
            client.check()
            try:
                html = client.continue_html(path) if resumed else client.html(path)
                if catalog and isinstance(client, BrowserClient):
                    html = client.wait_catalog(path, html, self.update)
                client.keep_tab = False
                return html
            except Blocked as exc:
                client.keep_tab = True
                self.resume_signal.clear()
                self.update(status='verification_waiting', last_error=str(exc), episode_no=None,
                            verification_context='',
                            verification_url=client.base + path,
                            message='작품 목록 보안 확인 대기 · 전용 브라우저의 현재 탭에서 직접 확인 후 계속을 누르세요.')
                if self.on_verification and not announced:
                    try:
                        self.on_verification(self.snapshot())
                    except Exception as notify_error:
                        self.update(last_warning='보안 확인 대기 알림 실패: ' + type(notify_error).__name__)
                announced = True
                while not self.resume_signal.wait(.5):
                    self.stop_check()
                    client.check()
                self.stop_check()
                self.update(status='running', last_error='', message='확인한 작업 탭에서 작품 목록 읽는 중')
                resumed = True

    def reload_modern_catalog(self, client, nid, config):
        """Reload the destination list first, pausing wherever human verification appears."""
        previous = client.base
        day = korean_day()
        try:
            html, source = fetch_announcement(config.get('announcement_url', ANNOUNCEMENT), client.check)
            _, modern = discover_candidates(html, source)
        except Cancelled:
            raise
        except SiteError as exc:
            modern = []
            self.update(warnings=True, last_warning='새주소 안내 확인 실패: ' + str(exc))
        client.base = modern[0] if modern else DEFAULT_MODERN
        self.update(base_url=client.base, verification_context='', message='새주소 작품 목록 다시 불러오는 중 · 인증이 나오면 확인 후 계속을 누르세요.')
        alternate = collect_catalog(client, nid, self.update, lambda path: self.read_page(client, path, catalog=True))
        if alternate['title'] != self.book_info(nid)['title']:
            raise SiteError('새주소의 작품 제목이 기존 작품과 다릅니다. 이어받기를 중단합니다.')
        return CatalogReloadRequired(alternate, {'base': previous, 'day': day})

    def accept_reloaded_catalog(self, client, config, previous):
        save = config.get('save_domain')
        if save:
            save(client.base)
        self.history.query('INSERT OR REPLACE INTO verified_domain_handoff VALUES (1,?,?)', (previous['day'], client.base))
        self.update(base_url=client.base, domain_change={'old': previous['base'], 'new': client.base, 'time': now()},
                    verification_context='', last_error='', message='새주소 목록과 기존 저장 파일 확인 완료 · 남은 회차를 이어받습니다.')

    def read_body(self, client, nid, item, on_wait=None, config=None):
        if item.get('pending'):
            raise EmptyBody('목록에 준비중으로 표시된 회차입니다. 본문 요청 없이 미저장으로 남깁니다.')
        resumed, retries, announced = False, 0, False
        switched = False
        while True:
            self.stop_check()
            client.check()
            try:
                text = client.continue_body(nid, item) if resumed else client.body(nid, item)
                client.keep_tab = False
                return text
            except Blocked as exc:
                # Never retry, refresh or skip a security check automatically.
                client.keep_tab = True
                self.resume_signal.clear()
                if on_wait and not announced:
                    try:
                        on_wait()
                    except Cancelled:
                        raise
                    except Exception as export_error:
                        self.update(last_warning='대기 전 EPUB 저장 실패: ' + type(export_error).__name__)
                if (isinstance(exc, DailyVerificationRequired) and not switched and config
                        and config.get('auto_domain') and config.get('domain_mode', 'legacy_first') == 'legacy_first'
                        and urlsplit(client.base).hostname.startswith('newtoki')):
                    switched = True
                    raise self.reload_modern_catalog(client, nid, config)
                self.update(status='verification_waiting', last_error=str(exc),
                            verification_url=client.base + '/novel/%s/%s' % (nid, item['id']),
                            message='보안 확인 대기 · 현재 %s회차 / %s편 저장 완료 · 전용 브라우저에서 인증 후 확인 후 계속을 누르세요.' % (item['no'], self.snapshot().get('completed', 0)))
                if self.on_verification and not announced:
                    try:
                        self.on_verification(self.snapshot())
                    except Exception as notify_error:
                        self.update(last_warning='보안 확인 대기 알림 실패: ' + type(notify_error).__name__)
                announced = True
                while not self.resume_signal.wait(.5):
                    self.stop_check()
                    client.check()
                self.stop_check()
                client.check()
                self.update(status='running', message='확인한 작업 탭에서 현재 회차 본문 읽는 중', last_error='')
                resumed = True
            except DomainUnavailable:
                if resumed or switched or not config or not config.get('auto_domain'):
                    raise
                switched = True
                previous = client.base
                self.update(message='주소 연결 장애 · 대체 주소에서 작품과 회차를 다시 확인합니다.')
                html = self.resolve(client, nid, config, exclude=(previous,))
                alternate = collect_catalog(client, nid, self.update, lambda path: self.read_page(client, path, catalog=True), html)
                match = next((e for e in alternate['episodes'] if e['id'] == item['id']), None)
                if not match or match['no'] != item['no']:
                    raise SiteError('대체 주소의 회차 ID/번호가 다릅니다. 다시 작품 분석을 실행하세요.')
                if match['locked']:
                    raise Locked('대체 주소에서 해당 회차가 잠겨 있습니다.')
            except Pending as exc:
                if retries >= 1:
                    if resumed and not isinstance(exc, EmptyBody):
                        client.keep_tab = True
                        self.resume_signal.clear()
                        self.update(status='verification_waiting', last_error=str(exc),
                                    verification_url=client.base + '/novel/%s/%s' % (nid, item['id']),
                                    message='확인한 본문이 아직 준비되지 않았습니다. 같은 탭에서 표시 완료 후 확인 후 계속을 누르세요.')
                        while not self.resume_signal.wait(.5):
                            self.stop_check()
                            client.check()
                        self.stop_check()
                        retries = 0
                        self.update(status='running', last_error='', message='확인한 작업 탭에서 현재 회차 본문 읽는 중')
                        continue
                    raise
                retries += 1
                self.update(message='일시적 본문 로딩 지연 · 2초 후 같은 회차를 한 번 다시 확인합니다.')
                if self.stop.wait(2):
                    client.check()
                # After human confirmation keep reading the same tab, even if loading is slow.
                # Navigating again here would discard the page the user just confirmed.

    def batch_rest(self, seconds):
        deadline = time.monotonic() + seconds
        self.update(phase='batch_rest', message='연속 수집 휴식 · %s초 동안 새 페이지를 요청하지 않습니다.' % seconds)
        try:
            while True:
                self.stop_check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.update(rest_remaining=int(remaining) + 1)
                self.stop.wait(min(1, remaining))
        finally:
            self.update(phase='', rest_remaining=0)

    def start(self, kind, ids, config, selected=None):
        import fcntl
        if kind not in ('analyze', 'download', 'domain', 'files_scan', 'files_apply', 'files_epub') or (not ids and kind not in ('files_scan', 'files_apply', 'files_epub')):
            raise SiteError('작업 종류와 작품 번호가 필요합니다.')
        ids = [novel_id(x) for x in ids]
        with self.guard:
            if self.state['status'] in ('running', 'verification_waiting'):
                raise SiteError('작업이 진행 중입니다.')
            handle = open(self.lock_path, 'a')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise SiteError('다른 FF 프로세스에서 작업 중입니다.') from None
            self.stop.clear()
            self.resume_signal.clear()
            self.on_event = config.get('on_event')
            self.on_verification = config.get('on_verification')
            self.state = {'status': 'running', 'message': '작업 준비 중', 'kind': kind, 'completed': 0,
                          'skipped': 0, 'failed': 0, 'done': 0, 'total': 0, 'started': now(),
                          'manual_mode': config.get('manual_mode', '')}
        self.thread = threading.Thread(target=self.run, args=(kind, ids, dict(config), selected, handle), daemon=True)
        try:
            self.thread.start()
        except Exception:
            handle.close()
            self.update(status='failed', message='작업 스레드를 시작하지 못했습니다.')
            raise

    def resolve(self, client, nid, config, exclude=()):
        old = client.base
        self.update(message='안내 사이트에서 구주소·새주소 확인 중')
        warning = ''
        try:
            html, source = fetch_announcement(config.get('announcement_url', ANNOUNCEMENT), client.check)
            legacy, modern = discover_candidates(html, source)
        except Cancelled:
            raise
        except SiteError as exc:
            # An announcement outage must not disable a working current address.
            legacy, modern = [], []
            warning = str(exc) + ' 현재 저장 주소의 작품 목록을 확인합니다.'
            self.update(warnings=True, last_warning=warning)
        mode = config.get('domain_mode', 'legacy_first')
        confirmed = self.history.query('SELECT base FROM verified_domain_handoff WHERE id=1 AND day=?', (korean_day(),))
        current = old
        if mode == 'legacy_first' and confirmed:
            current, mode = origin(confirmed[0]['base']), 'modern_only'
        candidates = domain_candidates(current, legacy, modern, mode, exclude)
        attempts, errors = [], []
        expected_title = self.book_info(nid)['title']
        for candidate in candidates:
            client.check()
            client.base = candidate
            family = '구주소' if urlsplit(candidate).hostname.startswith('newtoki') else '새주소'
            kind = ('현재 ' if candidate == old else '안내된 ' if candidate in legacy + modern else '기본 ') + family
            self.update(message=kind + ' 작품 목록 확인: ' + candidate)
            try:
                html = self.read_page(client, '/novel/' + nid, catalog=True)
                book = parse_catalog(html, client.base + '/novel/' + nid)
                if expected_title != '분석 전' and book['title'] != expected_title:
                    raise SiteError('같은 작품 번호의 제목이 기존 작품과 다릅니다. 주소를 적용하지 않습니다.')
            except Cancelled:
                client.base = old
                raise
            except (Blocked, Locked, AccessLimited, BrowserConnectionError) as exc:
                attempts.append({'url': client.base, 'requested_url': candidate, 'kind': kind, 'status': exc.code, 'message': str(exc)})
                self.update(domain_attempts=attempts)
                client.base = old
                raise
            except SiteError as exc:
                attempts.append({'url': client.base, 'requested_url': candidate, 'kind': kind, 'status': exc.code, 'message': str(exc)})
                errors.append(exc)
                self.update(domain_attempts=attempts)
                if isinstance(exc, CatalogStructureError) and not (config.get('domain_mode', 'legacy_first') == 'legacy_first' and family == '구주소'):
                    client.base = old
                    raise
                continue
            except Exception as exc:
                error = SiteError('주소 접속 실패: ' + type(exc).__name__)
                attempts.append({'url': client.base, 'requested_url': candidate, 'kind': kind, 'status': error.code, 'message': str(error)})
                errors.append(error)
                self.update(domain_attempts=attempts)
                continue
            final_base = client.base
            attempts.append({'url': final_base, 'requested_url': candidate, 'kind': kind, 'status': 'completed', 'message': '작품 목록 정상 확인'})
            self.update(domain_attempts=attempts, base_url=final_base)
            if config.get('manual_mode'):
                self.update(domain_selection=final_base)
            elif final_base != old:
                try:
                    save = config.get('save_domain')
                    if save:
                        save(final_base)
                except Exception:
                    client.base = old
                    raise
                self.update(domain_change={'old': old, 'new': final_base, 'time': now()})
            return html
        client.base = old
        detail = '\n'.join(x['kind'] + ' ' + x['url'] + ': ' + x['message'] for x in attempts)
        message = '모든 후보의 작품 목록 확인에 실패했습니다. 저장 주소는 유지합니다.\n' + detail
        if any(isinstance(exc, Blocked) for exc in errors):
            raise Blocked(message + '\n전용 브라우저에서 표시된 주소의 사람 확인을 직접 처리한 뒤 도메인 확인을 다시 실행하세요.')
        raise SiteError(message)

    def run(self, kind, ids, config, selected, handle):
        client = None
        downloads = []
        book, root = None, None
        source = config.get('source', 'EPUB 다시 생성' if kind == 'files_epub' else '기존 파일 검사' if kind == 'files_scan' else '기존 파일 번호 정리' if kind == 'files_apply' else '작품 분석' if kind == 'analyze' else '도메인 확인' if kind == 'domain' else '수동 다운로드' if selected is not None else '자동 목록 수집')
        self.update(message=source + ' 시작' + (' · 작품 ' + ', '.join(ids) if ids else ''))
        try:
            root = Path(config['download_path']).resolve()
            if kind in ('files_scan', 'files_apply', 'files_epub'):
                try:
                    from .file_manager import manage_files, rebuild_epubs, selected_paths
                except ImportError:
                    from file_manager import manage_files, rebuild_epubs, selected_paths
                folders = selected_paths(root, config.get('file_folders'))
                report = manage_files(root, self.history, kind == 'files_apply', self.stop_check, self.update, folders)
                if kind == 'files_epub':
                    self.update(file_report=report)
                    rebuild_epubs(root, self.history, config, report, self.stop_check, self.update, folders)
                self.update(history_imported=report['history_imported'], history_missing=report['history_missing'])
                count = report['files_renamed'] + report['folders_renamed'] + report['history_repaired']
                if kind == 'files_epub':
                    self.update(file_report=report, completed=report['epub_created'], failed=report['epub_failed'],
                                status='failed' if report['epub_failed'] else 'completed', finished=now(),
                                message='EPUB 다시 생성 완료 · 생성 %s · 실패 %s' % (report['epub_created'], report['epub_failed']))
                    return
                self.update(file_report=report, completed=count, status='completed', finished=now(),
                            message='기존 파일 %s 완료 · 변경 예정 %s · 파일 변경 %s · 폴더 변경 %s · 충돌 %s · 제외 %s' % ('정리' if report['applied'] else '검사', report['planned'], report['files_renamed'], report['folders_renamed'], report['conflicts'], report['skipped']))
                return
            client = self.factory(config['base_url'], config['browser_url'], config['request_delay'], self.stop, config['timeout'])
            batch_count = 0
            batch_size = int(config.get('batch_size', 10))
            batch_pause = int(config.get('batch_pause', 60))
            first_html = None
            if kind == 'domain' or config.get('auto_domain'):
                first_html = self.resolve(client, ids[0], config)
            if kind != 'domain':
                for nid in ids:
                    if kind == 'download' and batch_count >= batch_size:
                        self.batch_rest(batch_pause)
                        batch_count = 0
                    if first_html is None and config.get('auto_domain'):
                        first_html = self.resolve(client, nid, config)
                    book = collect_catalog(client, nid, self.update, lambda path: self.read_page(client, path, catalog=True), first_html)
                    first_html = None
                    self.history.cache_catalog(book)
                    try:
                        from .reconcile import reconcile_book
                    except ImportError:
                        from reconcile import reconcile_book
                    local = reconcile_book(root, self.history, book, client.check, self.update)
                    self.update(history_imported=self.snapshot().get('history_imported', 0) + local['imported'] + local['refreshed'],
                                history_missing=self.snapshot().get('history_missing', 0) + local['missing'])
                    self.history.query('INSERT OR REPLACE INTO books VALUES (?,?,?,?)', (nid, book['title'], book['author'], now()))
                    if kind == 'analyze':
                        for item in book['episodes']:
                            item['have'] = self.history.valid(nid, item, root)
                        self.update(analysis=book)
                        continue
                    wanted = set(map(str, selected)) if selected is not None else None
                    if wanted is not None and not wanted.issubset({e['id'] for e in book['episodes']}):
                        raise SiteError('선택 회차가 현재 목록에 없습니다. 다시 분석하세요.')
                    candidates = []
                    skipped = self.snapshot()['skipped']
                    for item in book['episodes']:
                        if wanted is not None and item['id'] not in wanted:
                            continue
                        if item['locked']:
                            if not self.history.valid(nid, item, root):
                                self.history.record(nid, item, 'locked', error='목록에 잠금/유료 표시')
                            skipped += 1
                        elif self.history.valid(nid, item, root):
                            skipped += 1
                        else:
                            candidates.append(item)
                    if wanted is None:
                        candidates = candidates[:config['max_per_title']]
                    self.update(total=len(candidates), done=0, skipped=skipped, title=book['title'])
                    folder = safe_path(root, safe_name(book['title']))
                    if folder.exists() and any(folder.iterdir()):
                        try:
                            existing = json.loads(safe_path(folder, 'metadata.json').read_text(encoding='utf-8'))
                        except FileNotFoundError:
                            # An exact work-title folder may contain externally supplied TXT files.
                            prefix = str(folder) + os.sep
                            claims = self.history.query('SELECT novel_id FROM entries WHERE novel_id<>? AND substr(path,1,?)=? LIMIT 1', (nid, len(prefix), prefix))
                            if claims:
                                raise SiteError('같은 이름의 폴더가 다른 작품 이력에 사용되고 있습니다.')
                            existing = {'id': nid}
                        except (OSError, ValueError):
                            raise SiteError('같은 이름의 기존 폴더 정보를 읽지 못했습니다.') from None
                        if str(existing.get('id')) != nid:
                            raise SiteError('같은 제목의 다른 작품 폴더가 있습니다. 저장 경로를 구분하세요.')
                    folder.mkdir(parents=True, exist_ok=True)
                    if config.get('save_cover', True) and book.get('cover_url'):
                        try:
                            # Return to the first catalog page: its cover is an ordinary loaded resource.
                            self.read_page(client, '/novel/' + nid)
                            payload = client.cover(book['cover_url'])
                            ext, mime = image_type(payload)
                            atomic_write(safe_path(folder, 'cover.' + ext), payload)
                            for other in ('jpg', 'png', 'webp'):
                                if other != ext:
                                    safe_path(folder, 'cover.' + other).unlink(missing_ok=True)
                        except (Blocked, Pending, Cancelled):
                            raise
                        except Exception as exc:
                            self.update(warnings=True, last_warning='표지 저장 실패: ' + type(exc).__name__)
                    metadata = {k: v for k, v in book.items() if k != 'episodes'}
                    atomic_write(safe_path(folder, 'metadata.json'), json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'))
                    atomic_write(safe_path(folder, 'info.xml'), ('<?xml version="1.0" encoding="utf-8"?><book><title>' + escape(book['title']) + '</title><author>' + escape(book['author']) + '</author></book>').encode('utf-8'))
                    i = 0
                    while i < len(candidates):
                        item = candidates[i]
                        client.check()
                        if batch_count >= batch_size:
                            self.batch_rest(batch_pause)
                            batch_count = 0
                        self.update(message='받는 중: %s / %s회차 · 작업 %s/%s' % (book['title'], item['no'], i + 1, len(candidates)), episode_id=item['id'], episode_no=item['no'], position=i + 1)
                        try:
                            text = self.read_body(client, nid, item, lambda: self.export(folder, book, root, config, client), config=config)
                            filename = '%0*d_%s.txt' % (config['episode_digits'], item['no'], safe_name(item['title']))
                            path = safe_path(folder, filename)
                            if path.exists():
                                same = self.history.query('SELECT * FROM entries WHERE novel_id=? AND episode_id=? AND path=?', (nid, item['id'], str(path)))
                                if not same:
                                    raise SiteError('같은 이름의 파일이 있어 덮어쓰지 않았습니다: ' + path.name)
                            data = ('%s. %s\n\n%s\n' % (item['no'], item['title'], text)).encode('utf-8')
                            client.check()
                            atomic_write(path, data)
                            self.history.record(nid, item, 'completed', path, hashlib.sha256(data).hexdigest())
                            downloads.append(dict(novel_id=nid, title=book['title'], no=item['no'], episode_title=item['title']))
                            self.update(completed=self.snapshot()['completed'] + 1)
                            batch_count += 1
                        except CatalogReloadRequired as reload:
                            alternate = reload.book
                            remaining = {int(e['no']) for e in candidates[i:]}
                            available = {int(e['no']) for e in alternate['episodes']}
                            if not remaining.issubset(available):
                                raise SiteError('새주소 목록에 남은 선택 회차 번호가 없습니다: ' + ', '.join(map(str, sorted(remaining - available))))
                            self.history.cache_catalog(alternate)
                            local = reconcile_book(root, self.history, alternate, client.check, self.update)
                            self.update(history_imported=self.snapshot().get('history_imported', 0) + local['imported'] + local['refreshed'],
                                        history_missing=self.snapshot().get('history_missing', 0) + local['missing'])
                            next_items, newly_skipped = [], 0
                            records = self.history.rows(nid)
                            records_by_id = {r['episode_id']: r for r in records}
                            claims = {}
                            for record in records:
                                if record['status'] == 'completed':
                                    claims.setdefault((record['no'], record['path']), set()).add(record['episode_id'])
                            for entry in alternate['episodes']:
                                row = records_by_id.get(entry['id'])
                                entry['have'] = bool(row and valid_record(row, root))
                                if entry['have']:
                                    # Retire the old-domain ID only when it names the exact same saved file/ordinal.
                                    if claims.get((entry['no'], row['path']), set()) - {entry['id']}:
                                        self.history.query("DELETE FROM entries WHERE novel_id=? AND episode_id<>? AND no=? AND path=? AND status='completed'",
                                                           (nid, entry['id'], entry['no'], row['path']))
                                if int(entry['no']) not in remaining:
                                    continue
                                if entry['have'] or entry['locked']:
                                    newly_skipped += 1
                                    if entry['locked'] and not entry['have']:
                                        self.history.record(nid, entry, 'locked', error='새주소 목록에 잠금/유료 표시')
                                else:
                                    next_items.append(entry)
                            book = alternate
                            candidates[i:] = sorted(next_items, key=lambda e: int(e['no']))
                            self.history.query('INSERT OR REPLACE INTO books VALUES (?,?,?,?)', (nid, book['title'], book['author'], now()))
                            metadata = {k: v for k, v in book.items() if k != 'episodes'}
                            atomic_write(safe_path(folder, 'metadata.json'), json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'))
                            self.accept_reloaded_catalog(client, config, reload.previous)
                            self.update(analysis=book, total=len(candidates), done=i,
                                        skipped=self.snapshot()['skipped'] + newly_skipped,
                                        message='새주소 목록 재분석 완료 · 저장 파일을 제외한 남은 %s회차를 진행합니다.' % len(next_items))
                            continue
                        except Locked as exc:
                            if not self.history.valid(nid, item, root):
                                self.history.record(nid, item, 'locked', error=str(exc))
                            self.update(skipped=self.snapshot()['skipped'] + 1, last_error=str(exc))
                            # Do not keep opening later gated episodes or spend points.
                            self.update(message='잠금 회차에서 수집 중단', done=i + 1)
                            self.export(folder, book, root, config, client)
                            raise
                        except EmptyBody as exc:
                            if not self.history.valid(nid, item, root):
                                self.history.record(nid, item, 'missing', error=str(exc))
                            empty = self.snapshot().get('empty_episodes', [])
                            empty.append(dict(novel_id=nid, title=book['title'], no=item['no']))
                            self.update(skipped=self.snapshot()['skipped'] + 1, warnings=True,
                                        empty_episodes=empty,
                                        last_warning='%s / %s회차: 본문 없음 · 미저장으로 남기고 다음 회차를 진행합니다.' % (book['title'], item['no']))
                        except (Blocked, Pending, Cancelled):
                            raise
                        except SiteError as exc:
                            # Preserve an earlier successful history/file on re-download failure.
                            if not self.history.valid(nid, item, root):
                                self.history.record(nid, item, 'failed', error=str(exc))
                            self.update(failed=self.snapshot()['failed'] + 1, last_error=str(exc))
                            raise
                        self.update(done=i + 1)
                        i += 1
                    self.export(folder, book, root, config, client)
            empty_count = len(self.snapshot().get('empty_episodes', []))
            self.update(status='completed', message=('완료 · 본문 없음 %s회차 미저장' % empty_count) if empty_count else '완료', finished=now())
        except SiteError as exc:
            if client and isinstance(exc, (Blocked, Pending, Locked, AccessLimited)):
                client.keep_tab = True
            self.update(status=exc.code, message=str(exc), last_error=str(exc), finished=now())
        except Exception as exc:
            self.update(status='failed', message='처리 오류: ' + type(exc).__name__, last_error=type(exc).__name__ + ': ' + str(exc), finished=now())
        finally:
            if book is not None and root is not None:
                try:
                    for item in book['episodes']:
                        item['have'] = self.history.valid(book['id'], item, root)
                    self.update(analysis=book)
                except Exception as exc:
                    self.update(last_warning='저장 상태 갱신 실패: ' + type(exc).__name__)
            try:
                if client:
                    client.close()
            finally:
                try:
                    callback = config.get('on_finished')
                    if callback:
                        callback({'state': self.snapshot(), 'source': source, 'downloads': downloads, 'base_url': client.base if client else config['base_url']})
                except Exception as exc:
                    self.update(last_warning='결과 알림/로그 처리 실패: ' + type(exc).__name__)
                finally:
                    handle.close()

    def export(self, folder, book, root, config, client):
        if config.get('make_epub'):
            self.update(message='다운로드 완료 회차로 EPUB 합본 생성 중')
            rows = [r for r in self.history.rows(book['id']) if valid_record(r, root)]
            make_epub(folder, book, rows, client.check, config)

