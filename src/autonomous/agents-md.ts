// AGENTS.md manager: maintains a living knowledge file in each product repo.
// Accumulates patterns, gotchas, conventions, and architecture notes
// as agents work, enabling compound learning across sessions.

import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const AGENTS_FILE = 'AGENTS.md';
const MAX_RECENT_CHANGES = 10;

const SECTIONS = [
  'Project Conventions',
  'Known Gotchas',
  'Architecture Notes',
  'Testing Patterns',
  'Recent Changes',
] as const;

type Section = (typeof SECTIONS)[number];

const TEMPLATE = `# AGENTS.md

Living knowledge base maintained by autonomous agents.
Do not edit manually unless correcting inaccuracies.

## Project Conventions

## Known Gotchas

## Architecture Notes

## Testing Patterns

## Recent Changes
`;

// -- Core Read/Write --

export async function readAgentsMd(repoPath: string): Promise<string> {
  const filepath = join(repoPath, AGENTS_FILE);
  try {
    return await readFile(filepath, 'utf-8');
  } catch {
    await writeFile(filepath, TEMPLATE, 'utf-8');
    return TEMPLATE;
  }
}

async function writeAgentsMd(repoPath: string, content: string): Promise<void> {
  await writeFile(join(repoPath, AGENTS_FILE), content, 'utf-8');
}

// -- Section Parsing --

function findSection(content: string, section: Section): { start: number; end: number } {
  const header = `## ${section}`;
  const start = content.indexOf(header);
  if (start === -1) return { start: -1, end: -1 };

  const afterHeader = start + header.length;

  // Find next ## heading or end of file
  const nextSection = content.indexOf('\n## ', afterHeader);
  const end = nextSection === -1 ? content.length : nextSection;

  return { start: afterHeader, end };
}

function getSectionContent(content: string, section: Section): string {
  const { start, end } = findSection(content, section);
  if (start === -1) return '';
  return content.slice(start, end).trim();
}

function replaceSection(content: string, section: Section, newBody: string): string {
  const header = `## ${section}`;
  const { start, end } = findSection(content, section);

  if (start === -1) {
    // Section missing -- append it
    return content.trimEnd() + `\n\n${header}\n\n${newBody}\n`;
  }

  return content.slice(0, start) + '\n\n' + newBody + '\n' + content.slice(end);
}

// -- Deduplication --

function isDuplicate(existing: string, entry: string): boolean {
  // Normalize for comparison: strip leading bullet/dash, trim whitespace
  const normalize = (s: string) =>
    s.replace(/^[-*]\s*/, '').replace(/\s+/g, ' ').trim().toLowerCase();
  const normalizedEntry = normalize(entry);

  return existing
    .split('\n')
    .some(line => normalize(line) === normalizedEntry);
}

// -- Public API --

export async function appendPattern(
  repoPath: string,
  section: string,
  entry: string,
): Promise<void> {
  // Validate section name
  const validSection = SECTIONS.find(
    s => s.toLowerCase() === section.toLowerCase(),
  );
  if (!validSection) {
    throw new Error(
      `Invalid section "${section}". Valid: ${SECTIONS.join(', ')}`,
    );
  }

  let content = await readAgentsMd(repoPath);
  const existing = getSectionContent(content, validSection);
  const bulletEntry = entry.startsWith('-') ? entry : `- ${entry}`;

  if (isDuplicate(existing, bulletEntry)) return;

  const updated = existing ? `${existing}\n${bulletEntry}` : bulletEntry;
  content = replaceSection(content, validSection, updated);
  await writeAgentsMd(repoPath, content);
}

export async function appendGotcha(
  repoPath: string,
  entry: string,
): Promise<void> {
  await appendPattern(repoPath, 'Known Gotchas', entry);
}

export async function appendRecentChange(
  repoPath: string,
  feature: string,
  summary: string,
): Promise<void> {
  let content = await readAgentsMd(repoPath);
  const existing = getSectionContent(content, 'Recent Changes');

  const date = new Date().toISOString().split('T')[0];
  const newEntry = `- **${date}** - ${feature}: ${summary}`;

  // Parse existing entries, prepend new one, cap at MAX_RECENT_CHANGES
  const lines = existing
    .split('\n')
    .filter(line => line.startsWith('- '));
  lines.unshift(newEntry);
  const trimmed = lines.slice(0, MAX_RECENT_CHANGES);

  content = replaceSection(content, 'Recent Changes', trimmed.join('\n'));
  await writeAgentsMd(repoPath, content);
}

export async function getAgentContext(repoPath: string): Promise<string> {
  return readAgentsMd(repoPath);
}
