// GitHub webhook handler for reacting to PR events.
// Verifies signatures, parses events, and queues fix tasks.

import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { createHmac, timingSafeEqual } from 'node:crypto';
import { log } from './logger.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WebhookEventKind =
  | 'review_changes_requested'
  | 'ci_failure'
  | 'fix_command'
  | 'retry_command';

export interface WebhookEvent {
  kind: WebhookEventKind;
  repo: string;
  prNumber: number;
  /** The branch the PR is on (head ref). */
  branch: string;
  /** Human-readable summary of what triggered the event. */
  summary: string;
  /** Raw payload excerpt relevant for the fixer (review body, CI output, etc.). */
  detail: string;
  receivedAt: number;
}

export type WebhookEventHandler = (event: WebhookEvent) => void;

// ---------------------------------------------------------------------------
// Signature verification
// ---------------------------------------------------------------------------

function verifySignature(payload: Buffer, signature: string, secret: string): boolean {
  if (!signature.startsWith('sha256=')) return false;
  const expected = Buffer.from(
    'sha256=' + createHmac('sha256', secret).update(payload).digest('hex'),
    'utf-8',
  );
  const received = Buffer.from(signature, 'utf-8');
  if (expected.length !== received.length) return false;
  return timingSafeEqual(expected, received);
}

// ---------------------------------------------------------------------------
// Payload helpers
// ---------------------------------------------------------------------------

function readBody(req: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let size = 0;
    const MAX_BODY = 5 * 1024 * 1024; // 5 MB hard limit

    req.on('data', (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_BODY) {
        req.destroy();
        reject(new Error('Payload too large'));
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks)));
    req.on('error', reject);
  });
}

function extractRepoFullName(payload: Record<string, unknown>): string {
  const repo = payload.repository as Record<string, unknown> | undefined;
  return (repo?.full_name as string) ?? 'unknown/unknown';
}

// ---------------------------------------------------------------------------
// Event parsers
// ---------------------------------------------------------------------------

function parsePullRequestReview(payload: Record<string, unknown>): WebhookEvent | null {
  const action = payload.action as string | undefined;
  if (action !== 'submitted') return null;

  const review = payload.review as Record<string, unknown> | undefined;
  if (!review) return null;

  const state = (review.state as string)?.toUpperCase();
  if (state !== 'CHANGES_REQUESTED') return null;

  const pr = payload.pull_request as Record<string, unknown>;
  const head = pr.head as Record<string, unknown>;

  return {
    kind: 'review_changes_requested',
    repo: extractRepoFullName(payload),
    prNumber: pr.number as number,
    branch: (head.ref as string) ?? '',
    summary: `Changes requested by ${(review.user as Record<string, unknown>)?.login ?? 'reviewer'}`,
    detail: (review.body as string) ?? '',
    receivedAt: Date.now(),
  };
}

function parseCheckRun(payload: Record<string, unknown>): WebhookEvent | null {
  const action = payload.action as string | undefined;
  if (action !== 'completed') return null;

  const checkRun = payload.check_run as Record<string, unknown> | undefined;
  if (!checkRun) return null;

  const conclusion = checkRun.conclusion as string | undefined;
  if (conclusion !== 'failure') return null;

  // Resolve the PR number from associated pull requests
  const pullRequests = checkRun.pull_requests as Array<Record<string, unknown>> | undefined;
  if (!pullRequests || pullRequests.length === 0) return null;

  const pr = pullRequests[0];
  const head = pr.head as Record<string, unknown>;
  const output = checkRun.output as Record<string, unknown> | undefined;

  return {
    kind: 'ci_failure',
    repo: extractRepoFullName(payload),
    prNumber: pr.number as number,
    branch: (head.ref as string) ?? (head.sha as string) ?? '',
    summary: `CI check "${checkRun.name}" failed`,
    detail: [
      output?.title ?? '',
      output?.summary ?? '',
      // text can be very long; trim to something the fixer can consume
      ((output?.text as string) ?? '').slice(0, 8_000),
    ]
      .filter(Boolean)
      .join('\n\n'),
    receivedAt: Date.now(),
  };
}

function parseIssueComment(payload: Record<string, unknown>): WebhookEvent | null {
  const action = payload.action as string | undefined;
  if (action !== 'created') return null;

  const comment = payload.comment as Record<string, unknown> | undefined;
  if (!comment) return null;

  const body = ((comment.body as string) ?? '').toLowerCase();
  const hasFix = body.includes('/fix');
  const hasRetry = body.includes('/retry');
  if (!hasFix && !hasRetry) return null;

  // issue_comment events for PRs include a pull_request key on the issue
  const issue = payload.issue as Record<string, unknown> | undefined;
  if (!issue) return null;

  const prMeta = issue.pull_request as Record<string, unknown> | undefined;
  if (!prMeta) return null; // comment is on an issue, not a PR

  return {
    kind: hasFix ? 'fix_command' : 'retry_command',
    repo: extractRepoFullName(payload),
    prNumber: issue.number as number,
    branch: '', // Will be resolved by the fixer via GitHub API
    summary: `${hasFix ? '/fix' : '/retry'} command from ${(comment.user as Record<string, unknown>)?.login ?? 'user'}`,
    detail: (comment.body as string) ?? '',
    receivedAt: Date.now(),
  };
}

// ---------------------------------------------------------------------------
// Request handler
// ---------------------------------------------------------------------------

function createRequestHandler(secret: string, onEvent: WebhookEventHandler) {
  return async function handleRequest(req: IncomingMessage, res: ServerResponse): Promise<void> {
    // Health check
    if (req.method === 'GET' && req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ status: 'ok', uptime: process.uptime() }));
      return;
    }

    // Only accept POST to the webhook path
    if (req.method !== 'POST' || (req.url !== '/' && req.url !== '/webhook')) {
      res.writeHead(404);
      res.end('Not found');
      return;
    }

    let rawBody: Buffer;
    try {
      rawBody = await readBody(req);
    } catch (err) {
      log.warn('webhook', 'Failed to read request body', {
        error: err instanceof Error ? err.message : String(err),
      });
      res.writeHead(400);
      res.end('Bad request');
      return;
    }

    // Verify HMAC signature
    const signature = req.headers['x-hub-signature-256'] as string | undefined;
    if (!signature || !verifySignature(rawBody, signature, secret)) {
      log.warn('webhook', 'Invalid or missing webhook signature');
      res.writeHead(401);
      res.end('Unauthorized');
      return;
    }

    // Parse body
    let payload: Record<string, unknown>;
    try {
      payload = JSON.parse(rawBody.toString('utf-8'));
    } catch {
      res.writeHead(400);
      res.end('Invalid JSON');
      return;
    }

    const ghEvent = req.headers['x-github-event'] as string | undefined;
    log.info('webhook', `Received event: ${ghEvent}`, {
      repo: extractRepoFullName(payload),
      action: payload.action as string,
    });

    let event: WebhookEvent | null = null;

    switch (ghEvent) {
      case 'pull_request_review':
        event = parsePullRequestReview(payload);
        break;
      case 'check_run':
        event = parseCheckRun(payload);
        break;
      case 'issue_comment':
        event = parseIssueComment(payload);
        break;
      default:
        // Acknowledge but ignore unhandled events
        break;
    }

    if (event) {
      log.info('webhook', `Queuing task: ${event.kind}`, {
        repo: event.repo,
        pr: event.prNumber,
        summary: event.summary,
      });
      try {
        onEvent(event);
      } catch (err) {
        log.error('webhook', 'Event handler threw', {
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }

    // Always return 200 to GitHub promptly
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ received: true }));
  };
}

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------

export interface WebhookServerOptions {
  /** Port to listen on. Defaults to WEBHOOK_PORT env or 9091. */
  port?: number;
  /** HMAC secret for verifying payloads. Defaults to GITHUB_WEBHOOK_SECRET env. */
  secret?: string;
  /** Callback invoked for every actionable webhook event. */
  onEvent: WebhookEventHandler;
}

export function startWebhookServer(options: WebhookServerOptions): {
  server: ReturnType<typeof createServer>;
  close: () => Promise<void>;
} {
  const port = options.port ?? (Number(process.env.WEBHOOK_PORT) || 9091);
  const secret = options.secret ?? process.env.GITHUB_WEBHOOK_SECRET;

  if (!secret) {
    throw new Error(
      'GITHUB_WEBHOOK_SECRET must be set (env var or options.secret). ' +
      'Refusing to start webhook server without signature verification.',
    );
  }

  const handler = createRequestHandler(secret, options.onEvent);

  const server = createServer((req, res) => {
    handler(req, res).catch((err) => {
      log.error('webhook', 'Unhandled error in request handler', {
        error: err instanceof Error ? err.message : String(err),
      });
      if (!res.headersSent) {
        res.writeHead(500);
        res.end('Internal server error');
      }
    });
  });

  server.listen(port, () => {
    log.info('webhook', `GitHub webhook server listening on port ${port}`);
  });

  const close = (): Promise<void> =>
    new Promise((resolve, reject) => {
      server.close((err) => (err ? reject(err) : resolve()));
    });

  return { server, close };
}
