import unittest
from datetime import date
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
from dataclasses import replace
import json
from fastapi.testclient import TestClient

from yosemite_monitor.manual_scan import browser_scan_plan, report_from_browser
from yosemite_monitor.web import (
    AppConfig, MonitorSettings, WatchCampground, app, require_user,
    AuthenticatedUser, config_from_variables, variables_from_config,
)

CAMP = {'campground_name': 'Oceano Camp (sites 43-82)', 'campground_id': '618',
        'park_name': 'Pismo SB', 'park_id': 691, 'provider': 'ReserveCalifornia'}

class ManualScanTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig(watched_campgrounds=[WatchCampground(**CAMP)], settings=MonitorSettings(
            date_mode='range', start_date='2026-11-15', end_date='2026-11-18', stay_nights=2))
        self.monitor_config, self.plan = browser_scan_plan(self.config, {}, date(2026, 11, 8))
        self.results = [{'key': self.plan['requests'][0]['key'], 'payload': {
            'Message': '', 'UnitTypeId': 0, 'Facility': {'FacilityId': 618, 'Units': {
                '43': {'UnitId': 43, 'Name': 'OC43', 'Slices': {
                    f'2026-11-{day}T00:00:00': {'Date': f'2026-11-{day}', 'IsFree': True}
                    for day in [15, 16]
                }},
            }},
        }}]

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_skipped_schedule_preserves_browser_results(self):
        from yosemite_monitor.monitor import preserve_results_on_skip
        report = report_from_browser(self.monitor_config, self.plan, self.results, {})
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            path.write_text(json.dumps(report))
            preserved = preserve_results_on_skip(replace(self.monitor_config, report_path=path), {
                'skipped_reason': 'Interval not reached', 'sms_status': 'interval_skipped',
                'email_status': 'interval_skipped',
            })
        self.assertEqual(preserved['current_openings_count'], 1)
        self.assertEqual(preserved['scan_source'], 'browser')
        self.assertEqual(preserved['generated_at'], report['generated_at'])
        self.assertEqual(preserved['new_openings_count'], 0)

    def test_configuration_retains_selected_provider_ids(self):
        restored = config_from_variables(variables_from_config(self.config))
        self.assertEqual(restored.watched_campgrounds[0].campground_id, '618')
        self.assertEqual(str(restored.watched_campgrounds[0].park_id), '691')

    def test_old_name_only_config_uses_resolved_ids(self):
        self.config.watched_campgrounds = [WatchCampground(campground_name=CAMP['campground_name'])]
        _, plan = browser_scan_plan(self.config, {'reserve_california_campgrounds': [
            {k: v for k, v in CAMP.items() if k != 'provider'}]}, date(2026, 11, 8))
        self.assertEqual(plan['requests'][0]['body']['FacilityId'], 618)
        self.assertEqual(plan['requests'][0]['body']['StartDate'], '2026-11-15')

    def test_filters_consecutive_nights_and_saves_source(self):
        report = report_from_browser(self.monitor_config, self.plan, self.results, {})
        self.assertEqual(report['current_openings_count'], 1)
        self.assertEqual(report['current_openings'][0]['nights'], 2)
        self.assertEqual(report['scan_source'], 'browser')
        self.assertEqual(report['sms_messages_sent'], 0)

    def test_rejects_missing_or_duplicate_months(self):
        for results in [[], self.results * 2]:
            with self.assertRaises(ValueError):
                report_from_browser(self.monitor_config, self.plan, results, {})

    def test_rejects_wrong_facility_response(self):
        self.results[0]['payload']['Facility']['FacilityId'] = 999
        with self.assertRaises(ValueError):
            report_from_browser(self.monitor_config, self.plan, self.results, {})

    def test_browser_routes_require_login(self):
        client = TestClient(app)
        for path, body in [('/api/scan/plan', {}), ('/api/scan/complete', {'plan_id': '', 'results': []})]:
            self.assertEqual(client.post(path, json=body).status_code, 401)

    def test_completion_persists_report_only_after_validation(self):
        app.dependency_overrides[require_user] = lambda: AuthenticatedUser(uid='1', email='owner@example.com')
        with patch('yosemite_monitor.web.current_browser_scan_plan', return_value=(self.monitor_config, self.plan)), \
             patch('yosemite_monitor.web.read_repository_json', return_value={}), \
             patch('yosemite_monitor.web.save_browser_report') as save:
            client = TestClient(app)
            self.assertEqual(client.post('/api/scan/complete', json={'plan_id': 'old', 'results': self.results}).status_code, 409)
            save.assert_not_called()
            self.assertEqual(client.post('/api/scan/complete', json={'plan_id': self.plan['id'], 'results': []}).status_code, 422)
            save.assert_not_called()
            response = client.post('/api/scan/complete', json={'plan_id': self.plan['id'], 'results': self.results})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['current_openings_count'], 1)
            save.assert_called_once()
