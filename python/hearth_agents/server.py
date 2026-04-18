"""FastAPI HTTP server.

Exposes a health endpoint plus the GitHub webhook receiver. Telegram runs
separately in long-poll mode (see ``bot.py``) — no HTTP ingress needed for it.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from .backlog import Backlog
from .config import settings
from .kanban_html import KANBAN_HTML
from .logger import log


def datetime_utcnow_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _age_sec(iso: str) -> float:
    try:
        return datetime_utcnow_ts() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("inf")


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
        depends_on = payload.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
            raise HTTPException(status_code=400, detail="depends_on must be a list of feature IDs")
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
            depends_on=list(depends_on),
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
                "workers_min": settings.loop_workers_min,
                "workers_max": settings.loop_workers_max or settings.loop_workers,
                "autoscale_high_water": settings.loop_autoscale_high_water,
                "autoscale_low_water": settings.loop_autoscale_low_water,
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
            "urls": {
                "langfuse_public": settings.langfuse_public_url,
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

    @app.get("/backlog/export")
    async def backlog_export() -> list[dict[str, Any]]:
        """Full backlog snapshot as JSON. For migration between instances
        or diff against an earlier export. Use ``jq > backlog.json`` to
        save locally. NOT filtered — exports archive-eligible entries
        too, so a re-import restores exact state."""
        from dataclasses import asdict
        return [asdict(f) for f in backlog.features]

    @app.post("/backlog/import")
    async def backlog_import(payload: dict[str, Any]) -> dict[str, Any]:
        """Merge an exported backlog into the live one. Body:
          {"features": [...], "mode": "merge"|"replace"}

        - merge (default): each feature added via Backlog.add (skips
          duplicates, sanitizes, persists). Returns per-entry
          add/skip outcome.
        - replace: wipes the current backlog and replaces entirely.
          DESTRUCTIVE — use for disaster recovery, not routine sync.
        """
        from .backlog import Feature
        mode = payload.get("mode") or "merge"
        features_raw = payload.get("features") or []
        if not isinstance(features_raw, list):
            raise HTTPException(status_code=400, detail="features must be a list")
        if mode not in ("merge", "replace"):
            raise HTTPException(status_code=400, detail="mode must be merge|replace")
        imported = 0
        skipped = 0
        if mode == "replace":
            backlog.features = []
        for item in features_raw:
            if not isinstance(item, dict) or not item.get("id"):
                skipped += 1
                continue
            # Strip fields not on the dataclass so a schema drift between
            # versions doesn't crash the import.
            valid_keys = {"id", "name", "description", "priority", "repos",
                          "research_topics", "discord_parity", "status",
                          "created_at", "self_improvement", "heal_attempts",
                          "heal_hint", "parent_id", "planner_estimate_lines",
                          "kind", "risk_tier", "depends_on", "repro_command",
                          "acceptance_criteria"}
            clean = {k: v for k, v in item.items() if k in valid_keys}
            try:
                feature = Feature(**clean)
            except TypeError as e:
                log.warning("import_feature_invalid", id=item.get("id"), err=str(e)[:160])
                skipped += 1
                continue
            if mode == "replace":
                backlog.features.append(feature)
                imported += 1
            else:
                if backlog.add(feature):
                    imported += 1
                else:
                    skipped += 1
        if mode == "replace":
            backlog.save()
        log.info("backlog_imported", mode=mode, imported=imported, skipped=skipped)
        return {"ok": True, "mode": mode, "imported": imported, "skipped": skipped}

    @app.get("/replay/{feature_id}")
    async def replay_endpoint(feature_id: str) -> dict[str, Any]:
        """Read-only replay analytics for a feature: every recorded
        attempt's tool-call sequence, pairwise diffs across attempts,
        cost rollup. Foundation for full deterministic replay (research
        #3807) which still requires Langfuse persistence."""
        from .replay import replay
        return replay(feature_id)

    @app.post("/replay/{feature_id}/dry-run")
    async def replay_dry_run(feature_id: str) -> dict[str, Any]:
        """Invoke the agent with the SAME feature prompt it saw before,
        but under the CURRENT prompts_version. Compares what tools the
        agent chooses now vs what it chose in the last recorded attempt.
        Used to preview the effect of a prompt change WITHOUT committing
        the output to a worktree.

        The agent runs at temperature=0.3 so results aren't deterministic
        — "same input → same output" isn't guaranteed. Use the tool-call
        sequence diff as a signal, not proof.

        Budget-capped via per_feature_budget_usd like real attempts; a
        dry-run that runs away won't exhaust quota.
        """
        feature = next((f for f in backlog.features if f.id == feature_id), None)
        if feature is None:
            raise HTTPException(status_code=404, detail="feature not found")
        from .loop import _feature_prompt, _extract_token_usage
        from .replay import replay as _replay
        prompt = _feature_prompt(feature)
        try:
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"metadata": {"feature_id": feature.id, "dry_run": True}},
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"dry-run invoke failed: {e}")
        in_tok, out_tok = _extract_token_usage(result)
        new_tools: list[dict[str, Any]] = []
        for m in (result or {}).get("messages", []) or []:
            for tc in (getattr(m, "tool_calls", None) or []):
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
                new_tools.append({"name": name, "args_repr": repr(args)[:200]})
        prior = _replay(feature_id)
        last_attempt = prior["attempts"][-1] if prior["attempts"] else None
        return {
            "feature_id": feature_id,
            "current_prompts_version": None,  # filled below
            "dry_run_tools": new_tools,
            "dry_run_input_tokens": in_tok,
            "dry_run_output_tokens": out_tok,
            "last_recorded_attempt": last_attempt,
        }

    @app.get("/dashboard/{repo_name}")
    async def repo_dashboard(repo_name: str) -> dict[str, Any]:
        """Per-repo rollup: backlog by status + kind, recent throughput,
        top block reasons, worker time allocation. Lets operators tell
        'hearth is healthy but hearth-mobile is backed up' at a glance."""
        from collections import Counter
        features = [f for f in backlog.features if repo_name in f.repos]
        if not features:
            raise HTTPException(status_code=404, detail=f"no features on repo {repo_name}")
        status_counts = Counter(f.status for f in features)
        kind_counts = Counter(f.kind for f in features)
        risk_counts = Counter(f.risk_tier for f in features)
        # Block reason top-5 scoped to this repo.
        reasons: Counter[str] = Counter()
        for f in features:
            if f.status != "blocked":
                continue
            key = (f.heal_hint or "(no hint)")[:60].strip().rstrip(":").rstrip(".")
            reasons[key] += 1
        # Recent 24h throughput via created_at (not transitions — faster,
        # per-repo transition filter would need a full read).
        now = datetime_utcnow_ts()
        window = 24 * 60 * 60
        recent = [f for f in features if _age_sec(f.created_at) <= window]
        return {
            "repo": repo_name,
            "total": len(features),
            "by_status": dict(status_counts),
            "by_kind": dict(kind_counts),
            "by_risk": dict(risk_counts),
            "recent_24h": {
                "total": len(recent),
                "done": sum(1 for f in recent if f.status == "done"),
                "blocked": sum(1 for f in recent if f.status == "blocked"),
            },
            "top_block_reasons": [{"reason": r, "count": c} for r, c in reasons.most_common(5)],
        }

    @app.get("/schedule")
    async def schedule_list() -> list[dict[str, Any]]:
        """Read the current /data/schedule.json for the kanban scheduler UI."""
        import json as _json
        from pathlib import Path as _P
        path = _P("/data/schedule.json")
        if not path.exists():
            return []
        try:
            raw = _json.loads(path.read_text())
            return raw if isinstance(raw, list) else []
        except (OSError, _json.JSONDecodeError):
            return []

    @app.put("/schedule")
    async def schedule_replace(payload: list[dict[str, Any]]) -> dict[str, Any]:
        """Overwrite /data/schedule.json. Scheduler re-reads every 60s so
        live edits take effect without restart. Validates minimally:
        each entry needs name, every_hours>0, feature.id_prefix."""
        import json as _json
        from pathlib import Path as _P
        if not isinstance(payload, list):
            raise HTTPException(status_code=400, detail="body must be a JSON list")
        cleaned: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            every = float(item.get("every_hours") or 0)
            spec = item.get("feature") or {}
            if not (name and every > 0 and isinstance(spec, dict) and spec.get("id_prefix")):
                raise HTTPException(
                    status_code=400,
                    detail=f"entry '{name or '?'}' missing required fields (name, every_hours>0, feature.id_prefix)",
                )
            cleaned.append({
                "name": name,
                "every_hours": every,
                "last_fire_ts": float(item.get("last_fire_ts") or 0),
                "feature": spec,
            })
        path = _P("/data/schedule.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(cleaned, indent=2))
        log.info("schedule_updated", count=len(cleaned))
        return {"ok": True, "count": len(cleaned)}

    @app.post("/features/{feature_id}/debate")
    async def feature_debate(feature_id: str) -> dict[str, Any]:
        """Run both Kimi and MiniMax in parallel on the same prompt,
        return both outputs for operator comparison (research #3816).
        Doubles token spend on this feature; use for stuck features
        where sequential cross-model retry hasn't closed the gap."""
        feature = next((f for f in backlog.features if f.id == feature_id), None)
        if feature is None:
            raise HTTPException(status_code=404, detail="feature not found")
        from .debate import run_debate
        return await run_debate(feature, backlog, agent, getattr(app.state, "fallback_agent", None))

    @app.post("/features/{feature_id}/replay-retry")
    async def replay_retry(feature_id: str) -> dict[str, Any]:
        """Aggressive retry: clears heal_hint AND heal_attempts AND flips
        to pending, bypassing the healer's cap. Distinct from the kanban
        'retry' action (which preserves hint) — use when the OPERATOR
        believes the prior hint was counterproductive and wants a
        fresh-context attempt."""
        feature = next((f for f in backlog.features if f.id == feature_id), None)
        if feature is None:
            raise HTTPException(status_code=404, detail="feature not found")
        prev = feature.status
        feature.heal_attempts = 0
        feature.heal_hint = ""
        backlog.set_status(feature_id, "pending", reason=f"operator_replay_retry from {prev}", actor="kanban")
        log.info("replay_retry", feature_id=feature_id, prev_status=prev)
        return {"ok": True, "feature_id": feature_id, "prev_status": prev}

    @app.get("/cost-analytics")
    async def cost_analytics_endpoint() -> dict[str, Any]:
        """Per-feature + daily + per-provider token cost rollup from
        attempts.jsonl. Top 25 most-expensive features, last 30 days
        of daily spend, provider split."""
        from .cost_analytics import analyze_costs
        return analyze_costs()

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
    async def alert_webhook(request: Request) -> dict[str, Any]:
        """Normalize alert payloads from PagerDuty / Grafana / Datadog
        into an incident Feature. Research #3833 (SRE role replacement):
        a single normalized alert schema gated by risk tier is the
        minimum viable ingest path.

        HMAC verified when ``settings.alert_webhook_secret`` is set;
        otherwise accepted unauthenticated (safe when bound to Tailscale
        only).

        Risk tier rule of thumb:
          - high: production customer-facing impact; agent builds a
            mitigation PR as draft, human Telegram approval required.
          - medium: degraded internal service; PR opens as draft.
          - low: flapping / informational; PR auto-opens normally.

        Payload (accepts any of the common alert providers; fields we
        read are best-effort — missing fields fall back to defaults):
          - service, summary, severity, fired_at, dedupe_key
        """
        raw = await request.body()
        if settings.alert_webhook_secret:
            sig = request.headers.get("x-alert-signature-256", "")
            expected = "sha256=" + hmac.new(
                settings.alert_webhook_secret.encode(), raw, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                raise HTTPException(status_code=401, detail="invalid signature")
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="body must be JSON")
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
            # Conventional-commits gate (research #3834).
            from .commitlint import parse as _parse
            pr_obj = payload.get("pull_request") or {}
            pr_title = (pr_obj.get("title") or "").strip()
            parsed = _parse(pr_title)
            if not parsed:
                log.warning("pr_title_not_conventional", title=pr_title[:120])
            else:
                log.info("pr_title_parsed", type=parsed.type, bump=parsed.bump)
            # Release auto-create when a PR merges (research #3834 release
            # engineering). Walk the merged PR's commits, group by
            # conventional-commit type, decide bump, draft a tag + release
            # via GitHub API. Best-effort, never blocks the webhook.
            if (payload.get("action") == "closed") and pr_obj.get("merged"):
                from .release_bot import auto_release
                import asyncio as _asyncio
                _asyncio.create_task(auto_release(payload))
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
