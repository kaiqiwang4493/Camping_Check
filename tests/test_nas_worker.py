"""Run with Python 3.8 on the NAS too; no project dependencies are imported."""
import importlib.util
import contextlib
import fcntl
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

WORKER = Path(__file__).resolve().parents[1] / "nas_worker/worker.py"
spec = importlib.util.spec_from_file_location("nas_worker", WORKER)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
install_spec = importlib.util.spec_from_file_location("nas_install", WORKER.parent / "install.py")
installer = importlib.util.module_from_spec(install_spec)
install_spec.loader.exec_module(installer)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        worker.setup(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def config(self, enabled=True):
        worker.atomic_json(self.root / "config.json", {"protocol_version": 1, "enabled": enabled,
                          "api_base": "https://127.0.0.1:9", "token": "test-only-" * 5})

    def command(self, action):
        return subprocess.run([sys.executable, str(WORKER), "--root", str(self.root), action],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=50)

    def test_unconfigured_start_keeps_stop_marker(self):
        self.config(False)
        (self.root / "state/STOPPED").touch()
        self.assertNotEqual(self.command("start").returncode, 0)
        self.assertTrue((self.root / "state/STOPPED").exists())
        self.assertFalse(worker.running(self.root))

    def test_start_is_single_instance_and_stop_survives_watchdog(self):
        self.config()
        try:
            self.assertEqual(self.command("start").returncode, 0)
            state1 = json.loads(self.command("status").stdout)
            self.assertTrue(state1["running"])
            self.assertEqual(self.command("start").returncode, 0)
            state2 = json.loads(self.command("status").stdout)
            self.assertEqual(state1.get("pid"), state2.get("pid"))
            self.assertEqual(self.command("stop").returncode, 0)
            self.assertEqual(self.command("watchdog").returncode, 0)
            self.assertFalse(worker.running(self.root))
            self.assertTrue((self.root / "state/STOPPED").exists())
        finally:
            self.command("stop")

    def test_cleanup_removes_old_orphans_not_active_or_symlinks(self):
        old = self.root / "work/job-old"
        active = self.root / "work/job-active"
        for directory in (old, active):
            directory.mkdir()
            (directory / "response.json").write_bytes(b"data")
            os.utime(str(directory), (1, 1))
        (self.root / "work/job-link").symlink_to(self.root, target_is_directory=True)
        worker.cleanup(self.root, active=active, now=2000)
        self.assertFalse(old.exists())
        self.assertTrue(active.exists())
        self.assertTrue((self.root / "work/job-link").is_symlink())

    def test_disk_quota_rejects_more_work(self):
        directory = self.root / "work/job-large"
        directory.mkdir()
        with (directory / "response.json").open("wb") as handle:
            handle.truncate(worker.MAX_WORK + 1)
        with self.assertRaises(ValueError):
            worker.cleanup(self.root)

    def rc(self):
        return {"provider": "ReserveCalifornia", "campground": {"campground_id": "618"},
                "url": "https://attacker.invalid/collect", "body": {"FacilityId": 618,
                "StartDate": "2026-11-17", "EndDate": "2026-11-19"}}

    def test_request_is_reconstructed_and_arbitrary_url_is_ignored(self):
        request = worker.provider_request(self.rc())
        self.assertEqual(request.full_url, worker.RC_ENDPOINT)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(json.loads(request.data)["FacilityId"], 618)

    def test_invalid_provider_id_or_date_is_rejected(self):
        for changes in ({"provider": "Shell"}, {"campground": {"campground_id": "../../etc/passwd"}},
                        {"body": {"FacilityId": 618, "StartDate": "2026-11-19", "EndDate": "2026-11-17"}}):
            item = self.rc()
            item.update(changes)
            with self.assertRaises(ValueError):
                worker.provider_request(item)

    def test_403_is_not_retried(self):
        error = HTTPError(worker.RC_ENDPOINT, 403, "Forbidden", {}, None)
        with patch.object(worker, "opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaises(HTTPError):
                worker.download(self.rc(), self.root / "response.json")
            self.assertEqual(factory.return_value.open.call_count, 1)

    def test_stream_limit_rejects_oversized_body(self):
        response = io.BytesIO(b"x" * 65)
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        with patch.object(worker, "MAX_PART", 64), patch.object(worker, "opener") as factory:
            factory.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                worker.download(self.rc(), self.root / "response.json")

    def test_logs_rotate_and_stay_bounded(self):
        log = worker.configure_logging(self.root)
        try:
            for _ in range(20):
                log.info("x" * 1024 * 1024)
            files = list((self.root / "logs").glob("worker.log*"))
            self.assertLessEqual(len(files), 3)
            self.assertLessEqual(sum(p.stat().st_size for p in files), 15 * 1024 * 1024)
        finally:
            for handler in list(log.handlers):
                handler.close()
                log.removeHandler(handler)

    def test_redirects_are_rejected(self):
        with self.assertRaises(ValueError):
            worker.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "http://localhost/")

    def test_task_upload_is_streamed_and_file_deleted_after_ack(self):
        self.config()
        directory = self.root / "work/job-test"
        directory.mkdir()
        job_file = directory / "job.json"
        item = dict(self.rc(), key="ReserveCalifornia|618|2026-11-01")
        worker.atomic_json(job_file, {"job_id": "job1", "attempt_id": "attempt1", "requests": [item]})
        observed = []

        def download(item, destination):
            destination.write_bytes(b'{"Facility":{"FacilityId":618}}')

        def api(config, path, body=None, **kwargs):
            if "parts/" in path:
                self.assertTrue(kwargs["file_path"].exists())
                self.assertEqual(kwargs["method"], "PUT")
                observed.append("part")
            elif path.endswith("/complete"):
                self.assertFalse((directory / "response.json").exists())
                observed.append("complete")

        with patch.object(worker, "download", side_effect=download), patch.object(worker, "api", side_effect=api):
            worker.execute(self.root, job_file)
        self.assertEqual(observed, ["part", "complete"])

    def test_installer_preserves_configuration_and_keeps_two_releases(self):
        source = self.root / "source"
        source.mkdir()
        root = self.root / "installed"
        with contextlib.redirect_stdout(io.StringIO()):
            for revision in range(3):
                (source / "worker.py").write_text("# release " + str(revision))
                installer.install(source, root)
                if revision == 0:
                    worker.atomic_json(root / "config.json", {"keep": "existing configuration"})
        self.assertEqual(worker.bounded_json(root / "config.json"), {"keep": "existing configuration"})
        self.assertEqual(len(list((root / "releases").iterdir())), 2)
        self.assertEqual((root / "current/worker.py").read_text(), "# release 2")
        self.assertTrue((root / "state/STOPPED").exists())

    def test_installer_refuses_to_replace_running_worker(self):
        with (self.root / "state/run.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                installer.install(WORKER.parent, self.root)
        self.assertFalse((self.root / "current").exists())


if __name__ == "__main__":
    unittest.main()
