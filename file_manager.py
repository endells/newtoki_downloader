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
        return str(json.loads(p.read_text(encoding='utf-8')).get('id')) == nid
    except (OSError, ValueError, TypeError):
        return False


def rebase_history(history, old, new):
    prefix = str(old) + os.sep
    history.query('UPDATE entries SET path=? || substr(path,?), updated=? WHERE substr(path,1,?)=?',
                  (str(new) + os.sep, len(prefix) + 1, now(), len(prefix), prefix))


def manage_files(root, history, apply=False, check=lambda: None, update=lambda **kw: None):
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
    local = reconcile_library(root, history, check, update)
    report.update(history_imported=local['imported'] + local['refreshed'], history_missing=local['missing'],
                  matched=local['matched'], zero=local['zero'], unknown=local['unknown'], books=local['books'], needs_analysis=local['needs_analysis'])

    rows = history.query("SELECT * FROM entries WHERE status IN ('completed','missing') ORDER BY novel_id,no")
    folders = {}
    for row in rows:
        check()
        old = Path(row['path'])
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
