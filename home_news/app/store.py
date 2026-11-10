"""SQLite store for collected stories, Claude's scores, and economic releases."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Item:
    id: str
    section: str
    source: str
    weight: int
    title: str
    link: str
    summary: str
    published: float
    fetched: float = 0.0
    scored: int = 0          # 0 = waiting for Claude, 1 = scored
    score: float = 0.0       # 0..100 relevance to Chase
    headline: str = ""       # Claude's clean one-liner
    topic: str = ""          # cluster key for duplicates across sources
    final_section: str = ""  # Claude may move a story (or "drop" it)


SCHEMA = """
create table if not exists items (
  id text primary key, section text, source text, weight integer, title text, link text,
  summary text, published real, fetched real, scored integer default 0, score real default 0,
  headline text default '', topic text default '', final_section text default ''
);
create index if not exists items_pub on items(published);
create table if not exists releases (
  key text primary key, title text, actual text, forecast text, previous text,
  released real, notified integer default 0
);
create table if not exists kv (k text primary key, v text);
"""


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- items ----
    def add_items(self, items: list[Item]) -> int:
        now = time.time()
        added = 0
        for it in items:
            cur = self.db.execute(
                "insert or ignore into items (id, section, source, weight, title, link, summary, published, fetched)"
                " values (?,?,?,?,?,?,?,?,?)",
                (it.id, it.section, it.source, it.weight, it.title, it.link, it.summary, it.published, now),
            )
            added += cur.rowcount
        self.db.commit()
        return added

    def unscored(self, limit: int = 400) -> list[Item]:
        rows = self.db.execute(
            "select * from items where scored = 0 order by published desc limit ?", (limit,)
        ).fetchall()
        return [Item(**dict(r)) for r in rows]

    def save_scores(self, scores: list[dict]) -> None:
        for s in scores:
            self.db.execute(
                "update items set scored = 1, score = ?, headline = ?, topic = ?, final_section = ? where id = ?",
                (float(s.get("score", 0)), s.get("headline", ""), s.get("topic", ""), s.get("section", ""), s["id"]),
            )
        self.db.commit()

    def recent(self, hours: float) -> list[Item]:
        cutoff = time.time() - hours * 3600
        rows = self.db.execute(
            "select * from items where published >= ? and scored = 1 and final_section != 'drop'", (cutoff,)
        ).fetchall()
        return [Item(**dict(r)) for r in rows]

    def purge(self, hours: float = 72) -> None:
        self.db.execute("delete from items where published < ?", (time.time() - hours * 3600,))
        self.db.commit()

    # ---- economic releases ----
    def release_seen(self, key: str) -> bool:
        return self.db.execute("select 1 from releases where key = ?", (key,)).fetchone() is not None

    def add_release(self, key: str, title: str, actual: str, forecast: str, previous: str) -> None:
        self.db.execute(
            "insert or ignore into releases (key, title, actual, forecast, previous, released) values (?,?,?,?,?,?)",
            (key, title, actual, forecast, previous, time.time()),
        )
        self.db.commit()

    def releases_since(self, hours: float) -> list[dict]:
        rows = self.db.execute(
            "select * from releases where released >= ? order by released desc", (time.time() - hours * 3600,)
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_notified(self, key: str) -> None:
        self.db.execute("update releases set notified = 1 where key = ?", (key,))
        self.db.commit()

    # ---- small key/value state ----
    def get(self, k: str, default: str = "") -> str:
        row = self.db.execute("select v from kv where k = ?", (k,)).fetchone()
        return row["v"] if row else default

    def set(self, k: str, v: str) -> None:
        self.db.execute("insert into kv (k, v) values (?, ?) on conflict(k) do update set v = excluded.v", (k, v))
        self.db.commit()
