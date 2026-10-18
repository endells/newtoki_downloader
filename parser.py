"""Site structure verified against user-supplied HTML, 2026-10-19.

No source HTML, session tokens, or novel text are bundled with this package.
"""
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urljoin, parse_qs


class SiteError(Exception):
    code = 'failed'


class Blocked(SiteError):
    code = 'verification_required'


class Locked(SiteError):
    code = 'locked'


class Pending(SiteError):
    code = 'body_pending'


class Cancelled(SiteError):
    code = 'canceled'


class Node:
    def __init__(self, tag='', attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self, tag=None, cls=None, attr=None):
        for child in self.children:
            if isinstance(child, Node):
                if ((tag is None or child.tag == tag) and
                        (cls is None or cls in child.attrs.get('class', '').split()) and
                        (attr is None or attr in child.attrs)):
                    yield child
                yield from child.walk(tag, cls, attr)

    def text(self):
        if self.tag in ('style', 'script'):
            return ''
        if self.tag == 'br':
            return '\n'
        content = ''.join(c.text() if isinstance(c, Node) else c for c in self.children)
        return content + ('\n' if self.tag in ('p', 'div', 'li', 'h1', 'h2', 'h3') else '')

    def raw_text(self):
        return ''.join(c.raw_text() if isinstance(c, Node) else c for c in self.children)

    def first(self, **kwargs):
        return next(self.walk(**kwargs), None)


class Document(Node, HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        Node.__init__(self)
        HTMLParser.__init__(self, convert_charrefs=True)
        self.stack = [self]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def origin(value):
    try:
        u = urlsplit(value)
        if (u.scheme != 'https' or not re.fullmatch(r'newtoki[0-9]+\.(?:org|com|net)', u.hostname or '')
                or u.username or u.password or u.port not in (None, 443)):
            raise ValueError()
    except ValueError:
        raise SiteError('구버전 newtoki숫자.org/com/net HTTPS 주소가 필요합니다.') from None
    return 'https://' + u.hostname


def novel_id(value):
    value = str(value or '').strip()
    if re.fullmatch(r'[1-9][0-9]{0,14}', value):
        return value
    origin(value)
    m = re.fullmatch(r'/novel/([1-9][0-9]{0,14})(?:/[1-9][0-9]{0,14})?/?', urlsplit(value).path)
    if not m:
        raise SiteError('작품 번호 또는 /novel/작품번호 주소를 입력하세요.')
    return m[1]


def title_ids(value):
    ids = list(dict.fromkeys(novel_id(v) for v in str(value or '').splitlines() if v.strip()))
    if len(ids) > 100:
        raise SiteError('자동 수집 작품은 최대 100개입니다.')
    return ids


def check_page(html):
    doc = Document(html)
    title = doc.first(tag='title')
    title = clean(title.text()).lower() if title else ''
    # Inspect challenge pages, not comments mentioning login, points or Cloudflare.
    known_site = doc.first(cls='serial-list') or doc.first(attr='data-theme-novel-content')
    if not known_site:
        visible = doc.text()
        if 'KR_ONLY' in visible:
            raise Blocked('KR_ONLY: 현재 브라우저의 접속 지역이 허용되지 않습니다.')
        if any(x in title for x in ('just a moment', 'access denied', 'attention required')) or doc.first(attr='data-cf-beacon') and 'Verify you are human' in visible:
            raise Blocked('사이트 보안 확인이 필요합니다. 전용 브라우저에서 확인 후 다시 실행하세요.')
        if any(x in visible for x in ('Checking your browser', 'Verify you are human', '인간인지 확인')):
            raise Blocked('사이트 보안 확인이 필요합니다. 자동 재시도하지 않습니다.')
    return doc


def parse_catalog(html, url):
    doc = check_page(html)
    nid, base = novel_id(url), origin(url)
    title_node = doc.first(cls='theme-detail-title-line')
    listing = doc.first(cls='serial-list')
    if not title_node or not listing:
        raise SiteError('작품 제목/회차 목록 구조를 확인하지 못했습니다.')
    episodes, seen = [], set()
    for row in listing.walk(tag='li', cls='list-item'):
        link, number = row.first(tag='a', cls='item-subject'), row.first(cls='wr-num')
        if not link or not number:
            continue
        href = urljoin(base, link.attrs.get('href', ''))
        u = urlsplit(href)
        m = re.fullmatch(r'/novel/' + re.escape(nid) + r'/([1-9][0-9]*)/?', u.path)
        if not m or origin(href) != base or not clean(number.text()).isdigit():
            raise SiteError('회차 목록의 ID/회차 번호/도메인이 일치하지 않습니다.')
        eid = m[1]
        if eid in seen:
            raise SiteError('한 페이지에 같은 회차 ID가 중복되었습니다.')
        seen.add(eid)
        label = clean(link.text())
        locked = bool(row.first(cls='fa-lock')) or any(x in label for x in ('포인트', '유료', '구매'))
        episodes.append({'id': eid, 'no': int(clean(number.text())), 'title': label,
                         'locked': locked, 'url': base + u.path})
    if not episodes:
        raise SiteError('회차 목록이 비어 있습니다.')
    pages = {1, int(parse_qs(urlsplit(url).query).get('epage', ['1'])[0])}
    for a in doc.walk(tag='a'):
        href = urljoin(base, a.attrs.get('href', ''))
        u = urlsplit(href)
        if u.hostname == urlsplit(base).hostname and u.path.rstrip('/') == '/novel/' + nid:
            p = parse_qs(u.query).get('epage', ['1'])[0]
            if p.isdigit():
                pages.add(int(p))
    if not all(1 <= p <= 1000 for p in pages):
        raise SiteError('페이지 범위가 비정상적입니다.')
    author = ''
    for row in doc.walk(cls='theme-detail-info-row'):
        label, val = row.first(cls='theme-detail-info-label'), row.first(cls='theme-detail-info-value')
        if label and val and clean(label.text()) == '작가':
            author = clean(val.text())
    description = doc.first(cls='theme-detail-description')
    return {'id': nid, 'title': clean(title_node.text()), 'author': author,
            'description': description.text().strip() if description else '',
            'episodes': episodes, 'pages': sorted(pages)}


def viewer_config(html, nid, eid):
    doc = check_page(html)
    node = next((x for x in doc.walk(tag='script') if x.attrs.get('id') == 'theme-novel-viewer-data'), None)
    if not node:
        raise SiteError('회차 뷰어 정보를 찾을 수 없습니다.')
    try:
        cfg = json.loads(node.raw_text())
    except (ValueError, TypeError):
        raise SiteError('회차 뷰어 정보가 올바른 JSON이 아닙니다.') from None
    if str(cfg.get('novelId')) != str(nid) or str(cfg.get('episodeId')) != str(eid):
        raise SiteError('요청한 회차와 페이지의 회차 ID가 다릅니다.')
    gate = cfg.get('paidGate') or {}
    if gate.get('active') is True or gate.get('locked') is True:
        raise Locked('로그인 또는 포인트 구매가 필요한 회차입니다. 자동 구매하지 않습니다.')
    # Return only nonsensitive metadata. Never return the site's token/session data.
    return {'id': str(eid), 'no': cfg.get('episodeNumber'), 'title': str(cfg.get('title') or '')}


def validate_body(snapshot):
    if not isinstance(snapshot, dict):
        raise Pending('본문이 아직 표시되지 않았습니다.')
    message = snapshot.get('message') or ''
    title = str(snapshot.get('title') or '').lower()
    if snapshot.get('locked'):
        raise Locked('로그인 또는 포인트 구매가 필요한 회차입니다.')
    if any(x in title for x in ('just a moment', 'access denied', 'attention required')) or any(x in message for x in ('인증', '검증', 'Verify you are human', 'KR_ONLY')):
        raise Blocked('본문 접근 확인이 필요합니다: ' + clean(message)[:180])
    if not snapshot.get('rendered'):
        raise Pending('본문이 아직 표시되지 않았습니다.')
    if snapshot.get('images'):
        raise SiteError('삽화가 포함된 회차입니다. 0.1 테스트 버전은 텍스트 회차만 지원하므로 저장하지 않았습니다.')
    text = str(snapshot.get('text') or '').replace('\r\n', '\n').strip()
    if len(text) < 30 or any(x in text for x in ('본문 불러오는 중...', '본문이 아직 준비되지 않았습니다.', '본문 보안 검증에 실패했습니다.')):
        raise Pending('정상 본문이 아직 준비되지 않았습니다.')
    return text


def discover_legacy(html, announcement):
    doc, found = Document(html), []
    for a in doc.walk(tag='a'):
        if '구버전' not in clean(a.text()):
            continue
        try:
            value = origin(urljoin(announcement, a.attrs.get('href', '')))
        except SiteError:
            continue
        if value not in found:
            found.append(value)
    if len(found) != 1:
        raise SiteError('안내 페이지에서 구버전 주소를 하나로 확정하지 못했습니다. 기존 주소를 유지합니다.')
    return found[0]
