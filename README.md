# hearth-agents

Autonomous development system for [Hearth](https://github.com/ghndrx/hearth), a self-hosted Discord alternative.

Runs MiniMax M2.7 (research/planning) and Kimi K2.5 (implementation) to research features, write PRDs, implement code, and create PRs across multiple repositories — continuously, without human intervention.

## Architecture

```
Telegram (/status, /add, /budget, /wiki, /research)
    │
    ▼
Autonomous Loop
    │
    ├─ Research (MiniMax M2.7 + Wikidelve)
    ├─ PRD Generation (MiniMax M2.7)
    ├─ Implementation (Kimi K2.5 with tool calling)
    ├─ Quality Gates (tsc, eslint, vitest, build)
    └─ PR Creation (gh CLI)
    │
    ▼
Hearth Repos (feature branches + PRs)
```

## Agents

| Role | Model | What it does |
|------|-------|-------------|
| PRD Writer | MiniMax M2.7 | Product requirements from research |
| Architect | MiniMax M2.7 | Task decomposition, system design |
| Backend SWE | Kimi K2.5 | Go, PostgreSQL, Redis, WebSocket |
| Frontend SWE | Kimi K2.5 | SvelteKit, Svelte 5, TailwindCSS |
| Security SWE | Kimi K2.5 | OWASP, E2EE, CVE monitoring |
| QA SWE | Kimi K2.5 | Testing, Playwright E2E, k6 |
| Database SWE | Kimi K2.5 | Migrations, partitioning, indexing |
| DevOps SWE | Kimi K2.5 | Docker, Kubernetes, CI/CD |
| Fullstack SWE | Kimi K2.5 | End-to-end feature implementation |
| Reviewer | Kimi K2.5 | Code review against PRDs |
| Docs SWE | MiniMax M2.7 | API documentation, developer guides |

## Features

- **Autonomous loop** — picks features, researches, implements, creates PRs
- **Self-generating backlog** — generates its own next tasks when queue runs low
- **Idea engine** ��� analyzes completed work to suggest research directions
- **Wikidelve integration** — agents call deep research API during implementation
- **Self-healing** — fixes git conflicts, stale worktrees, API cooldowns
- **Circuit breaker** — auto-failover between MiniMax, Kimi, and OpenRouter
- **Token budget** — tracks spend per provider and per feature
- **Rate limiting** — respects MiniMax 5-hour rolling windows
- **Quality gates** — validates code before PR (typecheck, lint, test, build)
- **TDD mode** — writes failing tests first, then implements to pass
- **Repo guard** — blocks secrets, TODOs, AI slop from reaching product repos
- **AGENTS.md** — compound learning file per repo, grows with each feature
- **Prometheus metrics** — custom exporter on :9090/metrics
- **GitHub webhooks** — auto-fix from PR review comments and CI failures
- **Notification batching** — digest windows, per-feature threading
- **PR approval** — approve/reject PRs via Telegram inline keyboards

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Full project dashboard |
| `/backlog` | Feature queue with priorities |
| `/add <feature>` | Add a feature to the autonomous backlog |
| `/budget` | Token spend per provider and feature |
| `/health` | System health (providers, rate limits, memory) |
| `/wiki <query>` | Search wikidelve knowledge base |
| `/research <topic>` | Queue a deep research job |
| `/prd <desc>` | Generate a PRD |
| `/implement <file>` | Implement from a PRD |
| `/review <branch>` | Code review a branch |
| `/plan <feature>` | Decompose a feature into tasks |
| `/cancel <id>` | Cancel a running task |

## Setup

```bash
git clone https://github.com/ghndrx/hearth-agents.git
cd hearth-agents
npm install
cp .env.example .env
# Fill in API keys in .env
npm run dev
```

### Required Environment Variables

```
TELEGRAM_BOT_TOKEN     # From @BotFather
TELEGRAM_ALLOWED_USERS # Your Telegram user ID
MINIMAX_API_KEY        # MiniMax Token Plan key
KIMI_API_KEY           # Kimi Allegretto key (sk-kimi- prefix)
WIKIDELVE_URL          # Wikidelve instance URL
HEARTH_REPO_PATH       # Path to hearth repo (default: ../hearth)
```

### Optional

```
SERPER_API_KEY         # Google search via Serper
OPENROUTER_API_KEY     # Fallback provider
GITHUB_WEBHOOK_SECRET  # For auto-fix webhooks
DAILY_BUDGET_USD       # Daily spend cap (default: 5.0)
```

## Production

```bash
# PM2
npm run pm2:start

# systemd
sudo cp deploy/hearth-agents.service /etc/systemd/system/
sudo systemctl enable --now hearth-agents
```

## Target Repositories

- [hearth](https://github.com/ghndrx/hearth) — Go backend + SvelteKit frontend
- [hearth-desktop](https://github.com/ghndrx/hearth-desktop) — Tauri desktop app
- [hearth-mobile](https://github.com/ghndrx/hearth-mobile) — React Native mobile app

## Stack

- **Runtime**: Node.js + TypeScript (ESM)
- **Planning**: MiniMax M2.7 ($0.30/M input)
- **Implementation**: Kimi K2.5 (76.8% SWE-Bench)
- **Research**: Wikidelve deep research API
- **Telegram**: grammY
- **Job Queue**: SQLite (better-sqlite3)
- **Metrics**: Custom Prometheus exporter
