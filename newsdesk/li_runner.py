"""Riya's LinkedIn autopilot: one post per scheduled slot, alternating news-based lessons and evergreen insights."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yaml

from . import alerts, factcheck, li_prompts, scoring, tokens
from .db import iso, parse_iso
from .linkedin import LIError, LinkedIn, post_url
from .llm import BudgetExceeded, LLMError
from .log import log
from .prompts import CHECKER_SYSTEM, checker_user

# weekday → (kind, label). kind "news" falls back to an evergreen pillar when no good story exists.
DEFAULT_PLAN = {
    "mon": ["news", "Marketing breakdown"],
    "tue": ["personal_branding", "Personal branding"],
    "wed": ["news", "Brand / campaign lesson"],
    "thu": ["linkedin_growth", "LinkedIn growth"],
    "fri": ["genz_careers", "Gen Z and careers"],
    "sat": ["hot_takes", "Hot take"],
}
FALLBACK_PILLARS = ["personal_branding", "linkedin_growth", "genz_careers", "hot_takes"]
NEWS_KEYWORDS = {3: ["campaign*", "rebrand*", "branding", "marketing", "advert*", "brand ambassador", "influencer*",
                     "creator*", "linkedin", "personal brand*", "viral", "backlash", "gen z", "intern*", "hiring",
                     "layoff*", "workplace", "career*", "job*", "remote work", "return to office"],
                 2: ["launch*", "startup*", "founder*", "ceo", "ai", "consumer*", "instagram", "youtube", "tiktok",
                     "brand*", "collab*", "salary", "salaries", "skills", "recruit*"]}


class LinkedInRunner:
    def __init__(self, settings, db, llm, now_fn, client_factory=None, alert_fn=None):
        self.s, self.db, self.llm, self.now_fn = settings, db, llm, now_fn
        self.cfg = settings.section("linkedin")
        self.client_factory = client_factory or self._client
        self.alert = alert_fn or alerts.send

    # ── state ────────────────────────────────────────────────────────────────
    def seed(self) -> str | None:
        return self.s.secret("LINKEDIN_CLIENT_SECRET")

    def _client(self) -> LinkedIn:
        token = tokens.load(self.db, self.seed(), key="li_token_enc")
        if not token:
            raise LIError("LinkedIn is not connected")
        return LinkedIn(token, self.cfg.get("api_version", "202606"))

    def connected(self) -> bool:
        return bool(self.db.get("li_person_id")) and bool(tokens.load(self.db, self.seed(), key="li_token_enc"))

    def history(self) -> list[dict]:
        return json.loads(self.db.get("li_history", "[]"))

    def notify(self, key: str, text: str, every_hours: int = 24) -> None:
        now = self.now_fn()
        last = self.db.get(f"li_alert_{key}")
        if last and now - parse_iso(last) < timedelta(hours=every_hours):
            return
        self.db.set(f"li_alert_{key}", iso(now))
        self.alert(text)

    # ── schedule ─────────────────────────────────────────────────────────────
    def local(self, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(self.s.section("brand").get("timezone", "Asia/Kolkata")))

    def due_slot(self, now: datetime) -> str | None:
        """Returns today's slot label (e.g. '2026-11-03 09:30') if a post is due now, else None."""
        local = self.local(now)
        day = local.strftime("%a").lower()
        if day not in (self.cfg.get("plan") or DEFAULT_PLAN):
            return None
        done = {h["slot"] for h in self.history()}
        for t in self.cfg.get("post_times", ["09:30"]):
            h, m = map(int, t.split(":"))
            slot_dt = local.replace(hour=h, minute=m, second=0, microsecond=0)
            label = slot_dt.strftime("%Y-%m-%d %H:%M")
            if slot_dt <= local <= slot_dt + timedelta(hours=4) and label not in done:
                tries = int(self.db.get(f"li_tries_{label}", "0") or 0)
                if tries < 3:
                    return label
        return None

    # ── content ──────────────────────────────────────────────────────────────
    def pick_story(self, now: datetime) -> tuple[dict, dict] | None:
        since = iso(now - timedelta(hours=36))
        used = {h.get("cluster_id") for h in self.history()}
        blocked = self.s.section("keywords").get("blocked", []) + ["fraud", "bank*", "payment*", "credit card*",
                                                                   "american express", "amex", "invest*", "stock*"]
        best = None
        for c in self.db.conn.execute("SELECT * FROM clusters WHERE created_at >= ? AND title NOT LIKE '%:%'",
                                      (since,)).fetchall():
            if c["id"] in used:
                continue
            items = [dict(i) for i in self.db.cluster_items(c["id"])]
            if not items:
                continue
            pts, _ = scoring.credibility(items, True)
            text = " ".join(f"{i['title']} {i['summary']}" for i in items)
            if pts < 18 or scoring.blocked_terms(text, blocked):
                continue
            score = scoring.keyword_score(text, NEWS_KEYWORDS) + len({i["domain"] for i in items})
            if score >= 4 and (best is None or score > best[0]):
                best = (score, c, items)
        if not best:
            return None
        _, c, items = best
        story = {"articles": [{"outlet": i["source"], "title": i["title"], "summary": i["summary"], "url": i["url"],
                               "published_at": i["published_at"]} for i in items],
                 "source_text": "\n\n".join(f"[{i['source']}] {i['title']}. {i['summary']}" for i in items)[:12000]}
        return {"cluster_id": c["id"], "title": c["title"]}, story

    def pick_angle(self, pillar: str) -> str | None:
        path = self.s.root / self.cfg.get("topics_file", "linkedin_topics.yaml")
        bank = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get(pillar, []) if path.exists() else []
        recent = {h.get("angle") for h in self.history()[-60:]}
        return next((a for a in bank if a not in recent), bank[0] if bank else None)

    def check(self, text: str, source_text: str, claims: list, news: bool) -> list[str]:
        problems = []
        if not 300 <= len(text) <= 2800:
            problems.append(f"length {len(text)} chars")
        allowed_numbers = factcheck._numbers(factcheck.norm(li_prompts.BIO + " " + source_text))
        extra = sorted(n for n in factcheck._numbers(text) - allowed_numbers if not (n.isdigit() and int(n) <= 10))
        if extra:
            problems.append(f"numbers not in sources: {', '.join(extra)}")
        if re.search(r"\b(I|we) (met|spoke to|was talking to|had a call with)\b|a (student|client|mentee) (told|asked) me",
                     text, re.I):
            problems.append("invented personal anecdote")
        blocked = self.s.section("keywords").get("blocked", []) + ["fraud", "american express", "amex"]
        hits = scoring.blocked_terms(text, blocked)
        if hits:
            problems.append(f"blocked topics: {', '.join(hits)}")
        if news:
            for c in claims or []:
                if not factcheck.evidence_supported(str(c.get("evidence", "")), source_text):
                    problems.append(f"unsupported claim: {str(c.get('text', ''))[:60]}")
            if not claims:
                problems.append("news post without evidence")
        return problems

    def write(self, now: datetime) -> dict | None:
        day = self.local(now).strftime("%a").lower()
        kind, label = (self.cfg.get("plan") or DEFAULT_PLAN)[day]
        attempts = []
        if kind == "news":
            picked = self.pick_story(now)
            if picked:
                attempts.append(("news", label, picked))
            kind = FALLBACK_PILLARS[self.local(now).toordinal() % len(FALLBACK_PILLARS)]
        attempts.append((kind, label, None))
        for kind, label, picked in attempts:
            if kind == "news":
                meta, story = picked
                out = self.llm.complete_json(li_prompts.system(), li_prompts.news_user(story, label))
                text, source = (out.get("post") or "").strip(), story["source_text"]
                if not out.get("usable") or self.check(text, source, out.get("claims"), True):
                    log.info("LinkedIn news draft rejected: %s", out.get("skip_reason") or
                             self.check(text, source, out.get("claims"), True))
                    continue
                verdict = self.llm.complete_json(CHECKER_SYSTEM, checker_user(source, {"caption": text}),
                                                 temperature=0.1)
                if str(verdict.get("verdict", "")).lower() != "pass":
                    log.info("LinkedIn news draft failed fact-check: %s", verdict.get("problems"))
                    continue
                return {"kind": "news", "label": label, "text": text, "cluster_id": meta["cluster_id"],
                        "topic": meta["title"], "sources": [a["url"] for a in story["articles"]]}
            angle = self.pick_angle(kind)
            if not angle:
                continue
            for _ in range(2):
                out = self.llm.complete_json(li_prompts.system(), li_prompts.evergreen_user(kind, angle))
                text = (out.get("post") or "").strip()
                problems = self.check(text, "", [], False)
                if out.get("usable") and not problems:
                    return {"kind": kind, "label": label, "text": text, "angle": angle, "topic": angle}
                log.info("LinkedIn draft rejected: %s", problems or out.get("skip_reason"))
        return None

    def preview(self, pillars=("news", "personal_branding", "linkedin_growth", "genz_careers", "hot_takes")) -> list[dict]:
        """Writes sample drafts without posting (for review of tone)."""
        out = []
        for kind in pillars:
            plan = {"x": ["news", "Marketing breakdown"] if kind == "news" else [kind, kind.replace("_", " ").title()]}
            self.cfg = dict(self.cfg, plan={self.local(self.now_fn()).strftime("%a").lower(): plan["x"]})
            try:
                draft = self.write(self.now_fn())
            except (LLMError, BudgetExceeded) as exc:
                draft = {"kind": kind, "error": str(exc)}
            out.append(draft or {"kind": kind, "error": "no draft passed the checks"})
        return out

    # ── main ─────────────────────────────────────────────────────────────────
    def run(self) -> dict:
        now = self.now_fn()
        if not self.cfg.get("enabled", True) or self.db.get("li_paused") == "1":
            return {}
        if not self.connected():
            return {}
        self.warn_expiry(now)
        slot = self.due_slot(now)
        if not slot:
            return {}
        self.db.set(f"li_tries_{slot}", str(int(self.db.get(f"li_tries_{slot}", "0") or 0) + 1))
        try:
            draft = self.write(now)
        except (LLMError, BudgetExceeded) as exc:
            log.warning("LinkedIn writer unavailable: %s", exc)
            return {"li_error": 1}
        if not draft:
            log.info("no LinkedIn post good enough for slot %s", slot)
            return {}
        if self.cfg.get("mode", "live") != "live":
            self.record(slot, draft, "", now, status="dry_run")
            return {"li_preview": 1}
        try:
            post_id = self.client_factory().post_text(self.db.get("li_person_id"), draft["text"])
        except LIError as exc:
            log.error("LinkedIn post failed: %s", exc)
            if exc.status in (401, 403):
                self.db.set("li_paused", "1")
                self.notify("li_auth", "🔑 LinkedIn connection expired or was revoked, so LinkedIn posting is "
                            "paused. Reconnect: riyagadhwal2002-blip.github.io/riya-newsdesk/connect-linkedin.html")
            else:
                self.notify("li_fail", f"❌ A LinkedIn post failed (nothing went live): {str(exc)[:200]}", 12)
            return {"li_error": 1}
        self.record(slot, draft, post_id, now)
        log.info("LinkedIn post published: %s", post_url(post_id))
        return {"li_posted": 1}

    def record(self, slot: str, draft: dict, post_id: str, now: datetime, status: str = "published") -> None:
        h = self.history()
        h.append({"slot": slot, "at": iso(now), "status": status, "kind": draft["kind"], "topic": draft["topic"],
                  "angle": draft.get("angle"), "cluster_id": draft.get("cluster_id"), "post_id": post_id,
                  "url": post_url(post_id), "text": draft["text"], "sources": draft.get("sources", [])})
        self.db.set("li_history", json.dumps(h[-200:], ensure_ascii=False))

    def warn_expiry(self, now: datetime) -> None:
        exp = self.db.get("li_token_expires_at")
        if exp and parse_iso(exp) - now < timedelta(days=7):
            days = max(0, (parse_iso(exp) - now).days)
            self.notify("li_expiry", f"⏳ Your LinkedIn connection expires in {days} days (LinkedIn's 60-day rule). "
                        "Tap to renew, takes 20 seconds: riyagadhwal2002-blip.github.io/riya-newsdesk/connect-linkedin.html",
                        every_hours=48)
