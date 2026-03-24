# Yosemite Camping Monitor

This project polls Recreation.gov for campsite availability at Yosemite's `Upper Pines`, `Lower Pines`, and `North Pines` campgrounds and sends SMS alerts through ClickSend when new openings appear.

## What it does

- Scans all configured campgrounds every 5 minutes in GitHub Actions
- Checks the current month plus the next 5 months by default
- Alerts only on newly appeared availability
- Stores current active openings in a tracked state file
- Supports `workflow_dispatch` for manual runs
- Supports `DRY_RUN=true` for testing without sending SMS

## Campgrounds

- Upper Pines: `232447`
- North Pines: `232449`
- Lower Pines: `232450`

## Repository layout

- `src/yosemite_monitor/`: monitor implementation
- `tests/`: unit tests
- `.github/workflows/monitor.yml`: scheduled monitor workflow
- `.github/workflows/ci.yml`: test workflow
- `state/notified-openings.json`: persisted state used for dedupe

## GitHub setup

1. Create a GitHub repository and push this project.
2. Add these repository secrets:
   - `CLICKSEND_USERNAME`
   - `CLICKSEND_API_KEY`
   - `PHONE_TO`
3. Optionally add these repository variables or secrets:
   - `PHONE_FROM`
   - `SCAN_MONTHS`
   - `DRY_RUN`
4. Enable GitHub Actions for the repository.

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

## ClickSend note

ClickSend's official documentation notes that SMS messages containing URLs may be paused for new customers until approved. The monitor includes campground links in the outgoing SMS because that is part of the requested behavior, but if delivery is blocked you may need to remove links or contact ClickSend support.

Official docs used for the SMS API:

- [ClickSend SMS docs](https://developers.clicksend.com/docs/messaging/sms/other/view-inbound-sms)
