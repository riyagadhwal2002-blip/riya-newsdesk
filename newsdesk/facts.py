"""'Did you know?' posts from facts.yaml. Every fact is re-checked against its Wikipedia page before posting."""
from __future__ import annotations

from pathlib import Path

import requests
import yaml

from .factcheck import norm

WIKI_API = "https://en.wikipedia.org/w/api.php"
UA = "NewsdeskBot/1.0 (personal Instagram page; fact checking)"


def load(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []


def wiki_text(title: str, session=None) -> str:
    http = session or requests
    r = http.get(WIKI_API, timeout=20, headers={"User-Agent": UA}, params={
        "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1, "format": "json", "titles": title})
    pages = (r.json().get("query") or {}).get("pages") or {}
    return "\n".join(p.get("extract", "") for p in pages.values())


def verified(fact: dict, page_text: str) -> tuple[bool, list[str]]:
    text = norm(page_text)
    missing = [w for w in fact.get("must_contain", []) if norm(w) not in text]
    return (bool(page_text) and not missing), missing


def next_fact(all_facts: list[dict], used: set[str]) -> dict | None:
    for f in all_facts:
        if f["id"] not in used:
            return f
    return None


def load_content(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
