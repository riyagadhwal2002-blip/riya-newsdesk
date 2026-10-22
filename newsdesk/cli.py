"""Command line: newsdesk check | run-once | loop | demo | status | pause | resume | live | dryrun | report | feeds."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from .config import load_settings
from .db import DB, iso, utcnow
from .instagram import IGError
from .llm import LLMError, make_llm
from .log import setup_logging
from .pipeline import Pipeline
from .render import render_card
from .telegram import Telegram


def build(args):
    settings = load_settings(args.config)
    setup_logging(settings.data_dir / "logs", settings.secret_values(), verbose=args.verbose)
    db_path = settings.data_dir / "newsdesk.db"
    db = DB(db_path)
    os.chmod(db_path, 0o600)  # holds the refreshed Instagram token
    tg = Telegram(settings.secret("TELEGRAM_BOT_TOKEN"), settings.secret("TELEGRAM_CHAT_ID"))
    return settings, db, Pipeline(settings, db, make_llm(settings, db), tg), tg


def cmd_run_once(args) -> int:
    settings, db, pipe, _ = build(args)
    lock_path = settings.data_dir / "newsdesk.lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another cycle is still running — skipping")
            return 0
        try:
            stats = pipe.run_cycle()
        finally:
            # fold the WAL into the main file so the state can be committed as one file (GitHub mode)
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.conn.close()
    print(json.dumps(stats))
    return 0


def cmd_loop(args) -> int:
    while True:
        try:
            cmd_run_once(args)
        except Exception as exc:  # keep the loop alive; the timer-based setup is preferred in production
            print(f"cycle crashed: {exc}", file=sys.stderr)
        time.sleep(args.every)


def cmd_status(args) -> int:
    _, _, pipe, _ = build(args)
    print(pipe.status_text())
    return 0


def cmd_set(args, key: str, value: str, message: str) -> int:
    _, db, _, tg = build(args)
    db.set(key, value)
    print(message)
    tg.send(message)
    return 0


def cmd_demo(args) -> int:
    settings, _, pipe, _ = build(args)
    samples = json.loads((settings.root / "tests" / "fixtures" / "sample_posts.json").read_text(encoding="utf-8"))
    out_dir = settings.data_dir / "demo"
    for n, s in enumerate(samples, 1):
        for theme in ("light", "dark"):
            brand = dict(pipe.brand, theme=theme)
            path = render_card(s["post"], s["sources"], brand, out_dir / f"sample{n}-{theme}.jpg", settings.root / "fonts")
            print(path)
    return 0


def cmd_feeds(args) -> int:
    """Checks every feed once and reports which work and how recent their newest item is."""
    from .feeds import entry_time, fetch_one
    import feedparser
    settings, db, _, _ = build(args)
    now = utcnow()
    ok = 0
    for f in settings.section("sources").get("feeds", []):
        _, content, _, err = fetch_one(f, None, 15)
        if err or not content:
            print(f"✗ {f['name']:<24} {err or 'empty'}")
            continue
        entries = feedparser.parse(content).entries
        times = [t for t in (entry_time(e) for e in entries) if t]
        newest = f"{int((now - max(times)).total_seconds() // 60)} min ago" if times else "no timestamps!"
        print(f"✓ {f['name']:<24} {len(entries):>3} items, newest {newest}")
        ok += 1
    print(f"\n{ok} feeds working")
    return 0


def cmd_check(args) -> int:
    settings, db, pipe, tg = build(args)
    good = True

    def line(ok: bool, label: str, detail: str = ""):
        nonlocal good
        good &= ok
        print(f"{'✓' if ok else '✗'} {label}" + (f" — {detail}" if detail else ""))

    line(True, "mode", f"{pipe.effective_mode()}{' (PAUSED)' if pipe.paused() else ''}")
    fonts = settings.root / "fonts"
    line(all((fonts / f).exists() for f in ("Inter-Regular.otf", "Inter-SemiBold.otf", "Inter-Bold.otf")), "fonts")
    av = pipe.brand.get("_avatar_abs")
    line(True, "avatar", str(av) if av and Path(av).exists() else "not found — using a lettered circle")

    try:
        res = pipe.llm.complete_json('Reply with JSON {"ok": true}', "ping", temperature=0)
        line(bool(res.get("ok")), f"AI writer ({settings.section('llm').get('provider')})")
    except LLMError as exc:
        line(False, "AI writer", str(exc)[:200])

    try:
        ig = pipe.ig_factory()
        me = ig.me()
        line(me.get("account_type") in ("BUSINESS", "MEDIA_CREATOR", "CREATOR", None),
             "Instagram token", f"@{me.get('username')} ({me.get('account_type')}), IG_USER_ID={me.get('user_id')}")
        line(True, "publishing quota used (24h)", str(ig.publishing_quota_used()))
    except IGError as exc:
        line(pipe.effective_mode() != "live", "Instagram", str(exc)[:200])

    if settings.public_base_url:
        test = settings.public_dir / "healthcheck.jpg"
        render_card({"news": "Health check", "take": "If you can read this, hosting works."}, [], pipe.brand,
                    test, fonts)
        import requests
        try:
            r = requests.head(f"{settings.public_base_url}/healthcheck.jpg", timeout=10)
            line(r.status_code == 200 and "image/jpeg" in r.headers.get("Content-Type", ""),
                 "public image URL", f"HTTP {r.status_code} {r.headers.get('Content-Type')}")
        except requests.RequestException as exc:
            line(False, "public image URL", type(exc).__name__)
    else:
        line(pipe.effective_mode() != "live", "public image URL", "PUBLIC_BASE_URL not set (needed for live)")

    line(tg.enabled, "Telegram", "connected — test message sent" if tg.enabled else "not set (no previews/alerts)")
    tg.send("👋 Newsdesk check ran. Commands: /status /pause /resume /live /dryrun")
    print("\nAll good." if good else "\nFix the ✗ items above.")
    return 0 if good else 1


def cmd_telegram_id(args) -> int:
    """Message your bot once from your phone, then run this to find your chat id."""
    import requests
    settings = load_settings(args.config)
    token = settings.secret("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first.")
        return 1
    updates = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20).json().get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in updates if "message" in u}
    if not chats:
        print("No messages yet — open your bot in Telegram, send it 'hi', then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
    return 0


def cmd_report(args) -> int:
    settings, db, _, tg = build(args)
    rows = db.conn.execute(
        "SELECT p.id, p.permalink, p.published_at, p.package FROM posts p WHERE status='published' "
        "ORDER BY published_at DESC LIMIT 60").fetchall()
    if not rows:
        print("No published posts yet.")
        return 0
    out, by_cat = [], {}
    for r in rows:
        m = {x["metric"]: x["value"] for x in db.conn.execute("SELECT metric, value FROM metrics WHERE post_id=?",
                                                               (r["id"],))}
        pkg = json.loads(r["package"])
        reach = m.get("reach") or 0
        engaged = (m.get("shares", 0) + m.get("saved", 0)) / reach * 100 if reach else 0
        cat = pkg.get("category") or "?"
        by_cat.setdefault(cat, []).append(engaged)
        out.append((engaged, reach, m, cat, pkg["post"]["take"][:70], r["permalink"]))
    out.sort(key=lambda x: x[0], reverse=True)
    lines = ["Top posts by (shares + saves) / reach:"]
    for engaged, reach, m, cat, take, link in out[:10]:
        lines.append(f"{engaged:5.1f}%  reach {int(reach):>6}  shares {int(m.get('shares', 0)):>4}  "
                     f"saves {int(m.get('saved', 0)):>4}  [{cat}] {take}  {link or ''}")
    lines.append("\nBy category (avg share+save rate):")
    for cat, vals in sorted(by_cat.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        lines.append(f"  {cat:<28} {sum(vals) / len(vals):5.1f}%  ({len(vals)} posts)")
    text = "\n".join(lines)
    print(text)
    if args.telegram:
        tg.send("📊 Weekly report\n\n" + text[:3800])
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="newsdesk")
    p.add_argument("--config")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run-once", help="one monitoring cycle (what the timer runs every minute)")
    lp = sub.add_parser("loop", help="run cycles forever (for testing on a laptop)")
    lp.add_argument("--every", type=int, default=60)
    sub.add_parser("check", help="test every connection and setting")
    sub.add_parser("feeds", help="test every news feed")
    sub.add_parser("demo", help="render sample cards offline")
    sub.add_parser("telegram-id", help="print your Telegram chat id")
    sub.add_parser("status")
    sub.add_parser("pause", help="emergency stop for publishing")
    sub.add_parser("resume")
    sub.add_parser("live", help="switch to automatic publishing")
    sub.add_parser("dryrun", help="switch to previews only")
    rp = sub.add_parser("report", help="performance report")
    rp.add_argument("--telegram", action="store_true")
    args = p.parse_args(argv)

    handlers = {
        "run-once": cmd_run_once, "loop": cmd_loop, "check": cmd_check, "feeds": cmd_feeds, "demo": cmd_demo,
        "status": cmd_status, "report": cmd_report, "telegram-id": cmd_telegram_id,
        "pause": lambda a: cmd_set(a, "paused", "1", "⏸ Publishing paused."),
        "resume": lambda a: cmd_set(a, "paused", "0", "▶️ Publishing resumed."),
        "live": lambda a: cmd_set(a, "mode_override", "live", "🔴 LIVE mode: posts publish automatically."),
        "dryrun": lambda a: cmd_set(a, "mode_override", "dry_run", "🧪 Dry-run mode: previews only."),
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
