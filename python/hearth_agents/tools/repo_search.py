"""BM25 retrieval over target-repo source files.

Lets an agent ask "show me code in repo X similar to Y" before generating
new code, so implementations match existing patterns instead of starting
cold. Index is built lazily per repo on first query and cached in-process;
call ``repo_reindex`` to force a refresh after the repo's branch moves.

Kept deliberately simple — file-level BM25, no embeddings, no AST parsing.
Research on retrieval-augmented code generation names BM25 as the cheap,
effective baseline; we can upgrade to function-level or dense retrieval
once we measure real gaps.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool
from rank_bm25 import BM25Okapi

from ..config import settings
from ..logger import log

# File types we index — skip binaries, lockfiles, generated code, vendor dirs.
_INDEX_EXTS = {
    ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".svelte",
    ".rs", ".md", ".yaml", ".yml", ".toml",
}
_SKIP_PARTS = {
    "node_modules", ".git", "target", "dist", "build", ".svelte-kit",
    "__pycache__", ".venv", "venv", ".next", "vendor", "worktrees-hearth",
    "worktrees-hearth-desktop", "worktrees-hearth-mobile", "worktrees-hearth-agents",
}
_MAX_FILE_BYTES = 120_000  # skip generated/minified junk
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _iter_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        if p.suffix not in _INDEX_EXTS:
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield p


@lru_cache(maxsize=8)
def _build_index(repo_path: str) -> tuple[BM25Okapi, list[tuple[str, str]]] | None:
    """Return ``(bm25, [(relpath, body), ...])`` for a repo, or None on failure."""
    root = Path(repo_path)
    if not root.is_dir():
        log.warning("repo_search_no_root", repo_path=repo_path)
        return None
    corpus_tokens: list[list[str]] = []
    docs: list[tuple[str, str]] = []
    for p in _iter_files(root):
        try:
            body = p.read_text(errors="ignore")
        except OSError:
            continue
        tokens = _tokenize(body)
        if not tokens:
            continue
        corpus_tokens.append(tokens)
        docs.append((str(p.relative_to(root)), body))
    if not docs:
        return None
    log.info("repo_search_indexed", repo_path=repo_path, docs=len(docs))
    return BM25Okapi(corpus_tokens), docs


def _resolve_repo(repo: str) -> str | None:
    """Accept either a logical repo name or a path."""
    if repo in settings.repo_paths:
        return settings.repo_paths[repo]
    p = Path(repo)
    if p.is_dir():
        return str(p.resolve())
    return None


@tool
def repo_search(repo: str, query: str, limit: int = 5) -> str:
    """Find the most-relevant existing files in a target repo for a query.

    Use this BEFORE writing new code to discover prior patterns, similar
    functions, existing types, or the canonical spot for your change.

    Args:
        repo: Logical repo name (``hearth``, ``hearth-desktop``, ``hearth-mobile``,
              ``hearth-agents``) or an absolute path.
        query: Natural-language description of what you're looking for, or a
               function/type name. Tokenised and scored with BM25.
        limit: Max file matches to return (default 5).

    Returns:
        Newline-delimited ``path | score | first-matching-line`` entries.
    """
    resolved = _resolve_repo(repo)
    if not resolved:
        return f"Unknown repo: {repo}. Valid: {', '.join(settings.repo_paths)}"
    idx = _build_index(resolved)
    if idx is None:
        return f"No indexable files found under {resolved}"
    bm25, docs = idx
    q_tokens = _tokenize(query)
    if not q_tokens:
        return "Query produced no tokens."
    scores = bm25.get_scores(q_tokens)
    ranked = sorted(range(len(docs)), key=lambda i: -scores[i])[:limit]
    q_lower_tokens = set(q_tokens)
    lines: list[str] = []
    for i in ranked:
        if scores[i] <= 0:
            break
        path, body = docs[i]
        # Pull the first line containing any query token for context.
        preview = ""
        for line in body.splitlines():
            if any(t in line.lower() for t in q_lower_tokens):
                preview = line.strip()[:160]
                break
        lines.append(f"{path} | {scores[i]:.2f} | {preview}")
    return "\n".join(lines) or "(no matches above 0)"


@tool
def repo_reindex(repo: str) -> str:
    """Rebuild the in-memory BM25 index for a repo. Call after the branch moves
    or after pulling new changes; normal usage doesn't need this.

    Args:
        repo: Logical repo name or path.
    """
    resolved = _resolve_repo(repo)
    if not resolved:
        return f"Unknown repo: {repo}"
    _build_index.cache_clear()
    idx = _build_index(resolved)
    if idx is None:
        return f"Rebuilt but no indexable files under {resolved}"
    _, docs = idx
    return f"Rebuilt index for {resolved}: {len(docs)} files"
