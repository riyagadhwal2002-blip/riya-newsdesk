"""Fetch RSS feeds, normalise items, and decide whether each item is genuinely fresh."""
from __future__ import annotations

import calendar
import html
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests

from .db import iso
from .log import log

USER_AGENT = "NewsdeskBot/1.0 (+personal news monitor; RSS reader)"
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref",
             "cmpid", "ocid", "src", "taid"}


# ── URL / text helpers ──────────────────────────────────────────────────────
def canonical_url(url: str) -> str:
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in _TRACKING])
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), path, "", query, ""))


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def domain_matches(domain: str, listed: list[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in listed)


# ── freshness ───────────────────────────────────────────────────────────────
def is_fresh(published: datetime, discovered: datetime, window_min: int, skew_min: int) -> bool:
    """New = first seen within `window_min` of its publish time, and not suspiciously future-dated."""
    age = discovered - published
    return timedelta(minutes=-skew_min) <= age <= timedelta(minutes=window_min)


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime.fromtimestamp(calendar.timegm(t), tz=timezone.utc)
    return None


# ── parsing ─────────────────────────────────────────────────────────────────
def parse_feed(content: bytes, feed_cfg: dict, settings_sources: dict, now: datetime,
               discovery_cfg: dict) -> list[dict]:
    parsed = feedparser.parse(content)
    items: list[dict] = []
    max_age = timedelta(hours=discovery_cfg.get("ignore_older_than_hours", 6))
    for e in parsed.entries:
        link = e.get("link")
        title = clean_text(e.get("title", ""))
        published = entry_time(e)
        if not link or not title or published is None:
            continue  # no timestamp → we cannot prove freshness → ignore
        if now - published > max_age:
            continue

        tier = feed_cfg.get("tier", "trusted")
        source = feed_cfg.get("name")
        domain = domain_of(link)
        if tier == "google_news":
            # Google News wraps links; the real outlet is in <source href=...>
            src = e.get("source") or {}
            outlet_url = src.get("href") or ""
            source = src.get("title") or source
            domain = domain_of(outlet_url) if outlet_url else domain
            # titles come as "Headline - Outlet"
            if source and title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            if domain_matches(domain, settings_sources.get("primary_domains", [])):
                tier = "primary"
            elif domain_matches(domain, settings_sources.get("trusted_domains", [])):
                tier = "trusted"
            else:
                tier = "unknown"

        items.append({
            "url": canonical_url(link),
            "title": title,
            "summary": clean_text(e.get("summary", ""))[:1200],
            "source": source,
            "domain": domain,
            "tier": tier,
            "published_at": iso(published),
            "discovered_at": iso(now),
            "fresh": is_fresh(published, now, discovery_cfg.get("freshness_minutes", 5),
                              discovery_cfg.get("max_clock_skew_minutes", 2)),
        })
    return items


def fetch_one(feed_cfg: dict, cache_row, timeout: int) -> tuple[dict, bytes | None, dict, str | None]:
    headers = {"User-Agent": USER_AGENT}
    if cache_row is not None:
        if cache_row["etag"]:
            headers["If-None-Match"] = cache_row["etag"]
        if cache_row["modified"]:
            headers["If-Modified-Since"] = cache_row["modified"]
    try:
        r = requests.get(feed_cfg["url"], headers=headers, timeout=timeout)
        if r.status_code == 304:
            return feed_cfg, None, {}, None
        r.raise_for_status()
        return feed_cfg, r.content, {"etag": r.headers.get("ETag"), "modified": r.headers.get("Last-Modified")}, None
    except Exception as exc:  # network errors are expected; one bad feed must not stop the rest
        return feed_cfg, None, {}, f"{type(exc).__name__}: {exc}"[:300]


def fetch_all(settings, db, now: datetime) -> list[dict]:
    sources = settings.section("sources")
    discovery = settings.section("discovery")
    feeds = sources.get("feeds", [])
    timeout = discovery.get("fetch_timeout_seconds", 10)
    items: list[dict] = []
    caches = {f["url"]: db.feed_cache(f["url"]) for f in feeds}  # SQLite stays on this thread
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda f: fetch_one(f, caches[f["url"]], timeout), feeds))
    for feed_cfg, content, meta, error in results:
        db.save_feed_cache(feed_cfg["url"], meta.get("etag"), meta.get("modified"), error is None, error, iso(now))
        if error:
            log.warning("feed failed: %s (%s)", feed_cfg["name"], error)
            continue
        if content:
            items.extend(parse_feed(content, feed_cfg, sources, now, discovery))
    return items


# ── article text (for fact-checking) ────────────────────────────────────────
class _ParagraphParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_p = 0
        self.skip = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "aside"):
            self.skip += 1
        if tag == "p":
            self.in_p += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "aside") and self.skip:
            self.skip -= 1
        if tag == "p" and self.in_p:
            self.in_p -= 1
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_p and not self.skip:
            self.parts.append(data)


def fetch_article_text(url: str, timeout: int = 10, limit: int = 6000) -> str:
    """Best-effort paragraph text. Paywalls/blocks simply return ''."""
    if "news.google.com" in url:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        if r.status_code != 200 or "html" not in r.headers.get("Content-Type", ""):
            return ""
        p = _ParagraphParser()
        p.feed(r.text)
        text = re.sub(r"[ \t]+", " ", "".join(p.parts))
        text = re.sub(r"\n\s*\n+", "\n", text).strip()
        return text[:limit]
    except Exception:
        return ""
