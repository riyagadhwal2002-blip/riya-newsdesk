"""One monitoring cycle: discover → cluster → verify → write → fact-check → render → publish (or preview)."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import cluster as clu
from . import factcheck, facts, hosting, schedule, scoring, tokens
from .db import DB, iso, parse_iso, utcnow
from .feeds import fetch_all, fetch_article_text
from .instagram import IGError, Instagram, TokenExpired
from .llm import BudgetExceeded, LLMError
from .log import log
from .prompts import CHECKER_SYSTEM, WRITER_SYSTEM, checker_user, writer_user
from .render import render_card, render_story
from .telegram import HELP, Telegram

MAX_LLM_STORIES_PER_CYCLE = 2


class Pipeline:
    def __init__(self, settings, db: DB, llm, telegram: Telegram, ig_factory=None, now_fn=utcnow,
                 fetch_fn=fetch_all, article_fn=fetch_article_text, verify_public_url: bool = True,
                 wiki_fn=None):
        self.s = settings
        self.db = db
        self.llm = llm
        self.tg = telegram
        self.ig_factory = ig_factory or self._default_ig
        self.now_fn = now_fn
        self.fetch_fn = fetch_fn
        self.article_fn = article_fn
        self.verify_public_url = verify_public_url
        self.wiki_fn = wiki_fn or facts.wiki_text
        self.disc = settings.section("discovery")
        self.pub = settings.section("publishing")
        self.kw = settings.section("keywords")
        self.rank = settings.section("ranking")
        self.brand = dict(settings.section("brand"))
        avatar = self.brand.get("avatar_path")
        self.brand["_avatar_abs"] = (settings.root / avatar) if avatar else None

    # ── switches ────────────────────────────────────────────────────────────
    def effective_mode(self) -> str:
        return (self.db.get("mode_override") or self.s.mode).lower()

    def paused(self) -> bool:
        return (self.s.data_dir / "PAUSE").exists() or self.db.get("paused") == "1"

    def windows(self) -> list[str] | None:
        return self.pub.get("active_windows") or None

    def tz(self) -> str:
        return self.brand.get("timezone", "Asia/Kolkata")

    def posting_open(self, now: datetime) -> bool:
        return schedule.is_open(self.windows(), now, self.tz())

    def story_start(self, first_published: datetime, now: datetime) -> datetime:
        return schedule.effective_start(first_published, self.windows(), now, self.tz())

    def token_seed(self) -> str | None:
        """Secret used to encrypt the stored token: the token secret if set, else the app secret."""
        return self.s.secret("IG_ACCESS_TOKEN") or self.s.secret("IG_APP_SECRET")

    def _default_ig(self) -> Instagram:
        token = tokens.load(self.db, self.token_seed()) or self.s.secret("IG_ACCESS_TOKEN")
        user_id = self.s.secret("IG_USER_ID") or self.db.get("ig_user_id") or "me"
        if not token:
            raise IGError("IG_ACCESS_TOKEN not configured")
        return Instagram(user_id, token, self.pub.get("instagram_api_version", "v26.0"))

    def handle_commands(self) -> None:
        offset = int(self.db.get("tg_offset", "0") or 0)
        cmds, offset = self.tg.commands(offset)
        self.db.set("tg_offset", str(offset))
        for c in cmds:
            if c == "/pause":
                self.db.set("paused", "1")
                self.tg.send("⏸ Paused. Nothing will be published until /resume.")
            elif c == "/resume":
                self.db.set("paused", "0")
                note = " (PAUSE file still present on the server)" if (self.s.data_dir / "PAUSE").exists() else ""
                self.tg.send("▶️ Resumed" + note + f". Mode: {self.effective_mode()}.")
            elif c == "/live":
                self.db.set("mode_override", "live")
                self.tg.send("🔴 LIVE: posts will publish automatically after all checks pass. /dryrun to undo.")
            elif c == "/dryrun":
                self.db.set("mode_override", "dry_run")
                self.tg.send("🧪 Dry run: previews only, nothing gets published.")
            elif c == "/status":
                self.tg.send(self.status_text())
            else:
                self.tg.send(HELP)

    def status_text(self) -> str:
        since = iso(self.now_fn() - timedelta(hours=24))
        pending = len(self.db.clusters_with_status("pending"))
        posts = self.db.posts_since(since)
        return (f"Mode: {self.effective_mode()} | Paused: {'yes' if self.paused() else 'no'}\n"
                f"Stories waiting for verification: {pending}\n"
                f"Posts last 24h: {sum(p['status'] == 'published' for p in posts)} published, "
                f"{sum(p['status'] == 'dry_run' for p in posts)} previews\n"
                f"Last cycle: {self.db.get('last_cycle', 'never')}")

    # ── cycle ───────────────────────────────────────────────────────────────
    def run_cycle(self) -> dict:
        now = self.now_fn()
        stats = {"new_items": 0, "fresh_items": 0, "processed": 0, "posted": 0, "previews": 0}
        self.handle_commands()

        items = self.fetch_fn(self.s, self.db, now)
        stats.update(self.ingest(items, now))
        self.expire(now)

        if self.paused():
            log.info("paused — ingested %d new items, not processing", stats["new_items"])
        else:
            if self.effective_mode() == "live":
                self.maybe_refresh_token(now)
            stats.update(self.process_candidates(now))
            if stats.get("posted", 0) + stats.get("previews", 0) == 0:
                stats.update(self.maybe_post_fact(now))
            if self.effective_mode() == "live":
                self.maybe_collect_metrics(now)

        if self.db.get("last_prune", "") < iso(now - timedelta(days=1)):
            self.db.prune(iso(now - timedelta(days=14)))
            hosting.cleanup(self.s.public_dir)
            self.db.set("last_prune", iso(now))
        self.db.set("last_cycle", iso(now))
        log.info("cycle done: %s", stats)
        return stats

    # ── ingest + cluster ────────────────────────────────────────────────────
    def ingest(self, items: list[dict], now: datetime) -> dict:
        lookback = iso(now - timedelta(hours=self.disc.get("cluster_lookback_hours", 48)))
        recent = self.db.conn.execute(
            "SELECT id, title, cluster_id FROM items WHERE discovered_at >= ? AND cluster_id IS NOT NULL",
            (lookback,)).fetchall()
        known = [(r["cluster_id"], clu.tokens(r["title"])) for r in recent]
        history = self._history_tokens(now)
        new_count = fresh_count = 0

        for it in sorted(items, key=lambda x: x["published_at"]):
            if self.db.item_exists(it["url"]):
                continue
            new_count += 1
            fresh_count += int(it["fresh"])
            item_id = self.db.add_item(it)
            toks = clu.tokens(it["title"])
            cid = clu.best_match(toks, known)
            if cid is not None:
                self.db.set_item_cluster(item_id, cid)
                self.db.merge_cluster_tokens(cid, [], it["published_at"], iso(now))
            else:
                status, reason = self._initial_status(it, toks, history)
                cid = self.db.add_cluster(it["title"], list(toks), it["published_at"], it["fresh"], status, iso(now))
                if reason:
                    self.db.update_cluster(cid, reason=reason)
                self.db.set_item_cluster(item_id, cid)
            known.append((cid, toks))
        return {"new_items": new_count, "fresh_items": fresh_count}

    def _initial_status(self, it: dict, toks: set[str], history: list[set[str]]) -> tuple[str, str | None]:
        text = f"{it['title']} {it.get('summary', '')}"
        if not it["fresh"]:
            return "stale", "not first seen within the freshness window"
        hits = scoring.blocked_terms(text, self.kw.get("blocked", []))
        if hits:
            return "blocked", "sensitive topic: " + ", ".join(hits)
        if scoring.keyword_score(text, self.kw.get("niche", {})) == 0:
            return "rejected", "off-niche (no niche keywords)"
        if any(clu.same_story(toks, h) for h in history):
            return "skipped_duplicate", "similar story already covered recently"
        return "pending", None

    def _history_tokens(self, now: datetime) -> list[set[str]]:
        since = iso(now - timedelta(days=self.disc.get("history_dedupe_days", 7)))
        return [set(json.loads(p["tokens"])) for p in self.db.posts_since(since)]

    def expire(self, now: datetime) -> None:
        if not self.posting_open(now):
            return  # stories found during closed hours wait for the next opening
        window = timedelta(minutes=self.disc.get("corroboration_window_minutes", 60))
        for c in self.db.clusters_with_status("pending"):
            if now - self.story_start(parse_iso(c["first_published_at"]), now) > window:
                items = self.db.cluster_items(c["id"])
                pts, why = scoring.credibility(items, self.rank.get("allow_single_trusted", False))
                need = self.rank.get("required_credibility", 20)
                self.db.update_cluster(c["id"], status="expired_unverified" if pts < need else "expired",
                                       reason=f"window passed; {why}")

    # ── candidates ──────────────────────────────────────────────────────────
    def process_candidates(self, now: datetime) -> dict:
        stats = {"processed": 0, "posted": 0, "previews": 0}
        if not self.posting_open(now):
            log.info("outside posting hours — stories are queued for the next window")
            return stats
        ready = []
        for c in self.db.clusters_with_status("pending"):
            items = self.db.cluster_items(c["id"])
            text = " ".join(f"{i['title']} {i['summary']}" for i in items)
            hits = scoring.blocked_terms(text, self.kw.get("blocked", []))
            if hits:  # re-checked here so rule changes also apply to stories already waiting
                self.db.update_cluster(c["id"], status="blocked", reason="sensitive topic: " + ", ".join(hits))
                continue
            pts, why = scoring.credibility(items, self.rank.get("allow_single_trusted", False))
            if pts < self.rank.get("required_credibility", 20):
                continue  # keep waiting for corroboration
            pre = scoring.keyword_score(text, self.kw.get("niche", {})) + len({i["domain"] for i in items})
            ready.append((pre, c, items, pts, why))
        if not ready:
            return stats

        cap_reason = self._cap_block(now)
        if cap_reason == "daily":
            for _, c, *_ in ready:
                self.db.update_cluster(c["id"], status="skipped_cap", reason="daily post cap reached")
            return stats
        if cap_reason == "gap":
            return stats  # try again next cycle, no AI calls spent

        ready.sort(key=lambda r: r[0], reverse=True)
        for _, c, items, cred, why in ready[:MAX_LLM_STORIES_PER_CYCLE]:
            stats["processed"] += 1
            try:
                outcome = self.handle_story(c, items, cred, why, now)
            except BudgetExceeded as exc:
                log.warning("%s", exc)
                self.tg.send(f"⚠️ {exc}. Discovery continues; writing resumes tomorrow.")
                break
            if outcome in ("published", "dry_run"):
                stats["posted" if outcome == "published" else "previews"] += 1
                break  # one post per cycle; the gap rule handles the rest
        return stats

    def _cap_block(self, now: datetime) -> str | None:
        statuses = ("published", "publishing") if self.effective_mode() == "live" else ("dry_run",)
        recent = self.db.posts_since(iso(now - timedelta(hours=24)), statuses)
        if len(recent) >= int(self.pub.get("daily_cap", 3)):
            return "daily"
        if recent:
            last = max(parse_iso(p["created_at"]) for p in recent)
            if now - last < timedelta(minutes=int(self.pub.get("min_gap_minutes", 90))):
                return "gap"
        return None

    def build_story(self, items) -> dict:
        articles = [{"outlet": i["source"], "domain": i["domain"], "tier": i["tier"], "title": i["title"],
                     "summary": i["summary"], "published_at": i["published_at"], "url": i["url"]} for i in items]
        parts = [f"[{i['source']}] {i['title']}. {i['summary']}" for i in items]
        if self.disc.get("fetch_article_text", True):
            fetched = 0
            for i in items:
                if fetched >= 2 or i["tier"] not in ("primary", "trusted"):
                    continue
                body = self.article_fn(i["url"])
                if body:
                    parts.append(f"[{i['source']} article text] {body}")
                    fetched += 1
        return {"articles": articles, "source_text": "\n\n".join(parts)[:14000]}

    def handle_story(self, c, items, cred: int, cred_why: str, now: datetime) -> str:
        cid = c["id"]
        self.db.update_cluster(cid, attempts=c["attempts"] + 1)
        story = self.build_story(items)
        system = WRITER_SYSTEM.format(handle=self.brand.get("handle", ""))

        try:
            pkg = self.llm.complete_json(system, writer_user(story))
        except BudgetExceeded:
            raise
        except LLMError as exc:
            log.warning("writer failed for cluster %s: %s", cid, exc)
            if c["attempts"] + 1 >= 3:
                self.db.update_cluster(cid, status="llm_error", reason=str(exc)[:300])
            return "llm_error"

        # ── editorial gates ──
        if cred < 20 and pkg.get("contested"):
            self.db.update_cluster(cid, status="rejected",
                                   reason="contested/alleged story needs a 2nd outlet or official source")
            return "rejected"
        if not pkg.get("relevant") or pkg.get("sensitive") or not pkg.get("new_development", True):
            reason = pkg.get("skip_reason") or ("sensitive" if pkg.get("sensitive") else "not relevant/new")
            self.db.update_cluster(cid, status="rejected", reason=f"AI editor: {reason}"[:300])
            return "rejected"

        first_pub = self.story_start(min(parse_iso(i["published_at"]) for i in items), now)
        score = scoring.total_score(scoring.recency_points(first_pub, now, self.rank.get("recency_full_minutes", 10)),
                                    cred, pkg.get("scores", {}))
        self.db.update_cluster(cid, score=score)
        if score < self.rank.get("threshold", 80):
            self.db.update_cluster(cid, status="rejected", reason=f"score {score} < threshold")
            return "rejected"

        post = pkg.setdefault("post", {})
        post["news"] = factcheck.strip_emoji(post.get("news", ""))
        post["take"] = factcheck.strip_emoji(post.get("take", ""))
        problems = factcheck.check_package(pkg, story["source_text"], [i["title"] for i in items],
                                           self.kw.get("blocked", []))
        if problems:
            self.db.update_cluster(cid, status="rejected_factcheck", reason="; ".join(problems)[:500])
            return "rejected_factcheck"
        try:
            verdict = self.llm.complete_json(CHECKER_SYSTEM, checker_user(story["source_text"], post), temperature=0.1)
        except BudgetExceeded:
            raise
        except LLMError as exc:
            self.db.update_cluster(cid, status="llm_error", reason=f"fact-checker failed: {exc}"[:300])
            return "llm_error"
        if str(verdict.get("verdict", "")).lower() != "pass":
            self.db.update_cluster(cid, status="rejected_factcheck",
                                   reason="AI fact-check: " + "; ".join(verdict.get("problems", []))[:480])
            return "rejected_factcheck"

        # last-second duplicate check (another post may have gone out since ingest)
        toks = clu.tokens(c["title"])
        if any(clu.same_story(toks, h) for h in self._history_tokens(now)):
            self.db.update_cluster(cid, status="skipped_duplicate", reason="covered meanwhile")
            return "skipped_duplicate"

        return self.produce_and_publish(c, items, pkg, story, score, cred_why, toks, now)

    # ── output ──────────────────────────────────────────────────────────────
    def full_caption(self, post: dict, items) -> str:
        outlets = list(dict.fromkeys(i["source"] for i in items if i["tier"] in ("primary", "trusted")))
        caption = re.sub(r"(?:\s*#[\w]+)+\s*$", "", post["caption"].strip())  # hashtags are added once, below
        caption += f"\n\nSource: {', '.join(outlets[:3])}"
        caption += "\n\n" + " ".join(post.get("hashtags", [])[:8])
        return caption[:2150]

    def produce_and_publish(self, c, items, pkg, story, score, cred_why, toks, now: datetime) -> str:
        cid = c["id"]
        post = pkg["post"]
        outlets = list(dict.fromkeys(i["source"] for i in items if i["tier"] in ("primary", "trusted")))
        post_dir = self.s.data_dir / "posts" / f"{now.strftime('%Y%m%d-%H%M')}-{cid}"
        card = render_card(post, outlets, self.brand, post_dir / "card.jpg", self.s.root / "fonts", when=now)
        caption = self.full_caption(post, items)
        package = {"cluster_id": cid, "score": score, "credibility": cred_why, "caption": caption,
                   "post": post, "claims": pkg.get("claims", []), "category": pkg.get("category"),
                   "scores": pkg.get("scores"), "sources": story["articles"], "created_at": iso(now)}
        (post_dir / "package.json").write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        links = "\n".join(f"• {a['outlet']}: {a['url']}" for a in story["articles"][:5])

        if self.effective_mode() != "live":
            self.db.add_post(cid, "dry_run", list(toks), package, iso(now), str(card))
            self.db.update_cluster(cid, status="dry_run", reason=f"score {score}")
            self.tg.send_photo(card, f"🧪 PREVIEW (dry run) · score {score}/100 · {cred_why}\n\n{caption}")
            self.tg.send(f"Sources:\n{links}\n\nFirst comment: {post.get('first_comment', '')}")
            return "dry_run"

        post_id = self.db.add_post(cid, "publishing", list(toks), package, iso(now), str(card))
        self.db.update_cluster(cid, status="ready")
        try:
            url = hosting.publish_image(card, self.s.public_dir, self.s.public_base_url, verify=self.verify_public_url,
                                        git_push=os.environ.get("HOSTING") == "git")
            self.db.update_post(post_id, image_url=url)
            ig = self.ig_factory()
            container = ig.create_image_container(url, caption, post.get("alt_text"))
            self.db.update_post(post_id, ig_container_id=container)
            ig.wait_until_ready(container)
            media_id = ig.publish_container(container, caption)
            permalink = ig.permalink(media_id)
            self.db.update_post(post_id, status="published", ig_media_id=media_id, permalink=permalink,
                                published_at=iso(self.now_fn()))
            self.db.update_cluster(cid, status="posted", reason=f"score {score}")
            if self.pub.get("post_first_comment", True) and post.get("first_comment"):
                try:
                    ig.comment(media_id, post["first_comment"])
                except IGError as exc:
                    log.warning("first comment failed: %s", exc)
            if self.pub.get("post_story", True):
                self.share_to_story(ig, card, post_id)
            self.tg.send_photo(card, f"✅ Published · score {score}/100\n{permalink}\n\n{caption[:600]}")
            self.tg.send(f"Sources:\n{links}")
            return "published"
        except TokenExpired as exc:
            self.db.update_post(post_id, status="failed", error=str(exc)[:500])
            self.db.update_cluster(cid, status="failed", reason="token expired")
            self.db.set("paused", "1")
            self.tg.send("🛑 Instagram token expired or revoked. Auto-publishing PAUSED. "
                         "Generate a new token, put it in .env, then run `newsdesk resume`.")
            return "failed"
        except (IGError, hosting.HostingError, OSError) as exc:
            self.db.update_post(post_id, status="failed", error=str(exc)[:500])
            self.db.update_cluster(cid, status="failed", reason=str(exc)[:300])
            self.tg.send(f"❌ Publishing failed (nothing went live): {exc}")
            log.error("publish failed: %s", exc)
            return "failed"

    # ── "Did you know?" posts ───────────────────────────────────────────────
    def maybe_post_fact(self, now: datetime) -> dict:
        cfg = self.s.section("facts")
        if not cfg.get("enabled", True) or not self.posting_open(now) or self._cap_block(now):
            return {}
        statuses = ("published", "publishing") if self.effective_mode() == "live" else ("dry_run",)
        recent = self.db.posts_since(iso(now - timedelta(minutes=int(cfg.get("min_gap_minutes", 90)))), statuses)
        if recent:
            return {}  # a post went out recently; facts only fill quiet stretches
        today = [p for p in self.db.posts_since(iso(now - timedelta(hours=24)), statuses)
                 if json.loads(p["package"]).get("category") == "Did you know?"]
        if len(today) >= int(cfg.get("max_per_day", 3)):
            return {}
        all_facts = facts.load(self.s.root / cfg.get("file", "facts.yaml"))
        used = set(json.loads(self.db.get("facts_used", "[]")))
        for _ in range(3):
            fact = facts.next_fact(all_facts, used)
            if not fact:
                return {}
            used.add(fact["id"])
            self.db.set("facts_used", json.dumps(sorted(used)))
            try:
                ok, missing = facts.verified(fact, self.wiki_fn(fact["wiki"]))
            except Exception as exc:  # Wikipedia down → try again next cycle
                used.discard(fact["id"])
                self.db.set("facts_used", json.dumps(sorted(used)))
                log.warning("wikipedia check failed: %s", exc)
                return {}
            if not ok:
                log.warning("fact %s skipped — not confirmed on Wikipedia (missing: %s)", fact["id"], missing)
                continue
            return self.publish_fact(fact, now)
        return {}

    def publish_fact(self, fact: dict, now: datetime) -> dict:
        url = "https://en.wikipedia.org/wiki/" + fact["wiki"].replace(" ", "_")
        post = {"news": fact["fact"], "take": fact["take"], "caption": fact["caption"],
                "hashtags": fact.get("hashtags", []), "first_comment": fact.get("first_comment", ""),
                "alt_text": f"{fact['fact']} {fact['take']}"}
        pkg = {"post": post, "category": "Did you know?", "claims": [{"text": fact["fact"], "evidence": url}],
               "scores": {}}
        cid = self.db.add_cluster(f"fact:{fact['id']}", [], iso(now), True, "pending", iso(now))
        cluster = self.db.get_cluster(cid)
        items = [{"source": "Wikipedia", "tier": "primary", "url": url, "title": fact["fact"]}]
        story = {"articles": [{"outlet": "Wikipedia", "url": url, "title": fact["wiki"]}], "source_text": fact["fact"]}
        outcome = self.produce_and_publish(cluster, items, pkg, story, 100, "checked on Wikipedia", set(), now)
        return {"facts": 1, "posted" if outcome == "published" else "previews": int(outcome in ("published", "dry_run"))}

    def share_to_story(self, ig, card: Path, post_id: int) -> None:
        try:
            story = render_story(card, Path(card).with_name("story.jpg"), self.brand)
            url = hosting.publish_image(story, self.s.public_dir, self.s.public_base_url,
                                        verify=self.verify_public_url, git_push=os.environ.get("HOSTING") == "git")
            container = ig.create_story_container(url)
            ig.wait_until_ready(container)
            story_id = ig.publish_story(container)
            self.db.set(f"story_for_post_{post_id}", story_id)
            log.info("story published for post %s", post_id)
        except (IGError, hosting.HostingError, OSError) as exc:
            log.warning("story failed (feed post is fine): %s", exc)
            self.tg.send(f"⚠️ Feed post is live, but adding it to your story failed: {exc}")

    # ── maintenance ─────────────────────────────────────────────────────────
    def maybe_refresh_token(self, now: datetime) -> None:
        last = self.db.get("ig_token_refreshed_at")
        if last and now - parse_iso(last) < timedelta(days=7):
            return
        if not last:  # first run: start the clock; Instagram needs the token to be 24h+ old to refresh
            self.db.set("ig_token_refreshed_at", iso(now))
            return
        try:
            token, expires = self.ig_factory().refresh_token()
            tokens.save(self.db, token, self.token_seed())
            self.db.set("ig_token_refreshed_at", iso(now))
            log.info("Instagram token refreshed (valid %d days)", expires // 86400)
        except TokenExpired:
            self.db.set("paused", "1")
            self.tg.send("🛑 Instagram token could not be refreshed (expired). Publishing paused.")
        except IGError as exc:
            log.warning("token refresh failed, will retry: %s", exc)

    def maybe_collect_metrics(self, now: datetime) -> None:
        cfg = self.s.section("analytics")
        if not cfg.get("enabled", True):
            return
        last = self.db.get("metrics_at")
        if last and now - parse_iso(last) < timedelta(hours=cfg.get("refresh_hours", 6)):
            return
        self.db.set("metrics_at", iso(now))
        try:
            ig = self.ig_factory()
        except IGError:
            return
        rows = self.db.conn.execute(
            "SELECT id, ig_media_id FROM posts WHERE status='published' AND published_at >= ?",
            (iso(now - timedelta(days=30)),)).fetchall()
        for r in rows:
            try:
                values = ig.insights(r["ig_media_id"], cfg.get("metrics", []))
            except IGError as exc:
                log.warning("insights failed for %s: %s", r["ig_media_id"], exc)
                continue
            for k, v in values.items():
                self.db.conn.execute(
                    "INSERT INTO metrics(post_id,fetched_at,metric,value) VALUES(?,?,?,?) "
                    "ON CONFLICT(post_id,metric) DO UPDATE SET value=excluded.value, fetched_at=excluded.fetched_at",
                    (r["id"], iso(now), k, float(v)))
        self.db.conn.commit()
