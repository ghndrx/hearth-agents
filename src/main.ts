// Main entry point: autonomous development loop with Telegram updates.

import 'dotenv/config';
import { AutonomousLoop } from './autonomous/index.js';

const TELEGRAM_TOKEN = process.env.TELEGRAM_BOT_TOKEN ?? '';
const CHAT_ID = parseInt(process.env.TELEGRAM_ALLOWED_USERS?.split(',')[0] ?? '0', 10);

if (!TELEGRAM_TOKEN) {
  console.error('[main] TELEGRAM_BOT_TOKEN is required');
  process.exit(1);
}

if (!CHAT_ID) {
  console.error('[main] TELEGRAM_ALLOWED_USERS is required (first ID used for notifications)');
  process.exit(1);
}

console.log('[main] Starting Hearth Agents - Autonomous Mode');
console.log(`[main] MiniMax mode: ${process.env.MINIMAX_PROVIDER || 'api'}`);
console.log(`[main] Hearth repo: ${process.env.HEARTH_REPO_PATH || '../hearth'}`);
console.log(`[main] Notifications: Telegram chat ${CHAT_ID}`);

const loop = new AutonomousLoop(TELEGRAM_TOKEN, CHAT_ID);

loop.start().catch((err) => {
  console.error('[main] Fatal error:', err);
  process.exit(1);
});

async function shutdown(signal: string) {
  console.log(`\n[main] ${signal} received, shutting down...`);
  loop.stop();
  process.exit(0);
}

process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));
