// Feature backlog: Discord features to implement in Hearth.
// The autonomous agent works through this list, creating PRDs and implementing each.

export interface Feature {
  id: string;
  name: string;
  description: string;
  priority: 'critical' | 'high' | 'medium' | 'low';
  repos: ('hearth' | 'hearth-desktop' | 'hearth-mobile')[];
  researchTopics: string[];
  discordParity: string;
  status: 'pending' | 'researching' | 'prd' | 'implementing' | 'reviewing' | 'done';
}

// Features ordered by priority - Matrix federation + E2EE is the top priority
export const FEATURE_BACKLOG: Feature[] = [
  {
    id: 'matrix-federation',
    name: 'Matrix Federation for E2EE',
    description: 'Implement Matrix protocol federation enabling end-to-end encrypted messaging across federated homeservers. Replace or augment the existing Signal protocol E2EE with Matrix Megolm/Vodozemac for group encryption, enabling cross-server communication while maintaining E2EE guarantees.',
    priority: 'critical',
    repos: ['hearth'],
    researchTopics: [
      'Matrix protocol federation server-to-server API',
      'Matrix Megolm and Vodozemac encryption for group chats',
      'Matrix room model event system state resolution',
      'Synapse vs Conduit vs Dendrite homeserver comparison 2026',
      'Matrix Simplified Sliding Sync MSC4186',
      'Matrix spaces as Discord server guild hierarchy',
      'MatrixRTC LiveKit integration for voice video',
      'Matrix cross-signing device verification',
      'Building custom Matrix clients with matrix-rust-sdk',
    ],
    discordParity: 'Server federation (Discord lacks this - this is a competitive advantage)',
    status: 'pending',
  },
  {
    id: 'voice-channels-always-on',
    name: 'Always-On Voice Channels',
    description: 'Discord-style persistent voice channels where users can drop in/out. Show who is currently in each voice channel in the sidebar. Support mute/deafen, push-to-talk, voice activity detection.',
    priority: 'high',
    repos: ['hearth', 'hearth-desktop'],
    researchTopics: [
      'LiveKit room management persistent voice channels',
      'WebRTC voice activity detection VAD',
      'Discord voice channel UX patterns',
      'LiveKit egress recording for voice channels',
    ],
    discordParity: 'Core Discord feature - voice channels with user presence',
    status: 'pending',
  },
  {
    id: 'screen-sharing',
    name: 'Screen Sharing & Go Live',
    description: 'Screen sharing in voice channels and DMs. Support application window sharing, full screen, with audio. Discord-style "Go Live" streaming to voice channels.',
    priority: 'high',
    repos: ['hearth', 'hearth-desktop'],
    researchTopics: [
      'LiveKit screen sharing tracks',
      'WebRTC getDisplayMedia API',
      'Electron desktopCapturer for screen sharing',
      'Discord Go Live streaming UX',
    ],
    discordParity: 'Screen sharing / Go Live in voice channels',
    status: 'pending',
  },
  {
    id: 'server-roles-permissions',
    name: 'Advanced Role & Permission System',
    description: 'Discord-style role hierarchy with color-coded roles, per-channel permission overrides, role-based access control for voice/text channels, drag-to-reorder role priority.',
    priority: 'high',
    repos: ['hearth'],
    researchTopics: [
      'Discord role permission bit flags system',
      'Role hierarchy and permission inheritance',
      'Per-channel permission overrides design patterns',
    ],
    discordParity: 'Full Discord role/permission system',
    status: 'pending',
  },
  {
    id: 'message-search',
    name: 'Full-Text Message Search',
    description: 'Search messages across channels with filters: from user, in channel, has attachment, date range. Results with context and jump-to-message.',
    priority: 'high',
    repos: ['hearth'],
    researchTopics: [
      'PostgreSQL full-text search with tsvector',
      'Search with E2EE messages client-side search',
      'Discord search syntax and filters',
    ],
    discordParity: 'Discord message search with filters',
    status: 'pending',
  },
  {
    id: 'user-status-activities',
    name: 'Custom Status & Rich Presence',
    description: 'Custom status messages with emoji, rich presence showing what users are playing/listening to, automatic status (online/idle/dnd/invisible).',
    priority: 'medium',
    repos: ['hearth', 'hearth-desktop'],
    researchTopics: [
      'Discord rich presence protocol',
      'Activity detection on desktop',
      'WebSocket presence broadcasting at scale',
    ],
    discordParity: 'Custom status + rich presence',
    status: 'pending',
  },
  {
    id: 'server-discovery',
    name: 'Server Discovery & Invites',
    description: 'Public server directory, vanity invite URLs, server templates, server preview before joining, member counts and descriptions.',
    priority: 'medium',
    repos: ['hearth'],
    researchTopics: [
      'Discord server discovery API',
      'Server listing and categorization',
      'Invite link management and analytics',
    ],
    discordParity: 'Discord server discovery',
    status: 'pending',
  },
  {
    id: 'mobile-push-notifications',
    name: 'Mobile Push Notifications',
    description: 'Push notifications for mobile apps (iOS APNs, Android FCM). Per-channel notification settings, mention notifications, DM notifications.',
    priority: 'high',
    repos: ['hearth', 'hearth-mobile'],
    researchTopics: [
      'APNs push notification service iOS',
      'Firebase Cloud Messaging Android',
      'Push notification with E2EE encrypted payload',
      'Per-channel notification preferences',
    ],
    discordParity: 'Mobile push notifications',
    status: 'pending',
  },
  {
    id: 'desktop-native-features',
    name: 'Desktop App Native Features',
    description: 'System tray, notifications, auto-start, hardware acceleration, keyboard shortcuts, overlay for games.',
    priority: 'medium',
    repos: ['hearth-desktop'],
    researchTopics: [
      'Electron system tray and notifications',
      'Tauri vs Electron for desktop chat apps 2026',
      'Desktop overlay rendering for games',
    ],
    discordParity: 'Desktop app features (tray, overlay, shortcuts)',
    status: 'pending',
  },
  {
    id: 'webhooks-integrations',
    name: 'Webhooks & Bot API',
    description: 'Incoming/outgoing webhooks, bot user accounts, OAuth2 for bot authorization, slash command registration API, message components (buttons, selects).',
    priority: 'medium',
    repos: ['hearth'],
    researchTopics: [
      'Discord webhook API design',
      'Bot OAuth2 flow and permissions',
      'Interactive message components API design',
    ],
    discordParity: 'Discord bot API and webhooks',
    status: 'pending',
  },
  {
    id: 'security-hardening',
    name: 'Security Hardening & CVE Monitoring',
    description: 'Comprehensive security audit and hardening: CSP headers, rate limiting per-endpoint, SQL injection prevention audit, XSS prevention in Svelte components, CSRF token validation, WebSocket authentication hardening, Redis ACL configuration, PostgreSQL row-level security for multi-tenant data isolation.',
    priority: 'critical',
    repos: ['hearth'],
    researchTopics: [
      'Go web application security OWASP Top 10 prevention',
      'WebSocket security authentication rate limiting DDoS',
      'PostgreSQL row level security multi-tenant chat',
      'SvelteKit CSP headers XSS CSRF prevention',
      'Redis ACL authentication TLS configuration',
    ],
    discordParity: 'Security hardening (Hearth advantage: self-hosted, auditable)',
    status: 'pending',
  },
  {
    id: 'dependency-vulnerability-scanning',
    name: 'Automated Vulnerability Scanning Pipeline',
    description: 'Set up continuous CVE monitoring and automated vulnerability scanning across all repos. govulncheck for Go, npm audit for Node, cargo audit for Rust, Trivy for Docker images. Automated PRs for security patches. Snyk or GitHub security advisories integration.',
    priority: 'critical',
    repos: ['hearth', 'hearth-desktop', 'hearth-mobile'],
    researchTopics: [
      'govulncheck automated Go vulnerability scanning CI',
      'npm audit automated security patching workflow',
      'Docker container CVE scanning Trivy Grype',
      'Supply chain security SLSA provenance dependency confusion',
    ],
    discordParity: 'Security scanning (Hearth advantage: transparent, auditable security)',
    status: 'pending',
  },
  {
    id: 'e2ee-security-audit',
    name: 'E2EE Implementation Security Audit',
    description: 'Formal security review of the Signal Protocol E2EE implementation. Key storage audit, session management review, forward secrecy verification, key rotation policies, device verification UX. Prepare for third-party security audit.',
    priority: 'high',
    repos: ['hearth'],
    researchTopics: [
      'Signal Protocol implementation security audit checklist',
      'E2EE key storage best practices Go backend',
      'Forward secrecy verification testing methodology',
      'Preparing for third-party security audit open source chat',
    ],
    discordParity: 'E2EE audit (Discord has no text E2EE - major advantage)',
    status: 'pending',
  },
  {
    id: 'rate-limiting-ddos',
    name: 'Advanced Rate Limiting & DDoS Protection',
    description: 'Per-endpoint rate limiting with Redis sliding window, WebSocket connection limits per IP, message flood protection, file upload rate limiting, API key rate limiting for bot developers, Cloudflare/fail2ban integration for self-hosters.',
    priority: 'high',
    repos: ['hearth'],
    researchTopics: [
      'Redis sliding window rate limiting Go implementation',
      'WebSocket DDoS protection connection limiting',
      'API rate limiting design patterns for chat applications',
      'Self-hosted DDoS protection fail2ban Cloudflare',
    ],
    discordParity: 'Rate limiting (Discord has sophisticated per-route limits)',
    status: 'pending',
  },
];

export function getNextFeature(): Feature | null {
  return FEATURE_BACKLOG.find(f => f.status === 'pending') ?? null;
}

export function updateFeatureStatus(id: string, status: Feature['status']): void {
  const feature = FEATURE_BACKLOG.find(f => f.id === id);
  if (feature) feature.status = status;
}

export function addFeature(feature: Feature): void {
  // Don't add duplicates
  if (FEATURE_BACKLOG.some(f => f.id === feature.id)) return;
  FEATURE_BACKLOG.push(feature);
}

export function getBacklogStats(): { pending: number; done: number; total: number } {
  return {
    pending: FEATURE_BACKLOG.filter(f => f.status === 'pending').length,
    done: FEATURE_BACKLOG.filter(f => f.status === 'done').length,
    total: FEATURE_BACKLOG.length,
  };
}
