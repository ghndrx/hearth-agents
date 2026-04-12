"""FastAPI HTTP server.

Exposes a health endpoint plus the GitHub webhook receiver. Telegram runs
separately in long-poll mode (see ``bot.py``) — no HTTP ingress needed for it.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .backlog import Backlog
from .config import settings
from .cost_tracker import CostTracker


def build_app(backlog: Backlog, agent: Any, cost_tracker: CostTracker | None = None) -> FastAPI:
    """Construct the FastAPI app with shared backlog + agent state."""
    app = FastAPI(title="hearth-agents", version="0.2.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "stats": backlog.stats()}

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

    # Cost tracking endpoints
    @app.get("/costs")
    async def get_all_costs() -> dict[str, Any]:
        """Get all tracked costs for all features."""
        if cost_tracker is None:
            raise HTTPException(status_code=503, detail="cost tracking not configured")
        return {"costs": cost_tracker.get_all_costs()}

    @app.get("/costs/{feature_id}")
    async def get_feature_cost(feature_id: str) -> dict[str, Any]:
        """Get cost summary for a specific feature."""
        if cost_tracker is None:
            raise HTTPException(status_code=503, detail="cost tracking not configured")
        return cost_tracker.get_feature_cost(feature_id)

    @app.delete("/costs/{feature_id}")
    async def reset_feature_cost(feature_id: str) -> dict[str, Any]:
        """Reset costs for a specific feature."""
        if cost_tracker is None:
            raise HTTPException(status_code=503, detail="cost tracking not configured")
        was_reset = await cost_tracker.reset_feature(feature_id)
        if not was_reset:
            raise HTTPException(status_code=404, detail=f"feature '{feature_id}' not found")
        return {"ok": True, "feature_id": feature_id, "message": "costs reset"}

    return app


def _verify_github(signature_header: str, raw_body: bytes) -> bool:
    """HMAC-SHA256 verification. Rejects spoofed events before they reach the agent."""
    secret = settings.github_webhook_secret
    if not secret or not signature_header:
        # No secret configured = accept nothing, to fail safely.
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature_header, expected)
