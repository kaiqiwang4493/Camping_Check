# NAS worker and DSM controls

This is the NAS-side executor, compatible with the DS418play's existing Python
3.8.12. It has no pip, Docker, browser, or cloud SDK dependencies.

**Deployment status:** the cloud job APIs, device registration, website job UI,
and cloud email processing described in the implementation plan are not yet
implemented. The NAS installation is deliberately stopped and unconfigured.
Installing this component alone does not enable scheduled campground checks or
email notifications. Existing production workflows have not been switched over.

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
revocable, device-scoped token after the backend protocol is ready; no Gmail,
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

- Idle task pickup is fixed at 120 seconds. This is separate from the future
  cloud scan interval (default two hours, controlled by website settings).
- One job child at a time; it exits after every scan. Its parent handles leases
  every 45 seconds and enforces a ten-minute job budget.
- Responses stream in 64 KiB chunks with a 16 MiB per-response limit. The NAS
  executor does not parse provider JSON; the cloud must validate it.
- Uploaded response files are removed after acknowledgement. Job folders are
  removed when the child ends. Orphans older than 15 minutes are reclaimed;
  total temporary work has a 64 MiB admission budget.
- Logs rotate at 5 MiB, with two backups (15 MiB total). No tokens or raw
  responses are logged. No memory-cache flushing or system swap changes.
- Only hardcoded Recreation.gov and ReserveCalifornia availability endpoints
  are used. Redirects and proxy inheritance are disabled; TLS remains verified.
- A 403 fails without retry. Cloud-side provider cooldown is still required.

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

## Worker protocol to implement on the backend

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
range. Raw supplied URLs are never used as destinations. Search/catalog jobs are
not implemented in this version. Cloud and NAS integration tests are required
before enabling the configuration and cutting over the website or Actions.
