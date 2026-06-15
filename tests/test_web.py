from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from yosemite_monitor.web import (
    AppConfig,
    AuthenticatedUser,
    MonitorSettings,
    WatchCampground,
    app,
    config_from_variables,
    latest_workflow_status,
    require_user,
    update_variables,
    variables_from_config,
)


class WebTests(unittest.TestCase):
    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    def test_api_requires_bearer_token(self) -> None:
        client = TestClient(app)
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 401)

    def test_config_maps_to_github_action_variables(self) -> None:
        config = AppConfig(
            watched_campgrounds=[
                WatchCampground(campground_name="Upper Pines"),
                WatchCampground(campground_name="Lower Pines"),
            ],
            settings=MonitorSettings(
                date_mode="range",
                start_date="2026-08-01",
                end_date="2026-08-09",
                stay_nights=3,
                query_interval_hours=4,
                require_weekend_or_holiday=True,
                schedule_enabled=False,
            ),
        )
        variables = variables_from_config(config)
        self.assertEqual(variables["CAMPGROUND_LIST"], "Upper Pines\nLower Pines")
        self.assertEqual(variables["QUERY_INTERVAL_MINUTES"], "240")
        self.assertEqual(variables["STAY_NIGHTS"], "3")
        self.assertEqual(variables["DATE_MODE"], "range")
        self.assertEqual(variables["REQUIRE_WEEKEND_OR_HOLIDAY"], "true")
        self.assertEqual(variables["SCHEDULE_ENABLED"], "false")

    def test_config_loads_from_github_action_variables(self) -> None:
        config = config_from_variables(
            {
                "CAMPGROUND_LIST": "Upper Pines\nLower Pines",
                "QUERY_INTERVAL_MINUTES": "180",
                "STAY_NIGHTS": "2",
                "DATE_MODE": "relative",
                "LOOKAHEAD_AMOUNT": "5",
                "LOOKAHEAD_UNIT": "weeks",
                "REQUIRE_WEEKEND_OR_HOLIDAY": "false",
                "SCHEDULE_ENABLED": "true",
            }
        )
        self.assertEqual(len(config.watched_campgrounds), 2)
        self.assertEqual(config.settings.query_interval_hours, 3)
        self.assertEqual(config.settings.lookahead_unit, "weeks")
        self.assertTrue(config.settings.schedule_enabled)

    def test_update_variables_deletes_existing_empty_values(self) -> None:
        import yosemite_monitor.web as web

        calls: list[tuple[str, str, dict[str, str] | None]] = []
        original_github_request = web.github_request
        original_repository = web.github_repository_required
        original_read_variables = web.read_variables
        try:
            web.github_repository_required = lambda: "owner/repo"
            web.read_variables = lambda: {"START_DATE": "2026-08-01"}

            def fake_github_request(path: str, **kwargs: object) -> None:
                calls.append(
                    (
                        path,
                        str(kwargs.get("method", "GET")),
                        kwargs.get("body") if isinstance(kwargs.get("body"), dict) else None,
                    )
                )

            web.github_request = fake_github_request
            update_variables({"START_DATE": "", "END_DATE": "", "STAY_NIGHTS": "2"})
        finally:
            web.github_request = original_github_request
            web.github_repository_required = original_repository
            web.read_variables = original_read_variables

        self.assertEqual(
            calls,
            [
                ("/repos/owner/repo/actions/variables/START_DATE", "DELETE", None),
                ("/repos/owner/repo/actions/variables", "POST", {"name": "STAY_NIGHTS", "value": "2"}),
            ],
        )

    def test_status_allows_overridden_authenticated_user(self) -> None:
        app.dependency_overrides[require_user] = lambda: AuthenticatedUser(
            uid="1",
            email="owner@example.com",
        )
        client = TestClient(app)
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_email"], "owner@example.com")

    def test_latest_workflow_status_returns_latest_run_summary(self) -> None:
        import yosemite_monitor.web as web

        original_github_request = web.github_request
        original_repository = web.github_repository_required
        try:
            web.github_repository_required = lambda: "owner/repo"
            web.github_request = lambda *args, **kwargs: {
                "workflow_runs": [
                    {
                        "id": 123,
                        "name": "Monitor Yosemite Camping",
                        "run_number": 7,
                        "event": "workflow_dispatch",
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": "2026-06-13T20:00:00Z",
                        "updated_at": "2026-06-13T20:02:00Z",
                        "run_started_at": "2026-06-13T20:00:10Z",
                        "html_url": "https://github.com/owner/repo/actions/runs/123",
                    }
                ]
            }
            status = latest_workflow_status()
        finally:
            web.github_request = original_github_request
            web.github_repository_required = original_repository

        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["conclusion"], "success")
        self.assertEqual(status["run"]["run_number"], 7)


if __name__ == "__main__":
    unittest.main()
