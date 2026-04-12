"""LLM clients for MiniMax (planning) and Kimi (implementation).

Both providers speak the OpenAI protocol so we point ChatOpenAI at their base URLs.
The Kimi coding endpoint requires a ``claude-code/1.0.0`` user-agent header — that
quirk tripped us up in the TypeScript version.
"""

from langchain_core.callbacks import BaseCallbackHandler
from langchain_openai import ChatOpenAI

from .config import settings


def build_minimax(callbacks: list[BaseCallbackHandler] | None = None) -> ChatOpenAI:
    """MiniMax M2.7: 205K context, cheap, strong at planning and decomposition.

    Args:
        callbacks: Optional list of LangChain callback handlers for tracking/logging.
    """
    if not settings.minimax_api_key:
        raise RuntimeError("MINIMAX_API_KEY is required")
    return ChatOpenAI(
        model=settings.minimax_model,
        api_key=settings.minimax_api_key,
        base_url=settings.minimax_base_url,
        temperature=0.3,
        max_retries=3,
        timeout=180,
        callbacks=callbacks,
    )


def build_kimi(callbacks: list[BaseCallbackHandler] | None = None) -> ChatOpenAI:
    """Kimi K2.5: 76.8% SWE-Bench, best-in-class for code generation.

    ``sk-kimi-`` prefixed keys hit the coding endpoint which enforces a coding-agent
    user-agent (learned from upstream GitHub issues — see docs/README).

    Args:
        callbacks: Optional list of LangChain callback handlers for tracking/logging.
    """
    if not settings.kimi_api_key:
        raise RuntimeError("KIMI_API_KEY is required")

    default_headers: dict[str, str] = {}
    if settings.kimi_api_key.startswith("sk-kimi-"):
        default_headers["User-Agent"] = "claude-code/1.0.0"

    return ChatOpenAI(
        model=settings.kimi_model,
        api_key=settings.kimi_api_key,
        base_url=settings.kimi_base_url,
        temperature=0.3,
        max_retries=3,
        timeout=180,
        default_headers=default_headers or None,
        callbacks=callbacks,
    )
