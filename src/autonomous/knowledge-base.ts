// Knowledge base: self-building documentation system.
// Accumulates research, decisions, patterns, and next-step suggestions
// as agents work through the feature backlog.

import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { join } from 'node:path';
import type { Feature } from './feature-backlog.js';
import type { ResearchReport } from './researcher.js';
import type { GeneratedPRD } from './prd-generator.js';
import type { ImplementationResult } from './implementer.js';

const KB_ROOT = join(process.cwd(), 'knowledge');

export interface KBEntry {
  id: string;
  type: 'research' | 'prd' | 'implementation' | 'next-step' | 'guide';
  title: string;
  feature: string;
  createdAt: string;
  tags: string[];
  path: string;
  summary: string;
}

export interface KBIndex {
  lastUpdated: string;
  totalEntries: number;
  entries: KBEntry[];
  nextSteps: string[];
  researchQueue: string[];
}

// -- Index Management --

export async function loadIndex(): Promise<KBIndex> {
  try {
    const raw = await readFile(join(KB_ROOT, 'index.json'), 'utf-8');
    return JSON.parse(raw);
  } catch {
    return {
      lastUpdated: new Date().toISOString(),
      totalEntries: 0,
      entries: [],
      nextSteps: [],
      researchQueue: [],
    };
  }
}

async function saveIndex(index: KBIndex): Promise<void> {
  index.lastUpdated = new Date().toISOString();
  index.totalEntries = index.entries.length;
  await writeFile(join(KB_ROOT, 'index.json'), JSON.stringify(index, null, 2), 'utf-8');
}

async function addEntry(entry: KBEntry): Promise<void> {
  const index = await loadIndex();
  // Replace existing entry with same id, or add new
  const existing = index.entries.findIndex(e => e.id === entry.id);
  if (existing >= 0) {
    index.entries[existing] = entry;
  } else {
    index.entries.push(entry);
  }
  await saveIndex(index);
}

// -- Research Reports --

export async function saveResearch(feature: Feature, report: ResearchReport): Promise<string> {
  const dir = join(KB_ROOT, 'research');
  await mkdir(dir, { recursive: true });

  const filename = `${feature.id}.md`;
  const filepath = join(dir, filename);

  const content = [
    `# Research: ${feature.name}`,
    '',
    `**Date**: ${new Date().toISOString()}`,
    `**Priority**: ${feature.priority}`,
    `**Repos**: ${feature.repos.join(', ')}`,
    `**Discord Parity**: ${feature.discordParity}`,
    '',
    '---',
    '',
    report.fullReport,
    '',
    '---',
    '',
    '## Research Topics Covered',
    ...report.topics.map(t => `- **${t.topic}**`),
    '',
    '## Links & References',
    '*(Auto-extracted from research findings)*',
    '',
    extractLinks(report.fullReport).map(l => `- ${l}`).join('\n'),
  ].join('\n');

  await writeFile(filepath, content, 'utf-8');

  await addEntry({
    id: `research-${feature.id}`,
    type: 'research',
    title: `Research: ${feature.name}`,
    feature: feature.id,
    createdAt: new Date().toISOString(),
    tags: feature.researchTopics.slice(0, 5),
    path: `research/${filename}`,
    summary: `${report.topics.length} topics researched for ${feature.name}`,
  });

  return filepath;
}

// -- PRD Summaries --

export async function savePRDSummary(feature: Feature, prd: GeneratedPRD): Promise<string> {
  const dir = join(KB_ROOT, 'prds');
  await mkdir(dir, { recursive: true });

  const filename = `${feature.id}.md`;
  const filepath = join(dir, filename);

  const content = [
    `# PRD Summary: ${feature.name}`,
    '',
    `**Date**: ${new Date().toISOString()}`,
    `**Full PRD**: \`hearth/PRDs/${prd.filename}\``,
    `**Priority**: ${feature.priority}`,
    `**Repos**: ${feature.repos.join(', ')}`,
    '',
    '---',
    '',
    '## Key Decisions',
    '*(Extracted from PRD)*',
    '',
    extractSections(prd.content, ['Technical Design', 'Architecture', 'API Spec']),
    '',
    '## Acceptance Criteria Summary',
    extractSections(prd.content, ['Acceptance Criteria', 'Success Metrics']),
    '',
    '## Implementation Phases',
    extractSections(prd.content, ['Implementation Plan', 'Phased Rollout', 'Migration']),
  ].join('\n');

  await writeFile(filepath, content, 'utf-8');

  await addEntry({
    id: `prd-${feature.id}`,
    type: 'prd',
    title: `PRD: ${feature.name}`,
    feature: feature.id,
    createdAt: new Date().toISOString(),
    tags: ['prd', feature.priority, ...feature.repos],
    path: `prds/${filename}`,
    summary: `PRD created for ${feature.name}, saved to hearth/PRDs/${prd.filename}`,
  });

  return filepath;
}

// -- Implementation Notes --

export async function saveImplementationNotes(
  feature: Feature,
  results: ImplementationResult[],
): Promise<string> {
  const dir = join(KB_ROOT, 'implementations');
  await mkdir(dir, { recursive: true });

  const filename = `${feature.id}.md`;
  const filepath = join(dir, filename);

  const content = [
    `# Implementation: ${feature.name}`,
    '',
    `**Date**: ${new Date().toISOString()}`,
    `**Status**: ${results.every(r => r.success) ? 'Success' : 'Partial'}`,
    '',
    '---',
    '',
    ...results.map(r => [
      `## ${r.repo}`,
      '',
      `- **Branch**: \`${r.branch}\``,
      `- **Success**: ${r.success ? 'Yes' : 'No'}`,
      `- **Files Changed**: ${r.filesChanged.length}`,
      '',
      '### Files Modified',
      r.filesChanged.map(f => `- \`${f}\``).join('\n'),
      '',
      '### Agent Output Summary',
      '```',
      r.output.slice(-2000),
      '```',
      '',
    ].join('\n')),
    '',
    '## Patterns Learned',
    '*(What worked, what to repeat for similar features)*',
    '',
    '## Issues Encountered',
    '*(What went wrong, what to avoid next time)*',
  ].join('\n');

  await writeFile(filepath, content, 'utf-8');

  await addEntry({
    id: `impl-${feature.id}`,
    type: 'implementation',
    title: `Implementation: ${feature.name}`,
    feature: feature.id,
    createdAt: new Date().toISOString(),
    tags: ['implementation', ...results.map(r => r.repo)],
    path: `implementations/${filename}`,
    summary: `Implemented in ${results.length} repo(s): ${results.map(r => `${r.repo} (${r.filesChanged.length} files)`).join(', ')}`,
  });

  return filepath;
}

// -- Next Steps Generator --

export async function generateNextSteps(
  completedFeatures: Feature[],
  remainingFeatures: Feature[],
): Promise<string> {
  const dir = join(KB_ROOT, 'next-steps');
  await mkdir(dir, { recursive: true });

  const timestamp = new Date().toISOString().split('T')[0];
  const filename = `${timestamp}-next-steps.md`;
  const filepath = join(dir, filename);

  const content = [
    `# Next Steps - ${timestamp}`,
    '',
    `**Completed**: ${completedFeatures.length} features`,
    `**Remaining**: ${remainingFeatures.length} features`,
    '',
    '---',
    '',
    '## Upcoming Features',
    ...remainingFeatures.slice(0, 5).map((f, i) => [
      `### ${i + 1}. ${f.name} (${f.priority})`,
      f.description,
      `**Repos**: ${f.repos.join(', ')}`,
      `**Research needed**: ${f.researchTopics.length} topics`,
      '',
    ].join('\n')),
    '',
    '## Suggested Research Topics',
    '*(New topics discovered during development)*',
    '',
    ...generateSuggestedTopics(completedFeatures),
    '',
    '## Infrastructure Improvements',
    '- [ ] Add quality gates (TDD, static analysis, AI review before PR)',
    '- [ ] Notification batching for Telegram updates',
    '- [ ] Message threading per feature in Telegram',
    '- [ ] Docker sandboxing for agent execution',
    '- [ ] Agent memory system (learn from past PRs)',
    '- [ ] Kimi K2.5 context caching optimization',
    '',
    '## Competitive Intelligence',
    '- [ ] Monitor Discord changelog for new features',
    '- [ ] Track Revolt/Element feature releases',
    '- [ ] Watch Matrix spec updates (currently v1.16)',
    '- [ ] Monitor LiveKit releases for voice/video improvements',
  ].join('\n');

  await writeFile(filepath, content, 'utf-8');

  const index = await loadIndex();
  index.nextSteps = remainingFeatures.slice(0, 5).map(f => f.name);
  index.researchQueue = remainingFeatures
    .flatMap(f => f.researchTopics.slice(0, 2))
    .slice(0, 10);
  await saveIndex(index);

  return filepath;
}

// -- Guide Generator --

export async function saveGuide(
  title: string,
  slug: string,
  content: string,
  tags: string[],
): Promise<string> {
  const dir = join(KB_ROOT, 'guides');
  await mkdir(dir, { recursive: true });

  const filename = `${slug}.md`;
  const filepath = join(dir, filename);

  const fullContent = [
    `# ${title}`,
    '',
    `**Generated**: ${new Date().toISOString()}`,
    `**Tags**: ${tags.join(', ')}`,
    '',
    '---',
    '',
    content,
  ].join('\n');

  await writeFile(filepath, fullContent, 'utf-8');

  await addEntry({
    id: `guide-${slug}`,
    type: 'guide',
    title,
    feature: 'general',
    createdAt: new Date().toISOString(),
    tags,
    path: `guides/${filename}`,
    summary: title,
  });

  return filepath;
}

// -- Query --

export async function searchKnowledge(query: string): Promise<KBEntry[]> {
  const index = await loadIndex();
  const q = query.toLowerCase();
  return index.entries.filter(e =>
    e.title.toLowerCase().includes(q) ||
    e.summary.toLowerCase().includes(q) ||
    e.tags.some(t => t.toLowerCase().includes(q)) ||
    e.feature.toLowerCase().includes(q)
  );
}

export async function getKnowledgeSummary(): Promise<string> {
  const index = await loadIndex();
  const byType = {
    research: index.entries.filter(e => e.type === 'research').length,
    prd: index.entries.filter(e => e.type === 'prd').length,
    implementation: index.entries.filter(e => e.type === 'implementation').length,
    guide: index.entries.filter(e => e.type === 'guide').length,
  };

  return [
    `Knowledge Base: ${index.totalEntries} entries`,
    `Research: ${byType.research} | PRDs: ${byType.prd} | Implementations: ${byType.implementation} | Guides: ${byType.guide}`,
    `Next steps: ${index.nextSteps.slice(0, 3).join(', ')}`,
    `Research queue: ${index.researchQueue.slice(0, 3).join(', ')}`,
    `Last updated: ${index.lastUpdated}`,
  ].join('\n');
}

// -- Helpers --

function extractLinks(text: string): string[] {
  const urlRegex = /https?:\/\/[^\s)\]>"']+/g;
  const matches = text.match(urlRegex) || [];
  return [...new Set(matches)].slice(0, 30);
}

function extractSections(text: string, headers: string[]): string {
  for (const header of headers) {
    const regex = new RegExp(`##?#?\\s*${header}[\\s\\S]*?(?=\\n##|$)`, 'i');
    const match = text.match(regex);
    if (match) return match[0].trim();
  }
  return '*(Section not found in PRD)*';
}

function generateSuggestedTopics(completed: Feature[]): string[] {
  const suggestions: string[] = [];

  if (completed.some(f => f.id === 'matrix-federation')) {
    suggestions.push(
      '- Matrix bridge ecosystem (WhatsApp, Signal, IRC bridges)',
      '- Matrix moderation tools (Mjolnir/Draupnir)',
      '- Matrix identity server integration',
    );
  }

  if (completed.some(f => f.id === 'voice-channels-always-on')) {
    suggestions.push(
      '- Noise suppression ML models (RNNoise, Krisp alternatives)',
      '- LiveKit egress for recording voice channels',
      '- Spatial audio for voice channels',
    );
  }

  // Always suggest these
  suggestions.push(
    '- Discord bot API compatibility layer',
    '- Accessibility audit (WCAG 2.1 AA compliance)',
    '- Performance benchmarking vs Discord/Element',
    '- Mobile offline mode and sync',
    '- Plugin/extension system architecture',
  );

  return suggestions;
}
