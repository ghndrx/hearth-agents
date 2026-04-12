/**
 * Interactive PR approval flow via Telegram inline keyboards.
 *
 * Sends rich PR detail messages with action buttons (Approve & Merge,
 * Request Changes, View Diff, Skip) and handles the full lifecycle
 * of each action through callback queries and follow-up text messages.
 */

import { type Bot, type Context, InlineKeyboard, InputFile } from 'grammy';
import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import { escapeHtml } from './formatters.js';

const execFile = promisify(execFileCb);

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface PRInfo {
  number: number;
  title: string;
  branch: string;
  repo: string;
  repoPath: string;
  url: string;
  filesChanged: number;
  additions: number;
  deletions: number;
  diffSummary: string;
}

/** Tracks users who are in the "request changes" feedback flow. */
interface PendingFeedback {
  prNumber: number;
  repo: string;
  repoPath: string;
  messageId: number;
  chatId: number;
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

/**
 * Map of chatId -> pending feedback request. When a user clicks
 * "Request Changes", we store the context here and wait for their
 * next text message as the review comment.
 */
const pendingFeedback = new Map<number, PendingFeedback>();

/**
 * Track which PR numbers have already been resolved (merged, closed,
 * or skipped) to handle stale callback queries gracefully.
 */
const resolvedPRs = new Set<string>();

/** Build a unique key for a PR in a given chat to track resolution state. */
function prKey(chatId: number, prNumber: number): string {
  return `${chatId}:${prNumber}`;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Maximum inline diff length before we send it as a file attachment. */
const MAX_INLINE_DIFF_LENGTH = 3000;

/** Telegram message text length limit. */
const MAX_MESSAGE_LENGTH = 4096;

/**
 * Run a `gh` CLI command and return stdout.
 * All git/GitHub operations go through `execFile` -- never `exec` or shell.
 */
async function runGh(
  args: string[],
  cwd: string,
): Promise<{ stdout: string; stderr: string }> {
  return execFile('gh', args, {
    cwd,
    maxBuffer: 10 * 1024 * 1024, // 10 MB for large diffs
    env: { ...process.env },
  });
}

/**
 * Check the current state of a PR via `gh pr view`.
 * Returns the state string ('OPEN', 'MERGED', 'CLOSED') or null on failure.
 */
async function getPRState(
  prNumber: number,
  repoPath: string,
): Promise<string | null> {
  try {
    const { stdout } = await runGh(
      ['pr', 'view', String(prNumber), '--json', 'state', '--jq', '.state'],
      repoPath,
    );
    return stdout.trim().toUpperCase();
  } catch {
    return null;
  }
}

/** Format the PR detail message sent to the user. */
function formatPRMessage(pr: PRInfo): string {
  const diffBar = buildDiffBar(pr.additions, pr.deletions);

  const lines = [
    `<b>\uD83D\uDD0D Pull Request #${pr.number}</b>`,
    '',
    `<b>Title:</b> ${escapeHtml(pr.title)}`,
    `<b>Branch:</b> <code>${escapeHtml(pr.branch)}</code>`,
    `<b>Repo:</b> <code>${escapeHtml(pr.repo)}</code>`,
    '',
    `<b>Files changed:</b> ${pr.filesChanged}`,
    `<b>Diff:</b> <code>${diffBar}</code>`,
    '',
    `<a href="${escapeHtml(pr.url)}">View on GitHub</a>`,
  ];

  if (pr.diffSummary.trim().length > 0) {
    const summary =
      pr.diffSummary.length > 800
        ? pr.diffSummary.slice(0, 797) + '...'
        : pr.diffSummary;
    lines.push('', `<pre>${escapeHtml(summary)}</pre>`);
  }

  return lines.join('\n');
}

/** Build a visual +/- bar for diff stats. */
function buildDiffBar(additions: number, deletions: number): string {
  return `+${additions} / -${deletions}`;
}

/** Build the inline keyboard for a PR approval message. */
function buildPRKeyboard(prNumber: number): InlineKeyboard {
  return new InlineKeyboard()
    .text('\u2705 Approve & Merge', `pr_approve:${prNumber}`)
    .text('\u270F\uFE0F Request Changes', `pr_changes:${prNumber}`)
    .row()
    .text('\uD83D\uDCC4 View Diff', `pr_diff:${prNumber}`)
    .text('\u23ED\uFE0F Skip', `pr_skip:${prNumber}`);
}

/** Parse the PR number from a callback query data string like "pr_approve:42". */
function parsePRNumber(data: string): number | null {
  const parts = data.split(':');
  if (parts.length < 2) return null;
  const num = Number.parseInt(parts[1]!, 10);
  return Number.isFinite(num) && num > 0 ? num : null;
}

/**
 * Attempt to edit the original message to show final status.
 * Failures are swallowed -- the message may have been deleted.
 */
async function editMessageStatus(
  ctx: Context,
  pr: PRInfo,
  statusLine: string,
): Promise<void> {
  try {
    const updatedText = [
      formatPRMessage(pr),
      '',
      statusLine,
    ].join('\n');

    // Telegram caps message length; truncate if needed.
    const safeText =
      updatedText.length > MAX_MESSAGE_LENGTH
        ? updatedText.slice(0, MAX_MESSAGE_LENGTH - 20) + '\n...(truncated)'
        : updatedText;

    await ctx.editMessageText(safeText, { parse_mode: 'HTML' });
  } catch {
    // Message may already be deleted or too old to edit.
  }
}

// ---------------------------------------------------------------------------
// We need to store PRInfo keyed by (chatId, prNumber) so callback handlers
// can reconstruct context without the full PRInfo in the callback data
// (Telegram limits callback data to 64 bytes).
// ---------------------------------------------------------------------------

const prInfoStore = new Map<string, PRInfo>();

function storePRInfo(chatId: number, pr: PRInfo): void {
  prInfoStore.set(prKey(chatId, pr.number), pr);
}

function loadPRInfo(chatId: number, prNumber: number): PRInfo | undefined {
  return prInfoStore.get(prKey(chatId, prNumber));
}

function cleanupPR(chatId: number, prNumber: number): void {
  const key = prKey(chatId, prNumber);
  prInfoStore.delete(key);
  resolvedPRs.add(key);
}

// ---------------------------------------------------------------------------
// Callback handlers
// ---------------------------------------------------------------------------

async function handleApproveAndMerge(ctx: Context): Promise<void> {
  const data = ctx.callbackQuery?.data;
  if (!data) return;

  const prNumber = parsePRNumber(data);
  const chatId = ctx.chat?.id;
  if (!prNumber || !chatId) return;

  // Guard against stale callbacks
  if (resolvedPRs.has(prKey(chatId, prNumber))) {
    await ctx.answerCallbackQuery({ text: 'This PR has already been handled.' });
    return;
  }

  const pr = loadPRInfo(chatId, prNumber);
  if (!pr) {
    await ctx.answerCallbackQuery({ text: 'PR info expired. Please re-send the PR.' });
    return;
  }

  // Check current PR state before attempting merge
  const state = await getPRState(prNumber, pr.repoPath);
  if (state === 'MERGED') {
    await ctx.answerCallbackQuery({ text: 'PR is already merged.' });
    await editMessageStatus(ctx, pr, '<b>\u2139\uFE0F Already merged</b>');
    cleanupPR(chatId, prNumber);
    return;
  }
  if (state === 'CLOSED') {
    await ctx.answerCallbackQuery({ text: 'PR has been closed.' });
    await editMessageStatus(ctx, pr, '<b>\u274C PR closed</b>');
    cleanupPR(chatId, prNumber);
    return;
  }

  await ctx.answerCallbackQuery({ text: 'Merging...' });

  try {
    await runGh(
      ['pr', 'merge', String(prNumber), '--merge', '--delete-branch'],
      pr.repoPath,
    );

    await editMessageStatus(
      ctx,
      pr,
      '<b>\u2705 Merged</b> by ' + escapeHtml(ctx.from?.first_name ?? 'user'),
    );
    cleanupPR(chatId, prNumber);
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : 'Unknown merge error';
    const safeMsg =
      message.length > 200 ? message.slice(0, 197) + '...' : message;

    await ctx.reply(
      `<b>\u274C Merge failed for PR #${prNumber}</b>\n\n<pre>${escapeHtml(safeMsg)}</pre>`,
      { parse_mode: 'HTML' },
    );
  }
}

async function handleRequestChanges(ctx: Context): Promise<void> {
  const data = ctx.callbackQuery?.data;
  if (!data) return;

  const prNumber = parsePRNumber(data);
  const chatId = ctx.chat?.id;
  if (!prNumber || !chatId) return;

  if (resolvedPRs.has(prKey(chatId, prNumber))) {
    await ctx.answerCallbackQuery({ text: 'This PR has already been handled.' });
    return;
  }

  const pr = loadPRInfo(chatId, prNumber);
  if (!pr) {
    await ctx.answerCallbackQuery({ text: 'PR info expired. Please re-send the PR.' });
    return;
  }

  // Store pending feedback state -- the next text message from this user
  // in this chat will be treated as the review comment.
  pendingFeedback.set(chatId, {
    prNumber,
    repo: pr.repo,
    repoPath: pr.repoPath,
    messageId: ctx.callbackQuery!.message?.message_id ?? 0,
    chatId,
  });

  await ctx.answerCallbackQuery({ text: 'Send your feedback as a text message.' });
  await ctx.reply(
    `<b>\u270F\uFE0F Requesting changes on PR #${prNumber}</b>\n\n` +
      'Reply with your feedback and it will be posted as a comment on the PR.\n' +
      'Send /cancel_review to abort.',
    { parse_mode: 'HTML' },
  );
}

async function handleViewDiff(ctx: Context): Promise<void> {
  const data = ctx.callbackQuery?.data;
  if (!data) return;

  const prNumber = parsePRNumber(data);
  const chatId = ctx.chat?.id;
  if (!prNumber || !chatId) return;

  const pr = loadPRInfo(chatId, prNumber);
  if (!pr) {
    await ctx.answerCallbackQuery({ text: 'PR info expired. Please re-send the PR.' });
    return;
  }

  await ctx.answerCallbackQuery({ text: 'Fetching diff...' });

  try {
    const { stdout: diff } = await runGh(
      ['pr', 'diff', String(prNumber)],
      pr.repoPath,
    );

    if (diff.trim().length === 0) {
      await ctx.reply('No diff available (empty changeset).');
      return;
    }

    if (diff.length <= MAX_INLINE_DIFF_LENGTH) {
      // Short diff: send inline as a code block
      const safeDiff =
        diff.length > MAX_MESSAGE_LENGTH - 100
          ? diff.slice(0, MAX_MESSAGE_LENGTH - 120) + '\n...(truncated)'
          : diff;

      await ctx.reply(`<pre>${escapeHtml(safeDiff)}</pre>`, {
        parse_mode: 'HTML',
      });
    } else {
      // Large diff: send as a file attachment
      const buffer = Buffer.from(diff, 'utf-8');
      const filename = `pr-${prNumber}.diff`;

      await ctx.replyWithDocument(new InputFile(buffer, filename), {
        caption: `Diff for PR #${prNumber} (${pr.filesChanged} files, +${pr.additions}/-${pr.deletions})`,
      });
    }
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : 'Unknown error fetching diff';
    await ctx.reply(
      `<b>\u274C Could not fetch diff for PR #${prNumber}</b>\n\n<pre>${escapeHtml(message.slice(0, 300))}</pre>`,
      { parse_mode: 'HTML' },
    );
  }
}

async function handleSkip(ctx: Context): Promise<void> {
  const data = ctx.callbackQuery?.data;
  if (!data) return;

  const prNumber = parsePRNumber(data);
  const chatId = ctx.chat?.id;
  if (!prNumber || !chatId) return;

  if (resolvedPRs.has(prKey(chatId, prNumber))) {
    await ctx.answerCallbackQuery({ text: 'This PR has already been handled.' });
    return;
  }

  const pr = loadPRInfo(chatId, prNumber);
  if (!pr) {
    await ctx.answerCallbackQuery({ text: 'Already dismissed.' });
    return;
  }

  await ctx.answerCallbackQuery({ text: 'Skipped' });
  await editMessageStatus(ctx, pr, '<b>\u23ED\uFE0F Skipped</b>');
  cleanupPR(chatId, prNumber);
}

/**
 * Middleware that intercepts text messages when the user is in the
 * "request changes" feedback flow. If the user has a pending feedback
 * request, their next text message is posted as a PR comment.
 */
async function handleFeedbackText(
  ctx: Context,
  next: () => Promise<void>,
): Promise<void> {
  const chatId = ctx.chat?.id;
  const text = ctx.message?.text;

  if (!chatId || !text) {
    await next();
    return;
  }

  const pending = pendingFeedback.get(chatId);
  if (!pending) {
    await next();
    return;
  }

  // Allow the user to cancel the feedback flow
  if (text.trim() === '/cancel_review') {
    pendingFeedback.delete(chatId);
    await ctx.reply('Review feedback cancelled.');
    return;
  }

  // Post the comment on the PR
  pendingFeedback.delete(chatId);

  try {
    await runGh(
      ['pr', 'comment', String(pending.prNumber), '--body', text],
      pending.repoPath,
    );

    await ctx.reply(
      `<b>\u2705 Comment posted on PR #${pending.prNumber}</b>`,
      { parse_mode: 'HTML' },
    );

    // Update the original PR message to reflect the review
    const pr = loadPRInfo(chatId, pending.prNumber);
    if (pr) {
      try {
        const updatedText = [
          formatPRMessage(pr),
          '',
          `<b>\u270F\uFE0F Changes requested</b> by ${escapeHtml(ctx.from?.first_name ?? 'user')}`,
        ].join('\n');

        const safeText =
          updatedText.length > MAX_MESSAGE_LENGTH
            ? updatedText.slice(0, MAX_MESSAGE_LENGTH - 20) + '\n...(truncated)'
            : updatedText;

        await ctx.api.editMessageText(chatId, pending.messageId, safeText, {
          parse_mode: 'HTML',
        });
      } catch {
        // Original message may be gone or uneditable
      }
      cleanupPR(chatId, pending.prNumber);
    }
  } catch (err: unknown) {
    const message =
      err instanceof Error ? err.message : 'Unknown error posting comment';
    await ctx.reply(
      `<b>\u274C Failed to post comment on PR #${pending.prNumber}</b>\n\n<pre>${escapeHtml(message.slice(0, 300))}</pre>`,
      { parse_mode: 'HTML' },
    );
  }
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Register all PR approval callback query handlers and the feedback
 * text interceptor middleware on the given bot instance.
 *
 * Must be called during bot setup, before `bot.start()`.
 */
export function registerPRApprovalHandlers(bot: Bot): void {
  // Callback query handlers for inline keyboard buttons
  bot.callbackQuery(/^pr_approve:\d+$/, handleApproveAndMerge);
  bot.callbackQuery(/^pr_changes:\d+$/, handleRequestChanges);
  bot.callbackQuery(/^pr_diff:\d+$/, handleViewDiff);
  bot.callbackQuery(/^pr_skip:\d+$/, handleSkip);

  // Text message middleware for the "request changes" feedback flow.
  // This must be registered so it runs before other text handlers
  // to intercept feedback replies.
  bot.on('message:text', handleFeedbackText);
}

/**
 * Send a PR approval message with an inline keyboard to the given chat.
 *
 * The message includes PR details (title, branch, diff stats) and four
 * action buttons. The user can approve/merge, request changes, view the
 * full diff, or skip the PR entirely.
 *
 * @param bot  - The grammY Bot instance.
 * @param chatId - Telegram chat ID to send the message to.
 * @param prInfo - Details about the pull request.
 */
export async function sendPRForApproval(
  bot: Bot,
  chatId: number,
  prInfo: PRInfo,
): Promise<void> {
  // Store PRInfo so callback handlers can access it later
  storePRInfo(chatId, prInfo);

  const message = formatPRMessage(prInfo);
  const keyboard = buildPRKeyboard(prInfo.number);

  await bot.api.sendMessage(chatId, message, {
    parse_mode: 'HTML',
    reply_markup: keyboard,
    link_preview_options: { is_disabled: true },
  });
}
