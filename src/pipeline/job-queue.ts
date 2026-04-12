import Database from 'better-sqlite3';
import type { AgentTask, JobRecord } from '../types/index.js';

const SCHEMA = `
  CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK(role IN ('prd', 'implement', 'review')),
    description   TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'queued'
                          CHECK(status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    chat_id       INTEGER NOT NULL,
    message_id    INTEGER,
    output        TEXT,
    pid           INTEGER,
    branch_name   TEXT,
    prd_path      TEXT,
    worktree_path TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    completed_at  TEXT
  );

  CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
  CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
`;

export class JobQueue {
  private db: Database.Database;
  private stmts: ReturnType<JobQueue['prepareStatements']>;

  constructor(dbPath: string = './hearth-agents.db') {
    this.db = new Database(dbPath);
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('busy_timeout = 5000');
    this.db.pragma('synchronous = NORMAL');
    this.db.pragma('foreign_keys = ON');
    this.db.exec(SCHEMA);
    this.stmts = this.prepareStatements();
  }

  private prepareStatements() {
    return {
      enqueue: this.db.prepare(`
        INSERT INTO jobs (type, role, description, chat_id, message_id, branch_name, prd_path)
        VALUES (?, ?, ?, ?, ?, ?, ?)
      `),

      // Atomic claim: grab the oldest queued job and mark it running in one transaction
      claimSelect: this.db.prepare(`
        SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1
      `),
      claimUpdate: this.db.prepare(`
        UPDATE jobs
        SET status = 'running', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ? AND status = 'queued'
      `),
      claimReturn: this.db.prepare(`
        SELECT * FROM jobs WHERE id = ?
      `),

      complete: this.db.prepare(`
        UPDATE jobs
        SET status = 'done',
            output = ?,
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
      `),

      fail: this.db.prepare(`
        UPDATE jobs
        SET status = 'failed',
            output = ?,
            completed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ?
      `),

      cancel: this.db.prepare(`
        UPDATE jobs
        SET status = 'cancelled',
            updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
        WHERE id = ? AND status IN ('queued', 'running')
      `),

      getActive: this.db.prepare(`
        SELECT * FROM jobs WHERE status = 'running' ORDER BY created_at ASC
      `),

      getRecent: this.db.prepare(`
        SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?
      `),

      get: this.db.prepare(`
        SELECT * FROM jobs WHERE id = ?
      `),

      updatePid: this.db.prepare(`
        UPDATE jobs SET pid = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?
      `),

      updateWorktree: this.db.prepare(`
        UPDATE jobs SET worktree_path = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?
      `),

      cleanup: this.db.prepare(`
        DELETE FROM jobs
        WHERE status IN ('done', 'failed', 'cancelled')
          AND completed_at < ?
      `),
    };
  }

  /**
   * Add a new job to the queue. Returns the created job record.
   */
  enqueue(task: AgentTask): JobRecord {
    const info = this.stmts.enqueue.run(
      task.role,
      task.role,
      task.description,
      task.chatId,
      task.messageId ?? null,
      task.branchName ?? null,
      task.prdPath ?? null,
    );
    return this.get(Number(info.lastInsertRowid))!;
  }

  /**
   * Atomically claim the next queued job. Returns null if queue is empty.
   * Uses a transaction to prevent double-claims under concurrent access.
   */
  claim(): JobRecord | null {
    const claimTxn = this.db.transaction(() => {
      const row = this.stmts.claimSelect.get() as { id: number } | undefined;
      if (!row) return null;

      const changes = this.stmts.claimUpdate.run(row.id);
      if (changes.changes === 0) return null; // lost race

      return this.stmts.claimReturn.get(row.id) as JobRecord;
    });

    return claimTxn();
  }

  /**
   * Mark a job as successfully completed with optional output.
   */
  complete(id: number, output?: string): void {
    this.stmts.complete.run(output ?? null, id);
  }

  /**
   * Mark a job as failed with optional error output.
   */
  fail(id: number, output?: string): void {
    this.stmts.fail.run(output ?? null, id);
  }

  /**
   * Cancel a queued or running job.
   */
  cancel(id: number): boolean {
    const result = this.stmts.cancel.run(id);
    return result.changes > 0;
  }

  /**
   * Get all currently running jobs.
   */
  getActive(): JobRecord[] {
    return this.stmts.getActive.all() as JobRecord[];
  }

  /**
   * Get the N most recent jobs regardless of status.
   */
  getRecent(limit: number = 20): JobRecord[] {
    return this.stmts.getRecent.all(limit) as JobRecord[];
  }

  /**
   * Get a single job by ID. Returns null if not found.
   */
  get(id: number): JobRecord | null {
    return (this.stmts.get.get(id) as JobRecord) ?? null;
  }

  /**
   * Update the PID of a running job (for process tracking).
   */
  updatePid(id: number, pid: number): void {
    this.stmts.updatePid.run(pid, id);
  }

  /**
   * Update the worktree path of a job.
   */
  updateWorktree(id: number, worktreePath: string): void {
    this.stmts.updateWorktree.run(worktreePath, id);
  }

  /**
   * Remove completed/failed/cancelled jobs older than the given date.
   * Pass an ISO 8601 timestamp string.
   */
  cleanup(olderThan: string): number {
    const result = this.stmts.cleanup.run(olderThan);
    return result.changes;
  }

  /**
   * Close the database connection. Call during graceful shutdown.
   */
  close(): void {
    this.db.close();
  }
}
