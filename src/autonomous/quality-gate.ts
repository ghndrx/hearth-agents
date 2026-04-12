// Quality gate system for validating agent-generated code before PR creation.
// Runs TypeScript or Go checks in sequence (fail-fast) and returns structured results.

import { execFile as execFileCb } from 'node:child_process';
import { access } from 'node:fs/promises';
import { join } from 'node:path';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFileCb);

const CHECK_TIMEOUT_MS = 120_000;

export interface CheckResult {
  name: string;
  passed: boolean;
  output: string;
  durationMs: number;
}

export interface ValidationResult {
  passed: boolean;
  checks: CheckResult[];
  summary: string;
}

type RepoType = 'typescript' | 'go';

interface CheckDefinition {
  name: string;
  command: string;
  args: string[];
}

async function detectRepoType(worktreePath: string): Promise<RepoType> {
  try {
    await access(join(worktreePath, 'package.json'));
    return 'typescript';
  } catch {
    // not a TS repo
  }
  try {
    await access(join(worktreePath, 'go.mod'));
    return 'go';
  } catch {
    // not a Go repo
  }
  throw new Error(
    `Cannot detect repo type in ${worktreePath}: neither package.json nor go.mod found`,
  );
}

function getChecks(repoType: RepoType): CheckDefinition[] {
  if (repoType === 'typescript') {
    return [
      { name: 'typecheck', command: 'npx', args: ['tsc', '--noEmit'] },
      { name: 'lint', command: 'npx', args: ['eslint', '.', '--max-warnings', '0'] },
      { name: 'test', command: 'npx', args: ['vitest', 'run', '--reporter=json'] },
      { name: 'build', command: 'npm', args: ['run', 'build'] },
    ];
  }
  return [
    { name: 'typecheck', command: 'go', args: ['vet', './...'] },
    { name: 'lint', command: 'golangci-lint', args: ['run'] },
    { name: 'test', command: 'go', args: ['test', './...'] },
    { name: 'build', command: 'go', args: ['build', './...'] },
  ];
}

async function runCheck(
  check: CheckDefinition,
  worktreePath: string,
): Promise<CheckResult> {
  const start = Date.now();
  try {
    const { stdout, stderr } = await execFileAsync(check.command, check.args, {
      cwd: worktreePath,
      timeout: CHECK_TIMEOUT_MS,
      maxBuffer: 10 * 1024 * 1024,
    });
    const durationMs = Date.now() - start;
    const output = (stdout + '\n' + stderr).trim();
    console.log(`[quality-gate] ${check.name} passed (${durationMs}ms)`);
    return { name: check.name, passed: true, output, durationMs };
  } catch (err: unknown) {
    const durationMs = Date.now() - start;
    const output = extractErrorOutput(err);
    console.error(`[quality-gate] ${check.name} failed (${durationMs}ms)`);
    return { name: check.name, passed: false, output, durationMs };
  }
}

function extractErrorOutput(err: unknown): string {
  if (err && typeof err === 'object') {
    const e = err as { stdout?: string; stderr?: string; message?: string };
    const parts: string[] = [];
    if (e.stdout) parts.push(e.stdout);
    if (e.stderr) parts.push(e.stderr);
    if (parts.length > 0) return parts.join('\n').trim();
    if (e.message) return e.message;
  }
  return String(err);
}

function buildSummary(checks: CheckResult[], repoType: RepoType): string {
  const passed = checks.filter(c => c.passed).length;
  const total = checks.length;
  const allPassed = passed === total;
  const totalDuration = checks.reduce((sum, c) => sum + c.durationMs, 0);

  const lines: string[] = [
    `Quality gate ${allPassed ? 'PASSED' : 'FAILED'} (${repoType}) - ${passed}/${total} checks passed in ${totalDuration}ms`,
  ];

  for (const check of checks) {
    const icon = check.passed ? 'PASS' : 'FAIL';
    lines.push(`  [${icon}] ${check.name} (${check.durationMs}ms)`);
  }

  return lines.join('\n');
}

export async function validateBeforePR(
  worktreePath: string,
): Promise<ValidationResult> {
  console.log(`[quality-gate] Starting validation in ${worktreePath}`);

  const repoType = await detectRepoType(worktreePath);
  console.log(`[quality-gate] Detected repo type: ${repoType}`);

  const checkDefs = getChecks(repoType);
  const checks: CheckResult[] = [];

  for (const def of checkDefs) {
    console.log(`[quality-gate] Running ${def.name}...`);
    const result = await runCheck(def, worktreePath);
    checks.push(result);

    if (!result.passed) {
      console.log(`[quality-gate] Fail-fast: ${def.name} failed, skipping remaining checks`);
      break;
    }
  }

  const summary = buildSummary(checks, repoType);
  const passed = checks.every(c => c.passed);

  console.log(`[quality-gate] ${summary}`);

  return { passed, checks, summary };
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function truncate(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen - 3) + '...';
}

export function formatValidationForTelegram(result: ValidationResult): string {
  const icon = result.passed ? '\u2705' : '\u274C';
  const status = result.passed ? 'PASSED' : 'FAILED';

  const lines: string[] = [
    `<b>${icon} Quality Gate ${status}</b>`,
    '',
  ];

  for (const check of result.checks) {
    const checkIcon = check.passed ? '\u2705' : '\u274C';
    lines.push(
      `${checkIcon} <b>${escapeHtml(check.name)}</b> (${check.durationMs}ms)`,
    );
    if (!check.passed) {
      lines.push(`<pre>${escapeHtml(truncate(check.output, 800))}</pre>`);
    }
  }

  const totalMs = result.checks.reduce((sum, c) => sum + c.durationMs, 0);
  lines.push('');
  lines.push(`Total: ${totalMs}ms`);

  return lines.join('\n');
}
