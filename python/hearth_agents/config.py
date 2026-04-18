"""Configuration loaded from environment variables."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. All values come from env vars or .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # MiniMax (planning, research, PRDs)
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.io/v1"
    minimax_model: str = "MiniMax-M2.7"

    # Kimi (implementation - 76.8% SWE-Bench)
    kimi_api_key: str = ""
    kimi_base_url: str = "https://api.kimi.com/coding/v1"
    kimi_model: str = "kimi-for-coding"

    # Wikidelve (deep research service)
    wikidelve_url: str = ""

    # Langfuse (LLM observability). Empty keys disable tracing so local dev
    # and first-boot containers don't fail when the Langfuse stack isn't up.
    # Host defaults to the sibling container; override to a Tailscale hostname
    # when pointing a dev agent at a remote Langfuse instance.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://langfuse-web:3000"
    # Operator-facing URL (exposed via /config for the kanban's trace
    # deeplink). Empty → kanban hides the trace button. When set, kanban
    # links to ``<langfuse_public_url>/project?search=feature:<id>``.
    langfuse_public_url: str = ""
    # Worker autoscaling bounds. Workers grow toward max when pending
    # depth rises above high_water, shrink toward min when depth falls
    # under low_water. Set min == max to disable scaling; default stays
    # at settings.loop_workers for backwards compat.
    loop_workers_min: int = 1
    loop_workers_max: int = 0  # 0 means "use loop_workers as the ceiling"
    loop_autoscale_high_water: int = 20
    loop_autoscale_low_water: int = 4

    # Serper (web search fallback)
    serper_api_key: str = ""

    # Target repositories
    hearth_repo_path: str = "/repos/hearth"
    hearth_desktop_path: str = "/repos/hearth-desktop"
    hearth_mobile_path: str = "/repos/hearth-mobile"
    hearth_agents_path: str = "/repos/hearth-agents"

    # When True, the idea engine queues product features for all Hearth repos.
    # Default False: pause product-feature generation while block rate is high
    # (research flags <80% task success as capability mismatch, not tuning).
    # The agent still works on already-pending product features and all
    # self-improvements against hearth-agents. Flip via env when block rate
    # drops below 30% on a sustained window.
    product_features_enabled: bool = False

    # Budget & rate limits
    daily_budget_usd: float = 5.0
    per_feature_budget_usd: float = 2.0
    minimax_rate_limit: int = 4500  # per 5-hour window
    max_concurrent_subagents: int = 3
    loop_workers: int = 1
    # Wall-clock timeout for a single feature's agent.ainvoke call. Stopgap for
    # ``self-hard-kill-switch`` — prevents runaway features from chewing quota.
    per_feature_timeout_sec: int = 1800
    # Max in-loop fixup retries per feature before giving up and marking
    # blocked. Distinct from healer retries (HEAL_MAX_ATTEMPTS in healer.py).
    # Raise when Kimi/MiniMax have quota headroom and failures cluster on
    # "tests failed" — those benefit from another swing; lower when quota is
    # tight and marginal retries are chewing budget without closing features.
    max_fixups: int = 6
    # When both providers are healthy, what fraction of features should be
    # routed to the fallback (MiniMax)? 0.5 = even split, 1.0 = always MiniMax
    # when healthy, 0.0 = always Kimi. Raise when MiniMax has more headroom
    # than Kimi (typical: Kimi Allegretto weekly is tighter than MiniMax Max
    # 5h window). Clamped to [0.0, 1.0] before use.
    minimax_bias: float = 0.5
    # Operator override: force every ainvoke through one provider,
    # bypassing ping-pong + cooldown + circuit-breaker routing. Useful
    # for A/B testing ("run 1h through MiniMax only, see if Kimi is
    # the problem") or quota-conservation mode. Empty = routing as
    # normal. Values: "primary" | "fallback".
    force_provider: str = ""

    # Server
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    metrics_port: int = 9090

    # Telegram
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = Field(
        default="", description="Comma-separated chat IDs allowed to use the bot"
    )
    telegram_notify_chat_id: int = 0

    # Additional notification destinations (optional; each one is
    # independent of the others). Empty string disables that channel.
    # All destinations receive the same message, fan-out happens in
    # Notifier.send().
    slack_webhook_url: str = ""
    discord_webhook_url: str = ""
    # Outbound webhook fired on every feature transition. The POST body
    # is the transition JSON (feature_id, from, to, reason, actor,
    # prompts_version, ts). Use to pipe into an external dashboard,
    # ticketing system, or audit log. Empty = disabled.
    outbound_transition_webhook_url: str = ""

    # GitHub
    github_token: str = ""
    github_webhook_secret: str = ""
    # Alert webhook HMAC secret. Callers (PagerDuty, Grafana, Datadog)
    # must send sha256=HMAC(body, secret) in header ``x-alert-signature-256``.
    # Empty secret = accept-all (the default) — fine when the endpoint
    # is only reachable over Tailscale but a real operator should set
    # this once the endpoint is public.
    alert_webhook_secret: str = ""

    # Persistence
    backlog_path: str = "/data/backlog.json"
    feature_templates_path: str = "/data/feature_templates.json"

    @property
    def allowed_chat_ids(self) -> set[int]:
        return {int(s) for s in self.telegram_allowed_chat_ids.split(",") if s.strip()}

    @property
    def repo_paths(self) -> dict[str, str]:
        return {
            "hearth": self.hearth_repo_path,
            "hearth-desktop": self.hearth_desktop_path,
            "hearth-mobile": self.hearth_mobile_path,
            "hearth-agents": self.hearth_agents_path,
        }


settings = Settings()
