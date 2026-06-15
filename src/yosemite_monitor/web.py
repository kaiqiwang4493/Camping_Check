from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import firebase_admin
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from firebase_admin import auth, credentials
from pydantic import BaseModel, Field

from yosemite_monitor.monitor import (
    CampgroundCandidate,
    Config,
    campground_match_score,
    load_config,
    resolve_campground_input,
)


GITHUB_API_BASE = "https://api.github.com"
VARIABLE_NAMES = (
    "CAMPGROUND_LIST",
    "QUERY_INTERVAL_MINUTES",
    "STAY_NIGHTS",
    "DATE_MODE",
    "LOOKAHEAD_AMOUNT",
    "LOOKAHEAD_UNIT",
    "START_DATE",
    "END_DATE",
    "REQUIRE_WEEKEND_OR_HOLIDAY",
    "SCHEDULE_ENABLED",
    "OPENAI_MODEL",
)


class WatchCampground(BaseModel):
    provider: str | None = None
    park_name: str | None = None
    campground_name: str
    campground_id: str | None = None
    park_id: str | int | None = None


class MonitorSettings(BaseModel):
    date_mode: str = Field(default="relative", regex="^(relative|range)$")
    lookahead_amount: int = Field(default=6, ge=1, le=104)
    lookahead_unit: str = Field(default="months", regex="^(weeks|months)$")
    start_date: str | None = None
    end_date: str | None = None
    stay_nights: int = Field(default=2, ge=1, le=30)
    require_weekend_or_holiday: bool = False
    schedule_enabled: bool = True
    query_interval_hours: int = Field(default=2, ge=1, le=168)
    openai_model: str | None = None


class AppConfig(BaseModel):
    watched_campgrounds: list[WatchCampground] = Field(default_factory=list)
    settings: MonitorSettings = Field(default_factory=MonitorSettings)


class CampgroundSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=200)


class WorkflowRunRequest(BaseModel):
    ref: str = "main"


class AuthenticatedUser(BaseModel):
    uid: str
    email: str


def create_app() -> FastAPI:
    app = FastAPI(title="Camping Check API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=parse_list_env("CORS_ALLOW_ORIGINS") or ["*"],
        allow_methods=["GET", "POST", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/status")
    def api_status(user: AuthenticatedUser = Depends(require_user)) -> dict[str, Any]:
        report = load_report()
        return {
            "status": "ok",
            "user_email": user.email,
            "github_configured": bool(github_token() and github_repository()),
            "openai_configured": bool(os.getenv("OPENAI_API_KEY")),
            "generated_at": report.get("generated_at") if report else None,
            "generated_at_display": report.get("generated_at_display") if report else None,
            "current_openings_count": report.get("current_openings_count", 0) if report else 0,
            "new_openings_count": report.get("new_openings_count", 0) if report else 0,
        }

    @app.get("/api/config", response_model=AppConfig)
    def get_config(_: AuthenticatedUser = Depends(require_user)) -> AppConfig:
        variables = read_variables()
        return config_from_variables(variables)

    @app.put("/api/config", response_model=AppConfig)
    def put_config(
        payload: AppConfig,
        _: AuthenticatedUser = Depends(require_user),
    ) -> AppConfig:
        update_variables(variables_from_config(payload))
        return payload

    @app.post("/api/campgrounds/search")
    def search_campgrounds(
        payload: CampgroundSearchRequest,
        _: AuthenticatedUser = Depends(require_user),
    ) -> dict[str, Any]:
        config = load_config()
        selected, candidates = resolve_campground_input(payload.query, config)
        ranked = rank_candidates(payload.query, candidates, selected)
        return {
            "query": payload.query,
            "selected": candidate_to_dict(selected) if selected else None,
            "candidates": [candidate_to_dict(candidate) for candidate in ranked[:25]],
            "ai_used": bool(config.openai_api_key and candidates),
        }

    @app.get("/api/results")
    def get_results(_: AuthenticatedUser = Depends(require_user)) -> dict[str, Any]:
        return load_report() or empty_report()

    @app.post("/api/workflow/run")
    def run_workflow(
        payload: WorkflowRunRequest,
        _: AuthenticatedUser = Depends(require_user),
    ) -> dict[str, Any]:
        workflow = os.getenv("GITHUB_WORKFLOW_ID", "monitor.yml")
        github_request(
            f"/repos/{github_repository_required()}/actions/workflows/{workflow}/dispatches",
            method="POST",
            body={"ref": payload.ref},
            expected=(204,),
        )
        return {"status": "queued", "workflow": workflow, "ref": payload.ref}

    @app.get("/api/workflow/latest")
    def latest_workflow_run(_: AuthenticatedUser = Depends(require_user)) -> dict[str, Any]:
        return latest_workflow_status()

    return app


def initialize_firebase() -> None:
    if firebase_admin._apps:
        return
    project_id = os.getenv("FIREBASE_PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
    if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        firebase_admin.initialize_app(credentials.ApplicationDefault(), {"projectId": project_id})
    else:
        firebase_admin.initialize_app(options={"projectId": project_id} if project_id else None)


def require_user(authorization: str | None = Header(default=None)) -> AuthenticatedUser:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    initialize_firebase()
    token = authorization.removeprefix("Bearer ").strip()
    try:
        decoded = auth.verify_id_token(token)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    email = str(decoded.get("email") or "").lower()
    allowed = {item.lower() for item in parse_list_env("ALLOWED_EMAILS")}
    if not email or not allowed or email not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Email is not allowed")
    return AuthenticatedUser(uid=str(decoded.get("uid") or decoded.get("sub") or ""), email=email)


def parse_list_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def github_token() -> str | None:
    return os.getenv("GITHUB_TOKEN")


def github_repository() -> str | None:
    return os.getenv("GITHUB_REPOSITORY")


def github_repository_required() -> str:
    repository = github_repository()
    if not repository:
        raise HTTPException(status_code=500, detail="GITHUB_REPOSITORY is not configured")
    return repository


def github_request(
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    expected: tuple[int, ...] = (200,),
) -> Any:
    token = github_token()
    if not token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN is not configured")

    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        f"{GITHUB_API_BASE}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "camping-check-web",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            if response.status not in expected:
                raise HTTPException(status_code=502, detail=f"GitHub returned {response.status}")
            if response.status == 204:
                return None
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 404:
            raise HTTPException(status_code=404, detail=detail) from exc
        raise HTTPException(status_code=502, detail=f"GitHub API failed: {detail}") from exc
    except URLError as exc:
        raise HTTPException(status_code=502, detail=f"GitHub API failed: {exc}") from exc


def read_variables() -> dict[str, str]:
    if not github_token() or not github_repository():
        return variables_from_env()
    payload = github_request(
        f"/repos/{github_repository_required()}/actions/variables?per_page=100",
        expected=(200,),
    )
    return {
        item["name"]: item.get("value", "")
        for item in payload.get("variables", [])
        if item.get("name") in VARIABLE_NAMES
    }


def variables_from_env() -> dict[str, str]:
    return {name: os.getenv(name, "") for name in VARIABLE_NAMES if os.getenv(name, "")}


def update_variables(values: dict[str, str]) -> None:
    repository = github_repository_required()
    existing = read_variables()
    for name, value in values.items():
        if not value:
            if name in existing:
                github_request(
                    f"/repos/{repository}/actions/variables/{name}",
                    method="DELETE",
                    expected=(204,),
                )
            continue

        body = {"name": name, "value": value}
        if name in existing:
            github_request(
                f"/repos/{repository}/actions/variables/{name}",
                method="PATCH",
                body=body,
                expected=(204,),
            )
        else:
            github_request(
                f"/repos/{repository}/actions/variables",
                method="POST",
                body=body,
                expected=(201,),
            )


def latest_workflow_status() -> dict[str, Any]:
    workflow = os.getenv("GITHUB_WORKFLOW_ID", "monitor.yml")
    payload = github_request(
        f"/repos/{github_repository_required()}/actions/workflows/{workflow}/runs?per_page=1",
        expected=(200,),
    )
    runs = payload.get("workflow_runs", [])
    if not runs:
        return {"status": "not_found", "workflow": workflow, "run": None}
    run = runs[0]
    return {
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "workflow": workflow,
        "run": {
            "id": run.get("id"),
            "name": run.get("name"),
            "run_number": run.get("run_number"),
            "event": run.get("event"),
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "run_started_at": run.get("run_started_at"),
            "html_url": run.get("html_url"),
        },
    }


def config_from_variables(values: dict[str, str]) -> AppConfig:
    campgrounds = [
        WatchCampground(campground_name=item)
        for item in split_campground_list(values.get("CAMPGROUND_LIST", ""))
    ]
    interval_minutes = int_or_default(values.get("QUERY_INTERVAL_MINUTES"), 120)
    return AppConfig(
        watched_campgrounds=campgrounds,
        settings=MonitorSettings(
            date_mode=values.get("DATE_MODE") or "relative",
            lookahead_amount=int_or_default(values.get("LOOKAHEAD_AMOUNT"), 6),
            lookahead_unit=values.get("LOOKAHEAD_UNIT") or "months",
            start_date=values.get("START_DATE") or None,
            end_date=values.get("END_DATE") or None,
            stay_nights=int_or_default(values.get("STAY_NIGHTS"), 2),
            require_weekend_or_holiday=parse_bool_value(
                values.get("REQUIRE_WEEKEND_OR_HOLIDAY", "")
            ),
            schedule_enabled=parse_bool_value_default(values.get("SCHEDULE_ENABLED"), True),
            query_interval_hours=max(1, round(interval_minutes / 60)),
            openai_model=values.get("OPENAI_MODEL") or None,
        ),
    )


def variables_from_config(config: AppConfig) -> dict[str, str]:
    settings = config.settings
    names = [
        item.campground_name.strip()
        for item in config.watched_campgrounds
        if item.campground_name.strip()
    ]
    return {
        "CAMPGROUND_LIST": "\n".join(dict.fromkeys(names)),
        "QUERY_INTERVAL_MINUTES": str(settings.query_interval_hours * 60),
        "STAY_NIGHTS": str(settings.stay_nights),
        "DATE_MODE": settings.date_mode,
        "LOOKAHEAD_AMOUNT": str(settings.lookahead_amount),
        "LOOKAHEAD_UNIT": settings.lookahead_unit,
        "START_DATE": settings.start_date or "",
        "END_DATE": settings.end_date or "",
        "REQUIRE_WEEKEND_OR_HOLIDAY": "true" if settings.require_weekend_or_holiday else "false",
        "SCHEDULE_ENABLED": "true" if settings.schedule_enabled else "false",
        "OPENAI_MODEL": settings.openai_model or os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
    }


def split_campground_list(raw: str) -> list[str]:
    normalized = raw.replace(";", "\n").replace(",", "\n")
    return [item.strip() for item in normalized.splitlines() if item.strip()]


def int_or_default(raw: str | None, default: int) -> int:
    try:
        return int(str(raw or "").strip())
    except ValueError:
        return default


def parse_bool_value(raw: str) -> bool:
    return raw.lower() in {"1", "true", "yes", "on"}


def parse_bool_value_default(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def rank_candidates(
    query: str,
    candidates: list[CampgroundCandidate],
    selected: CampgroundCandidate | None,
) -> list[CampgroundCandidate]:
    remaining = [candidate for candidate in candidates if candidate != selected]
    ranked = sorted(remaining, key=lambda item: campground_match_score(query, item), reverse=True)
    return ([selected] if selected else []) + ranked


def candidate_to_dict(candidate: CampgroundCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "provider": candidate.provider,
        "park_name": candidate.park_name,
        "campground_name": candidate.campground_name,
        "campground_id": candidate.campground_id,
        "park_id": candidate.park_id,
    }


def load_report() -> dict[str, Any] | None:
    report = load_report_from_github()
    if report is not None:
        return report
    path = Path(os.getenv("REPORT_PATH", "state/run-report.json"))
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_report_from_github() -> dict[str, Any] | None:
    if not github_token() or not github_repository():
        return None
    path = os.getenv("GITHUB_REPORT_PATH", "state/run-report.json")
    try:
        payload = github_request(
            f"/repos/{github_repository_required()}/contents/{path}",
            expected=(200,),
        )
    except HTTPException:
        return None
    content = payload.get("content")
    if not content:
        return None
    decoded = base64.b64decode(content).decode("utf-8")
    return json.loads(decoded)


def empty_report() -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current_openings_count": 0,
        "new_openings_count": 0,
        "current_openings": [],
        "new_openings": [],
    }


app = create_app()
