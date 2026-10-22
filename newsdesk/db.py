"""SQLite storage: seen items, story clusters, posts, state, metrics."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    source TEXT,
    domain TEXT,
    tier TEXT,
    published_at TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    fresh INTEGER NOT NULL DEFAULT 0,
    cluster_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    tokens TEXT NOT NULL,
    first_published_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    fresh_born INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,          -- pending | stale | blocked | expired_unverified | rejected | rejected_factcheck
                                   -- | skipped_cap | skipped_duplicate | llm_error | ready | posted | dry_run | failed
    reason TEXT,
    score REAL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clusters_status ON clusters(status);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    cluster_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- dry_run | publishing | published | failed
    tokens TEXT NOT NULL,
    package TEXT NOT NULL,
    image_path TEXT,
    image_url TEXT,
    ig_container_id TEXT,
    ig_media_id TEXT,
    permalink TEXT,
    published_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS feed_cache (
    url TEXT PRIMARY KEY, etag TEXT, modified TEXT, last_ok TEXT, last_error TEXT, fail_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS metrics (
    post_id INTEGER, fetched_at TEXT, metric TEXT, value REAL,
    PRIMARY KEY (post_id, metric)
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


class DB:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # state ---------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, value))
        self.conn.commit()

    def incr(self, key: str) -> int:
        n = int(self.get(key, "0") or 0) + 1
        self.set(key, str(n))
        return n

    # items ---------------------------------------------------------------
    def item_exists(self, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone() is not None

    def add_item(self, item: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO items(url,title,summary,source,domain,tier,published_at,discovered_at,fresh) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (item["url"], item["title"], item.get("summary", ""), item.get("source"), item.get("domain"),
             item.get("tier"), item["published_at"], item["discovered_at"], int(item.get("fresh", False))))
        self.conn.commit()
        return cur.lastrowid

    def set_item_cluster(self, item_id: int, cluster_id: int) -> None:
        self.conn.execute("UPDATE items SET cluster_id=? WHERE id=?", (cluster_id, item_id))
        self.conn.commit()

    def cluster_items(self, cluster_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM items WHERE cluster_id=? ORDER BY published_at", (cluster_id,)).fetchall()

    # clusters ------------------------------------------------------------
    def add_cluster(self, title: str, tokens: list[str], first_published_at: str, fresh_born: bool, status: str,
                    now: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO clusters(title,tokens,first_published_at,created_at,updated_at,fresh_born,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (title, json.dumps(sorted(tokens)), first_published_at, now, now, int(fresh_born), status))
        self.conn.commit()
        return cur.lastrowid

    def recent_clusters(self, since_iso: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clusters WHERE created_at >= ?", (since_iso,)).fetchall()

    def merge_cluster_tokens(self, cluster_id: int, tokens: list[str], first_published_at: str, now: str) -> None:
        row = self.conn.execute("SELECT tokens, first_published_at FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        merged = sorted(set(json.loads(row["tokens"])) | set(tokens))
        first = min(row["first_published_at"], first_published_at)
        self.conn.execute("UPDATE clusters SET tokens=?, first_published_at=?, updated_at=? WHERE id=?",
                          (json.dumps(merged), first, now, cluster_id))
        self.conn.commit()

    def clusters_with_status(self, status: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clusters WHERE status=? ORDER BY created_at", (status,)).fetchall()

    def update_cluster(self, cluster_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE clusters SET {cols} WHERE id=?", (*fields.values(), cluster_id))
        self.conn.commit()

    def get_cluster(self, cluster_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()

    # posts ---------------------------------------------------------------
    def add_post(self, cluster_id: int, status: str, tokens: list[str], package: dict, now: str,
                 image_path: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO posts(cluster_id,created_at,status,tokens,package,image_path) VALUES(?,?,?,?,?,?)",
            (cluster_id, now, status, json.dumps(sorted(tokens)), json.dumps(package, ensure_ascii=False), image_path))
        self.conn.commit()
        return cur.lastrowid

    def update_post(self, post_id: int, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))
        self.conn.commit()

    def published_since(self, since_iso: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM posts WHERE status IN ('published','publishing') AND created_at >= ? ORDER BY created_at",
            (since_iso,)).fetchall()

    def posts_since(self, since_iso: str, statuses=("published", "publishing", "dry_run")) -> list[sqlite3.Row]:
        q = f"SELECT * FROM posts WHERE status IN ({','.join('?' * len(statuses))}) AND created_at >= ?"
        return self.conn.execute(q, (*statuses, since_iso)).fetchall()

    # feeds ---------------------------------------------------------------
    def feed_cache(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM feed_cache WHERE url=?", (url,)).fetchone()

    def save_feed_cache(self, url: str, etag: str | None, modified: str | None, ok: bool, error: str | None,
                        now: str) -> None:
        prev = self.feed_cache(url)
        fails = 0 if ok else ((prev["fail_count"] if prev else 0) + 1)
        self.conn.execute(
            "INSERT INTO feed_cache(url,etag,modified,last_ok,last_error,fail_count) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET etag=COALESCE(excluded.etag, feed_cache.etag), "
            "modified=COALESCE(excluded.modified, feed_cache.modified), "
            "last_ok=COALESCE(excluded.last_ok, feed_cache.last_ok), last_error=excluded.last_error, "
            "fail_count=excluded.fail_count",
            (url, etag, modified, now if ok else None, error, fails))
        self.conn.commit()

    # maintenance -----------------------------------------------------------
    def prune(self, older_than_iso: str) -> None:
        self.conn.execute("DELETE FROM items WHERE discovered_at < ?", (older_than_iso,))
        self.conn.execute("DELETE FROM clusters WHERE created_at < ? AND status NOT IN ('posted','dry_run')",
                          (older_than_iso,))
        self.conn.commit()
