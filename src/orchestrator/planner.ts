/**
 * Task planning and decomposition using MiniMax M2.7.
 *
 * Converts feature descriptions and PRDs into structured OrchestratorPlan
 * objects with dependency-ordered, complexity-rated implementation steps.
 */

import { randomUUID } from "node:crypto";
import type { MiniMaxClient } from "./minimax-client.js";
import type {
  MiniMaxMessage,
  OrchestratorPlan,
  TaskStep,
  TaskComplexity,
  ToolDefinition,
} from "../types/index.js";

// ── System prompt ──────────────────────────────────────────────────────

const HEARTH_SYSTEM_PROMPT = `You are a senior software architect planning tasks for the Hearth development team.

Hearth is a real-time chat application with the following technology stack:
- **Backend**: Go (Fiber framework), WebSocket for real-time messaging, PostgreSQL (primary datastore), Redis (caching, pub/sub, sessions)
- **Frontend**: SvelteKit (Svelte 5), Tailwind CSS, TypeScript
- **Voice/Video**: LiveKit integration for real-time communication
- **Security**: Signal Protocol for end-to-end encryption (E2EE)
- **Scale**: 170+ backend services, 88+ database models, microservice-oriented architecture

When decomposing tasks:
1. Identify all implementation steps required, including database migrations, API changes, frontend updates, and tests.
2. Assign accurate dependency relationships between steps — a step can only depend on steps that must complete before it.
3. Rate complexity honestly: "trivial" (< 15 min), "low" (15-60 min), "medium" (1-4 hours), "high" (4-16 hours), "critical" (> 16 hours).
4. Assign the most appropriate agent role for each step.
5. Include acceptance criteria for each step so completion is verifiable.
6. Identify risk factors that could delay or complicate the work.
7. List the specific tech stack components involved.

Respond ONLY with valid JSON matching the requested schema. Do not include markdown fences or commentary.`;

// ── JSON schema for structured output ──────────────────────────────────

const PLAN_JSON_SCHEMA: ToolDefinition = {
  type: "function",
  function: {
    name: "submit_plan",
    description: "Submit a structured development plan for a feature or PRD.",
    parameters: {
      type: "object",
      required: ["title", "summary", "steps", "estimatedTotalMinutes", "riskFactors", "techStack"],
      properties: {
        title: { type: "string", description: "Concise plan title" },
        summary: { type: "string", description: "1-3 sentence overview of the plan" },
        steps: {
          type: "array",
          items: {
            type: "object",
            required: [
              "id",
              "title",
              "description",
              "agentRole",
              "complexity",
              "estimatedMinutes",
              "dependencies",
              "tags",
              "acceptanceCriteria",
            ],
            properties: {
              id: { type: "string", description: "Unique step identifier like step-1, step-2" },
              title: { type: "string" },
              description: { type: "string" },
              agentRole: {
                type: "string",
                enum: [
                  "backend",
                  "frontend",
                  "database",
                  "devops",
                  "security",
                  "testing",
                  "documentation",
                  "fullstack",
                ],
              },
              complexity: {
                type: "string",
                enum: ["trivial", "low", "medium", "high", "critical"],
              },
              estimatedMinutes: { type: "number" },
              dependencies: {
                type: "array",
                items: { type: "string" },
                description: "IDs of steps that must complete before this one",
              },
              tags: { type: "array", items: { type: "string" } },
              acceptanceCriteria: { type: "array", items: { type: "string" } },
            },
          },
        },
        estimatedTotalMinutes: { type: "number" },
        riskFactors: { type: "array", items: { type: "string" } },
        techStack: { type: "array", items: { type: "string" } },
      },
    },
  },
};

// ── Planner interface ──────────────────────────────────────────────────

export interface Planner {
  planFeature: (description: string) => Promise<OrchestratorPlan>;
  planFromPRD: (prdContent: string) => Promise<OrchestratorPlan>;
  prioritizeTasks: (plans: OrchestratorPlan[]) => OrchestratorPlan[];
}

/**
 * Create a planner backed by the given MiniMax client.
 */
export function createPlanner(client: MiniMaxClient): Planner {
  return {
    planFeature: (desc) => planFeature(client, desc),
    planFromPRD: (prd) => planFromPRD(client, prd),
    prioritizeTasks,
  };
}

// ── Plan generation ────────────────────────────────────────────────────

async function planFeature(client: MiniMaxClient, description: string): Promise<OrchestratorPlan> {
  const messages: MiniMaxMessage[] = [
    { role: "system", content: HEARTH_SYSTEM_PROMPT },
    {
      role: "user",
      content: `Decompose the following feature into implementable development tasks:\n\n${description}`,
    },
  ];

  return callAndParse(client, messages);
}

async function planFromPRD(client: MiniMaxClient, prdContent: string): Promise<OrchestratorPlan> {
  const messages: MiniMaxMessage[] = [
    { role: "system", content: HEARTH_SYSTEM_PROMPT },
    {
      role: "user",
      content: `Analyze the following PRD and decompose it into a development plan with ordered, dependency-aware tasks:\n\n${prdContent}`,
    },
  ];

  return callAndParse(client, messages);
}

// ── Call MiniMax and parse the structured response ─────────────────────

async function callAndParse(
  client: MiniMaxClient,
  messages: MiniMaxMessage[],
): Promise<OrchestratorPlan> {
  const response = await client.chat(messages, {
    tools: [PLAN_JSON_SCHEMA],
    toolChoice: { type: "function", function: { name: "submit_plan" } },
    temperature: 0.2,
    maxTokens: 8192,
  });

  // Prefer tool call output; fall back to content for models that
  // return structured JSON in the message body instead.
  let raw: string | null = null;

  if (response.toolCalls.length > 0) {
    raw = response.toolCalls[0].function.arguments;
  } else if (response.content) {
    raw = response.content;
  }

  if (!raw) {
    throw new PlannerError("MiniMax returned neither tool calls nor content");
  }

  return parsePlan(raw);
}

function parsePlan(raw: string): OrchestratorPlan {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new PlannerError(`Failed to parse plan JSON: ${raw.slice(0, 200)}...`);
  }

  const plan = parsed as Record<string, unknown>;

  // Validate required top-level fields.
  for (const field of ["title", "summary", "steps", "estimatedTotalMinutes", "riskFactors", "techStack"]) {
    if (!(field in plan)) {
      throw new PlannerError(`Plan missing required field: ${field}`);
    }
  }

  if (!Array.isArray(plan.steps) || plan.steps.length === 0) {
    throw new PlannerError("Plan must contain at least one step");
  }

  // Validate each step has required fields.
  const steps = plan.steps as TaskStep[];
  for (const step of steps) {
    if (!step.id || !step.title || !step.agentRole || !step.complexity) {
      throw new PlannerError(`Step is missing required fields: ${JSON.stringify(step).slice(0, 200)}`);
    }
  }

  // Validate dependency references point to existing step IDs.
  const stepIds = new Set(steps.map((s) => s.id));
  for (const step of steps) {
    for (const dep of step.dependencies) {
      if (!stepIds.has(dep)) {
        throw new PlannerError(
          `Step "${step.id}" depends on unknown step "${dep}"`,
        );
      }
    }
  }

  // Check for circular dependencies.
  detectCycles(steps);

  return {
    id: randomUUID(),
    title: plan.title as string,
    summary: plan.summary as string,
    steps,
    estimatedTotalMinutes: plan.estimatedTotalMinutes as number,
    riskFactors: plan.riskFactors as string[],
    techStack: plan.techStack as string[],
  };
}

// ── Dependency cycle detection (Kahn's algorithm) ──────────────────────

function detectCycles(steps: TaskStep[]): void {
  const inDegree = new Map<string, number>();
  const adjacency = new Map<string, string[]>();

  for (const step of steps) {
    inDegree.set(step.id, 0);
    adjacency.set(step.id, []);
  }

  for (const step of steps) {
    for (const dep of step.dependencies) {
      adjacency.get(dep)!.push(step.id);
      inDegree.set(step.id, (inDegree.get(step.id) ?? 0) + 1);
    }
  }

  const queue: string[] = [];
  for (const [id, degree] of inDegree) {
    if (degree === 0) queue.push(id);
  }

  let processed = 0;
  while (queue.length > 0) {
    const current = queue.shift()!;
    processed++;
    for (const neighbor of adjacency.get(current) ?? []) {
      const newDegree = (inDegree.get(neighbor) ?? 1) - 1;
      inDegree.set(neighbor, newDegree);
      if (newDegree === 0) queue.push(neighbor);
    }
  }

  if (processed !== steps.length) {
    throw new PlannerError("Plan contains circular dependencies between steps");
  }
}

// ── Task prioritization ────────────────────────────────────────────────

const COMPLEXITY_WEIGHT: Record<TaskComplexity, number> = {
  trivial: 1,
  low: 2,
  medium: 4,
  high: 8,
  critical: 16,
};

/**
 * Sort plans so that:
 * 1. Plans with fewer unresolved dependencies come first.
 * 2. Among equal dependency counts, higher complexity plans come first
 *    (tackle hard work early to surface risks sooner).
 * 3. Within each plan, steps are topologically sorted by dependencies.
 */
export function prioritizeTasks(plans: OrchestratorPlan[]): OrchestratorPlan[] {
  return plans
    .map((plan) => ({
      ...plan,
      steps: topologicalSort(plan.steps),
    }))
    .sort((a, b) => {
      const aMaxComplexity = Math.max(...a.steps.map((s) => COMPLEXITY_WEIGHT[s.complexity]));
      const bMaxComplexity = Math.max(...b.steps.map((s) => COMPLEXITY_WEIGHT[s.complexity]));
      // Higher complexity first (descending).
      return bMaxComplexity - aMaxComplexity;
    });
}

function topologicalSort(steps: TaskStep[]): TaskStep[] {
  const inDegree = new Map<string, number>();
  const adjacency = new Map<string, string[]>();
  const stepMap = new Map<string, TaskStep>();

  for (const step of steps) {
    stepMap.set(step.id, step);
    inDegree.set(step.id, 0);
    adjacency.set(step.id, []);
  }

  for (const step of steps) {
    for (const dep of step.dependencies) {
      adjacency.get(dep)!.push(step.id);
      inDegree.set(step.id, (inDegree.get(step.id) ?? 0) + 1);
    }
  }

  // Priority queue: among zero-indegree nodes, prefer higher complexity first.
  const queue: string[] = [];
  for (const [id, degree] of inDegree) {
    if (degree === 0) queue.push(id);
  }
  queue.sort(
    (a, b) =>
      COMPLEXITY_WEIGHT[stepMap.get(b)!.complexity] -
      COMPLEXITY_WEIGHT[stepMap.get(a)!.complexity],
  );

  const sorted: TaskStep[] = [];
  while (queue.length > 0) {
    const current = queue.shift()!;
    sorted.push(stepMap.get(current)!);

    const ready: string[] = [];
    for (const neighbor of adjacency.get(current) ?? []) {
      const newDegree = (inDegree.get(neighbor) ?? 1) - 1;
      inDegree.set(neighbor, newDegree);
      if (newDegree === 0) ready.push(neighbor);
    }
    // Insert newly-ready nodes in complexity-descending order.
    ready.sort(
      (a, b) =>
        COMPLEXITY_WEIGHT[stepMap.get(b)!.complexity] -
        COMPLEXITY_WEIGHT[stepMap.get(a)!.complexity],
    );
    queue.push(...ready);
  }

  return sorted;
}

// ── Error class ────────────────────────────────────────────────────────

export class PlannerError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PlannerError";
  }
}
