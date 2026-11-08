"""Regressions for scans on runners unable to fetch the provider directory."""
import json
import os
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from camply.containers import SearchWindow
from camply.containers.usedirect import UseDirectAvailabilityResponse
from requests.exceptions import HTTPError
from yosemite_monitor.monitor import (
    CampWatchReserveCalifornia, CampWatchSearchReserveCalifornia, main,
)

CAMPGROUND = {
    'campground_id': '618', 'park_id': 691,
    'campground_name': 'Oceano Camp (sites 43-82)', 'park_name': 'Pismo SB',
}


class ReserveCaliforniaScanTests(unittest.TestCase):
    def test_scan_uses_live_availability_without_directory(self):
        day = date.today() + timedelta(days=7)
        payload = UseDirectAvailabilityResponse(
            Message='', UnitTypeId=0,
            Facility={'FacilityId': 618, 'Latitude': 35, 'Longitude': -120,
                      'Units': {'43': {'UnitId': 43, 'Name': 'OC43', 'Slices': {
                          datetime.combine(day, datetime.min.time()):
                              {'Date': day, 'IsFree': True},
                          datetime.combine(day + timedelta(days=1), datetime.min.time()):
                              {'Date': day + timedelta(days=1), 'IsFree': False},
                      }}}},
        )
        with patch.object(CampWatchReserveCalifornia, '_get_campground_metadata',
                          side_effect=AssertionError('Directory must not be fetched')), \
             patch.object(CampWatchReserveCalifornia, 'get_campsites_response',
                          return_value=payload) as availability:
            search = CampWatchSearchReserveCalifornia(
                known_campgrounds=[CAMPGROUND],
                search_window=SearchWindow(start_date=day, end_date=day + timedelta(days=2)),
                recreation_area=[691], campgrounds=['618'], nights=1,
            )
            sites = search.get_all_campsites()
        availability.assert_called()
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].campsite_site_name, 'OC43')
        self.assertEqual(sites[0].facility_name, CAMPGROUND['campground_name'])
        self.assertEqual(sites[0].recreation_area, 'Pismo SB')

    def test_unknown_campgrounds_still_use_directory(self):
        with patch.object(CampWatchReserveCalifornia, '_get_campground_metadata',
                          side_effect=HTTPError('403 Forbidden')):
            with self.assertRaises(HTTPError):
                CampWatchReserveCalifornia().find_campgrounds(campground_id=[618])

    def test_cached_scan_failure_preserves_results_and_notification_state(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            cache = root / 'resolved.json'
            cache.write_text(json.dumps({
                'desired_inputs': [CAMPGROUND['campground_name']], 'unresolved': [],
                'reserve_california_campgrounds': [CAMPGROUND],
                'recreation_gov_campgrounds': [],
            }))
            state = root / 'state.json'
            report = root / 'report.json'
            state.write_text('{}')
            report.write_text('{"previous": true}')
            with patch.dict(os.environ, {
                'CAMPGROUND_LIST': CAMPGROUND['campground_name'],
                'RESOLVED_CAMPGROUNDS_PATH': str(cache), 'STATE_PATH': str(state),
                'REPORT_PATH': str(report), 'SUMMARY_PATH': str(root / 'summary.md'),
            }, clear=True), \
                 patch('yosemite_monitor.monitor.build_resolved_campgrounds_state',
                       side_effect=AssertionError('Unchanged list must reuse IDs')), \
                 patch('yosemite_monitor.monitor.collect_openings',
                       side_effect=HTTPError('Availability blocked')):
                with self.assertRaisesRegex(HTTPError, 'Availability blocked'):
                    main()
            self.assertEqual(state.read_text(), '{}')
            self.assertEqual(report.read_text(), '{"previous": true}')
