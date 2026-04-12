/**
 * MiniMax M2.7 API client using the OpenAI-compatible interface.
 *
 * Supports two modes:
 *   - API mode: MINIMAX_API_KEY + MINIMAX_BASE_URL (default: https://api.minimax.chat/v1)
 *   - Ollama local mode: OLLAMA_BASE_URL with model "minimax-m2.7"
 *
 * Provides retry logic with exponential backoff (max 3 attempts).
 */

import OpenAI from "openai";
import type {
  MiniMaxClientConfig,
  MiniMaxMessage,
  ChatOptions,
  ChatResponse,
} from "../types/index.js";
import { rateLimiter } from "../autonomous/rate-limiter.js";
import { log } from "../autonomous/logger.js";

// ── Concurrency limiter ───────────────────────────────────────────────

class Semaphore {
  private queue: Array<() => void> = [];
  private active = 0;
  constructor(private max: number) {}

  async acquire(): Promise<void> {
    if (this.active < this.max) {
      this.active++;
      return;
    }
    return new Promise<void>(resolve => {
      this.queue.push(() => { this.active++; resolve(); });
    });
  }

  release(): void {
    this.active--;
    const next = this.queue.shift();
    if (next) next();
  }
}

const concurrencyLimiter = new Semaphore(3); // Max 3 simultaneous API calls

// ── Constants ──────────────────────────────────────────────────────────

const DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.io/v1";
const DEFAULT_MINIMAX_MODEL = "MiniMax-M2.7";
const OLLAMA_MODEL = "minimax-m2.7";

const MAX_RETRIES = 3;
const INITIAL_BACKOFF_MS = 500;

// Errors that are safe to retry (transient server / network issues).
const RETRYABLE_STATUS_CODES = new Set([408, 429, 500, 502, 503, 504]);

// ── Configuration ──────────────────────────────────────────────────────

function resolveConfig(): MiniMaxClientConfig {
  const ollamaBase = process.env.OLLAMA_BASE_URL;

  if (ollamaBase) {
    return {
      apiKey: "ollama",
      baseURL: ollamaBase,
      model: OLLAMA_MODEL,
      mode: "ollama",
    };
  }

  const apiKey = process.env.MINIMAX_API_KEY;
  if (!apiKey) {
    throw new Error(
      "MiniMax client requires either MINIMAX_API_KEY (API mode) or OLLAMA_BASE_URL (Ollama mode) to be set.",
    );
  }

  return {
    apiKey,
    baseURL: process.env.MINIMAX_BASE_URL ?? DEFAULT_MINIMAX_BASE_URL,
    model: DEFAULT_MINIMAX_MODEL,
    mode: "api",
  };
}

// ── Factory ────────────────────────────────────────────────────────────

export interface MiniMaxClient {
  /** The underlying OpenAI-compatible SDK instance. */
  raw: OpenAI;
  config: Readonly<MiniMaxClientConfig>;
  chat: (messages: MiniMaxMessage[], options?: ChatOptions) => Promise<ChatResponse>;
}

/**
 * Create a configured MiniMax client. Reads env vars on each call so
 * tests can swap values without module-level caching.
 */
export function createMiniMaxClient(overrides?: Partial<MiniMaxClientConfig>): MiniMaxClient {
  const config: MiniMaxClientConfig = { ...resolveConfig(), ...overrides };

  const raw = new OpenAI({
    apiKey: config.apiKey,
    baseURL: config.baseURL,
    timeout: 60_000,
  });

  return { raw, config, chat: (msgs, opts) => chat(raw, config, msgs, opts) };
}

// ── Chat wrapper ───────────────────────────────────────────────────────

async function chat(
  client: OpenAI,
  config: MiniMaxClientConfig,
  messages: MiniMaxMessage[],
  options: ChatOptions = {},
): Promise<ChatResponse> {
  const model = options.model ?? config.model;

  const body: OpenAI.ChatCompletionCreateParamsNonStreaming = {
    model,
    messages: messages as OpenAI.ChatCompletionMessageParam[],
    temperature: options.temperature ?? 0.3,
    max_tokens: options.maxTokens ?? 4096,
  };

  if (options.tools?.length) {
    body.tools = options.tools as OpenAI.ChatCompletionTool[];
    if (options.toolChoice) {
      body.tool_choice = options.toolChoice as OpenAI.ChatCompletionToolChoiceOption;
    }
  }

  if (options.responseFormat) {
    body.response_format = options.responseFormat;
  }

  // Concurrency limiter: max 3 simultaneous API calls to avoid flooding
  await concurrencyLimiter.acquire();

  // Rate limit: wait for capacity before calling
  await rateLimiter.waitForCapacity();
  rateLimiter.track();

  log.info('minimax', `API call: ${model}`, { messageCount: messages.length, tools: options.tools?.length ?? 0 });

  try {
    const result = await executeWithRetry(config, body);
    concurrencyLimiter.release();
    log.apiCall('minimax', model, { input: result.usage.promptTokens, output: result.usage.completionTokens });
    return result;
  } catch (err) {
    concurrencyLimiter.release();
    const msg = err instanceof Error ? err.message : String(err);
    log.apiError('minimax', msg);
    throw err;
  }
}

// ── Retry logic ────────────────────────────────────────────────────────

async function executeWithRetry(
  config: MiniMaxClientConfig,
  body: OpenAI.ChatCompletionCreateParamsNonStreaming,
): Promise<ChatResponse> {
  let lastError: unknown;

  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      log.debug('minimax', `Attempt ${attempt + 1}/${MAX_RETRIES} to ${config.baseURL}`);
      const url = `${config.baseURL}/chat/completions`;
      log.debug('minimax', `Fetching ${url} with model ${body.model}, ${body.messages?.length} messages`);
      const fetchResponse = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${config.apiKey}`,
        },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(120_000),
      });

      log.debug('minimax', `Got response: ${fetchResponse.status}`);

      if (!fetchResponse.ok) {
        const errorText = await fetchResponse.text();
        throw new MiniMaxClientError(`HTTP ${fetchResponse.status}: ${errorText}`, fetchResponse.status);
      }

      const data = await fetchResponse.json() as any;
      log.debug('minimax', `Parsed response, content length: ${data.choices?.[0]?.message?.content?.length ?? 0}`);
      return mapFetchResponse(data);
    } catch (err: unknown) {
      lastError = err;
      log.warn('minimax', `Attempt ${attempt + 1} failed: ${err instanceof Error ? err.message : String(err)}`);

      if (attempt === MAX_RETRIES - 1) break;

      const delayMs = INITIAL_BACKOFF_MS * Math.pow(2, attempt);
      await sleep(delayMs);
    }
  }

  throw new MiniMaxClientError(
    `MiniMax API call failed after ${MAX_RETRIES} attempts`,
    lastError,
  );
}

function mapFetchResponse(data: any): ChatResponse {
  const choice = data.choices?.[0];
  if (!choice) {
    throw new MiniMaxClientError("MiniMax returned an empty choices array", data);
  }

  return {
    id: data.id || '',
    content: choice.message?.content ?? null,
    toolCalls: (choice.message?.tool_calls ?? []).map((tc: any) => ({
      id: tc.id,
      type: "function" as const,
      function: {
        name: tc.function.name,
        arguments: tc.function.arguments,
      },
    })),
    finishReason: choice.finish_reason,
    usage: {
      promptTokens: data.usage?.prompt_tokens ?? 0,
      completionTokens: data.usage?.completion_tokens ?? 0,
      totalTokens: data.usage?.total_tokens ?? 0,
    },
  };
}

function isRetryable(err: unknown): boolean {
  if (err instanceof OpenAI.APIError) {
    return RETRYABLE_STATUS_CODES.has(err.status);
  }
  // Network errors (ECONNRESET, ETIMEDOUT, etc.) are retryable.
  if (err instanceof Error && "code" in err) {
    const code = (err as NodeJS.ErrnoException).code;
    return code === "ECONNRESET" || code === "ETIMEDOUT" || code === "ECONNREFUSED";
  }
  return false;
}

// ── Response mapping ───────────────────────────────────────────────────

function mapResponse(response: OpenAI.ChatCompletion): ChatResponse {
  const choice = response.choices[0];
  if (!choice) {
    throw new MiniMaxClientError("MiniMax returned an empty choices array", response);
  }

  return {
    id: response.id,
    content: choice.message.content ?? null,
    toolCalls: (choice.message.tool_calls ?? []).map((tc) => {
      const ftc = tc as OpenAI.Chat.Completions.ChatCompletionMessageFunctionToolCall;
      return {
        id: ftc.id,
        type: "function" as const,
        function: {
          name: ftc.function.name,
          arguments: ftc.function.arguments,
        },
      };
    }),
    finishReason: choice.finish_reason,
    usage: {
      promptTokens: response.usage?.prompt_tokens ?? 0,
      completionTokens: response.usage?.completion_tokens ?? 0,
      totalTokens: response.usage?.total_tokens ?? 0,
    },
  };
}

// ── Helpers ────────────────────────────────────────────────────────────

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ── Error class ────────────────────────────────────────────────────────

export class MiniMaxClientError extends Error {
  public readonly cause: unknown;

  constructor(message: string, cause?: unknown) {
    super(message);
    this.name = "MiniMaxClientError";
    this.cause = cause;
  }
}
