"""Deterministic safety checks that run before (and in addition to) the AI fact-checker."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .scoring import blocked_terms

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_QUOTE = re.compile(r"[\"“”]([^\"“”]{12,})[\"“”]")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_emoji(s: str) -> str:
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", s or "")).strip()


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM.findall(text or "")}


def evidence_supported(evidence: str, source: str) -> bool:
    e, s = norm(evidence), norm(source)
    if len(e) < 8:
        return False
    if e in s:
        return True
    words = re.findall(r"[a-z0-9]+", e)
    src_words = set(re.findall(r"[a-z0-9]+", s))
    return bool(words) and sum(w in src_words for w in words) / len(words) >= 0.9


def check_package(pkg: dict, source_text: str, source_titles: list[str], blocked: list[str]) -> list[str]:
    """Returns a list of problems. Empty list = passed."""
    problems: list[str] = []
    post = pkg.get("post") or {}
    news, take = post.get("news", ""), post.get("take", "")
    caption, insight, comment = post.get("caption", ""), post.get("insight", ""), post.get("first_comment", "")
    src = norm(source_text)
    src_numbers = _numbers(src)

    # 1. required pieces and sizes
    if not news or not take or not caption:
        problems.append("missing news, take or caption")
    if len(news) > 240:
        problems.append(f"news line too long ({len(news)} chars)")
    if len(take) > 180:
        problems.append(f"take too long ({len(take)} chars)")
    tags = post.get("hashtags") or []
    if not (3 <= len(tags) <= 8) or any(not str(t).startswith("#") or " " in str(t) for t in tags):
        problems.append("hashtags must be 3-8 single-word tags starting with #")
    if len(caption) > 2000:
        problems.append("caption too long")

    # 2. every number must exist in the sources (small numbers allowed in the joke only)
    for field, text in (("news", news), ("caption", caption), ("insight", insight), ("first comment", comment)):
        missing = sorted(_numbers(text) - src_numbers)
        if missing:
            problems.append(f"{field} uses numbers not in sources: {', '.join(missing)}")
    joke_missing = [n for n in _numbers(take) - src_numbers if not (n.isdigit() and int(n) <= 10)]
    if joke_missing:
        problems.append(f"take uses numbers not in sources: {', '.join(sorted(joke_missing))}")

    # 3. no invented quotes
    for field, text in (("news", news), ("take", take), ("caption", caption), ("first comment", comment)):
        for q in _QUOTE.findall(text):
            if len(q.split()) >= 3 and norm(q) not in src:
                problems.append(f"{field} contains a quote not found in sources: “{q[:60]}”")

    # 4. claims must carry real evidence
    claims = pkg.get("claims") or []
    if not claims:
        problems.append("no claims/evidence returned")
    for c in claims:
        if not evidence_supported(str(c.get("evidence", "")), source_text):
            problems.append(f"evidence not found in sources for claim: {str(c.get('text', ''))[:80]}")

    # 5. own words, not the outlet's headline
    for t in source_titles:
        if SequenceMatcher(None, norm(news), norm(t)).ratio() >= 0.85:
            problems.append("news line copies a source headline")
            break

    # 6. sensitive topics never slip through via the AI's wording
    hits = blocked_terms(" ".join([news, take, caption, insight, comment]), blocked)
    if hits:
        problems.append(f"post mentions blocked topics: {', '.join(hits)}")
    return problems
