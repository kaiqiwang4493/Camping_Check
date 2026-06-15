from __future__ import annotations

import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from yosemite_monitor.monitor import (
    CampgroundCandidate,
    Config,
    Opening,
    RECREATION_GOV_CAMPGROUNDS,
    apply_resolved_campgrounds,
    build_resolved_campgrounds_state,
    build_email_body,
    build_email_subject,
    build_reserve_california_url,
    build_clicksend_payload,
    build_summary_markdown,
    build_state,
    clicksend_configured,
    clicksend_partially_configured,
    chunk_messages,
    diff_new_openings,
    email_configured,
    email_partially_configured,
    filter_minimum_lead_time,
    filter_minimum_stay,
    filter_weekend_or_holiday,
    is_weekend_or_us_holiday,
    load_state,
    load_config,
    main,
    month_starts,
    normalize_password_secret,
    normalize_booking_date,
    normalize_text_secret,
    parse_openings,
    parse_campground_list,
    parse_reserve_california_campgrounds,
    resolve_campground_input,
    resolved_campgrounds_changed,
    scan_window_for_config,
    select_local_candidate,
    save_state,
    should_skip_for_interval,
)


class MonitorTests(unittest.TestCase):
    def test_month_starts_rolls_over_year(self) -> None:
        starts = month_starts(date(2026, 11, 15), 4)
        self.assertEqual(
            [item.isoformat() for item in starts],
            ["2026-11-01", "2026-12-01", "2027-01-01", "2027-02-01"],
        )

    def test_parse_openings_filters_available_only(self) -> None:
        payload = {
            "campsites": {
                "1": {
                    "site": "044",
                    "availabilities": {
                        "2026-04-10T00:00:00Z": "Reserved",
                        "2026-04-11T00:00:00Z": "Available",
                    },
                }
            }
        }
        yosemite_upper = next(item for item in RECREATION_GOV_CAMPGROUNDS if item["campground_name"] == "Upper Pines")
        openings = parse_openings("Upper Pines", yosemite_upper["campground_id"], payload)
        self.assertEqual(
            openings,
            [
                Opening(
                    park_name="Yosemite National Park",
                    campground_name="Upper Pines",
                    campground_id=yosemite_upper["campground_id"],
                    provider="Recreation.gov",
                    site="044",
                    date="2026-04-11",
                    url="https://www.recreation.gov/camping/campgrounds/232447",
                    campsite_id="1",
                )
            ],
        )
        self.assertEqual(openings[0].day_name, "Saturday")
        self.assertEqual(openings[0].day_type, "Weekend")
        self.assertEqual(openings[0].campsite_id, "1")

    def test_day_type_treats_friday_as_weekend(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="Upper Pines",
            campground_id="232447",
            provider="Recreation.gov",
            site="044",
            date="2026-04-10",
            url="https://www.recreation.gov/camping/campgrounds/232447",
        )
        self.assertEqual(opening.day_name, "Friday")
        self.assertEqual(opening.day_type, "Weekend")

    def test_diff_new_openings_only_returns_unseen_keys(self) -> None:
        existing = build_state(
            [
                Opening(
                    park_name="Yosemite National Park",
                    campground_name="Upper Pines",
                    campground_id="232447",
                    provider="Recreation.gov",
                    site="044",
                    date="2026-04-11",
                    url="https://www.recreation.gov/camping/campgrounds/232447",
                )
            ]
        )
        current = [
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="044",
                date="2026-04-11",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            ),
            Opening(
                park_name="Yosemite National Park",
                campground_name="North Pines",
                campground_id="232449",
                provider="Recreation.gov",
                site="101",
                date="2026-04-12",
                url="https://www.recreation.gov/camping/campgrounds/232449",
            ),
        ]
        new_items = diff_new_openings(current, existing)
        self.assertEqual([item.site for item in new_items], ["101"])

    def test_filter_minimum_stay_returns_only_two_night_windows(self) -> None:
        openings = [
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="044",
                date="2026-04-11",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            ),
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="044",
                date="2026-04-12",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            ),
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="050",
                date="2026-04-11",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            ),
        ]
        stays = filter_minimum_stay(openings, 2)
        self.assertEqual(len(stays), 1)
        self.assertEqual(stays[0].site, "044")
        self.assertEqual(stays[0].nights, 2)
        self.assertEqual(stays[0].stay_dates_label, "2026-04-11 to 2026-04-13")

    def test_filter_minimum_stay_rejects_single_available_night(self) -> None:
        openings = [
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="044",
                date="2026-04-11",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            )
        ]
        self.assertEqual(filter_minimum_stay(openings, 2), [])

    def test_filter_minimum_lead_time_ignores_openings_within_three_days(self) -> None:
        openings = [
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="001",
                date="2026-04-30",
                url="https://www.recreation.gov/camping/campgrounds/232447",
                nights=2,
            ),
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="002",
                date="2026-05-02",
                url="https://www.recreation.gov/camping/campgrounds/232447",
                nights=2,
            ),
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="003",
                date="2026-05-03",
                url="https://www.recreation.gov/camping/campgrounds/232447",
                nights=2,
            ),
        ]
        filtered = filter_minimum_lead_time(openings, date(2026, 4, 30), 3)
        self.assertEqual([opening.site for opening in filtered], ["003"])

    def test_chunk_messages_splits_when_too_long(self) -> None:
        openings = [
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site=f"{index:03d}",
                date="2026-04-11",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            )
            for index in range(1, 7)
        ]
        messages = chunk_messages(openings, max_chars=130)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(message.startswith("Camping openings:") for message in messages))

    def test_build_clicksend_payload_includes_sender_when_present(self) -> None:
        config = Config(
            clicksend_username="user",
            clicksend_api_key="key",
            phone_to="+14155550123",
            phone_from="CampAlert",
            gmail_smtp_user=None,
            gmail_smtp_app_password=None,
            email_to=None,
            email_from=None,
            dry_run=False,
            scan_months=12,
            morro_bay_scan_months=1,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
        )
        payload = build_clicksend_payload(["hello"], config)
        self.assertEqual(
            payload,
            {
                "messages": [
                    {
                        "source": "python",
                        "body": "hello",
                        "to": "+14155550123",
                        "from": "CampAlert",
                    }
                ]
            },
        )

    def test_state_round_trip(self) -> None:
        with TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "state.json"
            state = build_state(
                [
                    Opening(
                        park_name="Yosemite National Park",
                        campground_name="Lower Pines",
                        campground_id="232450",
                        provider="Recreation.gov",
                        site="003",
                        date="2026-05-05",
                        url="https://www.recreation.gov/camping/campgrounds/232450",
                    )
                ]
            )
            save_state(state_path, state)
            loaded = load_state(state_path)
            self.assertEqual(loaded["active_openings"], state["active_openings"])
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["version"], 1)

    def test_clicksend_configured_requires_all_required_values(self) -> None:
        config = Config(
            clicksend_username="user",
            clicksend_api_key=None,
            phone_to="+14155550123",
            phone_from=None,
            gmail_smtp_user=None,
            gmail_smtp_app_password=None,
            email_to=None,
            email_from=None,
            dry_run=False,
            scan_months=6,
            morro_bay_scan_months=1,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
        )
        self.assertFalse(clicksend_configured(config))
        self.assertTrue(clicksend_partially_configured(config))

    def test_email_configured_requires_all_required_values(self) -> None:
        config = Config(
            clicksend_username=None,
            clicksend_api_key=None,
            phone_to=None,
            phone_from=None,
            gmail_smtp_user="user@gmail.com",
            gmail_smtp_app_password=None,
            email_to="dest@example.com",
            email_from=None,
            dry_run=False,
            scan_months=6,
            morro_bay_scan_months=1,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
        )
        self.assertFalse(email_configured(config))
        self.assertTrue(email_partially_configured(config))

    def test_build_summary_markdown_includes_opening_table(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="North Pines",
            campground_id="232449",
            provider="Recreation.gov",
            site="101",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232449",
        )
        summary = build_summary_markdown(
            {
                "generated_at": "2026-03-24T20:00:00+00:00",
                "generated_at_display": "2026-03-24 13:00:00 PDT",
                "scan_months": 6,
                "current_openings_count": 1,
                "new_openings_count": 1,
                "sms_status": "clicksend_not_configured",
                "email_status": "not_configured",
                "dry_run": False,
                "clicksend_configured": False,
                "clicksend_partially_configured": True,
                "email_configured": False,
                "email_partially_configured": True,
            },
            [opening],
        )
        self.assertIn("## Camping Monitor", summary)
        self.assertIn("Generated at (America/Los_Angeles)", summary)
        self.assertIn("| Yosemite National Park | North Pines | 101 | 2026-04-12 to 2026-04-13 | Sunday | Weekend | 1 |", summary)
        self.assertIn("partially configured", summary)

    def test_load_config_uses_default_when_scan_months_is_blank(self) -> None:
        previous = os.environ.get("YOSEMITE_SCAN_MONTHS")
        previous_morro = os.environ.get("MORRO_BAY_SCAN_MONTHS")
        previous_interval = os.environ.get("QUERY_INTERVAL_MINUTES")
        try:
            os.environ["YOSEMITE_SCAN_MONTHS"] = ""
            os.environ["MORRO_BAY_SCAN_MONTHS"] = ""
            os.environ["QUERY_INTERVAL_MINUTES"] = ""
            config = load_config()
        finally:
            if previous is None:
                os.environ.pop("YOSEMITE_SCAN_MONTHS", None)
            else:
                os.environ["YOSEMITE_SCAN_MONTHS"] = previous
            if previous_morro is None:
                os.environ.pop("MORRO_BAY_SCAN_MONTHS", None)
            else:
                os.environ["MORRO_BAY_SCAN_MONTHS"] = previous_morro
            if previous_interval is None:
                os.environ.pop("QUERY_INTERVAL_MINUTES", None)
            else:
                os.environ["QUERY_INTERVAL_MINUTES"] = previous_interval
        self.assertEqual(config.scan_months, 6)
        self.assertEqual(config.morro_bay_scan_months, 1)
        self.assertEqual(config.query_interval_minutes, 15)
        self.assertEqual(config.stay_nights, 2)
        self.assertEqual(config.date_mode, "relative")
        self.assertEqual(config.lookahead_amount, 6)
        self.assertEqual(config.lookahead_unit, "months")
        self.assertTrue(config.schedule_enabled)

    def test_should_skip_for_interval_when_last_run_too_recent(self) -> None:
        config = Config(
            clicksend_username=None,
            clicksend_api_key=None,
            phone_to=None,
            phone_from=None,
            gmail_smtp_user=None,
            gmail_smtp_app_password=None,
            email_to=None,
            email_from=None,
            dry_run=False,
            scan_months=6,
            morro_bay_scan_months=1,
            query_interval_minutes=60,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
        )
        previous_state = {
            "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        }
        should_skip, reason = should_skip_for_interval(config, previous_state)
        self.assertTrue(should_skip)
        self.assertIn("minimum interval is 60 minutes", reason)

    def test_load_config_reads_campground_list_and_reserve_california_json(self) -> None:
        env = {
            "RESERVE_CALIFORNIA_CAMPGROUNDS_JSON": json.dumps(
                [
                    {
                        "park_name": "Test Park",
                        "park_id": 123,
                        "campground_name": "Test Camp",
                        "campground_id": 456,
                    }
                ]
            ),
            "CAMPGROUND_LIST": "Pfeiffer Big Sur Weyland Campground; Yosemite Lower Pines Campground\nKirk Creek",
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_MODEL": "gpt-test",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertEqual(config.reserve_california_campgrounds[0]["park_id"], 123)
        self.assertEqual(
            config.campground_list,
            (
                "Pfeiffer Big Sur Weyland Campground",
                "Yosemite Lower Pines Campground",
                "Kirk Creek",
            ),
        )
        self.assertEqual(config.openai_api_key, "openai-key")
        self.assertEqual(config.openai_model, "gpt-test")

    def test_load_config_reads_ui_query_settings(self) -> None:
        env = {
            "STAY_NIGHTS": "3",
            "DATE_MODE": "range",
            "START_DATE": "2026-07-01",
            "END_DATE": "2026-07-10",
            "LOOKAHEAD_AMOUNT": "8",
            "LOOKAHEAD_UNIT": "weeks",
            "REQUIRE_WEEKEND_OR_HOLIDAY": "true",
            "SCHEDULE_ENABLED": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_config()
        self.assertEqual(config.stay_nights, 3)
        self.assertEqual(config.date_mode, "range")
        self.assertEqual(config.start_date, date(2026, 7, 1))
        self.assertEqual(config.end_date, date(2026, 7, 10))
        self.assertEqual(config.lookahead_amount, 8)
        self.assertEqual(config.lookahead_unit, "weeks")
        self.assertTrue(config.require_weekend_or_holiday)
        self.assertFalse(config.schedule_enabled)

    def test_scan_window_supports_relative_weeks_and_explicit_range(self) -> None:
        relative = Config(
            clicksend_username=None,
            clicksend_api_key=None,
            phone_to=None,
            phone_from=None,
            gmail_smtp_user=None,
            gmail_smtp_app_password=None,
            email_to=None,
            email_from=None,
            dry_run=False,
            scan_months=6,
            morro_bay_scan_months=1,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
            lookahead_amount=2,
            lookahead_unit="weeks",
        )
        self.assertEqual(
            scan_window_for_config(relative, date(2026, 6, 1)),
            (date(2026, 6, 1), date(2026, 6, 15)),
        )
        explicit = Config(
            clicksend_username=None,
            clicksend_api_key=None,
            phone_to=None,
            phone_from=None,
            gmail_smtp_user=None,
            gmail_smtp_app_password=None,
            email_to=None,
            email_from=None,
            dry_run=False,
            scan_months=6,
            morro_bay_scan_months=1,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
            date_mode="range",
            start_date=date(2026, 9, 5),
            end_date=date(2026, 9, 8),
        )
        self.assertEqual(
            scan_window_for_config(explicit, date(2026, 6, 1)),
            (date(2026, 9, 5), date(2026, 9, 8)),
        )

    def test_weekend_or_holiday_filter_keeps_friday_and_federal_holiday(self) -> None:
        self.assertTrue(is_weekend_or_us_holiday(date(2026, 7, 3)))
        self.assertFalse(is_weekend_or_us_holiday(date(2026, 7, 7)))
        openings = [
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="001",
                date="2026-07-03",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            ),
            Opening(
                park_name="Yosemite National Park",
                campground_name="Upper Pines",
                campground_id="232447",
                provider="Recreation.gov",
                site="002",
                date="2026-07-07",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            ),
        ]
        self.assertEqual([item.site for item in filter_weekend_or_holiday(openings)], ["001"])

    def test_parse_campground_list_splits_common_separators(self) -> None:
        self.assertEqual(
            parse_campground_list(" Lower Pines ;\nPfeiffer Big Sur Weyland, Kirk Creek "),
            ("Lower Pines", "Pfeiffer Big Sur Weyland", "Kirk Creek"),
        )

    def test_parse_reserve_california_campgrounds_rejects_missing_fields(self) -> None:
        with self.assertRaises(ValueError):
            parse_reserve_california_campgrounds('[{"park_name": "Test"}]')

    def test_select_local_candidate_handles_loose_recreation_gov_name(self) -> None:
        candidates = [
            CampgroundCandidate(
                provider="Recreation.gov",
                park_name="Yosemite National Park, CA",
                campground_name="Lower Pines Campground",
                campground_id="232450",
                park_id=2991,
            )
        ]
        selected = select_local_candidate("Yosemite Lower Pines Campground", candidates)
        self.assertEqual(selected, candidates[0])

    def test_resolve_campground_input_uses_broad_reserve_california_candidates(self) -> None:
        reserve_candidates = [
            CampgroundCandidate(
                provider="ReserveCalifornia",
                park_name="Pfeiffer Big Sur SP",
                campground_name="Group Sites A & B",
                campground_id="609",
                park_id=690,
            ),
            CampgroundCandidate(
                provider="ReserveCalifornia",
                park_name="Pfeiffer Big Sur SP",
                campground_name="Weyland Camp (sites 79-130)",
                campground_id="612",
                park_id=690,
            ),
        ]
        selected, _ = resolve_campground_input(
            "Pfeiffer Big Sur Weyland Campground",
            Config(
                clicksend_username=None,
                clicksend_api_key=None,
                phone_to=None,
                phone_from=None,
                gmail_smtp_user=None,
                gmail_smtp_app_password=None,
                email_to=None,
                email_from=None,
                dry_run=False,
                scan_months=6,
                morro_bay_scan_months=1,
                state_path=Path("state.json"),
                request_timeout=30,
                report_path=Path("report.json"),
                summary_path=Path("summary.md"),
            ),
            recreation_searcher=lambda query: [],
            reserve_california_searcher=lambda query: reserve_candidates,
            openai_selector=lambda query, candidates, config: None,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.provider, "ReserveCalifornia")
        self.assertEqual(selected.park_id, 690)
        self.assertEqual(selected.campground_id, "612")

    def test_ambiguous_candidates_use_openai_selector_index_only(self) -> None:
        candidates = [
            CampgroundCandidate(
                provider="Recreation.gov",
                park_name="Test Park",
                campground_name="Alpha Campground",
                campground_id="1",
            ),
            CampgroundCandidate(
                provider="Recreation.gov",
                park_name="Test Park",
                campground_name="Beta Campground",
                campground_id="2",
            ),
        ]
        calls = []

        def choose_with_ai(query, candidate_list, config):
            calls.append((query, candidate_list))
            return candidate_list[1]

        selected, _ = resolve_campground_input(
            "Test campground",
            Config(
                clicksend_username=None,
                clicksend_api_key=None,
                phone_to=None,
                phone_from=None,
                gmail_smtp_user=None,
                gmail_smtp_app_password=None,
                email_to=None,
                email_from=None,
                dry_run=False,
                scan_months=6,
                morro_bay_scan_months=1,
                state_path=Path("state.json"),
                request_timeout=30,
                report_path=Path("report.json"),
                summary_path=Path("summary.md"),
                openai_api_key="key",
            ),
            recreation_searcher=lambda query: candidates,
            reserve_california_searcher=lambda query: [],
            openai_selector=choose_with_ai,
        )
        self.assertEqual(selected, candidates[1])
        self.assertEqual(len(calls), 1)

    def test_unresolved_when_no_candidates(self) -> None:
        config = Config(
            clicksend_username=None,
            clicksend_api_key=None,
            phone_to=None,
            phone_from=None,
            gmail_smtp_user=None,
            gmail_smtp_app_password=None,
            email_to=None,
            email_from=None,
            dry_run=False,
            scan_months=6,
            morro_bay_scan_months=1,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
            campground_list=("Nowhere Campground",),
        )
        state = build_resolved_campgrounds_state(
            config,
            recreation_searcher=lambda query: [],
            reserve_california_searcher=lambda query: [],
            openai_selector=lambda query, candidates, config: None,
            now=date(2026, 4, 1),
        )
        self.assertEqual(state["recreation_gov_campgrounds"], [])
        self.assertEqual(state["reserve_california_campgrounds"], [])
        self.assertEqual(state["unresolved"][0]["input"], "Nowhere Campground")

    def test_resolved_state_can_override_default_monitoring_list(self) -> None:
        config = Config(
            clicksend_username=None,
            clicksend_api_key=None,
            phone_to=None,
            phone_from=None,
            gmail_smtp_user=None,
            gmail_smtp_app_password=None,
            email_to=None,
            email_from=None,
            dry_run=False,
            scan_months=6,
            morro_bay_scan_months=1,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
            campground_list=("Yosemite Lower Pines Campground",),
        )
        state = {
            "recreation_gov_campgrounds": [
                {
                    "park_name": "Yosemite National Park",
                    "campground_name": "Lower Pines Campground",
                    "campground_id": "232450",
                }
            ],
            "reserve_california_campgrounds": [],
        }
        updated = apply_resolved_campgrounds(config, state)
        self.assertEqual(len(updated.recreation_gov_campgrounds), 1)
        self.assertEqual(updated.recreation_gov_campgrounds[0]["campground_id"], "232450")

    def test_resolved_campgrounds_changed_ignores_timestamp(self) -> None:
        previous = {
            "desired_inputs": ["Lower Pines"],
            "resolved_at": "old",
            "recreation_gov_campgrounds": [{"campground_id": "232450"}],
            "reserve_california_campgrounds": [],
            "unresolved": [],
        }
        current = previous | {"resolved_at": "new"}
        self.assertFalse(resolved_campgrounds_changed(previous, current))
        changed = current | {"unresolved": [{"input": "Unknown"}]}
        self.assertTrue(resolved_campgrounds_changed(previous, changed))

    def test_build_email_subject_and_body_include_day_name(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="North Pines",
            campground_id="232449",
            provider="Recreation.gov",
            site="101",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232449",
        )
        report = {
            "generated_at": "2026-03-25T00:00:00+00:00",
            "generated_at_display": "2026-03-24 17:00:00 PDT",
            "scan_months": 6,
            "current_openings_count": 1,
            "new_openings_count": 1,
        }
        self.assertEqual(
            build_email_subject([opening]),
            "Camping availability found: 1 new opening(s)",
        )
        body = build_email_body(report, [opening])
        self.assertIn("Generated at (America/Los_Angeles)", body)
        self.assertIn("Sunday", body)
        self.assertIn("Weekend", body)
        self.assertIn("North Pines", body)

    def test_normalize_password_secret_removes_unicode_and_regular_spaces(self) -> None:
        raw = "abcd\u00a0efgh ijkl\u202fmnop"
        self.assertEqual(normalize_password_secret(raw), "abcdefghijklmnop")

    def test_normalize_text_secret_strips_unicode_spaces(self) -> None:
        raw = "\u00a0 user@gmail.com \u202f"
        self.assertEqual(normalize_text_secret(raw), "user@gmail.com")

    def test_normalize_booking_date_truncates_datetime_string(self) -> None:
        self.assertEqual(normalize_booking_date("2026-03-29 00:00:00"), "2026-03-29")
        opening = Opening(
            park_name="Morro Bay SP",
            campground_name="Upper Section",
            campground_id="583",
            provider="ReserveCalifornia",
            site="086",
            date="2026-03-29 00:00:00",
            url="https://www.reservecalifornia.com/",
        )
        self.assertEqual(opening.date, "2026-03-29")
        self.assertEqual(opening.day_name, "Sunday")

    def test_build_reserve_california_url_uses_canonical_park_route(self) -> None:
        self.assertEqual(
            build_reserve_california_url(680, 583),
            "https://www.reservecalifornia.com/park/680/583",
        )

    def test_schedule_event_exits_when_schedule_disabled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "run-report.json"
            summary_path = Path(temp_dir) / "run-summary.md"
            state_path = Path(temp_dir) / "state.json"
            env = {
                "GITHUB_EVENT_NAME": "schedule",
                "SCHEDULE_ENABLED": "false",
                "REPORT_PATH": str(report_path),
                "SUMMARY_PATH": str(summary_path),
                "STATE_PATH": str(state_path),
            }
            with patch.dict(os.environ, env, clear=True):
                exit_code = main()

            self.assertEqual(exit_code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["schedule_enabled"])
            self.assertEqual(report["sms_status"], "schedule_disabled")
            self.assertIn("Scheduled queries are disabled", report["skipped_reason"])


if __name__ == "__main__":
    unittest.main()
