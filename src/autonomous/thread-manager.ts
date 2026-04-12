// Manages threaded Telegram conversations per feature.
// All updates for a feature reply to its root message.

import { Bot } from 'grammy';

export class ThreadManager {
  private bot: Bot;
  private threads: Map<string, number> = new Map();

  constructor(bot: Bot) {
    this.bot = bot;
  }

  async sendOrReply(
    chatId: number,
    featureKey: string,
    text: string,
    options?: { forceNew?: boolean },
  ): Promise<number> {
    const existingRoot = this.threads.get(featureKey);

    if (existingRoot && !options?.forceNew) {
      return await this.replyToThread(chatId, existingRoot, text);
    }

    return await this.createThread(chatId, featureKey, text);
  }

  hasThread(featureKey: string): boolean {
    return this.threads.has(featureKey);
  }

  getThreadRoot(featureKey: string): number | undefined {
    return this.threads.get(featureKey);
  }

  clearThread(featureKey: string): void {
    this.threads.delete(featureKey);
  }

  private async createThread(
    chatId: number,
    featureKey: string,
    text: string,
  ): Promise<number> {
    try {
      const result = await this.sendWithRetry(chatId, text, undefined);
      this.threads.set(featureKey, result);
      return result;
    } catch (err) {
      console.error(`[thread-manager] Failed to create thread for ${featureKey}:`, err);
      return 0;
    }
  }

  private async replyToThread(
    chatId: number,
    rootMessageId: number,
    text: string,
  ): Promise<number> {
    try {
      return await this.sendWithRetry(chatId, text, rootMessageId);
    } catch (err) {
      console.error('[thread-manager] Failed to reply to thread:', err);
      // Fall back to a new message if reply fails
      try {
        const result = await this.bot.api.sendMessage(chatId, text, {
          parse_mode: 'HTML',
          link_preview_options: { is_disabled: true },
        });
        return result.message_id;
      } catch (fallbackErr) {
        console.error('[thread-manager] Fallback send also failed:', fallbackErr);
        return 0;
      }
    }
  }

  private async sendWithRetry(
    chatId: number,
    text: string,
    replyToMessageId: number | undefined,
  ): Promise<number> {
    const params: Parameters<Bot['api']['sendMessage']>[2] = {
      parse_mode: 'HTML' as const,
      link_preview_options: { is_disabled: true },
    };

    if (replyToMessageId !== undefined) {
      params.reply_parameters = {
        message_id: replyToMessageId,
        allow_sending_without_reply: true,
      };
    }

    try {
      const result = await this.bot.api.sendMessage(chatId, text, params);
      return result.message_id;
    } catch (err: unknown) {
      if (isTelegramRateLimitError(err)) {
        const retryAfter = extractRetryAfter(err);
        console.warn(`[thread-manager] Rate limited, retrying after ${retryAfter}s`);
        await sleep(retryAfter * 1000);
        const result = await this.bot.api.sendMessage(chatId, text, params);
        return result.message_id;
      }
      throw err;
    }
  }
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
