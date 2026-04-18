"""FastAPI HTTP server.

Exposes a health endpoint plus the GitHub webhook receiver. Telegram runs
separately in long-poll mode (see ``bot.py``) — no HTTP ingress needed for it.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .backlog import Backlog
from .config import settings
from .kanban_html import KANBAN_HTML
from .logger import log


def build_app(backlog: Backlog, agent: Any) -> FastAPI:
    """Construct the FastAPI app with shared backlog + agent state."""
    app = FastAPI(title="hearth-agents", version="0.2.0")

    # Permissive CORS so the kanban at hearth-agents.walleye-frog.ts.net can
    # fetch /features from a browser on any device on the tailnet. We only
    # bind to 127.0.0.1 + tailscale serve, so CORS is a UX affordance rather
    # than the security boundary — Tailscale auth is.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|[^/]*\.walleye-frog\.ts\.net)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "stats": backlog.stats()}

    @app.get("/features")
    async def list_features(status: str | None = None) -> list[dict[str, Any]]:
        """All features (or a single status slice) as lightweight dicts for
        the kanban UI. Ordered by last activity (updated_at desc) so the
        board top is the currently-moving work, not the oldest-birthday."""
        from .transitions import read_tail
        features = backlog.features
        if status:
            features = [f for f in features if f.status == status]
        # Build feature_id → latest transition ts map in one pass — avoids
        # the O(features × transitions) read that a naive to_dict()
        # would cause. read_tail returns chronological order, so the
        # last occurrence wins.
        latest: dict[str, str] = {}
        for t in read_tail(limit=10000):
            fid = t.get("feature_id")
            ts = t.get("ts")
            if fid and ts:
                latest[fid] = ts
        return sorted(
            (f.to_dict(updated_at=latest.get(f.id)) for f in features),
            key=lambda d: d["updated_at"],
            reverse=True,
        )

    @app.post("/features/{feature_id}/action")
    async def feature_action(feature_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a kanban action. Body: {"action": "approve|retry|nuke"}."""
        action = payload.get("action", "")
        ok, message = backlog.action(feature_id, action)
        if not ok:
            raise HTTPException(status_code=400, detail=message)
        log.info("kanban_action", feature=feature_id, action=action, result=message)
        return {"ok": True, "message": message}

    @app.get("/kanban", response_class=HTMLResponse)
    async def kanban() -> HTMLResponse:
        """Single-page kanban UI. Served as a static string — no build step,
        no frontend/ directory; Alpine.js via CDN does the rendering."""
        return HTMLResponse(KANBAN_HTML)

    @app.get("/config")
    async def config_view() -> dict[str, Any]:
        """Runtime configuration operators care about: loop dials, prompts
        version, provider bias. Read-only — env changes require a restart.
        Never returns secrets (api keys, tokens)."""
        from .transitions import prompts_version
        return {
            "prompts_version": prompts_version(),
            "loop": {
                "workers": settings.loop_workers,
                "max_fixups": settings.max_fixups,
                "per_feature_timeout_sec": settings.per_feature_timeout_sec,
                "minimax_bias": settings.minimax_bias,
            },
            "models": {
                "minimax_model": settings.minimax_model,
                "kimi_model": settings.kimi_model,
            },
            "flags": {
                "product_features_enabled": settings.product_features_enabled,
                "langfuse_enabled": bool(settings.langfuse_public_key and settings.langfuse_secret_key),
            },
        }

    @app.get("/transitions")
    async def transitions(limit: int = 500) -> list[dict[str, Any]]:
        """Recent status-change entries. Read from /data/transitions.jsonl,
        which only began populating with commit 608d1ff — older history
        isn't here. Cap limit at 5000 to stop a runaway query from reading
        an arbitrarily large file into memory."""
        from .transitions import read_tail
        capped = max(1, min(limit, 5000))
        return read_tail(limit=capped)

    @app.get("/features/{feature_id}/history")
    async def feature_history(feature_id: str) -> dict[str, Any]:
        """Per-feature transition timeline. Useful for RCA on 'why is
        feature X still blocked' — returns every status change with
        reason and actor in chronological order."""
        from .transitions import read_tail
        entries = read_tail(limit=5000, feature_id=feature_id)
        feature = next((f for f in backlog.features if f.id == feature_id), None)
        return {
            "feature": feature.to_dict() if feature else None,
            "transitions": entries,
        }

    @app.get("/stats")
    async def stats() -> dict[str, Any]:
        """Operational stats: backlog breakdown, recent velocity, split + heal
        activity, circuit-breaker state. Exists so operators can diagnose
        regressions without log-grepping."""
        from .loop import _primary_cooldown_until, _fallback_cooldown_until, circuit_state
        import asyncio as _asyncio
        import time as _time

        now_monotonic = _asyncio.get_event_loop().time()
        now_wall = _time.time()

        def _iso_age(iso: str) -> float:
            from datetime import datetime as _dt
            try:
                return now_wall - _dt.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
            except Exception:  # noqa: BLE001
                return float("inf")

        window = 24 * 60 * 60  # last 24h
        recent = [f for f in backlog.features if _iso_age(f.created_at) <= window]
        healed = sum(1 for f in backlog.features if f.heal_attempts > 0)
        split_children = sum(1 for f in backlog.features if f.parent_id)
        hinted = sum(1 for f in backlog.features if f.heal_hint)

        # Aggregate block reasons so operators can tell at a glance which
        # failure mode dominates. Keyed off the heal_hint prefix rather than
        # full text — different prompts produce different long-form hints
        # but the prefix clusters by mode ("PRIOR FAILURE: tests failed"...).
        block_reasons: dict[str, int] = {}
        for f in backlog.features:
            if f.status != "blocked":
                continue
            hint = f.heal_hint or "(no hint — first block attempt)"
            # Take first 60 chars for clustering; full hint stays per-card.
            key = hint[:60].strip().rstrip(":").rstrip(".") or "(blank)"
            block_reasons[key] = block_reasons.get(key, 0) + 1
        top_reasons = sorted(block_reasons.items(), key=lambda kv: -kv[1])[:10]

        return {
            "stats": backlog.stats(),
            "recent_24h": {
                "total": len(recent),
                "done": sum(1 for f in recent if f.status == "done"),
                "blocked": sum(1 for f in recent if f.status == "blocked"),
            },
            "heal": {
                "features_with_heal_attempts": healed,
                "features_carrying_hint": hinted,
            },
            "block_reasons_top10": [{"reason": r, "count": c} for r, c in top_reasons],
            "splitter": {"child_features": split_children},
            "rate_limit": {
                "primary_cooldown_sec": max(0, int(_primary_cooldown_until - now_monotonic)),
                "fallback_cooldown_sec": max(0, int(_fallback_cooldown_until - now_monotonic)),
            },
            "circuit_breaker": circuit_state(),
        }

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> dict[str, Any]:
        raw = await request.body()
        sig = request.headers.get("x-hub-signature-256", "")
        if not _verify_github(sig, raw):
            raise HTTPException(status_code=401, detail="invalid signature")

        event = request.headers.get("x-github-event", "unknown")
        payload = await request.json()

        # Only PR review comments + issue comments carry actionable context for
        # the agent today. Everything else is acknowledged and ignored — the
        # agent decides whether to act on what we forward.
        if event in ("pull_request_review", "issue_comment"):
            body = (payload.get("comment") or {}).get("body") or (payload.get("review") or {}).get("body", "")
            repo = (payload.get("repository") or {}).get("full_name", "?")
            if body:
                await agent.ainvoke(
                    {"messages": [{"role": "user", "content": f"GitHub {event} on {repo}: {body}"}]}
                )
        return {"ok": True}

    return app


def _verify_github(signature_header: str, raw_body: bytes) -> bool:
    """HMAC-SHA256 verification. Rejects spoofed events before they reach the agent."""
    secret = settings.github_webhook_secret
    if not secret or not signature_header:
        # No secret configured = accept nothing, to fail safely.
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)
