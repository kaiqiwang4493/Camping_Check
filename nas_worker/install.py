#!/usr/bin/env python3
"""Install a versioned, stopped worker under the current user's NAS directory."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil


def install(source, root):
    os.umask(0o077)
    source = source.resolve()
    root = root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(str(root), 0o700)
    for name in ("state", "logs", "work", "releases"):
        (root / name).mkdir(mode=0o700, exist_ok=True)
    with (root / "state/control.lock").open("a+") as control_lock, (root / "state/run.lock").open("a+") as run_lock:
        fcntl.flock(control_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        content = (source / "worker.py").read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        release = root / "releases" / digest[:16]
        release.mkdir(mode=0o700, exist_ok=True)
        destination = release / "worker.py"
        destination.write_bytes(content)
        os.chmod(str(destination), 0o600)
        (release / "SHA256SUMS").write_text(digest + "  worker.py\n", encoding="utf-8")
        if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Installed file checksum mismatch")
        staged = root / "current.new"
        if staged.is_symlink():
            staged.unlink()
        staged.symlink_to(release.relative_to(root), target_is_directory=True)
        staged.replace(root / "current")
        config = root / "config.json"
        if not config.exists():
            config.write_text(json.dumps({"protocol_version": 1, "enabled": False,
                                         "api_base": "https://camping-check-3cad5.web.app",
                                         "token": ""}, indent=2) + "\n", encoding="utf-8")
        os.chmod(str(config), 0o600)
        (root / "state/STOPPED").touch(mode=0o600)
        releases = sorted((p for p in (root / "releases").iterdir()
                           if p.is_dir() and not p.is_symlink() and len(p.name) == 16
                           and all(c in "0123456789abcdef" for c in p.name)),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        keep = {release}
        keep.update([p for p in releases if p != release][:1])
        for old in releases:
            if old not in keep:
                shutil.rmtree(str(old))
    print(json.dumps({"installed": str(root), "sha256": digest, "state": "stopped"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    install(Path(__file__).parent, args.root)
