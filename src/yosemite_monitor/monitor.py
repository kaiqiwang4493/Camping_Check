from __future__ import annotations

import base64
import difflib
import json
import os
import re
import smtplib
import sys
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

from camply.containers.data_containers import SearchWindow
from camply.search.search_usedirect import SearchReserveCalifornia
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


RECREATION_API_BASE = "https://www.recreation.gov/api/camps/availability/campground"
CLICKSEND_SMS_URL = "https://rest.clicksend.com/v3/sms/send"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
RECREATION_GOV_CHECKOUT_URL = "https://www.recreation.gov/cart"
RESERVE_CALIFORNIA_CHECKOUT_URL = "https://www.reservecalifornia.com/ShoppingCart"
DEFAULT_SCAN_MONTHS = 6
DEFAULT_MORRO_BAY_SCAN_MONTHS = 1
DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_QUERY_INTERVAL_MINUTES = 15
MIN_STAY_NIGHTS = 2
MIN_LEAD_DAYS = 3
DEFAULT_STATE_PATH = Path("state/notified-openings.json")
DEFAULT_RESOLVED_CAMPGROUNDS_PATH = Path("state/resolved-campgrounds.json")
DISPLAY_TIMEZONE = ZoneInfo("America/Los_Angeles")
DISPLAY_TIMEZONE_LABEL = "America/Los_Angeles"

RECREATION_GOV_CAMPGROUNDS = (
    {
        "park_name": "Yosemite National Park",
        "campground_name": "Upper Pines",
        "campground_id": "232447",
    },
    {
        "park_name": "Yosemite National Park",
        "campground_name": "Lower Pines",
        "campground_id": "232450",
    },
    {
        "park_name": "Los Padres National Forest",
        "campground_name": "Kirk Creek Campground",
        "campground_id": "233116",
    },
)

RESERVE_CALIFORNIA_CAMPGROUNDS = ()

UNICODE_SPACE_TRANSLATION = {
    ord("\u00a0"): " ",
    ord("\u2007"): " ",
    ord("\u202f"): " ",
}


@dataclass(frozen=True)
class Opening:
    park_name: str
    campground_name: str
    campground_id: str
    provider: str
    site: str
    date: str
    url: str
    nights: int = 1
    campsite_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", normalize_booking_date(self.date))

    @property
    def key(self) -> str:
        return f"{self.provider}|{self.campground_id}|{self.site}|{self.date}|{self.nights}"

    @property
    def start_date(self) -> date:
        return date.fromisoformat(self.date)

    @property
    def last_night_date(self) -> date:
        return self.start_date + timedelta(days=self.nights - 1)

    @property
    def checkout_date(self) -> date:
        return self.start_date + timedelta(days=self.nights)

    @property
    def stay_dates_label(self) -> str:
        return f"{self.date} to {self.checkout_date.isoformat()}"

    @property
    def day_name(self) -> str:
        return self.start_date.strftime("%A")

    @property
    def day_type(self) -> str:
        return "Weekend" if self.start_date.weekday() >= 4 else "Weekday"


@dataclass(frozen=True)
class Config:
    clicksend_username: str | None
    clicksend_api_key: str | None
    phone_to: str | None
    phone_from: str | None
    gmail_smtp_user: str | None
    gmail_smtp_app_password: str | None
    email_to: str | None
    email_from: str | None
    dry_run: bool
    scan_months: int
    morro_bay_scan_months: int
    state_path: Path
    request_timeout: int
    report_path: Path
    summary_path: Path
    auto_cart_enabled: bool = False
    recreation_gov_username: str | None = None
    recreation_gov_password: str | None = None
    reserve_california_username: str | None = None
    reserve_california_password: str | None = None
    recreation_gov_campgrounds: tuple[dict, ...] = RECREATION_GOV_CAMPGROUNDS
    reserve_california_campgrounds: tuple[dict, ...] = ()
    campground_list: tuple[str, ...] = ()
    resolved_campgrounds_path: Path = DEFAULT_RESOLVED_CAMPGROUNDS_PATH
    openai_api_key: str | None = None
    openai_model: str = DEFAULT_OPENAI_MODEL
    query_interval_minutes: int = DEFAULT_QUERY_INTERVAL_MINUTES


@dataclass(frozen=True)
class CartHoldResult:
    enabled: bool
    status: str
    provider: str | None = None
    opening: Opening | None = None
    error: str | None = None
    checkout_url: str | None = None
    attempted_count: int = 0


@dataclass(frozen=True)
class CampgroundCandidate:
    provider: str
    park_name: str
    campground_name: str
    campground_id: str
    park_id: str | int | None = None

    @property
    def search_text(self) -> str:
        return f"{self.park_name} {self.campground_name}"

    def to_config(self) -> dict:
        if self.provider == "ReserveCalifornia":
            return {
                "park_name": self.park_name,
                "park_id": self.park_id,
                "campground_name": self.campground_name,
                "campground_id": self.campground_id,
            }
        return {
            "park_name": self.park_name,
            "campground_name": self.campground_name,
            "campground_id": self.campground_id,
        }


def load_config() -> Config:
    scan_months_raw = os.getenv("YOSEMITE_SCAN_MONTHS", "").strip() or str(DEFAULT_SCAN_MONTHS)
    morro_bay_scan_months_raw = os.getenv("MORRO_BAY_SCAN_MONTHS", "").strip() or str(
        DEFAULT_MORRO_BAY_SCAN_MONTHS
    )
    query_interval_raw = os.getenv("QUERY_INTERVAL_MINUTES", "").strip() or str(
        DEFAULT_QUERY_INTERVAL_MINUTES
    )
    try:
        scan_months = max(1, int(scan_months_raw))
    except ValueError as exc:
        raise ValueError(f"Invalid YOSEMITE_SCAN_MONTHS value: {scan_months_raw}") from exc
    try:
        morro_bay_scan_months = max(1, int(morro_bay_scan_months_raw))
    except ValueError as exc:
        raise ValueError(
            f"Invalid MORRO_BAY_SCAN_MONTHS value: {morro_bay_scan_months_raw}"
        ) from exc
    try:
        query_interval_minutes = max(1, int(query_interval_raw))
    except ValueError as exc:
        raise ValueError(f"Invalid QUERY_INTERVAL_MINUTES value: {query_interval_raw}") from exc

    return Config(
        clicksend_username=normalize_text_secret(os.getenv("CLICKSEND_USERNAME")),
        clicksend_api_key=normalize_text_secret(os.getenv("CLICKSEND_API_KEY")),
        phone_to=normalize_text_secret(os.getenv("PHONE_TO")),
        phone_from=normalize_text_secret(os.getenv("PHONE_FROM")),
        gmail_smtp_user=normalize_text_secret(os.getenv("GMAIL_SMTP_USER")),
        gmail_smtp_app_password=normalize_password_secret(os.getenv("GMAIL_SMTP_APP_PASSWORD")),
        email_to=normalize_text_secret(os.getenv("EMAIL_TO")),
        email_from=normalize_text_secret(os.getenv("EMAIL_FROM")),
        dry_run=os.getenv("DRY_RUN", "").lower() in {"1", "true", "yes", "on"},
        scan_months=scan_months,
        morro_bay_scan_months=morro_bay_scan_months,
        query_interval_minutes=query_interval_minutes,
        state_path=Path(os.getenv("STATE_PATH", str(DEFAULT_STATE_PATH))),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        report_path=Path(os.getenv("REPORT_PATH", "state/run-report.json")),
        summary_path=Path(os.getenv("SUMMARY_PATH", "state/run-summary.md")),
        auto_cart_enabled=parse_bool_env("AUTO_CART_ENABLED"),
        recreation_gov_username=normalize_text_secret(os.getenv("RECREATION_GOV_USERNAME")),
        recreation_gov_password=normalize_text_secret(os.getenv("RECREATION_GOV_PASSWORD")),
        reserve_california_username=normalize_text_secret(os.getenv("RESERVE_CALIFORNIA_USERNAME")),
        reserve_california_password=normalize_text_secret(os.getenv("RESERVE_CALIFORNIA_PASSWORD")),
        reserve_california_campgrounds=parse_reserve_california_campgrounds(
            os.getenv("RESERVE_CALIFORNIA_CAMPGROUNDS_JSON")
        ),
        campground_list=parse_campground_list(os.getenv("CAMPGROUND_LIST")),
        resolved_campgrounds_path=Path(
            os.getenv("RESOLVED_CAMPGROUNDS_PATH", str(DEFAULT_RESOLVED_CAMPGROUNDS_PATH))
        ),
        openai_api_key=normalize_text_secret(os.getenv("OPENAI_API_KEY")),
        openai_model=normalize_text_secret(os.getenv("OPENAI_MODEL")) or DEFAULT_OPENAI_MODEL,
    )


def parse_bool_env(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def parse_campground_list(raw: str | None) -> tuple[str, ...]:
    normalized = normalize_text_secret(raw)
    if normalized is None:
        return ()
    parts = re.split(r"[,;\n]+", normalized)
    return tuple(part.strip() for part in parts if part.strip())


def parse_reserve_california_campgrounds(raw: str | None) -> tuple[dict, ...]:
    normalized = normalize_text_secret(raw)
    if normalized is None:
        return RESERVE_CALIFORNIA_CAMPGROUNDS

    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid RESERVE_CALIFORNIA_CAMPGROUNDS_JSON value") from exc

    if not isinstance(parsed, list):
        raise ValueError("RESERVE_CALIFORNIA_CAMPGROUNDS_JSON must be a JSON array")

    campgrounds = []
    required = {"park_name", "park_id", "campground_name", "campground_id"}
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise ValueError(f"ReserveCalifornia campground #{index + 1} must be an object")
        missing = required - item.keys()
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(
                f"ReserveCalifornia campground #{index + 1} is missing: {missing_list}"
            )
        campgrounds.append(
            {
                "park_name": str(item["park_name"]),
                "park_id": item["park_id"],
                "campground_name": str(item["campground_name"]),
                "campground_id": item["campground_id"],
            }
        )
    return tuple(campgrounds)


def normalize_text_secret(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.translate(UNICODE_SPACE_TRANSLATION).strip()
    return normalized or None


def normalize_password_secret(value: str | None) -> str | None:
    normalized = normalize_text_secret(value)
    if normalized is None:
        return None
    # Google app passwords are often shown in groups; remove all whitespace safely.
    compact = "".join(normalized.split())
    return compact or None


def normalize_booking_date(value: str) -> str:
    normalized = value.strip().replace("T", " ")
    if len(normalized) >= 10:
        return normalized[:10]
    return normalized


def normalize_campground_search_text(value: str) -> str:
    normalized = value.lower().translate(UNICODE_SPACE_TRANSLATION)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    words = [
        word
        for word in normalized.split()
        if word
        not in {
            "the",
            "campground",
            "campgrounds",
            "camp",
            "campsite",
            "campsites",
            "state",
            "park",
            "sp",
            "national",
            "forest",
        }
    ]
    return " ".join(words)


def campground_match_score(query: str, candidate: CampgroundCandidate) -> float:
    query_text = normalize_campground_search_text(query)
    candidate_text = normalize_campground_search_text(candidate.search_text)
    if not query_text or not candidate_text:
        return 0.0
    ratio = difflib.SequenceMatcher(None, query_text, candidate_text).ratio()
    query_words = set(query_text.split())
    candidate_words = set(candidate_text.split())
    overlap = len(query_words & candidate_words) / max(1, len(query_words))
    contains_bonus = 0.12 if query_text in candidate_text or candidate_text in query_text else 0.0
    return min(1.0, (ratio * 0.55) + (overlap * 0.45) + contains_bonus)


def select_local_candidate(query: str, candidates: list[CampgroundCandidate]) -> CampgroundCandidate | None:
    if not candidates:
        return None
    scored = sorted(
        ((campground_match_score(query, candidate), candidate) for candidate in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best_score >= 0.84 and best_score - second_score >= 0.08:
        return best
    return None


def derive_search_queries(query: str) -> list[str]:
    normalized = query.strip()
    words = normalized.split()
    queries = [normalized]
    suffixes = {"campground", "campgrounds", "camp", "campsite", "campsites"}
    without_suffixes = " ".join(word for word in words if word.lower() not in suffixes)
    if without_suffixes and without_suffixes != normalized:
        queries.append(without_suffixes)
    if len(words) > 2:
        queries.append(" ".join(words[:3]))
        queries.append(" ".join(words[:2]))
    deduped: list[str] = []
    for item in queries:
        item = item.strip()
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def dedupe_candidates(candidates: Iterable[CampgroundCandidate]) -> list[CampgroundCandidate]:
    deduped: dict[tuple[str, str, str | int | None], CampgroundCandidate] = {}
    for candidate in candidates:
        key = (candidate.provider, candidate.campground_id, candidate.park_id)
        deduped[key] = candidate
    return list(deduped.values())


def month_starts(today: date, count: int) -> list[date]:
    months: list[date] = []
    year = today.year
    month = today.month
    for offset in range(count):
        month_index = month - 1 + offset
        current_year = year + (month_index // 12)
        current_month = (month_index % 12) + 1
        months.append(date(current_year, current_month, 1))
    return months


def build_recreation_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.recreation.gov",
        "Referer": "https://www.recreation.gov/",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
    }


def fetch_month(campground_id: str, start_day: date, timeout: int) -> dict:
    params = urlencode({"start_date": f"{start_day.isoformat()}T00:00:00.000Z"})
    url = f"{RECREATION_API_BASE}/{campground_id}/month?{params}"
    request = Request(url, headers=build_recreation_headers())

    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Recreation.gov request failed for {campground_id} {start_day}: "
            f"HTTP {exc.code} {body}"
        ) from exc
    except URLError as exc:
        raise RuntimeError(
            f"Recreation.gov request failed for {campground_id} {start_day}: {exc}"
        ) from exc


def parse_openings(campground_name: str, campground_id: str, payload: dict) -> list[Opening]:
    openings: list[Opening] = []
    campground_url = f"https://www.recreation.gov/camping/campgrounds/{campground_id}"
    for campsite_id, campsite in payload.get("campsites", {}).items():
        site = str(campsite.get("site", "unknown"))
        for availability_date, status in campsite.get("availabilities", {}).items():
            if status != "Available":
                continue
            openings.append(
                Opening(
                    park_name="Yosemite National Park",
                    campground_name=campground_name,
                    campground_id=campground_id,
                    provider="Recreation.gov",
                    site=site,
                    date=availability_date[:10],
                    url=campground_url,
                    campsite_id=str(campsite_id),
                )
            )
    return openings


def parse_recreation_openings(park_name: str, campground_name: str, campground_id: str, payload: dict) -> list[Opening]:
    openings = parse_openings(campground_name, campground_id, payload)
    return [
        Opening(
            park_name=park_name,
            campground_name=item.campground_name,
            campground_id=item.campground_id,
            provider="Recreation.gov",
            site=item.site,
            date=item.date,
            url=item.url,
            campsite_id=item.campsite_id,
        )
        for item in openings
    ]


def build_reserve_california_url(park_id: int | str, campground_id: int | str) -> str:
    return f"https://www.reservecalifornia.com/park/{park_id}/{campground_id}"


def search_recreation_gov_candidates(query: str) -> list[CampgroundCandidate]:
    from camply.providers.recreation_dot_gov.recdotgov_camps import RecreationDotGov

    provider = RecreationDotGov()
    candidates: list[CampgroundCandidate] = []
    for search_query in derive_search_queries(query):
        try:
            results = provider.find_campgrounds(search_string=search_query)
        except Exception as exc:
            print(f"Recreation.gov campground search failed for {search_query!r}: {exc}")
            continue
        for item in results:
            candidates.append(
                CampgroundCandidate(
                    provider="Recreation.gov",
                    park_name=str(item.recreation_area),
                    campground_name=str(item.facility_name),
                    campground_id=str(item.facility_id),
                    park_id=getattr(item, "recreation_area_id", None),
                )
            )
    return dedupe_candidates(candidates)


def search_reserve_california_candidates(query: str) -> list[CampgroundCandidate]:
    from camply.providers.usedirect.variations import ReserveCalifornia

    provider = ReserveCalifornia()
    candidates: list[CampgroundCandidate] = []
    for search_query in derive_search_queries(query):
        try:
            results = provider.find_campgrounds(search_string=search_query, verbose=False)
        except Exception as exc:
            print(f"ReserveCalifornia campground search failed for {search_query!r}: {exc}")
            continue
        for item in results:
            candidates.append(
                CampgroundCandidate(
                    provider="ReserveCalifornia",
                    park_name=str(item.recreation_area),
                    campground_name=str(item.facility_name),
                    campground_id=str(item.facility_id),
                    park_id=int(item.recreation_area_id),
                )
            )
    return dedupe_candidates(candidates)


def extract_openai_text(payload: dict) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    texts: list[str] = []
    for output in payload.get("output", []):
        for content in output.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def select_candidate_with_openai(
    query: str,
    candidates: list[CampgroundCandidate],
    config: Config,
) -> CampgroundCandidate | None:
    if not config.openai_api_key or not candidates:
        return None

    candidate_payload = [
        {
            "index": index,
            "provider": candidate.provider,
            "park_name": candidate.park_name,
            "campground_name": candidate.campground_name,
            "park_id": candidate.park_id,
            "campground_id": candidate.campground_id,
        }
        for index, candidate in enumerate(candidates)
    ]
    prompt = (
        "Choose the one candidate campground that best matches the user's input. "
        "You must only choose from the provided candidates. If none match, return index null. "
        "Return strict JSON with keys index, confidence, reason.\n\n"
        f"User input: {query}\n"
        f"Candidates: {json.dumps(candidate_payload, ensure_ascii=True)}"
    )
    request_body = {
        "model": config.openai_model,
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "campground_choice",
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "index": {"type": ["integer", "null"]},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["index", "confidence", "reason"],
                },
                "strict": True,
            }
        },
    }
    request = Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.openai_api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=config.request_timeout) as response:
            parsed = json.load(response)
    except (HTTPError, URLError, TimeoutError) as exc:
        print(f"OpenAI candidate selection failed for {query!r}: {exc}")
        return None

    try:
        choice = json.loads(extract_openai_text(parsed))
    except json.JSONDecodeError:
        print(f"OpenAI returned invalid JSON for {query!r}.")
        return None

    index = choice.get("index")
    confidence = float(choice.get("confidence") or 0)
    if not isinstance(index, int) or index < 0 or index >= len(candidates) or confidence < 0.65:
        return None
    return candidates[index]


CandidateSearcher = Callable[[str], list[CampgroundCandidate]]
OpenAISelector = Callable[[str, list[CampgroundCandidate], Config], CampgroundCandidate | None]


def resolve_campground_input(
    query: str,
    config: Config,
    recreation_searcher: CandidateSearcher = search_recreation_gov_candidates,
    reserve_california_searcher: CandidateSearcher = search_reserve_california_candidates,
    openai_selector: OpenAISelector = select_candidate_with_openai,
) -> tuple[CampgroundCandidate | None, list[CampgroundCandidate]]:
    candidates = dedupe_candidates(
        [*recreation_searcher(query), *reserve_california_searcher(query)]
    )
    local_match = select_local_candidate(query, candidates)
    if local_match:
        return local_match, candidates
    ai_match = openai_selector(query, candidates, config)
    if ai_match:
        return ai_match, candidates
    return None, candidates


def build_resolved_campgrounds_state(
    config: Config,
    *,
    recreation_searcher: CandidateSearcher = search_recreation_gov_candidates,
    reserve_california_searcher: CandidateSearcher = search_reserve_california_candidates,
    openai_selector: OpenAISelector = select_candidate_with_openai,
    now: datetime | None = None,
) -> dict:
    recreation_gov: list[dict] = []
    reserve_california: list[dict] = []
    unresolved: list[dict] = []

    for query in config.campground_list:
        match, candidates = resolve_campground_input(
            query,
            config,
            recreation_searcher=recreation_searcher,
            reserve_california_searcher=reserve_california_searcher,
            openai_selector=openai_selector,
        )
        if match is None:
            unresolved.append(
                {
                    "input": query,
                    "candidate_count": len(candidates),
                    "candidates": [candidate.to_config() | {"provider": candidate.provider} for candidate in candidates[:10]],
                }
            )
            continue
        if match.provider == "ReserveCalifornia":
            reserve_california.append(match.to_config())
        else:
            recreation_gov.append(match.to_config())

    state = {
        "version": 1,
        "desired_inputs": list(config.campground_list),
        "resolved_at": (now or datetime.now(timezone.utc)).isoformat(),
        "recreation_gov_campgrounds": recreation_gov,
        "reserve_california_campgrounds": reserve_california,
        "unresolved": unresolved,
        "changed_from_previous": False,
    }
    previous = load_resolved_campgrounds_state(config.resolved_campgrounds_path)
    state["changed_from_previous"] = resolved_campgrounds_changed(previous, state)
    return state


def load_resolved_campgrounds_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolved_campgrounds_changed(previous: dict | None, current: dict) -> bool:
    if previous is None:
        return True
    keys = (
        "desired_inputs",
        "recreation_gov_campgrounds",
        "reserve_california_campgrounds",
        "unresolved",
    )
    return {key: previous.get(key) for key in keys} != {key: current.get(key) for key in keys}


def apply_resolved_campgrounds(config: Config, state: dict) -> Config:
    return replace(
        config,
        recreation_gov_campgrounds=tuple(state.get("recreation_gov_campgrounds", [])),
        reserve_california_campgrounds=tuple(state.get("reserve_california_campgrounds", [])),
    )


def end_date_for_scan(today: date, scan_months: int) -> date:
    return month_starts(today, scan_months + 1)[-1]


def collect_reserve_california_openings(config: Config, today: date) -> list[Opening]:
    scan_end = end_date_for_scan(today, config.morro_bay_scan_months)
    search_window = SearchWindow(start_date=today, end_date=scan_end)
    openings: list[Opening] = []

    for campground in config.reserve_california_campgrounds:
        search = SearchReserveCalifornia(
            search_window=search_window,
            recreation_area=[campground["park_id"]],
            campgrounds=[campground["campground_id"]],
            nights=1,
        )
        for campsite in search.get_matching_campsites(search_once=True, log=False):
            park_id = int(campsite.recreation_area_id or campground["park_id"])
            facility_id = int(campsite.facility_id or campground["campground_id"])
            openings.append(
                Opening(
                    park_name=str(campsite.recreation_area or campground["park_name"]),
                    campground_name=str(campsite.facility_name or campground["campground_name"]),
                    campground_id=str(facility_id),
                    provider="ReserveCalifornia",
                    site=str(campsite.campsite_site_name),
                    date=str(campsite.booking_date),
                    url=build_reserve_california_url(park_id, facility_id),
                )
            )

    return openings


def collect_openings(config: Config, today: date | None = None) -> list[Opening]:
    scan_from = today or date.today()
    openings: list[Opening] = []

    for campground in config.recreation_gov_campgrounds:
        for month_start in month_starts(scan_from, config.scan_months):
            payload = fetch_month(campground["campground_id"], month_start, config.request_timeout)
            openings.extend(
                parse_recreation_openings(
                    campground["park_name"],
                    campground["campground_name"],
                    campground["campground_id"],
                    payload,
                )
            )

    openings.extend(collect_reserve_california_openings(config, scan_from))

    openings = filter_minimum_stay(openings, MIN_STAY_NIGHTS)
    openings = filter_minimum_lead_time(openings, scan_from, MIN_LEAD_DAYS)
    return sorted(openings, key=lambda item: (item.date, item.park_name, item.campground_name, item.site))


def filter_minimum_stay(openings: Iterable[Opening], nights: int) -> list[Opening]:
    grouped: dict[tuple[str, str, str], list[Opening]] = defaultdict(list)
    for opening in openings:
        grouped[(opening.provider, opening.campground_id, opening.site)].append(opening)

    qualifying: list[Opening] = []
    for items in grouped.values():
        by_date = {item.start_date: item for item in items}
        for item in items:
            if all(item.start_date + timedelta(days=offset) in by_date for offset in range(nights)):
                qualifying.append(
                    Opening(
                        park_name=item.park_name,
                        campground_name=item.campground_name,
                        campground_id=item.campground_id,
                        provider=item.provider,
                        site=item.site,
                        date=item.date,
                        url=item.url,
                        nights=nights,
                        campsite_id=item.campsite_id,
                    )
                )

    return qualifying


def filter_minimum_lead_time(openings: Iterable[Opening], today: date, min_lead_days: int) -> list[Opening]:
    earliest_start = today + timedelta(days=min_lead_days)
    return [opening for opening in openings if opening.start_date >= earliest_start]


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "active_openings": {}, "updated_at": None}

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def should_skip_for_interval(config: Config, previous_state: dict) -> tuple[bool, str | None]:
    if config.dry_run:
        return False, None

    updated_at_raw = previous_state.get("updated_at")
    if not updated_at_raw:
        return False, None

    try:
        last_run = datetime.fromisoformat(updated_at_raw)
    except ValueError:
        return False, None

    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    elapsed_minutes = (datetime.now(timezone.utc) - last_run).total_seconds() / 60
    if elapsed_minutes < config.query_interval_minutes:
        return True, (
            f"Only {elapsed_minutes:.1f} minutes since last successful query; "
            f"minimum interval is {config.query_interval_minutes} minutes."
        )

    return False, None


def build_state(openings: Iterable[Opening]) -> dict:
    opening_map = {
        opening.key: {
            "park_name": opening.park_name,
            "campground_name": opening.campground_name,
            "campground_id": opening.campground_id,
            "provider": opening.provider,
            "site": opening.site,
            "date": opening.date,
            "nights": opening.nights,
            "url": opening.url,
            "campsite_id": opening.campsite_id,
        }
        for opening in openings
    }
    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "active_openings": opening_map,
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")


def diff_new_openings(current: Iterable[Opening], previous_state: dict) -> list[Opening]:
    previous = previous_state.get("active_openings", {})
    return [opening for opening in current if opening.key not in previous]


def format_opening_line(opening: Opening) -> str:
    return (
        f"{opening.park_name} | {opening.campground_name} site {opening.site} "
        f"{opening.stay_dates_label} ({opening.nights} nights) "
        f"{opening.day_name} ({opening.day_type}) {opening.url}"
    )


def chunk_messages(openings: Iterable[Opening], max_chars: int = 320) -> list[str]:
    sorted_openings = sorted(openings, key=lambda item: (item.park_name, item.campground_name, item.date, item.site))
    if not sorted_openings:
        return []

    header = "Camping openings:"
    messages: list[str] = []
    current = header

    for opening in sorted_openings:
        line = format_opening_line(opening)
        candidate = f"{current}\n{line}"
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current != header:
            messages.append(current)
            current = header

        if len(f"{header}\n{line}") <= max_chars:
            current = f"{header}\n{line}"
            continue

        # If a single line is too long, truncate the URL first to keep the alert readable.
        truncated = line[: max_chars - len(header) - 4] + "..."
        messages.append(f"{header}\n{truncated}")
        current = header

    if current != header:
        messages.append(current)

    return messages


def build_clicksend_payload(messages: Iterable[str], config: Config) -> dict:
    payload_messages = []
    for body in messages:
        message = {
            "source": "python",
            "body": body,
            "to": config.phone_to,
        }
        if config.phone_from:
            message["from"] = config.phone_from
        payload_messages.append(message)
    return {"messages": payload_messages}


def clicksend_configured(config: Config) -> bool:
    values = [config.clicksend_username, config.clicksend_api_key, config.phone_to]
    return all(values)


def clicksend_partially_configured(config: Config) -> bool:
    values = {
        "CLICKSEND_USERNAME": config.clicksend_username,
        "CLICKSEND_API_KEY": config.clicksend_api_key,
        "PHONE_TO": config.phone_to,
    }
    provided = {name: value for name, value in values.items() if value}
    return bool(provided) and len(provided) != len(values)


def email_configured(config: Config) -> bool:
    values = [config.gmail_smtp_user, config.gmail_smtp_app_password, config.email_to]
    return all(values)


def email_partially_configured(config: Config) -> bool:
    values = {
        "GMAIL_SMTP_USER": config.gmail_smtp_user,
        "GMAIL_SMTP_APP_PASSWORD": config.gmail_smtp_app_password,
        "EMAIL_TO": config.email_to,
    }
    provided = {name: value for name, value in values.items() if value}
    return bool(provided) and len(provided) != len(values)


def recreation_gov_cart_configured(config: Config) -> bool:
    return bool(config.recreation_gov_username and config.recreation_gov_password)


def reserve_california_cart_configured(config: Config) -> bool:
    return bool(config.reserve_california_username and config.reserve_california_password)


def opening_to_report(opening: Opening | None) -> dict | None:
    if opening is None:
        return None
    return {
        "park_name": opening.park_name,
        "campground_name": opening.campground_name,
        "campground_id": opening.campground_id,
        "provider": opening.provider,
        "site": opening.site,
        "date": opening.date,
        "stay_dates": opening.stay_dates_label,
        "day_name": opening.day_name,
        "day_type": opening.day_type,
        "nights": opening.nights,
        "url": opening.url,
        "campsite_id": opening.campsite_id,
    }


def cart_hold_to_report(result: CartHoldResult) -> dict:
    return {
        "cart_hold_enabled": result.enabled,
        "cart_hold_status": result.status,
        "cart_hold_provider": result.provider,
        "cart_hold_opening": opening_to_report(result.opening),
        "cart_hold_error": result.error,
        "cart_hold_checkout_url": result.checkout_url,
        "cart_hold_attempted_count": result.attempted_count,
    }


def build_recreation_gov_booking_url(opening: Opening) -> str:
    params = urlencode(
        {
            "startDate": opening.start_date.isoformat(),
            "endDate": opening.checkout_date.isoformat(),
        }
    )
    if opening.campsite_id:
        return f"https://www.recreation.gov/camping/campsites/{opening.campsite_id}?{params}"
    return f"{opening.url}?{params}"


def build_reserve_california_booking_url(opening: Opening) -> str:
    params = urlencode(
        {
            "arrivalDate": opening.start_date.isoformat(),
            "departureDate": opening.checkout_date.isoformat(),
        }
    )
    return f"{opening.url}?{params}"


def page_contains_blocking_challenge(page) -> bool:
    markers = ("captcha", "recaptcha", "verification", "two-factor", "two factor", "2fa")
    try:
        body_text = page.locator("body").inner_text(timeout=1000).lower()
    except Exception:
        return False
    return any(marker in body_text for marker in markers)


def first_locator(locator):
    first = getattr(locator, "first")
    return first() if callable(first) else first


def click_first_available(page, labels: Iterable[str], *, timeout: int = 5000) -> bool:
    for label in labels:
        locators = (
            lambda label=label: page.get_by_role("button", name=label),
            lambda label=label: page.get_by_role("link", name=label),
            lambda label=label: page.get_by_text(label, exact=True),
            lambda label=label: page.locator(f"text={label}"),
        )
        for locator_factory in locators:
            try:
                locator = locator_factory()
                first_locator(locator).click(timeout=timeout)
                return True
            except Exception:
                continue
    return False


def fill_first_available(page, labels: Iterable[str], value: str, *, timeout: int = 5000) -> bool:
    for label in labels:
        locators = (
            lambda label=label: page.get_by_label(label),
            lambda label=label: page.get_by_placeholder(label),
            lambda label=label: page.locator(label),
        )
        for locator_factory in locators:
            try:
                locator = locator_factory()
                first_locator(locator).fill(value, timeout=timeout)
                return True
            except Exception:
                continue
    return False


class RecreationGovCartClient:
    def __init__(self, username: str, password: str, timeout: int) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout

    def hold(self, opening: Opening) -> CartHoldResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed; run `python -m playwright install chromium`.") from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                return self.hold_with_page(page, opening)
            finally:
                browser.close()

    def hold_with_page(self, page, opening: Opening) -> CartHoldResult:
        page.set_default_timeout(self.timeout * 1000)
        page.goto("https://www.recreation.gov/", wait_until="domcontentloaded")
        click_first_available(page, ("Log In", "Sign In", "Login"))
        if not fill_first_available(page, ("Email", "Email Address", "Username", "input[type='email']"), self.username):
            raise RuntimeError("Could not find Recreation.gov username field")
        if not fill_first_available(page, ("Password", "input[type='password']"), self.password):
            raise RuntimeError("Could not find Recreation.gov password field")
        if not click_first_available(page, ("Log In", "Sign In", "Login")):
            raise RuntimeError("Could not submit Recreation.gov login form")
        page.wait_for_load_state("networkidle")
        if page_contains_blocking_challenge(page):
            raise RuntimeError("Recreation.gov login requires CAPTCHA or additional verification")

        page.goto(build_recreation_gov_booking_url(opening), wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        if page_contains_blocking_challenge(page):
            raise RuntimeError("Recreation.gov booking page requires CAPTCHA or additional verification")
        click_first_available(page, (opening.site,), timeout=3000)
        if not click_first_available(page, ("Add to Cart", "Reserve", "Book Now", "Continue")):
            raise RuntimeError("Could not find Recreation.gov Add to Cart button")
        page.wait_for_load_state("networkidle")
        if page_contains_blocking_challenge(page):
            raise RuntimeError("Recreation.gov cart hold requires CAPTCHA or additional verification")
        return CartHoldResult(
            enabled=True,
            status="held",
            provider=opening.provider,
            opening=opening,
            checkout_url=RECREATION_GOV_CHECKOUT_URL,
            attempted_count=1,
        )


class ReserveCaliforniaCartClient:
    def __init__(self, username: str, password: str, timeout: int) -> None:
        self.username = username
        self.password = password
        self.timeout = timeout

    def hold(self, opening: Opening) -> CartHoldResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed; run `python -m playwright install chromium`.") from exc

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                return self.hold_with_page(page, opening)
            finally:
                browser.close()

    def hold_with_page(self, page, opening: Opening) -> CartHoldResult:
        page.set_default_timeout(self.timeout * 1000)
        page.goto("https://www.reservecalifornia.com/", wait_until="domcontentloaded")
        click_first_available(page, ("Sign In", "Login", "Log In"))
        if not fill_first_available(page, ("Email", "Email Address", "Username", "input[type='email']"), self.username):
            raise RuntimeError("Could not find ReserveCalifornia username field")
        if not fill_first_available(page, ("Password", "input[type='password']"), self.password):
            raise RuntimeError("Could not find ReserveCalifornia password field")
        if not click_first_available(page, ("Sign In", "Login", "Log In")):
            raise RuntimeError("Could not submit ReserveCalifornia login form")
        page.wait_for_load_state("networkidle")
        if page_contains_blocking_challenge(page):
            raise RuntimeError("ReserveCalifornia login requires CAPTCHA or additional verification")

        page.goto(build_reserve_california_booking_url(opening), wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle")
        if page_contains_blocking_challenge(page):
            raise RuntimeError("ReserveCalifornia booking page requires CAPTCHA or additional verification")
        click_first_available(page, (opening.site,), timeout=3000)
        if not click_first_available(page, ("Add to Cart", "Reserve", "Book Now", "Continue")):
            raise RuntimeError("Could not find ReserveCalifornia Add to Cart button")
        page.wait_for_load_state("networkidle")
        if page_contains_blocking_challenge(page):
            raise RuntimeError("ReserveCalifornia cart hold requires CAPTCHA or additional verification")
        return CartHoldResult(
            enabled=True,
            status="held",
            provider=opening.provider,
            opening=opening,
            checkout_url=RESERVE_CALIFORNIA_CHECKOUT_URL,
            attempted_count=1,
        )


CartClientFactory = Callable[[Opening, Config], RecreationGovCartClient | ReserveCaliforniaCartClient]


def default_cart_client_factory(
    opening: Opening, config: Config
) -> RecreationGovCartClient | ReserveCaliforniaCartClient:
    if opening.provider == "Recreation.gov":
        if not recreation_gov_cart_configured(config):
            raise RuntimeError("Recreation.gov cart credentials are not configured")
        return RecreationGovCartClient(
            config.recreation_gov_username or "",
            config.recreation_gov_password or "",
            config.request_timeout,
        )
    if opening.provider == "ReserveCalifornia":
        if not reserve_california_cart_configured(config):
            raise RuntimeError("ReserveCalifornia cart credentials are not configured")
        return ReserveCaliforniaCartClient(
            config.reserve_california_username or "",
            config.reserve_california_password or "",
            config.request_timeout,
        )
    raise RuntimeError(f"Cart hold is not supported for provider: {opening.provider}")


def auto_hold_first_opening(
    new_openings: list[Opening],
    config: Config,
    client_factory: CartClientFactory = default_cart_client_factory,
) -> CartHoldResult:
    if not config.auto_cart_enabled:
        return CartHoldResult(enabled=False, status="disabled")
    if not new_openings:
        return CartHoldResult(enabled=True, status="no_new_openings")

    opening = sorted(new_openings, key=lambda item: (item.date, item.park_name, item.campground_name, item.site))[0]
    if config.dry_run:
        return CartHoldResult(
            enabled=True,
            status="dry_run_skipped",
            provider=opening.provider,
            opening=opening,
            attempted_count=0,
        )
    if opening.provider == "Recreation.gov" and not recreation_gov_cart_configured(config):
        return CartHoldResult(
            enabled=True,
            status="missing_credentials",
            provider=opening.provider,
            opening=opening,
            error="Recreation.gov cart credentials are not configured",
            attempted_count=0,
        )
    if opening.provider == "ReserveCalifornia" and not reserve_california_cart_configured(config):
        return CartHoldResult(
            enabled=True,
            status="missing_credentials",
            provider=opening.provider,
            opening=opening,
            error="ReserveCalifornia cart credentials are not configured",
            attempted_count=0,
        )

    try:
        client = client_factory(opening, config)
        return client.hold(opening)
    except Exception as exc:
        return CartHoldResult(
            enabled=True,
            status="failed",
            provider=opening.provider,
            opening=opening,
            error=str(exc),
            attempted_count=1,
        )


def send_clicksend(messages: list[str], config: Config) -> dict:
    payload = build_clicksend_payload(messages, config)
    credentials = f"{config.clicksend_username}:{config.clicksend_api_key}".encode("utf-8")
    auth_header = base64.b64encode(credentials).decode("ascii")
    request = Request(
        CLICKSEND_SMS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=config.request_timeout) as response:
            parsed = json.load(response)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ClickSend send failed: HTTP {exc.code} {body}") from exc
    except URLError as exc:
        raise RuntimeError(f"ClickSend send failed: {exc}") from exc

    if parsed.get("response_code") != "SUCCESS":
        raise RuntimeError(f"ClickSend send failed: {json.dumps(parsed, sort_keys=True)}")

    statuses = [item.get("status") for item in parsed.get("data", {}).get("messages", [])]
    allowed = {"SUCCESS", "QUEUED"}
    if statuses and any(status not in allowed for status in statuses):
        raise RuntimeError(f"ClickSend reported non-success status: {statuses}")

    return parsed


def build_email_subject(new_openings: list[Opening], cart_hold_result: CartHoldResult | None = None) -> str:
    if cart_hold_result and cart_hold_result.status == "held":
        return "Campsite held in cart: complete payment within 15 minutes"
    if cart_hold_result and cart_hold_result.status in {"failed", "missing_credentials"}:
        return "Cart hold failed: camping opening found"
    return f"Camping availability found: {len(new_openings)} new opening(s)"


def build_email_body(
    report: dict, new_openings: list[Opening], cart_hold_result: CartHoldResult | None = None
) -> str:
    lines = [
        "Camping Monitor",
        "",
        f"Generated at ({DISPLAY_TIMEZONE_LABEL}): {report['generated_at_display']}",
        f"Scan window: current month + next {report['scan_months'] - 1} month(s)",
        f"Query interval: {report.get('query_interval_minutes', DEFAULT_QUERY_INTERVAL_MINUTES)} minute(s)",
        f"Current openings found: {report['current_openings_count']}",
        f"New openings found: {report['new_openings_count']}",
        "",
    ]
    if cart_hold_result and cart_hold_result.opening:
        opening = cart_hold_result.opening
        if cart_hold_result.status == "held":
            lines.extend(
                [
                    "A campsite is held in your cart.",
                    "",
                    "Please complete payment within about 15 minutes before the hold expires.",
                    f"Checkout: {cart_hold_result.checkout_url or opening.url}",
                    "",
                    "Held opening:",
                    (
                        f"- {opening.provider} | {opening.campground_name} | site {opening.site} | "
                        f"{opening.stay_dates_label} | {opening.day_name} | {opening.day_type} | "
                        f"{opening.nights} nights"
                    ),
                    "",
                ]
            )
            return "\n".join(lines)
        if cart_hold_result.status in {"failed", "missing_credentials"}:
            lines.extend(
                [
                    "A camping opening was found, but the automatic cart hold did not complete.",
                    "",
                    f"Reason: {cart_hold_result.error or cart_hold_result.status}",
                    f"Manual booking link: {opening.url}",
                    "",
                    "Opening:",
                    (
                        f"- {opening.provider} | {opening.campground_name} | site {opening.site} | "
                        f"{opening.stay_dates_label} | {opening.day_name} | {opening.day_type} | "
                        f"{opening.nights} nights"
                    ),
                    "",
                ]
            )
            return "\n".join(lines)

    if new_openings:
        lines.extend(["New openings:", ""])
        for opening in new_openings:
            lines.append(
                f"- {opening.campground_name} | site {opening.site} | "
                f"{opening.stay_dates_label} | {opening.day_name} | {opening.day_type} | "
                f"{opening.nights} nights | {opening.url}"
            )
    else:
        lines.append(report.get("skipped_reason") or "No new openings in this run.")
    lines.append("")
    return "\n".join(lines)


def send_gmail_email(
    report: dict,
    new_openings: list[Opening],
    config: Config,
    cart_hold_result: CartHoldResult | None = None,
) -> None:
    message = EmailMessage()
    message["Subject"] = build_email_subject(new_openings, cart_hold_result)
    message["From"] = config.email_from or config.gmail_smtp_user
    message["To"] = config.email_to
    message.set_content(build_email_body(report, new_openings, cart_hold_result))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=config.request_timeout) as smtp:
        smtp.starttls()
        smtp.login(config.gmail_smtp_user, config.gmail_smtp_app_password)
        smtp.send_message(message)


def log_openings(label: str, openings: Iterable[Opening]) -> None:
    items = list(openings)
    print(f"{label}: {len(items)}")
    for opening in items:
        print(f"  - {format_opening_line(opening)}")


def build_run_report(
    *,
    config: Config,
    current_openings: list[Opening],
    new_openings: list[Opening],
    sms_status: str,
    sms_messages_sent: int,
    email_status: str,
    email_messages_sent: int,
    cart_hold_result: CartHoldResult | None = None,
    resolved_campgrounds_state: dict | None = None,
    skipped_reason: str | None = None,
) -> dict:
    generated_at_utc = datetime.now(timezone.utc)
    cart_hold = cart_hold_result or CartHoldResult(enabled=config.auto_cart_enabled, status="not_attempted")
    report = {
        "generated_at": generated_at_utc.isoformat(),
        "generated_at_display": generated_at_utc.astimezone(DISPLAY_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "scan_months": config.scan_months,
        "query_interval_minutes": config.query_interval_minutes,
        "dry_run": config.dry_run,
        "clicksend_configured": clicksend_configured(config),
        "clicksend_partially_configured": clicksend_partially_configured(config),
        "email_configured": email_configured(config),
        "email_partially_configured": email_partially_configured(config),
        "sms_status": sms_status,
        "sms_messages_sent": sms_messages_sent,
        "email_status": email_status,
        "email_messages_sent": email_messages_sent,
        "skipped_reason": skipped_reason,
        "current_openings_count": len(current_openings),
        "new_openings_count": len(new_openings),
        "campground_list_enabled": bool(config.campground_list),
        "campground_list_inputs": list(config.campground_list),
        "resolved_campgrounds_changed": bool(
            resolved_campgrounds_state and resolved_campgrounds_state.get("changed_from_previous")
        ),
        "resolved_recreation_gov_campgrounds": (
            resolved_campgrounds_state.get("recreation_gov_campgrounds", []) if resolved_campgrounds_state else []
        ),
        "resolved_reserve_california_campgrounds": (
            resolved_campgrounds_state.get("reserve_california_campgrounds", []) if resolved_campgrounds_state else []
        ),
        "unresolved_campgrounds": (
            resolved_campgrounds_state.get("unresolved", []) if resolved_campgrounds_state else []
        ),
        "new_openings": [
            {
                "campground_name": opening.campground_name,
                "site": opening.site,
                "date": opening.date,
                "stay_dates": opening.stay_dates_label,
                "day_name": opening.day_name,
                "day_type": opening.day_type,
                "nights": opening.nights,
                "url": opening.url,
                "campsite_id": opening.campsite_id,
            }
            for opening in new_openings
        ],
    }
    report.update(cart_hold_to_report(cart_hold))
    return report


def build_summary_markdown(report: dict, new_openings: list[Opening]) -> str:
    lines = [
        "## Camping Monitor",
        "",
        f"- Generated at ({DISPLAY_TIMEZONE_LABEL}): `{report['generated_at_display']}`",
        f"- Scan window: current month + next `{report['scan_months'] - 1}` month(s)",
        f"- Query interval: `{report.get('query_interval_minutes', DEFAULT_QUERY_INTERVAL_MINUTES)}` minute(s)",
        f"- Current openings found: `{report['current_openings_count']}`",
        f"- New openings found: `{report['new_openings_count']}`",
        f"- SMS status: `{report['sms_status']}`",
        f"- ClickSend configured: `{report['clicksend_configured']}`",
        f"- Email status: `{report['email_status']}`",
        f"- Email configured: `{report['email_configured']}`",
        f"- Auto cart hold: `{report.get('cart_hold_status', 'not_attempted')}`",
        f"- Dynamic campground list: `{report.get('campground_list_enabled', False)}`",
        f"- Dry run: `{report['dry_run']}`",
        "",
    ]
    if report.get("campground_list_enabled"):
        lines.extend(
            [
                "### Resolved campgrounds",
                "",
                f"- Changed from previous: `{report.get('resolved_campgrounds_changed', False)}`",
                f"- Recreation.gov campgrounds: `{len(report.get('resolved_recreation_gov_campgrounds', []))}`",
                f"- ReserveCalifornia campgrounds: `{len(report.get('resolved_reserve_california_campgrounds', []))}`",
                f"- Unresolved inputs: `{len(report.get('unresolved_campgrounds', []))}`",
                "",
            ]
        )
        unresolved = report.get("unresolved_campgrounds", [])
        if unresolved:
            lines.extend(["Unresolved campground inputs:", ""])
            for item in unresolved:
                lines.append(f"- `{item.get('input')}` ({item.get('candidate_count', 0)} candidate(s))")
            lines.append("")
    if report.get("cart_hold_opening"):
        opening = report["cart_hold_opening"]
        lines.extend(
            [
                "### Cart hold",
                "",
                f"- Provider: `{report.get('cart_hold_provider')}`",
                f"- Status: `{report.get('cart_hold_status')}`",
                (
                    f"- Target: `{opening['campground_name']} site {opening['site']} "
                    f"{opening['stay_dates']}`"
                ),
            ]
        )
        if report.get("cart_hold_checkout_url"):
            lines.append(f"- Checkout: [Open cart]({report['cart_hold_checkout_url']})")
        if report.get("cart_hold_error"):
            lines.append(f"- Error: `{report['cart_hold_error']}`")
        lines.append("")
    if report["clicksend_partially_configured"]:
        lines.extend(
            [
                "> Warning: ClickSend secrets are only partially configured. SMS was skipped.",
                "",
            ]
        )
    if report["email_partially_configured"]:
        lines.extend(
            [
                "> Warning: Gmail SMTP secrets are only partially configured. Email was skipped.",
                "",
            ]
        )
    if report.get("skipped_reason"):
        lines.extend([f"> Skipped: {report['skipped_reason']}", ""])
    if new_openings:
        lines.extend(
            [
                "### New openings",
                "",
                "| Park | Campground | Site | Stay Dates | Day | Day Type | Nights | Link |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for opening in new_openings:
            lines.append(
                f"| {opening.park_name} | {opening.campground_name} | {opening.site} | {opening.stay_dates_label} | "
                f"{opening.day_name} | "
                f"{opening.day_type} | "
                f"{opening.nights} | "
                f"[Book]({opening.url}) |"
            )
    else:
        lines.extend(["### New openings", "", "No new openings in this run."])
    lines.append("")
    return "\n".join(lines)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main() -> int:
    config = load_config()
    previous_state = load_state(config.state_path)
    should_skip, skipped_reason = should_skip_for_interval(config, previous_state)
    if should_skip:
        report = build_run_report(
            config=config,
            current_openings=[],
            new_openings=[],
            sms_status="interval_skipped",
            sms_messages_sent=0,
            email_status="interval_skipped",
            email_messages_sent=0,
            resolved_campgrounds_state=None,
            skipped_reason=skipped_reason,
        )
        write_json(config.report_path, report)
        write_text(config.summary_path, build_summary_markdown(report, []))
        print(skipped_reason)
        print(f"Run report written to {config.report_path}.")
        print(f"Run summary written to {config.summary_path}.")
        return 0

    resolved_campgrounds_state = None
    if config.campground_list:
        resolved_campgrounds_state = build_resolved_campgrounds_state(config)
        write_json(config.resolved_campgrounds_path, resolved_campgrounds_state)
        config = apply_resolved_campgrounds(config, resolved_campgrounds_state)
        print(
            "Resolved campground list: "
            f"{len(config.recreation_gov_campgrounds)} Recreation.gov, "
            f"{len(config.reserve_california_campgrounds)} ReserveCalifornia, "
            f"{len(resolved_campgrounds_state.get('unresolved', []))} unresolved."
        )
    current_openings = collect_openings(config)
    previous_state = load_state(config.state_path)
    new_openings = diff_new_openings(current_openings, previous_state)

    log_openings("Current openings", current_openings)
    log_openings("New openings", new_openings)

    cart_hold_result = auto_hold_first_opening(new_openings, config)
    if cart_hold_result.opening:
        print(
            "Cart hold status: "
            f"{cart_hold_result.status} for {cart_hold_result.provider} "
            f"{cart_hold_result.opening.campground_name} site {cart_hold_result.opening.site}."
        )
        if cart_hold_result.error:
            print(f"Cart hold error: {cart_hold_result.error}")
    else:
        print(f"Cart hold status: {cart_hold_result.status}.")
        if cart_hold_result.error:
            print(f"Cart hold error: {cart_hold_result.error}")

    sms_status = "not_attempted"
    sms_messages_sent = 0
    email_status = "not_attempted"
    email_messages_sent = 0

    if config.dry_run:
        sms_status = "dry_run_skipped"
        email_status = "dry_run_skipped"
        print("DRY_RUN enabled. Skipping notifications and state write.")
    elif clicksend_partially_configured(config):
        sms_status = "clicksend_partial_config_skipped"
        print("ClickSend is only partially configured. Skipping SMS and logging only.")
    elif new_openings and clicksend_configured(config):
        messages = chunk_messages(new_openings)
        send_result = send_clicksend(messages, config)
        queued = send_result.get("data", {}).get("queued_count")
        sms_messages_sent = len(messages)
        sms_status = "sent"
        print(f"ClickSend queued {queued} message(s).")
    elif new_openings and not clicksend_configured(config):
        sms_status = "clicksend_not_configured"
        print("New openings detected, but ClickSend is not configured. Logging only.")
    else:
        sms_status = "no_new_openings"
        print("No new openings detected.")

    report = build_run_report(
        config=config,
        current_openings=current_openings,
        new_openings=new_openings,
        sms_status=sms_status,
        sms_messages_sent=sms_messages_sent,
        email_status=email_status,
        email_messages_sent=email_messages_sent,
        cart_hold_result=cart_hold_result,
        resolved_campgrounds_state=resolved_campgrounds_state,
        skipped_reason=None,
    )

    if not config.dry_run and new_openings:
        if email_partially_configured(config):
            email_status = "email_partial_config_skipped"
            print("Gmail SMTP is only partially configured. Skipping email and logging only.")
        elif email_configured(config):
            send_gmail_email(report, new_openings, config, cart_hold_result)
            email_status = "sent"
            email_messages_sent = 1
            print("Gmail email sent.")
        else:
            email_status = "not_configured"
            print("Gmail SMTP is not configured. Skipping email and logging only.")
    elif not config.dry_run:
        email_status = "no_new_openings"

    report = build_run_report(
        config=config,
        current_openings=current_openings,
        new_openings=new_openings,
        sms_status=sms_status,
        sms_messages_sent=sms_messages_sent,
        email_status=email_status,
        email_messages_sent=email_messages_sent,
        cart_hold_result=cart_hold_result,
        resolved_campgrounds_state=resolved_campgrounds_state,
        skipped_reason=None,
    )
    write_json(config.report_path, report)
    write_text(config.summary_path, build_summary_markdown(report, new_openings))
    print(f"Run report written to {config.report_path}.")
    print(f"Run summary written to {config.summary_path}.")

    if not config.dry_run:
        save_state(config.state_path, build_state(current_openings))
        print(f"State saved to {config.state_path}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
