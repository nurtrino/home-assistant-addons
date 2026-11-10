"""Claude curation: Haiku scores and sorts every new story; Sonnet picks the top three.

Without an API key, a simple heuristic (source weight + recency) keeps everything working.
"""
from __future__ import annotations

import json
import logging
import re
import time

import anthropic

from store import Item

log = logging.getLogger("curate")

SECTIONS = ["war", "politics", "gaming", "ai", "economy"]

PROFILE = """You curate news for Chase, who will hear the top stories spoken by his TV when he walks into his room \
and see the rest on a dashboard. His interests, strongest first:

1. AI (heaviest weight): new frontier models and releases (Anthropic/Claude, OpenAI, Google Gemini, xAI, Meta, \
DeepSeek, Qwen, Mistral), benchmark results, notable papers, agents and developer tools, compute/chips, AI policy, \
and industry drama (lawsuits, departures, leaks, feuds).
2. War & defense: active conflicts (Middle East, Iran, Ukraine-Russia, China-Taiwan), US military operations and \
deployments, new weapons and aircraft (drones, Anduril, F-47, hypersonics), naval movements, the defense industry.
3. US politics from an America First angle: Trump administration actions, Congress, the Supreme Court, the border, \
and above all elections: race calls, primary and special-election results, and new polling (generic ballot, \
approval, midterm forecasts). The 2026 midterms are on November 3.
4. Gaming: big releases, reveals, delays, studio news and drama, leaks; any news about games he plays \
(ARC Raiders above all, Elden Ring, Sekiro, Black Myth: Wukong, Death Stranding, War Thunder).
5. Economy, only when it's big: Fed decisions, major data releases (CPI, jobs, GDP, PCE) with actual vs expected, \
market shocks, yields, tariffs.

Not interested: celebrity and lifestyle, sports, shopping deals and sales, listicles and guides, \
routine game patch notes or store sales, local crime, horoscopes, reviews of gadgets."""

SORT_RULES = """Score each story for Chase from 0 to 100:
- 85-100: major breaking news squarely in his interests (a new frontier model, a war escalation, a big race call or \
election result, a Fed decision or data release, a major game reveal/delay or ARC Raiders news).
- 60-84: solid, relevant news he'd want on his dashboard.
- 30-59: minor or niche but on-topic.
- 0-29: off-topic or low value. Use section "drop" for anything in the not-interested list.
For each story return: its id; the best section (war, politics, gaming, ai, economy, or drop); the score; \
a clean factual headline of at most 90 characters (no clickbait, no source name, no emoji); and a short \
lowercase topic key (2-5 words joined by dashes) shared by stories about the same event, so duplicates can be merged."""

EDITOR_RULES = """Pick the three stories Chase most needs to know right now, from the candidates below. Prefer \
genuinely important, fresh news over the merely interesting; spread across sections when importance is close, but \
never pad with weak stories. A fresh economic data release with a surprise vs forecast, a major race call, a new \
frontier AI model, or a war escalation should usually make the cut.

For each pick return: the candidate id; its section; a display headline (max 80 characters); and one natural spoken \
sentence for a text-to-speech voice (max 30 words, plain words, no source names, no URLs, no abbreviations that \
read badly aloud; say "percent", spell out "Federal Reserve" the first time)."""

SORT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "section": {"type": "string", "enum": SECTIONS + ["drop"]},
                    "score": {"type": "integer"},
                    "headline": {"type": "string"},
                    "topic": {"type": "string"},
                },
                "required": ["id", "section", "score", "headline", "topic"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

EDITOR_SCHEMA = {
    "type": "object",
    "properties": {
        "top": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "section": {"type": "string", "enum": SECTIONS},
                    "headline": {"type": "string"},
                    "spoken": {"type": "string"},
                },
                "required": ["id", "section", "headline", "spoken"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["top"],
    "additionalProperties": False,
}

JUNK = re.compile(r"\b(deal|deals|% off|sale|discount|coupon|best .* to buy|wordle|horoscope|review:|how to)\b", re.I)


def _text_json(response) -> dict:
    """Structured outputs put the JSON in the (first) text block; thinking blocks may precede it."""
    text = next(b.text for b in response.content if getattr(b, "type", "") == "text")
    return json.loads(text)


def _topic_key(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    stop = {"the", "a", "an", "to", "of", "in", "on", "for", "and", "is", "as", "at", "with", "by", "after", "new"}
    keep = [w for w in words if w not in stop][:4]
    return "-".join(keep)


class Curator:
    def __init__(self, api_key: str, sorter_model: str, editor_model: str):
        self.client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else None
        self.sorter_model = sorter_model
        self.editor_model = editor_model
        self.use_format = True

    # ---------- sorting (Haiku) ----------
    async def score(self, items: list[Item]) -> list[dict]:
        if not items:
            return []
        if not self.client:
            return [self._heuristic(it) for it in items]
        out: list[dict] = []
        for i in range(0, len(items), 60):
            batch = items[i:i + 60]
            try:
                out.extend(await self._score_batch(batch))
            except Exception as e:
                log.error("sorter failed (%s); using heuristic for %d items", e, len(batch))
                out.extend(self._heuristic(it) for it in batch)
        return out

    async def _score_batch(self, batch: list[Item]) -> list[dict]:
        lines = [
            f"[{it.id}] ({it.section} | {it.source}) {it.title}" + (f" :: {it.summary[:180]}" if it.summary else "")
            for it in batch
        ]
        kwargs = dict(
            model=self.sorter_model,
            max_tokens=8000,
            system=PROFILE + "\n\n" + SORT_RULES,
            messages=[{"role": "user", "content": "Stories:\n" + "\n".join(lines)}],
        )
        if self.use_format:
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": SORT_SCHEMA}}
        else:
            kwargs["messages"][0]["content"] += "\n\nReply with only JSON matching: " + json.dumps(SORT_SCHEMA)
        try:
            resp = await self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if self.use_format and "output_config" in str(e):
                log.warning("structured outputs unsupported on %s; falling back to JSON prompt", self.sorter_model)
                self.use_format = False
                return await self._score_batch(batch)
            raise
        data = _text_json(resp)
        valid = {it.id for it in batch}
        scored = [s for s in data.get("items", []) if s.get("id") in valid]
        missing = valid - {s["id"] for s in scored}
        scored.extend(self._heuristic(it) for it in batch if it.id in missing)
        log.info("haiku scored %d stories (in %d / out %d tokens)", len(scored),
                 resp.usage.input_tokens, resp.usage.output_tokens)
        return scored

    def _heuristic(self, it: Item) -> dict:
        age_h = max(0.0, (time.time() - it.published) / 3600)
        score = min(95.0, it.weight * 22 + max(0.0, 30 - age_h * 1.5))
        section = "drop" if JUNK.search(it.title) else it.section
        return {"id": it.id, "section": section, "score": round(score), "headline": it.title[:90],
                "topic": _topic_key(it.title)}

    # ---------- top three (Sonnet) ----------
    async def top_three(self, candidates: list[dict]) -> list[dict] | None:
        """candidates: [{id, section, headline, source, age_h, score}] already de-duplicated."""
        if not candidates:
            return []
        if not self.client:
            picks = sorted(candidates, key=lambda c: c["score"], reverse=True)[:3]
            return [{"id": c["id"], "section": c["section"], "headline": c["headline"],
                     "spoken": c["headline"].rstrip(".") + "."} for c in picks]
        lines = [
            f"[{c['id']}] {c['section']} | score {c['score']} | {c['age_h']:.0f}h old | {c['source']} | {c['headline']}"
            for c in candidates
        ]
        try:
            resp = await self.client.beta.messages.create(
                model=self.editor_model,
                max_tokens=8000,
                betas=["server-side-fallback-2026-07-01"],
                system=PROFILE + "\n\n" + EDITOR_RULES,
                messages=[{"role": "user", "content": "Candidates:\n" + "\n".join(lines)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": EDITOR_SCHEMA}},
                extra_body={"fallbacks": "default"},
            )
        except Exception as e:
            log.error("editor failed: %s", e)
            return None
        if resp.stop_reason == "refusal":
            log.warning("editor declined; keeping previous top three")
            return None
        data = _text_json(resp)
        ids = {c["id"] for c in candidates}
        picks = [p for p in data.get("top", []) if p.get("id") in ids][:3]
        log.info("sonnet picked %d (in %d / out %d tokens)", len(picks), resp.usage.input_tokens,
                 resp.usage.output_tokens)
        return picks
