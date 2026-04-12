// Repo guard: prevents agents from committing slop, sensitive data, or AI artifacts.
// Runs before every commit to product repos.

import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';

const execFile = promisify(execFileCb);

interface GuardResult {
  clean: boolean;
  violations: string[];
  autoFixed: boolean;
}

const FORBIDDEN_PATTERNS = [
  // AI slop markers
  { pattern: /TODO:?\s*(implement|add|fix|complete)/i, reason: 'TODO placeholder' },
  { pattern: /not implemented/i, reason: 'Not implemented stub' },
  { pattern: /placeholder/i, reason: 'Placeholder code' },
  { pattern: /FIXME/i, reason: 'FIXME marker' },
  { pattern: /\/\/ ?\.\.\./i, reason: 'Ellipsis comment' },

  // Sensitive data
  { pattern: /sk-[a-zA-Z0-9_-]{20,}/g, reason: 'API key detected' },
  { pattern: /AKIA[0-9A-Z]{16}/g, reason: 'AWS access key detected' },
  { pattern: /-----BEGIN (?:RSA |EC )?PRIVATE KEY/g, reason: 'Private key detected' },
  { pattern: /password\s*[:=]\s*["'][^"']+["']/gi, reason: 'Hardcoded password' },

  // AI-generated markdown slop
  { pattern: /^#{1,3}\s*(?:Overview|Summary|Conclusion|References)\s*$/m, reason: 'AI-generated markdown header in code repo' },
];

const FORBIDDEN_FILES = [
  /\.md$/i,        // No markdown in product repos (except README, CHANGELOG, CONTRIBUTING, LICENSE)
  /research/i,     // No research files
  /prd/i,          // No PRD files
  /knowledge/i,    // No knowledge base files
  /\.log$/i,       // No log files
  /\.env(?:\.|$)/i, // No env files
  /debug/i,        // No debug files
];

const ALLOWED_MD_FILES = new Set([
  'README.md',
  'CHANGELOG.md',
  'CONTRIBUTING.md',
  'LICENSE.md',
  'AGENTS.md',
  'CLAUDE.md',
  'SECURITY.md',
  'CODE_OF_CONDUCT.md',
]);

export async function guardCommit(repoPath: string): Promise<GuardResult> {
  const violations: string[] = [];

  // Get staged files
  const { stdout } = await execFile('git', ['diff', '--cached', '--name-only'], {
    cwd: repoPath,
    timeout: 10_000,
  });

  const stagedFiles = stdout.trim().split('\n').filter(Boolean);

  for (const file of stagedFiles) {
    const basename = file.split('/').pop() || '';

    // Check for forbidden file types
    for (const pattern of FORBIDDEN_FILES) {
      if (pattern.test(basename) || pattern.test(file)) {
        if (basename.endsWith('.md') && ALLOWED_MD_FILES.has(basename)) continue;
        violations.push(`Forbidden file: ${file} (${pattern.source})`);
      }
    }

    // Check file contents for forbidden patterns
    try {
      const { stdout: content } = await execFile('git', ['show', `:${file}`], {
        cwd: repoPath,
        timeout: 10_000,
        maxBuffer: 1024 * 1024,
      });

      for (const { pattern, reason } of FORBIDDEN_PATTERNS) {
        if (pattern.test(content)) {
          violations.push(`${file}: ${reason}`);
          pattern.lastIndex = 0; // Reset regex state
        }
      }
    } catch {
      // File might be binary or deleted
    }
  }

  // Check for outdated dependencies (npm audit)
  try {
    await execFile('npm', ['audit', '--audit-level=high', '--json'], {
      cwd: repoPath,
      timeout: 30_000,
    });
  } catch (err: any) {
    if (err.stdout) {
      try {
        const audit = JSON.parse(err.stdout);
        if (audit.metadata?.vulnerabilities?.high > 0 || audit.metadata?.vulnerabilities?.critical > 0) {
          violations.push(`npm audit: ${audit.metadata.vulnerabilities.high} high, ${audit.metadata.vulnerabilities.critical} critical vulnerabilities`);
        }
      } catch {
        // Audit parse failed, non-critical
      }
    }
  }

  if (violations.length > 0) {
    console.warn(`[repo-guard] ${violations.length} violation(s) found:`);
    for (const v of violations) {
      console.warn(`  - ${v}`);
    }
  }

  return {
    clean: violations.length === 0,
    violations,
    autoFixed: false,
  };
}

// Scrub sensitive data from git history if accidentally committed
export async function scrubFromHistory(repoPath: string, pattern: string): Promise<void> {
  console.warn(`[repo-guard] Scrubbing pattern from git history: ${pattern}`);
  await execFile('git', [
    'filter-branch', '--force', '--tree-filter',
    `find . -type f -exec sed -i '' 's/${pattern}/REDACTED/g' {} +`,
    'HEAD',
  ], {
    cwd: repoPath,
    timeout: 120_000,
  });
}

// Reset all working branches to clean state
export async function resetBranches(repoPath: string): Promise<string[]> {
  const { stdout } = await execFile('git', ['branch', '--format=%(refname:short)'], {
    cwd: repoPath,
    timeout: 10_000,
  });

  const branches = stdout.trim().split('\n').filter(b => b && b !== 'main' && b !== 'develop');
  const removed: string[] = [];

  for (const branch of branches) {
    if (branch.startsWith('feat/') || branch.startsWith('agent/')) {
      try {
        await execFile('git', ['branch', '-D', branch], { cwd: repoPath, timeout: 10_000 });
        removed.push(branch);
      } catch {
        // Branch might be checked out
      }
    }
  }

  return removed;
}
