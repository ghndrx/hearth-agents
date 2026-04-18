"""Rule-based auto-labeler.

Scans a Feature's name + description for domain keywords and suggests
labels. Conservative — returns up to 3 labels and only for
high-confidence keyword matches. No LLM call, no async. Runs in-place
from /features POST when the operator didn't provide labels.

Intentionally simple (not an ML classifier). Operators can override
by passing explicit labels; auto-labels are ONLY applied to bare
creates. Keywords curated from the first ~500 Hearth features.
"""

from __future__ import annotations

# Ordered by specificity; first match within a category wins.
_RULES: dict[str, list[str]] = {
    "auth":         ["login", "logout", "session", "token", "jwt", "oauth", "sso", "mfa", "password"],
    "voice":        ["voice channel", "livekit", "webrtc", "mute", "speaker", "audio"],
    "e2ee":         ["e2ee", "megolm", "signal protocol", "encryption"],
    "federation":   ["matrix", "federation", "server-to-server", "activitypub"],
    "messaging":    ["message", "channel", "dm", "thread", "reaction", "reply"],
    "ui":           ["sidebar", "button", "modal", "form", "layout", "color", "theme", "hover"],
    "mobile":       ["react native", "expo", "hearth-mobile", "push notification"],
    "desktop":      ["tauri", "hearth-desktop", "tray", "menu bar"],
    "api":          ["endpoint", "route", "handler", "openapi", "rest", "/api/"],
    "schema":       ["migration", "alter table", "column", "database", "sqlc"],
    "security":     ["cve", "vulnerability", "sanitiz", "xss", "injection", "rate limit"],
    "a11y":         ["accessibility", "aria", "screen reader", "wcag", "keyboard nav"],
    "i18n":         ["localization", "translation", "locale", "rtl", "i18n"],
    "performance":  ["performance", "latency", "benchmark", "throughput", "slow"],
    "observability":["metric", "log", "trace", "grafana", "dashboard", "telemetry"],
    "self":         ["hearth-agents", "agent platform", "self-improvement", "prompts.py"],
}


def infer_labels(name: str, description: str, cap: int = 3) -> list[str]:
    """Return up to ``cap`` labels inferred from the text. Stable
    order (label key order in _RULES) so repeated calls don't churn
    the Feature dict. Empty when nothing matches."""
    haystack = (name + " " + description).lower()
    out: list[str] = []
    for label, keywords in _RULES.items():
        if any(k in haystack for k in keywords):
            out.append(label)
            if len(out) >= cap:
                break
    return out
