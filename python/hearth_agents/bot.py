"""Telegram bot (aiogram v3).

Runs in long-poll mode as a background task inside the same process as the
FastAPI server and the autonomous loop. Shares one ``Backlog`` + one DeepAgent
instance with the rest of the app — no HTTP bridge, no shared secret, no second
container.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .backlog import Backlog, Feature
from .config import settings
from .logger import log


def build_dispatcher(backlog: Backlog, agent: Any) -> Dispatcher:
    """Wire handlers against the provided backlog + agent.

    Passing them in (rather than importing globals) keeps the bot testable and
    guarantees it shares state with the HTTP server.
    """
    dp = Dispatcher()
    allowed = settings.allowed_chat_ids

    # Allowlist middleware — silently drop messages from unauthorized chats so
    # we don't advertise the bot's existence to strangers.
    @dp.message.middleware()
    async def gate(handler, event: Message, data):  # type: ignore[no-untyped-def]
        if allowed and (event.chat.id not in allowed):
            return None
        return await handler(event, data)

    @dp.message(Command("start"))
    async def _start(msg: Message) -> None:
        await msg.answer(
            "Hearth operator. Commands:\n"
            "  /status /stats — backlog overview\n"
            "  /features /search <query-dsl> — list\n"
            "  /dash <repo> /forecast /recent — dashboards\n"
            "  /enqueue /bug — queue work\n"
            "  /approve /retry /nuke /debate — act on one\n"
            "  /schedule /deps /cost /who — ops views\n"
            "\n"
            "Or just chat naturally — 'what's blocked?', "
            "'nuke all the gh-* features', 'show me role-sort-order-api', "
            "'approve everything with CVE in the name'. "
            "The chat agent will pick the right tools."
        )

    @dp.message(Command("status"))
    async def _status(msg: Message) -> None:
        stats = backlog.stats()
        lines = [f"{k}: {v}" for k, v in stats.items()]
        await msg.answer("Backlog\n" + "\n".join(lines))

    @dp.message(Command("features"))
    async def _features(msg: Message) -> None:
        lines = [f"• [{f.status}] {f.priority} — {f.id}: {f.name}" for f in backlog.features]
        await msg.answer("\n".join(lines) or "(backlog empty)")

    @dp.message(Command("enqueue"))
    async def _enqueue(msg: Message, command: CommandObject) -> None:
        args = command.args or ""
        parts = [p.strip() for p in args.split("|")]
        if len(parts) < 3:
            await msg.answer("Usage: /enqueue <id> | <name> | <description>")
            return
        feature = Feature(id=parts[0], name=parts[1], description=parts[2])
        if not backlog.add(feature):
            await msg.answer(f"Feature {feature.id} already exists.")
            return
        await msg.answer(f"Queued: {feature.id}")

    @dp.message(Command("stats"))
    async def _stats(msg: Message) -> None:
        from .loop import circuit_state, watchdog_state
        stats = backlog.stats()
        reasons: dict[str, int] = {}
        for f in backlog.features:
            if f.status != "blocked":
                continue
            key = (f.heal_hint or "(no hint)")[:50].strip().rstrip(":")
            reasons[key] = reasons.get(key, 0) + 1
        top = sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
        cb = circuit_state()
        workers = watchdog_state()
        text = [
            "*Backlog*",
            *[f"  {k}: {v}" for k, v in stats.items()],
            "",
            f"*Circuit*: open={cb['open']} rate={cb.get('block_rate', 0):.0%}",
            "*Active workers*: " + str(len(workers)),
        ]
        if top:
            text += ["", "*Top block reasons*"] + [f"  {c}×  {r[:60]}" for r, c in top]
        await msg.answer("\n".join(text))

    @dp.message(Command("bug"))
    async def _bug(msg: Message, command: CommandObject) -> None:
        parts = [p.strip() for p in (command.args or "").split("|")]
        if len(parts) < 4:
            await msg.answer("Usage: /bug <id> | <name> | <desc> | <repro_command>")
            return
        feature = Feature(
            id=parts[0], name=parts[1], description=parts[2],
            kind="bug", repro_command=parts[3], priority="high",
        )
        if not backlog.add(feature):
            await msg.answer(f"Bug {feature.id} already exists.")
            return
        await msg.answer(f"Queued bug: {feature.id}")

    @dp.message(Command("approve"))
    async def _approve(msg: Message, command: CommandObject) -> None:
        fid = (command.args or "").strip()
        if not fid:
            await msg.answer("Usage: /approve <feature_id>")
            return
        ok, message = backlog.action(fid, "approve")
        await msg.answer(message)

    @dp.message(Command("retry"))
    async def _retry(msg: Message, command: CommandObject) -> None:
        fid = (command.args or "").strip()
        if not fid:
            await msg.answer("Usage: /retry <feature_id>")
            return
        ok, message = backlog.action(fid, "retry")
        await msg.answer(message)

    @dp.message(Command("nuke"))
    async def _nuke(msg: Message, command: CommandObject) -> None:
        fid = (command.args or "").strip()
        if not fid:
            await msg.answer("Usage: /nuke <feature_id>")
            return
        ok, message = backlog.action(fid, "nuke")
        await msg.answer(message)

    @dp.message(Command("cost"))
    async def _cost(msg: Message) -> None:
        from .cost_analytics import analyze_costs
        d = analyze_costs()
        lines = [
            f"Total spend: ${d['total_cost_usd']:.3f}",
            f"Tokens: {d['total_input_tokens']:,} in / {d['total_output_tokens']:,} out",
            "",
            "Top 3 features by cost:",
        ]
        for f in d.get("top_features", [])[:3]:
            lines.append(f"  ${f['cost_usd']:.4f}  {f['attempts']}× — {f['feature_id']}")
        if d.get("daily"):
            today = d["daily"][-1]
            lines += ["", f"Today: ${today['cost_usd']:.4f} ({today['day']})"]
        await msg.answer("\n".join(lines))

    @dp.message(Command("who"))
    async def _who(msg: Message) -> None:
        from .loop import watchdog_state
        w = watchdog_state()
        if not w:
            await msg.answer("No active workers.")
            return
        lines = ["Active workers:"]
        for wid, info in sorted(w.items(), key=lambda kv: int(kv[0])):
            lines.append(f"  w{wid}: {info['feature']} ({info['age_sec']}s ago)")
        await msg.answer("\n".join(lines))

    @dp.message(Command("schedule"))
    async def _schedule(msg: Message) -> None:
        """Preview upcoming scheduled firings."""
        import json as _json
        from pathlib import Path as _P
        path = _P("/data/schedule.json")
        if not path.exists():
            await msg.answer("No schedule configured. Set up via /kanban → schedule button.")
            return
        try:
            entries = _json.loads(path.read_text())
        except Exception:
            await msg.answer("Schedule file unreadable.")
            return
        if not entries:
            await msg.answer("Schedule is empty.")
            return
        import time as _t
        now = _t.time()
        lines = ["Scheduled features:"]
        for e in entries if isinstance(entries, list) else []:
            every = float(e.get("every_hours") or 0)
            last = float(e.get("last_fire_ts") or 0)
            next_fire = last + every * 3600 if last > 0 else now
            fires_in_h = max(0.0, (next_fire - now) / 3600)
            lines.append(f"  {e.get('name','?')}: every {every}h, next in {fires_in_h:.1f}h")
        await msg.answer("\n".join(lines))

    @dp.message(Command("deps"))
    async def _deps(msg: Message, command: CommandObject) -> None:
        """Show dependency state — a specific feature's deps OR a
        summary of features currently blocked by unfinished deps."""
        fid = (command.args or "").strip()
        if fid:
            f = next((x for x in backlog.features if x.id == fid), None)
            if f is None:
                await msg.answer(f"Feature {fid} not found.")
                return
            if not f.depends_on:
                await msg.answer(f"{fid} has no declared dependencies.")
                return
            lines = [f"Deps for {fid}:"]
            for dep in f.depends_on:
                dep_feat = next((x for x in backlog.features if x.id == dep), None)
                if dep_feat is None:
                    lines.append(f"  {dep}: (not in backlog)")
                else:
                    mark = "✅" if dep_feat.status == "done" else "⏳"
                    lines.append(f"  {mark} {dep}: {dep_feat.status}")
            await msg.answer("\n".join(lines))
            return
        # Summary mode.
        blocked_by_deps = []
        for f in backlog.features:
            if not f.depends_on or f.status != "pending":
                continue
            unfinished = [d for d in f.depends_on if not any(g.id == d and g.status == "done" for g in backlog.features)]
            if unfinished:
                blocked_by_deps.append((f.id, unfinished))
        if not blocked_by_deps:
            await msg.answer("No pending features are blocked by deps.")
            return
        lines = [f"{len(blocked_by_deps)} features blocked by deps:"]
        for fid, unfin in blocked_by_deps[:10]:
            lines.append(f"  {fid} waits for: {', '.join(unfin)}")
        await msg.answer("\n".join(lines))

    @dp.message(Command("debate"))
    async def _debate(msg: Message, command: CommandObject) -> None:
        fid = (command.args or "").strip()
        if not fid:
            await msg.answer("Usage: /debate <feature_id>\nRuns Kimi + MiniMax in parallel; doubles spend.")
            return
        feature = next((f for f in backlog.features if f.id == fid), None)
        if feature is None:
            await msg.answer(f"Feature {fid} not found.")
            return
        await msg.answer(f"🗣 Starting debate on {fid}. This doubles token spend; results in ~1-3 min.")
        from .debate import run_debate
        # Dig fallback agent out of where main.py stashed it. Bot starts
        # before main wires app.state, so we re-import models here.
        from .models import build_minimax
        try:
            fallback = build_minimax()
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"debate aborted: fallback agent unavailable ({e})")
            return
        try:
            from .agent import build_fallback_agent
            fb_agent = build_fallback_agent()
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"debate aborted: fallback agent build failed ({e})")
            return
        try:
            res = await run_debate(feature, backlog, agent, fb_agent)
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"debate failed: {e}")
            return
        if "error" in res:
            await msg.answer(f"debate skipped: {res['error']}")
            return
        lines = [f"🗣 Debate on {fid}:"]
        for r in res.get("results", []):
            tag = r.get("tag", "?")
            if r.get("error"):
                lines.append(f"  {tag}: ERROR — {r['error']}")
                continue
            lines.append(
                f"  {tag}: {r['tool_count']} tool calls, "
                f"{r['input_tokens']:,}in/{r['output_tokens']:,}out"
            )
        await msg.answer("\n".join(lines))

    @dp.message(Command("search"))
    async def _search(msg: Message, command: CommandObject) -> None:
        q = (command.args or "").strip()
        if not q:
            await msg.answer("Usage: /search <query-dsl>\nExample: /search status:blocked AND kind:bug")
            return
        matches = []
        from . import backlog as _b  # noqa: F401 (import for server access pattern)
        # Delegate through the HTTP query endpoint using the same DSL as
        # the kanban operator — keeps behavior identical.
        import urllib.parse, urllib.request, json as _json
        url = f"http://127.0.0.1:{settings.server_port}/features?query={urllib.parse.quote(q)}&limit=20"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                matches = _json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"search failed: {e}")
            return
        if not matches:
            await msg.answer("(no matches)")
            return
        lines = [f"Matched {len(matches)}:"]
        for f in matches[:15]:
            lines.append(f"  [{f['status']:12s}] {f['kind']:10s} {f['id']}: {f['name'][:55]}")
        await msg.answer("\n".join(lines))

    @dp.message(Command("dash"))
    async def _dash(msg: Message, command: CommandObject) -> None:
        repo = (command.args or "hearth").strip()
        import urllib.parse, urllib.request, json as _json
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{settings.server_port}/dashboard/{urllib.parse.quote(repo)}",
                timeout=10,
            ) as r:
                d = _json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"dash failed: {e}")
            return
        lines = [
            f"*{repo}* · total={d.get('total')}",
            f"by_status: {d.get('by_status')}",
            f"24h: done={d.get('recent_24h',{}).get('done')} blocked={d.get('recent_24h',{}).get('blocked')}",
        ]
        for r in (d.get("top_block_reasons") or [])[:3]:
            lines.append(f"  {r['count']}× {r['reason'][:60]}")
        await msg.answer("\n".join(lines))

    @dp.message(Command("forecast"))
    async def _forecast(msg: Message) -> None:
        import urllib.request, json as _json
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{settings.server_port}/cost-analytics/forecast", timeout=10,
            ) as r:
                f = _json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"forecast failed: {e}")
            return
        await msg.answer(
            f"month-to-date: ${f.get('month_to_date_usd',0):.4f}\n"
            f"trend: ${f.get('trend_avg_daily_usd',0):.4f}/d over {f.get('trend_sample_days',0)}d\n"
            f"forecast eom: ${f.get('forecast_usd',0):.4f}"
        )

    @dp.message(Command("recent"))
    async def _recent(msg: Message, command: CommandObject) -> None:
        try:
            limit = int((command.args or "15").strip())
        except ValueError:
            limit = 15
        import urllib.request, json as _json
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{settings.server_port}/transitions?limit={limit}", timeout=10,
            ) as r:
                rows = _json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            await msg.answer(f"recent failed: {e}")
            return
        if not rows:
            await msg.answer("(no transitions yet)")
            return
        lines = [f"Last {len(rows)} transitions:"]
        for t in rows:
            lines.append(f"  {t.get('ts','')[11:19]} {t.get('feature_id','?'):30s} {t.get('from','-')} → {t.get('to','?')} [{t.get('actor','?')}]")
        await msg.answer("\n".join(lines)[:4000])

    # Any non-command text routes to the KANBAN agent — a slim chat-operator
    # agent with only the kanban-ops tools. Heavyweight product agent is
    # reserved for /enqueue's freeform research flow.
    _kanban_agent = {"instance": None}

    @dp.message(F.text & ~F.text.startswith("/"))
    async def _freeform(msg: Message) -> None:
        await msg.bot.send_chat_action(chat_id=msg.chat.id, action="typing")
        if _kanban_agent["instance"] is None:
            try:
                from .agent import build_kanban_agent
                _kanban_agent["instance"] = build_kanban_agent()
                log.info("kanban_agent_built")
            except Exception as e:  # noqa: BLE001
                log.warning("kanban_agent_build_failed", err=str(e)[:200])
                # Fallback: use the main agent (heavier but still works).
                _kanban_agent["instance"] = agent
        try:
            result = await _kanban_agent["instance"].ainvoke(
                {"messages": [{"role": "user", "content": msg.text}]}
            )
            messages = result.get("messages", [])
            reply = messages[-1].content if messages else ""
            await msg.answer(reply[:4000] or "(empty reply)")
        except Exception as e:
            log.exception("bot_invoke_failed", error=str(e))
            await msg.answer(f"Error: {e}")

    return dp


async def run_bot(backlog: Backlog, agent: Any) -> None:
    """Long-poll loop. Cancel the task to stop."""
    if not settings.telegram_bot_token:
        log.info("bot_disabled", reason="no_telegram_token")
        return
    bot = Bot(settings.telegram_bot_token)
    dp = build_dispatcher(backlog, agent)
    me = await bot.get_me()
    log.info("bot_online", username=me.username)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
