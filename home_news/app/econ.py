"""Economic calendar (Forex Factory) and actual numbers at release time (BLS public API, no key).

The calendar gives every event this week with forecast and previous values. For the big BLS releases
(CPI, payrolls, unemployment) the actual figure is pulled from api.bls.gov as soon as it's published,
compared with the forecast, and announced once.
"""
from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import aiohttp

from store import Store

log = logging.getLogger("econ")
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
BLS_URL = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
UA = {"User-Agent": "Mozilla/5.0 (compatible; HomeNews/1.0)"}

# Forex Factory event title -> how to compute the actual from BLS.
BLS_EVENTS = {
    "CPI y/y": ("CUUR0000SA0", "yoy", "CPI"),
    "CPI m/m": ("CUSR0000SA0", "mom", "CPI"),
    "Core CPI m/m": ("CUSR0000SA0L1E", "mom", "Core CPI"),
    "Non-Farm Employment Change": ("CES0000000001", "diff", "Payrolls"),
    "Unemployment Rate": ("LNS14000000", "level", "Unemployment"),
}


def _parse_ff_time(date: str, time_s: str) -> datetime | None:
    """Forex Factory's feed uses UTC. 'All Day' / 'Tentative' events get no time."""
    try:
        day = datetime.strptime(date, "%m-%d-%Y")
    except ValueError:
        return None
    m = re.match(r"(\d{1,2}):(\d{2})(am|pm)", (time_s or "").strip().lower())
    if not m:
        return day.replace(tzinfo=timezone.utc)
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    return day.replace(hour=hour, minute=int(m.group(2)), tzinfo=timezone.utc)


async def fetch_calendar(session: aiohttp.ClientSession) -> list[dict]:
    try:
        async with session.get(FF_URL, headers=UA, timeout=aiohttp.ClientTimeout(total=25)) as r:
            raw = await r.read() if r.status == 200 else None
    except Exception as e:
        log.warning("calendar fetch failed: %s", e)
        return []
    if not raw:
        return []
    events = []
    for e in ET.fromstring(raw).findall(".//event"):
        when = _parse_ff_time(e.findtext("date") or "", e.findtext("time") or "")
        events.append({
            "title": (e.findtext("title") or "").strip(),
            "country": e.findtext("country") or "",
            "impact": e.findtext("impact") or "",
            "forecast": (e.findtext("forecast") or "").strip(),
            "previous": (e.findtext("previous") or "").strip(),
            "time": when.isoformat() if when else "",
            "all_day": not re.match(r"\d", (e.findtext("time") or "").strip()),
        })
    return events


def calendar_view(events: list[dict], tz, days: int = 5) -> list[dict]:
    """US high/medium-impact events from now through the next few days, in local time."""
    now = datetime.now(timezone.utc)
    out = []
    for ev in events:
        if ev["country"] != "USD" or ev["impact"] not in ("High", "Medium") or not ev["time"]:
            continue
        when = datetime.fromisoformat(ev["time"])
        if when < now - timedelta(hours=3) or when > now + timedelta(days=days):
            continue
        local = when.astimezone(tz)
        out.append({
            "title": ev["title"], "impact": ev["impact"], "forecast": ev["forecast"], "previous": ev["previous"],
            "time": when.isoformat(),
            "label": local.strftime("%a ") + ("All day" if ev["all_day"] else local.strftime("%I:%M %p").lstrip("0").lower()),
        })
    return sorted(out, key=lambda e: e["time"])[:14]


async def _bls(session: aiohttp.ClientSession, series: list[str]) -> dict[str, list[dict]]:
    body = json.dumps({"seriesid": series}).encode()
    try:
        async with session.post(BLS_URL, data=body, headers={**UA, "Content-Type": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=25)) as r:
            data = await r.json(content_type=None)
    except Exception as e:
        log.warning("BLS request failed: %s", e)
        return {}
    if data.get("status") != "REQUEST_SUCCEEDED":
        log.warning("BLS: %s %s", data.get("status"), data.get("message"))
        return {}
    return {s["seriesID"]: s["data"] for s in data.get("Results", {}).get("series", [])}


def _compute(kind: str, rows: list[dict]) -> str | None:
    """rows are newest first, monthly ('M01'..'M12')."""
    monthly = [r for r in rows if r["period"].startswith("M") and r["period"] != "M13"]
    try:
        v = [float(r["value"]) for r in monthly]
    except ValueError:
        return None
    if kind == "level" and v:
        return f"{v[0]:.1f}%"
    if kind == "diff" and len(v) > 1:
        change = (v[0] - v[1])  # thousands of jobs
        return f"{change:+.0f}K"
    if kind == "mom" and len(v) > 1:
        return f"{(v[0] / v[1] - 1) * 100:.1f}%"
    if kind == "yoy" and len(v) > 12:
        return f"{(v[0] / v[12] - 1) * 100:.1f}%"
    return None


async def check_releases(session: aiohttp.ClientSession, events: list[dict], store: Store) -> list[dict]:
    """Within a few hours after a scheduled BLS release, fetch the new number and record it once."""
    now = datetime.now(timezone.utc)
    due = {}
    for ev in events:
        spec = BLS_EVENTS.get(ev["title"])
        if not spec or ev["country"] != "USD" or not ev["time"]:
            continue
        when = datetime.fromisoformat(ev["time"])
        if timedelta(0) <= now - when <= timedelta(hours=4):
            due[ev["title"]] = (spec, ev)
    if not due:
        return []
    data = await _bls(session, sorted({spec[0] for spec, _ in due.values()}))
    fresh = []
    for title, ((series, kind, label), ev) in due.items():
        rows = data.get(series)
        if not rows:
            continue
        period = f"{rows[0]['year']}-{rows[0]['period']}"
        key = f"{series}:{period}"
        # Only announce a period newer than the one we had before the release time.
        if store.release_seen(key) or store.get(f"bls_before:{series}") == period:
            continue
        actual = _compute(kind, rows)
        if not actual:
            continue
        suffix = {"yoy": " y/y", "mom": " m/m"}.get(kind, "")
        store.add_release(key, f"{label}{suffix}", actual, ev["forecast"], ev["previous"])
        fresh.append({"key": key, "title": f"{label}{suffix}", "actual": actual,
                      "forecast": ev["forecast"], "previous": ev["previous"]})
        log.info("release: %s %s (fc %s, prev %s)", label + suffix, actual, ev["forecast"], ev["previous"])
    return fresh


async def remember_baselines(session: aiohttp.ClientSession, events: list[dict], store: Store) -> None:
    """Before each release window, note the latest period so the old number isn't announced as new."""
    now = datetime.now(timezone.utc)
    upcoming = [BLS_EVENTS[e["title"]][0] for e in events
                if e["title"] in BLS_EVENTS and e["time"]
                and timedelta(0) < datetime.fromisoformat(e["time"]) - now < timedelta(hours=12)]
    series = sorted(set(upcoming))
    if not series or store.get("bls_baseline_day") == now.strftime("%Y-%m-%d"):
        return
    data = await _bls(session, series)
    for s, rows in data.items():
        if rows:
            store.set(f"bls_before:{s}", f"{rows[0]['year']}-{rows[0]['period']}")
    store.set("bls_baseline_day", now.strftime("%Y-%m-%d"))


def describe(rel: dict) -> str:
    """'CPI y/y 3.1% (forecast 3.0%, previous 2.9%)'"""
    extra = ", ".join(x for x in (
        f"forecast {rel['forecast']}" if rel.get("forecast") else "",
        f"previous {rel['previous']}" if rel.get("previous") else "",
    ) if x)
    return f"{rel['title']} {rel['actual']}" + (f" ({extra})" if extra else "")
