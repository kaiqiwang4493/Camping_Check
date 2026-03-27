# Yosemite Camping Monitor

This project polls Recreation.gov and ReserveCalifornia for campsite availability at Yosemite's `Upper Pines`, `Lower Pines`, `North Pines`, and Morro Bay SP's `Upper Section`, records each run inside GitHub, and can optionally send alerts through ClickSend SMS and Gmail SMTP email when new openings appear.

## What it does

- Scans all configured campgrounds every 5 minutes in GitHub Actions
- Checks the current month plus the next 5 months by default
- Alerts only on newly appeared availability
- Stores current active openings in a tracked state file
- Writes a run summary that shows up in each GitHub Actions run
- Can optionally append each run to a fixed GitHub Issue as a log thread
- Can optionally send Gmail SMTP email when new openings appear
- Supports `workflow_dispatch` for manual runs
- Supports `DRY_RUN=true` for testing without sending notifications or updating state

## Campgrounds

- Upper Pines: `232447`
- North Pines: `232449`
- Lower Pines: `232450`
- Morro Bay SP - Upper Section: `park #680`, `campground #583`

## Repository layout

- `src/yosemite_monitor/`: monitor implementation
- `tests/`: unit tests
- `.github/workflows/monitor.yml`: scheduled monitor workflow
- `.github/workflows/ci.yml`: test workflow
- `state/notified-openings.json`: persisted state used for dedupe

## GitHub setup

1. Create a GitHub repository and push this project.
2. If you want SMS alerts, add these repository secrets:
   - `CLICKSEND_USERNAME`
   - `CLICKSEND_API_KEY`
   - `PHONE_TO`
3. If you want Gmail email alerts, add these repository secrets:
   - `GMAIL_SMTP_USER`
   - `GMAIL_SMTP_APP_PASSWORD`
   - `EMAIL_TO`
   - optional `EMAIL_FROM`
4. Optionally add these repository variables or secrets:
   - `PHONE_FROM`
   - `YOSEMITE_SCAN_MONTHS`
   - `MORRO_BAY_SCAN_MONTHS`
   - `DRY_RUN`
   - `LOG_ISSUE_NUMBER`
5. Enable GitHub Actions for the repository.

If you do not configure ClickSend or Gmail secrets, the workflow still runs and writes results to GitHub without sending notifications.

## Local usage

Install the package:

```bash
python3 -m pip install -e .
```

Run the monitor:

```bash
python3 -m yosemite_monitor
```

Dry-run mode:

```bash
DRY_RUN=true python3 -m yosemite_monitor
```

## Behavior notes

- Recreation.gov requests are sent with browser-like headers because the API may reject generic clients.
- The monitor stores only the openings that are active in the most recent successful run. If an opening disappears and later returns, it will alert again.
- If a run fails before finishing, the state file is not updated to avoid suppressing future alerts.
- Each successful run writes:
  - `state/run-report.json`
  - `state/run-summary.md`
- The GitHub workflow publishes the Markdown summary to the Actions run page.
- If `LOG_ISSUE_NUMBER` is configured, the workflow posts a comment to that issue only when new openings are found.
- Results now include a leftmost `Park` column so Yosemite and Morro Bay openings are easy to distinguish.
- Issue comments and summaries include both the day name and the `Weekend`/`Weekday` classification.

## Gmail note

- Gmail SMTP uses `smtp.gmail.com` on port `587` with STARTTLS.
- Use a Google App Password for `GMAIL_SMTP_APP_PASSWORD`.
- Email is sent only when new openings are found.
- By default Yosemite scans 6 months and Morro Bay scans 1 month.

## ClickSend note

ClickSend's official documentation notes that SMS messages containing URLs may be paused for new customers until approved. The monitor includes campground links in the outgoing SMS because that is part of the requested behavior, but if delivery is blocked you may need to remove links or contact ClickSend support.

Official docs used for the SMS API:

- [ClickSend SMS docs](https://developers.clicksend.com/docs/messaging/sms/other/view-inbound-sms)
