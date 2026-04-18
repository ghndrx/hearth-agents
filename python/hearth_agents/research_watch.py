"""Auto-synthesize landed wikidelve research.

Polls wikidelve every 30m for newly-completed articles whose topic
matches something we previously queued (tracked in
``/data/research_jobs.jsonl`` by ``wikidelve_research``'s
``record_job``). When a new one lands, runs ``wikidelve_synthesize``
and pings Telegram with the top recommendations.

Closes the research→digest loop without an operator polling the
wikidelve list. Cheap: one MiniMax call per new article.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from pathlib import Path

from .config import settings
from .heartbeat import beat
from .logger import log
from .notify import Notifier

POLL_INTERVAL_SEC = 30 * 60
SEEN_PATH = Path("/data/research_watch_seen.json")


def _load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        return set(json.loads(SEEN_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_seen(seen: set[str]) -> None:
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SEEN_PATH.write_text(json.dumps(sorted(seen)))
    except OSError as e:
        log.warning("research_watch_save_failed", err=str(e)[:200])


def _list_articles() -> list[dict]:
    if not settings.wikidelve_url:
        return []
    try:
        url = f"{settings.wikidelve_url}/api/articles?kb=personal&limit=500"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        if isinstance(data, dict):
            data = data.get("articles") or data.get("items") or []
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        log.warning("research_watch_list_failed", err=str(e)[:200])
        return []


async def _synthesize_and_notify(slug: str, notifier: Notifier) -> None:
    """Call /research/synthesize locally + post top recs."""
    payload = json.dumps({"kb": "personal", "slug": slug}).encode()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{settings.server_port}/research/synthesize",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Do the HTTP call in a thread so we don't block the event loop.
        def _call() -> dict:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        resp = await asyncio.to_thread(_call)
    except Exception as e:  # noqa: BLE001
        log.warning("research_watch_synth_failed", slug=slug[:40], err=str(e)[:200])
        return
    summary = (resp.get("summary") or "")[:200]
    recs = resp.get("recommendations") or []
    title_line = f"📚 new research landed: {slug[:60]}"
    body = f"\n{summary}"
    if recs:
        body += "\n\nTop recs:"
        for r in recs[:3]:
            body += f"\n  • {r.get('title','?')} ({r.get('leverage','?')})"
    await notifier.send(title_line + body)
    log.info("research_watch_synthesized", slug=slug[:60], rec_count=len(recs))


async def run_research_watch() -> None:
    """Background task."""
    beat("research_watch")
    notifier = Notifier()
    seen = _load_seen()
    # Seed with everything currently present so we don't spam on first boot.
    if not seen:
        seen = {a.get("slug", "") for a in _list_articles() if a.get("slug")}
        _save_seen(seen)
        log.info("research_watch_seeded", count=len(seen))
    try:
        while True:
            beat("research_watch")
            try:
                current = {a.get("slug", "") for a in _list_articles() if a.get("slug")}
                new_slugs = current - seen
                # Only synthesize articles that look like autonomous-agent
                # research — skip unrelated KB articles.
                targets = [s for s in new_slugs if "autonomous" in s.lower() or "agent" in s.lower()]
                for slug in sorted(targets)[:3]:  # cap per cycle to avoid MiniMax spend spike
                    await _synthesize_and_notify(slug, notifier)
                seen |= new_slugs
                if new_slugs:
                    _save_seen(seen)
            except Exception as e:  # noqa: BLE001
                log.warning("research_watch_tick_failed", err=str(e)[:200])
            await asyncio.sleep(POLL_INTERVAL_SEC)
    finally:
        await notifier.close()
