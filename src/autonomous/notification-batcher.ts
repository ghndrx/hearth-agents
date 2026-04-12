// Batches non-critical notifications into digest messages.
// Critical notifications bypass the batch and send immediately.

import { Bot } from 'grammy';

export interface Notification {
  text: string;
  featureKey?: string;
  critical?: boolean;
}

interface BatchedItem {
  text: string;
  featureKey: string;
  timestamp: number;
}

export class NotificationBatcher {
  private bot: Bot;
  private chatId: number;
  private batch: BatchedItem[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly maxBatchSize: number;
  private readonly batchWindowMs: number;

  constructor(
    bot: Bot,
    chatId: number,
    options?: { maxBatchSize?: number; batchWindowMs?: number },
  ) {
    this.bot = bot;
    this.chatId = chatId;
    this.maxBatchSize = options?.maxBatchSize ?? 15;
    this.batchWindowMs = options?.batchWindowMs ?? 30_000;
  }

  async add(notification: Notification): Promise<void> {
    if (notification.critical) {
      await this.sendImmediate(notification.text);
      return;
    }

    this.batch.push({
      text: notification.text,
      featureKey: notification.featureKey ?? 'general',
      timestamp: Date.now(),
    });

    if (this.batch.length >= this.maxBatchSize) {
      await this.flush();
      return;
    }

    if (!this.timer) {
      this.timer = setTimeout(() => {
        void this.flush();
      }, this.batchWindowMs);
    }
  }

  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }

    if (this.batch.length === 0) return;

    const items = this.batch.splice(0);
    const digest = this.formatDigest(items);

    await this.sendWithRetry(digest);
  }

  private formatDigest(items: BatchedItem[]): string {
    const grouped = new Map<string, string[]>();
    for (const item of items) {
      const existing = grouped.get(item.featureKey) ?? [];
      existing.push(item.text);
      grouped.set(item.featureKey, existing);
    }

    const sections: string[] = [];
    for (const [featureKey, texts] of grouped) {
      const header = `<b>${escapeHtml(featureKey)}</b>`;
      const lines = texts.map((t) => `  - ${t}`).join('\n');
      sections.push(`${header}\n${lines}`);
    }

    const count = items.length;
    return (
      `<b>Digest</b> (${count} update${count === 1 ? '' : 's'})\n\n` +
      sections.join('\n\n')
    );
  }

  private async sendImmediate(text: string): Promise<void> {
    await this.sendWithRetry(text);
  }

  private async sendWithRetry(text: string): Promise<void> {
    try {
      await this.bot.api.sendMessage(this.chatId, text, {
        parse_mode: 'HTML',
        link_preview_options: { is_disabled: true },
      });
    } catch (err: unknown) {
      if (isTelegramRateLimitError(err)) {
        const retryAfter = extractRetryAfter(err);
        console.warn(`[batcher] Rate limited, retrying after ${retryAfter}s`);
        await sleep(retryAfter * 1000);
        try {
          await this.bot.api.sendMessage(this.chatId, text, {
            parse_mode: 'HTML',
            link_preview_options: { is_disabled: true },
          });
        } catch (retryErr) {
          console.error('[batcher] Retry failed:', retryErr);
        }
      } else {
        console.error('[batcher] Failed to send message:', err);
      }
    }
  }

  destroy(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
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
