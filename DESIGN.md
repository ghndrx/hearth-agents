# hearth-agents — design document

> Autonomous SDLC harness. Takes natural-language feature / bug / incident
> requests and ships reviewable GitHub PRs across the Hearth polyrepo
> without per-feature human attention. Failures loop through self-correction
> until they land, time out, or escalate.

---

## 1. Goals + non-goals

### Goals
1. **Ship product features from text description to merged PR** without a
   human in the loop for the happy path.
2. **Self-correct on failure** — if tests fail, diff explodes, worktree
   abandons, the system routes itself through recovery without prompting.
3. **Operator-visible state** — at any moment, one command / one page
   tells me backlog shape, spend, block causes, worker assignments,
   subsystem liveness.
4. **Observability per prompt version** — every prompt change is A/B'd
   automatically against its predecessor's done-rate.
5. **Chat-first operator surface** — Telegram is the primary interface,
   kanban is a read-through cache.
6. **Defence in depth** — sanitizer on all external ingest, budget caps,
   risk-tier gating, HMAC on webhooks, per-provider circuit breakers.

### Non-goals
- Not a hosted service. Runs on one machine (gateway-01).
- Not model-agnostic. Hardcoded to Kimi-K2.6-Code primary + MiniMax-M2.7
  fallback. DeepAgents (LangChain) is the orchestrator harness.
- Not a bug tracker. Backlog is in-process + JSON persisted; no ticketing
  concepts like assignees or sprints at the data level.
- Not multi-tenant. One Backlog instance, one operator.
- Not real-time — per-feature turnaround is minutes to hours, not seconds.

---

## 2. High-level architecture

```
┌──────────────────────────────── gateway-01 (Docker Compose) ─────────────────────────────┐
│                                                                                          │
│  ┌────────────────────────────┐                   ┌──────────────────────────────┐       │
│  │  hearth-agents-agent-1     │  traces/spans     │  langfuse-web                │       │
│  │  (FastAPI + asyncio loop)  │ ────────────────▶ │  + langfuse-postgres         │       │
│  │                            │                   └──────────────────────────────┘       │
│  │  ┌──────────────────────┐  │                                                          │
│  │  │ uvicorn :8000        │◀─┼── tailscale-hearth sidecar ◀── hearth.walleye-frog.ts.net│
│  │  │   /kanban /features  │  │                                                          │
│  │  │   /webhooks/* ...    │  │                                                          │
│  │  └──────────────────────┘  │                                                          │
│  │                            │                                                          │
│  │  14 background asyncio:    │                                                          │
│  │    • N workers (autoscaled)│                                                          │
│  │    • watchdog + autoscaler │                                                          │
│  │    • healer                │                                                          │
│  │    • idea_engine           │                                                          │
│  │    • worktree_gc           │                                                          │
│  │    • digest (daily)        │                                                          │
│  │    • drift_alarm           │                                                          │
│  │    • archive (daily)       │                                                          │
│  │    • scheduler (cron)      │                                                          │
│  │    • stuck_feature_escal   │                                                          │
│  │    • self_imp_seeder       │                                                          │
│  │    • snapshot (daily)      │                                                          │
│  │    • bot (Telegram)        │                                                          │
│  └────────────────────────────┘                                                          │
│                                                                                          │
│  Persistence:                                                                            │
│    /data/backlog.json                — live Feature list                                 │
│    /data/transitions.jsonl           — append-only status-change log                     │
│    /data/attempts.jsonl              — per-ainvoke tool-call + tokens + duration         │
│    /data/backlog-snapshots/*.json    — daily snapshots, 30d retention                    │
│    /data/archive.json                — features older than 7d, compacted                 │
│    /data/schedule.json               — cron-style scheduled Features                     │
│                                                                                          │
│  Worktrees (per feature):  {repo}/../worktrees-{repo}/feat/{feature_id}/                 │
└──────────────────────────────────────────────────────────────────────────────────────────┘

External:
  GitHub (origin)       ← auto-PR + release-bot + workflow_run webhook ingest
  Kimi API              ← primary model
  MiniMax API           ← fallback + kanban-agent
  wikidelve.walleye...  ← research-article retrieval
  Telegram              ← primary operator surface
  (optional) Slack/Discord outbound notifiers
  (optional) PagerDuty/Grafana/Datadog → /webhooks/alert
```

---

## 3. Data model

### `Feature` — the atomic unit
- `id`: kebab-case string, unique
- `name`: human title
- `description`: sanitized markdown-ish body
- `priority`: critical | high | medium | low
- `repos`: list of repo names it targets
- `status`: pending → implementing → (done | blocked)
- `kind`: feature | bug | refactor | schema | security | incident | perf-revert
- `risk_tier`: low (auto-merge eligible) | medium (PR as draft) | high (human approval required)
- `depends_on`: list of feature IDs that must be `done` before this is schedulable
- `heal_attempts`: healer reset counter (0..MAX; ≥3 = escalated lane)
- `heal_hint`: carried across retries; healer populates it with adversarial framing
- `planner_estimate_lines`: tracked for >1.5× undercount gate
- `repro_command`: required when `kind=bug`
- `acceptance_criteria`: feeds the developer's ACCEPTANCE statement + SELF-AUDIT
- `budget_usd`: per-feature override of the global `per_feature_budget_usd`

### `transitions.jsonl` — every status change
```json
{"ts":"...","feature_id":"...","from":"pending","to":"implementing","reason":"",
 "actor":"loop","prompts_version":"4545df9b8b"}
```
`actor` ∈ `{loop, healer, kanban, webhook}`. `prompts_version` is a sha of
`prompts.py + loop.py + healer.py`, stamped at process start.

### `attempts.jsonl` — every `agent.ainvoke`
```json
{"ts":"...","feature_id":"...","attempt":0,"provider":"primary",
 "input_tokens":10554,"output_tokens":25,"duration_sec":23.4,
 "tool_calls":[{"name":"wikidelve_search","args":"..."}, ...]}
```
Foundation for deterministic replay; consumed by `/replay/{id}`,
`/cost-analytics`, and operator drill-down.

---

## 4. Feature lifecycle

```
                       ┌──────┐
                       │ IDLE │
                       └──┬───┘
         idea_engine / Telegram / HTTP / webhook
                          ↓ (Backlog.add)
                    ┌───────────┐
                    │ pending   │
                    └─────┬─────┘
          worker picks it (with dep-gate + splitter + self-improv lock)
                          ↓ (Backlog.set_status "implementing")
                  ┌────────────────┐
                  │ implementing   │
                  └────────┬───────┘
        agent.ainvoke with _feature_prompt (heal_hint if any)
                          │
                  ┌───────┴──────┐
                  ↓              ↓
           verify_changes    exception / timeout
             ok? yes   no        │
              │        │         ↓
              ↓        ↓     _rescue_uncommitted_worktrees
           done     fixup loop up to MAX_FIXUPS     │
                      │       (cross-model retry,   ↓
                      │        stuck detector,      blocked
                      │        budget cap)
                      ↓
                    blocked
                      │
          healer wakes every 5m, flips back to pending
          with adversarial heal_hint (up to 3× then escalate)
                      │
                      ↓
                  pending (or escalated column when heal_attempts ≥ 3)
```

Safety nets run in parallel:
- `stuck_feature_escalator` (every 5m): feature stuck in `implementing`
  > 3× timeout → flip to blocked with `stranded` reason
- `drift_alarm` (every 30m): current `prompts_version` done-rate <
  trailing median × 0.80 → Telegram alert
- `self_improvement_seeder` (every 30m): block-reason prefix ≥5×
  cluster → auto-file a self-improvement Feature targeting it
- `worktree_gc` (every 30m): reclaim disk from done/blocked worktrees
  past retention
- `snapshot` (every 24h): /data/backlog-snapshots/YYYY-MM-DD.json for
  time-machine diffs

---

## 5. Prompt surface

Four prompt families, all hashed together into `prompts_version`:

1. `ORCHESTRATOR_INSTRUCTIONS` (prompts.py) — 7-phase workflow
   (context → plan → delegate → verify → self-audit → release) with
   hard caps on wikidelve usage + explicit tool-first rule.
2. Subagent prompts — planner, backend-dev, frontend-dev, developer,
   reviewer, security. Each inherits `_DEVELOPER_CORE` with phases
   4.0 (first-write discipline), 4.3 (TDD scaffold), 4.4 (verify_staged),
   4.5 (5-category adversarial audit).
3. `_feature_prompt` (loop.py) — per-feature prompt builder. Emits
   the retry flow, bug reproduce-first flow, refactor characterize-
   first flow, breaking-change minimal-fix flow based on kind +
   fixup reason.
4. Healer hints (healer.py) — per-failure-reason targeted nudges,
   always prefixed with the adversarial-audit framing.

### Adversarial self-audit (Phase 4.5)

Replaces the rubber-stamping "self-critique" that research #3812
measured at 85% pass-even-on-broken-diffs. Mandates a JSON block
with five categories:
```
SELF-AUDIT:
  MISSING / WRONG / DANGEROUS / UNTYPED / TESTGAP
```
Empty categories require explicit `why_covered`. Up to 3 re-audits;
after that ship with `feat(partial):` + BLOCKED report.

---

## 6. Tool surface

```
Filesystem / shell     read_file  edit_file  write_file  write_todos  run_command
Git                    git_status  git_commit  git_push  git_branch_create
                       git_worktree_add  git_worktree_remove
Pre-commit verify      verify_staged (build + lint + type-check +
                                      SYMBOL_UNRESOLVED classification)
Test scaffolding       scaffold_test_file  scaffold_pbt  scaffold_contract_test
                       scaffold_migration  scaffold_otel  scaffold_i18n
Quality gates          validate_acceptance_criteria  a11y_audit
                       classify_bump  env_profile  bisect_bench
Planning               record_planner_estimate
Search / research      repo_search  repo_reindex  web_search
                       wikidelve_{search,read,research,pending_jobs,recent_completions}
Chat operator (kanban) kanban_list  kanban_act  kanban_queue  kanban_show
                       kanban_stats  kanban_cost  kanban_health  kanban_dashboard
```

---

## 7. Operator surface

### Telegram bot (primary)
19 commands; freeform chat routed to a dedicated kanban-ops agent
(MiniMax-backed, 8 tools, no worktree access) so natural-language
commands like "nuke all the gh-* features" or "approve everything
with CVE in the name" translate to the right tool-call sequence.

### HTTP (30+ routes)
- **Backlog**: GET /features (+ query DSL), POST /features,
  POST /features/bulk, GET /features/{id}/history + /attempts,
  POST /features/{id}/action + /replay-retry + /debate,
  GET /replay/{id}, POST /replay/{id}/dry-run
- **Analytics**: /stats, /prompt-analytics, /repo-analytics,
  /cost-analytics, /cost-analytics/forecast, /worker-metrics,
  /dashboard/{repo}
- **Admin**: /config, /health (with subsystem heartbeats),
  /transitions (filtered), /backlog/export + import + diff,
  /schedule + /schedule/preview (PUT), /dep-graph,
  POST /admin/restart-task/{name}
- **Ingest**: /webhooks/github (review + workflow_run +
  pull_request + issues), /webhooks/alert (HMAC),
  /webhooks/support, /webhooks/figma
- **UI**: /kanban (vanilla HTML + Alpine.js)

### CLI (`scripts/hearth`)
15 subcommands wrapping the HTTP surface. Uses stdlib urllib;
`HEARTH_URL` env for targeting.

---

## 8. Observability

1. **Langfuse** — every `agent.ainvoke` tagged with
   `feature:<id>`, `worker:<n>`, `provider`, `attempt`. Self-hosted
   alongside the agent.
2. **`/stats`** — live counters, 24h throughput, top-10 block-reason
   clusters, worker heartbeat map, per-provider circuit state.
3. **`/prompt-analytics`** — per-`prompts_version` done-rate + top
   failure clusters. Auto-identifies best-trusted version.
4. **`/cost-analytics`** — total spend, top 25 features by $,
   per-provider split, daily series, p50/p95 attempt duration.
5. **`/health`** — 12-subsystem heartbeat registry; degraded when
   any stale. Fed by `beat()` calls inside every background task.
6. **Telegram digest** — daily rollup of done/blocked/healed/
   approved/nuked activity.
7. **Drift alarm** — automatic regression detection on
   `prompts_version` change, Telegram ping when active version
   regresses vs trailing median.

---

## 9. Self-improvement loop

1. Agent ships features. Transitions logged with `prompts_version`.
2. Operator changes `prompts.py` / `loop.py` / `healer.py`. New
   `prompts_version` computed at next process start. `auto_rerun_on_
   new_prompts` flips every blocked feature back to pending so the
   new prompts get a shot at old failures.
3. `/prompt-analytics` naturally A/B's the two versions from
   transition data. No traffic-splitting needed; version cut is
   process-restart-granular.
4. `drift_alarm` pings if new prompts regress the done-rate below
   0.80 × trailing median.
5. `self_improvement_seeder` watches live block-reason clusters;
   when ≥5 features share a prefix, files a `self_improvement=True`
   Feature against `hearth-agents` itself. Worker 0 is pinned to
   pick these up (affinity rule) to avoid parallel prompts.py edits.
6. Operator reviews the self-improvement PR, merges, cycle repeats.

This is the loop that let this system improve from a 19% done-rate
(prompts_version `9d71d0ad79`) to 100% (`b699ad4330`) over one
session — measured in the A/B data at `/prompt-analytics`, not
hand-waved.

---

## 10. Safety + audit

- **Prompt injection** — `sanitize.py` strips turn-forgery
  patterns (`"role":"system"`, `\\n\\nHuman:`, `<|im_start|>`) +
  soft-override phrases ("ignore prior instructions", "you are now")
  from every externally-sourced string before it hits the prompt.
  Wrapped in `<untrusted source="...">` delimiters; agent prompts
  instruct it to treat those as data, not instructions.
- **HMAC** — `/webhooks/github` + `/webhooks/alert` verify
  sha256 signatures when the secret is configured.
- **Per-feature budget cap** — `per_feature_budget_usd` (global)
  or `Feature.budget_usd` (override); loop aborts with
  `budget_exhausted` when running cost crosses.
- **Risk tiers** — kind=incident + kind=perf-revert gate auto-PR
  merge. High → human approval; medium → draft; low → auto.
- **Per-provider circuit breakers** — quality collapse on one
  model quarantines it for 30m; fallback keeps serving.
- **Rate-limit predictor** — RPM + ITPM (input token/min) + OTPM
  (output token/min) with 10% headroom; 1–10s preemptive throttle
  to avoid 429-induced 15m cooldowns.
- **Stuck-state fingerprint detector** — post-ainvoke scan flags
  identical-3x + A-B-A-B + read-only spirals; feeds adversarial
  healer hint "stop reading, start writing".
- **Transition log = audit trail** — append-only, per-actor,
  stamped with prompts_version. Daily snapshots preserve the exact
  Backlog shape for 30 days.

---

## 11. Known limits + tradeoffs

- **Model coupling**: switching off Kimi + MiniMax means rewriting
  `models.py`, the circuit-breaker state, and the rate-limit
  predictor's token-ceiling defaults.
- **Backlog in-memory**: single `Backlog` instance; scales to ~10k
  features before `/features` response size + kanban rendering get
  painful. Archive task offloads to `archive.json` after 7d.
- **Replay is read-only**: `attempts.jsonl` captures tool sequences
  + tokens but not the complete LangGraph state. True replay
  requires Langfuse persistence or an independent recorder; the
  `/replay/{id}/dry-run` endpoint runs the CURRENT prompts against
  a fresh invoke and returns new tool sequence for comparison,
  which is adjacent but not identical.
- **Worktree isolation** relies on `git worktree`; if the target
  repo switches to a format git-worktree can't handle (bare?),
  `rescue_uncommitted_worktrees` is the lifeline.
- **Webhook bus is the single ingest point**: no retry queue,
  no at-least-once guarantee. A dropped webhook = a dropped event.
  `scheduler` + `self_improvement_seeder` backfill gaps, but a
  missed CI-failure webhook is silently lost.

---

## 12. What's deliberately NOT in this system

- Per-user auth. CORS is permissive over tailnet; Tailscale is the
  auth boundary.
- A web-based kanban-as-primary-UI. The current kanban exists but
  Telegram is first-class. Kanban is slated for read-only demotion.
- A sprint / team / assignee layer. Features have a worker at most
  transiently; there's no concept of "Alice owns this".
- Long-term memory across process restarts beyond the JSONL
  transition + attempts logs and the daily backlog snapshots.

---

## 13. Evolution

This system started the morning this doc was written as three
files that couldn't ship a feature end-to-end. Over ~30 commits it
accumulated: structured prompt phases, 10+ tool scaffolders, 14
background tasks, 30 HTTP endpoints, 15 Telegram commands, 44
wikidelve research jobs' worth of patterns. Every addition was
justified by either a live-observed failure mode or an explicit
research-article recommendation. Nothing is speculative.

The design is NOT frozen. Future directions, ranked by expected
leverage:

1. Replace the in-process Backlog with event-sourcing on top of
   `transitions.jsonl` — state becomes a projection, not canonical.
2. Expose hearth-agents itself as an MCP server so Claude Desktop
   can drive it directly.
3. Langfuse ts-proxy sidecar (Tailscale hostname) for
   browser-based trace viewing without SSH port-forward.
4. Multi-agent debate scoring + auto-selection (currently
   operator picks the winner).
5. Deprecate the kanban UI in favor of a chat + CLI-only surface.
