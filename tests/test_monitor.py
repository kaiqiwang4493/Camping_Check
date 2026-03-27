from __future__ import annotations

import json
import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from yosemite_monitor.monitor import (
    Config,
    Opening,
    RECREATION_GOV_CAMPGROUNDS,
    build_email_body,
    build_email_subject,
    build_clicksend_payload,
    build_summary_markdown,
    build_state,
    clicksend_configured,
    clicksend_partially_configured,
    chunk_messages,
    diff_new_openings,
    email_configured,
    email_partially_configured,
    load_state,
    load_config,
    month_starts,
    normalize_password_secret,
    normalize_booking_date,
    normalize_text_secret,
    parse_openings,
    save_state,
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
                )
            ],
        )
        self.assertEqual(openings[0].day_name, "Saturday")
        self.assertEqual(openings[0].day_type, "Weekend")

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
        self.assertTrue(all(message.startswith("Yosemite openings:") for message in messages))

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
        self.assertIn("## Yosemite Camping Monitor", summary)
        self.assertIn("| Yosemite National Park | North Pines | 101 | 2026-04-12 | Sunday | Weekend |", summary)
        self.assertIn("partially configured", summary)

    def test_load_config_uses_default_when_scan_months_is_blank(self) -> None:
        previous = os.environ.get("SCAN_MONTHS")
        try:
            os.environ["SCAN_MONTHS"] = ""
            config = load_config()
        finally:
            if previous is None:
                os.environ.pop("SCAN_MONTHS", None)
            else:
                os.environ["SCAN_MONTHS"] = previous
        self.assertEqual(config.scan_months, 6)

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
            "scan_months": 6,
            "current_openings_count": 1,
            "new_openings_count": 1,
        }
        self.assertEqual(
            build_email_subject([opening]),
            "Yosemite camping availability found: 1 new opening(s)",
        )
        body = build_email_body(report, [opening])
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


if __name__ == "__main__":
    unittest.main()
