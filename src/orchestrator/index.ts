/**
 * Main orchestrator — wraps the MiniMax client and planner into a
 * single entry point for feature decomposition, agent selection,
 * and cost estimation.
 */

import { createMiniMaxClient, type MiniMaxClient } from "./minimax-client.js";
import { createPlanner, type Planner } from "./planner.js";
import type {
  OrchestratorPlan,
  AgentRole,
  AgentAssignment,
  CostEstimate,
  StepCostEstimate,
  TaskComplexity,
  MiniMaxClientConfig,
} from "../types/index.js";

// ── Cost constants (MiniMax M2.7 pricing) ──────────────────────────────

/** USD per 1 million input tokens. */
const INPUT_COST_PER_M = 0.30;
/** USD per 1 million output tokens. */
const OUTPUT_COST_PER_M = 1.20;

/**
 * Rough token estimates per complexity tier.
 * These represent the expected MiniMax tokens consumed when an agent
 * works on a step of the given complexity (input + output combined).
 */
const TOKEN_ESTIMATES: Record<TaskComplexity, { input: number; output: number }> = {
  trivial: { input: 500, output: 200 },
  low: { input: 1_500, output: 800 },
  medium: { input: 4_000, output: 2_500 },
  high: { input: 10_000, output: 6_000 },
  critical: { input: 25_000, output: 15_000 },
};

// ── Agent role descriptions (used for rationale) ───────────────────────

const ROLE_DESCRIPTIONS: Partial<Record<AgentRole, string>> = {
  backend: "Go services, Fiber handlers, WebSocket logic, Redis/PostgreSQL integration",
  frontend: "SvelteKit pages/components, Svelte 5 reactivity, Tailwind styling",
  database: "PostgreSQL migrations, schema changes, query optimization, Redis data structures",
  devops: "CI/CD pipelines, Docker, deployment configuration, monitoring",
  security: "Signal Protocol E2EE, authentication, authorization, vulnerability remediation",
  testing: "Unit tests, integration tests, E2E tests, load testing",
  documentation: "API docs, architecture decision records, developer guides",
  fullstack: "Cross-cutting changes spanning backend and frontend",
};

// ── Orchestrator class ─────────────────────────────────────────────────

export class Orchestrator {
  private readonly client: MiniMaxClient;
  private readonly planner: Planner;

  constructor(configOverrides?: Partial<MiniMaxClientConfig>) {
    this.client = createMiniMaxClient(configOverrides);
    this.planner = createPlanner(this.client);
  }

  /**
   * Decompose a feature description into an ordered development plan.
   */
  async decompose(feature: string): Promise<OrchestratorPlan> {
    return this.planner.planFeature(feature);
  }

  /**
   * Decompose a full PRD into an ordered development plan.
   */
  async decomposeFromPRD(prdContent: string): Promise<OrchestratorPlan> {
    return this.planner.planFromPRD(prdContent);
  }

  /**
   * Prioritize multiple plans by dependency depth and complexity.
   */
  prioritize(plans: OrchestratorPlan[]): OrchestratorPlan[] {
    return this.planner.prioritizeTasks(plans);
  }

  /**
   * Decide which Claude agent role is best suited for each step in a plan.
   *
   * The planner already assigns an agentRole per step; this method validates
   * those assignments and provides a rationale for each.
   */
  selectAgents(plan: OrchestratorPlan): AgentAssignment[] {
    return plan.steps.map((step) => {
      const role = resolveAgentRole(step.agentRole, step.tags);
      return {
        stepId: step.id,
        agentRole: role,
        rationale: buildRationale(role, step.title, step.tags),
      };
    });
  }

  /**
   * Produce a rough cost estimate for executing a plan through MiniMax.
   *
   * This accounts for the orchestration tokens only (planning calls,
   * agent coordination). The cost of the Claude agents themselves
   * is not included here.
   */
  estimateCost(plan: OrchestratorPlan): CostEstimate {
    const breakdown: StepCostEstimate[] = plan.steps.map((step) => {
      const tokens = TOKEN_ESTIMATES[step.complexity];
      const inputCost = (tokens.input / 1_000_000) * INPUT_COST_PER_M;
      const outputCost = (tokens.output / 1_000_000) * OUTPUT_COST_PER_M;

      return {
        stepId: step.id,
        complexity: step.complexity,
        estimatedInputTokens: tokens.input,
        estimatedOutputTokens: tokens.output,
        estimatedCostUSD: roundTo6(inputCost + outputCost),
      };
    });

    const totals = breakdown.reduce(
      (acc, item) => {
        acc.inputTokens += item.estimatedInputTokens;
        acc.outputTokens += item.estimatedOutputTokens;
        acc.cost += item.estimatedCostUSD;
        return acc;
      },
      { inputTokens: 0, outputTokens: 0, cost: 0 },
    );

    return {
      planId: plan.id,
      inputTokensEstimate: totals.inputTokens,
      outputTokensEstimate: totals.outputTokens,
      inputCostUSD: roundTo6((totals.inputTokens / 1_000_000) * INPUT_COST_PER_M),
      outputCostUSD: roundTo6((totals.outputTokens / 1_000_000) * OUTPUT_COST_PER_M),
      totalCostUSD: roundTo6(totals.cost),
      breakdown,
    };
  }
}

// ── Agent role resolution ──────────────────────────────────────────────

/**
 * Refine the agent role assignment based on step tags.
 *
 * For example, a step tagged with both "go" and "svelte" should be
 * assigned to the fullstack role even if the planner said "backend".
 */
function resolveAgentRole(proposed: AgentRole, tags: string[]): AgentRole {
  const tagSet = new Set(tags.map((t) => t.toLowerCase()));

  const hasBackend = tagSet.has("go") || tagSet.has("fiber") || tagSet.has("api") || tagSet.has("websocket");
  const hasFrontend = tagSet.has("svelte") || tagSet.has("sveltekit") || tagSet.has("tailwind") || tagSet.has("ui");
  const hasSecurity = tagSet.has("e2ee") || tagSet.has("signal") || tagSet.has("encryption") || tagSet.has("auth");
  const hasDB = tagSet.has("postgresql") || tagSet.has("postgres") || tagSet.has("redis") || tagSet.has("migration");
  const hasTest = tagSet.has("test") || tagSet.has("testing") || tagSet.has("e2e");

  // Cross-cutting: both backend and frontend tags present.
  if (hasBackend && hasFrontend) return "fullstack";

  // Security takes priority when encryption/auth tags are present.
  if (hasSecurity) return "security";

  // If tags strongly suggest a different role than proposed, override.
  if (hasDB && proposed !== "database") return "database";
  if (hasTest && proposed !== "testing") return "testing";

  return proposed;
}

function buildRationale(role: AgentRole, stepTitle: string, tags: string[]): string {
  const desc = ROLE_DESCRIPTIONS[role] ?? role;
  const tagStr = tags.length > 0 ? ` (tags: ${tags.join(", ")})` : "";
  return `"${stepTitle}" assigned to ${role} agent — ${desc}${tagStr}`;
}

// ── Utilities ──────────────────────────────────────────────────────────

function roundTo6(n: number): number {
  return Math.round(n * 1_000_000) / 1_000_000;
}

// ── Re-exports ─────────────────────────────────────────────────────────

export { createMiniMaxClient, MiniMaxClientError } from "./minimax-client.js";
export type { MiniMaxClient } from "./minimax-client.js";
export { createPlanner, PlannerError } from "./planner.js";
export type { Planner } from "./planner.js";
