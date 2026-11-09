#!/usr/bin/env python3
"""Small Python 3.8 NAS executor. No third party dependencies or provider login.

The cloud worker protocol must be deployed before configuring a device token.
Run `worker.py --root /path/to/install status|start|stop|watchdog|run`.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener, HTTPSHandler

VERSION = "0.1.0"
POLL_SECONDS = 120
CHUNK = 64 * 1024
MAX_PART = 16 * 1024 * 1024
MAX_WORK = 64 * 1024 * 1024
MAX_CONTROL = 256 * 1024
JOB_SECONDS = 600
RC_ENDPOINT = "https://california-rdr.prod.cali.rd12.recreation-management.tylerapp.com/rdr/search/grid"
REC_ENDPOINT = "https://www.recreation.gov/api/camps/availability/campground/"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("HTTP redirects are disabled")


def opener():
    # No proxy inheritance, cookies, credential forwarding, or TLS bypass.
    return build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(str(temporary), 0o600)
    os.replace(str(temporary), str(path))


def bounded_json(path):
    with path.open("rb") as handle:
        raw = handle.read(MAX_CONTROL + 1)
    if len(raw) > MAX_CONTROL:
        raise ValueError("Control document is too large")
    return json.loads(raw)


def setup(root):
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True)
    for name in ("state", "logs", "work"):
        (root / name).mkdir(mode=0o700, exist_ok=True)


def cleanup(root, active=None, now=None):
    """Remove only this program's orphan work directories; never follow symlinks."""
    now = time.time() if now is None else now
    total = 0
    for directory in (root / "work").iterdir():
        if directory.is_symlink() or not directory.is_dir() or not directory.name.startswith("job-"):
            continue
        if directory != active and now - directory.stat().st_mtime > 900:
            shutil.rmtree(str(directory))
            continue
        total += sum(p.stat().st_size for p in directory.iterdir() if p.is_file() and not p.is_symlink())
    if total > MAX_WORK:
        raise ValueError("Work directory exceeds 64 MiB; refusing another job")
    return total


def read_config(root):
    config = bounded_json(root / "config.json")
    if config.get("protocol_version") != 1 or not config.get("enabled"):
        raise ValueError("Cloud worker integration is not enabled; installation is in standby")
    base = config.get("api_base", "")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("api_base must be an HTTPS origin")
    if not isinstance(config.get("token"), str) or len(config["token"]) < 32:
        raise ValueError("Device token is missing")
    config["api_base"] = base.rstrip("/")
    return config


def api(config, path, body=None, method="POST", file_path=None):
    if not path.startswith("/api/worker/") or ".." in path:
        raise ValueError("Invalid worker API path")
    headers = {"Authorization": "Bearer " + config["token"], "Content-Type": "application/json",
               "User-Agent": "camping-nas/" + VERSION}
    data = json.dumps(body or {}).encode("utf-8")
    handle = None
    try:
        if file_path is not None:
            headers["Content-Length"] = str(file_path.stat().st_size)
            handle = file_path.open("rb")
            data = iter(lambda: handle.read(CHUNK), b"")
        request = Request(config["api_base"] + path, data=data, headers=headers, method=method)
        with opener().open(request, timeout=30) as response:
            if response.status == 204:
                return None
            raw = response.read(MAX_CONTROL + 1)
            if len(raw) > MAX_CONTROL:
                raise ValueError("API response is too large")
            return json.loads(raw) if raw else {}
    finally:
        if handle is not None:
            handle.close()


def provider_request(item):
    """Reconstruct requests from typed fields; never execute a cloud-supplied URL."""
    provider = item["provider"]
    campground_id = str(item["campground"]["campground_id"])
    if not re.fullmatch(r"[0-9]{1,12}", campground_id):
        raise ValueError("Invalid campground ID")
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 CampingCheck/" + VERSION}
    if provider == "Recreation.gov":
        # Accept the current plan format, but use only its validated date.
        from urllib.parse import parse_qs
        raw = parse_qs(urlsplit(item["url"]).query).get("start_date", [""])[0]
        month = datetime.date.fromisoformat(raw[:10])
        if month.day != 1 or raw != month.isoformat() + "T00:00:00.000Z":
            raise ValueError("Invalid Recreation.gov month")
        url = REC_ENDPOINT + campground_id + "/month?" + urlencode({"start_date": raw})
        return Request(url, headers=headers)
    if provider == "ReserveCalifornia":
        incoming = item["body"]
        start = datetime.date.fromisoformat(incoming["StartDate"])
        end = datetime.date.fromisoformat(incoming["EndDate"])
        if not 0 <= (end - start).days <= 31 or int(incoming["FacilityId"]) != int(campground_id):
            raise ValueError("Invalid ReserveCalifornia range")
        body = {"FacilityId": int(campground_id), "StartDate": start.isoformat(),
                "EndDate": end.isoformat(), "WebOnly": True, "InSeasonOnly": True, "UnitSort": "orderby"}
        headers.update({"Content-Type": "application/json", "Origin": "https://www.reservecalifornia.com",
                        "Referer": "https://www.reservecalifornia.com/"})
        return Request(RC_ENDPOINT, data=json.dumps(body).encode(), headers=headers, method="POST")
    raise ValueError("Unsupported provider")


def download(item, destination):
    request = provider_request(item)
    for attempt in range(3):
        try:
            with opener().open(request, timeout=30) as response, destination.open("wb") as output:
                if response.status != 200:
                    raise ValueError("Provider returned an unexpected status")
                content_type = response.headers.get("Content-Type", "")
                if "json" not in content_type.lower():
                    raise ValueError("Provider returned non-JSON content")
                length = int(response.headers.get("Content-Length", "0"))
                if length > MAX_PART:
                    raise ValueError("Provider response exceeds 16 MiB")
                count = 0
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        return count
                    count += len(chunk)
                    if count > MAX_PART:
                        raise ValueError("Provider response exceeds 16 MiB")
                    output.write(chunk)
        except HTTPError as error:
            # A 403 is not retried. The cloud records a provider cooldown.
            if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            retry_after = error.headers.get("Retry-After", "")
            # A large Retry-After must not be shortened into an early retry.
            if retry_after.isdigit() and int(retry_after) > 60:
                raise
            time.sleep(max(2 ** (attempt + 1), int(retry_after) if retry_after.isdigit() else 0))


def job_path(job):
    for field in ("job_id", "attempt_id"):
        if not SAFE_ID.fullmatch(str(job.get(field, ""))):
            raise ValueError("Invalid job identity")
    return "/api/worker/jobs/" + job["job_id"]


def execute(root, job_file):
    config = read_config(root)
    job = bounded_json(job_file)
    base = job_path(job)
    identity = {"attempt_id": job["attempt_id"]}
    requests = job["requests"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= 200:
        raise ValueError("Invalid number of requests")
    parent = os.getppid()
    directory = job_file.parent
    provider = None
    # Bound the child even if its supervising process dies mid-response.
    signal.alarm(JOB_SECONDS)
    try:
        for index, item in enumerate(requests):
            if os.getppid() != parent or (root / "state/STOPPED").exists():
                raise RuntimeError("Task stopped")
            provider = item["provider"]
            part = directory / "response.json"
            download(item, part)
            # The hash identifies a planned request without putting raw keys in URLs.
            key = hashlib.sha256(item["key"].encode()).hexdigest()
            path = base + "/parts/" + key + "?attempt_id=" + job["attempt_id"]
            for attempt in range(3):
                try:
                    api(config, path, method="PUT", file_path=part)
                    break
                except HTTPError as error:
                    if error.code < 500 or attempt == 2:
                        raise
                    time.sleep(2 ** (attempt + 1))
            part.unlink()  # Reclaim immediately after the server acknowledges the part.
            if index + 1 < len(requests):
                time.sleep(1.1)
        # Completion must be idempotent server-side; response loss does not consume a second alert.
        api(config, base + "/complete", identity)
    except Exception as error:
        failure = dict(identity, provider=provider, error=type(error).__name__,
                       status_code=getattr(error, "code", None))
        try:
            api(config, base + "/fail", failure)
        except Exception:
            pass  # Lease expiry handles an unreachable backend.
        raise
    finally:
        signal.alarm(0)


def running(root):
    with (root / "state/run.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def configure_logging(root):
    log = logging.getLogger("camping-nas")
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(str(root / "logs/worker.log"), maxBytes=5 * 1024 * 1024,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    return log


def serve(root):
    lock = (root / "state/run.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    log = configure_logging(root)
    exiting = [False]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: exiting.__setitem__(0, True))
    status_file = root / "state/status.json"
    state = {"version": VERSION, "pid": os.getpid(), "started_at": time.time(), "phase": "starting"}
    config = read_config(root)
    child = None
    directory = None
    delay = POLL_SECONDS
    log.info("Worker started; task polling interval=%s seconds", POLL_SECONDS)
    try:
        while not exiting[0] and not (root / "state/STOPPED").exists():
            cleanup(root)
            try:
                job = api(config, "/api/worker/claim", {"version": VERSION})
                state.update(last_contact_at=time.time(), last_error=None, phase="idle", job_id=None)
                atomic_json(status_file, state)
                delay = POLL_SECONDS
                if job:
                    if cleanup(root) + MAX_PART + MAX_CONTROL > MAX_WORK:
                        raise ValueError("Not enough room in the 64 MiB work budget")
                    base = job_path(job)
                    directory = root / "work" / ("job-" + job["job_id"] + "-" + job["attempt_id"])
                    directory.mkdir(mode=0o700)
                    job_file = directory / "job.json"
                    atomic_json(job_file, job)
                    state.update(phase="running", job_id=job["job_id"])
                    atomic_json(status_file, state)
                    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                              "--root", str(root), "execute", str(job_file)],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, close_fds=True)
                    deadline = time.monotonic() + JOB_SECONDS
                    heartbeat = time.monotonic() + 45
                    while child.poll() is None:
                        if exiting[0] or (root / "state/STOPPED").exists() or time.monotonic() > deadline:
                            child.terminate()
                            child.wait(timeout=5)
                            break
                        if time.monotonic() >= heartbeat:
                            api(config, base + "/heartbeat", {"attempt_id": job["attempt_id"]})
                            state["last_contact_at"] = time.time()
                            atomic_json(status_file, state)
                            heartbeat = time.monotonic() + 45
                        time.sleep(0.5)
                    state.update(phase="idle" if child.returncode == 0 else "error", job_id=None,
                                 last_exit_code=child.returncode,
                                 last_error=None if child.returncode == 0 else "JobFailed")
                    log.info("Job finished, exit_code=%s", child.returncode)
            except Exception as error:
                # Do not log tokens, response bodies, URLs, or downloaded provider data.
                state.update(last_error=type(error).__name__, phase="error")
                log.warning("Worker operation failed: %s", type(error).__name__)
                delay = min(300, max(POLL_SECONDS, delay * 2))
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                child = None
                if directory is not None and directory.exists():
                    shutil.rmtree(str(directory))
                directory = None
                state["job_id"] = None
                atomic_json(status_file, state)
            until = time.monotonic() + delay
            while time.monotonic() < until and not exiting[0] and not (root / "state/STOPPED").exists():
                time.sleep(0.5)
    finally:
        state.update(phase="stopped", pid=None, job_id=None)
        atomic_json(status_file, state)
        cleanup(root)
        log.info("Worker stopped")
        lock.close()


def control(root, action):
    setup(root)
    stopped = root / "state/STOPPED"
    if not running(root):
        cleanup(root)
    if action == "status":
        state = bounded_json(root / "state/status.json") if (root / "state/status.json").exists() else {}
        state.update(version=VERSION, running=running(root), stopped=stopped.exists(), poll_seconds=POLL_SECONDS)
        try:
            read_config(root)
            state["configured"] = True
        except (ValueError, OSError):
            state["configured"] = False
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    with (root / "state/control.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if action == "stop":
            stopped.touch(mode=0o600)
            for _ in range(90):
                if not running(root):
                    print("Stopped. Automatic restart remains disabled until Start is run.")
                    return
                time.sleep(0.5)
            raise RuntimeError("Stop requested; worker has not exited within 45 seconds")
        if action == "watchdog" and stopped.exists():
            print("Stopped by user; no restart.")
            return
        read_config(root)  # Fail closed before removing the persistent stop marker.
        if stopped.exists():
            stopped.unlink()
        if running(root):
            print("Already running.")
            return
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "run"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        for _ in range(20):
            if running(root):
                print("Started.")
                return
            time.sleep(0.1)
        raise RuntimeError("Worker did not start; check configuration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("command", choices=["start", "stop", "status", "watchdog", "run", "execute"])
    parser.add_argument("job_file", nargs="?", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    setup(root)
    try:
        if args.command == "run":
            serve(root)
        elif args.command == "execute":
            execute(root, args.job_file)
        else:
            control(root, args.command)
        return 0
    except Exception as error:
        print(type(error).__name__ + ": " + str(error) if isinstance(error, (ValueError, RuntimeError))
              else type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
