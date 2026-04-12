/**
 * Main bot setup and configuration.
 *
 * Exports a `createBot()` factory that wires up authentication middleware,
 * command handlers, and error handling. Supports both long-polling (dev)
 * and webhook (prod) modes.
 */

import { Bot, webhookCallback } from 'grammy';
import type { Context } from 'grammy';
import { registerCommands, onTaskCreated } from './commands.js';
import type { TaskEventHandler } from './commands.js';

export { onTaskCreated } from './commands.js';
export { getTask, getAllTasks, getRecentTasks } from './commands.js';
export type { TaskEventHandler } from './commands.js';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

interface BotConfig {
  /** Telegram bot token from BotFather. */
  token: string;
  /** Comma-separated Telegram user IDs allowed to interact with the bot. */
  allowedUsers?: string;
  /** Optional webhook URL for production mode. */
  webhookUrl?: string;
  /** Optional webhook secret for request validation. */
  webhookSecret?: string;
}

// ---------------------------------------------------------------------------
// Auth middleware
// ---------------------------------------------------------------------------

function createAuthMiddleware(allowedUsers: Set<number>) {
  return async (ctx: Context, next: () => Promise<void>): Promise<void> => {
    const userId = ctx.from?.id;

    if (!userId || !allowedUsers.has(userId)) {
      // Silently ignore unauthorized users. Logging is preferred over
      // replying to avoid leaking the bot's existence to strangers.
      console.warn(
        `[auth] Rejected user ${userId ?? 'unknown'} (chat ${ctx.chat?.id ?? 'unknown'})`,
      );
      return;
    }

    await next();
  };
}

function parseAllowedUsers(raw: string | undefined): Set<number> {
  if (!raw || raw.trim().length === 0) {
    return new Set();
  }

  const ids = new Set<number>();
  for (const part of raw.split(',')) {
    const trimmed = part.trim();
    const parsed = Number.parseInt(trimmed, 10);
    if (Number.isFinite(parsed) && parsed > 0) {
      ids.add(parsed);
    } else if (trimmed.length > 0) {
      console.warn(`[auth] Ignoring invalid user ID: "${trimmed}"`);
    }
  }
  return ids;
}

// ---------------------------------------------------------------------------
// Bot factory
// ---------------------------------------------------------------------------

export interface HearthBot {
  /** The underlying grammY Bot instance. */
  bot: Bot;
  /** Start long-polling (development mode). */
  startPolling: () => void;
  /** Return an HTTP request handler for webhook mode (production). */
  createWebhookHandler: () => ReturnType<typeof webhookCallback>;
  /** Gracefully stop the bot. */
  stop: () => Promise<void>;
}

export function createBot(config: BotConfig): HearthBot {
  const { token, allowedUsers: allowedUsersRaw, webhookSecret } = config;

  if (!token) {
    throw new Error('TELEGRAM_BOT_TOKEN is required');
  }

  const bot = new Bot(token);

  // --- Auth middleware ---
  const allowedUsers = parseAllowedUsers(allowedUsersRaw);
  if (allowedUsers.size === 0) {
    console.warn(
      '[bot] TELEGRAM_ALLOWED_USERS is empty -- all messages will be rejected.',
    );
  } else {
    console.info(
      `[bot] Authorized users: ${[...allowedUsers].join(', ')}`,
    );
  }
  bot.use(createAuthMiddleware(allowedUsers));

  // --- Commands ---
  registerCommands(bot);

  // --- Error handling ---
  bot.catch((err) => {
    const ctx = err.ctx;
    console.error(
      `[bot] Error handling update ${ctx.update.update_id}:`,
      err.error,
    );

    // Best-effort error reply to the user
    ctx
      .reply('An internal error occurred. Please try again later.')
      .catch(() => {
        // Ignore reply failures (e.g. chat deleted, bot blocked)
      });
  });

  // --- Public interface ---
  return {
    bot,

    startPolling() {
      console.info('[bot] Starting long-polling...');
      bot.start({
        onStart: () => console.info('[bot] Bot is running (polling mode).'),
        drop_pending_updates: true,
      });
    },

    createWebhookHandler() {
      return webhookCallback(bot, 'express', {
        secretToken: webhookSecret,
      }) as unknown as ReturnType<typeof webhookCallback>;
    },

    async stop() {
      console.info('[bot] Stopping...');
      await bot.stop();
      console.info('[bot] Stopped.');
    },
  };
}
