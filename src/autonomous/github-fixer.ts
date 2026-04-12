// GitHub auto-fixer: reads review feedback or CI failure output,
// asks Kimi to generate fixes, commits and pushes to the PR branch.

import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import { createMiniMaxClient, runAgent } from '../agents/index.js';
import { getAgentConfig } from '../agents/definitions.js';
import { getModelForRole } from '../agents/model-router.js';
import { log } from './logger.js';
import { TelegramNotifier } from './notifier.js';
import type { WebhookEvent } from './github-webhook.js';

const execFile = promisify(execFileCb);

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

/** Safe wrapper around `gh` CLI. Uses execFile to prevent shell injection. */
async function gh(args: string[], cwd?: string): Promise<string> {
  const { stdout } = await execFile('gh', args, {
    cwd,
    timeout: 60_000,
    maxBuffer: 5 * 1024 * 1024,
    env: { ...process.env, GH_NO_UPDATE_NOTIFIER: '1' },
  });
  return stdout.trim();
}

/** Clone a repo into a temp directory and checkout the PR branch. */
async function prepareWorkdir(repo: string, branch: string): Promise<string> {
  const dir = `/tmp/hearth-fix-${Date.now()}`;
  await execFile('gh', ['repo', 'clone', repo, dir, '--', '--depth=50'], {
    timeout: 120_000,
    env: { ...process.env, GH_NO_UPDATE_NOTIFIER: '1' },
  });
  await execFile('git', ['checkout', branch], { cwd: dir, timeout: 30_000 });
  return dir;
}

/** Resolve the head branch for a PR when it's not in the event payload. */
async function resolveBranch(repo: string, prNumber: number): Promise<string> {
  const json = await gh([
    'pr', 'view', String(prNumber),
    '--repo', repo,
    '--json', 'headRefName',
  ]);
  const parsed = JSON.parse(json) as { headRefName: string };
  return parsed.headRefName;
}

/** Commit all staged + unstaged changes and push. */
async function commitAndPush(cwd: string, message: string): Promise<void> {
  await execFile('git', ['add', '-A'], { cwd, timeout: 15_000 });
  // Check if there are actually changes to commit
  const { stdout: status } = await execFile('git', ['status', '--porcelain'], {
    cwd,
    timeout: 10_000,
  });
  if (!status.trim()) {
    log.info('fixer', 'No changes to commit after fix attempt');
    return;
  }
  await execFile(
    'git',
    ['commit', '-m', message],
    { cwd, timeout: 30_000 },
  );
  await execFile('git', ['push'], { cwd, timeout: 60_000 });
}

/** Clean up the temporary workdir. */
async function cleanup(dir: string): Promise<void> {
  await execFile('rm', ['-rf', dir], { timeout: 15_000 }).catch(() => {});
}

/** Get an optional TelegramNotifier if env is configured. */
function getNotifier(): TelegramNotifier | null {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = Number(process.env.TELEGRAM_CHAT_ID);
  if (!token || !chatId) return null;
  return new TelegramNotifier(token, chatId);
}

/** Send a Telegram notification. Silently swallows errors. */
async function notify(message: string): Promise<void> {
  const notifier = getNotifier();
  if (!notifier) return;
  try {
    await notifier.send(message);
  } catch (err) {
    log.warn('fixer', 'Telegram notification failed', {
      error: err instanceof Error ? err.message : String(err),
    });
  } finally {
    notifier.destroy();
  }
}

// ---------------------------------------------------------------------------
// Agent runner helper
// ---------------------------------------------------------------------------

async function runFixAgent(prompt: string, cwd: string): Promise<string> {
  const client = createMiniMaxClient();
  const config = getAgentConfig('developer');
  if (!config) throw new Error('Developer agent config not found');

  const output: string[] = [];
  for await (const event of runAgent(null, config, prompt, {
    cwd,
    maxTurns: 80,
  })) {
    switch (event.type) {
      case 'output':
        output.push(event.data);
        break;
      case 'tool_call':
        output.push(`[tool] ${event.data}`);
        break;
      case 'error':
        output.push(`[error] ${event.data}`);
        break;
      case 'done':
        output.push(event.data);
        break;
    }
  }
  return output.join('\n');
}

// ---------------------------------------------------------------------------
// Public fix functions
// ---------------------------------------------------------------------------

/**
 * Fix a PR based on review feedback.
 * Reads the review body, asks the agent to generate fixes, commits and pushes.
 */
export async function fixFromReview(
  repo: string,
  prNumber: number,
  reviewBody: string,
): Promise<{ success: boolean; message: string }> {
  const component = 'fixer:review';
  log.info(component, `Fixing PR #${prNumber} from review`, { repo });

  let workdir: string | undefined;
  try {
    // Fetch PR details for branch + diff context
    const prJson = await gh([
      'pr', 'view', String(prNumber),
      '--repo', repo,
      '--json', 'headRefName,title,body',
    ]);
    const pr = JSON.parse(prJson) as {
      headRefName: string;
      title: string;
      body: string;
    };

    // Fetch the diff
    const diff = await gh([
      'pr', 'diff', String(prNumber),
      '--repo', repo,
    ]);

    workdir = await prepareWorkdir(repo, pr.headRefName);

    const prompt = `You are fixing a pull request based on reviewer feedback.

**Repository**: ${repo}
**PR #${prNumber}**: ${pr.title}
**Branch**: ${pr.headRefName}

## Reviewer Feedback (CHANGES REQUESTED)
${reviewBody}

## Current PR Diff
\`\`\`diff
${diff.slice(0, 30_000)}
\`\`\`

## Instructions
1. Read the reviewer feedback carefully.
2. Examine the relevant files in the working directory to understand the full context.
3. Make the requested changes, addressing every point in the review.
4. Ensure the code compiles/passes lint. Run any available test commands.
5. Do NOT commit -- the orchestrator will handle git operations.

Focus on precisely addressing the reviewer's concerns. Do not refactor unrelated code.`;

    const agentOutput = await runFixAgent(prompt, workdir);
    log.info(component, 'Agent completed fix attempt', { outputLength: agentOutput.length });

    await commitAndPush(workdir, `fix: address review feedback on PR #${prNumber}`);

    const msg =
      `<b>Auto-Fix Applied (Review)</b>\n\n` +
      `PR: <a href="https://github.com/${repo}/pull/${prNumber}">#${prNumber}</a>\n` +
      `Repo: ${repo}\n` +
      `Trigger: Changes requested by reviewer`;
    await notify(msg);

    return { success: true, message: `Pushed review fixes for PR #${prNumber}` };
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    log.error(component, `Failed to fix PR #${prNumber}`, { error: errMsg, repo });
    await notify(
      `<b>Auto-Fix Failed (Review)</b>\n\n` +
      `PR: #${prNumber} in ${repo}\n` +
      `<pre>${errMsg.slice(0, 400)}</pre>`,
    );
    return { success: false, message: errMsg };
  } finally {
    if (workdir) await cleanup(workdir);
  }
}

/**
 * Fix a PR based on CI failure output.
 * Reads the check run output, asks the agent to diagnose and fix, commits and pushes.
 */
export async function fixFromCIFailure(
  repo: string,
  prNumber: number,
  checkName: string,
  output: string,
): Promise<{ success: boolean; message: string }> {
  const component = 'fixer:ci';
  log.info(component, `Fixing CI failure "${checkName}" on PR #${prNumber}`, { repo });

  let workdir: string | undefined;
  try {
    const branch = await resolveBranch(repo, prNumber);
    workdir = await prepareWorkdir(repo, branch);

    const prompt = `You are fixing a CI failure on a pull request.

**Repository**: ${repo}
**PR #${prNumber}**
**Branch**: ${branch}
**Failed Check**: ${checkName}

## CI Failure Output
\`\`\`
${output.slice(0, 30_000)}
\`\`\`

## Instructions
1. Analyze the CI failure output to identify the root cause.
2. Read the relevant source files in the working directory.
3. Fix the issue -- this might be a test failure, lint error, type error, or build failure.
4. Verify the fix by running the same check locally if possible (e.g. \`npm test\`, \`go test ./...\`, \`npm run lint\`).
5. Do NOT commit -- the orchestrator will handle git operations.

Focus narrowly on what caused CI to fail. Do not refactor unrelated code.`;

    const agentOutput = await runFixAgent(prompt, workdir);
    log.info(component, 'Agent completed CI fix attempt', { outputLength: agentOutput.length });

    await commitAndPush(workdir, `fix: resolve "${checkName}" CI failure on PR #${prNumber}`);

    const msg =
      `<b>Auto-Fix Applied (CI)</b>\n\n` +
      `PR: <a href="https://github.com/${repo}/pull/${prNumber}">#${prNumber}</a>\n` +
      `Repo: ${repo}\n` +
      `Failed check: ${checkName}`;
    await notify(msg);

    return { success: true, message: `Pushed CI fixes for "${checkName}" on PR #${prNumber}` };
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    log.error(component, `Failed to fix CI on PR #${prNumber}`, { error: errMsg, repo });
    await notify(
      `<b>Auto-Fix Failed (CI)</b>\n\n` +
      `PR: #${prNumber} in ${repo}\n` +
      `Check: ${checkName}\n` +
      `<pre>${errMsg.slice(0, 400)}</pre>`,
    );
    return { success: false, message: errMsg };
  } finally {
    if (workdir) await cleanup(workdir);
  }
}

/**
 * Handle a /fix or /retry command from a PR comment.
 * Re-fetches PR context and attempts a general fix pass.
 */
export async function fixFromCommand(
  repo: string,
  prNumber: number,
  commentBody: string,
): Promise<{ success: boolean; message: string }> {
  const component = 'fixer:command';
  log.info(component, `Processing /fix command on PR #${prNumber}`, { repo });

  let workdir: string | undefined;
  try {
    const prJson = await gh([
      'pr', 'view', String(prNumber),
      '--repo', repo,
      '--json', 'headRefName,title,body,reviews,statusCheckRollup',
    ]);
    const pr = JSON.parse(prJson) as {
      headRefName: string;
      title: string;
      body: string;
      reviews: Array<{ body: string; state: string }>;
      statusCheckRollup: Array<{ name: string; conclusion: string; status: string }>;
    };

    // Gather context: latest failing reviews and CI
    const failingReviews = (pr.reviews ?? [])
      .filter((r) => r.state === 'CHANGES_REQUESTED')
      .map((r) => r.body)
      .filter(Boolean)
      .join('\n---\n');

    const failingChecks = (pr.statusCheckRollup ?? [])
      .filter((c) => c.conclusion === 'failure' || c.conclusion === 'FAILURE')
      .map((c) => c.name)
      .join(', ');

    workdir = await prepareWorkdir(repo, pr.headRefName);

    const prompt = `You are addressing issues on a pull request after a /fix command.

**Repository**: ${repo}
**PR #${prNumber}**: ${pr.title}
**Branch**: ${pr.headRefName}

## User Command
${commentBody}

${failingReviews ? `## Outstanding Review Feedback\n${failingReviews}\n` : ''}
${failingChecks ? `## Failing CI Checks\n${failingChecks}\n` : ''}

## Instructions
1. Read the command context and any review feedback above.
2. Examine the codebase for the relevant files.
3. Make targeted fixes addressing the issues mentioned.
4. Run available tests/lint to verify the fix.
5. Do NOT commit -- the orchestrator will handle git operations.`;

    const agentOutput = await runFixAgent(prompt, workdir);
    log.info(component, 'Agent completed command fix', { outputLength: agentOutput.length });

    await commitAndPush(workdir, `fix: address /fix command on PR #${prNumber}`);

    const msg =
      `<b>Auto-Fix Applied (/fix command)</b>\n\n` +
      `PR: <a href="https://github.com/${repo}/pull/${prNumber}">#${prNumber}</a>\n` +
      `Repo: ${repo}`;
    await notify(msg);

    return { success: true, message: `Pushed fixes for /fix command on PR #${prNumber}` };
  } catch (err) {
    const errMsg = err instanceof Error ? err.message : String(err);
    log.error(component, `Failed /fix command on PR #${prNumber}`, { error: errMsg, repo });
    await notify(
      `<b>Auto-Fix Failed (/fix command)</b>\n\n` +
      `PR: #${prNumber} in ${repo}\n` +
      `<pre>${errMsg.slice(0, 400)}</pre>`,
    );
    return { success: false, message: errMsg };
  } finally {
    if (workdir) await cleanup(workdir);
  }
}

// ---------------------------------------------------------------------------
// Event dispatcher -- wires WebhookEvent to the right fixer
// ---------------------------------------------------------------------------

export async function handleWebhookEvent(event: WebhookEvent): Promise<void> {
  const component = 'fixer:dispatch';
  log.info(component, `Dispatching ${event.kind}`, {
    repo: event.repo,
    pr: event.prNumber,
  });

  switch (event.kind) {
    case 'review_changes_requested':
      await fixFromReview(event.repo, event.prNumber, event.detail);
      break;

    case 'ci_failure':
      await fixFromCIFailure(event.repo, event.prNumber, event.summary, event.detail);
      break;

    case 'fix_command':
    case 'retry_command':
      await fixFromCommand(event.repo, event.prNumber, event.detail);
      break;

    default:
      log.warn(component, `Unknown event kind: ${(event as WebhookEvent).kind}`);
  }
}
