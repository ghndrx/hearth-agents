// Deep research using wikidelve + MiniMax synthesis.
// Wikidelve handles the deep research, MiniMax synthesizes findings.

import type { MiniMaxClient } from '../orchestrator/minimax-client.js';
import type { Feature } from './feature-backlog.js';
import type { MiniMaxMessage } from '../types/index.js';
import { researchAndGet, hybridSearch } from './wikidelve.js';

export interface ResearchReport {
  featureId: string;
  topics: { topic: string; findings: string }[];
  competitorAnalysis: string;
  technicalRecommendations: string;
  risks: string[];
  fullReport: string;
}

const RESEARCH_SYSTEM_PROMPT = `You are a senior technical researcher preparing deep research reports for software development.

Your job is to research topics thoroughly and provide actionable technical findings that will inform PRD creation and implementation.

For each research topic:
1. Explain the core concepts and how they work
2. Identify best practices and common pitfalls
3. Recommend specific libraries, protocols, or approaches
4. Note any security considerations
5. Provide concrete implementation guidance

Be thorough but concise. Focus on what a developer needs to know to implement this feature.`;

export async function researchFeature(
  client: MiniMaxClient,
  feature: Feature,
): Promise<ResearchReport> {
  // Limit to 2 topics to speed up research phase. Quality over quantity.
  const topicsToResearch = feature.researchTopics.slice(0, 2);
  console.log(`[researcher] Researching ${topicsToResearch.length} topics for "${feature.name}"`);

  const WIKIDELVE_TIMEOUT_MS = 3_000; // 3s timeout - fail fast, don't block MiniMax calls

  const topicPromises = topicsToResearch.map(async (topic) => {
    const fullTopic = `${topic} for ${feature.name} in a Discord-like chat application`;
    let findings = '';

    // Try wikidelve with a short timeout
    try {
      const wikidelvePromise = researchAndGet(fullTopic);
      const timeoutPromise = new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error('wikidelve timeout')), WIKIDELVE_TIMEOUT_MS)
      );
      const result = await Promise.race([wikidelvePromise, timeoutPromise]);
      findings = result.articles.map(a => a.raw_markdown).join('\n\n---\n\n');
    } catch {
      // Wikidelve unavailable or timed out - go straight to MiniMax
    }

    // If wikidelve didn't deliver, use MiniMax directly
    if (findings.length < 200) {
      const messages: MiniMaxMessage[] = [
        { role: 'system', content: RESEARCH_SYSTEM_PROMPT },
        {
          role: 'user',
          content: `Research the following topic for implementing "${feature.name}" in a Discord-like chat application:\n\n**Topic**: ${topic}\n\n**Context**: ${feature.description}\n\nProvide detailed, actionable technical findings.`,
        },
      ];
      const response = await client.chat(messages, { temperature: 0.3, maxTokens: 4096 });
      findings = (findings ? findings + '\n\n---\n\n' : '') + (response.content || 'No findings');
    }

    return { topic, findings };
  });

  const topics = await Promise.all(topicPromises);
  console.log(`[researcher] All ${topics.length} topics complete for "${feature.name}"`);

  // Skip synthesis step - PRD generator will synthesize. Saves a huge MiniMax call.
  const synthesis = { content: '' };

  const fullReport = [
    `# Research Report: ${feature.name}`,
    '',
    `## Feature Description`,
    feature.description,
    '',
    `## Discord Parity`,
    feature.discordParity,
    '',
    `## Research Findings`,
    ...topics.map(t => `### ${t.topic}\n${t.findings}`),
    '',
    `## Synthesis`,
    synthesis.content || '',
  ].join('\n');

  return {
    featureId: feature.id,
    topics,
    competitorAnalysis: synthesis.content || '',
    technicalRecommendations: '',
    risks: [],
    fullReport,
  };
}
