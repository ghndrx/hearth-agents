// Engineering standards enforced by hearth-agents across all repos.
// These match CONTRIBUTING.md in the hearth repo and apply to
// hearth, hearth-desktop, and hearth-mobile.

export const BRANCH_CONVENTIONS = {
  // Branch naming: type/short-description (from develop)
  prefixes: {
    feature: 'feat/',       // New features
    fix: 'fix/',            // Bug fixes
    hotfix: 'hotfix/',      // Urgent production fixes
    docs: 'docs/',          // Documentation only
    refactor: 'refactor/',  // Code restructuring
    test: 'test/',          // Test additions
    chore: 'chore/',        // Maintenance
  },
  base: 'develop',
  pattern: /^(feat|fix|hotfix|docs|refactor|test|chore)\/[a-z0-9][a-z0-9-]*$/,
  maxLength: 50,
} as const;

export const COMMIT_CONVENTIONS = {
  // Conventional Commits: type(scope): description
  types: ['feat', 'fix', 'docs', 'style', 'refactor', 'test', 'chore', 'perf', 'ci'] as const,
  pattern: /^(feat|fix|docs|style|refactor|test|chore|perf|ci)(\([a-z-]+\))?: .{3,72}$/,
  maxSubjectLength: 72,
  rules: [
    'Subject line: imperative mood, lowercase, no period',
    'Body: explain WHY, not WHAT (the diff shows what)',
    'Footer: reference issues with "Closes #123"',
    'NEVER add Co-Authored-By or AI attribution',
  ],
} as const;

export const PR_CONVENTIONS = {
  titlePattern: /^(feat|fix|docs|refactor|chore|test|perf|ci)(\([a-z-]+\))?: .{3,70}$/,
  template: `## Description
Brief description of what this PR does and why.

## Changes
- List specific changes made

## Type
- [ ] Feature
- [ ] Bug fix
- [ ] Refactor
- [ ] Documentation

## Testing
- [ ] Tests pass locally (\`go test ./...\` or \`npm test\`)
- [ ] Linter passes (\`golangci-lint run\` or \`npm run lint\`)
- [ ] Build succeeds (\`go build ./...\` or \`npm run build\`)

## Checklist
- [ ] No TODO/FIXME/placeholder code
- [ ] No hardcoded secrets or credentials
- [ ] No generated markdown or AI slop
- [ ] Comments explain WHY, not WHAT
- [ ] Follows existing code patterns`,
  base: 'develop',
} as const;

export const CODE_QUALITY = {
  // What NEVER goes into a product repo
  forbidden: {
    files: [
      '*.md',         // except README, CHANGELOG, CONTRIBUTING, AGENTS.md, LICENSE, SECURITY, CODE_OF_CONDUCT
      '*.log',
      '*.bak',
      '*.tmp',
      '*.env',
      'research*',
      'prd*',
      'knowledge*',
    ],
    allowedMd: [
      'README.md', 'CHANGELOG.md', 'CONTRIBUTING.md', 'AGENTS.md',
      'LICENSE.md', 'SECURITY.md', 'CODE_OF_CONDUCT.md', 'SPEC.md',
    ],
    codePatterns: [
      'TODO:', 'FIXME:', 'XXX:', 'HACK:',
      'not implemented',
      'placeholder',
      '// ...',
    ],
    secrets: [
      /sk-[a-zA-Z0-9_-]{20,}/,
      /AKIA[0-9A-Z]{16}/,
      /-----BEGIN.*PRIVATE KEY/,
      /password\s*[:=]\s*["'][^"']+["']/i,
    ],
  },
  // What IS expected
  required: {
    go: [
      'Error wrapping: fmt.Errorf("context: %w", err)',
      'Exported functions have doc comments',
      'Parameterized queries (never string interpolation)',
      'Context propagation (context.Context as first param)',
    ],
    typescript: [
      'Strict mode (no any unless justified)',
      'Explicit return types on exported functions',
      'Error handling (try/catch, not silent failures)',
      'Props interfaces for Svelte components',
    ],
  },
} as const;

export const TESTING_STANDARDS = {
  go: {
    command: 'go test ./... -v -count=1',
    coverageTarget: 80,
    naming: 'Test<FunctionName>_<Scenario>',
  },
  typescript: {
    command: 'npx vitest run',
    coverageTarget: 80,
    naming: 'describe("<Component>") > it("should <behavior>")',
  },
  e2e: {
    command: 'npx playwright test',
    naming: 'test("<user story>")',
  },
} as const;

export const DEPENDENCY_POLICY = {
  rules: [
    'Run npm audit / go vet before every PR',
    'No high or critical vulnerabilities allowed',
    'Pin major versions in package.json (^x.y.z)',
    'Update dependencies monthly',
    'Prefer well-maintained packages (>1000 stars, recent commits)',
    'No unnecessary dependencies - check if stdlib can do it',
  ],
  goCheck: 'govulncheck ./...',
  tsCheck: 'npm audit --audit-level=high',
} as const;

// Generate branch name from feature description
export function generateBranchName(type: string, description: string): string {
  const prefix = BRANCH_CONVENTIONS.prefixes[type as keyof typeof BRANCH_CONVENTIONS.prefixes] || 'feat/';
  const slug = description
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .slice(0, BRANCH_CONVENTIONS.maxLength - prefix.length);
  return `${prefix}${slug}`;
}

// Generate commit message
export function generateCommitMessage(type: string, scope: string, description: string): string {
  const scopePart = scope ? `(${scope})` : '';
  return `${type}${scopePart}: ${description.toLowerCase().slice(0, COMMIT_CONVENTIONS.maxSubjectLength)}`;
}

// Validate branch name
export function isValidBranchName(name: string): boolean {
  return BRANCH_CONVENTIONS.pattern.test(name);
}

// Validate commit message
export function isValidCommitMessage(message: string): boolean {
  const firstLine = message.split('\n')[0];
  return COMMIT_CONVENTIONS.pattern.test(firstLine);
}
