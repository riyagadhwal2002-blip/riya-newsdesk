"""Freshness, parsing, clustering and duplicate detection."""
from datetime import timedelta
from pathlib import Path

from conftest import T0, Clock, good_package, make_item, PASS
from newsdesk import cluster
from newsdesk.feeds import canonical_url, is_fresh, parse_feed
from newsdesk.scoring import blocked_terms, keyword_score

FIX = Path(__file__).parent / "fixtures"


def test_freshness_window():
    assert is_fresh(T0 - timedelta(minutes=3), T0, 5, 2)
    assert not is_fresh(T0 - timedelta(minutes=6), T0, 5, 2)          # too old
    assert not is_fresh(T0 + timedelta(minutes=10), T0, 5, 2)         # suspicious future date
    assert is_fresh(T0 + timedelta(minutes=1), T0, 5, 2)              # small clock skew tolerated


def test_parse_feed_rules(env):
    settings, _ = env
    content = (FIX / "feed.xml").read_bytes()
    items = parse_feed(content, {"name": "Adweek", "tier": "trusted"}, settings.section("sources"), T0,
                       settings.section("discovery"))
    titles = [i["title"] for i in items]
    assert "Item without a date must be ignored" not in titles          # no timestamp → can't prove fresh
    assert "Old story from this morning about ad budgets" not in titles  # older than 6h → ignored
    brew = items[0]
    assert brew["fresh"] is True                                          # published 13:27, seen 13:30
    assert "utm_" not in brew["url"]                                      # tracking stripped for dedupe


def test_google_news_outlet_and_tier(env):
    settings, _ = env
    items = parse_feed((FIX / "google_news.xml").read_bytes(), {"name": "GN", "tier": "google_news"},
                       settings.section("sources"), T0, settings.section("discovery"))
    et, blog = items
    assert et["source"] == "The Economic Times" and et["tier"] == "trusted"
    assert et["title"] == "Brew Co's crown logo sparks memes"           # " - Outlet" suffix removed
    assert blog["tier"] == "unknown"                                      # unknown outlets never verify a story


def test_canonical_url_dedupes_tracking():
    a = canonical_url("https://www.adweek.com/x/?utm_source=rss&id=7#top")
    b = canonical_url("https://www.adweek.com/x?id=7")
    assert a == b


def test_same_story_clustering():
    a = cluster.tokens("Brew Co rebrand: new logo swaps the cup for a crown")
    b = cluster.tokens("Brew Co's crown logo sparks memes as rebrand drops cup")
    c = cluster.tokens("Netflix raises subscription prices in India")
    assert cluster.same_story(a, b)
    assert not cluster.same_story(a, c)


def test_keywords_whole_words():
    niche = {3: ["ad", "apolog*"], 1: ["ai"]}
    assert keyword_score("Brand apologises for new ad", niche) == 6
    assert keyword_score("Officials said the plan was maintained", niche) == 0   # "ai" inside words ignored
    assert blocked_terms("Deadline for the campaign extended", ["dead"]) == []
    assert blocked_terms("Actor dead at 52", ["dead"]) == ["dead"]


def test_old_story_retrending_is_not_a_candidate(make_pipeline):
    clock = Clock()
    old = make_item("Brew Co rebrand: new logo swaps the cup for a crown", "Adweek", "adweek.com", "trusted",
                    T0 - timedelta(hours=3), T0)
    pipe, *_ = make_pipeline(batches=[[old]], clock=clock)
    pipe.run_cycle()
    # an hour later a fresh article about the same old story appears
    clock.advance(60)
    new = make_item("Brew Co's crown logo rebrand cup swap still trending", "Variety", "variety.com", "trusted",
                    clock() - timedelta(minutes=1), clock())
    pipe.fetch_fn = lambda s, d, n: [new]
    pipe.run_cycle()
    statuses = [r["status"] for r in pipe.db.conn.execute("SELECT status FROM clusters")]
    assert statuses == ["stale"]                                           # joined the old cluster, nothing pending


def test_duplicate_of_recent_post_is_skipped(make_pipeline):
    clock = Clock()
    a = make_item("Brew Co rebrand: new logo swaps the cup for a crown", "Adweek", "adweek.com", "trusted",
                  T0 - timedelta(minutes=2), T0)
    b = make_item("Brew Co new crown logo replaces cup in rebrand", "TechCrunch", "techcrunch.com", "trusted",
                  T0 - timedelta(minutes=1), T0)
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS], batches=[[a, b]], clock=clock)
    assert pipe.run_cycle()["previews"] == 1
    # three days later (cluster lookback passed) the same story resurfaces as "fresh"
    clock.advance(3 * 24 * 60)
    c = make_item("Brew Co crown logo rebrand swaps cup, fans react", "Variety", "variety.com", "trusted",
                  clock() - timedelta(minutes=1), clock())
    pipe.fetch_fn = lambda s, d, n: [c]
    pipe.run_cycle()
    row = pipe.db.conn.execute("SELECT status FROM clusters ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "skipped_duplicate"
    assert len(llm.calls) == 2                                             # no AI calls spent on the repeat


def test_fetch_all_survives_failing_feeds(env, monkeypatch):
    """Real fetch path (threads + cache): one feed works, one times out, one returns 500."""
    import requests
    from newsdesk import feeds as feeds_mod
    settings, db = env
    settings.raw["sources"]["feeds"] = [
        {"name": "Good", "url": "https://good.example/feed", "tier": "trusted"},
        {"name": "Slow", "url": "https://slow.example/feed", "tier": "trusted"},
        {"name": "Broken", "url": "https://broken.example/feed", "tier": "trusted"},
    ]

    class R:
        def __init__(self, code, content=b""):
            self.status_code, self.content, self.headers = code, content, {"ETag": "abc"}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"{self.status_code}")

    def fake_get(url, headers=None, timeout=None):
        if "slow" in url:
            raise requests.Timeout("timed out")
        if "broken" in url:
            return R(500)
        return R(200, (FIX / "feed.xml").read_bytes())

    monkeypatch.setattr(feeds_mod.requests, "get", fake_get)
    items = feeds_mod.fetch_all(settings, db, T0)
    assert [i["source"] for i in items] == ["Good"]
    assert db.feed_cache("https://slow.example/feed")["fail_count"] == 1
    assert db.feed_cache("https://good.example/feed")["etag"] == "abc"
