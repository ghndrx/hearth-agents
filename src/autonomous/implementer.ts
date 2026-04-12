// Autonomous implementation agent.
// Takes a PRD and implements it using MiniMax M2.7 with tool calling.

import { runAgent } from '../agents/index.js';
import { getAgentConfig } from '../agents/definitions.js';
import type { Feature } from './feature-backlog.js';
import type { GeneratedPRD } from './prd-generator.js';
import type { AgentRunnerEvent } from '../types/index.js';
import { GitManager } from '../pipeline/git-manager.js';

export interface ImplementationResult {
  featureId: string;
  branch: string;
  repo: string;
  success: boolean;
  output: string;
  filesChanged: string[];
}

export async function implementFeature(
  feature: Feature,
  prd: GeneratedPRD,
  repoPath: string,
  repoName: string,
): Promise<ImplementationResult> {
  const config = getAgentConfig('developer');
  if (!config) {
    return {
      featureId: feature.id,
      branch: '',
      repo: repoName,
      success: false,
      output: 'Developer agent config not found',
      filesChanged: [],
    };
  }

  const gitManager = new GitManager(repoPath);
  const branchName = `feat/${feature.id}`;

  // Clean up any existing worktree/branch from previous failed runs
  try {
    const repoDir = repoPath.split('/').pop() || 'repo';
    const wtBase = repoPath.replace(/\/[^/]+$/, `/worktrees-${repoDir}`);
    await gitManager.removeWorktree(`${wtBase}/${branchName}`).catch(() => {});
    const { execFile: ef } = await import('node:child_process');
    const { promisify: p } = await import('node:util');
    const exec = p(ef);
    await exec('git', ['branch', '-D', branchName], { cwd: repoPath }).catch(() => {});
    await exec('git', ['worktree', 'prune'], { cwd: repoPath }).catch(() => {});
  } catch {
    // Best effort cleanup
  }

  let worktreePath: string;
  try {
    worktreePath = await gitManager.createWorktree(branchName, 'develop');
  } catch {
    try {
      worktreePath = await gitManager.createWorktree(branchName);
    } catch (err) {
      return {
        featureId: feature.id,
        branch: branchName,
        repo: repoName,
        success: false,
        output: `Failed to create worktree: ${err}`,
        filesChanged: [],
      };
    }
  }

  const prompt = `Implement the following feature. You MUST use the provided tools to read and write files.

**Feature**: ${feature.name}
**Repository**: ${repoName}

## PRD
${prd.content.slice(0, 8000)}

## Required workflow - follow these steps exactly:
1. Use read_file and list_files to explore the codebase structure first
2. Use search_files to find relevant existing code patterns
3. Use write_file to create new files and edit_file to modify existing ones
4. Use git to commit your changes: git add -A then git commit -m "feat(${feature.id}): implement ${feature.name}"
5. Write at least one test file for the new functionality

IMPORTANT: You MUST call write_file or edit_file to create actual code changes. Do NOT just describe what to do - actually write the code using the tools.`;

  const output: string[] = [];

  // Pass null client - let model router pick the right provider (Kimi for developer role)
  for await (const event of runAgent(null as any, config, prompt, {
    cwd: worktreePath,
    maxTurns: 150,
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

  // Check what files changed
  let filesChanged: string[] = [];
  try {
    const status = await gitManager.getStatus(worktreePath);
    filesChanged = status.split('\n').filter(Boolean);
  } catch {
    // ignore
  }

  // Clean up worktree if no files were changed (failed implementation)
  if (filesChanged.length === 0) {
    try {
      await gitManager.removeWorktree(worktreePath);
      const { execFile: ef } = await import('node:child_process');
      const { promisify: p } = await import('node:util');
      const exec = p(ef);
      await exec('git', ['branch', '-D', branchName], { cwd: repoPath }).catch(() => {});
      await exec('git', ['worktree', 'prune'], { cwd: repoPath }).catch(() => {});
    } catch {
      // Best effort cleanup
    }
  }

  return {
    featureId: feature.id,
    branch: branchName,
    repo: repoName,
    success: filesChanged.length > 0,
    output: output.join('\n'),
    filesChanged,
  };
}
