"""Normal Chromium rendering via CDP; no CAPTCHA solving or access-control bypass.

Only opens its own tab. No fingerprint modifications, hidden API replay, cookie
exports, point purchases, credential entry, or proxy switching.
"""
import json
import ipaddress
import socket
import re
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urlunsplit

try:
    from .parser import SiteError, Blocked, Pending, EmptyBody, Cancelled, origin, viewer_config, validate_body, check_page, parse_catalog, CatalogSnapshot, verification_notice
except ImportError:
    from parser import SiteError, Blocked, Pending, EmptyBody, Cancelled, origin, viewer_config, validate_body, check_page, parse_catalog, CatalogSnapshot, verification_notice


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NavigationPending(SiteError):
    """Only a transient browser context loss while a document is navigating."""


class DomainUnavailable(SiteError):
    """DNS/transport failure only; never authentication, HTTP limits or browser disconnects."""


class BrowserConnectionError(SiteError):
    """The common CDP service failed, independently of the site's domain."""


def connection_failure(exc, address, stage):
    if isinstance(exc, urllib.error.HTTPError):
        detail = 'HTTP %s' % exc.code
        hint = ('CDP 중계 서비스 또는 Chromium 쪽 응답 실패입니다. 브라우저와 중계 컨테이너 로그를 확인하세요.' if exc.code in (502, 503, 504)
                else '이 포트가 CDP 중계 포트인지 확인하세요. 브라우저 화면용 포트와 다릅니다.' if exc.code in (404, 405)
                else 'CDP 중계 서비스의 요청 허용 설정과 로그를 확인하세요.')
    else:
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, socket.gaierror):
            detail, hint = '호스트 이름 해석 실패', 'FF와 브라우저가 같은 Docker 네트워크인지, 컨테이너 이름이 맞는지 확인하세요.'
        elif isinstance(reason, ConnectionRefusedError):
            detail, hint = '연결 거부', '브라우저/CDP 중계 컨테이너의 실행 상태와 설정한 포트를 확인하세요.'
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            detail, hint = '연결 시간 초과', 'FF에서 해당 브라우저 주소로 통신 가능한지 확인하세요.'
        elif isinstance(reason, (ValueError, UnicodeError)):
            detail, hint = 'CDP JSON 응답 형식 오류', '브라우저 화면용 포트 대신 CDP 중계 포트를 입력하세요.'
        else:
            detail, hint = type(reason).__name__ + ': ' + browser_detail(reason), '브라우저/CDP 중계 컨테이너 로그를 확인하세요.'
    return BrowserConnectionError('전용 브라우저 연결 실패 [%s · %s]: %s. %s 사이트 도메인은 변경하지 않습니다.' % (stage, address, detail, hint))


class CatalogStructureError(SiteError):
    """A loaded catalog needs a parser update; preserve the visible tab."""


def browser_detail(value):
    # Keep the JS error, never the site's stack trace, page HTML or token-bearing URLs.
    text = str(value or '').split('\n', 1)[0]
    text = re.sub(r'https?://\S+', '[페이지 주소]', text)
    text = re.sub(r'(?i)(token|cookie|authorization|password|secret)([\s:=]+)[^\s,;]+', r'\1\2[숨김]', text)
    return text[:300]


BODY_EXPRESSION = r'''(() => {
  const host = document.querySelector('[data-theme-novel-content]');
  const root = host && host.shadowRoot;
  const notice = host && !root ? (host.innerText || '') : (!host ? (document.body ? document.body.innerText || '' : '').slice(0, 1000) : '');
  const nodes = root ? Array.from(root.children).filter(n => n.tagName !== 'STYLE') : [];
  let gate = {};
  try { gate = JSON.parse(document.getElementById('theme-novel-viewer-data').textContent).paidGate || {}; } catch(e) {}
  return {rendered: !!root && nodes.length > 0, empty: !!root && nodes.length === 0,
    text: nodes.map(n => n.innerText || n.textContent || '').join('\n\n'),
    images: root ? root.querySelectorAll('img,svg,canvas,video').length : 0,
    message: notice, title: document.title, locked: gate.active === true || gate.locked === true || !!document.querySelector('[data-theme-novel-unlock]')};
})()'''

LOAD_MORE_EXPRESSION = r'''(() => {
  const list = document.querySelector('.novel-eps');
  const section = list && list.closest('section');
  const button = section && Array.from(section.querySelectorAll('button')).find(b => (b.innerText || '').trim() === '이전 회차 더 보기');
  if (!button || button.disabled || !button.getClientRects().length) return false;
  button.click();
  return true;
})()'''

LOAD_NEWER_EXPRESSION = LOAD_MORE_EXPRESSION.replace('이전 회차 더 보기', '더 최신 회차 보기')

MODERN_STATUS_EXPRESSION = r'''(() => {
  const article = document.querySelector('article.novel-viewer');
  const host = article && article.querySelector(':scope > div[style*="--novel-font-size"]');
  const notice = host ? '' : ((article || document.querySelector('main') || document.body)?.innerText || '').slice(0, 1000);
  return {title: document.title, message: notice,
    locked: !host && (notice.includes('로그인') || notice.includes('포인트') && (notice.includes('구매') || notice.includes('결제')))};
})()'''


def modern_dom_body(node):
    """Read paragraph nodes of an already-rendered viewer, including closed shadow roots."""
    roots = node.get('shadowRoots') or []
    if not roots:
        return dict(rendered=False, text='', images=0)
    paragraphs, images = [], 0
    def text(n):
        name = n.get('nodeName', '').upper()
        if name in ('STYLE', 'SCRIPT', 'TEMPLATE'):
            return ''
        if name == 'BR':
            return '\n'
        if n.get('nodeType') == 3:
            return n.get('nodeValue', '')
        return ''.join(text(c) for c in n.get('children', []))
    def walk(n):
        nonlocal images
        name = n.get('nodeName', '').upper()
        if name in ('STYLE', 'SCRIPT', 'TEMPLATE'):
            return
        if name in ('IMG','SVG','CANVAS','VIDEO'):
            images += 1
        if name == 'P':
            paragraphs.append(text(n))
        for c in n.get('children', []):
            walk(c)
    for root in roots:
        walk(root)
    return dict(rendered=bool(paragraphs), text='\n\n'.join(paragraphs), images=images, empty=not paragraphs and images == 0)


def endpoint(value):
    u = urlsplit(str(value).strip())
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.path not in ('', '/') or u.query or u.fragment:
        raise SiteError('브라우저 주소는 http://newtoki-browser:9223 같은 내부 주소여야 합니다.')
    try:
        port = u.port or (443 if u.scheme == 'https' else 80)
        addresses = socket.getaddrinfo(u.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not (ipaddress.ip_address(x[4][0]).is_private or ipaddress.ip_address(x[4][0]).is_loopback) for x in addresses):
            raise ValueError()
    except (OSError, ValueError):
        raise BrowserConnectionError('브라우저 호스트 %s를 내부 IP로 확인하지 못했습니다. FF와 브라우저의 Docker 네트워크/주소를 확인하세요.' % u.hostname) from None
    return urlunsplit((u.scheme, u.netloc, '', '', ''))


class BrowserClient:
    def __init__(self, base_url, browser_url, delay=3, stop=None, timeout=30):
        self.base = origin(base_url)
        self.address = endpoint(browser_url)
        self.stop = stop or threading.Event()
        self.delay, self.timeout = max(2, float(delay)), max(10, min(90, int(timeout)))
        self.last_request, self.sequence = 0, 0
        self.ws, self.target, self.keep_tab = None, None, False

    def check(self):
        if self.stop.is_set():
            raise Cancelled('중지했습니다.')

    def discovery(self, path, method='GET'):
        try:
            req = urllib.request.Request(self.address + path, method=method, headers={'Host': 'localhost', 'Accept': 'application/json'})
            with urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({})).open(req, timeout=10) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError()
            data = json.loads(raw.decode('utf-8'))
            if not isinstance(data, dict):
                raise ValueError('CDP object expected')
            return data
        except Exception as exc:
            raise connection_failure(exc, self.address, method + ' ' + path.split('?', 1)[0]) from None

    def check_connection(self):
        """Read only the CDP version endpoint; no tab, website, or settings changes."""
        data = self.discovery('/json/version')
        if not isinstance(data.get('Browser'), str) or not data.get('Browser'):
            raise connection_failure(ValueError('Missing Browser'), self.address, 'GET /json/version')
        return browser_detail(data['Browser'])

    def connect(self):
        if self.ws:
            return
        try:
            import websocket
        except ImportError:
            raise BrowserConnectionError('FF Python에 websocket-client가 필요합니다. README의 설치 명령을 실행하세요.') from None
        self.check_connection()
        info = self.discovery('/json/new?about:blank', method='PUT')
        self.target = info.get('id')
        wsurl = urlsplit(info.get('webSocketDebuggerUrl', ''))
        if not self.target or not wsurl.path.startswith('/devtools/page/'):
            raise BrowserConnectionError('브라우저가 새 작업 탭 주소를 반환하지 않았습니다. CDP 중계 응답을 확인하세요: ' + self.address)
        u = urlsplit(self.address)
        address = urlunsplit(('wss' if u.scheme == 'https' else 'ws', u.netloc, wsurl.path, '', ''))
        try:
            self.ws = websocket.create_connection(address, timeout=1, suppress_origin=True,
                                                  host='localhost', http_no_proxy=[u.hostname])
            self.call('Page.enable')
        except Exception as exc:
            self.close()
            if isinstance(exc, Cancelled):
                raise
            raise connection_failure(exc, self.address, 'WebSocket 연결') from None

    def call(self, method, params=None):
        self.check()
        self.sequence += 1
        ident = self.sequence
        self.ws.send(json.dumps({'id': ident, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.check()
            try:
                raw = self.ws.recv()
            except Exception as exc:
                if type(exc).__name__ in ('WebSocketTimeoutException', 'TimeoutError'):
                    continue
                raise connection_failure(exc, self.address, 'WebSocket 응답') from None
            if not raw or len(raw) > 12 * 1024 * 1024:
                raise BrowserConnectionError('브라우저 응답이 없거나 너무 큽니다. CDP 중계 상태를 확인하세요: ' + self.address)
            message = json.loads(raw)
            if message.get('id') == ident:
                if message.get('error'):
                    detail = browser_detail(message['error'].get('message', ''))
                    if method == 'Runtime.evaluate' and any(x in detail.lower() for x in ('execution context was destroyed', 'cannot find context', 'cannot find default execution context', 'inspected target navigated')):
                        raise NavigationPending('페이지 전환 중 실행 문맥 대기: ' + detail)
                    raise SiteError('브라우저 명령 실패 [' + method + ']: ' + detail)
                return message.get('result', {})
        raise BrowserConnectionError('브라우저 응답 시간이 초과되었습니다. CDP 중계 상태를 확인하세요: ' + self.address)

    def evaluate(self, expression):
        response = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        if response.get('exceptionDetails'):
            error = response['exceptionDetails']
            detail = browser_detail((error.get('exception') or {}).get('description') or error.get('text'))
            raise SiteError('페이지 JavaScript 실행 실패 [Runtime.evaluate]: ' + (detail or '상세 정보 없음'))
        return response.get('result', {}).get('value')

    def accept_location(self, actual, path):
        """Accept redirects within each site family, preserving the requested work/page."""
        actual_base = origin(actual)
        current, expected = urlsplit(actual), urlsplit(self.base + path)
        if actual_base != self.base:
            family = lambda host: 'legacy' if re.fullmatch(r'newtoki[0-9]+\.(?:com|org|net)', host or '') else 'modern' if re.fullmatch(r'toki[0-9]+\.(?:com|org|net)', host or '') else None
            if not (family(current.hostname) and family(current.hostname) == family(expected.hostname)
                    and current.path == expected.path and current.query == expected.query):
                raise SiteError('다른 도메인 또는 작품 경로로 이동했습니다. 주소를 적용하지 않습니다.')
            self.base = actual_base

    def wait_catalog(self, path, html, update=lambda **kw: None):
        deadline = time.monotonic() + self.timeout
        while True:
            self.check()
            try:
                data = parse_catalog(html, self.base + path)
                return self.expand_catalog(path, html, data, update)
            except Blocked:
                raise
            except SiteError as exc:
                if str(exc) != '작품 제목/회차 목록 구조를 확인하지 못했습니다.':
                    raise
                if time.monotonic() >= deadline:
                    self.keep_tab = True
                    raise CatalogStructureError('로딩 대기 후에도 작품 제목/회차 목록 구조를 확인하지 못했습니다. 작업 탭은 유지했습니다. 정상 목록 화면의 HTML 파일을 전달해 주세요.') from None
            if self.stop.wait(.5):
                self.check()
            html = self.continue_html(path)

    def expand_catalog(self, path, html, data, update=lambda **kw: None):
        if 'has_more' not in data:
            return html
        if not hasattr(self, 'catalog_progress'):
            self.catalog_progress = {}
        key = self.base + path
        progress = self.catalog_progress.setdefault(key, {'title': data['title'], 'rows': {}, 'newer': False})
        for _ in range(1000):
            self.check()
            if data['title'] != progress['title']:
                raise CatalogStructureError('더보기 중 작품 제목이 바뀌었습니다.')
            for item in data['episodes']:
                previous_item = progress['rows'].get(item['id'])
                if previous_item and previous_item != item:
                    raise CatalogStructureError('더보기 중 같은 회차의 정보가 바뀌었습니다.')
                progress['rows'][item['id']] = item
            total = data.get('total_episodes')
            update(message='작품 목록 누적 %s / %s회 · 현재 화면 %s회' % (len(progress['rows']), total if total is not None else '확인 중', len(data['episodes'])))
            if (total is not None and len(progress['rows']) == total) or not (data.get('has_older') or data.get('has_newer')):
                if total is not None and len(progress['rows']) != total:
                    self.keep_tab = True
                    raise CatalogStructureError('표시된 전체 회차 수와 읽은 목록 수가 다릅니다. 일부 목록으로 진행하지 않습니다.')
                merged = dict(data, episodes=sorted(progress['rows'].values(), key=lambda e: (e['no'], int(e['id']))), has_more=False, has_older=False, has_newer=False)
                if len({e['no'] for e in merged['episodes']}) != len(merged['episodes']):
                    raise CatalogStructureError('누적 목록에 같은 회차 번호가 중복됩니다.')
                self.catalog_progress.pop(key, None)
                return CatalogSnapshot(html, merged)
            if not data.get('has_older'):
                progress['newer'] = True
            previous = tuple(e['id'] for e in data['episodes'])
            if self.stop.wait(self.delay):
                self.check()
            expression = LOAD_NEWER_EXPRESSION if progress['newer'] else LOAD_MORE_EXPRESSION
            if not self.evaluate(expression):
                self.keep_tab = True
                raise CatalogStructureError('회차 더 보기 버튼을 누를 수 없습니다. 현재 작업 탭을 확인하세요.')
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                self.check()
                html = self.continue_html(path)
                data = parse_catalog(html, self.base + path)
                if tuple(e['id'] for e in data['episodes']) != previous:
                    break
                if self.stop.wait(.5):
                    self.check()
            else:
                self.keep_tab = True
                raise CatalogStructureError('더보기 후 표시 회차가 바뀌지 않았습니다. 현재 작업 탭을 유지합니다.')
        self.keep_tab = True
        raise CatalogStructureError('목록 더보기 횟수가 1000회를 넘었습니다.')

    def html(self, path):
        if not path.startswith('/novel/'):
            raise SiteError('작품 페이지 경로만 열 수 있습니다.')
        self.catalog_progress = {}
        self.connect()
        if self.stop.wait(max(0, self.last_request + self.delay - time.monotonic())):
            self.check()
        self.last_request = time.monotonic()
        result = self.call('Page.navigate', {'url': self.base + path})
        if result.get('errorText'):
            error = result['errorText']
            if error in ('net::ERR_HTTP_RESPONSE_CODE_FAILURE', 'net::ERR_BLOCKED_BY_RESPONSE', 'net::ERR_ACCESS_DENIED'):
                raise Blocked('사이트가 페이지 요청을 거부했습니다. 현재 작업 탭에서 응답과 접근 상태를 직접 확인하세요: ' + error)
            transport = any(error == 'net::' + code for code in ('ERR_NAME_NOT_RESOLVED', 'ERR_CONNECTION_REFUSED', 'ERR_CONNECTION_RESET', 'ERR_CONNECTION_TIMED_OUT', 'ERR_TIMED_OUT', 'ERR_ADDRESS_UNREACHABLE', 'ERR_EMPTY_RESPONSE'))
            raise (DomainUnavailable if transport else SiteError)('페이지 탐색 실패: ' + error)
        deadline = time.monotonic() + self.timeout
        last_navigation_error = ''
        while time.monotonic() < deadline:
            self.check()
            try:
                state = self.evaluate("({url:location.href, ready:document.readyState, html:document.documentElement ? document.documentElement.outerHTML : ''})") or {}
            except NavigationPending as exc:
                last_navigation_error = str(exc)
                self.stop.wait(.3)
                continue
            actual = state.get('url', '')
            if actual and actual != 'about:blank' and state.get('html'):
                self.accept_location(actual, path)
                check_page(state.get('html', ''))
                expected = urlsplit(self.base + path)
                if urlsplit(actual).path != expected.path or urlsplit(actual).query != expected.query:
                    if state.get('ready') == 'complete':
                        raise Blocked('요청 회차 대신 다른 페이지가 열렸습니다. 브라우저에서 접근 상태를 확인하세요.')
                elif state.get('ready') in ('interactive', 'complete'):
                    return state.get('html', '')
            self.stop.wait(0.3)
        raise SiteError('페이지 로딩 시간이 초과되었습니다.' + (' ' + last_navigation_error if last_navigation_error else ''))

    def continue_html(self, path):
        """Read the manually confirmed catalog tab without navigation or refresh."""
        self.check()
        try:
            state = self.evaluate("({url:location.href, ready:document.readyState, html:document.documentElement ? document.documentElement.outerHTML : ''})") or {}
        except NavigationPending:
            raise Blocked('확인한 목록 페이지가 전환 중입니다. 표시 완료 후 계속을 누르세요.') from None
        if not state.get('url'):
            raise Blocked('현재 작업 탭의 주소를 확인할 수 없습니다.')
        current, expected = urlsplit(state.get('url', '')), urlsplit(self.base + path)
        self.accept_location(state['url'], path)
        if current.path != expected.path or current.query != expected.query:
            raise Blocked('확인할 작품 목록과 현재 작업 탭 주소가 다릅니다. 안내된 주소에서 직접 확인하세요.')
        html = state.get('html') or ''
        check_page(html)
        if not html or state.get('ready') not in ('interactive', 'complete'):
            raise Blocked('작품 목록이 아직 준비되지 않았습니다. 목록 표시 확인 후 계속을 누르세요.')
        return html

    def body(self, nid, item):
        html = self.html('/novel/%s/%s' % (nid, item['id']))
        return self.displayed_body(nid, item, html)

    def continue_body(self, nid, item):
        # User explicitly resumes the same visible task tab. No navigation or challenge solving.
        try:
            return self.displayed_body(nid, item)
        except NavigationPending:
            raise Pending('확인한 페이지가 전환 중입니다.') from None
        except SiteError as exc:
            if '회차 뷰어 정보를 찾을 수 없습니다' in str(exc):
                raise Blocked('현재 작업 탭에서 본문이 준비되지 않았습니다. 해당 회차를 직접 확인한 후 계속을 누르세요.') from None
            raise

    def modern_body(self):
        state = self.evaluate(MODERN_STATUS_EXPRESSION) or {}
        # Check human verification and paid/login notices before inspecting the viewer.
        if state.get('locked') or verification_notice(state.get('message', '')) or any(x in str(state.get('message', '')) for x in ('인증','검증')):
            validate_body(dict(state, rendered=False))
        root = self.call('DOM.getDocument', {'depth': 0})['root']['nodeId']
        ident = self.call('DOM.querySelector', {'nodeId': root, 'selector': 'article.novel-viewer > div[style*="--novel-font-size"]'}).get('nodeId')
        if not ident:
            return dict(state, rendered=False)
        node = self.call('DOM.describeNode', {'nodeId': ident, 'depth': -1, 'pierce': True}).get('node', {})
        return dict(state, **modern_dom_body(node))

    def displayed_body(self, nid, item, html=None):
        path = '/novel/%s/%s' % (nid, item['id'])
        current = self.evaluate('location.href')
        if origin(current) != self.base or urlsplit(current).path != path:
            raise Blocked('전용 브라우저의 작업 탭에서 해당 회차를 열고 확인 후 계속을 누르세요.')
        if html is None:
            html = self.evaluate("document.documentElement ? document.documentElement.outerHTML : ''") or ''
            if not html:
                raise Pending('현재 작업 탭의 문서가 아직 준비되지 않았습니다.')
        meta = viewer_config(html, nid, item['id'], expected_no=item['no'])
        if int(meta.get('no') or 0) != int(item['no']):
            raise SiteError('목록과 본문의 회차 번호가 다릅니다.')
        deadline = time.monotonic() + self.timeout
        last_pending = None
        while time.monotonic() < deadline:
            self.check()
            try:
                current = self.evaluate('location.href')
                state = self.modern_body() if meta.get('format') == 'modern' else self.evaluate(BODY_EXPRESSION)
                if meta.get('format') == 'modern':
                    current = self.evaluate('location.href')
            except NavigationPending:
                last_pending = None
                self.stop.wait(.5)
                continue
            if origin(current) != self.base or urlsplit(current).path != path:
                raise Blocked('본문 로딩 중 다른 페이지로 이동했습니다.')
            try:
                return validate_body(state)
            except Pending as exc:
                last_pending = exc
                self.stop.wait(0.5)
        if isinstance(last_pending, EmptyBody):
            raise last_pending
        raise Pending('본문이 표시되지 않았습니다. 전용 브라우저의 해당 탭을 확인하세요.')

    def cover(self, url):
        """Read a cover already loaded by the ordinary catalog page."""
        import base64
        if not url or urlsplit(url).scheme != 'https':
            raise SiteError('작품 표지 주소를 찾지 못했습니다.')
        deadline = time.monotonic() + min(self.timeout, 10)
        while time.monotonic() < deadline:
            self.check()
            loaded = self.evaluate("Array.from(document.images).some(i => (i.currentSrc === " + json.dumps(url) + " || i.src === " + json.dumps(url) + ") && i.complete && i.naturalWidth > 0)")
            if loaded:
                break
            self.stop.wait(.25)
        tree = self.call('Page.getResourceTree')['frameTree']
        def resources(node):
            for resource in node.get('resources', []):
                yield node['frame']['id'], resource
            for child in node.get('childFrames', []):
                yield from resources(child)
        for frame, resource in resources(tree):
            if resource.get('url') == url and resource.get('type') == 'Image':
                data = self.call('Page.getResourceContent', {'frameId': frame, 'url': url})
                if data.get('base64Encoded'):
                    payload = base64.b64decode(data['content'], validate=True)
                    if len(payload) > 10 * 1024 * 1024:
                        raise SiteError('표지 이미지가 10MB를 넘습니다.')
                    return payload
        raise SiteError('표지 이미지가 브라우저에 로딩되지 않았습니다.')

    def close(self):
        if self.ws:
            self.ws.close()
            self.ws = None
        if self.target and not self.keep_tab:
            try:
                # /json/close returns a plain string, unlike the discovery endpoints.
                req = urllib.request.Request(self.address + '/json/close/' + self.target, headers={'Host': 'localhost'})
                with urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({})).open(req, timeout=3) as response:
                    response.read(1024)
            except Exception:
                pass
        self.target = None

