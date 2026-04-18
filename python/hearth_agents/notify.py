"""Push notifications to a Telegram chat for autonomous-loop events.

Includes a process-wide coalescer so repeated events with the same key
collapse into one alert per ``min_interval_sec``. Applied across rate-limit,
healer-batch, circuit-breaker, and generic-failure paths — the categories
that spammed the channel every few minutes during sustained outages.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from aiogram import Bot

from .config import settings
from .logger import log

# Process-wide coalescer state. Module level so every Notifier instance shares
# it — multiple workers + healer + circuit-breaker all coordinate through this.
_last_sent_at: dict[str, float] = {}

# Default suppression window for coalesced alerts. An hour is long enough to
# stop spam during a full quota exhaustion but short enough that a recovering
# system will emit fresh alerts on the next real incident.
_DEFAULT_COALESCE_SEC = 60 * 60


class Notifier:
    def __init__(self) -> None:
        self._bot: Bot | None = None
        if settings.telegram_bot_token and settings.telegram_notify_chat_id:
            self._bot = Bot(settings.telegram_bot_token)

    async def send(self, text: str) -> None:
        """Unconditional send to every configured destination: Telegram,
        Slack, Discord. Returns as soon as the first reachable one
        succeeds; a failure on one channel doesn't block the others.

        Use for genuinely-rare events only (boot, feature_end done,
        human-escalation). Noisy channels should prefer send_coalesced."""
        truncated = text[:4000]
        tasks: list[asyncio.Task] = []
        if self._bot is not None:
            tasks.append(asyncio.create_task(self._send_telegram(truncated)))
        if settings.slack_webhook_url:
            tasks.append(asyncio.create_task(_send_slack(truncated)))
        if settings.discord_webhook_url:
            tasks.append(asyncio.create_task(_send_discord(truncated)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_telegram(self, text: str) -> None:
        if self._bot is None:
            return
        try:
            await self._bot.send_message(settings.telegram_notify_chat_id, text)
        except Exception as e:  # noqa: BLE001
            log.warning("notify_telegram_failed", error=str(e))

    async def send_coalesced(
        self, key: str, text: str, min_interval_sec: int = _DEFAULT_COALESCE_SEC
    ) -> bool:
        """Send only if we haven't sent for this ``key`` within ``min_interval_sec``.
        Returns True if sent, False if suppressed. Callers don't need to handle
        the False case — it just means the alert was deduped."""
        now = time.time()
        if now - _last_sent_at.get(key, 0.0) < min_interval_sec:
            return False
        _last_sent_at[key] = now
        await self.send(text)
        return True

    async def close(self) -> None:
        if self._bot is not None:
            await self._bot.session.close()


async def _send_slack(text: str) -> None:
    """POST to a Slack Incoming Webhook. Silent failure on network issues."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(settings.slack_webhook_url, json={"text": text})
            if r.status_code >= 300:
                log.warning("notify_slack_non2xx", status=r.status_code, body=r.text[:200])
    except httpx.HTTPError as e:
        log.warning("notify_slack_failed", error=str(e)[:160])


async def _send_discord(text: str) -> None:
    """POST to a Discord webhook. Silent failure on network issues."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(settings.discord_webhook_url, json={"content": text[:2000]})
            if r.status_code >= 300:
                log.warning("notify_discord_non2xx", status=r.status_code, body=r.text[:200])
    except httpx.HTTPError as e:
        log.warning("notify_discord_failed", error=str(e)[:160])
