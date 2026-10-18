"""Offline parser report or one live episode test; no body or secrets in reports."""
import argparse
import json
from pathlib import Path

from parser import parse_catalog, viewer_config, SiteError
from browser_client import BrowserClient


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--catalog-html')
    p.add_argument('--viewer-html')
    p.add_argument('--browser', help='CDP URL; live test opens exactly one episode')
    p.add_argument('--base', default='https://newtoki1.org')
    p.add_argument('--novel', default='58035')
    p.add_argument('--episode', default='5397869')
    p.add_argument('--number', type=int, default=440)
    args = p.parse_args()
    result, client = {}, None
    try:
        if args.catalog_html:
            data = parse_catalog(Path(args.catalog_html).read_text(encoding='utf-8'), args.base + '/novel/' + args.novel + '?epage=6')
            result['catalog'] = {'title': data['title'], 'count': len(data['episodes']), 'pages': data['pages'], 'min': min(e['no'] for e in data['episodes']), 'max': max(e['no'] for e in data['episodes'])}
        if args.viewer_html:
            html = Path(args.viewer_html).read_text(encoding='utf-8')
            result['viewer'] = viewer_config(html, args.novel, args.episode)
            result['note'] = '정적 HTML의 메타데이터 확인이며 실제 본문 성공을 의미하지 않습니다.'
        if args.browser:
            client = BrowserClient(args.base, args.browser)
            text = client.body(args.novel, {'id': args.episode, 'no': args.number})
            result['live'] = {'status': 'ok', 'characters': len(text), 'episode': args.episode}
        if not result:
            p.error('HTML 파일 또는 --browser를 지정하세요.')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except SiteError as exc:
        if client:
            client.keep_tab = True
        print(json.dumps({'status': exc.code, 'message': str(exc)}, ensure_ascii=False, indent=2))
        return 1
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    raise SystemExit(main())
