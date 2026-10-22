import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from newsdesk.config import load_settings  # noqa: E402
from newsdesk.db import DB, iso  # noqa: E402
from newsdesk.feeds import is_fresh  # noqa: E402
from newsdesk.instagram import IGError  # noqa: E402
from newsdesk.llm import MockLLM  # noqa: E402
from newsdesk.pipeline import Pipeline  # noqa: E402

T0 = datetime(2026, 10, 22, 13, 30, tzinfo=timezone.utc)

SOURCE_SUMMARY = ("Brew Co unveiled a new logo on Thursday that replaces its coffee cup with a crown. "
                  "The redesign cost 2 million dollars, the company said. Social media users compared it to a "
                  "chess piece.")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += timedelta(minutes=minutes)


def make_item(title, source, domain, tier, published, discovered, summary=SOURCE_SUMMARY, url=None):
    return {
        "url": url or f"https://{domain}/{abs(hash((title, source))) % 10**8}",
        "title": title, "summary": summary, "source": source, "domain": domain, "tier": tier,
        "published_at": iso(published), "discovered_at": iso(discovered),
        "fresh": is_fresh(published, discovered, 5, 2),
    }


def good_package(**overrides):
    pkg = {
        "relevant": True, "sensitive": False, "new_development": True, "category": "Rebrand",
        "scores": {"relevance": 23, "interest": 17, "wit": 9}, "skip_reason": "",
        "post": {
            "news": "Brew Co swapped the coffee cup in its logo for a crown, and the redesign reportedly cost 2 million dollars.",
            "take": "Imagine paying that much to tell customers you'd rather be a chess piece than a cafe.",
            "insight": "A rebrand that drops what you actually sell confuses people faster than it impresses them.",
            "caption": "Brew Co just rebranded and the coffee cup is gone. Is this bold or a very expensive identity crisis? 👑☕",
            "hashtags": ["#rebrand", "#branding", "#marketing", "#logodesign"],
            "first_comment": "Next update: the crown gets a latte.",
            "alt_text": "Text post about Brew Co's new crown logo.",
        },
        "claims": [
            {"text": "Brew Co replaced the cup with a crown", "evidence": "new logo on Thursday that replaces its coffee cup with a crown"},
            {"text": "cost 2 million dollars", "evidence": "The redesign cost 2 million dollars"},
        ],
    }
    for k, v in overrides.items():
        if k in pkg["post"]:
            pkg["post"][k] = v
        else:
            pkg[k] = v
    return pkg


PASS = {"verdict": "pass", "problems": []}


class FakeIG:
    def __init__(self, fail_container=0, publish_errors=None, expired=False):
        self.calls = []
        self.fail_container = fail_container
        self.publish_errors = list(publish_errors or [])
        self.expired = expired
        self.published = 0

    def create_image_container(self, url, caption, alt):
        from newsdesk.instagram import TokenExpired
        self.calls.append(("container", url))
        if self.expired:
            raise TokenExpired("OAuthException 190: token expired", 190)
        if self.fail_container:
            self.fail_container -= 1
            raise IGError("boom", transient=False)
        return "C1"

    def wait_until_ready(self, cid):
        self.calls.append(("wait", cid))

    def publish_container(self, cid, caption):
        self.calls.append(("publish", cid))
        if self.publish_errors:
            raise self.publish_errors.pop(0)
        self.published += 1
        return "M1"

    def permalink(self, mid):
        return "https://www.instagram.com/p/FAKE/"

    def comment(self, mid, msg):
        self.calls.append(("comment", msg))
        return "K1"

    def insights(self, mid, metrics):
        self.calls.append(("insights", mid))
        return {"reach": 1000, "shares": 40, "saved": 25}

    def refresh_token(self):
        self.calls.append(("refresh", None))
        return "NEWTOKEN", 60 * 86400


class FakeTG:
    enabled = True

    def __init__(self):
        self.messages, self.photos, self.queued = [], [], []

    def send(self, text):
        self.messages.append(text)

    def send_photo(self, path, caption):
        self.photos.append((path, caption))

    def commands(self, offset):
        cmds, self.queued = self.queued, []
        return cmds, offset


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSDESK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PUBLIC_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://riya.example/p")
    monkeypatch.delenv("NEWSDESK_MODE", raising=False)
    settings = load_settings(ROOT / "config.yaml", env_path=tmp_path / "none.env")
    settings.raw["llm"]["provider"] = "mock"
    # tests pin their own numbers so they don't depend on how config.yaml is tuned
    settings.raw["publishing"].update({"daily_cap": 3, "min_gap_minutes": 90})
    settings.raw["discovery"].update({"freshness_minutes": 5, "corroboration_window_minutes": 60})
    settings.raw["ranking"]["recency_full_minutes"] = 10
    db = DB(settings.data_dir / "test.db")
    return settings, db


@pytest.fixture
def make_pipeline(env):
    settings, db = env

    def _make(responses=None, ig=None, mode="dry_run", batches=None, clock=None):
        settings.raw["publishing"]["mode"] = mode
        clock = clock or Clock()
        llm = MockLLM(settings, db, responses or [])
        llm.sleep = lambda s: None
        batches = list(batches or [])
        tg = FakeTG()
        pipe = Pipeline(settings, db, llm, tg, ig_factory=(lambda: ig) if ig else None, now_fn=clock,
                        fetch_fn=lambda s, d, now: batches.pop(0) if batches else [],
                        article_fn=lambda url: "", verify_public_url=False)
        return pipe, llm, tg, clock

    return _make
