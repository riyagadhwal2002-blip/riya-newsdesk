"""Email alerts via GitHub: comments on one 'Newsdesk alerts' issue that @mentions the repo owner.
GitHub emails each comment to the owner's GitHub email. Works inside GitHub Actions with GITHUB_TOKEN."""
from __future__ import annotations

import os

import requests

from .log import log

TITLE = "Newsdesk alerts"


def send(text: str, session=None) -> bool:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log.info("[alert, no GitHub token] %s", text[:300])
        return False
    http = session or requests
    owner = repo.split("/")[0]
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{repo}"
    try:
        issues = http.get(f"{api}/issues", headers=h, params={"state": "open", "per_page": 50}, timeout=20).json()
        issue = next((i for i in issues if isinstance(i, dict) and i.get("title") == TITLE), None)
        body = f"@{owner} {text}"
        if issue:
            r = http.post(f"{api}/issues/{issue['number']}/comments", headers=h, json={"body": body}, timeout=20)
        else:
            r = http.post(f"{api}/issues", headers=h, json={"title": TITLE, "body": body}, timeout=20)
        return r.status_code < 300
    except (requests.RequestException, ValueError) as exc:
        log.warning("alert failed: %s", type(exc).__name__)
        return False
