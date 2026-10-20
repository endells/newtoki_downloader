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
from urllib.parse import urlsplit, urlunsplit

try:
    from .parser import SiteError, Blocked, Pending, Cancelled, origin, viewer_config, validate_body, check_page
except ImportError:
    from parser import SiteError, Blocked, Pending, Cancelled, origin, viewer_config, validate_body, check_page


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class NavigationPending(SiteError):
    """Only a transient browser context loss while a document is navigating."""


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
  return {rendered: !!root && nodes.length > 0,
    text: nodes.map(n => n.innerText || n.textContent || '').join('\n\n'),
    images: root ? root.querySelectorAll('img,svg,canvas,video').length : 0,
    message: notice, title: document.title, locked: gate.active === true || gate.locked === true || !!document.querySelector('[data-theme-novel-unlock]')};
})()'''


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
        raise SiteError('브라우저 주소를 내부 IP로 확인하지 못했습니다. 컨테이너 네트워크/주소를 확인하세요.') from None
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
            return json.loads(raw.decode('utf-8'))
        except Exception:
            raise SiteError('Chromium 연결 실패. 브라우저 컨테이너와 CDP 주소/포트(9223)를 확인하세요.') from None

    def connect(self):
        if self.ws:
            return
        try:
            import websocket
        except ImportError:
            raise SiteError('FF Python에 websocket-client가 필요합니다. README의 설치 명령을 실행하세요.') from None
        self.discovery('/json/version')
        info = self.discovery('/json/new?about:blank', method='PUT')
        self.target = info.get('id')
        wsurl = urlsplit(info.get('webSocketDebuggerUrl', ''))
        if not self.target or not wsurl.path.startswith('/devtools/page/'):
            raise SiteError('브라우저가 새 작업 탭 주소를 반환하지 않았습니다.')
        u = urlsplit(self.address)
        address = urlunsplit(('wss' if u.scheme == 'https' else 'ws', u.netloc, wsurl.path, '', ''))
        try:
            self.ws = websocket.create_connection(address, timeout=1, suppress_origin=True,
                                                  host='localhost', http_no_proxy=[u.hostname])
            self.call('Page.enable')
        except Exception:
            self.close()
            raise SiteError('브라우저 WebSocket 연결 실패. CDP 중계 컨테이너를 확인하세요.') from None

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
                raise SiteError('브라우저 연결이 끊어졌습니다.') from None
            if not raw or len(raw) > 12 * 1024 * 1024:
                raise SiteError('브라우저 응답이 없거나 너무 큽니다.')
            message = json.loads(raw)
            if message.get('id') == ident:
                if message.get('error'):
                    detail = browser_detail(message['error'].get('message', ''))
                    if method == 'Runtime.evaluate' and any(x in detail.lower() for x in ('execution context was destroyed', 'cannot find context', 'cannot find default execution context', 'inspected target navigated')):
                        raise NavigationPending('페이지 전환 중 실행 문맥 대기: ' + detail)
                    raise SiteError('브라우저 명령 실패 [' + method + ']: ' + detail)
                return message.get('result', {})
        raise SiteError('브라우저 응답 시간이 초과되었습니다.')

    def evaluate(self, expression):
        response = self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
        if response.get('exceptionDetails'):
            error = response['exceptionDetails']
            detail = browser_detail((error.get('exception') or {}).get('description') or error.get('text'))
            raise SiteError('페이지 JavaScript 실행 실패 [Runtime.evaluate]: ' + (detail or '상세 정보 없음'))
        return response.get('result', {}).get('value')

    def html(self, path):
        if not path.startswith('/novel/'):
            raise SiteError('작품 페이지 경로만 열 수 있습니다.')
        self.connect()
        if self.stop.wait(max(0, self.last_request + self.delay - time.monotonic())):
            self.check()
        self.last_request = time.monotonic()
        result = self.call('Page.navigate', {'url': self.base + path})
        if result.get('errorText'):
            raise SiteError('페이지 탐색 실패: ' + result['errorText'])
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
                if origin(actual) != self.base:
                    raise SiteError('다른 도메인으로 이동했습니다. 도메인 확인을 실행하세요.')
                check_page(state.get('html', ''))
                expected = urlsplit(self.base + path)
                if urlsplit(actual).path != expected.path or urlsplit(actual).query != expected.query:
                    if state.get('ready') == 'complete':
                        raise Blocked('요청 회차 대신 다른 페이지가 열렸습니다. 브라우저에서 접근 상태를 확인하세요.')
                elif state.get('ready') in ('interactive', 'complete'):
                    return state.get('html', '')
            self.stop.wait(0.3)
        raise SiteError('페이지 로딩 시간이 초과되었습니다.' + (' ' + last_navigation_error if last_navigation_error else ''))

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

    def displayed_body(self, nid, item, html=None):
        path = '/novel/%s/%s' % (nid, item['id'])
        current = self.evaluate('location.href')
        if origin(current) != self.base or urlsplit(current).path != path:
            raise Blocked('전용 브라우저의 작업 탭에서 해당 회차를 열고 확인 후 계속을 누르세요.')
        if html is None:
            html = self.evaluate("document.documentElement ? document.documentElement.outerHTML : ''") or ''
            if not html:
                raise Pending('현재 작업 탭의 문서가 아직 준비되지 않았습니다.')
        meta = viewer_config(html, nid, item['id'])
        if int(meta.get('no') or 0) != int(item['no']):
            raise SiteError('목록과 본문의 회차 번호가 다릅니다.')
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.check()
            try:
                current = self.evaluate('location.href')
                state = self.evaluate(BODY_EXPRESSION)
            except NavigationPending:
                self.stop.wait(.5)
                continue
            if origin(current) != self.base or urlsplit(current).path != path:
                raise Blocked('본문 로딩 중 다른 페이지로 이동했습니다.')
            try:
                return validate_body(state)
            except Pending:
                self.stop.wait(0.5)
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
