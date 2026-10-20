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
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

try:
    from .parser import SiteError, Blocked, Locked, Pending, Cancelled, parse_catalog, discover_legacy, origin, novel_id
    from .browser_client import BrowserClient, NoRedirect
except ImportError:
    from parser import SiteError, Blocked, Locked, Pending, Cancelled, parse_catalog, discover_legacy, origin, novel_id
    from browser_client import BrowserClient, NoRedirect

ANNOUNCEMENT = 'https://xn--vg1b79zgsf1sj.com/'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


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


def collect_catalog(client, nid, update=lambda **kw: None):
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
        data = parse_catalog(client.html(path), client.base + path)
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


def make_epub(folder, book, records, check=lambda: None, config=None):
    config = config or {}
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
            text = Path(row['path']).read_text(encoding='utf-8')
            if '\n\n' in text:
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
    atomic_write(safe_path(folder, safe_name(book['title']) + '.epub'), buffer.getvalue())


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
        self.lock_path = str(db_path) + '.lock'
        self.thread = None
        self.history.query('CREATE TABLE IF NOT EXISTS books (id TEXT PRIMARY KEY, title TEXT, author TEXT, updated TEXT)')
        self.state = {'status': 'idle', 'message': '대기 중', 'completed': 0, 'skipped': 0, 'failed': 0}

    def book_info(self, nid):
        rows = self.history.query('SELECT title,author,updated FROM books WHERE id=?', (nid,))
        return rows[0] if rows else {'title': '분석 전', 'author': '', 'updated': ''}

    def update(self, **values):
        with self.guard:
            self.state.update(values)

    def snapshot(self):
        with self.guard:
            return copy.deepcopy(self.state)

    def cancel(self):
        self.stop.set()

    def start(self, kind, ids, config, selected=None):
        import fcntl
        if kind not in ('analyze', 'download', 'domain') or not ids:
            raise SiteError('작업 종류와 작품 번호가 필요합니다.')
        ids = [novel_id(x) for x in ids]
        with self.guard:
            if self.state['status'] == 'running':
                raise SiteError('작업이 진행 중입니다.')
            handle = open(self.lock_path, 'a')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise SiteError('다른 FF 프로세스에서 작업 중입니다.') from None
            self.stop.clear()
            self.state = {'status': 'running', 'message': '작업 준비 중', 'kind': kind, 'completed': 0,
                          'skipped': 0, 'failed': 0, 'done': 0, 'total': 0, 'started': now()}
        self.thread = threading.Thread(target=self.run, args=(kind, ids, dict(config), selected, handle), daemon=True)
        try:
            self.thread.start()
        except Exception:
            handle.close()
            self.update(status='failed', message='작업 스레드를 시작하지 못했습니다.')
            raise

    def resolve(self, client, nid, config):
        self.update(message='안내 페이지의 구버전 주소 확인 중')
        try:
            req = urllib.request.Request(ANNOUNCEMENT, headers={'User-Agent': 'NewtokiFF/0.1 domain-check'})
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError()
            candidate = discover_legacy(raw.decode('utf-8'), ANNOUNCEMENT)
        except SiteError:
            raise
        except Exception:
            raise SiteError('주소 안내 페이지를 읽지 못했습니다. 현재 주소를 유지합니다.') from None
        old = client.base
        client.base = candidate
        try:
            parse_catalog(client.html('/novel/' + nid), candidate + '/novel/' + nid)
        except Exception:
            client.base = old
            raise
        if candidate != old:
            save = config.get('save_domain')
            if save:
                save(candidate)
            self.update(domain_change={'old': old, 'new': candidate, 'time': now()})
        self.update(base_url=candidate)

    def run(self, kind, ids, config, selected, handle):
        client = None
        downloads = []
        try:
            root = Path(config['download_path']).resolve()
            client = self.factory(config['base_url'], config['browser_url'], config['request_delay'], self.stop, config['timeout'])
            if kind == 'domain' or config.get('auto_domain'):
                self.resolve(client, ids[0], config)
            if kind != 'domain':
                for nid in ids:
                    book = collect_catalog(client, nid, self.update)
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
                        elif wanted is None and self.history.valid(nid, item, root):
                            skipped += 1
                        else:
                            candidates.append(item)
                    if wanted is None:
                        candidates = candidates[:config['max_per_title']]
                    self.update(total=len(candidates), done=0, skipped=skipped, title=book['title'])
                    folder = safe_path(root, '%s [%s]' % (safe_name(book['title']), nid))
                    folder.mkdir(parents=True, exist_ok=True)
                    if config.get('save_cover', True) and book.get('cover_url'):
                        try:
                            # Return to the first catalog page: its cover is an ordinary loaded resource.
                            client.html('/novel/' + nid)
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
                    for i, item in enumerate(candidates):
                        client.check()
                        self.update(message='받는 중: %s / %s' % (book['title'], item['title']), episode_id=item['id'])
                        try:
                            text = client.body(nid, item)
                            filename = '%0*d_%s [%s].txt' % (config['episode_digits'], item['no'], safe_name(item['title']), item['id'])
                            path = safe_path(folder, filename)
                            data = ('%s. %s\n\n%s\n' % (item['no'], item['title'], text)).encode('utf-8')
                            client.check()
                            atomic_write(path, data)
                            self.history.record(nid, item, 'completed', path, hashlib.sha256(data).hexdigest())
                            downloads.append(dict(novel_id=nid, title=book['title'], no=item['no'], episode_title=item['title']))
                            self.update(completed=self.snapshot()['completed'] + 1)
                        except Locked as exc:
                            if not self.history.valid(nid, item, root):
                                self.history.record(nid, item, 'locked', error=str(exc))
                            self.update(skipped=self.snapshot()['skipped'] + 1, last_error=str(exc))
                            # Do not keep opening later gated episodes or spend points.
                            self.update(message='잠금 회차에서 수집 중단', done=i + 1)
                            self.export(folder, book, root, config, client)
                            raise
                        except (Blocked, Pending, Cancelled):
                            raise
                        except SiteError as exc:
                            # Preserve an earlier successful history/file on re-download failure.
                            if not self.history.valid(nid, item, root):
                                self.history.record(nid, item, 'failed', error=str(exc))
                            self.update(failed=self.snapshot()['failed'] + 1, last_error=str(exc))
                            raise
                        self.update(done=i + 1)
                    self.export(folder, book, root, config, client)
            self.update(status='completed', message='완료', finished=now())
        except SiteError as exc:
            if client and isinstance(exc, (Blocked, Pending, Locked)):
                client.keep_tab = True
            self.update(status=exc.code, message=str(exc), last_error=str(exc), finished=now())
        except Exception as exc:
            self.update(status='failed', message='처리 오류: ' + type(exc).__name__, last_error=type(exc).__name__ + ': ' + str(exc), finished=now())
        finally:
            try:
                if client:
                    client.close()
            finally:
                try:
                    callback = config.get('on_finished')
                    if callback and kind in ('download', 'domain'):
                        callback({'state': self.snapshot(), 'source': config.get('source', '수동 다운로드' if selected is not None else '자동 목록 수집'), 'downloads': downloads, 'base_url': client.base if client else config['base_url']})
                except Exception as exc:
                    self.update(last_warning='결과 알림/로그 처리 실패: ' + type(exc).__name__)
                finally:
                    handle.close()

    def export(self, folder, book, root, config, client):
        if config.get('make_epub'):
            self.update(message='다운로드 완료 회차로 EPUB 합본 생성 중')
            rows = [r for r in self.history.rows(book['id']) if valid_record(r, root)]
            make_epub(folder, book, rows, client.check, config)
