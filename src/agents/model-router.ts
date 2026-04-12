// Model router: routes agent roles to the best model.
// MiniMax M2.7 for planning/research (cheap, good at decomposition)
// Kimi K2.5 for implementation/review (76.8% SWE-Bench, better code)

import OpenAI from 'openai';
import type { AgentRole } from '../types/index.js';
import { providerFailover } from '../autonomous/circuit-breaker.js';

export type ModelProvider = 'minimax' | 'kimi' | 'openrouter';

export interface ModelConfig {
  provider: ModelProvider;
  model: string;
  client: OpenAI;
}

// Role -> model mapping
const ROLE_MODEL_MAP: Record<string, ModelProvider> = {
  // MiniMax M2.7: planning, PRDs, architecture, documentation
  'prd-writer': 'minimax',
  'architect': 'minimax',
  'documentation': 'minimax',

  // Kimi K2.5: ALL code-writing roles (76.8% SWE-Bench)
  'developer': 'kimi',
  'backend': 'kimi',
  'frontend': 'kimi',
  'security': 'kimi',
  'testing': 'kimi',
  'reviewer': 'kimi',
  'fullstack': 'kimi',
  'database': 'kimi',
  'devops': 'kimi',
};

let minimaxClient: OpenAI | null = null;
let kimiClient: OpenAI | null = null;
let openrouterClient: OpenAI | null = null;

function getMiniMaxClient(): OpenAI {
  if (!minimaxClient) {
    const apiKey = process.env.MINIMAX_API_KEY;
    if (!apiKey) throw new Error('MINIMAX_API_KEY is required');
    minimaxClient = new OpenAI({
      apiKey,
      baseURL: process.env.MINIMAX_BASE_URL || 'https://api.minimax.io/v1',
    });
  }
  return minimaxClient;
}

function getKimiClient(): OpenAI {
  if (!kimiClient) {
    const apiKey = process.env.KIMI_API_KEY;
    if (!apiKey) throw new Error('KIMI_API_KEY is required');

    // sk-kimi- prefixed keys use the coding endpoint at api.kimi.com
    // and require a coding agent user-agent header
    const isKimiCodingKey = apiKey.startsWith('sk-kimi-');
    const baseURL = process.env.KIMI_BASE_URL ||
      (isKimiCodingKey ? 'https://api.kimi.com/coding/v1' : 'https://api.moonshot.ai/v1');

    kimiClient = new OpenAI({
      apiKey,
      baseURL,
      defaultHeaders: isKimiCodingKey
        ? { 'User-Agent': 'claude-code/1.0.0' }
        : undefined,
    });
  }
  return kimiClient;
}

function getOpenRouterClient(): OpenAI | null {
  if (!openrouterClient) {
    const apiKey = process.env.OPENROUTER_API_KEY;
    if (!apiKey) return null;
    openrouterClient = new OpenAI({
      apiKey,
      baseURL: 'https://openrouter.ai/api/v1',
      defaultHeaders: {
        'HTTP-Referer': 'https://github.com/ghndrx/hearth-agents',
      },
    });
  }
  return openrouterClient;
}

export function getModelForRole(role: AgentRole | string): ModelConfig {
  const preferred = ROLE_MODEL_MAP[role] || 'minimax';
  // Circuit breaker: if preferred provider is down, failover to the other
  const provider = providerFailover.getAvailableProvider(preferred) as ModelProvider;

  if (provider === 'openrouter') {
    const client = getOpenRouterClient();
    if (client) {
      // Map the preferred provider's role to the equivalent OpenRouter model
      const model = preferred === 'kimi'
        ? 'moonshotai/kimi-k2.5'
        : 'minimax/minimax-m2.7';
      return { provider: 'openrouter', model, client };
    }
    // OpenRouter not configured, fall through to preferred provider
  }

  if (provider === 'kimi') {
    const apiKey = process.env.KIMI_API_KEY || '';
    const model = apiKey.startsWith('sk-kimi-') ? 'kimi-for-coding' : 'kimi-k2.5';
    return {
      provider: 'kimi',
      model,
      client: getKimiClient(),
    };
  }

  return {
    provider: 'minimax',
    model: 'MiniMax-M2.7',
    client: getMiniMaxClient(),
  };
}

export function getResearchClient(): ModelConfig {
  return {
    provider: 'minimax',
    model: 'MiniMax-M2.7',
    client: getMiniMaxClient(),
  };
}

export function getImplementationClient(): ModelConfig {
  return {
    provider: 'kimi',
    model: 'kimi-k2.5',
    client: getKimiClient(),
  };
}
