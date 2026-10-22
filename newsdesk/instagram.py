"""Instagram API with Instagram Login (graph.instagram.com) — image publishing, comments, insights, token refresh."""
from __future__ import annotations

import time

import requests

from .log import log

BASE = "https://graph.instagram.com"


class IGError(Exception):
    def __init__(self, message: str, code: int | None = None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.transient = transient


class TokenExpired(IGError):
    pass


class Instagram:
    def __init__(self, user_id: str, token: str, version: str = "v26.0", session=None, sleep=time.sleep):
        self.user_id = user_id
        self.token = token
        self.version = version
        self.http = session or requests.Session()
        self.sleep = sleep

    # low level ---------------------------------------------------------------
    def _url(self, path: str, versioned: bool = True) -> str:
        return f"{BASE}/{self.version}/{path}" if versioned else f"{BASE}/{path}"

    def _request(self, method: str, path: str, versioned: bool = True, **params) -> dict:
        params["access_token"] = self.token
        try:
            if method == "GET":
                r = self.http.get(self._url(path, versioned), params=params, timeout=30)
            else:
                r = self.http.post(self._url(path, versioned), data=params, timeout=60)
        except requests.RequestException as exc:
            raise IGError(f"network error: {type(exc).__name__}", transient=True)
        try:
            data = r.json()
        except ValueError:
            raise IGError(f"HTTP {r.status_code}: non-JSON response", transient=r.status_code >= 500)
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            code = err.get("code")
            msg = f"{err.get('type', 'Error')} {code}: {err.get('message', r.status_code)}"
            if code == 190:
                raise TokenExpired(msg, code)
            transient = r.status_code >= 500 or code in (1, 2, 4, 17, 32, 341, 9007) or err.get("is_transient")
            raise IGError(msg, code, bool(transient))
        return data

    def _retry(self, fn, attempts: int = 3, wait: int = 10):
        for i in range(attempts):
            try:
                return fn()
            except TokenExpired:
                raise
            except IGError as exc:
                if not exc.transient or i == attempts - 1:
                    raise
                log.warning("Instagram transient error, retrying: %s", exc)
                self.sleep(wait * (i + 1))

    # publishing ----------------------------------------------------------------
    def publishing_quota_used(self) -> int | None:
        data = self._request("GET", f"{self.user_id}/content_publishing_limit", fields="quota_usage")
        rows = data.get("data") or []
        return rows[0].get("quota_usage") if rows else None

    def create_image_container(self, image_url: str, caption: str, alt_text: str | None = None) -> str:
        def call():
            params = {"image_url": image_url, "caption": caption}
            if alt_text:
                params["alt_text"] = alt_text[:1000]
            try:
                return self._request("POST", f"{self.user_id}/media", **params)
            except IGError as exc:
                if alt_text and exc.code == 100 and "alt_text" in str(exc):
                    params.pop("alt_text")  # field not accepted for this account/version → publish without it
                    return self._request("POST", f"{self.user_id}/media", **params)
                raise
        return self._retry(call)["id"]

    def container_status(self, container_id: str) -> str:
        return self._request("GET", container_id, fields="status_code").get("status_code", "")

    def wait_until_ready(self, container_id: str, max_checks: int = 10, every: int = 30) -> None:
        for _ in range(max_checks):
            status = self.container_status(container_id)
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise IGError(f"container {container_id} status {status}")
            self.sleep(every)
        raise IGError(f"container {container_id} not ready after {max_checks} checks", transient=True)

    def publish_container(self, container_id: str, caption: str) -> str:
        """Publishes once. If the call fails ambiguously, checks whether it went live before retrying,
        so a network blip can never produce a double post."""
        for attempt in range(3):
            try:
                return self._request("POST", f"{self.user_id}/media_publish", creation_id=container_id)["id"]
            except TokenExpired:
                raise
            except IGError as exc:
                if not exc.transient:
                    raise
                log.warning("publish attempt %d unclear: %s — checking before retry", attempt + 1, exc)
                self.sleep(15)
                try:
                    if self.container_status(container_id) == "PUBLISHED":
                        media_id = self.find_recent_media_by_caption(caption)
                        if media_id:
                            return media_id
                        raise IGError("published but media id not found", transient=False)
                except IGError as check_exc:
                    if not check_exc.transient:
                        raise
        raise IGError("publish failed after retries")

    def find_recent_media_by_caption(self, caption: str) -> str | None:
        data = self._request("GET", f"{self.user_id}/media", fields="id,caption,timestamp", limit=5)
        head = caption[:80]
        for m in data.get("data", []):
            if (m.get("caption") or "")[:80] == head:
                return m["id"]
        return None

    def permalink(self, media_id: str) -> str | None:
        return self._request("GET", media_id, fields="permalink").get("permalink")

    def comment(self, media_id: str, message: str) -> str:
        return self._request("POST", f"{media_id}/comments", message=message)["id"]

    # analytics + token -------------------------------------------------------------
    def insights(self, media_id: str, metrics: list[str]) -> dict:
        out = {}
        try:
            data = self._request("GET", f"{media_id}/insights", metric=",".join(metrics))
            rows = data.get("data", [])
        except IGError:
            rows = []
            for m in metrics:  # one unsupported metric fails the batch, so fall back to one by one
                try:
                    rows += self._request("GET", f"{media_id}/insights", metric=m).get("data", [])
                except IGError:
                    continue
        for row in rows:
            vals = row.get("values") or [{}]
            value = row.get("total_value", {}).get("value", vals[0].get("value"))
            if value is not None:
                out[row["name"]] = value
        return out

    def refresh_token(self) -> tuple[str, int]:
        data = self._request("GET", "refresh_access_token", versioned=False, grant_type="ig_refresh_token")
        return data["access_token"], int(data.get("expires_in", 0))

    def me(self) -> dict:
        return self._request("GET", "me", fields="user_id,username,account_type")
