"""Fetches every source and normalizes entries into Items."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import html
import logging
import re
import time

import aiohttp
import feedparser

from store import Item

log = logging.getLogger("feeds")
UA = "Mozilla/5.0 (compatible; HomeNews/1.0; personal news dashboard)"
REDDIT_UA = "HomeNews/1.0 (personal news dashboard; contact via github.com/nurtrino)"
TAG_RE = re.compile(r"<[^>]+>")

# How far back a story may be to count as news (hours).
MAX_AGE = {"default": 36, "steam": 24 * 7, "ai": 48}


def _clean(text: str, limit: int) -> str:
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _published(entry) -> float:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            return float(calendar.timegm(t))
    return time.time()


def _item(section: str, source: str, weight: int, entry, kind: str) -> Item | None:
    title = _clean(entry.get("title", ""), 300)
    link = entry.get("link", "") or ""
    if not title:
        return None
    src = source
    if kind == "google":
        # Google News titles end in " - Outlet"; keep the outlet as the source.
        outlet = (entry.get("source") or {}).get("title") if isinstance(entry.get("source"), dict) else None
        if outlet and title.endswith(" - " + outlet):
            title = title[: -(len(outlet) + 3)].strip()
        if outlet:
            src = f"{outlet} (via Google News)"
    summary = _clean(entry.get("summary", "") or entry.get("description", ""), 400)
    uid = hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16]
    return Item(id=uid, section=section, source=src, weight=weight, title=title, link=link,
                summary=summary, published=_published(entry))


async def _get(session: aiohttp.ClientSession, url: str, ua: str = UA) -> bytes | None:
    try:
        async with session.get(url, headers={"User-Agent": ua}, timeout=aiohttp.ClientTimeout(total=25)) as r:
            if r.status != 200:
                log.warning("HTTP %s for %s", r.status, url[:90])
                return None
            return await r.read()
    except Exception as e:  # network errors shouldn't stop the cycle
        log.warning("fetch failed %s: %s", url[:90], e)
        return None


def _parse(raw: bytes | None, section: str, name: str, weight: int, kind: str, max_age_h: float) -> list[Item]:
    if not raw:
        return []
    feed = feedparser.parse(raw)
    cutoff = time.time() - max_age_h * 3600
    out = []
    for entry in feed.entries[:60]:
        it = _item(section, name, weight, entry, kind)
        if it and it.published >= cutoff:
            out.append(it)
    return out


async def collect(sources: dict, steam_app_ids: list[int]) -> list[Item]:
    """One pass over every source. Reddit is fetched one feed at a time with a pause (rate limits)."""
    jobs, reddit = [], []
    for section, spec in sources.get("sections", {}).items():
        for src in spec.get("sources", []):
            kind = src.get("type", "rss")
            entry = (section, src["name"], int(src.get("weight", 1)), kind, src["url"])
            (reddit if kind == "reddit" else jobs).append(entry)

    items: list[Item] = []
    async with aiohttp.ClientSession() as session:
        async def run(section, name, weight, kind, url):
            raw = await _get(session, url)
            age = MAX_AGE.get(section, MAX_AGE["default"])
            return _parse(raw, section, name, weight, kind, age)

        async def steam(app_id: int):
            raw = await _get(session, f"https://store.steampowered.com/feeds/news/app/{app_id}/?l=english")
            if not raw:
                return []
            feed = feedparser.parse(raw)
            game = re.sub(r"\s*-\s*Steam News.*$", "", feed.feed.get("title", "") or f"App {app_id}").strip()
            return _parse(raw, "gaming", f"Steam: {game}", 3, "steam", MAX_AGE["steam"])

        results = await asyncio.gather(*(run(*j) for j in jobs), *(steam(a) for a in steam_app_ids))
        for r in results:
            items.extend(r)

        for section, name, weight, kind, url in reddit:
            raw = await _get(session, url, REDDIT_UA)
            items.extend(_parse(raw, section, name, weight, kind, MAX_AGE.get(section, MAX_AGE["default"])))
            await asyncio.sleep(8)

    # The same story can arrive from several feeds; keep one per id.
    unique = {}
    for it in items:
        unique.setdefault(it.id, it)
    log.info("collected %d items from %d feeds", len(unique), len(jobs) + len(reddit) + len(steam_app_ids))
    return list(unique.values())
