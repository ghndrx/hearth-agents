"""LLM clients for MiniMax (planning) and Kimi (implementation).

Both providers speak the OpenAI protocol so we point ChatOpenAI at their base URLs.
The Kimi coding endpoint requires a ``claude-code/1.0.0`` user-agent header — that
quirk tripped us up in the TypeScript version.
"""

from functools import lru_cache

from langchain_openai import ChatOpenAI

from .config import settings
from .logger import log


@lru_cache(maxsize=1)
def _langfuse_callbacks() -> list:
    """Return a list with one LangfuseCallbackHandler when keys are configured,
    else []. Cached so we don't pay the import + handshake cost on every
    build_* call — the handler is safe to share across clients.

    Import is deferred so the package stays optional: when langfuse isn't
    installed (local dev, lean CI) we silently fall back to no-op callbacks.
    """
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return []
    try:
        from langfuse.callback import CallbackHandler  # langfuse v2
    except ImportError:
        try:
            from langfuse.langchain import CallbackHandler  # langfuse v3
        except ImportError:
            log.warning("langfuse_not_installed", action="skipping_callbacks")
            return []
    try:
        handler = CallbackHandler(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        log.info("langfuse_callbacks_enabled", host=settings.langfuse_host)
        return [handler]
    except Exception as e:  # noqa: BLE001
        log.warning("langfuse_init_failed", err=str(e)[:200])
        return []


def build_minimax() -> ChatOpenAI:
    """MiniMax M2.7: 205K context, cheap, strong at planning and decomposition.

    Explicit httpx timeouts added alongside the LangChain-level `timeout`
    so a hung TCP connection fails fast instead of holding a worker for
    the full 180s LangChain budget + OS TCP retransmit (which in
    gateway-01 infra-pressure incidents stretched to ~10m per call).
    """
    if not settings.minimax_api_key:
        raise RuntimeError("MINIMAX_API_KEY is required")
    import httpx
    return ChatOpenAI(
        model=settings.minimax_model,
        api_key=settings.minimax_api_key,
        base_url=settings.minimax_base_url,
        temperature=0.3,
        max_retries=3,
        timeout=180,
        http_client=httpx.Client(timeout=httpx.Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0)),
        callbacks=_langfuse_callbacks() or None,
    )


def build_kimi() -> ChatOpenAI:
    """Kimi K2.5: 76.8% SWE-Bench, best-in-class for code generation.

    ``sk-kimi-`` prefixed keys hit the coding endpoint which enforces a coding-agent
    user-agent (learned from upstream GitHub issues — see docs/README).
    """
    if not settings.kimi_api_key:
        raise RuntimeError("KIMI_API_KEY is required")

    default_headers: dict[str, str] = {}
    if settings.kimi_api_key.startswith("sk-kimi-"):
        default_headers["User-Agent"] = "claude-code/1.0.0"

    # Kimi's coding endpoint enables "thinking" mode by default, which requires
    # each subsequent turn to echo back ``reasoning_content``. LangChain's
    # ChatOpenAI doesn't preserve that field, so every multi-turn tool call
    # fails with "reasoning_content is missing". Disabling thinking sidesteps
    # the whole class of errors; Kimi still beats everything on SWE-Bench without it.
    return ChatOpenAI(
        model=settings.kimi_model,
        api_key=settings.kimi_api_key,
        base_url=settings.kimi_base_url,
        temperature=0.3,
        max_retries=3,
        timeout=180,
        default_headers=default_headers or None,
        extra_body={"thinking": {"type": "disabled"}},
        http_client=__import__("httpx").Client(
            timeout=__import__("httpx").Timeout(connect=10.0, read=180.0, write=10.0, pool=10.0),
            headers=default_headers or None,
        ),
        callbacks=_langfuse_callbacks() or None,
    )

