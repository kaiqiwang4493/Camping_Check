# NAS worker and DSM controls

This is the NAS-side executor, compatible with the DS418play's existing Python
3.8.12. It has no pip, Docker, browser, or cloud SDK dependencies.

The NAS polls the private cloud queue every two minutes. Both providers' availability
and campground searches execute on the NAS. Website settings are versioned in
Cloud Storage; changing them invalidates unfinished jobs using old settings.
Manual queries update results without sending mail or consuming notification state.
Scheduled queries create durable email records when new matching openings appear.
The current owner setting keeps scheduled queries paused; enable them in the website
when desired. Stopping the NAS in DSM also stops manual task pickup.

## Install

Run as the NAS user, not root:

```sh
/bin/python3 nas_worker/install.py --root /volume1/homes/kaiqiwang/camping-check
```

The installer verifies the copied worker's SHA-256, keeps the current and one
previous release, restricts directory/config permissions to the user, and sets
the persistent stop marker. It refuses to replace a running worker. Existing
configuration is preserved. No NAS packages or system services are modified.

The root contains `current`, `releases`, `config.json`, `state`, `work`, and
`logs`. Never commit `config.json` or a device token. The worker must receive a
revocable, device-scoped token through a private deployment file; no Gmail,
GitHub, or booking account credentials belong here.

## DSM Task Scheduler

All five tasks run as `kaiqiwang`. The common command prefix is:

```sh
/bin/python3 /volume1/homes/kaiqiwang/camping-check/current/worker.py --root /volume1/homes/kaiqiwang/camping-check
```

| Task | Last argument | Schedule |
| --- | --- | --- |
| Camping Start | `start` | Disabled schedule; select the task and click Run |
| Camping Stop | `stop` | Disabled schedule; select the task and click Run |
| Camping Status | `status` | Disabled schedule; select the task and click Run |
| Camping Boot | `watchdog` | Boot-up trigger |
| Camping Watchdog | `watchdog` | Every 10 minutes |

`stop` persists across NAS reboots. Both recovery tasks respect it. `start`
clears that marker only after validating an enabled configuration, and a file
lock prevents concurrent workers. An unconfigured Start reports an error and
leaves the stop marker in place. Never enable the schedules for the three manual
control tasks: that would schedule daily starts/stops rather than manual buttons.

DSM's global **Save output results** is disabled at the owner's request. No root
log-cleanup task was created. DSM's View Result still shows whether a command
completed, but it will not retain future detailed stdout. Application logs remain
bounded independently. A few fixed installation-test outputs already saved by
DSM are in `/volume1/docker/camping-check-results`; this does not install Docker
and no new output logs will accumulate there while saving is disabled.

## Resources and verification

- Idle task pickup is fixed at 120 seconds. This is separate from the cloud
  scan interval (default two hours, controlled by website settings).
- One job child at a time; it exits after every scan. Its parent handles leases
  every 45 seconds and enforces a ten-minute job budget.
- Responses stream in 64 KiB chunks with a 16 MiB per-response limit. The NAS
  executor does not parse provider JSON; the cloud validates it.
- Uploaded response files are removed after acknowledgement. Job folders are
  removed when the child ends. Orphans older than 15 minutes are reclaimed;
  total temporary work has a 64 MiB admission budget.
- Logs rotate at 5 MiB, with two backups (15 MiB total). No tokens or raw
  responses are logged. No memory-cache flushing or system swap changes.
- Only hardcoded Recreation.gov and ReserveCalifornia availability and catalog endpoints
  are used. Redirects and proxy inheritance are disabled; TLS remains verified.
- A 403 fails without retry. The cloud applies a 30-minute provider cooldown for 403/429 responses.

Test without importing the main Python 3.11+ project:

```sh
python3 -m unittest discover -s tests -p test_nas_worker.py -v
python3 nas_worker/probe.py
```

On the actual NAS, all 13 tests passed, including process start/stop, a repeated
start, stop/watchdog persistence, cleanup boundaries, quota rejection, 403
handling, upload acknowledgement cleanup, log rotation, configuration-preserving
updates, release retention, and refusal to replace a running worker. A read-only probe
of Oceano (618) and Upper Pines (232447) returned validated HTTP 200 responses;
the combined diagnostic's peak RSS was 25,188 KiB (about 24.6 MiB). This is a
small probe, not a measurement of a complete six-month scan or idle daemon.

## Worker protocol

Every request uses the device token in the Authorization header. The configured
API base must be an HTTPS origin.

| Endpoint | Contract |
| --- | --- |
| `POST /api/worker/claim` | 204 when idle; otherwise bounded JSON containing `job_id`, `attempt_id`, `requests` |
| `POST /api/worker/jobs/{id}/heartbeat` | Body contains `attempt_id`; reject expired/stale leases |
| `PUT /api/worker/jobs/{id}/parts/{sha256(key)}?attempt_id=...` | Raw JSON response streamed with Content-Length; verify expected key, size, identity and checksum; idempotent acknowledgements |
| `POST /api/worker/jobs/{id}/complete` | Body contains `attempt_id`; validate all parts and commit atomically and idempotently |
| `POST /api/worker/jobs/{id}/fail` | Attempt, provider, exception category and optional HTTP status; preserve the last successful report |

`requests` currently accepts the typed fields produced by `browser_scan_plan`:
`key`, `provider`, `campground`, and a validated month or ReserveCalifornia date
range. Raw supplied URLs are never used as destinations. Search jobs accept only
`search` for Recreation.gov and `places` / `facilities` for ReserveCalifornia.

## Cloud and notifications

`nas/control.json` in the existing private GCS bucket is updated using generation
preconditions. It contains configuration revisions, job leases and the notification
outbox. Provider payloads, reports and notification baselines are immutable objects.
Only complete, validated, current attempts can replace the latest successful report.
A failed query never turns an old result into a fresh empty result.

Jobs have 120-second leases and at most three attempts. Old attempts cannot upload
or commit after a lease is reclaimed. Scheduled scans use the configured interval;
manual scans do not postpone the next scheduled scan. Provider data is kept at most
seven days unless referenced by a current report, job or pending notification.
Maintenance prunes terminal job/mail records and unreferenced objects. This policy
applies only to this project's `nas/` prefixes, not unrelated bucket data.

GitHub Actions runs `nas-maintenance.yml` every ten minutes and on completion when
mail is waiting. It uses the existing Gmail secrets. A short-lived GitHub OIDC token
must match the immutable repository ID, main branch, workflow path, issuer, audience
and scheduled/manual event. NAS credentials cannot access email or user settings.
The cloud stores only the NAS token hash; rotate the device token to revoke access.

Mail is leased before sending and marked `sending` immediately before SMTP delivery.
Ambiguous delivery is retained as `unknown` and never automatically duplicated.
Definite failures get at most three attempts. Changing filters cancels unsent mail
for previous filters. SMTP acceptance confirms sending, not inbox placement.

Deployment requires `NAS_ENABLED=true`, `GITHUB_REPOSITORY_ID=1191031735`, and the
existing `BROWSER_REPORT_BUCKET` on Cloud Run. The GitHub variable `NAS_ENABLED=true`
disables the legacy cloud provider scan and enables the outbox sender. Device tokens
and Firebase user tokens have separate endpoints and permissions.

Run all cloud tests with `python3 -m unittest discover -s tests -p 'test_*.py'` and
build the UI with `npm --prefix web run build`. The standalone NAS tests also run
on its actual Python 3.8.12 interpreter. No booking account or shopping cart is used.

## Deployment verification — 2026-11-08 Pacific time

- Website and Cloud Run deployed; code `e499198`, Cloud Run revision
  `camping-check-api-00010-rw4`. All 91 project tests passed locally and in CI;
  the 13 standalone worker tests also passed on the NAS's Python 3.8.12.
- A manual query submitted through the authenticated website completed all four
  provider requests and returned 256 matching two-night windows across Upper Pines
  and Oceano. No availability email was sent and the old notification baseline was
  unchanged. The Oceano search completed all three catalog requests and returned
  three candidates. Results are a point-in-time observation, not reserved inventory.
- The existing mail configuration sent one clearly labelled test message through
  the OIDC-authenticated Actions workflow; the owner confirmed receipt.
- Sampled parent-plus-child RSS peaked at 48,996 KiB (47.8 MiB); idle RSS was
  24,608 KiB (24.0 MiB). This measures the current two-campground, two-month scan
  and a catalog search, not a guarantee for every future workload. The work
  directory returned to empty after both jobs; logs occupied 4 KiB at verification.
- Real Stop → Watchdog left the worker stopped; Start and a repeated Start created
  only one worker. Reboot was not performed. Existing DSM Boot/Watchdog tasks remain
  enabled and DSM output saving remains disabled.
- **Current preference: scheduled scans stay paused; manual queries only.** NAS
  stays running to claim manually submitted tasks every 120 seconds. The saved
  scan interval remains two hours for use if the owner later enables scheduling.
