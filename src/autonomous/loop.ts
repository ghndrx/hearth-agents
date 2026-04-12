// Autonomous development loop.
// Picks features from the backlog, researches them, writes PRDs,
// implements them, and sends updates to Telegram.

import { resolve, join } from 'node:path';
import { execFile as execFileCb } from 'node:child_process';
import { promisify } from 'node:util';
import { createMiniMaxClient } from '../orchestrator/minimax-client.js';
import { log } from './logger.js';
import { runHealthCheck, apiHealth, healGitState } from './self-healer.js';
import { TelegramNotifier } from './notifier.js';
import { FEATURE_BACKLOG, getNextFeature, updateFeatureStatus } from './feature-backlog.js';
import { rateLimiter } from './rate-limiter.js';
import { researchFeature } from './researcher.js';
import { generatePRD } from './prd-generator.js';
import { implementFeature } from './implementer.js';
import {
  saveResearch,
  savePRDSummary,
  saveImplementationNotes,
  generateNextSteps,
  getKnowledgeSummary,
} from './knowledge-base.js';
import { refillBacklog } from './backlog-generator.js';
import { generateResearchIdeas, saveIdeasToKB, formatIdeasForTelegram } from './idea-engine.js';
import type { Feature } from './feature-backlog.js';
import type { ImplementationResult } from './implementer.js';

const execFile = promisify(execFileCb);

const REPOS: Record<string, string> = {
  hearth: resolve(process.env.HEARTH_REPO_PATH || '../hearth'),
  'hearth-desktop': resolve(process.env.HEARTH_DESKTOP_PATH || '../hearth-desktop'),
  'hearth-mobile': resolve(process.env.HEARTH_MOBILE_PATH || '../hearth-mobile'),
};

const UPDATE_INTERVAL_MS = 30 * 60 * 1000; // 45 minutes between progress updates

export class AutonomousLoop {
  private client = createMiniMaxClient();
  private notifier: TelegramNotifier;
  private running = false;
  private currentFeature: Feature | null = null;
  private currentPhase = 'idle';
  private recentActivity: string[] = [];
  private updateTimer: ReturnType<typeof setInterval> | null = null;
  private featuresCompleted = 0;

  constructor(telegramToken: string, chatId: number) {
    this.notifier = new TelegramNotifier(telegramToken, chatId);
  }

  async start(): Promise<void> {
    this.running = true;
    console.log('[autonomous] Starting autonomous development loop');

    await this.notifier.sendStartup();

    // Skip idea engine on startup - go straight to processing features
    // Idea engine runs after every 3rd feature instead
    log.info('loop', 'Skipping idea engine on startup, processing features immediately');

    // Periodic progress updates
    this.updateTimer = setInterval(() => {
      this.sendProgressUpdate().catch(console.error);
    }, UPDATE_INTERVAL_MS);

    // Main loop - process features aggressively
    while (this.running) {
      // Health check before each cycle
      const health = await runHealthCheck(Object.values(REPOS));
      if (health.fixes.length > 0) {
        log.info('loop', `Self-healed ${health.fixes.length} issues`, { fixes: health.fixes });
        await this.notifier.send(
          `<b>Self-Healed</b>\n${health.fixes.map(f => `- ${f}`).join('\n')}`
        ).catch(() => {});
      }
      if (health.issues.length > 0) {
        log.warn('loop', `${health.issues.length} health issues`, { issues: health.issues });
      }

      const features: Feature[] = [];
      for (let i = 0; i < 3; i++) {
        const f = getNextFeature();
        if (f) {
          updateFeatureStatus(f.id, 'researching');
          features.push(f);
        }
      }

      if (features.length === 0) {
        console.log('[autonomous] All features complete!');
        await this.notifier.send('<b>All features in backlog have been processed!</b>');
        break;
      }

      this.currentFeature = features[0];
      console.log(`[autonomous] Starting ${features.length} feature(s): ${features.map(f => f.name).join(', ')}`);

      // Process features concurrently
      const results = await Promise.allSettled(
        features.map(feature => this.processFeature(feature))
      );

      for (let i = 0; i < results.length; i++) {
        const result = results[i];
        const feature = features[i];
        if (result.status === 'rejected') {
          const msg = result.reason instanceof Error ? result.reason.message : String(result.reason);
          console.error(`[autonomous] Feature ${feature.id} failed:`, msg);
          await this.notifier.sendError(`Feature: ${feature.name}`, msg);
          updateFeatureStatus(feature.id, 'pending');
        }
      }
    }

    this.cleanup();
  }

  stop(): void {
    console.log('[autonomous] Stopping...');
    this.running = false;
    this.cleanup();
  }

  private cleanup(): void {
    if (this.updateTimer) {
      clearInterval(this.updateTimer);
      this.updateTimer = null;
    }
  }

  private async processFeature(feature: Feature): Promise<void> {
    // Phase 1: Research
    this.currentPhase = 'researching';
    updateFeatureStatus(feature.id, 'researching');
    this.logActivity(`Research started: ${feature.name}`);
    await this.notifier.sendResearchStarted(feature.name, feature.researchTopics.length);

    console.log(`[autonomous] Researching ${feature.researchTopics.length} topics...`);
    const research = await researchFeature(this.client, feature);
    this.logActivity(`Research complete: ${feature.name} (${feature.researchTopics.length} topics)`);

    // Save research to knowledge base
    await saveResearch(feature, research);
    console.log(`[autonomous] Research saved to knowledge base`);

    // Phase 2: Generate PRD
    this.currentPhase = 'prd';
    updateFeatureStatus(feature.id, 'prd');
    console.log(`[autonomous] Generating PRD for ${feature.name}...`);

    // PRDs stay in hearth-agents knowledge base, NOT pushed to product repos
    const kbRoot = join(process.cwd(), 'knowledge', 'prds');
    const prd = await generatePRD(this.client, feature, research, kbRoot);
    this.logActivity(`PRD created: ${prd.filename}`);
    await this.notifier.sendPRDCreated(feature.name, prd.filename);

    // Save PRD summary to knowledge base
    await savePRDSummary(feature, prd);

    // Phase 3: Implementation (for each target repo)
    this.currentPhase = 'implementing';
    updateFeatureStatus(feature.id, 'implementing');
    const implResults: ImplementationResult[] = [];

    for (const repoName of feature.repos) {
      const repoPath = REPOS[repoName];
      if (!repoPath) {
        console.warn(`[autonomous] Unknown repo: ${repoName}`);
        continue;
      }

      const branch = `feat/${feature.id}`;
      this.logActivity(`Implementation started: ${repoName}/${branch}`);
      await this.notifier.sendImplementationStarted(feature.name, repoName, branch);

      console.log(`[autonomous] Implementing in ${repoName}...`);
      const result = await implementFeature(feature, prd, repoPath, repoName);
      implResults.push(result);

      if (result.success && result.filesChanged.length > 0) {
        this.logActivity(`Implementation done: ${repoName} (${result.filesChanged.length} files)`);
        await this.notifier.sendImplementationDone(
          feature.name,
          repoName,
          branch,
          result.filesChanged.length,
        );

        // Push branch and create PR only if actual files were changed
        try {
          await this.pushBranchAndCreatePR(repoPath, repoName, branch, feature);
        } catch (err) {
          console.error(`[autonomous] PR creation failed for ${repoName}:`, err);
          await this.notifier.sendError(`PR creation: ${repoName}`, String(err));
        }
      } else if (result.success && result.filesChanged.length === 0) {
        this.logActivity(`Implementation produced no changes: ${repoName}`);
        log.warn('loop', `Feature ${feature.name} produced 0 file changes in ${repoName}`);
      } else {
        this.logActivity(`Implementation failed: ${repoName}`);
        await this.notifier.sendError(
          `Implementation: ${feature.name} in ${repoName}`,
          result.output.slice(-500),
        );
      }
    }

    // Save implementation notes to knowledge base
    await saveImplementationNotes(feature, implResults);

    // Phase 4: Done - update knowledge base with next steps
    updateFeatureStatus(feature.id, 'done');
    this.currentPhase = 'updating knowledge base';

    const completed = FEATURE_BACKLOG.filter(f => f.status === 'done');
    const remaining = FEATURE_BACKLOG.filter(f => f.status === 'pending');
    await generateNextSteps(completed, remaining);

    // Send KB summary to Telegram
    const kbSummary = await getKnowledgeSummary();
    await this.notifier.send(
      `<b>Knowledge Base Updated</b>\n\n<pre>${kbSummary}</pre>`
    );

    // Auto-generate more features when backlog runs low
    const added = await refillBacklog(this.client);
    if (added > 0) {
      this.logActivity(`Backlog refilled: ${added} new features generated`);
      await this.notifier.send(
        `<b>Backlog Auto-Refilled</b>\n\n` +
        `Added ${added} new features to the queue.\n` +
        `The agents will never run out of work.`
      );
    }

    // Run idea engine every 3 features to discover new research directions
    this.featuresCompleted++;
    if (this.featuresCompleted % 3 === 0) {
      await this.runIdeaEngine();
    }

    this.currentPhase = 'idle';
    this.logActivity(`Feature complete: ${feature.name}`);
    console.log(`[autonomous] Feature ${feature.name} complete!`);
  }

  private async runIdeaEngine(): Promise<void> {
    try {
      this.currentPhase = 'generating ideas';
      console.log('[autonomous] Running idea engine...');

      const report = await generateResearchIdeas(this.client);
      const filepath = await saveIdeasToKB(report);

      this.logActivity(`Idea engine: ${report.ideas.length} research ideas generated`);
      console.log(`[autonomous] ${report.ideas.length} ideas saved to ${filepath}`);

      // Send top ideas to Telegram
      if (report.ideas.length > 0) {
        await this.notifier.send(formatIdeasForTelegram(report, 5));
      }
    } catch (err) {
      console.error('[autonomous] Idea engine failed:', err);
    }
  }

  private async gitCommitAndPush(
    repoPath: string,
    repoName: string,
    message: string,
    paths: string[],
  ): Promise<void> {
    try {
      for (const p of paths) {
        await execFile('git', ['add', p], { cwd: repoPath });
      }
      await execFile('git', ['commit', '-m', message], { cwd: repoPath });
      await execFile('git', ['push', 'origin', 'HEAD'], { cwd: repoPath, timeout: 30_000 });
    } catch (err) {
      console.warn(`[autonomous] Git commit/push failed in ${repoName}:`, err);
    }
  }

  private async pushBranchAndCreatePR(
    repoPath: string,
    repoName: string,
    branch: string,
    feature: Feature,
  ): Promise<void> {
    // Push the branch
    await execFile('git', ['push', '-u', 'origin', branch], {
      cwd: repoPath,
      timeout: 60_000,
    });

    // Detect default branch (develop or main or master)
    let baseBranch = 'develop';
    try {
      const { stdout: defaultBranch } = await execFile(
        'gh', ['repo', 'view', '--json', 'defaultBranchRef', '-q', '.defaultBranchRef.name'],
        { cwd: repoPath, timeout: 10_000 },
      );
      baseBranch = defaultBranch.trim() || 'develop';
    } catch {
      // fallback to develop
    }

    const prTitle = `feat: ${feature.name}`.slice(0, 70);
    const prBody = [
      'Summary:',
      feature.description.slice(0, 500),
      '',
      `Discord parity: ${feature.discordParity}`,
    ].join('\n');

    const { stdout } = await execFile(
      'gh',
      ['pr', 'create', '--title', prTitle, '--body', prBody, '--base', baseBranch],
      { cwd: repoPath, timeout: 30_000 },
    );

    const prUrl = stdout.trim();
    this.logActivity(`PR created: ${repoName} - ${prUrl}`);
    await this.notifier.sendPRCreated(repoName, prUrl, prTitle);
  }

  private logActivity(message: string): void {
    const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });
    this.recentActivity.unshift(`[${timestamp}] ${message}`);
    if (this.recentActivity.length > 10) this.recentActivity.pop();
  }

  private async sendProgressUpdate(): Promise<void> {
    const completed = FEATURE_BACKLOG.filter(f => f.status === 'done').length;
    const total = FEATURE_BACKLOG.length;
    const stats = rateLimiter.getStats();
    const resetMin = Math.ceil(stats.windowResetMs / 60_000);

    const activity = [
      ...this.recentActivity.slice(0, 5),
      `API: ${stats.used}/${stats.limit} requests (resets in ${resetMin}m)`,
    ];

    await this.notifier.sendProgressUpdate(
      this.currentFeature?.name || 'idle',
      this.currentPhase,
      completed,
      total,
      activity,
    );
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise(r => setTimeout(r, ms));
}
