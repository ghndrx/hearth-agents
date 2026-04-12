// Core types for the Hearth multi-agent orchestration system.
// All agents run on MiniMax M2.7 via OpenAI-compatible API.

// -- MiniMax / OpenAI-compatible message types --

export type MiniMaxRole = 'system' | 'user' | 'assistant' | 'tool';

export interface MiniMaxMessage {
  role: MiniMaxRole;
  content: string | null;
  name?: string;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

export interface ToolCall {
  id: string;
  type: 'function';
  function: {
    name: string;
    arguments: string;
  };
}

export interface ToolDefinition {
  type: 'function';
  function: {
    name: string;
    description: string;
    parameters: Record<string, unknown>;
  };
}

export interface ChatOptions {
  model?: string;
  temperature?: number;
  maxTokens?: number;
  tools?: ToolDefinition[];
  toolChoice?: 'auto' | 'none' | 'required' | { type: 'function'; function: { name: string } };
  responseFormat?: { type: 'json_object' };
}

export interface ChatResponse {
  id: string;
  content: string | null;
  toolCalls: ToolCall[];
  finishReason: string | null;
  usage: {
    promptTokens: number;
    completionTokens: number;
    totalTokens: number;
  };
}

// -- Client configuration --

export interface MiniMaxClientConfig {
  apiKey: string;
  baseURL: string;
  model: string;
  mode: 'api' | 'ollama';
}

// -- Agent roles and tasks --

export type AgentRole =
  | 'prd-writer'
  | 'developer'
  | 'reviewer'
  | 'architect'
  | 'backend'
  | 'frontend'
  | 'database'
  | 'devops'
  | 'security'
  | 'testing'
  | 'documentation'
  | 'fullstack';

export type TaskStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

export interface AgentConfig {
  name: string;
  role: AgentRole;
  systemPrompt: string;
  model: string;
  tools: ToolDefinition[];
  maxBudgetUsd: number;
}

export interface AgentTask {
  id: string;
  role: AgentRole;
  status: TaskStatus;
  description: string;
  chatId: number;
  messageId: number | null;
  requestedBy: number;
  output: string[];
  lastOutputLine: string | null;
  createdAt: number;
  startedAt: number | null;
  completedAt: number | null;
  branchName: string | null;
  prdPath: string | null;
  worktreePath: string | null;
  pid: number | null;
}

export interface AgentRunnerEvent {
  type: 'output' | 'tool_call' | 'error' | 'done';
  data: string;
  toolCall?: ToolCall;
  exitCode?: number;
}

// -- Orchestrator plan types --

export type TaskComplexity = 'trivial' | 'low' | 'medium' | 'high' | 'critical';

export interface TaskStep {
  id: string;
  title: string;
  description: string;
  agentRole: AgentRole;
  complexity: TaskComplexity;
  estimatedMinutes: number;
  dependencies: string[];
  tags: string[];
  acceptanceCriteria: string[];
}

export interface OrchestratorPlan {
  id: string;
  title: string;
  summary: string;
  steps: TaskStep[];
  estimatedTotalMinutes: number;
  riskFactors: string[];
  techStack: string[];
}

export interface AgentAssignment {
  stepId: string;
  agentRole: AgentRole;
  rationale: string;
}

// -- Cost estimation --

export interface CostEstimate {
  planId: string;
  inputTokensEstimate: number;
  outputTokensEstimate: number;
  inputCostUSD: number;
  outputCostUSD: number;
  totalCostUSD: number;
  breakdown: StepCostEstimate[];
}

export interface StepCostEstimate {
  stepId: string;
  complexity: TaskComplexity;
  estimatedInputTokens: number;
  estimatedOutputTokens: number;
  estimatedCostUSD: number;
}

// -- Pipeline types --

export interface PipelineConfig {
  maxConcurrentAgents: number;
  hearthRepoPath: string;
  worktreeBaseDir?: string;
  dbPath?: string;
  tickIntervalMs?: number;
}

export interface JobRecord {
  id: number;
  type: string;
  role: string;
  description: string;
  status: string;
  chat_id: number;
  message_id: number | null;
  output: string | null;
  pid: number | null;
  branch_name: string | null;
  prd_path: string | null;
  worktree_path: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

// -- Interface contracts for dependency injection --

export interface BotNotifier {
  notify(chatId: number, message: string): Promise<void>;
}

export interface OrchestratorInterface {
  spawnAgent(opts: {
    role: string;
    prompt: string;
    cwd: string;
    onOutput?: (chunk: string) => void;
  }): Promise<{ exitCode: number; output: string }>;
}
