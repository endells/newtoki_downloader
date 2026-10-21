"""Read a configurable public HTTPS announcement and rank explicitly marked links."""
import ipaddress
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit, urljoin

try:
    from .parser import Document, SiteError, clean, origin
    from .browser_client import NoRedirect
except ImportError:
    from parser import Document, SiteError, clean, origin
    from browser_client import NoRedirect

DEFAULT_ANNOUNCEMENT = 'https://xn--vg1b79zgsf1sj.com/'
DEFAULT_BASE = 'https://toki35.com'


def announcement_url(value):
    try:
        u = urlsplit(str(value or '').strip())
        host = (u.hostname or '').encode('idna').decode('ascii').lower()
        if (u.scheme != 'https' or not host or u.username or u.password or u.fragment
                or u.port not in (None, 443) or '.' not in host
                or not re.fullmatch(r'[a-z0-9.-]+', host)):
            raise ValueError()
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not ip.is_global:
            raise ValueError()
        if host.endswith(('.localhost', '.local', '.internal')):
            raise ValueError()
        return urlunsplit(('https', host, u.path or '/', u.query, ''))
    except (ValueError, UnicodeError):
        raise SiteError('주소 안내 사이트는 공개 HTTPS 주소로 입력하세요. 로그인 정보·내부 IP는 사용할 수 없습니다.') from None


def fetch_announcement(value, check=lambda: None):
    url = announcement_url(value)
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    for hop in range(4):
        check()
        try:
            addresses = socket.getaddrinfo(urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise SiteError('주소 안내 사이트가 공개 IP로 확인되지 않았습니다.')
            request = urllib.request.Request(url, headers={'User-Agent': 'NewtokiFF/0.2 domain-check'})
            try:
                response = opener.open(request, timeout=15)
            except urllib.error.HTTPError as exc:
                with exc:
                    if exc.code in (301, 302, 303, 307, 308) and hop < 3:
                        url = announcement_url(urljoin(url, exc.headers.get('Location', '')))
                        continue
                raise
            with response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise SiteError('주소 안내 페이지가 너무 큽니다.')
            return raw.decode('utf-8-sig'), url
        except SiteError:
            raise
        except Exception:
            raise SiteError('주소 안내 페이지를 읽지 못했습니다.') from None
    raise SiteError('주소 안내 사이트의 이동 횟수가 너무 많습니다.')


def discover_candidates(html, source):
    legacy, modern = [], []
    for link in Document(html).walk(tag='a'):
        label = clean(link.text())
        try:
            value = origin(urljoin(source, link.attrs.get('href', '')))
        except SiteError:
            continue
        host = urlsplit(value).hostname
        if '구버전' in label and host.startswith('newtoki'):
            if value not in legacy:
                legacy.append(value)
        elif host.startswith('toki') and any(word in label for word in ('바로가기', '신버전', '메인 서버')):
            if value not in modern:
                modern.append(value)
    if not legacy and not modern:
        raise SiteError('주소 안내 페이지에서 구버전/신버전 바로가기 링크를 찾지 못했습니다.')
    return legacy, modern
