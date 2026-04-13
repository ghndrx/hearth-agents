"""Push notifications to a Telegram chat for autonomous-loop events."""

from __future__ import annotations

from aiogram import Bot

from .config import settings
from .logger import log


class Notifier:
    def __init__(self) -> None:
        self._bot: Bot | None = None
        if settings.telegram_bot_token and settings.telegram_notify_chat_id:
            self._bot = Bot(settings.telegram_bot_token)

    async def send(self, text: str) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send_message(settings.telegram_notify_chat_id, text[:4000])
        except Exception as e:
            log.warning("notify_failed", error=str(e))

    async def close(self) -> None:
        if self._bot is not None:
            await self._bot.session.close()
