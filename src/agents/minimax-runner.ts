// Multi-model agent runner with function calling and tool execution.
// Routes to MiniMax M2.7 (planning/research) or Kimi K2.5 (implementation/review)
// based on agent role via the model router.

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { dirname } from 'node:path';
import { glob } from 'node:fs';
import OpenAI from 'openai';
import {
  AgentConfig,
  AgentRunnerEvent,
  MiniMaxMessage,
  ToolCall,
} from '../types/index.js';
import { getModelForRole, type ModelConfig } from './model-router.js';
import { log } from '../autonomous/logger.js';

const execFileAsync = promisify(execFile);

export interface RunnerOptions {
  cwd: string;
  maxTurns?: number;
  onEvent?: (event: AgentRunnerEvent) => void;
  signal?: AbortSignal;
}

export function createMiniMaxClient(): OpenAI {
  const provider = process.env.MINIMAX_PROVIDER || 'api';

  if (provider === 'ollama') {
    return new OpenAI({
      apiKey: 'ollama',
      baseURL: process.env.OLLAMA_BASE_URL || 'http://localhost:11434/v1',
    });
  }

  return new OpenAI({
    apiKey: process.env.MINIMAX_API_KEY!,
    baseURL: process.env.MINIMAX_BASE_URL || 'https://api.minimax.io/v1',
  });
}

export async function* runAgent(
  _client: OpenAI | null,
  config: AgentConfig,
  prompt: string,
  options: RunnerOptions,
): AsyncGenerator<AgentRunnerEvent> {
  // Route to the right model based on agent role
  const modelConfig = getModelForRole(config.role);
  const activeClient = modelConfig.client; // Always use model router's client, not passed-in
  const activeModel = modelConfig.model;
  const isMiniMax = modelConfig.provider === 'minimax';

  log.info('runner', `Agent "${config.name}" using ${modelConfig.provider}/${activeModel}`, {
    role: config.role,
    cwd: options.cwd,
    toolCount: config.tools.length,
  });

  const maxTurns = options.maxTurns ?? 50;
  const messages: OpenAI.Chat.ChatCompletionMessageParam[] = [
    { role: 'system', content: config.systemPrompt },
    { role: 'user', content: prompt },
  ];

  const tools: OpenAI.Chat.ChatCompletionTool[] = config.tools.map((t) => ({
    type: 'function' as const,
    function: t.function,
  }));

  for (let turn = 0; turn < maxTurns; turn++) {
    if (options.signal?.aborted) {
      yield { type: 'error', data: 'Aborted by signal' };
      return;
    }

    let response: OpenAI.Chat.ChatCompletion;
    try {
      const requestBody: Record<string, unknown> = {
        model: activeModel,
        messages,
        tools: tools.length > 0 ? tools : undefined,
        temperature: 0.3,
      };
      log.debug('runner', `Turn ${turn + 1}/${maxTurns} calling ${modelConfig.provider}/${activeModel}`);
      response = await (activeClient.chat.completions.create as Function)(requestBody);
      const usage = response.usage;
      if (usage) {
        log.apiCall(modelConfig.provider, activeModel, {
          input: usage.prompt_tokens,
          output: usage.completion_tokens,
        });
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      log.apiError(modelConfig.provider, msg);
      yield { type: 'error', data: `API error (${modelConfig.provider}): ${msg}` };
      return;
    }

    const choice = response.choices[0];
    if (!choice) {
      yield { type: 'error', data: 'No response from MiniMax' };
      return;
    }

    const assistantMessage = choice.message;

    // Emit text output if present
    if (assistantMessage.content) {
      yield { type: 'output', data: assistantMessage.content };
    }

    // If no tool calls on first few turns AND we have tools, nudge the model to use them
    if (!assistantMessage.tool_calls || assistantMessage.tool_calls.length === 0) {
      if (turn < 3 && tools.length > 0) {
        // Model responded with text instead of calling tools - push it to use them
        messages.push(assistantMessage as any);
        messages.push({
          role: 'user',
          content: 'You must use the provided tools (write_file, edit_file, read_file, etc.) to make actual changes. Do not describe what to do - call the tools now.',
        });
        log.debug('runner', `Turn ${turn + 1}: nudging model to use tools instead of text response`);
        continue;
      }
      yield { type: 'done', data: assistantMessage.content || '', exitCode: 0 };
      return;
    }

    // CRITICAL: Add the ENTIRE assistant message to history, including
    // reasoning_details field. This preserves M2.7's interleaved thinking
    // chain across tool-call rounds. Breaking this degrades performance.
    messages.push(assistantMessage as any);

    // Execute tool calls
    for (const toolCall of assistantMessage.tool_calls) {
      const tc = toolCall as OpenAI.Chat.Completions.ChatCompletionMessageFunctionToolCall;
      yield {
        type: 'tool_call',
        data: `${tc.function.name}(${tc.function.arguments})`,
        toolCall: {
          id: tc.id,
          type: 'function',
          function: tc.function,
        },
      };

      let result: string;
      try {
        result = await executeToolCall(
          tc.function.name,
          JSON.parse(tc.function.arguments),
          options.cwd,
        );
      } catch (err) {
        result = `Error: ${err instanceof Error ? err.message : String(err)}`;
      }

      messages.push({
        role: 'tool',
        tool_call_id: tc.id,
        content: result,
      });
    }
  }

  yield { type: 'error', data: `Exceeded max turns (${maxTurns})` };
}

async function executeToolCall(
  name: string,
  args: Record<string, string>,
  cwd: string,
): Promise<string> {
  switch (name) {
    case 'read_file': {
      const content = await readFile(resolvePath(args.path, cwd), 'utf-8');
      return content.length > 50_000
        ? content.slice(0, 50_000) + '\n... (truncated)'
        : content;
    }

    case 'write_file': {
      const fullPath = resolvePath(args.path, cwd);
      await mkdir(dirname(fullPath), { recursive: true });
      await writeFile(fullPath, args.content, 'utf-8');
      return `Written to ${args.path}`;
    }

    case 'edit_file': {
      const fullPath = resolvePath(args.path, cwd);
      const content = await readFile(fullPath, 'utf-8');
      if (!content.includes(args.old_string)) {
        return `Error: old_string not found in ${args.path}`;
      }
      const newContent = content.replace(args.old_string, args.new_string);
      await writeFile(fullPath, newContent, 'utf-8');
      return `Edited ${args.path}`;
    }

    case 'list_files': {
      const dir = args.path ? resolvePath(args.path, cwd) : cwd;
      const { stdout } = await execFileAsync('find', [dir, '-name', args.pattern, '-type', 'f'], {
        cwd,
        timeout: 10_000,
      });
      const files = stdout.trim().split('\n').filter(Boolean).slice(0, 200);
      return files.join('\n') || 'No files found';
    }

    case 'search_files': {
      const searchArgs = ['--no-heading', '-n', args.pattern];
      if (args.glob) searchArgs.push('--glob', args.glob);
      searchArgs.push(args.path || '.');
      try {
        const { stdout } = await execFileAsync('rg', searchArgs, {
          cwd,
          timeout: 15_000,
        });
        const lines = stdout.trim().split('\n').slice(0, 100);
        return lines.join('\n') || 'No matches found';
      } catch {
        return 'No matches found';
      }
    }

    case 'run_command': {
      const execCwd = args.cwd ? resolvePath(args.cwd, cwd) : cwd;
      const { stdout, stderr } = await execFileAsync('sh', ['-c', args.command], {
        cwd: execCwd,
        timeout: 60_000,
        maxBuffer: 1024 * 1024,
      });
      return (stdout + (stderr ? `\nstderr: ${stderr}` : '')).trim();
    }

    case 'git': {
      const gitCwd = args.cwd ? resolvePath(args.cwd, cwd) : cwd;
      const gitArgs = args.args.split(/\s+/);
      const { stdout, stderr } = await execFileAsync('git', gitArgs, {
        cwd: gitCwd,
        timeout: 30_000,
      });
      return (stdout + (stderr ? `\n${stderr}` : '')).trim();
    }

    case 'web_search': {
      try {
        const { stdout } = await execFileAsync('curl', [
          '-sf', '-m', '10',
          `https://api.serper.dev/search`,
          '-H', 'X-API-KEY: ' + (process.env.SERPER_API_KEY || ''),
          '-H', 'Content-Type: application/json',
          '-d', JSON.stringify({ q: args.query }),
        ], { timeout: 15_000 });
        const data = JSON.parse(stdout);
        const results = (data.organic || []).slice(0, 5);
        return results.map((r: any) => `${r.title}\n${r.link}\n${r.snippet}`).join('\n\n') || 'No results';
      } catch {
        return 'Web search unavailable';
      }
    }

    case 'wikidelve_search': {
      const wdUrl = process.env.WIKIDELVE_URL;
      try {
        const { stdout } = await execFileAsync('curl', [
          '-sf', '-m', '10',
          `${wdUrl}/api/search/hybrid?q=${encodeURIComponent(args.query)}&limit=5`,
        ], { timeout: 15_000 });
        const results = JSON.parse(stdout);
        return results.map((r: any) => `[${r.kb}:${r.slug}] ${r.title}\n${r.snippet || ''}`).join('\n\n') || 'No results';
      } catch {
        return 'Wikidelve search unavailable';
      }
    }

    case 'wikidelve_read': {
      const wdUrl = process.env.WIKIDELVE_URL;
      const kb = args.kb || 'personal';
      try {
        const { stdout } = await execFileAsync('curl', [
          '-sf', '-m', '15',
          `${wdUrl}/api/articles/${kb}/${args.slug}`,
        ], { timeout: 20_000 });
        const article = JSON.parse(stdout);
        const md = article.raw_markdown || '';
        return md.length > 30_000 ? md.slice(0, 30_000) + '\n... (truncated)' : md;
      } catch {
        return 'Article not found';
      }
    }

    case 'wikidelve_research': {
      const wdUrl = process.env.WIKIDELVE_URL;
      try {
        const { stdout } = await execFileAsync('curl', [
          '-sf', '-m', '10', '-X', 'POST',
          `${wdUrl}/api/research`,
          '-H', 'Content-Type: application/json',
          '-d', JSON.stringify({ topic: args.topic }),
        ], { timeout: 15_000 });
        const job = JSON.parse(stdout);
        return `Research job queued: ${job.job_id || job.id} - ${job.topic}`;
      } catch {
        return 'Wikidelve research unavailable';
      }
    }

    default:
      return `Unknown tool: ${name}`;
  }
}

function resolvePath(p: string, cwd: string): string {
  if (p.startsWith('/')) return p;
  return `${cwd}/${p}`;
}
