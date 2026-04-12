// Circuit breaker: auto-fallback between MiniMax and Kimi when one provider fails.
// Prevents cascading failures and maximizes uptime.

import { log } from './logger.js';

type BreakerState = 'closed' | 'open' | 'half-open';

export class CircuitBreaker {
  private state: BreakerState = 'closed';
  private failures = 0;
  private lastFailure = 0;
  private lastSuccess = 0;
  private readonly threshold: number;
  private readonly resetTimeMs: number;
  private readonly provider: string;

  constructor(provider: string, threshold = 5, resetTimeMs = 120_000) {
    this.provider = provider;
    this.threshold = threshold;
    this.resetTimeMs = resetTimeMs;
  }

  canExecute(): boolean {
    if (this.state === 'closed') return true;

    if (this.state === 'open') {
      if (Date.now() - this.lastFailure > this.resetTimeMs) {
        this.state = 'half-open';
        log.info('circuit-breaker', `${this.provider} transitioning to half-open`);
        return true;
      }
      return false;
    }

    // half-open: allow one request to test
    return true;
  }

  recordSuccess(): void {
    if (this.state === 'half-open') {
      log.info('circuit-breaker', `${this.provider} recovered, closing circuit`);
    }
    this.state = 'closed';
    this.failures = 0;
    this.lastSuccess = Date.now();
  }

  recordFailure(): void {
    this.failures++;
    this.lastFailure = Date.now();

    if (this.failures >= this.threshold) {
      this.state = 'open';
      log.warn('circuit-breaker', `${this.provider} circuit OPEN after ${this.failures} failures, cooldown ${this.resetTimeMs / 1000}s`);
    }
  }

  getState(): { provider: string; state: BreakerState; failures: number } {
    return { provider: this.provider, state: this.state, failures: this.failures };
  }
}

// Provider failover: if primary fails, try the other
export class ProviderFailover {
  private breakers = new Map<string, CircuitBreaker>();

  constructor(providers: string[]) {
    for (const p of providers) {
      this.breakers.set(p, new CircuitBreaker(p));
    }
  }

  getAvailableProvider(preferred: string): string {
    const preferredBreaker = this.breakers.get(preferred);
    if (preferredBreaker?.canExecute()) return preferred;

    // Fallback to any available provider
    for (const [name, breaker] of this.breakers) {
      if (name !== preferred && breaker.canExecute()) {
        log.info('failover', `${preferred} unavailable, falling back to ${name}`);
        return name;
      }
    }

    // All providers down - try preferred anyway (might recover)
    log.warn('failover', `All providers down, forcing ${preferred}`);
    return preferred;
  }

  recordSuccess(provider: string): void {
    this.breakers.get(provider)?.recordSuccess();
  }

  recordFailure(provider: string): void {
    this.breakers.get(provider)?.recordFailure();
  }

  getStatus(): Record<string, { state: string; failures: number }> {
    const result: Record<string, any> = {};
    for (const [name, breaker] of this.breakers) {
      result[name] = breaker.getState();
    }
    return result;
  }
}

export const providerFailover = new ProviderFailover(['minimax', 'kimi', 'openrouter']);
