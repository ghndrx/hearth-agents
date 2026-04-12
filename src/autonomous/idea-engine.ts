// Idea Engine: uses MiniMax M2.7 to analyze the knowledge base,
// completed work, industry trends, and competitor moves to generate
// high-value research topics. Feeds the research pipeline continuously.

import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import type { MiniMaxClient } from '../orchestrator/minimax-client.js';
import type { MiniMaxMessage } from '../types/index.js';
import { FEATURE_BACKLOG } from './feature-backlog.js';
import { searchKnowledge, loadIndex } from './knowledge-base.js';

export interface ResearchIdea {
  topic: string;
  rationale: string;
  category: 'feature' | 'performance' | 'security' | 'ux' | 'infrastructure' | 'competitive' | 'integration' | 'monetization';
  priority: 'high' | 'medium' | 'low';
  searchQueries: string[];
  expectedOutcome: string;
}

export interface IdeaReport {
  generatedAt: string;
  ideas: ResearchIdea[];
  reasoning: string;
}

const IDEA_ENGINE_PROMPT = `You are a strategic product intelligence agent for Hearth, a self-hosted Discord alternative.

Your job is to evaluate what research would be most valuable RIGHT NOW based on:
1. What has already been built and researched (knowledge base)
2. What features are in progress or planned
3. Current industry trends and competitor moves
4. Technical opportunities and risks
5. User demand signals

You think like a combination of:
- A product manager (what do users need?)
- A CTO (what technical investments pay off?)
- A competitive analyst (what are Discord/Element/Revolt doing?)
- A growth hacker (what gets attention and adoption?)

## Hearth Context
- **Stack**: Go 1.25, SvelteKit/Svelte 5, PostgreSQL 16, Redis 7, LiveKit, Signal E2EE
- **Repos**: hearth (main), hearth-desktop (Tauri), hearth-mobile (React Native)
- **Key differentiators**: Self-hosted, Matrix federation, E2EE, no surveillance, Nitro features free
- **Target**: Discord exodus users (privacy concerns, age verification controversy)
- **Agent system**: Autonomous MiniMax M2.7 + Kimi K2.5 agents building features 24/7

## Output Format
Return a JSON object with:
- "reasoning": your analysis of what areas need research most and why
- "ideas": array of research ideas, each with:
  - "topic": concise research topic
  - "rationale": why this research is valuable now
  - "category": feature|performance|security|ux|infrastructure|competitive|integration|monetization
  - "priority": high|medium|low
  - "searchQueries": 3-5 specific search queries to run
  - "expectedOutcome": what we'll learn and how it helps Hearth

Generate 8-12 ideas, ordered by priority. Be specific and actionable.
Do NOT suggest research we've already done. Focus on gaps and opportunities.`;

export async function generateResearchIdeas(client: MiniMaxClient): Promise<IdeaReport> {
  // Gather context from knowledge base
  const index = await loadIndex();
  const completedResearch = index.entries
    .filter(e => e.type === 'research')
    .map(e => `- ${e.title}: ${e.summary}`)
    .join('\n');

  const completedFeatures = FEATURE_BACKLOG
    .filter(f => f.status === 'done')
    .map(f => `- ${f.name}: ${f.description.slice(0, 100)}`)
    .join('\n');

  const pendingFeatures = FEATURE_BACKLOG
    .filter(f => f.status === 'pending')
    .map(f => `- ${f.name} (${f.priority}): ${f.description.slice(0, 100)}`)
    .join('\n');

  const inProgress = FEATURE_BACKLOG
    .filter(f => f.status !== 'pending' && f.status !== 'done')
    .map(f => `- ${f.name} (${f.status})`)
    .join('\n');

  const messages: MiniMaxMessage[] = [
    { role: 'system', content: IDEA_ENGINE_PROMPT },
    {
      role: 'user',
      content: `Analyze our current state and generate high-value research ideas.

## Already Researched (${index.entries.filter(e => e.type === 'research').length} reports)
${completedResearch || '(none yet)'}

## Completed Features (${FEATURE_BACKLOG.filter(f => f.status === 'done').length})
${completedFeatures || '(none yet)'}

## In Progress
${inProgress || '(none)'}

## Pending Features (${FEATURE_BACKLOG.filter(f => f.status === 'pending').length})
${pendingFeatures}

## Knowledge Base Stats
${index.totalEntries} entries total, last updated ${index.lastUpdated}

## Current Date
${new Date().toISOString().split('T')[0]}

What research should we prioritize next? Think about what would give the biggest advantage for Hearth's development and adoption. Return ONLY valid JSON.`,
    },
  ];

  const response = await client.chat(messages, {
    temperature: 0.5, // Slightly creative for idea generation
    maxTokens: 6144,
  });

  if (!response.content) {
    return { generatedAt: new Date().toISOString(), ideas: [], reasoning: 'No response' };
  }

  try {
    let jsonStr = response.content.trim();
    if (jsonStr.startsWith('```')) {
      jsonStr = jsonStr.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '');
    }
    const parsed = JSON.parse(jsonStr);
    return {
      generatedAt: new Date().toISOString(),
      ideas: parsed.ideas || [],
      reasoning: parsed.reasoning || '',
    };
  } catch {
    return {
      generatedAt: new Date().toISOString(),
      ideas: [],
      reasoning: response.content.slice(0, 500),
    };
  }
}

// Save ideas to knowledge base
export async function saveIdeasToKB(report: IdeaReport): Promise<string> {
  const { writeFile, mkdir } = await import('node:fs/promises');
  const dir = join(process.cwd(), 'knowledge', 'next-steps');
  await mkdir(dir, { recursive: true });

  const date = new Date().toISOString().split('T')[0];
  const filename = `${date}-research-ideas.md`;
  const filepath = join(dir, filename);

  const content = [
    `# Research Ideas - ${date}`,
    '',
    `*Generated by Idea Engine at ${report.generatedAt}*`,
    '',
    '## Strategic Reasoning',
    report.reasoning,
    '',
    '---',
    '',
    ...report.ideas.map((idea, i) => [
      `## ${i + 1}. ${idea.topic}`,
      '',
      `**Category**: ${idea.category} | **Priority**: ${idea.priority}`,
      '',
      `**Rationale**: ${idea.rationale}`,
      '',
      `**Expected Outcome**: ${idea.expectedOutcome}`,
      '',
      '**Search Queries**:',
      ...idea.searchQueries.map(q => `- \`${q}\``),
      '',
    ].join('\n')),
  ].join('\n');

  await writeFile(filepath, content, 'utf-8');
  return filepath;
}

// Format ideas for Telegram
export function formatIdeasForTelegram(report: IdeaReport, limit = 5): string {
  const ideas = report.ideas.slice(0, limit);
  const priorityIcon = { high: '🔴', medium: '🟡', low: '🟢' };
  const categoryIcon: Record<string, string> = {
    feature: '✨', performance: '⚡', security: '🛡️', ux: '🎨',
    infrastructure: '🏗️', competitive: '⚔️', integration: '🔌', monetization: '💰',
  };

  let msg = `<b>💡 Research Ideas Generated</b>\n\n`;
  msg += `<i>${report.reasoning.slice(0, 200)}...</i>\n\n`;

  for (const idea of ideas) {
    const pi = priorityIcon[idea.priority] || '⚪';
    const ci = categoryIcon[idea.category] || '📋';
    msg += `${pi}${ci} <b>${idea.topic}</b>\n`;
    msg += `   ${idea.rationale.slice(0, 100)}\n\n`;
  }

  msg += `<i>${report.ideas.length} ideas total. See knowledge/next-steps/ for full details.</i>`;
  return msg;
}
