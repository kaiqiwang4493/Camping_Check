import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from datetime import date
from yosemite_monitor.nas_protocol import build_result, parse_search, search_plan
from yosemite_monitor.manual_scan import browser_scan_plan
from yosemite_monitor.web import AppConfig, MonitorSettings, WatchCampground

ROOT = Path(__file__).resolve().parents[1]
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
worker = module('nas_worker_protocol', 'nas_worker/worker.py')
mailer = module('nas_mailer', 'scripts/nas_maintenance.py')


class ProtocolTests(unittest.TestCase):
    def test_search_requests_are_allowlisted(self):
        plan = search_plan('Upper Pines & test')
        requests = [worker.provider_request(r) for r in plan['requests']]
        self.assertTrue(requests[0].full_url.startswith('https://www.recreation.gov/api/search?'))
        self.assertIn('q=Upper+Pines+%26+test', requests[0].full_url)
        self.assertTrue(requests[1].full_url.endswith('/rdr/fd/places'))
        self.assertTrue(requests[2].full_url.endswith('/rdr/fd/facilities'))
        for provider, operation in [('Unknown', 'search'), ('ReserveCalifornia', '../../evil'), ('Recreation.gov', 'facilities')]:
            with self.assertRaises(ValueError): worker.provider_request({'provider': provider, 'operation': operation})
        with self.assertRaises(ValueError): worker.provider_request({'provider':'Recreation.gov','operation':'search','query':'a'*201})

    def test_catalog_join(self):
        plan = search_plan('Oceano')
        payloads = [{'results': []}, [{'PlaceId': 691, 'Name':'Pismo SB'}],
                    [{'PlaceId':691,'FacilityId':618,'Name':'Oceano Camp','AllowWebBooking':True},
                     {'PlaceId':691,'FacilityId':999,'Name':'Private Oceano','AllowWebBooking':False}]]
        result = parse_search(plan, zip(plan['requests'],payloads))
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(result['candidates'][0]['campground_id'], '618')

    def test_real_parser_baseline_deduplicates_next_run(self):
        app = AppConfig(watched_campgrounds=[WatchCampground(provider='Recreation.gov',
            campground_name='Upper Pines', campground_id='232447', park_name='Yosemite')],
            settings=MonitorSettings(date_mode='range',start_date='2027-01-10',end_date='2027-01-15',stay_nights=2))
        _, plan = browser_scan_plan(app, {}, date(2026,11,8))
        payload = {'campsites': {'123': {'site':'001', 'availabilities': {
            '2027-01-10T00:00:00Z':'Available', '2027-01-11T00:00:00Z':'Available'}}}}
        job = {'kind':'scan','plan':plan,'config':app.dict(),'trigger':'scheduled','job_id':'test','config_revision':1}
        report, baseline = build_result(job, [(plan['requests'][0],payload)], {})
        self.assertEqual(report['current_openings_count'],1)
        self.assertEqual(report['new_openings_count'],1)
        next_report, _ = build_result(job, [(plan['requests'][0],payload)], baseline)
        self.assertEqual(next_report['new_openings_count'],0)


class MailTests(unittest.TestCase):
    def setUp(self):
        self.mail = {'id':'test','attempt_id':'attempt','kind':'test'}
        self.client = Mock()
        self.env = patch.dict('os.environ', {'GMAIL_SMTP_USER':'test@example.com','GMAIL_SMTP_APP_PASSWORD':'test',
                                            'EMAIL_TO':'target@example.com'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_send_then_ack(self):
        with patch.object(mailer.smtplib, 'SMTP') as smtp:
            smtp.return_value.send_message.return_value = {}
            self.assertEqual(mailer.deliver(self.client,self.mail),'sent')
        self.client.call.assert_called_once()
        self.client.ack.assert_called_once_with(self.mail,'sent',None)

    def test_timeout_after_begin_is_unknown(self):
        with patch.object(mailer.smtplib, 'SMTP') as smtp:
            smtp.return_value.send_message.side_effect = TimeoutError()
            self.assertEqual(mailer.deliver(self.client,self.mail),'unknown')
        self.assertEqual(self.client.ack.call_args.args[1], 'unknown')

    def test_login_failure_retries_without_begin(self):
        with patch.object(mailer.smtplib, 'SMTP') as smtp:
            smtp.return_value.login.side_effect = mailer.smtplib.SMTPAuthenticationError(535,b'rejected')
            self.assertEqual(mailer.deliver(self.client,self.mail),'retry')
        self.client.call.assert_not_called()

    def test_lost_begin_response_never_sends(self):
        self.client.call.side_effect = TimeoutError()
        with patch.object(mailer.smtplib, 'SMTP') as smtp:
            mailer.deliver(self.client,self.mail)
            smtp.return_value.send_message.assert_not_called()

if __name__ == '__main__': unittest.main()
