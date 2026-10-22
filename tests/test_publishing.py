"""Publishing flow, caps, kill switches, token expiry, double-post protection, rendering."""
from datetime import timedelta

import pytest
from PIL import Image

from conftest import PASS, T0, Clock, FakeIG, good_package, make_item
from newsdesk.instagram import IGError, Instagram


TITLES = ["Brew Co rebrand swaps cup for crown logo", "Zepto mascot campaign launches with talking onion",
          "Spotify Wrapped ad campaign goes viral worldwide", "Nike unveils sneaker collab with chess club",
          "Amul topical ad trolls airline delays"]


def story(clock, n=0):
    return [make_item(TITLES[n], "Meta Newsroom", "about.fb.com", "primary", clock() - timedelta(minutes=1), clock())]


def test_live_publish_happy_path(make_pipeline):
    ig = FakeIG()
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS], ig=ig, mode="live",
                                     batches=[story(clock)], clock=clock)
    stats = pipe.run_cycle()
    assert stats["posted"] == 1
    post = pipe.db.conn.execute("SELECT * FROM posts").fetchone()
    assert post["status"] == "published" and post["permalink"].startswith("https://www.instagram.com/")
    assert post["image_url"].startswith("https://riya.example/p/") and post["image_url"].endswith(".jpg")
    assert ("comment", "Next update: the crown gets a latte.") in ig.calls
    assert any("Published" in c for _, c in tg.photos)


def test_dry_run_never_touches_instagram(make_pipeline):
    ig = FakeIG()
    clock = Clock()
    pipe, *_ = make_pipeline(responses=[good_package(), PASS], ig=ig, mode="dry_run", batches=[story(clock)], clock=clock)
    assert pipe.run_cycle()["previews"] == 1
    assert ig.calls == []


@pytest.mark.parametrize("switch", ["file", "telegram"])
def test_kill_switch_stops_everything(make_pipeline, switch):
    ig = FakeIG()
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS], ig=ig, mode="live", batches=[story(clock)],
                                     clock=clock)
    if switch == "file":
        (pipe.s.data_dir / "PAUSE").touch()
    else:
        tg.queued = ["/pause"]
    pipe.run_cycle()
    assert ig.calls == [] and llm.calls == []


def test_min_gap_holds_story_without_spending_ai_calls(make_pipeline):
    ig = FakeIG()
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS] * 2, ig=ig, mode="live",
                                     batches=[story(clock, 0)], clock=clock)
    assert pipe.run_cycle()["posted"] == 1
    clock.advance(10)
    pipe.fetch_fn = lambda s, d, now: story(clock, 1)
    pipe.run_cycle()
    assert ig.published == 1 and len(llm.calls) == 2           # held back by the 90-min gap, no AI spend
    status = pipe.db.conn.execute("SELECT status FROM clusters ORDER BY id DESC LIMIT 1").fetchone()["status"]
    assert status == "pending"


def test_daily_cap(make_pipeline):
    ig = FakeIG()
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS] * 5, ig=ig, mode="live", clock=clock)
    for n in range(4):
        pipe.fetch_fn = lambda s, d, now, n=n: story(clock, n)
        pipe.run_cycle()
        clock.advance(100)
    assert ig.published == 3                                    # cap = 3 per rolling 24h
    status = pipe.db.conn.execute("SELECT status FROM clusters ORDER BY id DESC LIMIT 1").fetchone()["status"]
    assert status == "skipped_cap"
    # a day later the cap frees up again
    clock.advance(24 * 60)
    pipe.fetch_fn = lambda s, d, now: story(clock, 4)
    pipe.run_cycle()
    assert ig.published == 4


def test_token_expiry_pauses_and_alerts(make_pipeline):
    ig = FakeIG(expired=True)
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS], ig=ig, mode="live", batches=[story(clock)],
                                     clock=clock)
    pipe.run_cycle()
    assert pipe.paused()
    assert any("token expired" in m for m in tg.messages)


def test_instagram_failure_is_reported_not_retried_forever(make_pipeline):
    ig = FakeIG(fail_container=1)
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS], ig=ig, mode="live", batches=[story(clock)],
                                     clock=clock)
    pipe.run_cycle()
    post = pipe.db.conn.execute("SELECT status, error FROM posts").fetchone()
    assert post["status"] == "failed" and any("Publishing failed" in m for m in tg.messages)


# ── real Instagram client against a fake HTTP session ─────────────────────────
class Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.log = []

    def _next(self, method, url, payload):
        self.log.append((method, url.split("graph.instagram.com/")[-1], dict(payload or {})))
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def get(self, url, params=None, timeout=None):
        return self._next("GET", url, params)

    def post(self, url, data=None, timeout=None):
        return self._next("POST", url, data)


def test_publish_timeout_does_not_double_post():
    import requests
    caption = "Brew Co caption here #rebrand"
    sess = FakeSession([
        requests.ConnectionError("timeout"),                              # media_publish: unclear outcome
        Resp(200, {"status_code": "PUBLISHED", "id": "C1"}),               # container says it went live
        Resp(200, {"data": [{"id": "M9", "caption": caption}]}),          # find the media id
    ])
    ig = Instagram("123", "TOKEN", session=sess, sleep=lambda s: None)
    assert ig.publish_container("C1", caption) == "M9"
    assert sum(1 for m, path, _ in sess.log if path.endswith("media_publish")) == 1


def test_container_retries_transient_errors_and_strips_alt_text():
    sess = FakeSession([
        Resp(500, {"error": {"code": 2, "message": "temporary", "type": "OAuthException"}}),
        Resp(400, {"error": {"code": 100, "message": "Invalid parameter alt_text", "type": "OAuthException"}}),
        Resp(200, {"id": "C7"}),
    ])
    ig = Instagram("123", "TOKEN", session=sess, sleep=lambda s: None)
    assert ig.create_image_container("https://x/y.jpg", "cap", alt_text="desc") == "C7"
    assert "alt_text" not in sess.log[-1][2]


def test_token_never_appears_in_logs(env, caplog):
    from newsdesk.log import RedactFilter
    f = RedactFilter(["SUPERSECRETTOKEN123"])
    assert "SUPERSECRET" not in f.redact("GET /me?access_token=SUPERSECRETTOKEN123&fields=id")
    assert "***" in f.redact("https://api.telegram.org/bot123456:ABCdef_ghi/sendMessage")


def test_card_render_spec(tmp_path, env):
    settings, _ = env
    from newsdesk.render import render_card
    long_post = {"news": "A very long news line " * 9, "take": "And an equally long punchline that keeps going " * 3}
    brand = dict(settings.section("brand"))
    path = render_card(long_post, ["Adweek"], brand, tmp_path / "c.jpg", settings.root / "fonts")
    im = Image.open(path)
    assert im.format == "JPEG" and im.size == (1080, 1350)                 # 4:5 portrait, Instagram-legal
    assert path.stat().st_size < 8 * 1024 * 1024


def test_token_refresh_weekly_and_metrics_collected(make_pipeline, monkeypatch):
    from newsdesk import tokens
    monkeypatch.setenv("IG_ACCESS_TOKEN", "ORIGINAL-SECRET-TOKEN")
    ig = FakeIG()
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS], ig=ig, mode="live",
                                     batches=[story(clock, 0)], clock=clock)
    pipe.run_cycle()                       # publishes; starts the token clock
    assert ("refresh", None) not in ig.calls
    clock.advance(8 * 24 * 60)
    pipe.run_cycle()
    assert ("refresh", None) in ig.calls
    assert tokens.load(pipe.db, "ORIGINAL-SECRET-TOKEN") == "NEWTOKEN"          # readable with the secret
    dump = "\n".join(pipe.db.conn.iterdump())
    assert "NEWTOKEN" not in dump                                                # never stored in plain text
    assert tokens.load(pipe.db, "someone-elses-guess") is None
    m = dict(pipe.db.conn.execute("SELECT metric, value FROM metrics").fetchall())
    assert m["reach"] == 1000 and m["shares"] == 40


def test_git_hosting_pushes_then_waits_for_public_url(tmp_path, monkeypatch):
    """GitHub mode: image is committed + pushed, and we wait until the public URL actually serves it."""
    import subprocess
    from newsdesk import hosting
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True)
    for k, v in (("user.email", "bot@example.com"), ("user.name", "bot")):
        subprocess.run(["git", "config", k, v], cwd=repo, check=True)
    (repo / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=repo, check=True)
    card = tmp_path / "card.jpg"
    Image.new("RGB", (10, 10)).save(card)

    calls = {"n": 0}

    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {"Content-Type": "image/jpeg"}

    def fake_head(url, timeout=None, allow_redirects=None):
        calls["n"] += 1
        return R(404 if calls["n"] < 3 else 200)          # appears on the 3rd check

    monkeypatch.setattr(hosting.requests, "head", fake_head)
    url = hosting.publish_image(card, repo / "public", "https://raw.example/u/r/main/public", verify=True,
                                git_push=True, sleep=lambda s: None)
    assert url.startswith("https://raw.example/u/r/main/public/") and calls["n"] == 3
    log = subprocess.run(["git", "log", "--oneline", "origin/HEAD", "-1"], cwd=repo, capture_output=True,
                         text=True).stdout
    pushed = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline", "-1"], capture_output=True,
                            text=True).stdout
    assert "post image" in pushed
