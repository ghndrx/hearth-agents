import type { PipelineConfig, BotNotifier, OrchestratorInterface } from '../types/index.js';
import { JobQueue } from './job-queue.js';
import { GitManager } from './git-manager.js';
import { PipelineRunner } from './runner.js';

export { JobQueue } from './job-queue.js';
export { GitManager } from './git-manager.js';
export type { WorktreeInfo } from './git-manager.js';
export { PipelineRunner } from './runner.js';

export interface Pipeline {
  jobQueue: JobQueue;
  gitManager: GitManager;
  runner: PipelineRunner;
  /** Start the pipeline tick loop and signal handlers. */
  start(): void;
  /** Stop gracefully, waiting for active agents to drain. */
  stop(): Promise<void>;
}

/**
 * Factory that wires up all pipeline components from a single config object.
 *
 * Usage:
 *   const pipeline = createPipeline(config, orchestrator, notifier);
 *   pipeline.start();
 *   // ... on shutdown:
 *   await pipeline.stop();
 */
export function createPipeline(
  config: PipelineConfig,
  orchestrator: OrchestratorInterface,
  botNotifier: BotNotifier,
): Pipeline {
  const dbPath = config.dbPath ?? './hearth-agents.db';
  const jobQueue = new JobQueue(dbPath);

  const gitManager = new GitManager(
    config.hearthRepoPath,
    config.worktreeBaseDir,
  );

  const runner = new PipelineRunner(
    jobQueue,
    orchestrator,
    botNotifier,
    gitManager,
    config,
  );

  return {
    jobQueue,
    gitManager,
    runner,
    start() {
      runner.start();
    },
    async stop() {
      await runner.stop();
      jobQueue.close();
    },
  };
}
