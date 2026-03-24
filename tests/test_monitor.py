from __future__ import annotations

import json
import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from yosemite_monitor.monitor import (
    CAMPGROUNDS,
    Config,
    Opening,
    build_clicksend_payload,
    build_summary_markdown,
    build_state,
    clicksend_configured,
    clicksend_partially_configured,
    chunk_messages,
    diff_new_openings,
    load_state,
    load_config,
    month_starts,
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
        openings = parse_openings("Upper Pines", CAMPGROUNDS["Upper Pines"], payload)
        self.assertEqual(
            openings,
            [
                Opening(
                    campground_name="Upper Pines",
                    campground_id=CAMPGROUNDS["Upper Pines"],
                    site="044",
                    date="2026-04-11",
                    url="https://www.recreation.gov/camping/campgrounds/232447",
                )
            ],
        )

    def test_diff_new_openings_only_returns_unseen_keys(self) -> None:
        existing = build_state(
            [
                Opening(
                    campground_name="Upper Pines",
                    campground_id="232447",
                    site="044",
                    date="2026-04-11",
                    url="https://www.recreation.gov/camping/campgrounds/232447",
                )
            ]
        )
        current = [
            Opening(
                campground_name="Upper Pines",
                campground_id="232447",
                site="044",
                date="2026-04-11",
                url="https://www.recreation.gov/camping/campgrounds/232447",
            ),
            Opening(
                campground_name="North Pines",
                campground_id="232449",
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
                campground_name="Upper Pines",
                campground_id="232447",
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
                        campground_name="Lower Pines",
                        campground_id="232450",
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
            dry_run=False,
            scan_months=6,
            state_path=Path("state.json"),
            request_timeout=30,
            report_path=Path("report.json"),
            summary_path=Path("summary.md"),
        )
        self.assertFalse(clicksend_configured(config))
        self.assertTrue(clicksend_partially_configured(config))

    def test_build_summary_markdown_includes_opening_table(self) -> None:
        opening = Opening(
            campground_name="North Pines",
            campground_id="232449",
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
                "dry_run": False,
                "clicksend_configured": False,
                "clicksend_partially_configured": True,
            },
            [opening],
        )
        self.assertIn("## Yosemite Camping Monitor", summary)
        self.assertIn("| North Pines | 101 | 2026-04-12 |", summary)
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


if __name__ == "__main__":
    unittest.main()
