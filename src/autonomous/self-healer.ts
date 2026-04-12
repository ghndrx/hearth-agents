// Self-healing system: detects and recovers from common failure modes.
// Keeps the autonomous loop running no matter what.

import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import { log } from './logger.js';

const execFile = promisify(execFileCb);

// -- Git healing --

export async function healGitState(repoPath: string): Promise<string[]> {
  const fixes: string[] = [];

  try {
    // Check for merge conflicts
    const { stdout: status } = await execFile('git', ['status', '--porcelain'], {
      cwd: repoPath, timeout: 10_000,
    });

    if (status.includes('UU ') || status.includes('AA ') || status.includes('DD ')) {
      log.warn('healer', 'Merge conflict detected, aborting merge', { repoPath });
      await execFile('git', ['merge', '--abort'], { cwd: repoPath, timeout: 10_000 }).catch(() => {});
      await execFile('git', ['rebase', '--abort'], { cwd: repoPath, timeout: 10_000 }).catch(() => {});
      fixes.push('Aborted merge/rebase conflict');
    }

    // Check for detached HEAD
    const { stdout: branch } = await execFile('git', ['rev-parse', '--abbrev-ref', 'HEAD'], {
      cwd: repoPath, timeout: 10_000,
    });
    if (branch.trim() === 'HEAD') {
      log.warn('healer', 'Detached HEAD detected, checking out develop', { repoPath });
      await execFile('git', ['checkout', 'develop'], { cwd: repoPath, timeout: 10_000 }).catch(() =>
        execFile('git', ['checkout', 'main'], { cwd: repoPath, timeout: 10_000 })
      );
      fixes.push('Fixed detached HEAD');
    }

    // Check for dirty worktree that blocks operations
    if (status.trim().length > 0 && !status.includes('??')) {
      log.warn('healer', 'Dirty worktree detected, stashing changes', { repoPath });
      await execFile('git', ['stash', '--include-untracked'], { cwd: repoPath, timeout: 10_000 });
      fixes.push('Stashed dirty worktree');
    }

    // Check for lock files
    const { stdout: lockCheck } = await execFile('ls', ['.git/index.lock'], {
      cwd: repoPath, timeout: 5_000,
    }).catch(() => ({ stdout: '' }));
    if (lockCheck) {
      log.warn('healer', 'Git lock file detected, removing', { repoPath });
      await execFile('rm', ['-f', '.git/index.lock'], { cwd: repoPath, timeout: 5_000 });
      fixes.push('Removed git lock file');
    }

    // Prune stale worktrees
    await execFile('git', ['worktree', 'prune'], { cwd: repoPath, timeout: 10_000 }).catch(() => {});

  } catch (err) {
    log.error('healer', `Git healing failed for ${repoPath}`, {
      error: err instanceof Error ? err.message : String(err),
    });
  }

  if (fixes.length > 0) {
    log.info('healer', `Applied ${fixes.length} git fixes`, { repoPath, fixes });
  }
  return fixes;
}

// -- API healing --

export class APIHealthMonitor {
  private consecutiveFailures = new Map<string, number>();
  private lastSuccess = new Map<string, number>();
  private cooldowns = new Map<string, number>();

  recordSuccess(provider: string): void {
    this.consecutiveFailures.set(provider, 0);
    this.lastSuccess.set(provider, Date.now());
    this.cooldowns.delete(provider);
  }

  recordFailure(provider: string, error: string): void {
    const failures = (this.consecutiveFailures.get(provider) || 0) + 1;
    this.consecutiveFailures.set(provider, failures);
    log.warn('healer', `API failure #${failures} for ${provider}`, { error: error.slice(0, 200) });

    // After 5 consecutive failures, enter cooldown
    if (failures >= 5) {
      const cooldownMs = Math.min(failures * 60_000, 300_000); // 1-5 min cooldown
      this.cooldowns.set(provider, Date.now() + cooldownMs);
      log.warn('healer', `${provider} in cooldown for ${cooldownMs / 1000}s after ${failures} failures`);
    }
  }

  isAvailable(provider: string): boolean {
    const cooldownEnd = this.cooldowns.get(provider);
    if (cooldownEnd && Date.now() < cooldownEnd) {
      return false;
    }
    if (cooldownEnd && Date.now() >= cooldownEnd) {
      this.cooldowns.delete(provider);
      log.info('healer', `${provider} cooldown expired, retrying`);
    }
    return true;
  }

  getStatus(): Record<string, { failures: number; available: boolean; lastSuccess: number }> {
    const providers = new Set([
      ...this.consecutiveFailures.keys(),
      ...this.lastSuccess.keys(),
    ]);
    const result: Record<string, any> = {};
    for (const p of providers) {
      result[p] = {
        failures: this.consecutiveFailures.get(p) || 0,
        available: this.isAvailable(p),
        lastSuccess: this.lastSuccess.get(p) || 0,
      };
    }
    return result;
  }
}

export const apiHealth = new APIHealthMonitor();

// -- Process healing --

export async function healProcess(): Promise<string[]> {
  const fixes: string[] = [];

  // Check memory usage
  const memUsage = process.memoryUsage();
  const heapUsedMB = Math.round(memUsage.heapUsed / 1024 / 1024);
  const heapTotalMB = Math.round(memUsage.heapTotal / 1024 / 1024);

  if (heapUsedMB > 500) {
    log.warn('healer', `High memory usage: ${heapUsedMB}MB / ${heapTotalMB}MB`);
    if (global.gc) {
      global.gc();
      fixes.push(`Forced GC at ${heapUsedMB}MB heap`);
    }
  }

  // Check event loop lag
  const start = Date.now();
  await new Promise(r => setImmediate(r));
  const lag = Date.now() - start;
  if (lag > 100) {
    log.warn('healer', `Event loop lag: ${lag}ms`);
    fixes.push(`Event loop lag detected: ${lag}ms`);
  }

  return fixes;
}

// -- Comprehensive health check --

export async function runHealthCheck(repoPaths: string[]): Promise<{
  healthy: boolean;
  issues: string[];
  fixes: string[];
}> {
  const issues: string[] = [];
  const fixes: string[] = [];

  // Check all repos
  for (const repoPath of repoPaths) {
    const gitFixes = await healGitState(repoPath);
    fixes.push(...gitFixes.map(f => `${repoPath}: ${f}`));
  }

  // Check API health
  const apiStatus = apiHealth.getStatus();
  for (const [provider, status] of Object.entries(apiStatus)) {
    if (!status.available) {
      issues.push(`${provider} API in cooldown (${status.failures} consecutive failures)`);
    }
    if (status.failures > 0 && status.failures < 5) {
      issues.push(`${provider} API has ${status.failures} recent failures`);
    }
  }

  // Check process health
  const processFixes = await healProcess();
  fixes.push(...processFixes);

  const healthy = issues.length === 0;
  if (!healthy) {
    log.warn('healer', `Health check: ${issues.length} issues found`, { issues });
  }

  return { healthy, issues, fixes };
}
