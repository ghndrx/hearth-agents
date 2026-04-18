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

    @app.post("/features")
    async def create_feature(payload: dict[str, Any]) -> dict[str, Any]:
        """Enqueue a new feature or bug. Body fields:
          - id (required): kebab-case identifier
          - name (required): human title
          - description (required): what to build or what's broken
          - kind: "feature" | "bug", default "feature"
          - priority: critical | high | medium | low, default medium
          - repos: list of repo names, default ["hearth"]
          - research_topics: list of strings, default []
          - discord_parity: string, default ""
          - repro_command: string (bugs only)
          - acceptance_criteria: string

        Lets external integrations (GitHub issue webhook, browser form,
        Telegram bot, CLI) push work into the backlog through one path.
        Applies the same sanitizer to description so a malicious issue
        body can't inject instructions via the agent prompt.
        """
        from .backlog import Feature
        from .sanitize import sanitize as _sanitize
        fid = (payload.get("id") or "").strip()
        name = (payload.get("name") or "").strip()
        desc_raw = (payload.get("description") or "").strip()
        if not fid or not name or not desc_raw:
            raise HTTPException(status_code=400, detail="id, name, description are required")
        desc_sres = _sanitize(desc_raw, provenance=f"http_enqueue:{fid}", max_len=4000)
        if desc_sres.rejected:
            raise HTTPException(status_code=400, detail=f"description rejected: {desc_sres.reject_reason}")
        kind = payload.get("kind") or "feature"
        if kind not in ("feature", "bug", "refactor", "schema", "security"):
            raise HTTPException(
                status_code=400,
                detail="kind must be feature|bug|refactor|schema|security",
            )
        if kind == "bug" and not (payload.get("repro_command") or "").strip():
            raise HTTPException(status_code=400, detail="bug requires repro_command")
        priority = payload.get("priority") or "medium"
        if priority not in ("critical", "high", "medium", "low"):
            raise HTTPException(status_code=400, detail="priority must be critical|high|medium|low")
        repos = payload.get("repos") or ["hearth"]
        if not isinstance(repos, list) or not repos:
            raise HTTPException(status_code=400, detail="repos must be a non-empty list")
        feature = Feature(
            id=fid,
            name=name,
            description=desc_sres.safe_text,
            priority=priority,  # type: ignore[arg-type]
            repos=[r for r in repos if isinstance(r, str)],  # type: ignore[arg-type]
            research_topics=payload.get("research_topics") or [],
            discord_parity=payload.get("discord_parity") or "",
            kind=kind,  # type: ignore[arg-type]
            repro_command=(payload.get("repro_command") or "")[:400],
            acceptance_criteria=(payload.get("acceptance_criteria") or "")[:800],
        )
        if not backlog.add(feature):
            raise HTTPException(status_code=409, detail="feature id or name already exists")
        log.info("http_enqueue", id=fid, kind=kind, priority=priority, repos=repos)
        return {"ok": True, "id": fid, "status": feature.status}

    @app.get("/features/{feature_id}/attempts")
    async def feature_attempts(feature_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent agent.ainvoke attempts for one feature from
        /data/attempts.jsonl. Useful for debugging why a feature keeps
        failing — shows the actual tool-call sequence per attempt +
        token spend. Foundation for replay tooling."""
        import json as _json
        from pathlib import Path as _P
        path = _P("/data/attempts.jsonl")
        if not path.exists():
            return []
        capped = max(1, min(limit, 500))
        try:
            with path.open("r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        matches: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if entry.get("feature_id") == feature_id:
                matches.append(entry)
        return matches[-capped:]

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

    @app.get("/prompt-analytics")
    async def prompt_analytics() -> dict[str, Any]:
        """Per-prompts_version done-rate + top failure clusters. Reads the
        transition log; no external state. Feeds the kanban analytics
        drawer and is the foundation for DSPy-style prompt compilation
        (research #3824)."""
        from .prompt_analyzer import analyze
        return analyze()

    @app.get("/repo-analytics")
    async def repo_analytics() -> dict[str, Any]:
        """Per-repo done-rate + block cluster. Answers 'is hearth-mobile
        harder to land than hearth?' — gives signal on whether repo-
        specific prompt variants would help."""
        from collections import Counter, defaultdict
        per_repo_done: Counter[str] = Counter()
        per_repo_blocked: Counter[str] = Counter()
        per_repo_reasons: dict[str, Counter[str]] = defaultdict(Counter)
        per_repo_kind: dict[str, Counter[str]] = defaultdict(Counter)
        for f in backlog.features:
            for r in f.repos:
                per_repo_kind[r][f.kind] += 1
                if f.status == "done":
                    per_repo_done[r] += 1
                elif f.status == "blocked":
                    per_repo_blocked[r] += 1
                    key = (f.heal_hint or "(no hint)")[:60].strip().rstrip(":").rstrip(".")
                    per_repo_reasons[r][key or "(blank)"] += 1
        repos: list[dict[str, Any]] = []
        for repo in sorted(set(list(per_repo_done) + list(per_repo_blocked) + list(per_repo_kind))):
            done = per_repo_done[repo]
            blocked = per_repo_blocked[repo]
            total = done + blocked
            repos.append({
                "repo": repo,
                "done": done,
                "blocked": blocked,
                "done_rate": round(done / total, 3) if total else 0.0,
                "kinds": dict(per_repo_kind[repo]),
                "top_reasons": [
                    {"reason": r, "count": c}
                    for r, c in per_repo_reasons[repo].most_common(3)
                ],
            })
        return {"repos": repos}

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
        from .loop import (
            _primary_cooldown_until,
            _fallback_cooldown_until,
            circuit_state,
            watchdog_state,
        )
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
            "workers": watchdog_state(),
        }

    @app.post("/webhooks/alert")
    async def alert_webhook(payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize alert payloads from PagerDuty / Grafana / Datadog
        into an incident Feature. Research #3833 (SRE role replacement):
        a single normalized alert schema gated by risk tier is the
        minimum viable ingest path.

        Risk tier rule of thumb:
          - high: production customer-facing impact; agent builds a
            mitigation PR as draft, human Telegram approval required.
          - medium: degraded internal service; PR opens as draft.
          - low: flapping / informational; PR auto-opens normally.

        Payload (accepts any of the common alert providers; fields we
        read are best-effort — missing fields fall back to defaults):
          - service, summary, severity, fired_at, dedupe_key
        """
        from .backlog import Feature
        from .sanitize import sanitize as _sanitize
        service = (payload.get("service") or payload.get("source") or "unknown")[:40]
        summary = (payload.get("summary") or payload.get("title") or payload.get("description") or "").strip()
        severity = (payload.get("severity") or payload.get("urgency") or "medium").lower()
        dedupe = (payload.get("dedupe_key") or payload.get("incident_key") or "")[:40]
        if not summary:
            raise HTTPException(status_code=400, detail="summary/title required")
        risk_tier = "high" if severity in ("critical", "p1", "sev1", "high") else "medium" if severity in ("warning", "p2", "sev2") else "low"
        # Sanitize the external summary — a compromised alert source
        # could carry injection.
        sres = _sanitize(summary, provenance=f"alert:{service}", max_len=4000)
        if sres.rejected:
            raise HTTPException(status_code=400, detail=f"summary rejected: {sres.reject_reason}")
        # Stable feature_id so repeated alerts on the same incident don't
        # create duplicates; use dedupe_key if provided, else timestamp.
        import time as _t
        fid = f"incident-{dedupe or int(_t.time())}"[:60]
        feature = Feature(
            id=fid,
            name=f"[{severity}] {service}: {summary[:80]}",
            description=sres.safe_text,
            priority="critical" if risk_tier == "high" else "high",
            repos=[payload.get("repo") or "hearth"],  # type: ignore[list-item]
            kind="incident",
            risk_tier=risk_tier,  # type: ignore[arg-type]
            acceptance_criteria="Service recovered; post-incident report filed as comment on PR",
        )
        added = backlog.add(feature)
        log.info("alert_ingested", service=service, severity=severity, tier=risk_tier, added=added, feature_id=fid)
        return {"ok": True, "feature_id": fid, "added": added, "risk_tier": risk_tier}

    @app.post("/webhooks/github")
    async def github_webhook(request: Request) -> dict[str, Any]:
        raw = await request.body()
        sig = request.headers.get("x-hub-signature-256", "")
        if not _verify_github(sig, raw):
            raise HTTPException(status_code=401, detail="invalid signature")

        event = request.headers.get("x-github-event", "unknown")
        payload = await request.json()

        # PR review / inline review comments / issue comments carry actionable
        # context. Research #3805 (PR review response loops) prescribes a
        # STRUCTURED routing: identify which feature the PR maps to, which
        # file+line the comment references, and prepend that context before
        # invoking the agent. Generic forwarding degrades to guessing.
        if event in ("pull_request_review", "pull_request_review_comment", "issue_comment"):
            from .pr_review import build_structured_prompt, apply_review_to_feature
            structured = build_structured_prompt(event, payload)
            if structured:
                # If the PR maps to one of our feat/<id> branches, the
                # handler may flip that feature back to pending with a
                # targeted heal_hint so the next loop pass applies the
                # suggestion. Orthogonal to calling the agent directly.
                apply_review_to_feature(backlog, structured)
                await agent.ainvoke(
                    {"messages": [{"role": "user", "content": structured["prompt"]}]}
                )
        elif event == "issues":
            # GitHub Issues → bug auto-ingest. When a new issue lands on
            # one of our repos, normalize it into a Feature.kind=bug and
            # let the loop pick it up. Only on action="opened" so we
            # don't spam features on every label change.
            if (payload.get("action") or "") == "opened":
                from .backlog import Feature
                from .sanitize import sanitize as _sanitize
                issue = payload.get("issue") or {}
                title = (issue.get("title") or "").strip()
                body = (issue.get("body") or "").strip()
                number = issue.get("number") or 0
                repo_full = (payload.get("repository") or {}).get("full_name", "")
                repo_short = repo_full.split("/")[-1] if "/" in repo_full else "hearth"
                if title:
                    sres = _sanitize(body or title, provenance=f"github_issue:{repo_full}#{number}", max_len=4000)
                    if not sres.rejected:
                        # Treat issues with /repro: in the body or title as bugs;
                        # everything else stays as a "feature" (enhancement request).
                        is_bug = "/repro:" in body.lower() or "[bug]" in title.lower() or any(
                            (l.get("name") or "").lower() == "bug" for l in (issue.get("labels") or [])
                        )
                        kind = "bug" if is_bug else "feature"
                        repro = ""
                        if is_bug and "/repro:" in body.lower():
                            # Pull the line after /repro: as the repro_command.
                            for line in body.splitlines():
                                if line.lower().startswith("/repro:"):
                                    repro = line.split(":", 1)[1].strip()
                                    break
                            if not repro:
                                repro = "(see issue body)"
                        feature_id = f"gh-{repo_short}-{number}"[:60]
                        feature = Feature(
                            id=feature_id,
                            name=title[:200],
                            description=sres.safe_text,
                            priority="high" if is_bug else "medium",
                            repos=[repo_short],  # type: ignore[list-item]
                            kind=kind,  # type: ignore[arg-type]
                            repro_command=repro[:200] if is_bug else "",
                        )
                        added = backlog.add(feature)
                        log.info("github_issue_ingested", feature_id=feature_id, added=added, kind=kind)
        elif event == "pull_request":
            # Conventional-commits gate (research #3834). Flag PRs whose
            # title doesn't parse as a conventional-commit header so the
            # agent (or a future bot) can comment back asking for a
            # rename. Non-blocking — just a log signal today.
            from .commitlint import parse as _parse
            pr_title = ((payload.get("pull_request") or {}).get("title") or "").strip()
            parsed = _parse(pr_title)
            if not parsed:
                log.warning("pr_title_not_conventional", title=pr_title[:120])
            else:
                log.info("pr_title_parsed", type=parsed.type, bump=parsed.bump)
        elif event == "workflow_run":
            # Live CI ingestion (research #3801): a failing GitHub Actions
            # run on one of our feat/ branches flips the feature back to
            # pending with a CI-specific heal_hint, so the healer routes
            # the next attempt with the real CI failure in context — not
            # just whatever our local verify_changes caught.
            from .ci_ingest import handle_workflow_run
            await handle_workflow_run(backlog, payload)
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
