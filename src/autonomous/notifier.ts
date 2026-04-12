// Telegram notification agent.
// Sends periodic updates to Greg with GitHub links and progress.

import { Bot } from 'grammy';
import { NotificationBatcher } from './notification-batcher.js';
import type { Notification } from './notification-batcher.js';
import { ThreadManager } from './thread-manager.js';

export class TelegramNotifier {
  private bot: Bot;
  private chatId: number;
  private batcher: NotificationBatcher;
  private threadManager: ThreadManager;

  constructor(token: string, chatId: number) {
    this.bot = new Bot(token);
    this.chatId = chatId;
    this.batcher = new NotificationBatcher(this.bot, chatId);
    this.threadManager = new ThreadManager(this.bot);
  }

  /** Send a message immediately (critical / one-off). */
  async send(message: string): Promise<void> {
    try {
      await this.bot.api.sendMessage(this.chatId, message, {
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      });
    } catch (err) {
      if (isTelegramRateLimitError(err)) {
        const retryAfter = extractRetryAfter(err);
        console.warn(`[notifier] Rate limited, retrying after ${retryAfter}s`);
        await sleep(retryAfter * 1000);
        try {
          await this.bot.api.sendMessage(this.chatId, message, {
            parse_mode: 'HTML',
            link_preview_options: { is_disabled: true },
          });
        } catch (retryErr) {
          console.error('[notifier] Retry failed:', retryErr);
        }
      } else {
        console.error('[notifier] Failed to send Telegram message:', err);
      }
    }
  }

  /** Edit an existing message in-place for live status updates. */
  async editMessage(chatId: number, messageId: number, text: string): Promise<void> {
    try {
      await this.bot.api.editMessageText(chatId, messageId, text, {
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      });
    } catch (err: unknown) {
      if (isTelegramRateLimitError(err)) {
        const retryAfter = extractRetryAfter(err);
        console.warn(`[notifier] Edit rate limited, retrying after ${retryAfter}s`);
        await sleep(retryAfter * 1000);
        try {
          await this.bot.api.editMessageText(chatId, messageId, text, {
            parse_mode: 'HTML',
            link_preview_options: { is_disabled: true },
          });
        } catch {
          // Message might be too old or deleted; fail silently.
        }
      } else {
        // Edit failures are non-critical (message might be too old).
        console.debug('[notifier] Edit failed (non-critical):', (err as Error).message ?? err);
      }
    }
  }

  /** Send or reply within a feature thread. */
  async sendWithThread(featureKey: string, text: string): Promise<number> {
    return this.threadManager.sendOrReply(this.chatId, featureKey, text);
  }

  /** Batch a non-critical notification into the next digest. */
  async batch(notification: Notification): Promise<void> {
    await this.batcher.add(notification);
  }

  /** Force-flush any pending batched notifications. */
  async flushBatch(): Promise<void> {
    await this.batcher.flush();
  }

  async sendStartup(): Promise<void> {
    await this.send(
      `<b>Hearth Agents Online</b>\n\n` +
      `Autonomous development pipeline started.\n` +
      `Updates every 30-60 minutes.\n\n` +
      `<b>Repos:</b>\n` +
      `- <a href="https://github.com/ghndrx/hearth">hearth</a>\n` +
      `- <a href="https://github.com/ghndrx/hearth-desktop">hearth-desktop</a>\n` +
      `- <a href="https://github.com/ghndrx/hearth-mobile">hearth-mobile</a>`,
    );
  }

  async sendResearchStarted(featureName: string, topicCount: number): Promise<void> {
    await this.send(
      `<b>Research Started</b>\n\n` +
      `Feature: <i>${escapeHtml(featureName)}</i>\n` +
      `Topics: ${topicCount} research queries`,
    );
  }

  async sendPRDCreated(featureName: string, filename: string): Promise<void> {
    await this.send(
      `<b>PRD Created</b>\n\n` +
      `Feature: <i>${escapeHtml(featureName)}</i>\n` +
      `File: <code>PRDs/${escapeHtml(filename)}</code>`,
    );
  }

  async sendImplementationStarted(featureName: string, repo: string, branch: string): Promise<void> {
    await this.send(
      `<b>Implementation Started</b>\n\n` +
      `Feature: <i>${escapeHtml(featureName)}</i>\n` +
      `Repo: <a href="https://github.com/ghndrx/${escapeHtml(repo)}">${escapeHtml(repo)}</a>\n` +
      `Branch: <code>${escapeHtml(branch)}</code>`,
    );
  }

  async sendImplementationDone(
    featureName: string,
    repo: string,
    branch: string,
    filesChanged: number,
  ): Promise<void> {
    const branchUrl = `https://github.com/ghndrx/${repo}/tree/${branch}`;
    await this.send(
      `<b>Implementation Complete</b>\n\n` +
      `Feature: <i>${escapeHtml(featureName)}</i>\n` +
      `Repo: <a href="https://github.com/ghndrx/${escapeHtml(repo)}">${escapeHtml(repo)}</a>\n` +
      `Branch: <a href="${escapeHtml(branchUrl)}">${escapeHtml(branch)}</a>\n` +
      `Files changed: ${filesChanged}`,
    );
  }

  async sendPRCreated(repo: string, prUrl: string, title: string): Promise<void> {
    await this.send(
      `<b>Pull Request Created</b>\n\n` +
      `<a href="${escapeHtml(prUrl)}">${escapeHtml(title)}</a>\n` +
      `Repo: <a href="https://github.com/ghndrx/${escapeHtml(repo)}">${escapeHtml(repo)}</a>`,
    );
  }

  async sendProgressUpdate(
    currentFeature: string,
    phase: string,
    completed: number,
    total: number,
    recentActivity: string[],
  ): Promise<void> {
    const progress = Math.round((completed / total) * 100);
    const bar = '█'.repeat(Math.floor(progress / 10)) + '░'.repeat(10 - Math.floor(progress / 10));

    await this.send(
      `<b>Progress Update</b>\n\n` +
      `${bar} ${progress}% (${completed}/${total} features)\n\n` +
      `<b>Current:</b> ${escapeHtml(currentFeature)}\n` +
      `<b>Phase:</b> ${escapeHtml(phase)}\n\n` +
      `<b>Recent Activity:</b>\n` +
      recentActivity.map(a => `- ${escapeHtml(a)}`).join('\n') +
      `\n\n<b>Repos:</b>\n` +
      `<a href="https://github.com/ghndrx/hearth">hearth</a> | ` +
      `<a href="https://github.com/ghndrx/hearth-desktop">hearth-desktop</a> | ` +
      `<a href="https://github.com/ghndrx/hearth-mobile">hearth-mobile</a>`,
    );
  }

  async sendError(context: string, error: string): Promise<void> {
    await this.send(
      `<b>Error</b>\n\n` +
      `Context: ${escapeHtml(context)}\n` +
      `<pre>${escapeHtml(error.slice(0, 500))}</pre>`,
    );
  }

  /** Clean up timers when shutting down. */
  destroy(): void {
    this.batcher.destroy();
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function isTelegramRateLimitError(err: unknown): boolean {
  if (err && typeof err === 'object' && 'error_code' in err) {
    return (err as { error_code: number }).error_code === 429;
  }
  return false;
}

function extractRetryAfter(err: unknown): number {
  if (
    err &&
    typeof err === 'object' &&
    'parameters' in err &&
    typeof (err as Record<string, unknown>).parameters === 'object'
  ) {
    const params = (err as { parameters: { retry_after?: number } }).parameters;
    if (typeof params.retry_after === 'number') {
      return params.retry_after;
    }
  }
  return 5;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
