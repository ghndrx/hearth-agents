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

    # GitHub webhooks
    github_webhook_secret: str = ""

    # Persistence
    backlog_path: str = "/data/backlog.json"

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
