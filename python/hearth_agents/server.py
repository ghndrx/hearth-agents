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
        the kanban UI. Ordered newest-first so the board top is current work."""
        features = backlog.features
        if status:
            features = [f for f in features if f.status == status]
        return sorted(
            (f.to_dict() for f in features),
            key=lambda d: d["created_at"],
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

    @app.get("/stats")
    async def stats() -> dict[str, Any]:
        """Operational stats: backlog breakdown, recent velocity, split + heal
        activity, circuit-breaker state. Exists so operators can diagnose
        regressions without log-grepping."""
        import asyncio as _asyncio
        import time as _time

        from .loop import _fallback_cooldown_until, _primary_cooldown_until, circuit_state

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
            body = (payload.get("comment") or {}).get("body") or (payload.get("review") or {}).get(
                "body", ""
            )
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
