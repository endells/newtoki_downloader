"""Match local TXT files to site episodes by the filename's leading number."""
import hashlib
import json
from pathlib import Path
import re

try:
    from .engine import safe_name, safe_path, valid_record
except ImportError:
    from engine import safe_name, safe_path, valid_record


PREFIX = re.compile(r'^\s*(?:\[([0-9]{1,20})\]|([0-9]{1,20})(?=$|[\s._\-)]|화|회))')


def leading_number(name):
    match = PREFIX.match(Path(name).stem)
    return int(match[1] or match[2]) if match else None


def digest_file(path, check):
    before = path.stat()
    if before.st_size <= 0:
        return None
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            check()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        return None
    return digest.hexdigest()


def folder_metadata(folder):
    p = folder / 'metadata.json'
    if not p.exists():
        return None
    if p.is_symlink() or p.stat().st_size > 1024 * 1024:
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def local_folders(root, history, book):
    nid = str(book['id'])
    paths = {root / safe_name(book['title']), root / ('%s [%s]' % (safe_name(book['title']), nid))}
    paths.update(Path(r['path']).parent for r in history.rows(nid) if r['path'])
    for path in root.iterdir() if root.is_dir() else []:
        if path.is_dir() and not path.is_symlink():
            try:
                meta = folder_metadata(path)
            except OSError:
                continue
            if meta and str(meta.get('id')) == nid:
                paths.add(path)
    claims = history.query('SELECT DISTINCT novel_id,path FROM entries WHERE novel_id<>? AND path<>?', (nid, ''))
    claimed = {Path(r['path']).parent for r in claims}
    output = []
    for path in sorted(paths, key=str):
        try:
            path.resolve().relative_to(root)
            # Work folders are direct children, preventing unrelated nested libraries from matching.
            if path.parent != root or path.is_symlink() or not path.is_dir():
                continue
            meta = folder_metadata(path)
            if meta is not None and str(meta.get('id')) != nid:
                continue
            if meta is None and path in claimed:
                continue
            output.append(path)
        except (OSError, ValueError):
            continue
    return output


def reconcile_book(root, history, book, check=lambda: None, update=lambda **kw: None, selected_folders=None):
    root = Path(root).resolve()
    nid = str(book['id'])
    report = dict(imported=0, refreshed=0, missing=0, matched=0, duplicates=0, zero=0, unknown=0)
    by_number = {}
    for item in book['episodes']:
        by_number.setdefault(int(item['no']), []).append(item)
    files, zero_files = {}, {}
    for folder in local_folders(root, history, book):
        if selected_folders is not None and folder not in selected_folders:
            continue
        check()
        for path in sorted(folder.iterdir(), key=lambda p: p.name):
            check()
            if path.suffix.lower() != '.txt' or path.is_symlink() or not path.is_file():
                continue
            number = leading_number(path.name)
            if number is None:
                continue
            if number not in by_number:
                report['unknown'] += 1
                continue
            if path.stat().st_size == 0:
                report['zero'] += 1
                zero_files.setdefault(number, []).append(path)
                continue
            files.setdefault(number, []).append(path)
    old_rows = {r['episode_id']: r for r in history.rows(nid)}
    for number, items in by_number.items():
        check()
        if len(items) != 1:
            # One filename number cannot resolve two different site episode IDs.
            update(warnings=True, last_warning='사이트 목록에 같은 회차 번호가 중복되어 파일 대조 제외: %s' % number)
            continue
        item = items[0]
        previous = old_rows.get(item['id'])
        if selected_folders is not None and previous and previous['path'] and Path(previous['path']).parent not in selected_folders:
            continue
        candidates = files.get(number, [])
        if len(candidates) > 1:
            report['duplicates'] += 1
        # Prefer the previously recorded path, otherwise a stable filename order.
        candidates.sort(key=lambda p: (0 if previous and str(p) == previous['path'] else 1, str(p)))
        chosen = None
        for path in candidates:
            try:
                digest = digest_file(path, check)
            except OSError:
                continue
            if digest:
                chosen = (path, digest)
                break
        if chosen:
            path, digest = chosen
            report['matched'] += 1
            if not previous or previous['status'] != 'completed' or previous['path'] != str(path) or previous['digest'] != digest:
                history.record(nid, item, 'completed', path, digest)
                key = 'refreshed' if previous and previous['status'] == 'completed' else 'imported'
                report[key] += 1
                update(message='파일명 회차로 저장 이력 반영: %s / %s · %s' % (book['title'], number, path.name))
        elif zero_files.get(number) and not previous:
            history.record(nid, item, 'missing', zero_files[number][0], '', '0바이트 파일이므로 미저장으로 표시합니다.')
            report['missing'] += 1
        elif previous and previous['status'] == 'completed' and not valid_record(previous, root):
            history.record(nid, item, 'missing', previous['path'], previous['digest'], '해당 회차의 정상 크기 파일을 찾지 못했습니다.')
            report['missing'] += 1
    if report['duplicates']:
        update(warnings=True, last_warning='같은 앞 회차 번호의 파일이 %s개 회차에 중복됩니다. 기존 이력 경로, 파일명 순으로 하나를 사용했습니다.' % report['duplicates'])
    return report


def reconcile_library(root, history, check=lambda: None, update=lambda **kw: None, selected_folders=None):
    root = Path(root).resolve()
    books = {str(b['id']): b for b in history.catalogs()}
    # Older installations can still reconcile known episode IDs offline from history.
    for folder in root.iterdir() if root.is_dir() else []:
        check()
        if selected_folders is not None and folder not in selected_folders:
            continue
        if folder.is_symlink() or not folder.is_dir():
            continue
        try:
            meta = folder_metadata(folder)
        except OSError:
            continue
        if not meta or not str(meta.get('id', '')).isdigit() or not meta.get('title'):
            continue
        nid = str(meta['id'])
        if nid not in books:
            rows = history.rows(nid)
            if rows:
                books[nid] = dict(id=nid, title=meta['title'], episodes=[dict(id=r['episode_id'], no=r['no'], title=r['title'], locked=False) for r in rows])
    total = dict(imported=0, refreshed=0, missing=0, matched=0, duplicates=0, zero=0, unknown=0, books=len(books), needs_analysis=0)
    known_folders = set()
    for book in books.values():
        check()
        if selected_folders is not None and not any(p in selected_folders for p in local_folders(root, history, book)):
            continue
        known_folders.update(local_folders(root, history, book))
        report = reconcile_book(root, history, book, check, update, selected_folders)
        for key, count in report.items():
            total[key] += count
    for folder in root.iterdir() if root.is_dir() else []:
        check()
        if selected_folders is not None and folder not in selected_folders:
            continue
        if folder in known_folders or folder.is_symlink() or not folder.is_dir():
            continue
        if any(p.is_file() and not p.is_symlink() and p.suffix.lower() == '.txt' and leading_number(p.name) is not None for p in folder.iterdir()):
            total['needs_analysis'] += 1
    if total['needs_analysis'] or total['unknown']:
        update(warnings=True, last_warning='회차 목록이 없는 작품 폴더 %s개, 목록에 없는 파일 %s개입니다. 해당 작품을 한 번 분석하면 전체 회차 이력을 대조할 수 있습니다.' % (total['needs_analysis'], total['unknown']))
    return total
