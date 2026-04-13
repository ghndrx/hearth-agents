"""Autonomous idea engine — keeps the product backlog full of fresh Hearth features.

Runs as a background task alongside the loop. When pending product features
(``self_improvement=False``) drop below ``IDEA_LOW_WATER``, asks MiniMax to
propose new ideas grounded in (a) the existing backlog so it doesn't repeat
itself and (b) Hearth-tagged wikidelve articles so it stays grounded in what
we've actually researched.

Throttled to one generation every ``IDEA_INTERVAL_SEC`` so we don't incinerate
MiniMax quota generating ideas the loop will never build.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from .backlog import Backlog, Feature
from .config import settings
from .logger import log
from .models import build_minimax

IDEA_INTERVAL_SEC = 1800  # normal cadence: 30 minutes between top-ups
IDEA_RETRY_SEC = 60       # fast retry when last generation added 0 (parse failure / dupes)
IDEA_LOW_WATER = 5        # generate when fewer than this many product features pend
IDEA_BATCH = 10           # ask MiniMax for this many ideas per generation
WIKIDELVE_HINT_LIMIT = 8  # how many KB titles to feed in as grounding


_SYSTEM_PROMPT = """You are a product strategist for Hearth, a self-hosted, federated, open-source \
Discord/Slack alternative. Generate concrete, implementable feature ideas for an autonomous \
coding agent to build. Each idea must be specific enough to implement in one sitting (not \
"build a chat platform"), tied to either Discord parity or a clear competitive advantage \
(federation, self-hosting, open-source, privacy, customization).

Return ONLY a JSON array of feature objects, no prose. Each object:
{
  "id": "kebab-case-unique-slug",
  "name": "Short human title",
  "description": "2-4 sentences: what it does, why users want it, key technical approach.",
  "priority": "critical" | "high" | "medium" | "low",
  "repos": ["hearth"] | ["hearth", "hearth-desktop"] | ...,
  "research_topics": ["specific topic strings to research first"],
  "discord_parity": "What Discord feature this matches, OR competitive advantage"
}

Valid repos: hearth, hearth-desktop, hearth-mobile. Do NOT propose features for hearth-agents \
(those are self-improvement and handled separately)."""


async def _wikidelve_hints() -> list[str]:
    """Pull recent Hearth-tagged article titles from wikidelve for grounding.

    Best-effort — returns empty on failure rather than blocking idea generation.
    """
    if not settings.wikidelve_url:
        return []
    try:
        async with httpx.AsyncClient(base_url=settings.wikidelve_url, timeout=10) as c:
            r = await c.get("/api/search/hybrid", params={"q": "hearth", "limit": WIKIDELVE_HINT_LIMIT})
            r.raise_for_status()
            return [a.get("title", "") for a in r.json() if a.get("title")]
    except Exception as e:
        log.warning("idea_wikidelve_hint_failed", error=str(e))
        return []


def _user_prompt(backlog: Backlog, hints: list[str]) -> str:
    existing = [f.id for f in backlog.features]
    parts = [
        f"Existing feature IDs (do NOT propose any of these): {', '.join(existing)}",
        f"Generate {IDEA_BATCH} new feature ideas now.",
    ]
    if hints:
        parts.append("Recent research articles (use as grounding where relevant):")
        parts.extend(f"  - {t}" for t in hints)
    return "\n\n".join(parts)


def _parse_ideas(text: str) -> list[dict[str, Any]]:
    """Extract the JSON array from MiniMax's reply.

    MiniMax M2.7 wraps responses in ``<think>...</think>`` reasoning blocks and
    sometimes ```` ```json ```` fences. Strip both, then locate the first ``[``
    and last ``]`` to isolate the JSON array even if there's trailing prose.
    """
    text = text.strip()
    # Drop think blocks (MiniMax M2.7 always emits these)
    while "<think>" in text and "</think>" in text:
        start = text.index("<think>")
        end = text.index("</think>") + len("</think>")
        text = (text[:start] + text[end:]).strip()
    # Drop code fences
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    # Slice from first [ to last ] to tolerate any remaining prose
    if "[" in text and "]" in text:
        text = text[text.index("["): text.rindex("]") + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError as e:
        log.warning("idea_parse_failed", error=str(e), preview=text[:200])
        return []


async def _generate_once(backlog: Backlog, model: Any) -> int:
    """Ask MiniMax for ideas, append valid ones to backlog. Returns count added."""
    hints = await _wikidelve_hints()
    user = _user_prompt(backlog, hints)
    try:
        resp = await model.ainvoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ])
    except Exception as e:
        log.warning("idea_minimax_failed", error=str(e))
        return 0

    ideas = _parse_ideas(resp.content if hasattr(resp, "content") else str(resp))
    valid_repos = {"hearth", "hearth-desktop", "hearth-mobile"}
    added = 0
    for raw in ideas:
        if not isinstance(raw, dict) or "id" not in raw or "name" not in raw:
            continue
        repos = [r for r in raw.get("repos", ["hearth"]) if r in valid_repos] or ["hearth"]
        feature = Feature(
            id=str(raw["id"]),
            name=str(raw["name"]),
            description=str(raw.get("description", "")),
            priority=raw.get("priority", "medium") if raw.get("priority") in ("critical", "high", "medium", "low") else "medium",
            repos=repos,
            research_topics=[str(t) for t in raw.get("research_topics", []) if isinstance(t, str)],
            discord_parity=str(raw.get("discord_parity", "")),
        )
        if backlog.add(feature):
            added += 1
            log.info("idea_added", id=feature.id, name=feature.name)
    return added


async def run_idea_engine(backlog: Backlog) -> None:
    """Background task: top up the backlog with fresh product ideas."""
    if not settings.minimax_api_key:
        log.info("idea_engine_disabled", reason="no_minimax_key")
        return
    model = build_minimax()
    log.info("idea_engine_started", interval_sec=IDEA_INTERVAL_SEC, low_water=IDEA_LOW_WATER)
    while True:
        pending_product = [
            f for f in backlog.features
            if f.status == "pending" and not f.self_improvement
        ]
        sleep_for = IDEA_INTERVAL_SEC
        if len(pending_product) < IDEA_LOW_WATER:
            log.info("idea_generating", pending_product=len(pending_product))
            added = await _generate_once(backlog, model)
            log.info("idea_generation_done", added=added)
            # If we produced nothing (parse failure or all dupes), retry quickly
            # rather than letting workers starve for the full interval.
            if added == 0:
                sleep_for = IDEA_RETRY_SEC
        await asyncio.sleep(sleep_for)
