"""Group articles about the same story, and spot repeats of stories already covered."""
from __future__ import annotations

import re

STOPWORDS = set("""
a an the and or but if of on in at to for from by with without into onto over under about after before as is are was
were be been being has have had do does did will would can could should may might must its it this that these those
their his her they them he she we you your our i me my mine new says said say report reports reported amid after
here there what why how when who whom which while than then just now also more most less very not no yes vs via
up down out off again still yet one two three first last week today tonight year years day days time live update
updates latest breaking watch video photos exclusive inside full read know need wants want gets get got make makes
made top best big huge viral internet netizens users fans people check out
""".split())

_WORD = re.compile(r"[a-z0-9][a-z0-9'&.+-]*")


def tokens(text: str) -> set[str]:
    words = _WORD.findall(text.lower())
    out = set()
    for w in words:
        w = w.strip(".'-")
        if w.endswith("'s"):
            w = w[:-2]
        if len(w) >= 3 and w not in STOPWORDS and not w.isdigit():
            out.add(w)
    return out


def similarity(a: set[str], b: set[str]) -> tuple[float, int]:
    """Overlap coefficient — robust when one headline is much shorter than the other."""
    if not a or not b:
        return 0.0, 0
    shared = len(a & b)
    return shared / min(len(a), len(b)), shared


def same_story(a: set[str], b: set[str], min_overlap: float = 0.5, min_shared: int = 3) -> bool:
    sim, shared = similarity(a, b)
    return sim >= min_overlap and shared >= min_shared


def best_match(item_tokens: set[str], candidates: list[tuple[int, set[str]]]) -> int | None:
    best_id, best_sim = None, 0.0
    for cid, ctoks in candidates:
        if same_story(item_tokens, ctoks):
            sim, _ = similarity(item_tokens, ctoks)
            if sim > best_sim:
                best_id, best_sim = cid, sim
    return best_id
