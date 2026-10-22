"""Credibility gate, keyword pre-filter, recency points, and the final 0–100 score."""
from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache


def credibility(items, allow_single_trusted: bool = False) -> tuple[int, str]:
    """20 = verified: an official primary source, or 2+ independent trusted outlets.
    18 = one trusted outlet (only counts when allow_single_trusted; contested stories still need 20)."""
    primary = {i["domain"] for i in items if i["tier"] == "primary"}
    trusted = {i["domain"] for i in items if i["tier"] == "trusted"}
    unknown = {i["domain"] for i in items if i["tier"] == "unknown"}
    if primary:
        return 20, f"official source ({', '.join(sorted(primary))})"
    if len(trusted) >= 2:
        return 20, f"{len(trusted)} independent trusted outlets"
    if len(trusted) == 1 and allow_single_trusted:
        return 18, f"1 trusted outlet ({next(iter(trusted))})"
    if len(trusted) == 1 and unknown:
        return 12, "1 trusted outlet + unverified outlets"
    if len(trusted) == 1:
        return 10, "only 1 trusted outlet so far"
    return 0, "no trusted source"


@lru_cache(maxsize=2048)
def _term_re(term: str) -> re.Pattern:
    """Whole-word match; a trailing * means 'starts with' (apolog* → apology, apologises)."""
    term = term.strip().lower()
    if term.endswith("*"):
        return re.compile(r"\b" + re.escape(term[:-1]))
    return re.compile(r"\b" + re.escape(term) + r"\b")


def blocked_terms(text: str, blocked: list[str]) -> list[str]:
    t = text.lower()
    return [b for b in blocked if _term_re(b).search(t)]


def keyword_score(text: str, niche: dict) -> int:
    t = text.lower()
    return sum(int(weight) for weight, words in niche.items() for w in words if _term_re(w).search(t))


def recency_points(first_published: datetime, now: datetime, full_minutes: int = 10) -> int:
    """25 points while the story is within `full_minutes`, then fading to 0 at 3x that age."""
    minutes = (now - first_published).total_seconds() / 60
    if minutes <= full_minutes:
        return 25
    if minutes >= 3 * full_minutes:
        return 0
    return int(25 * (1 - (minutes - full_minutes) / (2 * full_minutes)))


def total_score(recency: int, credibility_pts: int, llm_scores: dict) -> int:
    rel = max(0, min(25, int(llm_scores.get("relevance", 0))))
    interest = max(0, min(20, int(llm_scores.get("interest", 0))))
    wit = max(0, min(10, int(llm_scores.get("wit", 0))))
    return recency + credibility_pts + rel + interest + wit
