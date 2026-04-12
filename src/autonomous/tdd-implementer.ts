// TDD implementation agent.
// Replaces single-shot implementation with a two-phase Red/Green TDD cycle:
//   Phase 1 (RED):   Write failing tests from PRD acceptance criteria
//   Phase 2 (GREEN): Implement minimum code to make tests pass (max 3 retries)

import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import { runAgent, getAgentConfig } from '../agents/index.js';
import { getModelForRole } from '../agents/model-router.js';
import { GitManager } from '../pipeline/git-manager.js';
import type { Feature } from './feature-backlog.js';
import type { GeneratedPRD } from './prd-generator.js';

const execFile = promisify(execFileCb);

const MAX_GREEN_RETRIES = 3;

export interface TDDResult {
  testsWritten: number;
  testsPassing: number;
  testsFailing: number;
  retries: number;
  success: boolean;
  output: string;
}

interface TestRunResult {
  passed: number;
  failed: number;
  total: number;
  raw: string;
  exitCode: number;
}

// ---------------------------------------------------------------------------
// Language detection and test runner
// ---------------------------------------------------------------------------

type RepoLang = 'go' | 'ts';

function detectLanguage(repoName: string): RepoLang {
  // hearth main repo is Go backend; desktop/mobile are TS
  if (repoName === 'hearth') return 'go';
  return 'ts';
}

async function runTests(worktreePath: string, lang: RepoLang): Promise<TestRunResult> {
  const result: TestRunResult = { passed: 0, failed: 0, total: 0, raw: '', exitCode: 0 };

  try {
    const { stdout, stderr } = lang === 'go'
      ? await execFile('go', ['test', './...', '-v', '-count=1'], {
          cwd: worktreePath,
          timeout: 120_000,
          maxBuffer: 5 * 1024 * 1024,
        })
      : await execFile('npx', ['vitest', 'run', '--reporter=verbose'], {
          cwd: worktreePath,
          timeout: 120_000,
          maxBuffer: 5 * 1024 * 1024,
        });

    result.raw = stdout + (stderr ? `\n${stderr}` : '');
    result.exitCode = 0;
  } catch (err: unknown) {
    // execFile rejects on non-zero exit code; capture output anyway
    const execErr = err as { stdout?: string; stderr?: string; code?: number };
    result.raw = (execErr.stdout ?? '') + '\n' + (execErr.stderr ?? '');
    result.exitCode = execErr.code ?? 1;
  }

  // Parse counts from output
  if (lang === 'go') {
    const passMatches = result.raw.match(/--- PASS/g);
    const failMatches = result.raw.match(/--- FAIL/g);
    result.passed = passMatches?.length ?? 0;
    result.failed = failMatches?.length ?? 0;
    result.total = result.passed + result.failed;
  } else {
    // Vitest output: "Tests  X passed | Y failed | Z total"
    const summary = result.raw.match(/Tests\s+(\d+)\s+passed\s*(?:\|\s*(\d+)\s+failed)?\s*\|\s*(\d+)\s+total/i);
    if (summary) {
      result.passed = parseInt(summary[1], 10);
      result.failed = summary[2] ? parseInt(summary[2], 10) : 0;
      result.total = parseInt(summary[3], 10);
    } else {
      // Fallback: any non-zero exit means failure
      if (result.exitCode !== 0) {
        result.failed = 1;
        result.total = 1;
      }
    }
  }

  return result;
}

// ---------------------------------------------------------------------------
// Git helpers (thin wrappers around execFile in the worktree)
// ---------------------------------------------------------------------------

async function gitCommit(worktreePath: string, message: string): Promise<void> {
  await execFile('git', ['add', '-A'], { cwd: worktreePath, timeout: 15_000 });
  await execFile('git', ['commit', '-m', message, '--allow-empty'], {
    cwd: worktreePath,
    timeout: 15_000,
  });
}

// ---------------------------------------------------------------------------
// Prompts
// ---------------------------------------------------------------------------

function buildRedPrompt(feature: Feature, prd: GeneratedPRD, repoName: string, lang: RepoLang): string {
  const testFramework = lang === 'go' ? 'Go testing package (testing.T)' : 'Vitest';
  const testExt = lang === 'go' ? '_test.go' : '.test.ts';

  return `You are writing **failing tests** for a new feature. This is the RED phase of TDD.

**Feature**: ${feature.name}
**Repository**: ${repoName}
**Test framework**: ${testFramework}

## PRD (contains acceptance criteria)
${prd.content}

## Instructions
1. Read the existing codebase to understand test patterns and conventions.
2. Write comprehensive test files (*${testExt}) covering **every** acceptance criterion in the PRD.
3. Tests MUST call functions/endpoints that **do not exist yet** so they compile but FAIL.
4. Do NOT implement any production code. Only write tests.
5. Commit your test files with the message: "test: add failing tests for ${feature.name}"

Focus on clear, descriptive test names that map to acceptance criteria.`;
}

function buildGreenPrompt(
  feature: Feature,
  prd: GeneratedPRD,
  repoName: string,
  lang: RepoLang,
  testOutput: string,
): string {
  return `You are implementing the **minimum code** to make all failing tests pass. This is the GREEN phase of TDD.

**Feature**: ${feature.name}
**Repository**: ${repoName}

## PRD
${prd.content}

## Current test output (these tests are FAILING)
\`\`\`
${testOutput}
\`\`\`

## Instructions
1. Read the failing test files to understand what is expected.
2. Implement the minimum production code to make every test pass.
3. Follow existing code style and patterns in the repository.
4. Run the tests to verify they pass.
5. Commit your changes with the message: "feat: implement ${feature.name}"

Do NOT modify the test files. Only add production code.`;
}

function buildRetryPrompt(
  feature: Feature,
  repoName: string,
  attempt: number,
  testOutput: string,
): string {
  return `Tests are still failing after implementation attempt ${attempt}. Fix the code.

**Feature**: ${feature.name}
**Repository**: ${repoName}

## Failing test output
\`\`\`
${testOutput}
\`\`\`

## Instructions
1. Read the error output carefully to understand what is still broken.
2. Fix the production code (do NOT modify tests).
3. Run the tests again to confirm they pass.
4. Commit fixes with the message: "fix: resolve failing tests for ${feature.name} (attempt ${attempt + 1})"`;
}

// ---------------------------------------------------------------------------
// Main TDD loop
// ---------------------------------------------------------------------------

export async function tddImplement(
  feature: Feature,
  prd: GeneratedPRD,
  repoPath: string,
  repoName: string,
): Promise<TDDResult> {
  const output: string[] = [];
  const lang = detectLanguage(repoName);

  // Resolve model and client for the developer role (Kimi K2.5)
  const modelConfig = getModelForRole('developer');
  const client = modelConfig.client;
  const config = getAgentConfig('developer');

  if (!config) {
    return {
      testsWritten: 0,
      testsPassing: 0,
      testsFailing: 0,
      retries: 0,
      success: false,
      output: 'Developer agent config not found',
    };
  }

  // Set up worktree
  const gitManager = new GitManager(repoPath);
  const branchName = `feat/${feature.id}`;

  let worktreePath: string;
  try {
    worktreePath = await gitManager.createWorktree(branchName, 'develop');
  } catch {
    try {
      worktreePath = await gitManager.createWorktree(branchName);
    } catch (err) {
      return {
        testsWritten: 0,
        testsPassing: 0,
        testsFailing: 0,
        retries: 0,
        success: false,
        output: `Failed to create worktree: ${err}`,
      };
    }
  }

  // -------------------------------------------------------------------
  // Phase 1: RED - Write failing tests
  // -------------------------------------------------------------------
  output.push('[tdd] Phase 1: RED - writing failing tests');

  const redPrompt = buildRedPrompt(feature, prd, repoName, lang);

  for await (const event of runAgent(null, config, redPrompt, {
    cwd: worktreePath,
    maxTurns: 80,
  })) {
    if (event.type === 'output' || event.type === 'error') {
      output.push(event.data);
    }
  }

  // Run tests - they MUST fail
  const redRun = await runTests(worktreePath, lang);
  output.push(`[tdd] RED test run: ${redRun.total} tests, ${redRun.passed} passed, ${redRun.failed} failed`);

  if (redRun.total === 0) {
    output.push('[tdd] No tests were written. Aborting.');
    return {
      testsWritten: 0,
      testsPassing: 0,
      testsFailing: 0,
      retries: 0,
      success: false,
      output: output.join('\n'),
    };
  }

  if (redRun.failed === 0 && redRun.exitCode === 0) {
    output.push('[tdd] Tests did not fail. RED phase violated - tests must fail before implementation.');
    return {
      testsWritten: redRun.total,
      testsPassing: redRun.passed,
      testsFailing: 0,
      retries: 0,
      success: false,
      output: output.join('\n'),
    };
  }

  // Commit failing tests
  try {
    await gitCommit(worktreePath, `test: add failing tests for ${feature.name}`);
    output.push('[tdd] Committed failing tests');
  } catch (err) {
    output.push(`[tdd] Warning: failed to commit tests: ${err}`);
  }

  // -------------------------------------------------------------------
  // Phase 2: GREEN - Implement to make tests pass
  // -------------------------------------------------------------------
  output.push('[tdd] Phase 2: GREEN - implementing to pass tests');

  let retries = 0;
  let latestRun = redRun;

  // Initial implementation attempt
  const greenPrompt = buildGreenPrompt(feature, prd, repoName, lang, redRun.raw);

  for await (const event of runAgent(null, config, greenPrompt, {
    cwd: worktreePath,
    maxTurns: 100,
  })) {
    if (event.type === 'output' || event.type === 'error') {
      output.push(event.data);
    }
  }

  latestRun = await runTests(worktreePath, lang);
  output.push(`[tdd] GREEN attempt 1: ${latestRun.passed}/${latestRun.total} passing`);

  // Retry loop if tests still fail
  while (latestRun.exitCode !== 0 && retries < MAX_GREEN_RETRIES) {
    retries++;
    output.push(`[tdd] Tests still failing, retry ${retries}/${MAX_GREEN_RETRIES}`);

    const retryPrompt = buildRetryPrompt(feature, repoName, retries, latestRun.raw);

    for await (const event of runAgent(null, config, retryPrompt, {
      cwd: worktreePath,
      maxTurns: 80,
    })) {
      if (event.type === 'output' || event.type === 'error') {
        output.push(event.data);
      }
    }

    latestRun = await runTests(worktreePath, lang);
    output.push(`[tdd] GREEN attempt ${retries + 1}: ${latestRun.passed}/${latestRun.total} passing`);
  }

  const allPassing = latestRun.exitCode === 0;

  // Commit implementation
  const commitMsg = allPassing
    ? `feat: implement ${feature.name}`
    : `wip: partial implementation of ${feature.name} (${latestRun.passed}/${latestRun.total} tests passing)`;

  try {
    await gitCommit(worktreePath, commitMsg);
    output.push(`[tdd] Committed: ${commitMsg}`);
  } catch (err) {
    output.push(`[tdd] Warning: failed to commit implementation: ${err}`);
  }

  return {
    testsWritten: redRun.total,
    testsPassing: latestRun.passed,
    testsFailing: latestRun.failed,
    retries,
    success: allPassing,
    output: output.join('\n'),
  };
}
