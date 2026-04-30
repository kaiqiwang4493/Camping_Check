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
    CartHoldResult,
    Config,
    Opening,
    RECREATION_GOV_CAMPGROUNDS,
    RecreationGovCartClient,
    ReserveCaliforniaCartClient,
    apply_resolved_campgrounds,
    auto_hold_first_opening,
    build_resolved_campgrounds_state,
    build_email_body,
    build_email_subject,
    build_recreation_gov_booking_url,
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
    filter_minimum_stay,
    load_state,
    load_config,
    month_starts,
    normalize_password_secret,
    normalize_booking_date,
    normalize_text_secret,
    parse_openings,
    parse_campground_list,
    parse_reserve_california_campgrounds,
    resolve_campground_input,
    resolved_campgrounds_changed,
    select_local_candidate,
    save_state,
    should_skip_for_interval,
)


class FakeLocator:
    def __init__(self, page, name: str, *, clickable: bool = True, fillable: bool = True) -> None:
        self.page = page
        self.name = name
        self.clickable = clickable
        self.fillable = fillable

    @property
    def first(self):
        return self

    def click(self, timeout: int = 5000) -> None:
        if not self.clickable:
            raise RuntimeError(f"{self.name} is not clickable")
        self.page.actions.append(("click", self.name))

    def fill(self, value: str, timeout: int = 5000) -> None:
        if not self.fillable:
            raise RuntimeError(f"{self.name} is not fillable")
        self.page.actions.append(("fill", self.name, value))

    def inner_text(self, timeout: int = 1000) -> str:
        return self.page.body_text


class FakePage:
    def __init__(self) -> None:
        self.actions = []
        self.body_text = "normal page"

    def set_default_timeout(self, timeout: int) -> None:
        self.actions.append(("timeout", timeout))

    def goto(self, url: str, wait_until: str = "load") -> None:
        self.actions.append(("goto", url, wait_until))

    def wait_for_load_state(self, state: str) -> None:
        self.actions.append(("wait", state))

    def locator(self, selector: str) -> FakeLocator:
        if selector == "body":
            return FakeLocator(self, selector)
        return FakeLocator(self, selector, fillable=selector.startswith("input"))

    def get_by_role(self, role: str, name: str) -> FakeLocator:
        return FakeLocator(self, f"{role}:{name}", fillable=False)

    def get_by_text(self, text: str, exact: bool = False) -> FakeLocator:
        return FakeLocator(self, f"text:{text}", fillable=False)

    def get_by_label(self, label: str) -> FakeLocator:
        return FakeLocator(self, f"label:{label}", clickable=False)

    def get_by_placeholder(self, label: str) -> FakeLocator:
        return FakeLocator(self, f"placeholder:{label}", clickable=False)


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
        self.assertEqual(stays[0].stay_dates_label, "2026-04-11 to 2026-04-12")

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
        self.assertIn("| Yosemite National Park | North Pines | 101 | 2026-04-12 | Sunday | Weekend | 1 |", summary)
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

    def test_load_config_reads_auto_cart_and_reserve_california_json(self) -> None:
        env = {
            "AUTO_CART_ENABLED": "true",
            "RECREATION_GOV_USERNAME": " camper@example.com ",
            "RECREATION_GOV_PASSWORD": "secret",
            "RESERVE_CALIFORNIA_USERNAME": "rc@example.com",
            "RESERVE_CALIFORNIA_PASSWORD": "rc-secret",
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
        self.assertTrue(config.auto_cart_enabled)
        self.assertEqual(config.recreation_gov_username, "camper@example.com")
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

    def test_build_email_for_successful_cart_hold_prompts_payment(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="Upper Pines",
            campground_id="232447",
            provider="Recreation.gov",
            site="044",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232447",
            nights=2,
        )
        result = CartHoldResult(
            enabled=True,
            status="held",
            provider="Recreation.gov",
            opening=opening,
            checkout_url="https://www.recreation.gov/cart",
            attempted_count=1,
        )
        report = {
            "generated_at_display": "2026-03-24 17:00:00 PDT",
            "scan_months": 6,
            "current_openings_count": 1,
            "new_openings_count": 1,
        }
        self.assertEqual(
            build_email_subject([opening], result),
            "Campsite held in cart: complete payment within 15 minutes",
        )
        body = build_email_body(report, [opening], result)
        self.assertIn("complete payment within about 15 minutes", body)
        self.assertIn("https://www.recreation.gov/cart", body)

    def test_build_email_for_failed_cart_hold_includes_manual_link(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="Upper Pines",
            campground_id="232447",
            provider="Recreation.gov",
            site="044",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232447",
            nights=2,
        )
        result = CartHoldResult(
            enabled=True,
            status="failed",
            provider="Recreation.gov",
            opening=opening,
            error="Recreation.gov login requires CAPTCHA or additional verification",
            attempted_count=1,
        )
        report = {
            "generated_at_display": "2026-03-24 17:00:00 PDT",
            "scan_months": 6,
            "current_openings_count": 1,
            "new_openings_count": 1,
        }
        self.assertEqual(build_email_subject([opening], result), "Cart hold failed: camping opening found")
        body = build_email_body(report, [opening], result)
        self.assertIn("Manual booking link", body)
        self.assertIn("CAPTCHA", body)

    def test_auto_hold_first_opening_uses_only_first_sorted_opening(self) -> None:
        later = Opening(
            park_name="Yosemite National Park",
            campground_name="Upper Pines",
            campground_id="232447",
            provider="Recreation.gov",
            site="044",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232447",
        )
        first = Opening(
            park_name="Yosemite National Park",
            campground_name="Lower Pines",
            campground_id="232450",
            provider="Recreation.gov",
            site="003",
            date="2026-04-11",
            url="https://www.recreation.gov/camping/campgrounds/232450",
        )
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
            auto_cart_enabled=True,
            recreation_gov_username="user",
            recreation_gov_password="password",
        )
        held = []

        class FakeClient:
            def hold(self, opening: Opening) -> CartHoldResult:
                held.append(opening)
                return CartHoldResult(
                    enabled=True,
                    status="held",
                    provider=opening.provider,
                    opening=opening,
                    checkout_url="cart",
                    attempted_count=1,
                )

        result = auto_hold_first_opening([later, first], config, lambda opening, config: FakeClient())
        self.assertEqual(result.status, "held")
        self.assertEqual(held, [first])

    def test_auto_hold_reports_missing_credentials_without_attempting(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="Upper Pines",
            campground_id="232447",
            provider="Recreation.gov",
            site="044",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232447",
        )
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
            auto_cart_enabled=True,
        )
        result = auto_hold_first_opening([opening], config)
        self.assertEqual(result.status, "missing_credentials")
        self.assertEqual(result.attempted_count, 0)

    def test_build_recreation_gov_booking_url_prefers_campsite_id(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="Upper Pines",
            campground_id="232447",
            provider="Recreation.gov",
            site="044",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232447",
            nights=2,
            campsite_id="101001",
        )
        url = build_recreation_gov_booking_url(opening)
        self.assertIn("/camping/campsites/101001", url)
        self.assertIn("startDate=2026-04-12", url)
        self.assertIn("endDate=2026-04-14", url)

    def test_recreation_gov_client_logs_in_and_clicks_add_to_cart(self) -> None:
        opening = Opening(
            park_name="Yosemite National Park",
            campground_name="Upper Pines",
            campground_id="232447",
            provider="Recreation.gov",
            site="044",
            date="2026-04-12",
            url="https://www.recreation.gov/camping/campgrounds/232447",
            nights=2,
            campsite_id="101001",
        )
        page = FakePage()
        result = RecreationGovCartClient("user@example.com", "password", 30).hold_with_page(page, opening)
        self.assertEqual(result.status, "held")
        self.assertIn(("fill", "label:Email", "user@example.com"), page.actions)
        self.assertIn(("fill", "label:Password", "password"), page.actions)
        self.assertIn(("click", "button:Add to Cart"), page.actions)
        self.assertTrue(any(action[0] == "goto" and "/camping/campsites/101001" in action[1] for action in page.actions))

    def test_reserve_california_client_logs_in_and_clicks_add_to_cart(self) -> None:
        opening = Opening(
            park_name="Test Park",
            campground_name="Test Camp",
            campground_id="456",
            provider="ReserveCalifornia",
            site="012",
            date="2026-04-12",
            url="https://www.reservecalifornia.com/park/123/456",
            nights=2,
        )
        page = FakePage()
        result = ReserveCaliforniaCartClient("user@example.com", "password", 30).hold_with_page(page, opening)
        self.assertEqual(result.status, "held")
        self.assertIn(("fill", "label:Email", "user@example.com"), page.actions)
        self.assertIn(("fill", "label:Password", "password"), page.actions)
        self.assertIn(("click", "button:Add to Cart"), page.actions)
        self.assertTrue(any(action[0] == "goto" and "reservecalifornia.com/park/123/456" in action[1] for action in page.actions))

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


if __name__ == "__main__":
    unittest.main()
