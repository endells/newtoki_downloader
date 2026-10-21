"""Rename this plugin's tracked files without changing content or discarding history."""
import hashlib
import json
import os
from pathlib import Path
import re

try:
    from .parser import SiteError
    from .engine import safe_path, now, valid_record
except ImportError:
    from parser import SiteError
    from engine import safe_path, now, valid_record


def metadata_matches(folder, nid):
    try:
        p = folder / 'metadata.json'
        if p.is_symlink() or p.stat().st_size > 1024 * 1024:
            return False
        data = json.loads(p.read_text(encoding='utf-8'))
        return isinstance(data, dict) and str(data.get('id')) == nid
    except (OSError, ValueError, TypeError):
        return False


def rebase_history(history, old, new):
    prefix = str(old) + os.sep
    history.query('UPDATE entries SET path=? || substr(path,?), updated=? WHERE substr(path,1,?)=?',
                  (str(new) + os.sep, len(prefix) + 1, now(), len(prefix), prefix))


def selected_paths(root, names):
    root = Path(root).resolve()
    if names is None:
        return None
    if not isinstance(names, list) or not names:
        raise SiteError('실행할 작품 폴더를 하나 이상 선택하세요.')
    result = set()
    for name in names:
        if not isinstance(name, str) or not name or name in ('.', '..') or Path(name).name != name:
            raise SiteError('저장 폴더 바로 아래의 작품 폴더만 선택할 수 있습니다.')
        path = root / name
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != root:
            raise SiteError('선택한 작품 폴더가 없거나 허용되지 않는 경로입니다: ' + name)
        result.add(path)
    return result


def manage_files(root, history, apply=False, check=lambda: None, update=lambda **kw: None, selected_folders=None):
    root = Path(root).resolve()
    if not root.is_dir():
        raise SiteError('설정의 NAS 저장 폴더가 없습니다. 경로를 확인하세요.')
    report = dict(applied=apply, planned=0, files_renamed=0, folders_renamed=0,
                  history_repaired=0, conflicts=0, skipped=0, unchanged=0, rows=[], truncated=False)

    def event(kind, source, target, status, message):
        if status == 'planned': report['planned'] += 1
        elif status == 'conflict': report['conflicts'] += 1
        elif status == 'skipped': report['skipped'] += 1
        if len(report['rows']) < 500:
            report['rows'].append(dict(kind=kind, source=str(source), target=str(target), status=status, message=message))
        else:
            report['truncated'] = True
        update(message=message + ': ' + str(source))

    try:
        from .reconcile import reconcile_library
    except ImportError:
        from reconcile import reconcile_library
    local = reconcile_library(root, history, check, update, selected_folders)
    report.update(history_imported=local['imported'] + local['refreshed'], history_missing=local['missing'],
                  matched=local['matched'], zero=local['zero'], unknown=local['unknown'], books=local['books'], needs_analysis=local['needs_analysis'])

    rows = history.query("SELECT * FROM entries WHERE status IN ('completed','missing') ORDER BY novel_id,no")
    folders = {}
    for row in rows:
        check()
        old = Path(row['path'])
        if selected_folders is not None and old.parent not in selected_folders:
            continue
        try:
            old.resolve().relative_to(root)
            if old.is_symlink() or old.parent.is_symlink(): raise ValueError()
        except (ValueError, OSError):
            event('파일', old, '', 'skipped', '설정 폴더 밖 또는 심볼릭 링크라 제외')
            continue
        match = re.fullmatch(r'(.+) \[([0-9]+)\]', old.parent.name)
        if match and match[2] == row['novel_id'] and old.parent.parent == root:
            folders[old.parent] = (safe_path(root, match[1]), row['novel_id'])

    projected = {}
    for old, (new, nid) in sorted(folders.items(), key=lambda x: str(x[0])):
        check()
        if not old.exists() and new.is_dir() and not new.is_symlink() and metadata_matches(new, nid):
            if apply:
                rebase_history(history, old, new)
                report['history_repaired'] += 1
            event('이력', old, new, 'repaired' if apply else 'planned', '폴더명 변경 후 남은 이력 경로 복구' if apply else '이력 경로 복구 예정')
            projected[old] = new
            continue
        if not old.is_dir() or not metadata_matches(old, nid):
            event('폴더', old, new, 'skipped', '작품 메타데이터를 확인할 수 없어 폴더명 유지')
            continue
        if new.exists() or new.is_symlink():
            event('폴더', old, new, 'conflict', '같은 이름의 폴더가 있어 폴더명 유지')
            continue
        if apply:
            old.rename(new)
            try:
                rebase_history(history, old, new)
            except Exception:
                if not old.exists(): new.rename(old)
                raise
            report['folders_renamed'] += 1
        projected[old] = new
        event('폴더', old, new, 'renamed' if apply else 'planned', '폴더 번호 제거 완료' if apply else '폴더 번호 제거 예정')

    if apply:
        rows = history.query("SELECT * FROM entries WHERE status IN ('completed','missing') ORDER BY novel_id,no")
    for row in rows:
        check()
        old = Path(row['path'])
        allowed = set(selected_folders or ()) | set(projected.values())
        if selected_folders is not None and old.parent not in allowed:
            continue
        try:
            old.resolve().relative_to(root)
            if old.is_symlink() or old.parent.is_symlink(): continue
        except (ValueError, OSError):
            continue
        if not apply and not old.exists() and old.parent in projected and projected[old.parent].is_dir():
            old = projected[old.parent] / old.name
            row = dict(row, path=str(old))
        suffix = ' [' + row['episode_id'] + '].txt'
        if not old.name.endswith(suffix):
            report['unchanged'] += 1
            continue
        new = safe_path(old.parent, old.name[:-len(suffix)] + '.txt')
        shown_new = projected.get(old.parent, old.parent) / new.name
        # Recover an interruption after a rename but before the history update.
        repaired = dict(row, path=str(new))
        if not old.exists() and valid_record(repaired, root):
            if apply:
                history.query('UPDATE entries SET path=?,updated=? WHERE novel_id=? AND episode_id=?',
                              (str(new), now(), row['novel_id'], row['episode_id']))
                report['history_repaired'] += 1
            event('이력', old, shown_new, 'repaired' if apply else 'planned', '파일 이력 경로 복구 완료' if apply else '파일 이력 경로 복구 예정')
            continue
        if not valid_record(row, root):
            event('파일', old, shown_new, 'skipped', '누락·0바이트·내용 불일치라 이름 변경 제외')
            continue
        if new.exists() or new.is_symlink():
            event('파일', old, shown_new, 'conflict', '같은 이름의 파일이 있어 원본 유지')
            continue
        if apply:
            # Hard-link creation is atomic and refuses existing destinations.
            os.link(old, new)
            try:
                history.query('UPDATE entries SET path=?,updated=? WHERE novel_id=? AND episode_id=?',
                              (str(new), now(), row['novel_id'], row['episode_id']))
            except Exception:
                new.unlink()
                raise
            old.unlink()
            report['files_renamed'] += 1
        event('파일', old, shown_new, 'renamed' if apply else 'planned', '파일 번호 제거 완료' if apply else '파일 번호 제거 예정')
    return report


def rebuild_epubs(root, history, config, report, check=lambda: None, update=lambda **kw: None, selected_folders=None):
    """Rebuild from reconciled local files only, without renaming or browsing."""
    try:
        from .engine import make_epub, safe_name
        from .parser import Cancelled
    except ImportError:
        from engine import make_epub, safe_name
        from parser import Cancelled
    root = Path(root).resolve()
    catalogs = {str(b['id']): b for b in history.catalogs()}
    groups = {}
    for row in history.query("SELECT * FROM entries WHERE status='completed' ORDER BY novel_id,no,episode_id"):
        check()
        path = Path(row['path'])
        if selected_folders is not None and path.parent not in selected_folders:
            continue
        try:
            path.resolve().relative_to(root)
            relative = path.relative_to(root)
            if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
                continue
        except (ValueError, OSError):
            continue
        if valid_record(row, root):
            groups.setdefault((row['novel_id'], path.parent), []).append(row)
    report.update(epub_created=0, epub_failed=0)
    for (nid, folder), rows in groups.items():
        check()
        target = folder
        try:
            book = dict(catalogs.get(nid) or {'id': nid, 'title': re.sub(r' \[' + re.escape(nid) + r'\]$', '', folder.name)})
            meta = folder / 'metadata.json'
            if meta.exists():
                if not metadata_matches(folder, nid):
                    raise SiteError('작품 메타데이터가 일치하지 않아 EPUB 생성 제외')
                book.update(json.loads(meta.read_text(encoding='utf-8')))
            target = safe_path(folder, safe_name(book['title']) + '.epub')
            update(message='EPUB 다시 생성 중: ' + book['title'])
            make_epub(folder, book, rows, check, config)
            report['epub_created'] += 1
            status, message = 'completed', 'EPUB 다시 생성 완료 · %s회차' % len(rows)
        except Cancelled:
            raise
        except (OSError, ValueError, TypeError, KeyError, SiteError) as exc:
            report['epub_failed'] += 1
            status, message = 'failed', 'EPUB 생성 실패: ' + str(exc)
            update(last_warning=message, warnings=True)
        if len(report['rows']) < 500:
            report['rows'].append(dict(kind='EPUB', source=str(folder), target=str(target), status=status, message=message))
        else:
            report['truncated'] = True
        update(file_report=report)
    return report
