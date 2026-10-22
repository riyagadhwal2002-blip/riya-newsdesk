"""Puts the JPEG at a public URL so Instagram can fetch it (Instagram requires image_url)."""
from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from pathlib import Path

import requests


class HostingError(Exception):
    pass


def _git(*args: str, cwd: Path) -> None:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise HostingError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")


def push_to_git(dest: Path) -> None:
    """GitHub mode: commit the image and push so raw.githubusercontent.com serves it."""
    repo = dest.parent
    while not (repo / ".git").exists() and repo != repo.parent:
        repo = repo.parent
    _git("add", str(dest), cwd=repo)
    _git("commit", "-m", f"post image {dest.name}", cwd=repo)
    for attempt in range(3):
        try:
            _git("push", cwd=repo)
            return
        except HostingError:
            if attempt == 2:
                raise
            _git("pull", "--rebase", cwd=repo)


def publish_image(local_path: Path, public_dir: Path, base_url: str, verify: bool = True,
                  git_push: bool = False, sleep=time.sleep) -> str:
    if not base_url:
        raise HostingError("PUBLIC_BASE_URL is not set — Instagram needs a public https URL for the image")
    name = f"{time.strftime('%Y%m%d')}-{secrets.token_hex(8)}.jpg"  # unguessable file name
    Path(public_dir).mkdir(parents=True, exist_ok=True)
    dest = Path(public_dir) / name
    shutil.copyfile(local_path, dest)
    if git_push:
        push_to_git(dest)
    url = f"{base_url}/{name}"
    if verify:
        last = ""
        for _ in range(12):  # a freshly pushed file can take a moment to appear
            try:
                r = requests.head(url, timeout=10, allow_redirects=True)
                ctype = r.headers.get("Content-Type", "")
                if r.status_code == 200 and "image/jpeg" in ctype:
                    return url
                last = f"HTTP {r.status_code}, Content-Type {ctype!r}"
            except requests.RequestException as exc:
                last = type(exc).__name__
            sleep(10)
        raise HostingError(f"public URL check failed: {last}")
    return url


def cleanup(public_dir: Path, older_than_days: int = 7) -> int:
    cutoff = time.time() - older_than_days * 86400
    removed = 0
    for f in Path(public_dir).glob("*.jpg"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    return removed
