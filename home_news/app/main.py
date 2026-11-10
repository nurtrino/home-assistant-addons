"""Home News: collect -> score (Haiku) -> top three (Sonnet) -> publish to Home Assistant."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

import aiohttp

import econ
from config import load_settings, load_sources
from curate import SECTIONS, Curator
from feeds import collect
from ha import HomeAssistant
from store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("home_news")

LABELS = {"war": "War & Defense", "politics": "Politics", "gaming": "Gaming", "ai": "AI", "economy": "Economy"}
EDITOR_EVERY_H = 4
MIN_SCORE = 40


class HomeNews:
    def __init__(self):
        self.s = load_settings()
        self.sources = load_sources(self.s)
        self.store = Store(self.s.data_dir / "news.db")
        self.curator = Curator(self.s.anthropic_api_key, self.s.sorter_model, self.s.editor_model)
        self.ha = HomeAssistant(self.s.ha_url, self.s.ha_token)
        self.events: list[dict] = json.loads(self.store.get("calendar_events", "[]"))
        self.top: list[dict] = json.loads(self.store.get("top3", "[]"))
        self.top_at = float(self.store.get("top3_at", "0"))
        self.calendar_at = 0.0
        self.editor_lock = asyncio.Lock()
        log.info("Home News starting: AI %s (sorter %s, editor %s), %d steam games",
                 "on" if self.s.has_ai else "OFF (heuristic ranking)", self.s.sorter_model, self.s.editor_model,
                 len(self.s.steam_app_ids))

    # ---------- schedule helpers ----------
    def _now(self) -> datetime:
        return datetime.now(self.s.tz)

    def _active(self) -> bool:
        now = self._now().strftime("%H:%M")
        return self.s.active_start <= now <= self.s.active_end

    # ---------- collection cycle ----------
    async def cycle(self, session: aiohttp.ClientSession) -> None:
        items = await collect(self.sources, self.s.steam_app_ids)
        added = self.store.add_items(items)
        pending = self.store.unscored()
        log.info("%d new stories, %d to score", added, len(pending))
        if pending:
            self.store.save_scores(await self.curator.score(pending))
        self.store.purge(72)
        if time.time() - self.calendar_at > 3 * 3600 or not self.events:
            await self.refresh_calendar(session)
        if self._editor_due():
            await self.run_editor("scheduled")
        await self.publish(session)

    def _editor_due(self) -> bool:
        if not self.top:
            return True
        if not self._active():
            return False
        now = self._now()
        morning = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= morning and self.top_at < morning.timestamp():
            return True
        return time.time() - self.top_at > EDITOR_EVERY_H * 3600

    async def refresh_calendar(self, session: aiohttp.ClientSession) -> None:
        events = await econ.fetch_calendar(session)
        if events:
            self.events = events
            self.calendar_at = time.time()
            self.store.set("calendar_events", json.dumps(events))
            await econ.remember_baselines(session, events, self.store)

    # ---------- economics loop (every 5 min) ----------
    async def econ_loop(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    await econ.remember_baselines(session, self.events, self.store)
                    fresh = await econ.check_releases(session, self.events, self.store)
                    for rel in fresh:
                        text = econ.describe(rel)
                        await self.ha.notify(session, self.s.notify_target, "Economic data", text)
                        self.store.mark_notified(rel["key"])
                    if fresh:
                        if self._active():
                            await self.run_editor("data release")
                        await self.publish(session)
                except Exception:
                    log.exception("econ loop error")
                await asyncio.sleep(300)

    # ---------- clustering + top three ----------
    def clusters(self, hours: float = 36) -> list[dict]:
        """One entry per story (topic), best-scored version first, with how many outlets carried it."""
        groups: dict[str, list] = {}
        for it in self.store.recent(hours):
            if it.score < MIN_SCORE or it.final_section not in SECTIONS:
                continue
            groups.setdefault(f"{it.final_section}:{it.topic or it.id}", []).append(it)
        now = time.time()
        out = []
        for members in groups.values():
            members.sort(key=lambda m: (m.score, m.weight, m.published), reverse=True)
            best = members[0]
            boost = min(8, 2 * (len({m.source for m in members}) - 1))  # coverage by several outlets
            out.append({
                "id": best.id, "section": best.final_section, "headline": best.headline or best.title,
                "source": best.source, "link": best.link, "score": round(min(100, best.score + boost)),
                "age_h": (now - best.published) / 3600, "time": datetime.fromtimestamp(best.published, self.s.tz).isoformat(),
                "sources": len({m.source for m in members}),
            })
        return sorted(out, key=lambda c: (c["score"], -c["age_h"]), reverse=True)

    async def run_editor(self, reason: str) -> None:
        async with self.editor_lock:
            candidates = [c for c in self.clusters(24) if c["score"] >= 50][:50]
            for rel in self.store.releases_since(12):
                candidates.insert(0, {"id": f"rel:{rel['key']}", "section": "economy", "headline": econ.describe(rel),
                                      "source": "BLS", "link": "https://www.bls.gov", "score": 90, "age_h": 0})
            picks = await self.curator.top_three(candidates)
            if picks is None:
                return
            by_id = {c["id"]: c for c in candidates}
            self.top = [{**p, "source": by_id[p["id"]]["source"], "link": by_id[p["id"]]["link"]} for p in picks]
            self.top_at = time.time()
            self.store.set("top3", json.dumps(self.top))
            self.store.set("top3_at", str(self.top_at))
            log.info("top three (%s): %s", reason, " | ".join(p["headline"] for p in self.top))

    # ---------- publish ----------
    async def publish(self, session: aiohttp.ClientSession) -> None:
        clusters = self.clusters(36)
        updated = self._now().isoformat(timespec="seconds")
        for section in SECTIONS:
            items = [{k: c[k] for k in ("headline", "source", "link", "score", "time", "sources")}
                     for c in clusters if c["section"] == section][:15]
            await self.ha.set_state(session, f"sensor.home_news_{section}", str(len(items)), {
                "friendly_name": f"Home News · {LABELS[section]}", "icon": "mdi:newspaper-variant-outline",
                "label": LABELS[section], "items": items, "updated": updated,
            })
        releases = [{"title": r["title"], "actual": r["actual"], "forecast": r["forecast"], "previous": r["previous"],
                     "text": econ.describe(r)} for r in self.store.releases_since(24)]
        await self.ha.set_state(session, "sensor.home_news", updated, {
            "friendly_name": "Home News", "icon": "mdi:newspaper-variant",
            "top3": self.top, "top3_at": datetime.fromtimestamp(self.top_at, self.s.tz).isoformat() if self.top_at else None,
            "calendar": econ.calendar_view(self.events, self.s.tz), "releases": releases,
            "ranking": "claude" if self.s.has_ai else "heuristic", "updated": updated,
        })
        log.info("published: top3=%d, %s", len(self.top),
                 ", ".join(f"{s}={sum(1 for c in clusters if c['section'] == s)}" for s in SECTIONS))

    # ---------- entry ----------
    async def on_mode(self, old: str, new: str) -> None:
        if new == self.s.editor_trigger_state:
            log.info("room mode %s -> %s: refreshing the top three", old, new)
            await self.run_editor("heading home")
            async with aiohttp.ClientSession() as session:
                await self.publish(session)

    async def run(self) -> None:
        asyncio.create_task(self.econ_loop())
        asyncio.create_task(self.ha.watch_state(self.s.mode_entity, self.on_mode))
        async with aiohttp.ClientSession() as session:
            while True:
                started = time.time()
                try:
                    await self.cycle(session)
                except Exception:
                    log.exception("cycle failed")
                interval = (self.s.refresh_minutes if self._active() else 120) * 60
                await asyncio.sleep(max(60, interval - (time.time() - started)))


if __name__ == "__main__":
    asyncio.run(HomeNews().run())
