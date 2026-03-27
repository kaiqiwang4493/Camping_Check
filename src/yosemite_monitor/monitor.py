from __future__ import annotations

import base64
import json
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

from camply.containers.data_containers import SearchWindow
from camply.search.search_usedirect import SearchReserveCalifornia
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


RECREATION_API_BASE = "https://www.recreation.gov/api/camps/availability/campground"
CLICKSEND_SMS_URL = "https://rest.clicksend.com/v3/sms/send"
DEFAULT_SCAN_MONTHS = 6
DEFAULT_MORRO_BAY_SCAN_MONTHS = 1
DEFAULT_STATE_PATH = Path("state/notified-openings.json")

RECREATION_GOV_CAMPGROUNDS = (
    {
        "park_name": "Yosemite National Park",
        "campground_name": "Upper Pines",
        "campground_id": "232447",
    },
    {
        "park_name": "Yosemite National Park",
        "campground_name": "North Pines",
        "campground_id": "232449",
    },
    {
        "park_name": "Yosemite National Park",
        "campground_name": "Lower Pines",
        "campground_id": "232450",
    },
)

RESERVE_CALIFORNIA_CAMPGROUNDS = (
    {
        "park_name": "Morro Bay SP",
        "park_id": 680,
        "campground_name": "Upper Section",
        "campground_id": 583,
    },
)

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "date", normalize_booking_date(self.date))

    @property
    def key(self) -> str:
        return f"{self.provider}|{self.campground_id}|{self.site}|{self.date}"

    @property
    def day_name(self) -> str:
        opening_date = date.fromisoformat(self.date)
        return opening_date.strftime("%A")

    @property
    def day_type(self) -> str:
        opening_date = date.fromisoformat(self.date)
        return "Weekend" if opening_date.weekday() >= 4 else "Weekday"


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


def load_config() -> Config:
    scan_months_raw = os.getenv("YOSEMITE_SCAN_MONTHS", "").strip() or str(DEFAULT_SCAN_MONTHS)
    morro_bay_scan_months_raw = os.getenv("MORRO_BAY_SCAN_MONTHS", "").strip() or str(
        DEFAULT_MORRO_BAY_SCAN_MONTHS
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
        state_path=Path(os.getenv("STATE_PATH", str(DEFAULT_STATE_PATH))),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        report_path=Path(os.getenv("REPORT_PATH", "state/run-report.json")),
        summary_path=Path(os.getenv("SUMMARY_PATH", "state/run-summary.md")),
    )


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
    for campsite in payload.get("campsites", {}).values():
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
        )
        for item in openings
    ]


def end_date_for_scan(today: date, scan_months: int) -> date:
    return month_starts(today, scan_months + 1)[-1]


def collect_reserve_california_openings(config: Config, today: date) -> list[Opening]:
    scan_end = end_date_for_scan(today, config.morro_bay_scan_months)
    search_window = SearchWindow(start_date=today, end_date=scan_end)
    openings: list[Opening] = []

    for campground in RESERVE_CALIFORNIA_CAMPGROUNDS:
        search = SearchReserveCalifornia(
            search_window=search_window,
            recreation_area=[campground["park_id"]],
            campgrounds=[campground["campground_id"]],
            nights=1,
        )
        for campsite in search.get_matching_campsites(search_once=True, log=False):
            openings.append(
                Opening(
                    park_name=str(campsite.recreation_area or campground["park_name"]),
                    campground_name=str(campsite.facility_name or campground["campground_name"]),
                    campground_id=str(campsite.facility_id or campground["campground_id"]),
                    provider="ReserveCalifornia",
                    site=str(campsite.campsite_site_name),
                    date=str(campsite.booking_date),
                    url=str(campsite.booking_url),
                )
            )

    return openings


def collect_openings(config: Config, today: date | None = None) -> list[Opening]:
    scan_from = today or date.today()
    openings: list[Opening] = []

    for campground in RECREATION_GOV_CAMPGROUNDS:
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

    return sorted(openings, key=lambda item: (item.date, item.park_name, item.campground_name, item.site))


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "active_openings": {}, "updated_at": None}

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_state(openings: Iterable[Opening]) -> dict:
    opening_map = {
        opening.key: {
            "campground_name": opening.campground_name,
            "campground_id": opening.campground_id,
            "site": opening.site,
            "date": opening.date,
            "url": opening.url,
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
        f"{opening.date} {opening.day_name} ({opening.day_type}) {opening.url}"
    )


def chunk_messages(openings: Iterable[Opening], max_chars: int = 320) -> list[str]:
    sorted_openings = sorted(openings, key=lambda item: (item.park_name, item.campground_name, item.date, item.site))
    if not sorted_openings:
        return []

    header = "Yosemite openings:"
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


def build_email_subject(new_openings: list[Opening]) -> str:
    return f"Yosemite camping availability found: {len(new_openings)} new opening(s)"


def build_email_body(report: dict, new_openings: list[Opening]) -> str:
    lines = [
        "Yosemite Camping Monitor",
        "",
        f"Generated at (UTC): {report['generated_at']}",
        f"Scan window: current month + next {report['scan_months'] - 1} month(s)",
        f"Current openings found: {report['current_openings_count']}",
        f"New openings found: {report['new_openings_count']}",
        "",
    ]
    if new_openings:
        lines.extend(["New openings:", ""])
        for opening in new_openings:
            lines.append(
                f"- {opening.campground_name} | site {opening.site} | "
                f"{opening.date} | {opening.day_name} | {opening.day_type} | {opening.url}"
            )
    else:
        lines.append("No new openings in this run.")
    lines.append("")
    return "\n".join(lines)


def send_gmail_email(report: dict, new_openings: list[Opening], config: Config) -> None:
    message = EmailMessage()
    message["Subject"] = build_email_subject(new_openings)
    message["From"] = config.email_from or config.gmail_smtp_user
    message["To"] = config.email_to
    message.set_content(build_email_body(report, new_openings))

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
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_months": config.scan_months,
        "dry_run": config.dry_run,
        "clicksend_configured": clicksend_configured(config),
        "clicksend_partially_configured": clicksend_partially_configured(config),
        "email_configured": email_configured(config),
        "email_partially_configured": email_partially_configured(config),
        "sms_status": sms_status,
        "sms_messages_sent": sms_messages_sent,
        "email_status": email_status,
        "email_messages_sent": email_messages_sent,
        "current_openings_count": len(current_openings),
        "new_openings_count": len(new_openings),
        "new_openings": [
            {
                "campground_name": opening.campground_name,
                "site": opening.site,
                "date": opening.date,
                "day_name": opening.day_name,
                "day_type": opening.day_type,
                "url": opening.url,
            }
            for opening in new_openings
        ],
    }


def build_summary_markdown(report: dict, new_openings: list[Opening]) -> str:
    lines = [
        "## Yosemite Camping Monitor",
        "",
        f"- Generated at (UTC): `{report['generated_at']}`",
        f"- Scan window: current month + next `{report['scan_months'] - 1}` month(s)",
        f"- Current openings found: `{report['current_openings_count']}`",
        f"- New openings found: `{report['new_openings_count']}`",
        f"- SMS status: `{report['sms_status']}`",
        f"- ClickSend configured: `{report['clicksend_configured']}`",
        f"- Email status: `{report['email_status']}`",
        f"- Email configured: `{report['email_configured']}`",
        f"- Dry run: `{report['dry_run']}`",
        "",
    ]
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
    if new_openings:
        lines.extend(
            [
                "### New openings",
                "",
                "| Park | Campground | Site | Date | Day | Day Type | Link |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for opening in new_openings:
            lines.append(
                f"| {opening.park_name} | {opening.campground_name} | {opening.site} | {opening.date} | "
                f"{opening.day_name} | "
                f"{opening.day_type} | "
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
    current_openings = collect_openings(config)
    previous_state = load_state(config.state_path)
    new_openings = diff_new_openings(current_openings, previous_state)

    log_openings("Current openings", current_openings)
    log_openings("New openings", new_openings)

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
    )

    if not config.dry_run and new_openings:
        if email_partially_configured(config):
            email_status = "email_partial_config_skipped"
            print("Gmail SMTP is only partially configured. Skipping email and logging only.")
        elif email_configured(config):
            send_gmail_email(report, new_openings, config)
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
