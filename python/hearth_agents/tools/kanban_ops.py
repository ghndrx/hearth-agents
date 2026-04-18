"""Kanban operator tools for the chat surface.

Exposes the common kanban actions as LangChain ``@tool`` decorated
functions so an LLM-driven bot can translate natural-language chat
("approve everything blocked by test failures", "what's costing the
most?") into structured backlog mutations. The ops talk to the HTTP
server running in the same process — FastAPI is the single source of
truth for Backlog state (sanitizer, validation, transition logging all
route through it) rather than having two paths to the same data.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from langchain_core.tools import tool

from ..config import settings

_BASE = f"http://127.0.0.1:{settings.server_port}"


def _req(method: str, path: str, body: dict | None = None, params: dict | None = None, timeout: int = 15) -> Any:
    url = _BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # type: ignore[name-defined]
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}


@tool
def kanban_list(query: str = "", status: str = "", kind: str = "", limit: int = 20) -> str:
    """List features. ``query`` is the query-DSL
    (e.g. ``status:blocked AND heal_attempts>=2``), ``status``/``kind``
    are exact-match shortcuts. Returns up to ``limit`` rows as a
    human-readable bullet list. Use this FIRST when the user's
    question mentions a condition — you'll often need the IDs before
    you can call kanban_act on them.
    """
    params: dict[str, Any] = {"limit": limit}
    if query:
        params["query"] = query
    if status:
        params["status"] = status
    if kind:
        params["kind"] = kind
    rows = _req("GET", "/features", params=params)
    if isinstance(rows, dict) and "error" in rows:
        return f"error: {rows['error']}"
    if not rows:
        return "(no matches)"
    lines = []
    for f in rows[:limit]:
        lines.append(
            f"- [{f.get('status','?')}] {f.get('priority','?')} {f.get('kind','?')} "
            f"{f.get('id')}: {f.get('name','')[:60]}"
            + (f" [heal {f.get('heal_attempts')}/3]" if f.get('heal_attempts') else "")
        )
    return f"Found {len(rows)}:\n" + "\n".join(lines)


@tool
def kanban_act(feature_id: str, action: str) -> str:
    """Apply a kanban action. ``action`` is one of:
      - ``approve``: mark blocked → done (human verified)
      - ``retry``:   reset heal + flip to pending (keeps heal_hint)
      - ``nuke``:    remove from backlog entirely (irreversible)
      - ``cleanup_branch``: delete origin branch + worktree for a done feature
      - ``fresh_retry``: clear heal_attempts AND heal_hint, then pending

    Use kanban_list first to find the feature_id.
    """
    if action == "fresh_retry":
        resp = _req("POST", f"/features/{urllib.parse.quote(feature_id)}/replay-retry")
    else:
        resp = _req("POST", f"/features/{urllib.parse.quote(feature_id)}/action", body={"action": action})
    if isinstance(resp, dict) and "error" in resp:
        return f"error: {resp['error']}"
    return json.dumps(resp)


@tool
def kanban_queue(
    id: str,
    name: str,
    description: str,
    kind: str = "feature",
    priority: str = "medium",
    repos: str = "hearth",
    repro_command: str = "",
    acceptance_criteria: str = "",
) -> str:
    """Enqueue a new feature or bug. ``kind`` is feature|bug|refactor|
    schema|security|incident|perf-revert. ``repos`` is a comma-separated
    list. ``repro_command`` is required when kind=bug.
    """
    body = {
        "id": id,
        "name": name,
        "description": description,
        "kind": kind,
        "priority": priority,
        "repos": [r.strip() for r in repos.split(",") if r.strip()],
    }
    if repro_command:
        body["repro_command"] = repro_command
    if acceptance_criteria:
        body["acceptance_criteria"] = acceptance_criteria
    resp = _req("POST", "/features", body=body)
    return json.dumps(resp)


@tool
def kanban_show(feature_id: str) -> str:
    """Return a feature's current state plus its last 5 transitions.
    Use this when the user asks about the status or history of one
    specific feature."""
    h = _req("GET", f"/features/{urllib.parse.quote(feature_id)}/history")
    if isinstance(h, dict) and "error" in h:
        return f"error: {h['error']}"
    f = h.get("feature") or {}
    lines = [
        f"{f.get('id')}: {f.get('name','')}",
        f"  status={f.get('status')} kind={f.get('kind')} priority={f.get('priority')} "
        f"heal={f.get('heal_attempts')}/3 risk={f.get('risk_tier','low')}",
    ]
    if f.get("depends_on"):
        lines.append(f"  depends_on: {', '.join(f['depends_on'])}")
    if f.get("heal_hint"):
        lines.append(f"  hint: {f['heal_hint'][:200]}")
    ts = h.get("transitions") or []
    if ts:
        lines.append(f"  last {min(5, len(ts))} transitions:")
        for t in ts[-5:]:
            lines.append(f"    {t.get('ts')} {t.get('from','-')} → {t.get('to')} [{t.get('actor','?')}]")
    return "\n".join(lines)


@tool
def kanban_stats() -> str:
    """Return backlog counts, 24h throughput, top block reasons, and
    active workers. Use when the user asks "how's it going?" or
    "what's broken?"."""
    s = _req("GET", "/stats")
    if "error" in s:
        return s["error"]
    lines = [
        f"Backlog: {s.get('stats', {})}",
        f"24h: {s.get('recent_24h', {})}",
    ]
    top = s.get("block_reasons_top10") or []
    if top:
        lines.append("Top block reasons:")
        for r in top[:5]:
            lines.append(f"  {r['count']}× {r['reason'][:70]}")
    workers = s.get("workers") or {}
    if workers:
        lines.append(f"Workers: {len(workers)} active")
        for wid, info in list(workers.items())[:6]:
            lines.append(f"  w{wid}: {info.get('feature','-')}")
    return "\n".join(lines)


@tool
def kanban_cost() -> str:
    """Return spend-to-date + end-of-month forecast."""
    c = _req("GET", "/cost-analytics")
    f = _req("GET", "/cost-analytics/forecast")
    if "error" in c or "error" in f:
        return f"error: {c.get('error') or f.get('error')}"
    return (
        f"total: ${c.get('total_cost_usd', 0):.4f} "
        f"(in:{c.get('total_input_tokens',0):,} out:{c.get('total_output_tokens',0):,})\n"
        f"p50 duration: {c.get('duration_percentiles',{}).get('p50','?')}s · "
        f"p95: {c.get('duration_percentiles',{}).get('p95','?')}s\n"
        f"month-to-date: ${f.get('month_to_date_usd', 0):.4f} · "
        f"forecast: ${f.get('forecast_usd', 0):.4f}"
    )


@tool
def kanban_health() -> str:
    """Return /health status + any stale background subsystems."""
    h = _req("GET", "/health")
    stale = [k for k, v in (h.get("subsystems") or {}).items() if v.get("stale")]
    return f"status={h.get('status')} stale_subsystems={stale or 'none'}"


@tool
def kanban_dashboard(repo: str) -> str:
    """Per-repo dashboard: status counts, kind breakdown, 24h throughput,
    7-day trendline, top block reasons. ``repo`` is the repo name
    (hearth, hearth-desktop, hearth-mobile, hearth-agents)."""
    d = _req("GET", f"/dashboard/{urllib.parse.quote(repo)}")
    if "error" in d:
        return d["error"]
    lines = [
        f"{repo}: total={d.get('total')} · 24h done={d.get('recent_24h',{}).get('done')} blocked={d.get('recent_24h',{}).get('blocked')}",
        f"  by_status: {d.get('by_status', {})}",
        f"  by_kind: {d.get('by_kind', {})}",
    ]
    top = d.get("top_block_reasons") or []
    if top:
        lines.append("  top blocks:")
        for r in top[:3]:
            lines.append(f"    {r['count']}× {r['reason'][:60]}")
    return "\n".join(lines)
