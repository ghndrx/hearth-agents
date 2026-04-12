import type {
  JobRecord,
  BotNotifier,
  OrchestratorInterface,
  PipelineConfig,
} from '../types/index.js';
import type { JobQueue } from './job-queue.js';
import type { GitManager } from './git-manager.js';

const DEFAULT_MAX_CONCURRENT = 3;
const DEFAULT_TICK_INTERVAL_MS = 5_000;

export class PipelineRunner {
  private readonly jobQueue: JobQueue;
  private readonly orchestrator: OrchestratorInterface;
  private readonly botNotifier: BotNotifier;
  private readonly gitManager: GitManager;
  private readonly hearthRepoPath: string;
  private readonly maxConcurrent: number;
  private readonly tickIntervalMs: number;

  private tickTimer: ReturnType<typeof setInterval> | null = null;
  private running = false;
  private shuttingDown = false;
  private activeJobs = new Map<number, AbortController>();
  private signalHandlers: Array<{ signal: string; handler: () => void }> = [];

  constructor(
    jobQueue: JobQueue,
    orchestrator: OrchestratorInterface,
    botNotifier: BotNotifier,
    gitManager: GitManager,
    config: PipelineConfig,
  ) {
    this.jobQueue = jobQueue;
    this.orchestrator = orchestrator;
    this.botNotifier = botNotifier;
    this.gitManager = gitManager;
    this.hearthRepoPath = config.hearthRepoPath;
    this.maxConcurrent = config.maxConcurrentAgents ?? DEFAULT_MAX_CONCURRENT;
    this.tickIntervalMs = config.tickIntervalMs ?? DEFAULT_TICK_INTERVAL_MS;
  }

  /**
   * Start the pipeline runner. Sets up the tick interval and signal handlers.
   */
  start(): void {
    if (this.running) return;
    this.running = true;
    this.shuttingDown = false;

    this.tickTimer = setInterval(() => {
      this.tick().catch((err) => {
        console.error('[PipelineRunner] tick error:', err);
      });
    }, this.tickIntervalMs);

    // Run first tick immediately
    this.tick().catch((err) => {
      console.error('[PipelineRunner] initial tick error:', err);
    });

    this.registerSignalHandlers();
    console.log(`[PipelineRunner] started (max ${this.maxConcurrent} concurrent, ${this.tickIntervalMs}ms interval)`);
  }

  /**
   * Stop the runner gracefully. Waits for active agents to finish.
   */
  async stop(): Promise<void> {
    if (!this.running) return;
    this.shuttingDown = true;
    this.running = false;

    if (this.tickTimer) {
      clearInterval(this.tickTimer);
      this.tickTimer = null;
    }

    this.removeSignalHandlers();

    if (this.activeJobs.size > 0) {
      console.log(`[PipelineRunner] waiting for ${this.activeJobs.size} active job(s) to finish...`);
      // Signal all active jobs to abort
      for (const controller of this.activeJobs.values()) {
        controller.abort();
      }
      // Wait up to 30s for graceful completion
      await this.waitForActiveJobs(30_000);
    }

    console.log('[PipelineRunner] stopped');
  }

  /**
   * Single tick: claim queued jobs up to concurrency limit and execute them.
   */
  async tick(): Promise<void> {
    if (this.shuttingDown) return;

    const activeCount = this.activeJobs.size;
    const slotsAvailable = this.maxConcurrent - activeCount;

    if (slotsAvailable <= 0) return;

    // Claim up to the number of available slots
    for (let i = 0; i < slotsAvailable; i++) {
      if (this.shuttingDown) break;

      const job = this.jobQueue.claim();
      if (!job) break; // queue is empty

      console.log(`[PipelineRunner] claimed job #${job.id} (${job.role}): ${job.description.slice(0, 80)}`);

      // Execute job in background (don't await - it runs concurrently)
      const controller = new AbortController();
      this.activeJobs.set(job.id, controller);

      this.executeJob(job, controller.signal)
        .catch((err) => {
          console.error(`[PipelineRunner] job #${job.id} unhandled error:`, err);
        })
        .finally(() => {
          this.activeJobs.delete(job.id);
        });
    }
  }

  /**
   * Execute a single job based on its role. Handles worktree creation,
   * agent spawning, output streaming, and status updates.
   */
  async executeJob(job: JobRecord, signal?: AbortSignal): Promise<void> {
    const startTime = Date.now();

    try {
      await this.notifySafe(
        job.chat_id,
        `Started ${job.role} job #${job.id}: ${job.description.slice(0, 100)}`,
      );

      let result: { exitCode: number; output: string };

      switch (job.role) {
        case 'prd':
          result = await this.executePrdJob(job, signal);
          break;
        case 'implement':
          result = await this.executeImplementJob(job, signal);
          break;
        case 'review':
          result = await this.executeReviewJob(job, signal);
          break;
        default:
          throw new Error(`Unknown job role: ${job.role}`);
      }

      const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);

      if (result.exitCode === 0) {
        this.jobQueue.complete(job.id, result.output);
        await this.notifySafe(
          job.chat_id,
          `Job #${job.id} (${job.role}) completed in ${elapsed}s`,
        );
      } else {
        const truncatedOutput = result.output.slice(-2000);
        this.jobQueue.fail(job.id, truncatedOutput);
        await this.notifySafe(
          job.chat_id,
          `Job #${job.id} (${job.role}) failed (exit ${result.exitCode}) after ${elapsed}s:\n${truncatedOutput.slice(0, 500)}`,
        );
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.jobQueue.fail(job.id, message);
      await this.notifySafe(
        job.chat_id,
        `Job #${job.id} (${job.role}) errored: ${message.slice(0, 500)}`,
      );
    }
  }

  /**
   * PRD writer: runs in the main Hearth repo to generate a PRD document.
   */
  private async executePrdJob(
    job: JobRecord,
    signal?: AbortSignal,
  ): Promise<{ exitCode: number; output: string }> {
    const prompt = [
      'You are a PRD writer agent. Write a detailed product requirements document for the following task.',
      `Task: ${job.description}`,
      'Output the PRD as a markdown file. Be thorough with acceptance criteria, technical constraints, and implementation notes.',
    ].join('\n\n');

    return this.spawnAgent(job, prompt, this.hearthRepoPath, signal);
  }

  /**
   * Implementation: creates a worktree, runs developer agent inside it.
   */
  private async executeImplementJob(
    job: JobRecord,
    signal?: AbortSignal,
  ): Promise<{ exitCode: number; output: string }> {
    const branchName = job.branch_name ?? `agent/implement-${job.id}`;
    let worktreePath: string | undefined;

    try {
      worktreePath = await this.gitManager.createWorktree(branchName);
      this.jobQueue.updateWorktree(job.id, worktreePath);

      const prdContext = job.prd_path
        ? `\nRefer to the PRD at: ${job.prd_path}`
        : '';

      const prompt = [
        'You are a developer agent. Implement the following task in this repository.',
        `Task: ${job.description}`,
        prdContext,
        'Write clean, tested code. Commit your changes with descriptive messages when done.',
      ].join('\n\n');

      return await this.spawnAgent(job, prompt, worktreePath, signal);
    } catch (err) {
      // Clean up worktree on failure
      if (worktreePath) {
        await this.safeRemoveWorktree(worktreePath);
      }
      throw err;
    }
  }

  /**
   * Review: runs reviewer agent against the branch diff.
   */
  private async executeReviewJob(
    job: JobRecord,
    signal?: AbortSignal,
  ): Promise<{ exitCode: number; output: string }> {
    const branchName = job.branch_name;
    if (!branchName) {
      throw new Error('Review job requires a branch_name');
    }

    let diff: string;
    try {
      diff = await this.gitManager.getDiff(branchName);
    } catch {
      diff = '(unable to generate diff)';
    }

    const prompt = [
      'You are a code reviewer agent. Review the following branch diff thoroughly.',
      `Branch: ${branchName}`,
      `Task description: ${job.description}`,
      '',
      'Diff:',
      '```',
      diff.slice(0, 50_000), // cap diff size to avoid token overflow
      '```',
      '',
      'Provide a thorough code review covering correctness, security, performance, and style.',
    ].join('\n');

    return this.spawnAgent(job, prompt, this.hearthRepoPath, signal);
  }

  /**
   * Spawn a Claude agent via the orchestrator, streaming output.
   */
  private async spawnAgent(
    job: JobRecord,
    prompt: string,
    cwd: string,
    signal?: AbortSignal,
  ): Promise<{ exitCode: number; output: string }> {
    if (signal?.aborted) {
      throw new Error('Job aborted before agent spawn');
    }

    const outputChunks: string[] = [];

    const result = await this.orchestrator.spawnAgent({
      role: job.role,
      prompt,
      cwd,
      onOutput: (chunk: string) => {
        outputChunks.push(chunk);
      },
    });

    return {
      exitCode: result.exitCode,
      output: result.output || outputChunks.join(''),
    };
  }

  /**
   * Send a notification, swallowing errors to avoid disrupting job execution.
   */
  private async notifySafe(chatId: number, message: string): Promise<void> {
    try {
      await this.botNotifier.notify(chatId, message);
    } catch (err) {
      console.error('[PipelineRunner] notification error:', err);
    }
  }

  /**
   * Safely remove a worktree, logging but not throwing on failure.
   */
  private async safeRemoveWorktree(path: string): Promise<void> {
    try {
      await this.gitManager.removeWorktree(path);
    } catch (err) {
      console.error(`[PipelineRunner] failed to remove worktree ${path}:`, err);
    }
  }

  /**
   * Wait for all active jobs to complete, with a timeout.
   */
  private waitForActiveJobs(timeoutMs: number): Promise<void> {
    return new Promise((resolve) => {
      const start = Date.now();
      const check = () => {
        if (this.activeJobs.size === 0 || Date.now() - start > timeoutMs) {
          if (this.activeJobs.size > 0) {
            console.warn(`[PipelineRunner] timed out waiting for ${this.activeJobs.size} job(s)`);
          }
          resolve();
          return;
        }
        setTimeout(check, 500);
      };
      check();
    });
  }

  /**
   * Register SIGTERM/SIGINT handlers for graceful shutdown.
   */
  private registerSignalHandlers(): void {
    const handler = () => {
      console.log('[PipelineRunner] received shutdown signal');
      this.stop().catch((err) => {
        console.error('[PipelineRunner] error during shutdown:', err);
        process.exit(1);
      });
    };

    for (const signal of ['SIGTERM', 'SIGINT'] as const) {
      const bound = () => handler();
      process.on(signal, bound);
      this.signalHandlers.push({ signal, handler: bound });
    }
  }

  /**
   * Remove signal handlers to prevent leaks on restart.
   */
  private removeSignalHandlers(): void {
    for (const { signal, handler } of this.signalHandlers) {
      process.removeListener(signal, handler);
    }
    this.signalHandlers = [];
  }

  /**
   * Get current pipeline status for monitoring.
   */
  getStatus(): { running: boolean; activeJobs: number; shuttingDown: boolean } {
    return {
      running: this.running,
      activeJobs: this.activeJobs.size,
      shuttingDown: this.shuttingDown,
    };
  }
}
