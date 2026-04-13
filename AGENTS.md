# AGENTS.md — hearth-agents (autonomous dev loop)

## Stack
- Python 3.11+ with `uv`/`pip`
- LangChain + DeepAgents + LangGraph
- FastAPI HTTP server, aiogram for Telegram
- pytest for tests, ruff + mypy
- Runs in Docker Compose (`/opt/hearth-agents/docker-compose.yml`)
- Default branch: `main`

## Commands
- Test: `cd python && pytest tests -x -q`
- Lint: `cd python && ruff check .`
- Typecheck: `cd python && mypy hearth_agents`
- Format: `cd python && ruff format .`

## Conventions
- Commit format: Conventional Commits. Scope = module (`feat(loop):`, `fix(verify):`).
- Branch from `main`, name `feat/<feature-id>`.
- Module layout: `python/hearth_agents/<module>.py`, tests in `python/tests/`.
- Async-first: use `async def` for anything touching httpx, aiogram, LangChain.
- Keep per-feature changes focused — if your diff exceeds ~400 lines, split the feature.

## Do not touch without explicit task
- `Dockerfile`, `docker-compose.yml` — production runtime
- `.env` / `.env.example` — secrets live here, never commit real values
- `python/uv.lock` — regenerate with `uv lock`, don't hand-edit

## Security
- No secrets in source. All config via `pydantic-settings` reading env vars.
- Telegram chat IDs, API keys, webhook secrets — env only.
- Shell tool (`tools/shell.py`) runs arbitrary commands; don't expose it to untrusted input.

## This repo is self-dogfooding
- The agent system improves itself. When editing `loop.py`, `verify.py`, `idea_engine.py`,
  `prompts.py`, or `notify.py`, the next run of the agent will read your change.
- Self-improvement features (`self_improvement=True`) run single-threaded; product features
  run on multiple workers (`LOOP_WORKERS`).
- External verifier in `verify.py` is the ground truth — if your feature doesn't push a
  remote branch and pass tests, it's `blocked` regardless of what the agent claims.
