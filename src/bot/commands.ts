/**
 * Telegram bot command handlers.
 *
 * Commands create task descriptors and emit events for the pipeline to pick up.
 * They never spawn agents directly -- that separation keeps the bot layer thin.
 */

import { type Bot, type Context, InlineKeyboard } from 'grammy';
import { randomUUID } from 'node:crypto';
import { formatTaskStatus, escapeHtml } from './formatters.js';
import type { AgentTask, AgentRole, TaskStatus } from '../types/index.js';
import { FEATURE_BACKLOG, addFeature, getBacklogStats, type Feature } from '../autonomous/feature-backlog.js';
import { getKnowledgeSummary, searchKnowledge } from '../autonomous/knowledge-base.js';
import { tokenBudget } from '../autonomous/token-budget.js';
import { providerFailover } from '../autonomous/circuit-breaker.js';
import { rateLimiter } from '../autonomous/rate-limiter.js';

// ---------------------------------------------------------------------------
// In-memory task store (will be replaced by SQLite persistence later)
// ---------------------------------------------------------------------------

const tasks = new Map<string, AgentTask>();

export function getTask(id: string): AgentTask | undefined {
  return tasks.get(id);
}

export function getAllTasks(): AgentTask[] {
  return [...tasks.values()].sort(
    (a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0),
  );
}

export function getRecentTasks(limit = 10): AgentTask[] {
  return getAllTasks().slice(0, limit);
}

// ---------------------------------------------------------------------------
// Event emitter for pipeline integration
// ---------------------------------------------------------------------------

export type TaskEventHandler = (task: AgentTask) => void;

const eventHandlers: TaskEventHandler[] = [];

export function onTaskCreated(handler: TaskEventHandler): void {
  eventHandlers.push(handler);
}

function emitTaskCreated(task: AgentTask): void {
  for (const handler of eventHandlers) {
    try {
      handler(task);
    } catch {
      // Pipeline handler errors should not crash the bot
    }
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Sanitize user-provided text: trim, limit length, strip control chars. */
function sanitize(input: string, maxLength = 500): string {
  // Strip zero-width and control characters (except newline/tab)
  const cleaned = input.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F\u200B-\u200F\u2028-\u202F\uFEFF]/g, '');
  return cleaned.trim().slice(0, maxLength);
}

function createTask(
  role: AgentRole,
  description: string,
  chatId: number,
  requestedBy: number,
): AgentTask {
  const now = Date.now();
  const task: AgentTask = {
    id: randomUUID(),
    role,
    status: 'queued' as TaskStatus,
    description,
    chatId,
    messageId: null,
    requestedBy,
    createdAt: now,
    startedAt: null,
    completedAt: null,
    lastOutputLine: null,
    output: [],
    branchName: null,
    prdPath: null,
    worktreePath: null,
    pid: null,
  };
  tasks.set(task.id, task);
  return task;
}

// ---------------------------------------------------------------------------
// Command registration
// ---------------------------------------------------------------------------

export function registerCommands(bot: Bot): void {
  bot.command('start', handleStart);
  bot.command('help', handleHelp);
  bot.command('prd', handlePrd);
  bot.command('implement', handleImplement);
  bot.command('review', handleReview);
  bot.command('status', handleStatus);
  bot.command('cancel', handleCancel);
  bot.command('plan', handlePlan);
  bot.command('backlog', handleBacklog);
  bot.command('add', handleAddFeature);
  bot.command('kb', handleKnowledge);
  bot.command('search', handleSearch);
  bot.command('budget', handleBudget);
  bot.command('health', handleHealth);
  bot.command('wiki', handleWiki);
  bot.command('research', handleResearch);

  // Handle inline keyboard callbacks for implementation confirmation
  bot.callbackQuery(/^confirm_impl:/, handleConfirmImplement);
  bot.callbackQuery(/^cancel_impl:/, handleCancelImplement);
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

const HELP_TEXT = `
<b>Hearth Agent Commands</b>

<b>Task Commands</b>
/prd &lt;feature description&gt; \u2014 Generate a PRD
/implement &lt;prd-filename&gt; \u2014 Implement from a PRD
/review &lt;branch-name&gt; \u2014 Review a branch
/plan &lt;feature&gt; \u2014 Decompose a feature into tasks

<b>Management</b>
/status \u2014 Show active and recent tasks
/cancel &lt;task-id&gt; \u2014 Cancel a running task
/budget \u2014 Show token budget stats
/health \u2014 System health and provider status

<b>Knowledge</b>
/wiki &lt;query&gt; \u2014 Search wikidelve knowledge base
/research &lt;topic&gt; \u2014 Queue a wikidelve research job

/help \u2014 Show this message
`.trim();

async function handleStart(ctx: Context): Promise<void> {
  await ctx.reply(HELP_TEXT, { parse_mode: 'HTML' });
}

async function handleHelp(ctx: Context): Promise<void> {
  await ctx.reply(HELP_TEXT, { parse_mode: 'HTML' });
}

async function handlePrd(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /prd &lt;feature description&gt;', {
      parse_mode: 'HTML',
    });
    return;
  }

  const description = sanitize(String(raw));
  if (description.length === 0) {
    await ctx.reply('Invalid input. Provide a feature description.');
    return;
  }

  const task = createTask(
    'prd-writer' as AgentRole,
    description,
    ctx.chat!.id,
    ctx.from!.id,
  );

  emitTaskCreated(task);

  await ctx.reply(
    `\u2705 PRD task queued\n\nID: <code>${escapeHtml(task.id.slice(0, 8))}</code>\nFeature: <i>${escapeHtml(description)}</i>`,
    { parse_mode: 'HTML' },
  );
}

async function handleImplement(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /implement &lt;prd-filename&gt;', {
      parse_mode: 'HTML',
    });
    return;
  }

  const prdFilename = sanitize(String(raw), 200);
  if (prdFilename.length === 0) {
    await ctx.reply('Invalid input. Provide a PRD filename.');
    return;
  }

  // Require explicit confirmation via inline keyboard
  const keyboard = new InlineKeyboard()
    .text('\u2705 Confirm', `confirm_impl:${prdFilename}`)
    .text('\u274C Cancel', `cancel_impl:${prdFilename}`);

  await ctx.reply(
    `<b>Implement from PRD</b>\n\nFile: <code>${escapeHtml(prdFilename)}</code>\n\nThis will spawn an implementation agent. Confirm?`,
    { parse_mode: 'HTML', reply_markup: keyboard },
  );
}

async function handleConfirmImplement(ctx: Context): Promise<void> {
  const data = ctx.callbackQuery?.data;
  if (!data) return;

  const prdFilename = sanitize(data.replace('confirm_impl:', ''), 200);

  const task = createTask(
    'developer' as AgentRole,
    prdFilename,
    ctx.chat!.id,
    ctx.from!.id,
  );

  emitTaskCreated(task);

  await ctx.answerCallbackQuery({ text: 'Implementation queued' });
  await ctx.editMessageText(
    `\u2705 Implementation queued\n\nID: <code>${escapeHtml(task.id.slice(0, 8))}</code>\nPRD: <code>${escapeHtml(prdFilename)}</code>`,
    { parse_mode: 'HTML' },
  );
}

async function handleCancelImplement(ctx: Context): Promise<void> {
  await ctx.answerCallbackQuery({ text: 'Cancelled' });
  await ctx.editMessageText('Implementation cancelled.');
}

async function handleReview(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /review &lt;branch-name&gt;', {
      parse_mode: 'HTML',
    });
    return;
  }

  const branch = sanitize(String(raw), 200);

  // Validate branch name: alphanumeric, hyphens, slashes, underscores, dots
  if (!/^[\w.\-/]+$/.test(branch)) {
    await ctx.reply('Invalid branch name. Use alphanumeric characters, hyphens, slashes, underscores, and dots.');
    return;
  }

  const task = createTask(
    'reviewer' as AgentRole,
    branch,
    ctx.chat!.id,
    ctx.from!.id,
  );

  emitTaskCreated(task);

  await ctx.reply(
    `\u2705 Review queued\n\nID: <code>${escapeHtml(task.id.slice(0, 8))}</code>\nBranch: <code>${escapeHtml(branch)}</code>`,
    { parse_mode: 'HTML' },
  );
}

async function handleStatus(ctx: Context): Promise<void> {
  const stats = getBacklogStats();
  const inProgress = FEATURE_BACKLOG.filter(f =>
    f.status === 'researching' || f.status === 'prd' || f.status === 'implementing'
  );
  const done = FEATURE_BACKLOG.filter(f => f.status === 'done');
  const pending = FEATURE_BACKLOG.filter(f => f.status === 'pending');

  const rlStats = rateLimiter.getStats();
  const rlPct = Math.round((rlStats.used / rlStats.limit) * 100);
  const providerStatus = providerFailover.getStatus();
  const memMB = Math.round(process.memoryUsage().heapUsed / 1024 / 1024);
  const uptimeH = Math.round(process.uptime() / 3600 * 10) / 10;

  let kbSummary = 'Not initialized';
  try { kbSummary = await getKnowledgeSummary(); } catch {}

  let msg = `<b>Hearth Agents - Project Status</b>\n\n`;

  // Progress
  const progress = stats.total > 0 ? Math.round((stats.done / stats.total) * 100) : 0;
  const bar = '\u2588'.repeat(Math.floor(progress / 10)) + '\u2591'.repeat(10 - Math.floor(progress / 10));
  msg += `<b>Progress:</b> ${bar} ${progress}%\n`;
  msg += `Features: ${stats.done} done / ${stats.pending} pending / ${stats.total} total\n\n`;

  // Current work
  if (inProgress.length > 0) {
    msg += `<b>In Progress:</b>\n`;
    for (const f of inProgress) {
      msg += `  \u25B6\uFE0F ${escapeHtml(f.name)} <i>(${f.status})</i>\n`;
    }
    msg += '\n';
  }

  // Up next
  if (pending.length > 0) {
    msg += `<b>Up Next:</b>\n`;
    for (const f of pending.slice(0, 3)) {
      msg += `  \u23F3 ${escapeHtml(f.name)} [${f.priority}]\n`;
    }
    if (pending.length > 3) msg += `  ... +${pending.length - 3} more\n`;
    msg += '\n';
  }

  // Recently completed
  if (done.length > 0) {
    msg += `<b>Recently Completed:</b>\n`;
    for (const f of done.slice(-5)) {
      msg += `  \u2705 ${escapeHtml(f.name)}\n`;
    }
    msg += '\n';
  }

  // System health
  msg += `<b>System:</b>\n`;
  const providerIcons: Record<string, string> = {};
  for (const [name, s] of Object.entries(providerStatus)) {
    const icon = (s as any).state === 'closed' ? '\u2705' : '\u26A0\uFE0F';
    providerIcons[name] = `${icon} ${name}`;
  }
  msg += `  Providers: ${Object.values(providerIcons).join(' | ')}\n`;
  msg += `  MiniMax: ${rlStats.used}/${rlStats.limit} requests (${rlPct}%)\n`;
  msg += `  Memory: ${memMB}MB | Uptime: ${uptimeH}h\n\n`;

  // Knowledge base
  msg += `<b>Knowledge Base:</b>\n`;
  msg += `  <pre>${escapeHtml(kbSummary)}</pre>\n\n`;

  // Capabilities
  msg += `<b>Capabilities:</b>\n`;
  msg += `  \u2022 14 specialized SWE agents (MiniMax + Kimi K2.5)\n`;
  msg += `  \u2022 Autonomous research, PRD, implementation, PR pipeline\n`;
  msg += `  \u2022 Wikidelve deep research integration\n`;
  msg += `  \u2022 Self-healing (git, API, process)\n`;
  msg += `  \u2022 Circuit breaker (MiniMax \u2194 Kimi \u2194 OpenRouter)\n`;
  msg += `  \u2022 Quality gates, TDD, repo guard\n`;
  msg += `  \u2022 GitHub webhook auto-fix\n\n`;

  // Repos
  msg += `<b>Repos:</b>\n`;
  msg += `  <a href="https://github.com/ghndrx/hearth">hearth</a> | `;
  msg += `<a href="https://github.com/ghndrx/hearth-desktop">desktop</a> | `;
  msg += `<a href="https://github.com/ghndrx/hearth-mobile">mobile</a> | `;
  msg += `<a href="https://github.com/ghndrx/hearth-agents">agents</a>`;

  await ctx.reply(msg, { parse_mode: 'HTML', link_preview_options: { is_disabled: true } });
}

async function handleCancel(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /cancel &lt;task-id&gt;', {
      parse_mode: 'HTML',
    });
    return;
  }

  const taskId = sanitize(String(raw), 100);
  // Support both full UUID and short prefix
  const match = [...tasks.values()].find(
    (t) => t.id === taskId || t.id.startsWith(taskId),
  );

  if (!match) {
    await ctx.reply(`No task found matching <code>${escapeHtml(taskId)}</code>`, {
      parse_mode: 'HTML',
    });
    return;
  }

  if (match.status === 'completed' || match.status === 'failed' || match.status === 'cancelled') {
    await ctx.reply(
      `Task <code>${escapeHtml(match.id.slice(0, 8))}</code> already ${escapeHtml(match.status as string)}.`,
      { parse_mode: 'HTML' },
    );
    return;
  }

  match.status = 'cancelled' as TaskStatus;
  match.completedAt = Date.now();

  await ctx.reply(
    `\u23F9\uFE0F Task <code>${escapeHtml(match.id.slice(0, 8))}</code> cancelled.`,
    { parse_mode: 'HTML' },
  );
}

async function handlePlan(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /plan &lt;feature description&gt;', {
      parse_mode: 'HTML',
    });
    return;
  }

  const feature = sanitize(String(raw));
  if (feature.length === 0) {
    await ctx.reply('Invalid input. Provide a feature description.');
    return;
  }

  const task = createTask(
    'architect' as AgentRole,
    feature,
    ctx.chat!.id,
    ctx.from!.id,
  );

  emitTaskCreated(task);

  await ctx.reply(
    `\u2705 Planning task queued\n\nID: <code>${escapeHtml(task.id.slice(0, 8))}</code>\nFeature: <i>${escapeHtml(feature)}</i>`,
    { parse_mode: 'HTML' },
  );
}

async function handleBacklog(ctx: Context): Promise<void> {
  const stats = getBacklogStats();
  const pending = FEATURE_BACKLOG.filter(f => f.status === 'pending');
  const inProgress = FEATURE_BACKLOG.filter(f =>
    f.status === 'researching' || f.status === 'prd' || f.status === 'implementing'
  );
  const done = FEATURE_BACKLOG.filter(f => f.status === 'done');

  let msg = `<b>Feature Backlog</b>\n\n`;
  msg += `Total: ${stats.total} | Done: ${stats.done} | Pending: ${stats.pending}\n\n`;

  if (inProgress.length > 0) {
    msg += `<b>In Progress:</b>\n`;
    for (const f of inProgress) {
      msg += `  \u25B6\uFE0F <b>${escapeHtml(f.name)}</b> (${f.status})\n`;
    }
    msg += '\n';
  }

  msg += `<b>Up Next:</b>\n`;
  for (const f of pending.slice(0, 5)) {
    msg += `  \u23F3 ${escapeHtml(f.name)} [${f.priority}]\n`;
  }

  if (pending.length > 5) {
    msg += `  ... and ${pending.length - 5} more\n`;
  }

  if (done.length > 0) {
    msg += `\n<b>Completed:</b>\n`;
    for (const f of done.slice(0, 5)) {
      msg += `  \u2705 ${escapeHtml(f.name)}\n`;
    }
  }

  msg += `\n<i>Backlog auto-refills when < 3 features remain.</i>`;

  await ctx.reply(msg, { parse_mode: 'HTML' });
}

async function handleAddFeature(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /add &lt;feature description&gt;\n\nExample: /add noise suppression for voice channels using RNNoise', {
      parse_mode: 'HTML',
    });
    return;
  }

  const description = sanitize(String(raw));
  const id = description.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40);

  const feature: Feature = {
    id,
    name: description.slice(0, 60),
    description,
    priority: 'medium',
    repos: ['hearth'],
    researchTopics: [description],
    discordParity: 'User requested feature',
    status: 'pending',
  };

  addFeature(feature);
  const stats = getBacklogStats();

  await ctx.reply(
    `\u2705 <b>Feature added to backlog</b>\n\n` +
    `<b>Name:</b> ${escapeHtml(feature.name)}\n` +
    `<b>ID:</b> <code>${escapeHtml(feature.id)}</code>\n` +
    `<b>Position:</b> #${stats.pending} in queue\n\n` +
    `<i>Agents will pick this up automatically.</i>`,
    { parse_mode: 'HTML' },
  );
}

async function handleKnowledge(ctx: Context): Promise<void> {
  try {
    const summary = await getKnowledgeSummary();
    await ctx.reply(
      `<b>Knowledge Base</b>\n\n<pre>${escapeHtml(summary)}</pre>`,
      { parse_mode: 'HTML' },
    );
  } catch {
    await ctx.reply('Knowledge base is empty. It will populate as agents complete features.');
  }
}

async function handleSearch(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /search &lt;query&gt;\n\nSearches the knowledge base.', {
      parse_mode: 'HTML',
    });
    return;
  }

  const query = sanitize(String(raw));
  try {
    const results = await searchKnowledge(query);
    if (results.length === 0) {
      await ctx.reply(`No results for "${escapeHtml(query)}"`);
      return;
    }

    let msg = `<b>Search: "${escapeHtml(query)}"</b>\n\n`;
    for (const entry of results.slice(0, 5)) {
      msg += `<b>${escapeHtml(entry.title)}</b>\n`;
      msg += `  ${escapeHtml(entry.summary)}\n`;
      msg += `  <code>${escapeHtml(entry.path)}</code>\n\n`;
    }
    await ctx.reply(msg, { parse_mode: 'HTML' });
  } catch {
    await ctx.reply('Knowledge base not yet initialized.');
  }
}

async function handleBudget(ctx: Context): Promise<void> {
  try {
    const html = tokenBudget.formatForTelegram();
    await ctx.reply(html, { parse_mode: 'HTML' });
  } catch {
    await ctx.reply('Token budget data unavailable.');
  }
}

async function handleHealth(ctx: Context): Promise<void> {
  const providerStatus = providerFailover.getStatus();
  const rateStats = rateLimiter.getStats();
  const mem = process.memoryUsage();
  const uptimeSeconds = process.uptime();

  const hours = Math.floor(uptimeSeconds / 3600);
  const minutes = Math.floor((uptimeSeconds % 3600) / 60);
  const rssM = (mem.rss / 1024 / 1024).toFixed(1);
  const heapM = (mem.heapUsed / 1024 / 1024).toFixed(1);
  const heapTotalM = (mem.heapTotal / 1024 / 1024).toFixed(1);
  const windowResetMin = Math.ceil(rateStats.windowResetMs / 60_000);

  let msg = `<b>System Health</b>\n\n`;

  msg += `<b>API Providers</b>\n`;
  for (const [name, info] of Object.entries(providerStatus)) {
    const icon = info.state === 'closed' ? '\u2705' : info.state === 'open' ? '\u274C' : '\u26A0\uFE0F';
    msg += `  ${icon} <b>${escapeHtml(name)}</b>: ${escapeHtml(info.state)} (${info.failures} failures)\n`;
  }

  msg += `\n<b>Rate Limiter</b>\n`;
  msg += `  Requests: ${rateStats.used}/${rateStats.effectiveLimit} (limit ${rateStats.limit})\n`;
  msg += `  Window resets in: ${windowResetMin}m\n`;

  msg += `\n<b>Process</b>\n`;
  msg += `  RSS: ${rssM} MB | Heap: ${heapM}/${heapTotalM} MB\n`;
  msg += `  Uptime: ${hours}h ${minutes}m\n`;

  await ctx.reply(msg, { parse_mode: 'HTML' });
}

async function handleWiki(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /wiki &lt;query&gt;\n\nSearch the wikidelve knowledge base.', {
      parse_mode: 'HTML',
    });
    return;
  }

  const query = sanitize(String(raw));

  try {
    const url = `${process.env.WIKIDELVE_URL}/api/search/hybrid?q=${encodeURIComponent(query)}&limit=5`;
    const res = await fetch(url);

    if (!res.ok) {
      await ctx.reply(`Wikidelve search failed (HTTP ${res.status}).`);
      return;
    }

    const data = (await res.json()) as Array<{ title?: string; snippet?: string; url?: string }>;

    if (!Array.isArray(data) || data.length === 0) {
      await ctx.reply(`No wikidelve results for "${escapeHtml(query)}".`);
      return;
    }

    let msg = `<b>Wikidelve: "${escapeHtml(query)}"</b>\n\n`;
    for (const item of data.slice(0, 5)) {
      const title = item.title ?? 'Untitled';
      const snippet = item.snippet ?? '';
      msg += `<b>${escapeHtml(title)}</b>\n`;
      if (snippet) {
        msg += `  ${escapeHtml(snippet.slice(0, 200))}\n`;
      }
      msg += '\n';
    }

    await ctx.reply(msg, { parse_mode: 'HTML' });
  } catch {
    await ctx.reply('Failed to reach wikidelve. Is the service running?');
  }
}

async function handleResearch(ctx: Context): Promise<void> {
  const raw = ctx.match;
  if (!raw || (typeof raw === 'string' && raw.trim().length === 0)) {
    await ctx.reply('Usage: /research &lt;topic&gt;\n\nQueue a new wikidelve research job.', {
      parse_mode: 'HTML',
    });
    return;
  }

  const topic = sanitize(String(raw));

  try {
    const res = await fetch(`${process.env.WIKIDELVE_URL}/api/research`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic }),
    });

    if (!res.ok) {
      await ctx.reply(`Research request failed (HTTP ${res.status}).`);
      return;
    }

    const data = (await res.json()) as { id?: string; jobId?: string };
    const jobId = data.id ?? data.jobId ?? 'unknown';

    await ctx.reply(
      `\u2705 <b>Research job queued</b>\n\n` +
      `<b>Topic:</b> ${escapeHtml(topic)}\n` +
      `<b>Job ID:</b> <code>${escapeHtml(String(jobId))}</code>`,
      { parse_mode: 'HTML' },
    );
  } catch {
    await ctx.reply('Failed to reach wikidelve. Is the service running?');
  }
}
