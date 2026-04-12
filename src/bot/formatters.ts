/**
 * Telegram message formatting helpers.
 * All functions produce Telegram HTML parse mode output.
 */

import type { AgentTask } from '../types/index.js';

const HTML_ESCAPE_MAP: Record<string, string> = {
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
};

/**
 * Escape text for safe inclusion in Telegram HTML messages.
 * Handles &, <, >, and " which are the characters Telegram's HTML parser requires escaping.
 */
export function escapeHtml(text: string): string {
  return text.replace(/[&<>"]/g, (ch) => HTML_ESCAPE_MAP[ch] ?? ch);
}

/** Human-readable duration from milliseconds. */
function humanDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m ${remainingSeconds}s`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return `${hours}h ${remainingMinutes}m`;
}

const STATUS_ICONS: Record<string, string> = {
  queued: '\u23F3',     // hourglass
  running: '\u25B6\uFE0F', // play
  completed: '\u2705',  // check
  failed: '\u274C',     // cross
  cancelled: '\u23F9\uFE0F', // stop
};

/**
 * Format an array of tasks into an HTML status dashboard.
 */
export function formatTaskStatus(tasks: AgentTask[]): string {
  if (tasks.length === 0) {
    return '<b>No tasks found.</b>\nUse /prd, /implement, /review, or /plan to create one.';
  }

  const lines: string[] = ['<b>\uD83D\uDCCB Task Dashboard</b>', ''];

  for (const task of tasks) {
    const icon = STATUS_ICONS[task.status] ?? '\u2753';
    const elapsed = task.startedAt
      ? humanDuration((task.completedAt ?? Date.now()) - task.startedAt)
      : '-';

    lines.push(
      `${icon} <b>${escapeHtml(task.id.slice(0, 8))}</b> | <code>${escapeHtml(task.role)}</code>`,
    );
    lines.push(
      `   Status: <i>${escapeHtml(task.status)}</i> | Elapsed: ${elapsed}`,
    );

    if (task.lastOutputLine) {
      const truncated =
        task.lastOutputLine.length > 120
          ? task.lastOutputLine.slice(0, 117) + '...'
          : task.lastOutputLine;
      lines.push(`   Last: <code>${escapeHtml(truncated)}</code>`);
    }

    lines.push('');
  }

  return lines.join('\n');
}

/**
 * Format a PR creation notification with action context.
 */
export function formatPRCreated(
  prUrl: string,
  title: string,
  branch: string,
): string {
  return [
    '<b>\uD83D\uDE80 Pull Request Created</b>',
    '',
    `<b>Title:</b> ${escapeHtml(title)}`,
    `<b>Branch:</b> <code>${escapeHtml(branch)}</code>`,
    '',
    `<a href="${escapeHtml(prUrl)}">View on GitHub</a>`,
  ].join('\n');
}

/**
 * Format a progress update for a running agent task.
 */
export function formatAgentProgress(
  taskId: string,
  output: string[],
  elapsed: number,
): string {
  const shortId = taskId.slice(0, 8);
  const tail = output.slice(-5);
  const outputBlock = tail.map((l) => escapeHtml(l)).join('\n');

  return [
    `<b>\u2699\uFE0F Task ${escapeHtml(shortId)}</b> | ${humanDuration(elapsed)}`,
    '',
    '<pre>',
    outputBlock || '(no output yet)',
    '</pre>',
  ].join('\n');
}

/**
 * Format an error notification for a failed task.
 */
export function formatError(taskId: string, error: string): string {
  const shortId = taskId.slice(0, 8);
  const safeError =
    error.length > 500 ? error.slice(0, 497) + '...' : error;

  return [
    `<b>\u274C Task ${escapeHtml(shortId)} Failed</b>`,
    '',
    `<pre>${escapeHtml(safeError)}</pre>`,
  ].join('\n');
}
