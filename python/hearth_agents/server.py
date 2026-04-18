"""FastAPI HTTP server.

Exposes a health endpoint plus the GitHub webhook receiver. Telegram runs
separately in long-poll mode (see ``bot.py``) — no HTTP ingress needed for it.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import asyncio
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
        from .heartbeat import status as _status
        subs = _status()
        any_stale = any(v["stale"] for v in subs.values())
        return {
            "status": "degraded" if any_stale else "ok",
            "stats": backlog.stats(),
            "subsystems": subs,
        }

    def _eval_query(query: str, f) -> bool:  # type: ignore[no-untyped-def]
        """Tiny AND-only query language:
          status:blocked AND kind:bug AND heal_attempts>=2 AND priority:critical
        Supported fields: status, kind, priority, risk_tier, id, name (substring),
        heal_attempts (>=, >, <, <=, =), repos (membership test).
        Whitespace-split on AND; each clause is field OP value."""
        import re
        clauses = [c.strip() for c in re.split(r"\s+AND\s+", query, flags=re.IGNORECASE) if c.strip()]
        for clause in clauses:
            m = re.match(r"(\w+)\s*(>=|<=|=|:|>|<)\s*(.+)", clause)
            if not m:
                return False
            field, op, value = m.group(1).lower(), m.group(2), m.group(3).strip().strip('"\'')
            if field in ("status", "kind", "priority", "risk_tier", "id"):
                if op not in ("=", ":"):
                    return False
                if (getattr(f, field, "") or "") != value:
                    return False
            elif field == "name":
                if value.lower() not in (f.name or "").lower():
                    return False
            elif field == "repos":
                if value not in (f.repos or []):
                    return False
            elif field == "heal_attempts":
                try:
                    rhs = int(value)
                except ValueError:
                    return False
                actual = int(getattr(f, "heal_attempts", 0))
                if op == ">=" and not actual >= rhs: return False
                if op == ">" and not actual > rhs: return False
                if op == "<=" and not actual <= rhs: return False
                if op == "<" and not actual < rhs: return False
                if op in ("=", ":") and not actual == rhs: return False
            else:
                return False
        return True

    @app.get("/features")
    async def list_features(
        status: str | None = None,
        q: str | None = None,
        kind: str | None = None,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        """All features (or a single status slice) as lightweight dicts for
        the kanban UI. Ordered by last activity (updated_at desc) so the
        board top is the currently-moving work, not the oldest-birthday.

        ``q`` is a case-insensitive substring filter against id, name,
        description, and any heal_hint. ``kind`` is exact-match. Both
        compose with ``status``.
        """
        from .transitions import read_tail
        features = backlog.features
        if status:
            features = [f for f in features if f.status == status]
        if kind:
            features = [f for f in features if f.kind == kind]
        if q:
            ql = q.lower()
            features = [
                f for f in features
                if ql in (f.id or "").lower()
                or ql in (f.name or "").lower()
                or ql in (f.description or "").lower()
                or ql in (f.heal_hint or "").lower()
            ]
        if query:
            features = [f for f in features if _eval_query(query, f)]
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

    @app.post("/features/bulk")
    async def create_features_bulk(payload: dict[str, Any]) -> dict[str, Any]:
        """Enqueue many features at once. Body: {"features": [...]}.
        Each entry uses the same schema as POST /features. Returns
        per-entry outcome so callers can tell which failed validation
        and why. Useful for ingesting a project plan or quarterly
        roadmap as one upload."""
        items = payload.get("features") or []
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="features must be a list")
        results: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                results.append({"index": i, "ok": False, "error": "not an object"})
                continue
            try:
                # Reuse the single-feature handler for validation parity.
                resp = await create_feature(item)
                results.append({"index": i, "ok": True, "id": resp.get("id")})
            except HTTPException as e:
                results.append({"index": i, "ok": False, "error": e.detail})
        ok_count = sum(1 for r in results if r["ok"])
        return {"submitted": len(items), "ok": ok_count, "failed": len(items) - ok_count, "results": results}

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

    @app.get("/events")
    async def events() -> Any:
        """Server-Sent Events stream. Clients (kanban) subscribe and
        get one event per transition in ~real time instead of polling
        /features every 10s. Also sends keepalive pings every 15s so
        the connection doesn't idle-close behind proxies.

        Uses a simple in-memory subscriber queue — there's no
        durability; missed events between connection drops are not
        backfilled. Kanban re-fetches /features on reconnect anyway.
        """
        from fastapi.responses import StreamingResponse
        from .transitions import subscribe as _subscribe

        async def stream() -> Any:
            queue = _subscribe()
            yield "retry: 2000\n\n"
            try:
                while True:
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15)
                        import json as _json
                        yield f"event: transition\ndata: {_json.dumps(entry)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                pass

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/build")
    async def build_info() -> dict[str, Any]:
        """Return git sha + build timestamp + image digest so the
        operator can compare expected-vs-running code. Reads from
        /app/BUILD_INFO (written by Dockerfile) when present, falls
        back to .git inspection. An answer of ``unknown`` for git_sha
        means the running process was started outside docker build."""
        import os
        from pathlib import Path as _P
        info: dict[str, Any] = {"git_sha": "unknown", "git_branch": "unknown", "built_at": "unknown"}
        bi = _P("/app/BUILD_INFO")
        if bi.exists():
            try:
                for line in bi.read_text().splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        info[k.strip().lower()] = v.strip()
            except OSError:
                pass
        # Fallback: read from source-tree .git if mounted.
        if info["git_sha"] == "unknown":
            try:
                import subprocess
                r = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd="/app", capture_output=True, text=True, timeout=5, check=False,
                )
                if r.returncode == 0:
                    info["git_sha"] = r.stdout.strip()[:12]
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        info["process_started_at"] = os.environ.get("PROCESS_STARTED_AT", "unknown")
        from .transitions import prompts_version
        info["prompts_version"] = prompts_version()
        return info

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
    async def transitions(
        limit: int = 500,
        feature_id: str | None = None,
        prompts_version: str | None = None,
        actor: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent status-change entries with optional filtering.

        Filters compose: a request with both prompts_version=X and
        actor=Y returns transitions matching BOTH. Each filter is exact-
        match. Limit caps post-filter results.
        """
        from .transitions import read_tail
        capped = max(1, min(limit, 5000))
        # read_tail handles feature_id; apply other filters here.
        rows = read_tail(limit=20000, feature_id=feature_id) if feature_id else read_tail(limit=20000)
        if prompts_version:
            rows = [r for r in rows if r.get("prompts_version") == prompts_version]
        if actor:
            rows = [r for r in rows if r.get("actor") == actor]
        return rows[-capped:]

    @app.get("/prompt-analytics")
    async def prompt_analytics() -> dict[str, Any]:
        """Per-prompts_version done-rate + top failure clusters. Reads the
        transition log; no external state. Feeds the kanban analytics
        drawer and is the foundation for DSPy-style prompt compilation
        (research #3824)."""
        from .prompt_analyzer import analyze
        return analyze()

    @app.post("/research/synthesize")
    async def research_synthesize(payload: dict[str, Any]) -> dict[str, Any]:
        """Run wikidelve_synthesize on an article slug. Body:
          {"kb": "personal", "slug": "autonomous-..."}
        Returns the article summary + structured recommendations as
        parsed JSON (or {raw: true, summary: ...} when the LLM output
        didn't parse as JSON). Operator-triggered closure of the
        research→recommendations→Feature loop."""
        kb = (payload.get("kb") or "personal").strip()
        slug = (payload.get("slug") or "").strip()
        if not slug:
            raise HTTPException(status_code=400, detail="slug required")
        from .tools.wikidelve_synthesize import wikidelve_synthesize
        result = await wikidelve_synthesize.ainvoke({"kb": kb, "slug": slug})
        import json as _json
        try:
            return _json.loads(result)
        except _json.JSONDecodeError:
            return {"raw": True, "text": result[:4000]}

    @app.post("/backlog/repair")
    async def backlog_repair(payload: dict[str, Any]) -> dict[str, Any]:
        """Sync live state to match /backlog/replay projection.
        Destructive — use only when /backlog/replay reports drift that
        you want to heal. Body: {"dry_run": true|false}.

        In dry_run mode, returns the set of mutations that WOULD run
        without performing them. Honestly-run mode applies:
          - for each status_mismatch: Backlog.set_status(id, projection)
          - for each missing_in_projection id: remove from backlog
        Doesn't heal ``missing_in_live`` (that'd need re-hydrating an
        entire Feature row from transition data, which doesn't carry
        full Feature fields)."""
        from .transitions import read_tail
        projection: dict[str, str] = {}
        for t in read_tail(limit=100000):
            fid = t.get("feature_id") or ""
            to = t.get("to") or ""
            if not fid or not to:
                continue
            if to == "nuked":
                projection.pop(fid, None)
            else:
                projection[fid] = to
        live: dict[str, str] = {f.id: f.status for f in backlog.features}
        mismatches = [fid for fid in projection if fid in live and live[fid] != projection[fid]]
        missing_in_projection = [fid for fid in live if fid not in projection]
        dry_run = bool(payload.get("dry_run", False))
        if dry_run:
            return {
                "dry_run": True,
                "would_fix_status": [
                    {"id": fid, "live": live[fid], "projection": projection[fid]}
                    for fid in mismatches
                ],
                "would_remove": missing_in_projection,
            }
        fixed = 0
        for fid in mismatches:
            backlog.set_status(fid, projection[fid], reason="repair_to_projection", actor="webhook")  # type: ignore[arg-type]
            fixed += 1
        removed = 0
        for fid in missing_in_projection:
            ok, _ = backlog.action(fid, "nuke")
            if ok:
                removed += 1
        log.info("backlog_repaired", status_fixed=fixed, removed=removed)
        return {"dry_run": False, "status_fixed": fixed, "removed": removed}

    @app.get("/backlog/replay")
    async def backlog_replay() -> dict[str, Any]:
        """Rebuild Backlog state as a projection from transitions.jsonl.

        Useful to (a) confirm the live backlog matches the audit trail,
        (b) diagnose divergence when something looks off in the kanban
        vs the transition history. Does NOT mutate live state — it's
        a read-only projection showing what the backlog WOULD look
        like if you replayed every transition from scratch.

        Drift between projection and live state is itself a signal —
        usually means a direct mutation bypassed record_transition.
        """
        from .transitions import read_tail
        # Start with all features that ever had a transition, final
        # status derived from the last transition's "to".
        projection: dict[str, str] = {}
        ever_seen: set[str] = set()
        for t in read_tail(limit=100000):
            fid = t.get("feature_id") or ""
            to = t.get("to") or ""
            if not fid or not to:
                continue
            ever_seen.add(fid)
            if to == "nuked":
                projection.pop(fid, None)
            else:
                projection[fid] = to
        live: dict[str, str] = {f.id: f.status for f in backlog.features}
        missing_in_live = sorted(fid for fid in projection if fid not in live and projection.get(fid))
        missing_in_projection = sorted(fid for fid in live if fid not in projection)
        status_mismatches = sorted(
            fid for fid in projection
            if fid in live and live[fid] != projection[fid]
        )
        return {
            "projection_feature_count": len(projection),
            "live_feature_count": len(live),
            "missing_in_live": missing_in_live[:40],
            "missing_in_projection": missing_in_projection[:40],
            "status_mismatches": [
                {"id": fid, "projection": projection[fid], "live": live[fid]}
                for fid in status_mismatches[:40]
            ],
            "healthy": not (missing_in_live or missing_in_projection or status_mismatches),
        }

    @app.get("/backlog/export")
    async def backlog_export() -> list[dict[str, Any]]:
        """Full backlog snapshot as JSON. For migration between instances
        or diff against an earlier export. Use ``jq > backlog.json`` to
        save locally. NOT filtered — exports archive-eligible entries
        too, so a re-import restores exact state."""
        from dataclasses import asdict
        return [asdict(f) for f in backlog.features]

    @app.get("/backlog/snapshots")
    async def backlog_snapshots_list() -> list[str]:
        """List available snapshot dates for the kanban diff UI."""
        from pathlib import Path as _P
        snap_dir = _P("/data/backlog-snapshots")
        if not snap_dir.exists():
            return []
        return sorted(p.stem for p in snap_dir.glob("*.json"))

    @app.get("/backlog/diff")
    async def backlog_diff(from_date: str, to_date: str) -> dict[str, Any]:
        """Diff two daily snapshots from /data/backlog-snapshots/.
        Reports features ADDED (in 'to' not 'from'), REMOVED (in 'from'
        not 'to'), and STATUS_CHANGED (id present in both, status
        differs). Use to answer 'what moved overnight?' or
        'what regressed since the last good snapshot?'.

        Date format: YYYY-MM-DD. ``from_date`` < ``to_date`` enforced
        only via filename ordering — the underlying files are read
        as-is."""
        import json as _json
        from pathlib import Path as _P
        snap_dir = _P("/data/backlog-snapshots")
        from_path = snap_dir / f"{from_date}.json"
        to_path = snap_dir / f"{to_date}.json"
        if not from_path.exists():
            raise HTTPException(status_code=404, detail=f"snapshot not found: {from_date}")
        if not to_path.exists():
            raise HTTPException(status_code=404, detail=f"snapshot not found: {to_date}")
        try:
            from_features = {f["id"]: f for f in _json.loads(from_path.read_text())}
            to_features = {f["id"]: f for f in _json.loads(to_path.read_text())}
        except (OSError, _json.JSONDecodeError, KeyError) as e:
            raise HTTPException(status_code=500, detail=f"snapshot parse failed: {e}")
        added = [{"id": fid, "name": f.get("name"), "status": f.get("status")} for fid, f in to_features.items() if fid not in from_features]
        removed = [{"id": fid, "name": f.get("name"), "status": f.get("status")} for fid, f in from_features.items() if fid not in to_features]
        status_changed: list[dict[str, Any]] = []
        for fid, f_to in to_features.items():
            if fid not in from_features:
                continue
            if from_features[fid].get("status") != f_to.get("status"):
                status_changed.append({
                    "id": fid,
                    "name": f_to.get("name"),
                    "from": from_features[fid].get("status"),
                    "to": f_to.get("status"),
                })
        return {
            "from_date": from_date,
            "to_date": to_date,
            "added_count": len(added),
            "removed_count": len(removed),
            "status_changed_count": len(status_changed),
            "added": added,
            "removed": removed,
            "status_changed": status_changed,
        }

    @app.post("/webhooks/test")
    async def webhooks_test() -> dict[str, Any]:
        """Fire a canned payload at ``settings.outbound_transition_webhook_url``.
        Confirms your outbound-webhook receiver is reachable before wiring
        it to live traffic. Returns {ok, status_code, elapsed_ms} or an
        error. No side effects beyond the POST."""
        if not settings.outbound_transition_webhook_url:
            raise HTTPException(status_code=400, detail="outbound_transition_webhook_url not configured")
        import httpx
        import time as _t
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "feature_id": "webhook-test",
            "from": "pending",
            "to": "done",
            "reason": "test ping from /webhooks/test",
            "actor": "operator",
            "prompts_version": "test",
        }
        start = _t.time()
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(settings.outbound_transition_webhook_url, json=payload)
        except httpx.HTTPError as e:
            return {"ok": False, "error": str(e)[:200]}
        return {
            "ok": 200 <= r.status_code < 300,
            "status_code": r.status_code,
            "elapsed_ms": int((_t.time() - start) * 1000),
            "body": r.text[:200],
        }

    @app.post("/backlog/import-markdown")
    async def backlog_import_markdown(payload: dict[str, Any]) -> dict[str, Any]:
        """Parse a markdown-table roadmap into Features.

        Body: {"markdown": "...", "mode": "merge|replace"}. Expected
        header row: ``| id | name | description | priority | repos | kind |``
        Extra columns are ignored. ``priority`` and ``kind`` default when
        absent. Perfect for pasting a quarterly plan from a Google Doc
        into the system without hand-editing JSON.
        """
        md = payload.get("markdown", "")
        mode = payload.get("mode") or "merge"
        if not md.strip():
            raise HTTPException(status_code=400, detail="markdown body required")
        if mode not in ("merge", "replace"):
            raise HTTPException(status_code=400, detail="mode must be merge|replace")
        # Crude but sufficient: find the header row, split the body on |.
        lines = [line.strip() for line in md.splitlines() if line.strip().startswith("|")]
        if len(lines) < 3:
            raise HTTPException(status_code=400, detail="need at least header + separator + one data row")
        header = [c.strip().lower() for c in lines[0].strip("|").split("|")]
        # Skip the separator row (| --- | --- |).
        data_rows = [
            [c.strip() for c in line.strip("|").split("|")]
            for line in lines[2:]
        ]
        col_ix = {name: i for i, name in enumerate(header)}
        if "id" not in col_ix or "name" not in col_ix or "description" not in col_ix:
            raise HTTPException(status_code=400, detail="header must include id, name, description")
        from .backlog import Feature
        features: list[dict[str, Any]] = []
        for row in data_rows:
            def _c(name: str, default: str = "") -> str:
                i = col_ix.get(name)
                return row[i] if i is not None and i < len(row) else default
            if not _c("id") or not _c("name") or not _c("description"):
                continue
            features.append({
                "id": _c("id"),
                "name": _c("name"),
                "description": _c("description"),
                "priority": _c("priority", "medium") or "medium",
                "repos": [r.strip() for r in (_c("repos", "hearth") or "hearth").split(",")],
                "kind": _c("kind", "feature") or "feature",
            })
        resp = await backlog_import({"features": features, "mode": mode})
        resp["parsed_rows"] = len(features)
        return resp

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
        # 7-day daily trendline: done & blocked per day, scoped to this repo.
        trendline: dict[str, dict[str, int]] = {}
        from datetime import timedelta
        for offset in range(7):
            day = (datetime.now(timezone.utc) - timedelta(days=offset)).strftime("%Y-%m-%d")
            trendline[day] = {"done": 0, "blocked": 0}
        for f in features:
            day = (f.created_at or "")[:10]
            if day in trendline:
                if f.status == "done":
                    trendline[day]["done"] += 1
                elif f.status == "blocked":
                    trendline[day]["blocked"] += 1
        trend = sorted(
            ({"day": k, **v} for k, v in trendline.items()),
            key=lambda d: d["day"],
        )
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
            "trendline_7d": trend,
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

    @app.get("/schedule/preview")
    async def schedule_preview() -> list[dict[str, Any]]:
        """Show next-fire timestamp and time-until-fire for each schedule
        entry. Useful before saving an edit — confirm "weekly dep audit
        will fire next at <T>" without waiting for the 60s scheduler tick."""
        import json as _json
        import time as _t
        from pathlib import Path as _P
        path = _P("/data/schedule.json")
        if not path.exists():
            return []
        try:
            entries = _json.loads(path.read_text())
        except (OSError, _json.JSONDecodeError):
            return []
        now = _t.time()
        preview: list[dict[str, Any]] = []
        for e in entries if isinstance(entries, list) else []:
            if not isinstance(e, dict):
                continue
            every = float(e.get("every_hours") or 0)
            last = float(e.get("last_fire_ts") or 0)
            next_fire = last + every * 3600 if last > 0 else now
            preview.append({
                "name": e.get("name", ""),
                "every_hours": every,
                "last_fire_ts": last,
                "next_fire_ts": next_fire,
                "fires_in_sec": max(0, int(next_fire - now)),
                "feature_id_prefix": (e.get("feature") or {}).get("id_prefix", ""),
            })
        return sorted(preview, key=lambda p: p["fires_in_sec"])

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

    @app.get("/worker-metrics")
    async def worker_metrics() -> dict[str, Any]:
        """Per-worker historical win-rate keyed by kind. Walks the
        attempts log to attribute tool activity to workers, and the
        transitions log to attribute terminal verdicts. Shows which
        worker ID specializes in which kind — useful for affinity-
        based scheduling decisions."""
        import json as _json
        from collections import defaultdict
        from pathlib import Path as _P
        # transitions.jsonl records feature_id + to-status; we infer
        # kind via backlog lookup. A feature's current kind is stable
        # across its transitions.
        kind_by_id = {f.id: f.kind for f in backlog.features}
        # attempts.jsonl stores feature_id + ...; we don't record
        # worker_id there today. As a proxy: group by terminal
        # transitions and show done_count / blocked_count per kind.
        done_by_kind: dict[str, int] = defaultdict(int)
        blocked_by_kind: dict[str, int] = defaultdict(int)
        from .transitions import read_tail
        for t in read_tail(limit=30000):
            fid = t.get("feature_id") or ""
            k = kind_by_id.get(fid, "feature")
            status = t.get("to") or ""
            if status == "done":
                done_by_kind[k] += 1
            elif status == "blocked":
                blocked_by_kind[k] += 1
        # Current active workers + what they're on.
        from .loop import watchdog_state
        active = watchdog_state()
        by_kind = []
        for kind in sorted(set(done_by_kind) | set(blocked_by_kind)):
            d = done_by_kind[kind]
            b = blocked_by_kind[kind]
            total = d + b
            by_kind.append({
                "kind": kind,
                "done": d,
                "blocked": b,
                "done_rate": round(d / total, 3) if total else 0.0,
            })
        return {
            "active_workers": active,
            "by_kind": sorted(by_kind, key=lambda x: -x["done"]),
        }

    @app.get("/cost-analytics/forecast")
    async def cost_forecast() -> dict[str, Any]:
        """Project spend to end-of-month based on daily trend. Uses
        the last 7 days of /cost-analytics.daily as the trend window;
        forecasts linearly to the last day of the current month.
        Conservative — doesn't model growth curves or seasonality."""
        from .cost_analytics import analyze_costs
        from datetime import datetime, timezone
        import calendar
        d = analyze_costs()
        daily = d.get("daily", [])
        if not daily:
            return {"forecast_usd": 0.0, "trend_sample_days": 0, "end_of_month_days_ahead": 0}
        recent = daily[-7:]
        avg_daily = sum(x["cost_usd"] for x in recent) / max(1, len(recent))
        now = datetime.now(timezone.utc)
        last_day = calendar.monthrange(now.year, now.month)[1]
        days_ahead = max(0, last_day - now.day)
        month_so_far = sum(
            x["cost_usd"] for x in daily
            if x["day"].startswith(f"{now.year:04d}-{now.month:02d}")
        )
        return {
            "month_to_date_usd": round(month_so_far, 4),
            "trend_avg_daily_usd": round(avg_daily, 4),
            "trend_sample_days": len(recent),
            "end_of_month_days_ahead": days_ahead,
            "forecast_usd": round(month_so_far + avg_daily * days_ahead, 4),
        }

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

    @app.get("/features/{feature_id}/similar")
    async def feature_similar(feature_id: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return up to ``limit`` features with the highest name-similarity
        to the target. Uses difflib ratio on lowercased-alnum-normalized
        names — cheap, no embeddings required, good enough to catch
        obvious near-duplicates ("add logout button" vs "logout button
        in header"). Filters out the target itself and features in
        status=done (older ones that already shipped — operator cares
        about dupes in live work)."""
        import difflib
        target = next((f for f in backlog.features if f.id == feature_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="feature not found")
        def _norm(s: str) -> str:
            return "".join(c.lower() for c in s if c.isalnum() or c == " ").strip()
        target_norm = _norm(target.name)
        scored: list[tuple[float, Any]] = []
        for f in backlog.features:
            if f.id == feature_id or f.status == "done":
                continue
            ratio = difflib.SequenceMatcher(a=target_norm, b=_norm(f.name)).ratio()
            if ratio >= 0.4:
                scored.append((ratio, f))
        scored.sort(key=lambda r: -r[0])
        capped = max(1, min(limit, 50))
        return [
            {**f.to_dict(), "similarity": round(r, 3)}
            for r, f in scored[:capped]
        ]

    @app.post("/features/bulk-action")
    async def features_bulk_action(payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one action to every feature matching a query-DSL.
        Body: {"query": "status:blocked AND heal_attempts>=2",
               "action": "approve|retry|nuke|fresh_retry",
               "dry_run": true|false}

        dry_run=true returns the matching IDs without acting. Always
        run dry_run first on destructive actions (nuke, fresh_retry).
        Returns per-feature {id, ok, message}."""
        query = (payload.get("query") or "").strip()
        action = (payload.get("action") or "").strip()
        dry_run = bool(payload.get("dry_run", False))
        if not query or not action:
            raise HTTPException(status_code=400, detail="query and action required")
        if action not in ("approve", "retry", "nuke", "fresh_retry", "cleanup_branch"):
            raise HTTPException(status_code=400, detail="unknown action")
        matching = [f for f in backlog.features if _eval_query(query, f)]
        if dry_run:
            return {
                "dry_run": True,
                "matched": len(matching),
                "ids": [f.id for f in matching][:200],
            }
        results: list[dict[str, Any]] = []
        for f in matching:
            if action == "fresh_retry":
                f.heal_attempts = 0
                f.heal_hint = ""
                backlog.set_status(f.id, "pending", reason=f"bulk_action fresh_retry", actor="kanban")
                results.append({"id": f.id, "ok": True, "message": "fresh retry"})
            else:
                ok, msg = backlog.action(f.id, action)
                results.append({"id": f.id, "ok": ok, "message": msg})
        log.info("bulk_action", action=action, count=len(results))
        return {"dry_run": False, "count": len(results), "results": results[:200]}

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

    @app.get("/dep-graph")
    async def dep_graph() -> dict[str, Any]:
        """Return the feature dependency graph as nodes + edges, suitable
        for client-side rendering. Only includes features with deps OR
        features that are deps of others — drops the sea of standalone
        cards that would clutter the visualization."""
        edges: list[dict[str, str]] = []
        depended_on: set[str] = set()
        for f in backlog.features:
            for d in f.depends_on or []:
                edges.append({"from": d, "to": f.id})
                depended_on.add(d)
        relevant_ids = {e["from"] for e in edges} | {e["to"] for e in edges}
        nodes = [
            {
                "id": f.id,
                "name": f.name[:60],
                "status": f.status,
                "kind": f.kind,
                "blocked_by_deps": bool(f.depends_on) and not all(
                    any(g.id == d and g.status == "done" for g in backlog.features)
                    for d in f.depends_on
                ),
            }
            for f in backlog.features
            if f.id in relevant_ids
        ]
        return {"nodes": nodes, "edges": edges}

    @app.post("/admin/restart-task/{task_name}")
    async def restart_task(task_name: str) -> dict[str, Any]:
        """Re-spawn a wedged background task. Operator nudge — no full
        process restart needed. Looks up the named task in app.state.background_tasks
        (populated by main.py), cancels it if alive, then re-creates it
        with the original coroutine factory."""
        registry = getattr(app.state, "background_tasks", None)
        if registry is None:
            raise HTTPException(status_code=503, detail="background-task registry not wired yet")
        entry = registry.get(task_name)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown task '{task_name}'; known: {list(registry)}")
        task, factory = entry
        if not task.done():
            task.cancel()
        new_task = asyncio.create_task(factory())
        registry[task_name] = (new_task, factory)
        log.info("admin_task_restarted", task=task_name)
        return {"ok": True, "task": task_name, "previous_done": task.done()}

    @app.post("/webhooks/figma")
    async def figma_webhook(payload: dict[str, Any]) -> dict[str, Any]:
        """Figma → Feature ingest (research #3837). Expects a Figma
        file-update webhook payload OR a hand-crafted body with
        {file_key, component_name, description, repo?}.

        Emits a kind=feature Feature with the Figma file URL embedded
        in the description so the developer can pull the node tree via
        the Figma REST API during implementation. Keeps this module
        free of Figma-API credentials — the operator configures the
        developer side with FIGMA_TOKEN when they want to enable
        actual pixel-extraction.
        """
        from .backlog import Feature
        from .sanitize import sanitize as _sanitize
        file_key = (payload.get("file_key") or payload.get("fileKey") or "").strip()
        component = (payload.get("component_name") or payload.get("componentName") or "").strip()
        description = (payload.get("description") or "").strip()
        if not file_key:
            raise HTTPException(status_code=400, detail="file_key required")
        repo = payload.get("repo") or "hearth"
        import hashlib
        fid = f"figma-{hashlib.sha256((file_key + component).encode()).hexdigest()[:10]}"
        body_text = (
            f"Figma design to implement.\n\n"
            f"File: https://www.figma.com/file/{file_key}\n"
            f"Component: {component or '(whole file)'}\n\n"
            f"Description: {description or '(none)'}\n\n"
            "Fetch the node tree via the Figma REST API "
            "(GET /v1/files/{file_key}/nodes?ids=...), extract tokens "
            "(colors, typography, spacing), and emit framework-appropriate "
            "components. Match pixel-level where practical; keyboard + "
            "screen-reader affordances are non-negotiable. Run a11y_audit "
            "on the built output before committing."
        )
        sres = _sanitize(body_text, provenance=f"figma:{file_key}")
        if sres.rejected:
            raise HTTPException(status_code=400, detail=f"description rejected: {sres.reject_reason}")
        feature = Feature(
            id=fid,
            name=f"Figma: {component or file_key[:20]}",
            description=sres.safe_text,
            priority="medium",
            repos=[repo],  # type: ignore[list-item]
            kind="feature",
            acceptance_criteria=(
                "Implemented component matches the Figma node tree "
                "(tokens, spacing, variants); a11y_audit is clean; "
                "Storybook or Ladle entry exists."
            ),
        )
        added = backlog.add(feature)
        log.info("figma_ingested", file_key=file_key, component=component, added=added, feature_id=fid)
        return {"new": added, "feature_id": fid}

    @app.post("/webhooks/support")
    async def support_webhook(payload: dict[str, Any]) -> dict[str, Any]:
        """Customer-support ticket ingest (research #3844). Expects:
          {subject, body, urgency, repo?, dedupe_key?}

        Classifies urgency → risk_tier, runs a substring dedup against
        open Features (cheap first pass; semantic dedup would need an
        embedding service), emits a kind=bug Feature when new.
        Returns {new: bool, feature_id, matched_existing?}.
        """
        from .backlog import Feature
        from .sanitize import sanitize as _sanitize
        subject = (payload.get("subject") or "").strip()
        body = (payload.get("body") or "").strip()
        if not subject and not body:
            raise HTTPException(status_code=400, detail="subject or body required")
        urgency = (payload.get("urgency") or "medium").lower()
        repo = payload.get("repo") or "hearth"
        dedupe = (payload.get("dedupe_key") or subject[:40] or body[:40]).strip()
        sres = _sanitize(body or subject, provenance=f"support:{dedupe}", max_len=4000)
        if sres.rejected:
            raise HTTPException(status_code=400, detail=f"body rejected: {sres.reject_reason}")
        # Cheap dedup: subject substring match against open bug features.
        # Semantic dedup (iPACK) would need an embedding service.
        subject_lc = subject.lower()
        for f in backlog.features:
            if f.kind != "bug" or f.status == "done":
                continue
            if subject_lc and subject_lc in (f.name or "").lower():
                return {"new": False, "matched_existing": f.id, "status": f.status}
        risk_tier = (
            "high" if urgency in ("p1", "sev1", "critical", "high")
            else "medium" if urgency in ("p2", "sev2", "normal", "medium")
            else "low"
        )
        priority = "critical" if risk_tier == "high" else "high" if risk_tier == "medium" else "medium"
        import hashlib
        fid = f"support-{hashlib.sha256(dedupe.encode()).hexdigest()[:10]}"
        feature = Feature(
            id=fid,
            name=subject[:200] or body[:80],
            description=sres.safe_text,
            priority=priority,  # type: ignore[arg-type]
            repos=[repo],  # type: ignore[list-item]
            kind="bug",
            risk_tier=risk_tier,  # type: ignore[arg-type]
            acceptance_criteria=(
                "Support ticket resolved; repro_command fails before fix "
                "and passes after; regression test added."
            ),
        )
        added = backlog.add(feature)
        log.info("support_ingested", dedupe=dedupe, urgency=urgency, tier=risk_tier, added=added, feature_id=fid)
        return {"new": added, "feature_id": fid, "risk_tier": risk_tier}

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
