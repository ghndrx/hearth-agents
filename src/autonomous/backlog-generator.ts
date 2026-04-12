// Self-generating backlog: after completing features, the agent
// analyzes what's been built and generates new features to work on.
// The loop never runs out of work.

import type { MiniMaxClient } from '../orchestrator/minimax-client.js';
import type { MiniMaxMessage } from '../types/index.js';
import { FEATURE_BACKLOG, addFeature, type Feature } from './feature-backlog.js';

const GENERATE_SYSTEM_PROMPT = `You are a senior product strategist for Hearth, a self-hosted Discord alternative.

Hearth's tech stack: Go 1.25 (Fiber, WebSocket), PostgreSQL 16, Redis 7, SvelteKit/Svelte 5, TailwindCSS, LiveKit (voice/video), Signal Protocol E2EE.
Repos: hearth (main backend+frontend), hearth-desktop (Tauri), hearth-mobile (React Native).

Your job: analyze what features have been completed and what gaps remain, then generate the NEXT batch of features to implement. Consider:

1. Discord feature parity - what does Discord have that Hearth doesn't?
2. Competitive advantages - what can Hearth do BETTER than Discord? (self-hosting, privacy, federation, no paywalls)
3. User experience polish - what makes the difference between "it works" and "it's delightful"?
4. Infrastructure improvements - performance, reliability, scalability
5. Developer platform - APIs, webhooks, bots, integrations
6. Mobile/desktop parity - ensure all platforms have feature parity

Respond with a JSON array of features. Each feature must have: id, name, description, priority (critical/high/medium/low), repos (array of hearth/hearth-desktop/hearth-mobile), researchTopics (array of strings), discordParity (string).

Generate 5-8 features, ordered by priority.`;

export async function generateNewFeatures(client: MiniMaxClient): Promise<Feature[]> {
  const completed = FEATURE_BACKLOG.filter(f => f.status === 'done');
  const pending = FEATURE_BACKLOG.filter(f => f.status === 'pending');
  const allIds = new Set(FEATURE_BACKLOG.map(f => f.id));

  const messages: MiniMaxMessage[] = [
    { role: 'system', content: GENERATE_SYSTEM_PROMPT },
    {
      role: 'user',
      content: `Here is the current state of the Hearth feature backlog:

## Completed Features (${completed.length})
${completed.map(f => `- **${f.name}**: ${f.description.slice(0, 100)}...`).join('\n')}

## Still Pending (${pending.length})
${pending.map(f => `- **${f.name}** (${f.priority}): ${f.description.slice(0, 100)}...`).join('\n')}

## All Feature IDs (to avoid duplicates)
${[...allIds].join(', ')}

Generate the next batch of 5-8 features that should be built after the pending ones are done. Focus on gaps in Discord parity, user experience polish, and competitive advantages. Use unique IDs that don't conflict with existing ones.

Respond with ONLY a JSON array, no markdown fences.`,
    },
  ];

  const response = await client.chat(messages, {
    temperature: 0.4,
    maxTokens: 4096,
  });

  if (!response.content) return [];

  try {
    // Try to extract JSON from response
    let jsonStr = response.content.trim();
    // Strip markdown fences if present
    if (jsonStr.startsWith('```')) {
      jsonStr = jsonStr.replace(/^```(?:json)?\n?/, '').replace(/\n?```$/, '');
    }

    const features = JSON.parse(jsonStr) as Array<{
      id: string;
      name: string;
      description: string;
      priority: string;
      repos: string[];
      researchTopics: string[];
      discordParity: string;
    }>;

    return features
      .filter(f => !allIds.has(f.id)) // No duplicates
      .map(f => ({
        id: f.id,
        name: f.name,
        description: f.description,
        priority: (f.priority || 'medium') as Feature['priority'],
        repos: (f.repos || ['hearth']) as Feature['repos'],
        researchTopics: f.researchTopics || [],
        discordParity: f.discordParity || '',
        status: 'pending' as const,
      }));
  } catch (err) {
    console.error('[backlog-generator] Failed to parse features:', err);
    return [];
  }
}

export async function refillBacklog(client: MiniMaxClient): Promise<number> {
  const pending = FEATURE_BACKLOG.filter(f => f.status === 'pending').length;

  // Only generate more when we're running low
  if (pending > 3) return 0;

  console.log(`[backlog-generator] Only ${pending} features pending, generating more...`);
  const newFeatures = await generateNewFeatures(client);

  for (const feature of newFeatures) {
    addFeature(feature);
  }

  console.log(`[backlog-generator] Added ${newFeatures.length} new features to backlog`);
  return newFeatures.length;
}
