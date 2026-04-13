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
from .models import build_kimi, build_minimax
from .notify import Notifier

IDEA_INTERVAL_SEC = 1800  # normal cadence: 30 minutes between top-ups
IDEA_RETRY_SEC = 60       # fast retry when last generation added 0 (parse failure / dupes)
IDEA_LOW_WATER = 15       # keep at least this many product features pending so workers never idle
IDEA_BATCH = 20           # ask MiniMax for this many ideas per generation
WIKIDELVE_HINT_LIMIT = 8  # how many KB titles to feed in as grounding
REVIEW_MIN_SCORE = 3      # Kimi gate: reject ideas scoring below this on either axis


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


_REVIEWER_PROMPT = """You are a senior engineer reviewing proposed feature ideas for the \
Hearth product backlog. For each idea you receive, score it 1-5 on two axes:

- implementability: can a coding agent build this in one focused sitting? (5 = clearly yes, \
1 = vague/research-level/multi-week)
- uniqueness: is this distinct from existing backlog features and a real product win? \
(5 = novel + valuable, 1 = duplicate or trivial restating)

Then pick verdict: "accept" if both scores >= 3, else "reject".

Return ONLY a JSON object: {"implementability": int, "uniqueness": int, "verdict": "accept"|"reject", "reason": "one short sentence"}"""


def _parse_review(text: str) -> dict[str, Any] | None:
    """Same lenient JSON extraction as _parse_ideas, but for a single object."""
    text = text.strip()
    while "<think>" in text and "</think>" in text:
        start = text.index("<think>")
        end = text.index("</think>") + len("</think>")
        text = (text[:start] + text[end:]).strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    if "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        return None


async def _review_idea(reviewer: Any, idea: dict[str, Any], existing_titles: list[str]) -> tuple[bool, str]:
    """Kimi gate: returns (accept, reason). Failures default to accept (don't block on reviewer outage)."""
    if reviewer is None:
        return True, "review-skipped (no reviewer configured)"
    user = (
        f"Existing backlog titles:\n  - " + "\n  - ".join(existing_titles[-30:]) +
        f"\n\nProposed idea:\n{json.dumps(idea, indent=2)}"
    )
    try:
        resp = await reviewer.ainvoke([
            {"role": "system", "content": _REVIEWER_PROMPT},
            {"role": "user", "content": user},
        ])
    except Exception as e:
        log.warning("idea_review_failed", id=idea.get("id"), error=str(e))
        return True, "review-skipped (reviewer error)"
    parsed = _parse_review(resp.content if hasattr(resp, "content") else str(resp))
    if not parsed:
        return True, "review-skipped (unparseable)"
    impl = int(parsed.get("implementability", 0) or 0)
    uniq = int(parsed.get("uniqueness", 0) or 0)
    verdict = parsed.get("verdict", "")
    accept = verdict == "accept" and impl >= REVIEW_MIN_SCORE and uniq >= REVIEW_MIN_SCORE
    return accept, f"impl={impl} uniq={uniq} {parsed.get('reason','')[:120]}"


async def _generate_once(backlog: Backlog, model: Any, reviewer: Any) -> tuple[int, int]:
    """Ask MiniMax for ideas, gate each through Kimi, append accepted ones.

    Returns (accepted, rejected).
    """
    hints = await _wikidelve_hints()
    user = _user_prompt(backlog, hints)
    try:
        resp = await model.ainvoke([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ])
    except Exception as e:
        log.warning("idea_minimax_failed", error=str(e))
        return 0, 0

    ideas = _parse_ideas(resp.content if hasattr(resp, "content") else str(resp))
    valid_repos = {"hearth", "hearth-desktop", "hearth-mobile"}
    existing_titles = [f.name for f in backlog.features]
    accepted = 0
    rejected = 0

    for raw in ideas:
        if not isinstance(raw, dict) or "id" not in raw or "name" not in raw:
            rejected += 1
            continue
        if any(f.id == str(raw["id"]) for f in backlog.features):
            rejected += 1
            log.info("idea_rejected", id=raw.get("id"), reason="duplicate-id")
            continue

        accept, reason = await _review_idea(reviewer, raw, existing_titles)
        if not accept:
            rejected += 1
            log.info("idea_rejected", id=raw.get("id"), reason=reason)
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
            accepted += 1
            existing_titles.append(feature.name)
            log.info("idea_added", id=feature.id, name=feature.name, review=reason)
        else:
            rejected += 1
    return accepted, rejected


async def run_idea_engine(backlog: Backlog) -> None:
    """Background task: top up the backlog with fresh product ideas."""
    if not settings.minimax_api_key:
        log.info("idea_engine_disabled", reason="no_minimax_key")
        return
    model = build_minimax()
    reviewer = build_kimi() if settings.kimi_api_key else None
    notifier = Notifier()
    log.info("idea_engine_started", interval_sec=IDEA_INTERVAL_SEC, low_water=IDEA_LOW_WATER, reviewer=bool(reviewer))
    try:
        while True:
            pending_product = [
                f for f in backlog.features
                if f.status == "pending" and not f.self_improvement
            ]
            sleep_for = IDEA_INTERVAL_SEC
            if len(pending_product) < IDEA_LOW_WATER:
                log.info("idea_generating", pending_product=len(pending_product))
                accepted, rejected = await _generate_once(backlog, model, reviewer)
                log.info("idea_generation_done", accepted=accepted, rejected=rejected)
                if accepted > 0:
                    await notifier.send(
                        f"💡 idea engine: +{accepted} accepted, {rejected} rejected by review"
                    )
                else:
                    sleep_for = IDEA_RETRY_SEC
            await asyncio.sleep(sleep_for)
    finally:
        await notifier.close()
