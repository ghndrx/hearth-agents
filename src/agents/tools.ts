// Tool definitions for MiniMax M2.7 function calling.
// These define what each agent role can do via tool use.

import { ToolDefinition } from '../types/index.js';

const readFile: ToolDefinition = {
  type: 'function',
  function: {
    name: 'read_file',
    description: 'Read the contents of a file at the given path',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'Absolute or relative file path' },
      },
      required: ['path'],
    },
  },
};

const writeFile: ToolDefinition = {
  type: 'function',
  function: {
    name: 'write_file',
    description: 'Write content to a file, creating it if it does not exist',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'File path to write to' },
        content: { type: 'string', description: 'Content to write' },
      },
      required: ['path', 'content'],
    },
  },
};

const editFile: ToolDefinition = {
  type: 'function',
  function: {
    name: 'edit_file',
    description: 'Replace a specific string in a file with new content',
    parameters: {
      type: 'object',
      properties: {
        path: { type: 'string', description: 'File path to edit' },
        old_string: { type: 'string', description: 'Exact string to find and replace' },
        new_string: { type: 'string', description: 'Replacement string' },
      },
      required: ['path', 'old_string', 'new_string'],
    },
  },
};

const listFiles: ToolDefinition = {
  type: 'function',
  function: {
    name: 'list_files',
    description: 'List files matching a glob pattern',
    parameters: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Glob pattern (e.g. "**/*.go", "src/**/*.ts")' },
        path: { type: 'string', description: 'Directory to search in' },
      },
      required: ['pattern'],
    },
  },
};

const searchFiles: ToolDefinition = {
  type: 'function',
  function: {
    name: 'search_files',
    description: 'Search file contents for a regex pattern',
    parameters: {
      type: 'object',
      properties: {
        pattern: { type: 'string', description: 'Regex pattern to search for' },
        path: { type: 'string', description: 'Directory to search in' },
        glob: { type: 'string', description: 'File glob filter (e.g. "*.go")' },
      },
      required: ['pattern'],
    },
  },
};

const runCommand: ToolDefinition = {
  type: 'function',
  function: {
    name: 'run_command',
    description: 'Execute a shell command and return its output',
    parameters: {
      type: 'object',
      properties: {
        command: { type: 'string', description: 'Shell command to execute' },
        cwd: { type: 'string', description: 'Working directory' },
      },
      required: ['command'],
    },
  },
};

const gitOperation: ToolDefinition = {
  type: 'function',
  function: {
    name: 'git',
    description: 'Run a git command (e.g. status, diff, add, commit, branch, checkout)',
    parameters: {
      type: 'object',
      properties: {
        args: { type: 'string', description: 'Git subcommand and arguments (e.g. "status", "diff HEAD~1", "commit -m msg")' },
        cwd: { type: 'string', description: 'Repository directory' },
      },
      required: ['args'],
    },
  },
};

const webSearch: ToolDefinition = {
  type: 'function',
  function: {
    name: 'web_search',
    description: 'Search the web for documentation, examples, and current best practices',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Search query' },
      },
      required: ['query'],
    },
  },
};

// Wikidelve knowledge base tools - agents can research before coding
const wikidelveSearch: ToolDefinition = {
  type: 'function',
  function: {
    name: 'wikidelve_search',
    description: 'Search the Hearth knowledge base for existing research, architecture decisions, implementation patterns, and technical documentation. Use this BEFORE implementing to understand existing patterns.',
    parameters: {
      type: 'object',
      properties: {
        query: { type: 'string', description: 'Search query (e.g. "WebSocket scaling patterns", "PostgreSQL migration strategy")' },
      },
      required: ['query'],
    },
  },
};

const wikidelveResearch: ToolDefinition = {
  type: 'function',
  function: {
    name: 'wikidelve_research',
    description: 'Start a deep research job on a topic. Returns a job ID. Use this when you need in-depth knowledge about a technology, library, or pattern before implementing.',
    parameters: {
      type: 'object',
      properties: {
        topic: { type: 'string', description: 'Research topic (min 10 chars, e.g. "LiveKit WebRTC screen sharing implementation in Go")' },
      },
      required: ['topic'],
    },
  },
};

const wikidelveRead: ToolDefinition = {
  type: 'function',
  function: {
    name: 'wikidelve_read',
    description: 'Read a specific article from the knowledge base by its slug. Use after wikidelve_search returns results.',
    parameters: {
      type: 'object',
      properties: {
        slug: { type: 'string', description: 'Article slug from search results' },
        kb: { type: 'string', description: 'Knowledge base name (default: personal)' },
      },
      required: ['slug'],
    },
  },
};

const readOnlyTools: ToolDefinition[] = [readFile, listFiles, searchFiles, gitOperation];

const wikidelveTools: ToolDefinition[] = [wikidelveSearch, wikidelveRead, wikidelveResearch];

const writerTools: ToolDefinition[] = [...readOnlyTools, writeFile, editFile, runCommand];

export const AGENT_TOOLS = {
  readOnly: readOnlyTools,
  prdWriter: [...readOnlyTools, writeFile, webSearch, ...wikidelveTools],
  developer: [...writerTools, webSearch, ...wikidelveTools],
  researcher: [readFile, listFiles, searchFiles, webSearch, ...wikidelveTools],
};
