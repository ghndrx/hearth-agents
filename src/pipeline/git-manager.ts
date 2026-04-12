import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import { join, resolve } from 'node:path';
import { mkdir, rm } from 'node:fs/promises';

const execFile = promisify(execFileCb);

export interface WorktreeInfo {
  path: string;
  branch: string;
  head: string;
  bare: boolean;
}

export class GitManager {
  private readonly repoPath: string;
  private readonly worktreeBaseDir: string;

  constructor(repoPath: string, worktreeBaseDir?: string) {
    this.repoPath = resolve(repoPath);
    // Per-repo worktree directory to avoid collisions between hearth/desktop/mobile
    const repoName = this.repoPath.split('/').pop() || 'repo';
    this.worktreeBaseDir = resolve(worktreeBaseDir ?? join(repoPath, '..', `worktrees-${repoName}`));
  }

  /**
   * Execute a git command in a specific working directory.
   * Uses execFile (not shell) to prevent injection.
   */
  private async git(args: string[], cwd?: string): Promise<string> {
    const { stdout } = await execFile('git', args, {
      cwd: cwd ?? this.repoPath,
      maxBuffer: 10 * 1024 * 1024, // 10 MB
      timeout: 60_000,
    });
    return stdout.trim();
  }

  /**
   * Create a new git worktree for a branch.
   * Creates the branch from the given base (default: HEAD) if it doesn't exist.
   * Returns the absolute path to the new worktree.
   */
  async createWorktree(branchName: string, fromRef?: string): Promise<string> {
    const worktreePath = join(this.worktreeBaseDir, branchName);

    // Ensure the parent directory exists
    await mkdir(this.worktreeBaseDir, { recursive: true });

    // Check if branch already exists
    const branchExists = await this.branchExists(branchName);

    if (branchExists) {
      // Worktree from existing branch
      await this.git(['worktree', 'add', worktreePath, branchName]);
    } else {
      // Create new branch and worktree together
      const base = fromRef ?? 'HEAD';
      await this.git(['worktree', 'add', '-b', branchName, worktreePath, base]);
    }

    return worktreePath;
  }

  /**
   * Remove a worktree and prune its tracking metadata.
   */
  async removeWorktree(worktreePath: string): Promise<void> {
    const absPath = resolve(worktreePath);
    try {
      await this.git(['worktree', 'remove', absPath, '--force']);
    } catch {
      // If git worktree remove fails (e.g. already removed), clean up manually
      await rm(absPath, { recursive: true, force: true });
      await this.git(['worktree', 'prune']);
    }
  }

  /**
   * List all active worktrees for this repository.
   */
  async listWorktrees(): Promise<WorktreeInfo[]> {
    const output = await this.git(['worktree', 'list', '--porcelain']);
    if (!output) return [];

    const worktrees: WorktreeInfo[] = [];
    let current: Partial<WorktreeInfo> = {};

    for (const line of output.split('\n')) {
      if (line.startsWith('worktree ')) {
        if (current.path) worktrees.push(current as WorktreeInfo);
        current = { path: line.slice('worktree '.length), bare: false };
      } else if (line.startsWith('HEAD ')) {
        current.head = line.slice('HEAD '.length);
      } else if (line.startsWith('branch ')) {
        // Format: refs/heads/branch-name
        current.branch = line.slice('branch '.length).replace('refs/heads/', '');
      } else if (line === 'bare') {
        current.bare = true;
      } else if (line === 'detached') {
        current.branch = '(detached)';
      }
    }
    if (current.path) worktrees.push(current as WorktreeInfo);

    return worktrees;
  }

  /**
   * Create a new branch from an optional base ref.
   */
  async createBranch(name: string, from?: string): Promise<void> {
    const args = ['branch', name];
    if (from) args.push(from);
    await this.git(args);
  }

  /**
   * Get git status output for a given worktree path.
   */
  async getStatus(worktreePath: string): Promise<string> {
    return this.git(['status', '--porcelain'], worktreePath);
  }

  /**
   * Get the diff for a branch compared to a base ref (default: main).
   * Useful for review jobs.
   */
  async getDiff(branchName: string, baseRef: string = 'main'): Promise<string> {
    return this.git(['diff', `${baseRef}...${branchName}`]);
  }

  /**
   * Get the current HEAD commit hash (short form) in a worktree.
   */
  async getHead(worktreePath: string): Promise<string> {
    return this.git(['rev-parse', '--short', 'HEAD'], worktreePath);
  }

  /**
   * Check if a branch exists locally.
   */
  private async branchExists(name: string): Promise<boolean> {
    try {
      await this.git(['rev-parse', '--verify', `refs/heads/${name}`]);
      return true;
    } catch {
      return false;
    }
  }
}
