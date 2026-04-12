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
            "Hearth agent online. Commands: /status, /features, "
            "/enqueue <id> | <name> | <desc>. Anything else is a one-shot query."
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

    # Any non-command text becomes a one-shot agent invocation.
    @dp.message(F.text & ~F.text.startswith("/"))
    async def _freeform(msg: Message) -> None:
        await msg.bot.send_chat_action(chat_id=msg.chat.id, action="typing")
        try:
            result = await agent.ainvoke({"messages": [{"role": "user", "content": msg.text}]})
            messages = result.get("messages", [])
            reply = messages[-1].content if messages else ""
            # Telegram caps messages at 4096; truncate rather than split to keep
            # the bot's behavior predictable — long answers belong in PRs, not DMs.
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
