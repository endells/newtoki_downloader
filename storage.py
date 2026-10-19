"""Read-only directory picker plus an explicit write-permission probe."""
import os
from pathlib import Path
import tempfile

from .parser import SiteError


def available_roots():
    # These paths are bind-mounted in the user's existing FF container.
    # Existence alone does not prove persistence; the UI documents that distinction.
    return [Path(p) for p in ('/volume1', '/volume2', '/volume3', '/data') if Path(p).is_dir()]


def browse_folders(value='', roots=None):
    roots = [Path(p).resolve() for p in (available_roots() if roots is None else roots)]
    result = {'roots': [str(p) for p in roots], 'path': '', 'parent': '', 'directories': []}
    if not value:
        return result
    path = Path(value)
    if not path.is_absolute():
        raise SiteError('폴더 경로는 /로 시작해야 합니다.')
    path = path.resolve()
    root = next((r for r in roots if path == r or r in path.parents), None)
    if root is None:
        raise SiteError('NAS 볼륨 또는 FF /data 안에서 폴더를 선택하세요.')
    if not path.is_dir():
        raise SiteError('아직 없는 폴더입니다. 상위 폴더를 선택한 뒤 새 폴더 이름을 직접 붙이세요.')
    try:
        children = []
        for item in path.iterdir():
            if item.name.startswith('.') or not item.is_dir():
                continue
            target = item.resolve()
            if target == root or root in target.parents:
                children.append({'name': item.name, 'path': str(item)})
    except OSError:
        raise SiteError('폴더 목록을 읽지 못했습니다. FF 계정의 접근 권한을 확인하세요.') from None
    children.sort(key=lambda x: x['name'].casefold())
    result.update(path=str(path), parent='' if path == root else str(path.parent),
                  directories=children[:500], truncated=len(children) > 500)
    return result


def check_storage(value):
    path = Path(str(value).strip())
    if not path.is_absolute():
        raise SiteError('NAS 저장 폴더는 /로 시작하는 절대 경로여야 합니다.')
    path = path.resolve()
    parent = path
    while not parent.exists():
        parent = parent.parent
    if not parent.is_dir() or (path.exists() and not path.is_dir()):
        raise SiteError('폴더 경로에 파일이 있습니다.')
    try:
        # No requested download folder or novel file is created by this check.
        with tempfile.TemporaryFile(prefix='.newtoki-write-check-', dir=str(parent)) as f:
            f.write(b'write-check')
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        raise SiteError('저장 폴더(또는 상위 폴더)에 쓸 수 없습니다. NAS 권한/읽기 전용 마운트를 확인하세요.') from None
    message = '쓰기 확인 완료: ' + str(path)
    if parent != path:
        message += ' · 새 폴더는 다운로드 시작 시 생성합니다. 현재 상위 폴더의 쓰기 권한을 확인했습니다.'
    return {'path': str(path), 'exists': path.is_dir(), 'checked_parent': str(parent), 'message': message}
