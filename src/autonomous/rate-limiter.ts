// Rate limiter for MiniMax token plan.
// Tracks requests per 5-hour window and backs off when approaching limits.

const WINDOW_MS = 5 * 60 * 60 * 1000; // 5 hours
const DEFAULT_LIMIT = 4500; // Plus plan
const SAFETY_MARGIN = 0.95; // Stop at 95% - run hot, we have room

export class RateLimiter {
  private requests: number[] = [];
  private limit: number;
  private effectiveLimit: number;

  constructor(limit = DEFAULT_LIMIT) {
    this.limit = limit;
    this.effectiveLimit = Math.floor(limit * SAFETY_MARGIN);
  }

  /** Record a request. */
  track(): void {
    this.requests.push(Date.now());
    this.prune();
  }

  /** Check if we can make another request. */
  canProceed(): boolean {
    this.prune();
    return this.requests.length < this.effectiveLimit;
  }

  /** Get time in ms until we can make another request. */
  getWaitTime(): number {
    this.prune();
    if (this.requests.length < this.effectiveLimit) return 0;

    // Wait until oldest request falls out of window
    const oldest = this.requests[0];
    if (!oldest) return 0;
    return Math.max(0, (oldest + WINDOW_MS) - Date.now() + 1000);
  }

  /** Wait until rate limit allows another request. */
  async waitForCapacity(): Promise<void> {
    const wait = this.getWaitTime();
    if (wait > 0) {
      const minutes = Math.ceil(wait / 60_000);
      console.log(`[rate-limiter] At ${this.requests.length}/${this.effectiveLimit} requests. Waiting ${minutes}m for capacity...`);
      await new Promise(r => setTimeout(r, wait));
    }
  }

  /** Get current usage stats. */
  getStats(): { used: number; limit: number; effectiveLimit: number; windowResetMs: number } {
    this.prune();
    const oldest = this.requests[0];
    const windowResetMs = oldest ? Math.max(0, (oldest + WINDOW_MS) - Date.now()) : 0;
    return {
      used: this.requests.length,
      limit: this.limit,
      effectiveLimit: this.effectiveLimit,
      windowResetMs,
    };
  }

  private prune(): void {
    const cutoff = Date.now() - WINDOW_MS;
    while (this.requests.length > 0 && this.requests[0] < cutoff) {
      this.requests.shift();
    }
  }
}

// Singleton for the app
export const rateLimiter = new RateLimiter(
  parseInt(process.env.MINIMAX_RATE_LIMIT || '4500', 10),
);
