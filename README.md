# hearth-agents

Autonomous development system for [Hearth](https://github.com/ghndrx/hearth), a self-hosted Discord alternative.

Single Python service built on LangChain DeepAgents. Drives Kimi K2.5 (76.8% SWE-Bench) for implementation and MiniMax M2.7 for planning/research. Runs a Telegram bot (aiogram), a GitHub webhook receiver (FastAPI), and an autonomous feature loop in one process, sharing one backlog and one DeepAgent instance.

## Architecture

```
 Telegram (long-poll)    GitHub webhooks (HMAC-verified)
         │                      │
         └────────┬─────────────┘
                  ▼
       python/ — single process
         ├─ aiogram bot
         ├─ FastAPI webhook receiver
         └─ autonomous loop
                  │
          DeepAgent orchestrator
           ├─ planner
           ├─ developer
           ├─ reviewer
           └─ security
                  │
                  ▼
         git worktrees → commits → PRs
```

## Running

```bash
cp .env.example .env   # fill in keys
docker compose up --build
```

Point Hearth checkouts at `$REPOS_DIR`. Agent writes to them via worktrees under `worktrees-<repo>/<branch>/`.

## Telegram commands

- `/status` — backlog stats
- `/features` — list features
- `/enqueue <id> | <name> | <description>` — add a feature
- any other message — one-shot agent query

## Development

```bash
cd python && uv sync --dev && uv run pytest
```
