// Token budget: tracks spend across providers and enforces per-feature budgets.
// Prevents runaway costs and provides spend visibility.

import { log } from './logger.js';

interface TokenUsage {
  provider: string;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  timestamp: number;
  feature: string;
}

const PRICING: Record<string, { input: number; output: number }> = {
  minimax: { input: 0.30 / 1_000_000, output: 1.20 / 1_000_000 },
  kimi: { input: 0.60 / 1_000_000, output: 2.50 / 1_000_000 },
};

export class TokenBudget {
  private usage: TokenUsage[] = [];
  private dailyBudgetUsd: number;
  private perFeatureBudgetUsd: number;

  constructor(dailyBudgetUsd = 5.0, perFeatureBudgetUsd = 2.0) {
    this.dailyBudgetUsd = dailyBudgetUsd;
    this.perFeatureBudgetUsd = perFeatureBudgetUsd;
  }

  record(provider: string, inputTokens: number, outputTokens: number, feature: string): number {
    const pricing = PRICING[provider] || PRICING.minimax;
    const cost = (inputTokens * pricing.input) + (outputTokens * pricing.output);

    this.usage.push({
      provider,
      inputTokens,
      outputTokens,
      costUsd: cost,
      timestamp: Date.now(),
      feature,
    });

    return cost;
  }

  getDailySpend(): number {
    const dayStart = new Date().setHours(0, 0, 0, 0);
    return this.usage
      .filter(u => u.timestamp >= dayStart)
      .reduce((sum, u) => sum + u.costUsd, 0);
  }

  getFeatureSpend(feature: string): number {
    return this.usage
      .filter(u => u.feature === feature)
      .reduce((sum, u) => sum + u.costUsd, 0);
  }

  canSpend(feature: string): boolean {
    const daily = this.getDailySpend();
    if (daily >= this.dailyBudgetUsd) {
      log.warn('budget', `Daily budget exhausted: $${daily.toFixed(4)}/$${this.dailyBudgetUsd}`);
      return false;
    }

    const featureSpend = this.getFeatureSpend(feature);
    if (featureSpend >= this.perFeatureBudgetUsd) {
      log.warn('budget', `Feature budget exhausted for ${feature}: $${featureSpend.toFixed(4)}/$${this.perFeatureBudgetUsd}`);
      return false;
    }

    return true;
  }

  getStats(): {
    dailySpend: number;
    dailyBudget: number;
    totalTokens: { input: number; output: number };
    byProvider: Record<string, { calls: number; cost: number; tokens: number }>;
    byFeature: Record<string, { cost: number; calls: number }>;
  } {
    const dayStart = new Date().setHours(0, 0, 0, 0);
    const today = this.usage.filter(u => u.timestamp >= dayStart);

    const byProvider: Record<string, { calls: number; cost: number; tokens: number }> = {};
    const byFeature: Record<string, { cost: number; calls: number }> = {};
    let totalInput = 0;
    let totalOutput = 0;

    for (const u of today) {
      if (!byProvider[u.provider]) byProvider[u.provider] = { calls: 0, cost: 0, tokens: 0 };
      byProvider[u.provider].calls++;
      byProvider[u.provider].cost += u.costUsd;
      byProvider[u.provider].tokens += u.inputTokens + u.outputTokens;

      if (!byFeature[u.feature]) byFeature[u.feature] = { cost: 0, calls: 0 };
      byFeature[u.feature].cost += u.costUsd;
      byFeature[u.feature].calls++;

      totalInput += u.inputTokens;
      totalOutput += u.outputTokens;
    }

    return {
      dailySpend: this.getDailySpend(),
      dailyBudget: this.dailyBudgetUsd,
      totalTokens: { input: totalInput, output: totalOutput },
      byProvider,
      byFeature,
    };
  }

  formatForTelegram(): string {
    const stats = this.getStats();
    const pct = Math.round((stats.dailySpend / stats.dailyBudget) * 100);

    let msg = `<b>Token Budget</b>\n`;
    msg += `Daily: $${stats.dailySpend.toFixed(4)} / $${stats.dailyBudget} (${pct}%)\n\n`;

    for (const [provider, data] of Object.entries(stats.byProvider)) {
      msg += `<b>${provider}</b>: ${data.calls} calls, $${data.cost.toFixed(4)}, ${data.tokens} tokens\n`;
    }

    if (Object.keys(stats.byFeature).length > 0) {
      msg += `\n<b>By Feature:</b>\n`;
      for (const [feature, data] of Object.entries(stats.byFeature)) {
        msg += `  ${feature}: $${data.cost.toFixed(4)} (${data.calls} calls)\n`;
      }
    }

    return msg;
  }
}

export const tokenBudget = new TokenBudget(
  parseFloat(process.env.DAILY_BUDGET_USD || '5.0'),
  parseFloat(process.env.PER_FEATURE_BUDGET_USD || '2.0'),
);
