// Structured logging for hearth-agents.
// All agent activity, API calls, and errors logged with timestamps.

import { appendFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';

const LOG_DIR = join(process.cwd(), 'logs');
const LOG_FILE = join(LOG_DIR, `agent-${new Date().toISOString().split('T')[0]}.log`);

type LogLevel = 'DEBUG' | 'INFO' | 'WARN' | 'ERROR';

async function ensureLogDir(): Promise<void> {
  await mkdir(LOG_DIR, { recursive: true });
}

function formatEntry(level: LogLevel, component: string, message: string, data?: Record<string, unknown>): string {
  const ts = new Date().toISOString();
  const dataStr = data ? ` ${JSON.stringify(data)}` : '';
  return `${ts} [${level}] [${component}] ${message}${dataStr}\n`;
}

async function writeLog(level: LogLevel, component: string, message: string, data?: Record<string, unknown>): Promise<void> {
  const entry = formatEntry(level, component, message, data);

  // Always console log
  const consoleFn = level === 'ERROR' ? console.error : level === 'WARN' ? console.warn : console.log;
  consoleFn(entry.trim());

  // Write to file
  try {
    await ensureLogDir();
    await appendFile(LOG_FILE, entry);
  } catch {
    // Don't crash if file logging fails
  }
}

export const log = {
  debug: (component: string, message: string, data?: Record<string, unknown>) =>
    writeLog('DEBUG', component, message, data),
  info: (component: string, message: string, data?: Record<string, unknown>) =>
    writeLog('INFO', component, message, data),
  warn: (component: string, message: string, data?: Record<string, unknown>) =>
    writeLog('WARN', component, message, data),
  error: (component: string, message: string, data?: Record<string, unknown>) =>
    writeLog('ERROR', component, message, data),

  // Convenience for API call logging
  apiCall: (provider: string, model: string, tokens?: { input?: number; output?: number }) =>
    writeLog('INFO', 'api', `${provider}/${model}`, { tokens }),
  apiError: (provider: string, error: string, status?: number) =>
    writeLog('ERROR', 'api', `${provider} failed`, { error: error.slice(0, 500), status }),
};
