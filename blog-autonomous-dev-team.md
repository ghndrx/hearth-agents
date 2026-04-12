# Building an Autonomous Dev Team for $51/month: How Hearth Uses MiniMax M2.7 + Kimi K2.5

*Published April 12, 2026*

What if you could have an autonomous dev team building your product 24/7 for less than a Netflix subscription?

Not a copilot. Not "AI-assisted development." An actual autonomous loop that researches features, writes product requirements, generates implementations, runs quality gates, and opens pull requests -- all while you sleep.

That is what [hearth-agents](https://github.com/ghndrx/hearth-agents) does. It is a TypeScript system that builds [Hearth](https://github.com/ghndrx/hearth), an open-source Discord alternative, using two cheap LLMs working in tandem. Total cost: $51/month.

Here is how it works, what went right, and what is still rough.

---

## The Problem

I am a solo developer building a Discord alternative. Hearth is a full-stack project: Go backend, SvelteKit frontend, LiveKit for voice/video, Signal protocol for E2EE, Tauri desktop app, React Native mobile app. Three repositories, two languages, dozens of features needed to hit Discord parity.

The feature gap between "working chat app" and "something people would actually switch to" is enormous. Voice channels, screen sharing, role permissions, message search, federation, push notifications -- each one is a multi-week project.

I needed to ship faster than one person can type.

## The Architecture

```
                          +------------------+
                          |   Telegram Bot   |
                          |  (Control Plane) |
                          +--------+---------+
                                   |
                                   | commands / notifications
                                   |
                          +--------v---------+
                          | Autonomous Loop  |
                          |                  |
                          | 1. Pick feature  |
                          | 2. Research      |
                          | 3. Write PRD     |
                          | 4. Implement     |
                          | 5. Quality gates |
                          | 6. Create PR     |
                          +--------+---------+
                                   |
                    +--------------+--------------+
                    |                             |
           +--------v--------+          +--------v--------+
           |  MiniMax M2.7   |          |   Kimi K2.5     |
           |  ($20/month)    |          |  ($31/month)    |
           |                 |          |                 |
           | - Research      |          | - Implementation|
           | - PRD writing   |          | - Code review   |
           | - Architecture  |          | - Testing       |
           | - Planning      |          | - Security      |
           +-----------------+          +-----------------+
                    |                             |
                    +-------------+---------------+
                                  |
                    +-------------v--------------+
                    |      Knowledge Base        |
                    | (self-building docs)       |
                    |                            |
                    | research/ prds/ guides/    |
                    | implementations/           |
                    | next-steps/                |
                    +----------------------------+
                                  |
                    +-------------v--------------+
                    |     Target Repositories    |
                    |                            |
                    | hearth (Go + SvelteKit)    |
                    | hearth-desktop (Tauri)     |
                    | hearth-mobile (React Native)|
                    +----------------------------+
```

The system is roughly 2,000 lines of TypeScript. It has no framework -- just the OpenAI SDK, grammY for Telegram, better-sqlite3 for persistence, and a lot of `execFile` calls to git and build tools.

## The Dual-Model Strategy

The core insight is that research and implementation are fundamentally different tasks, and different models are better at each.

**MiniMax M2.7** ($20/month flat) is strong at decomposition, synthesis, and following complex instructions. It handles research, PRD generation, architecture planning, and the idea engine. It is not a top-tier coder, but it does not need to be.

**Kimi K2.5** ($31/month Allegretto tier) scores 76.8% on SWE-Bench Verified. It handles implementation, code review, testing, and security analysis. It writes actual code that compiles.

The model router is simple -- a static map from agent role to provider:

```typescript
const ROLE_MODEL_MAP: Record<string, ModelProvider> = {
  // MiniMax M2.7: research, planning, PRDs, architecture
  'prd-writer':     'minimax',
  'architect':      'minimax',
  'backend':        'minimax',
  'frontend':       'minimax',
  'documentation':  'minimax',

  // Kimi K2.5: implementation, code review, testing
  'developer':      'kimi',
  'reviewer':       'kimi',
  'testing':        'kimi',
  'security':       'kimi',
};

export function getModelForRole(role: AgentRole | string): ModelConfig {
  const provider = ROLE_MODEL_MAP[role] || 'minimax';

  if (provider === 'kimi') {
    return {
      provider: 'kimi',
      model: 'kimi-k2.5',
      client: getKimiClient(),
    };
  }

  return {
    provider: 'minimax',
    model: 'MiniMax-M2.7',
    client: getMiniMaxClient(),
  };
}
```

Both providers expose OpenAI-compatible APIs, so the entire system uses a single `OpenAI` client class with different base URLs. No vendor lock-in, no custom SDKs.

Why not just use one model for everything? Cost and capability. MiniMax is cheaper for the high-token tasks (research reports are verbose). Kimi is better at the thing that matters most: writing code that actually works. Routing by role lets each model play to its strengths.

## The Autonomous Loop

The main loop is a `while(true)` that pulls features from a priority-ordered backlog and processes them through a pipeline:

```typescript
// Simplified from src/autonomous/loop.ts
while (this.running) {
  const feature = getNextFeature();
  if (!feature) {
    await this.notifier.send('All features in backlog have been processed!');
    break;
  }

  try {
    // Phase 1: Deep research with MiniMax
    const research = await researchFeature(this.client, feature);
    await saveResearch(feature, research);

    // Phase 2: Generate PRD from research
    const prd = await generatePRD(this.client, feature, research);
    await savePRDSummary(feature, prd);

    // Phase 3: Implementation (per target repo)
    for (const repoName of feature.repos) {
      const result = await implementFeature(feature, prd, repoPath, repoName);

      if (result.success) {
        await this.pushBranchAndCreatePR(repoPath, repoName, branch, feature);
      }
    }

    // Phase 4: Update knowledge base, refill backlog
    await generateNextSteps(completed, remaining);
    await refillBacklog(this.client);
  } catch (err) {
    await this.notifier.sendError(`Feature: ${feature.name}`, err.message);
    updateFeatureStatus(feature.id, 'pending'); // retry later
    await sleep(60_000);
  }
}
```

Each feature in the backlog is a structured object with research topics, target repositories, priority, and a Discord parity description:

```typescript
{
  id: 'voice-channels-always-on',
  name: 'Always-On Voice Channels',
  description: 'Discord-style persistent voice channels where users can drop in/out...',
  priority: 'high',
  repos: ['hearth', 'hearth-desktop'],
  researchTopics: [
    'LiveKit room management persistent voice channels',
    'WebRTC voice activity detection VAD',
    'Discord voice channel UX patterns',
  ],
  discordParity: 'Core Discord feature - voice channels with user presence',
  status: 'pending',
}
```

The research phase queries MiniMax for each topic separately, then synthesizes the results into a comprehensive report with competitor analysis and technical recommendations. This is the "wikidelve" pattern -- multiple targeted queries composed into a single coherent document.

The implementation phase creates a git worktree for isolation, hands the PRD to a Kimi-powered agent with tool-calling capabilities, and lets it read the codebase and write code. The agent gets up to 100 turns to explore the repo, understand patterns, write code, and commit.

## Quality Gates

Before any PR gets created, the code passes through quality gates. The system auto-detects the repo type (TypeScript or Go) and runs the appropriate checks in fail-fast order:

```typescript
function getChecks(repoType: RepoType): CheckDefinition[] {
  if (repoType === 'typescript') {
    return [
      { name: 'typecheck', command: 'npx', args: ['tsc', '--noEmit'] },
      { name: 'lint',      command: 'npx', args: ['eslint', '.', '--max-warnings', '0'] },
      { name: 'test',      command: 'npx', args: ['vitest', 'run', '--reporter=json'] },
      { name: 'build',     command: 'npm', args: ['run', 'build'] },
    ];
  }
  // Go repos
  return [
    { name: 'typecheck', command: 'go',             args: ['vet', './...'] },
    { name: 'lint',      command: 'golangci-lint',   args: ['run'] },
    { name: 'test',      command: 'go',             args: ['test', './...'] },
    { name: 'build',     command: 'go',             args: ['build', './...'] },
  ];
}
```

Fail-fast means the first check that fails aborts the pipeline. No point running tests if the code does not type-check. Results get formatted and sent to Telegram so I can see exactly what broke:

```
Quality gate FAILED (typescript) - 2/4 checks passed in 8340ms
  [PASS] typecheck (2100ms)
  [PASS] lint (3200ms)
  [FAIL] test (3040ms)
```

This is one of the most important parts of the system. Without quality gates, the agents produce code that looks plausible but does not compile. The gates create a hard boundary between "agent thinks it is done" and "code actually works."

## The Self-Building Knowledge Base

Every phase of the pipeline writes to a local knowledge base under `knowledge/`:

```
knowledge/
  research/          # Deep research reports per feature
  prds/              # PRD summaries and key decisions
  implementations/   # What files changed, what worked
  guides/            # Generated technical guides
  next-steps/        # Auto-generated roadmap and ideas
  index.json         # Searchable index of all entries
```

The knowledge base serves two purposes. First, it prevents the agents from researching the same topics twice. The idea engine reads the index before generating new research directions. Second, it creates a persistent memory across runs. When the system restarts, it knows what has been built and what comes next.

The index is a simple JSON file with entries like:

```json
{
  "id": "research-matrix-federation",
  "type": "research",
  "title": "Research: Matrix Federation for E2EE",
  "tags": ["Matrix protocol", "Megolm encryption", "federation"],
  "summary": "9 topics researched for Matrix Federation for E2EE",
  "path": "research/matrix-federation.md"
}
```

Searchable via Telegram with `/search matrix` -- useful when I need to quickly check what the agents have already learned about a topic.

## The Idea Engine

This is where it gets interesting. Every three completed features, the system runs an "idea engine" that analyzes the knowledge base and generates new research directions:

```typescript
const IDEA_ENGINE_PROMPT = `You are a strategic product intelligence agent for Hearth...

Your job is to evaluate what research would be most valuable RIGHT NOW based on:
1. What has already been built and researched (knowledge base)
2. What features are in progress or planned
3. Current industry trends and competitor moves
4. Technical opportunities and risks
5. User demand signals

You think like a combination of:
- A product manager (what do users need?)
- A CTO (what technical investments pay off?)
- A competitive analyst (what are Discord/Element/Revolt doing?)
- A growth hacker (what gets attention and adoption?)`;
```

The engine outputs structured research ideas with categories, priorities, and specific search queries. These feed back into the backlog generator, which creates new features from the highest-priority ideas.

The result is a system that does not just execute a static list -- it discovers new work to do based on what it has already learned. The backlog auto-refills when it drops below three pending features.

Is this AGI? No. It is a prompt with good context. But the emergent behavior is useful: the system surfaces research topics I would not have thought to investigate, because it can cross-reference everything it has learned about the problem space.

## The Telegram Control Plane

Every significant event gets pushed to Telegram. The bot serves dual duty: it is both a notification channel for the autonomous loop and an interactive control plane.

Notifications come through at each phase transition -- research started, PRD created, implementation in progress, PR opened, quality gate results. Progress updates arrive every 45 minutes with a visual progress bar and API rate limit status.

The interactive commands let me manage the system from my phone:

- `/backlog` -- view the feature queue with status
- `/add <feature>` -- inject a new feature into the backlog
- `/status` -- see active tasks and recent history
- `/kb` -- knowledge base summary
- `/search <query>` -- search the knowledge base
- `/implement <prd>` -- manually trigger implementation (requires confirmation)

The confirmation step uses grammY inline keyboards:

```typescript
async function handleImplement(ctx: Context): Promise<void> {
  const prdFilename = sanitize(String(ctx.match), 200);

  const keyboard = new InlineKeyboard()
    .text('Confirm', `confirm_impl:${prdFilename}`)
    .text('Cancel', `cancel_impl:${prdFilename}`);

  await ctx.reply(
    `<b>Implement from PRD</b>\n\n` +
    `File: <code>${escapeHtml(prdFilename)}</code>\n\n` +
    `This will spawn an implementation agent. Confirm?`,
    { parse_mode: 'HTML', reply_markup: keyboard },
  );
}
```

This is important for safety. The autonomous loop handles the happy path, but I want a manual gate for ad-hoc implementation requests. One tap to confirm, one tap to cancel.

The auth layer is a simple allowlist of Telegram user IDs. Messages from anyone not on the list get silently dropped -- no response, no acknowledgment that the bot exists.

## What the $51 Gets You

The monthly cost breaks down to:

| Service | Cost | What it does |
|---------|------|-------------|
| MiniMax M2.7 | $20/mo | Research, PRDs, architecture, idea engine |
| Kimi K2.5 (Allegretto) | $31/mo | Implementation, code review, testing |
| **Total** | **$51/mo** | |

For context, a single Claude Opus API call for a complex coding task can cost $0.50-2.00. A human contractor charges $50-150/hour. The tradeoff is obvious: agent output requires review, but the volume-to-cost ratio is hard to beat.

## What Works

**The pipeline structure is solid.** Research-then-implement produces better code than just throwing a feature description at a coding agent. The PRD step forces the agent to think about edge cases, API contracts, and migration concerns before writing a single line.

**Quality gates catch real problems.** Agents are optimistic -- they assume their code works. The typecheck/lint/test/build pipeline catches the gap between "looks right" and "compiles and passes."

**The knowledge base compounds.** After a few features, the agents have meaningful context about the codebase patterns, architectural decisions, and competitive landscape. Research reports for later features reference findings from earlier ones.

**Telegram as a control plane works surprisingly well.** Being able to check on the agents and add features from my phone while walking the dog is genuinely useful.

## What Is Still Rough

**Implementation quality varies.** Kimi K2.5 is good but not infallible. Complex features that require understanding multiple interacting systems still produce code that needs significant human editing. The agents are best at well-scoped, clearly-defined tasks.

**The backlog generator is opinionated.** Auto-generated features sometimes drift from what I actually want to build next. The `/add` command exists specifically to override this.

**Error recovery is basic.** If an implementation fails, the feature gets reset to pending and retried after a 60-second cooldown. There is no attempt to diagnose why it failed or adjust the approach.

**No sandboxing.** The agents run `git`, `npm`, `go`, and other tools directly on the host. A Docker-based sandbox is on the roadmap but not implemented yet.

**Rate limiting is coarse.** The system tracks API usage but does not do sophisticated cost optimization. Some research queries are unnecessarily verbose.

## The Stack

If you want to understand the full picture:

- **Hearth** (the product): Go 1.25 + SvelteKit/Svelte 5 + PostgreSQL + Redis + LiveKit + Signal E2EE
- **hearth-agents** (this system): TypeScript + OpenAI SDK + grammY + better-sqlite3
- **Models**: MiniMax M2.7 (OpenAI-compatible API) + Kimi K2.5 (OpenAI-compatible API)
- **CI/CD integration**: `gh` CLI for PR creation, git worktrees for branch isolation

## Try It Yourself

Both repositories are public:

- **Hearth**: [github.com/ghndrx/hearth](https://github.com/ghndrx/hearth)
- **hearth-agents**: [github.com/ghndrx/hearth-agents](https://github.com/ghndrx/hearth-agents)

To run the agent system:

```bash
git clone https://github.com/ghndrx/hearth-agents
cd hearth-agents
npm install

# Configure .env
cp .env.example .env
# Add your API keys: MINIMAX_API_KEY, KIMI_API_KEY, TELEGRAM_BOT_TOKEN

# Start the autonomous loop
npm run dev

# Or just the Telegram bot (interactive mode)
npm run bot
```

You will need API keys for MiniMax and Kimi (both offer OpenAI-compatible endpoints), a Telegram bot token from BotFather, and the target repository cloned next to the agents repo.

## What Is Next

The system currently handles the "build new features" loop well. The next areas of focus:

1. **Docker sandboxing** for agent execution -- the biggest safety gap right now.
2. **TDD-first implementation** -- write tests from the PRD before writing code, then implement against the tests. The `tdd-implementer.ts` is stubbed but not wired up.
3. **Feedback loops from PR reviews** -- when I leave comments on an agent-generated PR, feed those back into the knowledge base to improve future implementations.
4. **Context caching** -- Kimi supports prefix caching that could reduce implementation costs further.
5. **Multi-agent collaboration** -- having the research agent and implementation agent share a conversation, rather than communicating through documents.

The broader point is that the cost floor for autonomous development has dropped to a level where solo developers can realistically run agent teams on their projects. The models are not perfect, the output needs review, and the system requires babysitting. But the ratio of output to cost is already useful, and it improves every time the models get better.

The agents are building Hearth right now. If you check the [repo](https://github.com/ghndrx/hearth), some of those PRs were written by a $51/month AI team.

---

*Greg Henderson builds [Hearth](https://github.com/ghndrx/hearth), an open-source Discord alternative with E2EE. The agent system is at [github.com/ghndrx/hearth-agents](https://github.com/ghndrx/hearth-agents). Feedback welcome as issues or on the Hearth Discord.*
