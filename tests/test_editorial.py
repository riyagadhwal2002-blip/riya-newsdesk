"""Verification gate, sensitivity, scoring threshold and fact-checking."""
from datetime import timedelta

from conftest import PASS, T0, Clock, good_package, make_item, SOURCE_SUMMARY
from newsdesk.factcheck import check_package
from newsdesk.llm import LLMError

BLOCKED = ["dead", "election"]


def brew(source, domain, tier, minutes_ago=2, title=None, clock=None):
    now = clock() if clock else T0
    return make_item(title or f"Brew Co rebrand: new logo swaps the cup for a crown ({source})", source, domain,
                     tier, now - timedelta(minutes=minutes_ago), now)


def cluster_status(pipe):
    return pipe.db.conn.execute("SELECT status, reason FROM clusters ORDER BY id DESC LIMIT 1").fetchone()


def test_single_source_waits_then_publishes_once_corroborated(make_pipeline):
    clock = Clock()
    pipe, llm, tg, _ = make_pipeline(responses=[good_package(), PASS], batches=[[brew("Adweek", "adweek.com", "trusted")]],
                                     clock=clock)
    pipe.run_cycle()
    assert cluster_status(pipe)["status"] == "pending" and llm.calls == []   # waiting, no AI spend
    clock.advance(10)
    second = brew("TechCrunch", "techcrunch.com", "trusted", minutes_ago=1, clock=clock,
                  title="Brew Co swaps cup for crown in new logo rebrand")
    pipe.fetch_fn = lambda s, d, n: [second]
    stats = pipe.run_cycle()
    assert stats["previews"] == 1 and cluster_status(pipe)["status"] == "dry_run"


def test_unknown_outlets_never_verify(make_pipeline):
    clock = Clock()
    items = [brew("Blog A", "a.example", "unknown"), brew("Blog B", "b.example", "unknown", title="Brew Co crown logo rebrand cup swap")]
    pipe, llm, *_ = make_pipeline(batches=[items], clock=clock)
    pipe.run_cycle()
    clock.advance(61)
    pipe.run_cycle()
    assert cluster_status(pipe)["status"] == "expired_unverified" and llm.calls == []


def test_primary_source_alone_is_enough(make_pipeline):
    pipe, *_ = make_pipeline(responses=[good_package(), PASS],
                             batches=[[brew("Meta Newsroom", "about.fb.com", "primary")]])
    assert pipe.run_cycle()["previews"] == 1


def test_sensitive_story_blocked_before_ai(make_pipeline):
    item = make_item("Brand ambassador found dead, campaign paused", "Variety", "variety.com", "trusted",
                     T0 - timedelta(minutes=1), T0)
    pipe, llm, *_ = make_pipeline(batches=[[item]])
    pipe.run_cycle()
    assert cluster_status(pipe)["status"] == "blocked" and llm.calls == []


def test_low_score_rejected(make_pipeline):
    weak = good_package(scores={"relevance": 10, "interest": 8, "wit": 3})
    pipe, *_ = make_pipeline(responses=[weak], batches=[[brew("Meta Newsroom", "about.fb.com", "primary")]])
    pipe.run_cycle()
    s = cluster_status(pipe)
    assert s["status"] == "rejected" and "score" in s["reason"]


def test_ai_editor_can_skip(make_pipeline):
    skip = good_package(relevant=False, skip_reason="no marketing angle")
    pipe, *_ = make_pipeline(responses=[skip], batches=[[brew("Meta Newsroom", "about.fb.com", "primary")]])
    pipe.run_cycle()
    assert cluster_status(pipe)["reason"] == "AI editor: no marketing angle"


def test_ai_fact_checker_can_veto(make_pipeline):
    pipe, *_ = make_pipeline(responses=[good_package(), {"verdict": "fail", "problems": ["cost not confirmed"]}],
                             batches=[[brew("Meta Newsroom", "about.fb.com", "primary")]])
    pipe.run_cycle()
    s = cluster_status(pipe)
    assert s["status"] == "rejected_factcheck" and "cost not confirmed" in s["reason"]


def test_llm_outage_retries_then_gives_up(make_pipeline):
    clock = Clock()
    errs = [LLMError("503")] * 9
    pipe, llm, *_ = make_pipeline(responses=errs, batches=[[brew("Meta Newsroom", "about.fb.com", "primary")]],
                                  clock=clock)
    for _ in range(3):
        pipe.run_cycle()          # never crashes
        clock.advance(1)
    assert cluster_status(pipe)["status"] == "llm_error"


# ── deterministic fact checks ────────────────────────────────────────────────
SRC = "[Adweek] Brew Co rebrand. " + SOURCE_SUMMARY
TITLES = ["Brew Co rebrand: new logo swaps the cup for a crown"]


def test_good_package_passes():
    assert check_package(good_package(), SRC, TITLES, BLOCKED) == []


def test_invented_number_fails():
    pkg = good_package(news="Brew Co spent 5 million dollars replacing its cup logo with a crown.")
    assert any("numbers not in sources" in p for p in check_package(pkg, SRC, TITLES, BLOCKED))


def test_invented_quote_fails():
    pkg = good_package(caption='The CEO said "we are a royal brand now" and nobody asked. Thoughts?')
    assert any("quote not found" in p for p in check_package(pkg, SRC, TITLES, BLOCKED))


def test_fake_evidence_fails():
    pkg = good_package(claims=[{"text": "sales doubled", "evidence": "sales doubled after the rebrand"}])
    assert any("evidence not found" in p for p in check_package(pkg, SRC, TITLES, BLOCKED))


def test_copied_headline_fails():
    pkg = good_package(news="Brew Co rebrand: new logo swaps the cup for a crown")
    assert any("copies a source headline" in p for p in check_package(pkg, SRC, TITLES, BLOCKED))


def test_blocked_topic_in_ai_text_fails():
    pkg = good_package(take="This rebrand is dead on arrival, like the election promises.")
    assert any("blocked topics" in p for p in check_package(pkg, SRC, TITLES, BLOCKED))
