import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2]))
from newtoki_downloader.notifications import ActivityLog, DiscordNotifier, summary_messages, webhook_url
from newtoki_downloader.parser import SiteError as MunpiaError


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []
        self.notifier = DiscordNotifier(self.tmp.name, lambda url, message: self.sent.append(message))
        self.url = 'https://discord.com/api/webhooks/123456/' + 'X' * 40
        self.report = {'source': '자동 수집', 'state': {'status': 'completed', 'completed': 1, 'failed': 0, 'finished': '2026-10-20'},
                       'downloads': [{'novel_id': '123', 'title': '시험 작품', 'no': 1, 'episode_title': '시작'}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_disabled_no_send_save_redaction_and_test(self):
        self.assertFalse(self.notifier.notify(self.report))
        public = self.notifier.save({'enabled': True, 'url': self.url})
        self.assertNotIn(self.url, json.dumps(public))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.notifier.path.stat().st_mode & 0o777, 0o600)
        self.notifier.save({'enabled': True, 'url': ''})
        self.assertTrue(self.notifier.notify(self.report))
        self.assertIn('회차: 0001', self.sent[0])
        self.assertNotIn('시작',self.sent[0])
        self.assertIn('자동 수집', self.sent[0])
        self.assertIn('시험 작품 — 1회 저장', self.sent[0])
        self.assertNotIn('/novel/', self.sent[0])
        self.assertNotIn('https://', self.sent[0])
        self.notifier.test()
        self.assertIn('테스트', self.sent[-1])

    def test_empty_success_silent_errors_and_cancel_reported(self):
        self.notifier.save({'enabled': True, 'url': self.url})
        self.report['state']['completed'] = 0
        self.report['downloads'] = []
        self.assertFalse(self.notifier.notify(self.report))
        self.report['state'].update(status='failed', message='시험 오류')
        self.assertTrue(self.notifier.notify(self.report))
        self.assertIn('시험 오류', self.sent[-1])
        self.report['state']['status'] = 'canceled'
        self.assertTrue(self.notifier.notify(self.report))

    def test_compact_ranges_and_sparse_summary_within_limit(self):
        from newtoki_downloader.notifications import episode_ranges
        self.assertEqual(episode_ranges([1,2,3,7,10,11,3]),['0001-0003','0007','0010-0011'])
        self.report['downloads'] = [{'novel_id':'123','title':'작품','no':i,'episode_title':'title-do-not-send'} for i in range(1,101)]
        text='\n'.join(summary_messages(self.report))
        self.assertIn('회차: 0001-0100',text)
        self.assertNotIn('title-do-not-send',text)
        self.report['downloads'] = [{'novel_id':'123','title':'작품','no':i} for i in range(1,3000,2)]
        messages=summary_messages(self.report)
        self.assertGreater(len(messages),1)
        self.assertTrue(all(len(s.encode('utf-16-le'))//2<=2000 for s in messages))
        self.assertIn('2999','\n'.join(messages))

    def test_reject_non_discord_and_redact_network_error(self):
        for url in ['http://discord.com/api/webhooks/1/x', 'https://discord.com.evil.test/api/webhooks/1/x', 'https://127.0.0.1/x']:
            with self.assertRaises(MunpiaError):
                webhook_url(url)
        with patch('urllib.request.build_opener', side_effect=RuntimeError(self.url)):
            with self.assertRaises(MunpiaError) as ctx:
                DiscordNotifier.send(self.url, 'test')
        self.assertNotIn(self.url, str(ctx.exception))

    def test_empty_body_summary_reports_missing_ranges_without_work_link(self):
        self.report['state']['empty_episodes']=[{'novel_id':'123','title':'시험 작품','no':n} for n in (2,3,7)]
        text='\n'.join(summary_messages(self.report))
        self.assertIn('시험 작품 — 본문 없음 · 미저장',text)
        self.assertIn('회차: 0002-0003, 0007',text)
        self.assertNotIn('/novel/',text)

    def test_sender_payload_has_no_mentions(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            DiscordNotifier.send(self.url, '@everyone')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(json.loads(request.data)['allowed_mentions'], {'parse': []})
            self.assertTrue(request.full_url.endswith('?wait=true'))

    def test_notification_times_are_korean_without_offset(self):
        for source, expected in [
            ('2026-10-21T00:22:24+00:00', '2026-10-21 09:22:24'),
            ('2026-10-21T00:22:24Z', '2026-10-21 09:22:24'),
            ('2026-10-21T09:22:24+09:00', '2026-10-21 09:22:24'),
            ('2026-10-20T23:22:24+00:00', '2026-10-21 08:22:24'),
            ('2026-10-21T00:22:24', '2026-10-21 09:22:24'),
        ]:
            with self.subTest(source=source):
                self.report['state']['finished']=source
                text='\n'.join(summary_messages(self.report))
                self.assertIn('종료: '+expected,text)
                self.assertNotIn('+00:00',text)
                self.assertNotIn('+09:00',text)
        self.report['state'].update(status='verification_waiting')
        self.report['state'].pop('finished')
        with patch('newtoki_downloader.notifications.now',return_value='2026-10-21T00:22:24+00:00'):
            self.assertIn('대기 시작: 2026-10-21 09:22:24','\n'.join(summary_messages(self.report)))

    def test_activity_is_bounded_and_redacted(self):
        log = ActivityLog(Path(self.tmp.name) / 'log.db')
        log.add('ERROR', self.url + '\nCookie: secret')
        row = log.rows()[0]
        self.assertNotIn(self.url, row['message'])
        self.assertNotIn('secret', row['message'])
        self.assertEqual(row['level'], 'ERROR')

    def test_same_domain_event_sent_once_but_errors_and_downloads_still_sent(self):
        self.notifier.save({'enabled':True,'url':self.url})
        event={'old':'https://newtoki1.org','new':'https://toki35.com','time':'2026-10-22T15:00:00+00:00'}
        self.report['state']['domain_change']=event
        self.notifier.notify(self.report)
        self.assertIn('도메인 변경:',self.sent[-1])
        fresh=DiscordNotifier(self.tmp.name,lambda url,message:self.sent.append(message))
        self.report['state'].update(status='failed',last_error='확인할 오류',failed=1)
        self.assertTrue(fresh.notify(self.report))
        self.assertNotIn('도메인 변경:',self.sent[-1])
        self.assertIn('확인할 오류',self.sent[-1])
        self.assertIn('회차: 0001',self.sent[-1])
        self.assertEqual(self.report['state']['domain_change'],event)
        self.report['state']['domain_change']=dict(event,old=event['new'],new=event['old'],time='2026-10-23T00:00:00+00:00')
        fresh.notify(self.report)
        self.assertIn('도메인 변경:',self.sent[-1])

    def test_failed_domain_notice_is_retried_and_empty_duplicate_is_silent(self):
        self.notifier.save({'enabled':True,'url':self.url})
        self.report['state'].update(completed=0,domain_change={'old':'old','new':'new','time':'time'})
        self.report['downloads']=[]
        self.notifier.sender=lambda *args:(_ for _ in ()).throw(RuntimeError('offline'))
        with self.assertRaises(RuntimeError):self.notifier.notify(self.report)
        self.notifier.sender=lambda url,message:self.sent.append(message)
        self.assertTrue(self.notifier.notify(self.report))
        self.assertIn('도메인 변경:',self.sent[-1])
        count=len(self.sent)
        self.assertFalse(self.notifier.notify(self.report))
        self.assertEqual(len(self.sent),count)

