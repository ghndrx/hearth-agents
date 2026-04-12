/**
 * Bot entry point for development mode.
 *
 * Loads environment variables, creates the bot, starts long-polling,
 * and handles graceful shutdown.
 */

import 'dotenv/config';
import { createBot } from './bot/index.js';

const bot = createBot({
  token: process.env.TELEGRAM_BOT_TOKEN ?? '',
  allowedUsers: process.env.TELEGRAM_ALLOWED_USERS,
  webhookUrl: process.env.TELEGRAM_WEBHOOK_URL,
  webhookSecret: process.env.TELEGRAM_WEBHOOK_SECRET,
});

bot.startPolling();

// Graceful shutdown
function shutdown(signal: string) {
  console.info(`\n[bot] Received ${signal}, shutting down...`);
  bot.stop().then(() => {
    process.exit(0);
  }).catch((err) => {
    console.error('[bot] Error during shutdown:', err);
    process.exit(1);
  });
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
