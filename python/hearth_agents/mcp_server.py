"""MCP stdio server exposing kanban ops as MCP tools.

Run:
  python -m hearth_agents.mcp_server

Designed for Claude Desktop to drive hearth-agents directly — same
tool surface the Telegram bot's kanban-ops agent uses, now reachable
from any MCP client. No new code paths: each MCP tool just forwards
to the HTTP server, keeping Backlog as the single source of truth.

Uses the official MCP Python SDK if available; falls back to a
minimal JSON-RPC-over-stdio implementation that covers the handshake
+ tools/list + tools/call methods. The fallback is intentional —
lets this ship without adding a new runtime dep.
"""

from __future__ import annotations

import json
import sys
import traceback
import urllib.parse
import urllib.request
from typing import Any, Callable

from .config import settings

_BASE = f"http://127.0.0.1:{settings.server_port}"


def _http(method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
    url = _BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# Tool registry: name → (schema, handler).
TOOLS: dict[str, tuple[dict, Callable[[dict], Any]]] = {
    "list_features": (
        {
            "description": "List features. Use query-DSL for AND filtering.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "e.g. status:blocked AND kind:bug"},
                    "status": {"type": "string"},
                    "kind": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
        lambda a: _http("GET", "/features", params={k: a.get(k) for k in ("query", "status", "kind", "limit")}),
    ),
    "show_feature": (
        {
            "description": "Get one feature plus its transition history.",
            "inputSchema": {
                "type": "object",
                "properties": {"feature_id": {"type": "string"}},
                "required": ["feature_id"],
            },
        },
        lambda a: _http("GET", f"/features/{urllib.parse.quote(a['feature_id'])}/history"),
    ),
    "feature_action": (
        {
            "description": "Apply a kanban action (approve|retry|nuke|cleanup_branch|fresh_retry).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "feature_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["approve", "retry", "nuke", "cleanup_branch", "fresh_retry"]},
                },
                "required": ["feature_id", "action"],
            },
        },
        lambda a: (
            _http("POST", f"/features/{urllib.parse.quote(a['feature_id'])}/replay-retry")
            if a.get("action") == "fresh_retry"
            else _http("POST", f"/features/{urllib.parse.quote(a['feature_id'])}/action", body={"action": a.get("action", "")})
        ),
    ),
    "queue_feature": (
        {
            "description": "Enqueue a feature or bug. Use kind=bug to require repro_command.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"},
                    "description": {"type": "string"},
                    "kind": {"type": "string", "default": "feature"},
                    "priority": {"type": "string", "default": "medium"},
                    "repos": {"type": "array", "items": {"type": "string"}},
                    "repro_command": {"type": "string"},
                    "acceptance_criteria": {"type": "string"},
                },
                "required": ["id", "name", "description"],
            },
        },
        lambda a: _http("POST", "/features", body=a),
    ),
    "stats": (
        {"description": "Backlog counts + 24h velocity + top block reasons + active workers.",
         "inputSchema": {"type": "object", "properties": {}}},
        lambda _a: _http("GET", "/stats"),
    ),
    "cost": (
        {"description": "Total spend + end-of-month forecast.",
         "inputSchema": {"type": "object", "properties": {}}},
        lambda _a: {"cost": _http("GET", "/cost-analytics"), "forecast": _http("GET", "/cost-analytics/forecast")},
    ),
    "health": (
        {"description": "Subsystem liveness snapshot.",
         "inputSchema": {"type": "object", "properties": {}}},
        lambda _a: _http("GET", "/health"),
    ),
    "dashboard": (
        {"description": "Per-repo dashboard.",
         "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}},
        lambda a: _http("GET", f"/dashboard/{urllib.parse.quote(a['repo'])}"),
    ),
}


def _respond(req_id: Any, result: Any = None, error: dict | None = None) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _handle(req: dict) -> None:
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        _respond(rid, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "hearth-agents", "version": "1.0.0"},
        })
    elif method == "tools/list":
        _respond(rid, {
            "tools": [{"name": n, **s} for n, (s, _) in TOOLS.items()],
        })
    elif method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            _respond(rid, error={"code": -32601, "message": f"unknown tool: {name}"})
            return
        _, handler = TOOLS[name]
        try:
            out = handler(args)
        except Exception as e:  # noqa: BLE001
            _respond(rid, error={"code": -32000, "message": f"{type(e).__name__}: {e}"})
            return
        _respond(rid, {"content": [{"type": "text", "text": json.dumps(out, indent=2)[:20000]}]})
    elif method == "notifications/initialized":
        # client ack — no response required
        pass
    elif rid is not None:
        _respond(rid, error={"code": -32601, "message": f"method not found: {method}"})


def _main_official_sdk() -> bool:
    """Try the official Anthropic MCP Python SDK if installed. Returns
    True when it handled the stdio loop, False when we should fall
    back to the hand-rolled JSON-RPC implementation below.

    The official SDK handles session lifecycle / sampling / resources
    more robustly than our minimal loop; prefer it when available.
    """
    try:
        from mcp.server import Server  # type: ignore[import-not-found]
        from mcp.server.stdio import stdio_server  # type: ignore[import-not-found]
        import mcp.types as mcp_types  # type: ignore[import-not-found]
    except ImportError:
        return False
    import asyncio
    server = Server("hearth-agents")

    @server.list_tools()
    async def _list_tools():  # type: ignore[no-untyped-def]
        return [
            mcp_types.Tool(name=name, description=schema["description"], inputSchema=schema["inputSchema"])
            for name, (schema, _) in TOOLS.items()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict):  # type: ignore[no-untyped-def]
        if name not in TOOLS:
            return [mcp_types.TextContent(type="text", text=f"unknown tool: {name}")]
        _, handler = TOOLS[name]
        out = handler(arguments or {})
        return [mcp_types.TextContent(type="text", text=json.dumps(out, indent=2)[:20000])]

    async def _run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    asyncio.run(_run())
    return True


def main() -> None:
    """Stdio loop. Prefer the official MCP SDK when available; fall
    back to the hand-rolled JSON-RPC implementation when not."""
    if _main_official_sdk():
        return
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _handle(req)
        except Exception:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            sys.stderr.flush()


if __name__ == "__main__":
    main()
