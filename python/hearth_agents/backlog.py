"""Feature backlog for the autonomous loop.

This is a Python port of the TypeScript backlog. The loop pulls the next
``pending`` feature, hands it to the DeepAgent, and the agent marks it ``done``
when its subagents finish implementation.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

Priority = Literal["critical", "high", "medium", "low"]
Status = Literal["pending", "researching", "implementing", "reviewing", "done", "blocked"]
Repo = Literal["hearth", "hearth-desktop", "hearth-mobile"]


@dataclass
class Feature:
    id: str
    name: str
    description: str
    priority: Priority = "medium"
    repos: list[Repo] = field(default_factory=lambda: ["hearth"])
    research_topics: list[str] = field(default_factory=list)
    discord_parity: str = ""
    status: Status = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# Initial backlog. The idea engine appends to this over time.
INITIAL_FEATURES: list[Feature] = [
    Feature(
        id="matrix-federation",
        name="Matrix Federation for E2EE",
        description=(
            "Implement Matrix protocol federation with Megolm/Vodozemac group encryption. "
            "Uniquely positions Hearth against Discord which has no federation."
        ),
        priority="critical",
        repos=["hearth"],
        research_topics=[
            "Matrix server-to-server API implementation in Go with gomatrixserverlib",
            "Matrix Megolm Vodozemac E2EE for federated chat",
        ],
        discord_parity="Server federation (competitive advantage — Discord has none)",
    ),
    Feature(
        id="voice-channels-always-on",
        name="Always-On Voice Channels",
        description=(
            "Discord-style persistent voice channels. Drop-in/drop-out, voice activity detection, "
            "push-to-talk, current-user sidebar display."
        ),
        priority="high",
        repos=["hearth", "hearth-desktop"],
        research_topics=[
            "LiveKit persistent voice channel architecture",
            "WebRTC voice activity detection VAD patterns",
        ],
        discord_parity="Core Discord feature",
    ),
    Feature(
        id="noise-suppression",
        name="Noise Suppression in Voice Channels",
        description=(
            "RNNoise-based ML noise cancellation for voice channels. Users immediately notice "
            "the quality difference vs Discord's Krisp-powered suppression."
        ),
        priority="high",
        repos=["hearth", "hearth-desktop", "hearth-mobile"],
        research_topics=[
            "RNNoise WebAssembly integration for browser-based noise suppression",
            "LiveKit audio track processor API for client-side ML",
        ],
        discord_parity="Discord uses Krisp — we match with open-source equivalent",
    ),
]


class Backlog:
    """In-memory backlog with optional JSON persistence."""

    def __init__(self, persist_path: str | None = None):
        self.features: list[Feature] = list(INITIAL_FEATURES)
        self._path = Path(persist_path) if persist_path else None
        if self._path and self._path.exists():
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        data = json.loads(self._path.read_text())
        self.features = [Feature(**f) for f in data]

    def save(self) -> None:
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps([asdict(f) for f in self.features], indent=2))

    def next_pending(self) -> Feature | None:
        priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        candidates = [f for f in self.features if f.status == "pending"]
        candidates.sort(key=lambda f: priority_order.get(f.priority, 99))
        return candidates[0] if candidates else None

    def set_status(self, feature_id: str, status: Status) -> None:
        for f in self.features:
            if f.id == feature_id:
                f.status = status
                self.save()
                return

    def add(self, feature: Feature) -> bool:
        """Append a feature. Returns False if the ID already exists."""
        if any(f.id == feature.id for f in self.features):
            return False
        self.features.append(feature)
        self.save()
        return True

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.features:
            counts[f.status] = counts.get(f.status, 0) + 1
        counts["total"] = len(self.features)
        return counts
