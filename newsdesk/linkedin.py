"""LinkedIn: OAuth code exchange, member id, and text posts via the Posts API (Share on LinkedIn, w_member_social)."""
from __future__ import annotations

import re
import time

import requests

from .log import log

AUTH = "https://www.linkedin.com/oauth/v2"
API = "https://api.linkedin.com"
RESERVED = r"\|{}@[]()<>#*_~"


class LIError(Exception):
    def __init__(self, message: str, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


def little_text(text: str) -> str:
    """LinkedIn 'little text' format: escape reserved characters, keep trailing #tags as real hashtags."""
    out = []
    for token in re.split(r"(#[A-Za-z][A-Za-z0-9_]*)", text):
        if re.fullmatch(r"#[A-Za-z][A-Za-z0-9_]*", token):
            out.append("{hashtag|\\#|" + token[1:] + "}")
        else:
            out.append("".join("\\" + ch if ch in RESERVED else ch for ch in token))
    return "".join(out)


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str, session=None) -> dict:
    http = session or requests
    r = http.post(f"{AUTH}/accessToken", timeout=30, data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect_uri})
    data = r.json()
    if "access_token" not in data:
        raise LIError(f"code exchange failed: {data.get('error_description') or data}", r.status_code)
    return data


class LinkedIn:
    def __init__(self, token: str, version: str = "202606", session=None, sleep=time.sleep):
        self.token = token
        self.version = version
        self.http = session or requests.Session()
        self.sleep = sleep

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "LinkedIn-Version": self.version,
                "X-Restli-Protocol-Version": "2.0.0", "Content-Type": "application/json"}

    def me(self) -> dict:
        r = self.http.get(f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
        if r.status_code >= 400:
            raise LIError(f"userinfo HTTP {r.status_code}: {r.text[:200]}", r.status_code)
        return r.json()

    def post_text(self, person_id: str, text: str) -> str:
        body = {"author": f"urn:li:person:{person_id}", "commentary": little_text(text), "visibility": "PUBLIC",
                "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                                 "thirdPartyDistributionChannels": []},
                "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}
        for attempt in range(3):
            try:
                r = self.http.post(f"{API}/rest/posts", headers=self._headers(), json=body, timeout=60)
            except requests.RequestException as exc:
                err = LIError(f"network error: {type(exc).__name__}", transient=True)
            else:
                if r.status_code in (200, 201):
                    return r.headers.get("x-restli-id") or r.headers.get("X-RestLi-Id") or ""
                transient = r.status_code == 429 or r.status_code >= 500
                err = LIError(f"post HTTP {r.status_code}: {r.text[:300]}", r.status_code, transient)
                if not transient:
                    raise err
            if attempt < 2:
                log.warning("LinkedIn post attempt %d failed: %s", attempt + 1, err)
                self.sleep(20 * (attempt + 1))
        raise err


def post_url(post_id: str) -> str:
    return f"https://www.linkedin.com/feed/update/{post_id}/" if post_id else ""
