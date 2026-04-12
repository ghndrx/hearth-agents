// Autonomous PRD generator using MiniMax M2.7.
// Takes research reports and generates full PRDs, saving them to the Hearth repo.

import { writeFile, mkdir } from 'node:fs/promises';
import { join } from 'node:path';
import type { MiniMaxClient } from '../orchestrator/minimax-client.js';
import type { Feature } from './feature-backlog.js';
import type { ResearchReport } from './researcher.js';
import type { MiniMaxMessage } from '../types/index.js';

const PRD_SYSTEM_PROMPT = `You are a senior product engineer writing PRDs for Hearth, a self-hosted Discord alternative.

## Hearth Architecture
- **Backend**: Go 1.25 (Fiber HTTP, WebSocket gateway), PostgreSQL 16, Redis 7 (pub/sub + caching)
- **Frontend**: SvelteKit with Svelte 5, TypeScript, TailwindCSS
- **Voice/Video**: LiveKit integration for SFU routing
- **Encryption**: Signal Protocol (libsignal-client) for E2EE DMs
- **Auth**: JWT + bcrypt, OAuth 2.0, MFA/TOTP
- **Scale**: 170+ backend services, 88+ data models, distributed WebSocket hub via Redis
- **Repos**: hearth (main), hearth-desktop (Electron), hearth-mobile (iOS/Android native)

## PRD Format
Write comprehensive PRDs with these sections:

1. **Overview** - Problem statement, goals, success metrics, target users
2. **User Stories** - As a [role], I want [action], so that [benefit]
3. **Acceptance Criteria** - Given/When/Then for each user story
4. **Technical Design** - Architecture, component interactions, data flow, sequence diagrams
5. **API Specifications** - HTTP endpoints and WebSocket events with schemas
6. **Data Models** - PostgreSQL schemas, Redis key patterns, TypeScript types
7. **Security Considerations** - E2EE implications, auth, data protection
8. **Testing Strategy** - Unit, integration, E2E tests with coverage targets
9. **Implementation Plan** - Phased rollout, task breakdown with estimates
10. **Migration Strategy** - How to deploy without breaking existing users

Be thorough, specific, and implementation-ready. Reference existing Hearth patterns.`;

export interface GeneratedPRD {
  featureId: string;
  filename: string;
  filepath: string;
  content: string;
}

export async function generatePRD(
  client: MiniMaxClient,
  feature: Feature,
  research: ResearchReport,
  hearthRepoPath: string,
): Promise<GeneratedPRD> {
  const messages: MiniMaxMessage[] = [
    { role: 'system', content: PRD_SYSTEM_PROMPT },
    {
      role: 'user',
      content: `Write a comprehensive PRD for the following feature:

**Feature**: ${feature.name}
**Description**: ${feature.description}
**Priority**: ${feature.priority}
**Target Repos**: ${feature.repos.join(', ')}
**Discord Parity**: ${feature.discordParity}

## Research Report
${research.fullReport}

Write the full PRD now. Be thorough and implementation-ready.`,
    },
  ];

  const response = await client.chat(messages, {
    temperature: 0.3,
    maxTokens: 8192,
  });

  const content = response.content || '';
  const filename = `${feature.id}-prd.md`;
  const prdDir = join(hearthRepoPath, 'PRDs');
  const filepath = join(prdDir, filename);

  await mkdir(prdDir, { recursive: true });
  await writeFile(filepath, content, 'utf-8');

  return {
    featureId: feature.id,
    filename,
    filepath,
    content,
  };
}
