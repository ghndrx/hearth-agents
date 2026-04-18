"""Static HTML for the kanban UI.

Served from ``GET /kanban``. Alpine.js + Tailwind CDN, no build step.
Columns: pending | implementing (incl. researching, reviewing) | blocked
| escalated | done.

Design: dark GitHub-ish theme, Inter typography, subtle glass-morphism
on modals, Heroicons inline SVG. Single-page; refresh every 10s.
"""

KANBAN_HTML = r"""<!doctype html>
<html lang="en" class="h-full">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>hearth-agents</title>
<script src="https://cdn.tailwindcss.com?plugins=forms"></script>
<link rel="preconnect" href="https://rsms.me/">
<link rel="stylesheet" href="https://rsms.me/inter/inter.css">
<script defer src="https://unpkg.com/alpinejs@3.14.1/dist/cdn.min.js"></script>
<style>
  :root { font-family: 'Inter', system-ui, sans-serif; }
  @supports (font-variation-settings: normal) {
    :root { font-family: 'Inter var', system-ui, sans-serif; }
  }
  .scrollbar-thin::-webkit-scrollbar { width: 6px; height: 6px; }
  .scrollbar-thin::-webkit-scrollbar-track { background: transparent; }
  .scrollbar-thin::-webkit-scrollbar-thumb { background: #30363d; border-radius: 3px; }
  .scrollbar-thin::-webkit-scrollbar-thumb:hover { background: #484f58; }
  .glass { backdrop-filter: blur(12px); background: rgba(13, 17, 23, 0.85); }
  @keyframes slideup { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .card-enter { animation: slideup 0.18s ease-out; }
</style>
<script>
  tailwind.config = {
    theme: {
      extend: {
        colors: {
          bg: '#0d1117', surface: '#161b22', border: '#30363d',
          fg: '#e6edf3', muted: '#8b949e', accent: '#58a6ff',
          done: '#3fb950', pending: '#8b949e', impl: '#58a6ff',
          blocked: '#f85149', escalated: '#bc8cff',
          crit: '#f85149', high: '#f0883e', med: '#d29922', low: '#8b949e',
        },
        boxShadow: {
          card: '0 1px 0 rgba(0,0,0,0.25), 0 4px 12px rgba(0,0,0,0.12)',
          modal: '0 20px 48px rgba(0,0,0,0.5)',
        },
      },
    },
  };
</script>
</head>
<body class="h-full bg-bg text-fg antialiased" x-data="kanban()" x-init="init()">

<!-- Header -->
<header class="border-b border-border bg-surface/60 backdrop-blur">
  <div class="px-5 py-3 flex items-center gap-3 flex-wrap">
    <div class="flex items-center gap-2">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="w-5 h-5 text-accent">
        <path d="M12.963 2.286a.75.75 0 00-1.071-.136 9.742 9.742 0 00-3.539 6.177A7.547 7.547 0 016.648 6.61a.75.75 0 00-1.152-.082A9 9 0 1015.68 4.534a7.46 7.46 0 01-2.717-2.248zM15.75 14.25a3.75 3.75 0 11-7.313-1.172c.628.465 1.35.81 2.133 1a5.99 5.99 0 011.925-3.545 3.75 3.75 0 013.255 3.717z" />
      </svg>
      <h1 class="text-sm font-semibold tracking-tight">hearth-agents</h1>
      <span class="text-xs text-muted">kanban</span>
    </div>

    <!-- worker badges -->
    <div class="flex items-center gap-1.5 flex-wrap">
      <template x-for="w in workerBadges" :key="w.id">
        <span :class="'inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-medium rounded-md ' + (w.age < 120 ? 'bg-done/20 text-done' : w.age < 600 ? 'bg-high/20 text-high' : 'bg-blocked/20 text-blocked')"
              :title="'worker ' + w.id + ' · beat ' + w.age + 's ago'">
          <span class="w-1.5 h-1.5 rounded-full" :class="w.age < 120 ? 'bg-done' : w.age < 600 ? 'bg-high' : 'bg-blocked'"></span>
          <span x-text="'w' + w.id"></span>
          <span class="text-muted/80" x-text="(w.feature || '—').slice(0, 24)"></span>
        </span>
      </template>
    </div>

    <div class="flex items-center gap-2 ml-auto">
      <input type="search" placeholder="search id / name / repo" x-model="filterText"
             class="w-56 text-xs bg-bg border border-border rounded-md px-3 py-1.5 text-fg placeholder-muted focus:outline-none focus:border-accent transition-colors" />

      <select x-model="kindFilter"
              class="text-xs bg-bg border border-border rounded-md py-1.5 pl-2.5 pr-8 focus:outline-none focus:border-accent">
        <option value="">all kinds</option>
        <option>feature</option><option>bug</option><option>refactor</option>
        <option>schema</option><option>security</option><option>incident</option><option>perf-revert</option>
      </select>
      <select x-model="riskFilter"
              class="text-xs bg-bg border border-border rounded-md py-1.5 pl-2.5 pr-8 focus:outline-none focus:border-accent">
        <option value="">all risk</option>
        <option>low</option><option>medium</option><option>high</option>
      </select>

      <span class="text-[10px] text-muted font-mono" x-text="'refreshed ' + sinceLabel + 's ago'"></span>
      <button @click="refresh()" class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface transition-colors">refresh</button>
      <button @click="openAddModal()" class="text-xs px-3 py-1.5 rounded-md bg-done text-bg font-medium hover:bg-done/90 transition-colors">+ add</button>
      <button @click="loadAnalytics()" class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface">analytics</button>
      <button @click="loadSchedule()" class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface">schedule</button>
      <button @click="loadDepGraph()" class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface">deps</button>
      <button @click="loadSnapshots()" class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface">diff</button>
      <button @click="openViews = !openViews" class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface">views</button>
      <button :disabled="!blockedFeatures.length" @click="bulkApproveBlocked()"
              class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface disabled:opacity-40 disabled:cursor-not-allowed">
        approve blocked
      </button>
      <button :disabled="!escalatedFeatures.length" @click="bulkRetryEscalated()"
              class="text-xs px-2.5 py-1.5 rounded-md border border-border hover:bg-surface disabled:opacity-40 disabled:cursor-not-allowed">
        retry escalated
      </button>
    </div>
  </div>

  <!-- Saved views (localStorage) -->
  <div class="px-5 pb-2 flex items-center gap-2 flex-wrap" x-show="openViews">
    <span class="text-[10px] uppercase tracking-wider text-muted font-medium">views</span>
    <template x-for="v in savedViews" :key="v.name">
      <button @click="applyView(v)" class="text-[10px] px-2 py-0.5 rounded-md bg-surface border border-border hover:border-accent">
        <span x-text="v.name"></span>
        <span @click.stop="deleteView(v.name)" class="ml-1 text-muted hover:text-blocked cursor-pointer">×</span>
      </button>
    </template>
    <button @click="saveCurrentView()" class="text-[10px] px-2 py-0.5 rounded-md bg-done/20 text-done hover:bg-done/30" title="Save current filter + kind + risk as named view">+ save current</button>
  </div>

  <!-- Block-reasons strip -->
  <div class="px-5 pb-3 flex items-center gap-2 flex-wrap" x-show="stats && stats.block_reasons_top10 && stats.block_reasons_top10.length">
    <span class="text-[10px] uppercase tracking-wider text-muted font-medium">block reasons</span>
    <template x-for="r in (stats && stats.block_reasons_top10) || []" :key="r.reason">
      <button @click="filterText = r.reason.slice(0, 40)" class="inline-flex items-center gap-1.5 text-[10px] px-2 py-0.5 rounded-md bg-surface border border-border hover:border-accent cursor-pointer" :title="'click to filter to features matching this reason\n\n' + r.reason">
        <span class="text-blocked font-semibold" x-text="r.count"></span>
        <span class="text-muted" x-text="r.reason.slice(0, 50)"></span>
      </button>
    </template>
  </div>
</header>

<!-- Board -->
<main class="grid grid-cols-5 gap-3 p-4" style="height: calc(100vh - 110px);">
  <template x-for="col in columns" :key="col.key">
    <div class="flex flex-col min-h-0 bg-surface rounded-lg border border-border overflow-hidden">
      <div class="flex items-center gap-2 px-3 py-2 border-b border-border">
        <span class="w-2 h-2 rounded-full" :style="'background:' + col.color"></span>
        <span class="text-[11px] uppercase tracking-wider font-semibold" x-text="col.label"></span>
        <span class="ml-auto text-[11px] text-muted font-mono tabular-nums" x-text="featuresByColumn(col).length"></span>
      </div>
      <div class="flex-1 overflow-y-auto scrollbar-thin px-2 py-2 space-y-2">
        <!-- Virtualization: when a column has >100 cards, show only the
             first 100 + a "N more" summary. Rendering 300 cards × 5
             columns kills Alpine on big backlogs. -->
        <div class="text-[10px] text-muted text-center italic py-1" x-show="featuresByColumn(col).length > 100">
          showing first 100 of <span x-text="featuresByColumn(col).length"></span> — use filter to narrow
        </div>
        <template x-for="f in featuresByColumn(col).slice(0, 100)" :key="f.id">
          <div class="card-enter bg-bg rounded-md border border-border p-2.5 hover:border-accent/40 transition-colors cursor-pointer"
               :class="selectedId === f.id ? 'ring-2 ring-accent' : ''"
               @click="selectedId = f.id">
            <!-- Title row -->
            <div class="flex items-start gap-2 mb-1.5">
              <span class="flex-1 text-[13px] font-medium text-fg truncate" :title="f.name" x-text="f.name"></span>
              <span class="text-[9px] uppercase px-1.5 py-0.5 rounded tabular-nums font-semibold"
                    :class="{
                      'bg-crit/20 text-crit': f.priority === 'critical',
                      'bg-high/20 text-high': f.priority === 'high',
                      'bg-med/20 text-med': f.priority === 'medium',
                      'bg-low/20 text-low': f.priority === 'low',
                    }"
                    x-text="f.priority"></span>
            </div>

            <!-- Meta row -->
            <div class="flex items-center gap-1.5 mb-1.5 flex-wrap">
              <span class="font-mono text-[10px] text-muted" x-text="f.id"></span>
              <template x-if="f.self_improvement">
                <span class="text-[9px] px-1.5 py-0.5 rounded bg-escalated/20 text-escalated font-medium">self</span>
              </template>
              <template x-for="r in f.repos" :key="r">
                <span class="text-[9px] px-1.5 py-0.5 rounded bg-surface border border-border text-muted" x-text="r"></span>
              </template>
              <template x-if="f.heal_attempts > 0">
                <span class="text-[9px] px-1.5 py-0.5 rounded bg-high/10 text-high" x-text="'heal ' + f.heal_attempts + '/3'"></span>
              </template>
              <template x-if="f.depends_on && f.depends_on.length">
                <span class="text-[9px] px-1.5 py-0.5 rounded bg-accent/15 text-accent" :title="'depends on: ' + f.depends_on.join(', ')" x-text="'↘ ' + f.depends_on.length"></span>
              </template>
              <template x-if="f.kind && f.kind !== 'feature'">
                <span class="text-[9px] px-1.5 py-0.5 rounded font-medium"
                      :class="{
                        'bg-blocked/20 text-blocked': f.kind === 'bug',
                        'bg-escalated/20 text-escalated': f.kind === 'refactor',
                        'bg-impl/20 text-impl': f.kind === 'schema',
                        'bg-crit/20 text-crit': f.kind === 'security',
                        'bg-high/20 text-high': f.kind === 'incident',
                        'bg-done/20 text-done': f.kind === 'perf-revert',
                      }"
                      x-text="f.kind"></span>
              </template>
              <template x-if="f.risk_tier && f.risk_tier !== 'low'">
                <span class="text-[9px] px-1.5 py-0.5 rounded font-medium"
                      :class="f.risk_tier === 'high' ? 'bg-blocked/20 text-blocked' : 'bg-high/20 text-high'"
                      x-text="'risk: ' + f.risk_tier"></span>
              </template>
              <template x-for="l in (f.labels || [])" :key="l">
                <button @click.stop="filterText = l" class="text-[9px] px-1.5 py-0.5 rounded bg-accent/15 text-accent hover:bg-accent/25" :title="'click to filter by label: ' + l" x-text="'#' + l"></button>
              </template>
              <template x-if="costByFeature[f.id] && costByFeature[f.id] > 0">
                <span class="text-[9px] px-1.5 py-0.5 rounded bg-done/10 text-done font-mono tabular-nums" title="tokens spent on this feature" x-text="'$' + costByFeature[f.id].toFixed(3)"></span>
              </template>
              <span class="ml-auto text-[9px] text-muted font-mono tabular-nums" :title="'created ' + f.created_at + ' · updated ' + f.updated_at" x-text="ageLabel(f.updated_at)"></span>
            </div>

            <!-- Heal hint -->
            <template x-if="f.heal_hint">
              <div class="text-[11px] text-high bg-high/10 border-l-2 border-high px-2 py-1.5 rounded-r mb-1.5 max-h-16 overflow-hidden hover:max-h-80 transition-all whitespace-pre-wrap" x-text="f.heal_hint"></div>
            </template>

            <!-- Actions -->
            <div class="flex items-center gap-1 flex-wrap text-[10px]">
              <template x-if="f.status === 'blocked'">
                <button class="px-2 py-1 rounded bg-done/20 text-done hover:bg-done/30" @click.stop="act(f.id, 'approve')">approve</button>
              </template>
              <template x-if="f.status === 'blocked'">
                <button class="px-2 py-1 rounded bg-impl/20 text-impl hover:bg-impl/30" @click.stop="act(f.id, 'retry')">retry</button>
              </template>
              <a class="px-2 py-1 rounded border border-border hover:bg-surface" :href="langfuseUrl(f.id)" target="_blank" @click.stop>trace</a>
              <a class="px-2 py-1 rounded border border-border hover:bg-surface" :href="ghBranchUrl(f)" target="_blank" x-show="f.status !== 'pending'" @click.stop>branch</a>
              <button class="px-2 py-1 rounded border border-border hover:bg-surface" @click.stop="toggleHistory(f.id)" x-text="history[f.id] ? 'hide' : 'history'"></button>
              <a class="px-2 py-1 rounded border border-border hover:bg-surface" :href="'/replay/' + encodeURIComponent(f.id)" target="_blank" @click.stop>replay</a>
              <template x-if="f.status === 'blocked' || (f.heal_attempts || 0) > 0">
                <button class="px-2 py-1 rounded bg-escalated/20 text-escalated hover:bg-escalated/30" @click.stop="replayRetry(f)">fresh</button>
              </template>
              <template x-if="f.status === 'done'">
                <button class="px-2 py-1 rounded border border-border hover:bg-surface" @click.stop="confirmCleanup(f)">cleanup</button>
              </template>
              <button class="ml-auto px-2 py-1 rounded bg-blocked/20 text-blocked hover:bg-blocked/30" @click.stop="confirmNuke(f)">nuke</button>
            </div>

            <!-- History -->
            <template x-if="history[f.id]">
              <div class="mt-2 bg-surface rounded p-2 text-[10px] text-muted max-h-40 overflow-y-auto scrollbar-thin border-l-2 border-impl">
                <template x-for="h in history[f.id]" :key="h.ts + h.to">
                  <div class="py-0.5">
                    <span class="font-mono text-muted/70" x-text="fmtTs(h.ts)"></span>
                    <span class="text-fg font-medium" x-text="(h.from || '—') + ' → ' + h.to"></span>
                    <span x-show="h.actor" class="text-muted/70" x-text="' [' + h.actor + ']'"></span>
                    <span x-show="h.reason" x-text="' — ' + (h.reason || '').slice(0, 80)"></span>
                  </div>
                </template>
                <div x-show="!history[f.id].length" class="italic text-muted/60">no transitions recorded yet</div>
              </div>
            </template>
          </div>
        </template>
        <div class="text-[11px] text-muted text-center py-3 italic" x-show="!featuresByColumn(col).length">nothing here</div>
      </div>
    </div>
  </template>
</main>

<!-- Footer shortcuts -->
<div class="fixed bottom-2 left-4 text-[10px] text-muted font-mono">
  / search · j/k nav · a approve · r retry · n nuke · d debate · ? help
</div>

<!-- Toast -->
<div x-show="toast" x-transition
     :class="'fixed bottom-4 right-4 glass border px-4 py-2.5 rounded-lg text-xs max-w-sm shadow-modal ' + (toastErr ? 'border-blocked' : 'border-border')"
     x-text="toast"></div>

<!-- Analytics modal -->
<template x-if="analytics">
  <div @keydown.escape.window="analytics = null">
    <div class="fixed inset-0 bg-black/70 backdrop-blur-sm z-10" @click="analytics = null"></div>
    <div class="fixed inset-8 glass border border-border rounded-xl z-20 p-6 overflow-auto scrollbar-thin shadow-modal">
      <button class="absolute top-3 right-4 text-xs px-3 py-1.5 rounded border border-border hover:bg-surface" @click="analytics = null">close</button>
      <h2 class="text-sm font-semibold mb-1">Prompt version analytics</h2>
      <p class="text-xs text-muted mb-4">
        <span x-text="analytics.total_transitions"></span> transitions · best trusted:
        <span class="font-mono" x-text="analytics.best_trusted_version || 'n/a'"></span>
        <span x-show="analytics.best_trusted_done_rate !== null">@
          <span class="font-semibold" x-text="(analytics.best_trusted_done_rate * 100).toFixed(1) + '%'"></span>
        </span>
      </p>
      <table class="w-full text-xs">
        <thead>
          <tr class="text-muted border-b border-border">
            <th class="text-left pb-2 pr-3">version</th>
            <th class="text-left pb-2 pr-3">first seen</th>
            <th class="text-left pb-2 pr-3">last seen</th>
            <th class="text-right pb-2 pr-3">features</th>
            <th class="text-right pb-2 pr-3">done</th>
            <th class="text-right pb-2 pr-3">blocked</th>
            <th class="text-right pb-2 pr-3">rate</th>
            <th class="text-left pb-2">top reasons</th>
          </tr>
        </thead>
        <tbody>
          <template x-for="v in analytics.versions" :key="v.prompts_version">
            <tr class="border-b border-border/50">
              <td class="py-2 pr-3 font-mono" x-text="v.prompts_version"></td>
              <td class="py-2 pr-3 text-muted" x-text="v.first_seen ? v.first_seen.slice(0, 16).replace('T', ' ') : '—'"></td>
              <td class="py-2 pr-3 text-muted" x-text="v.last_seen ? v.last_seen.slice(0, 16).replace('T', ' ') : '—'"></td>
              <td class="py-2 pr-3 text-right tabular-nums" x-text="v.feature_count"></td>
              <td class="py-2 pr-3 text-right tabular-nums text-done" x-text="v.terminal_done"></td>
              <td class="py-2 pr-3 text-right tabular-nums text-blocked" x-text="v.terminal_blocked"></td>
              <td class="py-2 pr-3 text-right font-semibold tabular-nums"
                  :class="v.done_rate >= 0.75 ? 'text-done' : v.done_rate < 0.5 ? 'text-blocked' : ''">
                <div class="flex items-center gap-2 justify-end">
                  <div class="w-16 h-1.5 rounded-full bg-blocked/20 overflow-hidden" :title="(v.done_rate * 100).toFixed(1) + '% done'">
                    <div class="h-full" :class="v.done_rate >= 0.75 ? 'bg-done' : v.done_rate < 0.5 ? 'bg-blocked' : 'bg-high'" :style="'width:' + (v.done_rate * 100) + '%'"></div>
                  </div>
                  <span x-text="(v.done_rate * 100).toFixed(1) + '%'"></span>
                </div>
                <span x-show="v.low_confidence" class="text-[9px] text-muted ml-1">(n&lt;10)</span>
              </td>
              <td class="py-2 text-muted">
                <template x-for="r in v.top_reasons" :key="r.reason">
                  <div class="truncate max-w-md"><b class="text-blocked" x-text="r.count"></b> <span x-text="r.reason"></span></div>
                </template>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<!-- Dep-graph modal with SVG visualization -->
<template x-if="depGraph !== null">
  <div @keydown.escape.window="depGraph = null">
    <div class="fixed inset-0 bg-black/70 backdrop-blur-sm z-10" @click="depGraph = null"></div>
    <div class="fixed inset-8 glass border border-border rounded-xl z-20 p-6 overflow-auto scrollbar-thin shadow-modal">
      <button class="absolute top-3 right-4 text-xs px-3 py-1.5 rounded border border-border hover:bg-surface" @click="depGraph = null">close</button>
      <h2 class="text-sm font-semibold mb-1">Feature dependency graph</h2>
      <p class="text-xs text-muted mb-4">
        <span x-text="depGraph.nodes.length"></span> linked nodes ·
        <span x-text="depGraph.edges.length"></span> edges · red ID = blocked by unfinished dep.
      </p>
      <div class="mb-4 p-4 bg-bg rounded-lg border border-border overflow-auto">
        <svg :width="depGraphLayout.width" :height="depGraphLayout.height" class="block">
          <defs>
            <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="10" refY="3.5" orient="auto">
              <polygon points="0 0, 10 3.5, 0 7" fill="#8b949e" />
            </marker>
          </defs>
          <!-- Edges -->
          <template x-for="e in depGraphLayout.edges" :key="e.from + '-' + e.to">
            <line :x1="e.x1" :y1="e.y1" :x2="e.x2" :y2="e.y2" stroke="#8b949e" stroke-width="1.5" marker-end="url(#arrowhead)" opacity="0.7" />
          </template>
          <!-- Nodes -->
          <template x-for="n in depGraphLayout.nodes" :key="n.id">
            <g :transform="'translate(' + n.x + ',' + n.y + ')'">
              <rect x="-70" y="-14" width="140" height="28" rx="6"
                    :fill="n.blocked_by_deps ? '#f85149' : n.status === 'done' ? '#3fb950' : n.status === 'pending' ? '#8b949e' : '#58a6ff'"
                    fill-opacity="0.2"
                    :stroke="n.blocked_by_deps ? '#f85149' : n.status === 'done' ? '#3fb950' : n.status === 'pending' ? '#8b949e' : '#58a6ff'"
                    stroke-width="1.5" />
              <text text-anchor="middle" y="4" fill="#e6edf3" font-size="10" font-family="SFMono-Regular, Menlo, monospace" x-text="n.id.slice(0, 16)"></text>
            </g>
          </template>
        </svg>
      </div>
      <table class="w-full text-xs">
        <thead><tr class="text-muted border-b border-border">
          <th class="text-left pb-2 pr-3">feature</th>
          <th class="text-left pb-2 pr-3">status</th>
          <th class="text-left pb-2 pr-3">depends on</th>
          <th class="text-left pb-2">depended on by</th>
        </tr></thead>
        <tbody>
          <template x-for="n in depGraph.nodes" :key="n.id">
            <tr class="border-b border-border/50">
              <td class="py-2 pr-3">
                <span :class="n.blocked_by_deps ? 'text-blocked font-semibold' : ''" class="font-mono" x-text="n.id"></span>
                <div class="text-[10px] text-muted truncate max-w-xs" x-text="n.name"></div>
              </td>
              <td class="py-2 pr-3"><span class="text-[10px] px-2 py-0.5 rounded bg-surface border border-border" x-text="n.status"></span></td>
              <td class="py-2 pr-3">
                <template x-for="e in depGraph.edges.filter(x => x.to === n.id)" :key="'i-' + e.from + '-' + e.to">
                  <span class="inline-block text-[10px] px-2 py-0.5 rounded bg-high/15 text-high mr-1 mb-1 font-mono" x-text="e.from"></span>
                </template>
              </td>
              <td class="py-2">
                <template x-for="e in depGraph.edges.filter(x => x.from === n.id)" :key="'o-' + e.from + '-' + e.to">
                  <span class="inline-block text-[10px] px-2 py-0.5 rounded bg-accent/15 text-accent mr-1 mb-1 font-mono" x-text="e.to"></span>
                </template>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>
</template>

<!-- Snapshot-diff modal -->
<template x-if="snapshotPicker !== null">
  <div @keydown.escape.window="snapshotPicker = null">
    <div class="fixed inset-0 bg-black/70 backdrop-blur-sm z-10" @click="snapshotPicker = null"></div>
    <div class="fixed inset-8 glass border border-border rounded-xl z-20 p-6 overflow-auto scrollbar-thin shadow-modal">
      <button class="absolute top-3 right-4 text-xs px-3 py-1.5 rounded border border-border hover:bg-surface" @click="snapshotPicker = null">close</button>
      <h2 class="text-sm font-semibold mb-3">Backlog diff</h2>
      <div class="flex items-center gap-3 mb-4 text-xs">
        <label class="text-muted">from</label>
        <select x-model="diffFrom" class="bg-bg border border-border rounded px-2 py-1 focus:outline-none focus:border-accent">
          <option value="">select...</option>
          <template x-for="s in snapshotPicker" :key="s"><option x-text="s"></option></template>
        </select>
        <label class="text-muted">to</label>
        <select x-model="diffTo" class="bg-bg border border-border rounded px-2 py-1 focus:outline-none focus:border-accent">
          <option value="">select...</option>
          <template x-for="s in snapshotPicker" :key="s"><option x-text="s"></option></template>
        </select>
        <button @click="runDiff()" class="px-3 py-1 rounded bg-done text-bg font-medium hover:bg-done/90">diff</button>
      </div>
      <template x-if="diffResult">
        <div class="text-xs">
          <div class="mb-4 text-muted">
            <span class="text-done font-semibold" x-text="diffResult.added_count"></span> added ·
            <span class="text-blocked font-semibold" x-text="diffResult.removed_count"></span> removed ·
            <span class="text-high font-semibold" x-text="diffResult.status_changed_count"></span> status-changed
          </div>
          <template x-if="diffResult.added && diffResult.added.length">
            <div class="mb-3">
              <h3 class="text-done font-semibold mb-1">Added (<span x-text="diffResult.added.length"></span>)</h3>
              <template x-for="f in diffResult.added" :key="f.id">
                <div class="py-0.5 font-mono"><span x-text="f.id"></span> <span class="text-muted" x-text="'· ' + (f.name || '')"></span></div>
              </template>
            </div>
          </template>
          <template x-if="diffResult.removed && diffResult.removed.length">
            <div class="mb-3">
              <h3 class="text-blocked font-semibold mb-1">Removed (<span x-text="diffResult.removed.length"></span>)</h3>
              <template x-for="f in diffResult.removed" :key="f.id">
                <div class="py-0.5 font-mono"><span x-text="f.id"></span> <span class="text-muted" x-text="'· ' + (f.name || '')"></span></div>
              </template>
            </div>
          </template>
          <template x-if="diffResult.status_changed && diffResult.status_changed.length">
            <div>
              <h3 class="text-high font-semibold mb-1">Status changed (<span x-text="diffResult.status_changed.length"></span>)</h3>
              <template x-for="f in diffResult.status_changed" :key="f.id">
                <div class="py-0.5 font-mono">
                  <span x-text="f.id"></span>
                  <span class="text-muted" x-text="': ' + f.from + ' → ' + f.to"></span>
                </div>
              </template>
            </div>
          </template>
        </div>
      </template>
    </div>
  </div>
</template>

<!-- Schedule modal -->
<template x-if="schedule !== null">
  <div @keydown.escape.window="schedule = null">
    <div class="fixed inset-0 bg-black/70 backdrop-blur-sm z-10" @click="schedule = null"></div>
    <div class="fixed inset-8 glass border border-border rounded-xl z-20 p-6 overflow-auto scrollbar-thin shadow-modal">
      <button class="absolute top-3 right-4 text-xs px-3 py-1.5 rounded border border-border hover:bg-surface" @click="schedule = null">close</button>
      <h2 class="text-sm font-semibold mb-1">Scheduled recurring features</h2>
      <p class="text-[11px] text-muted mb-3">
        Scheduler re-reads every 60s. Save to apply; no restart needed.
      </p>
      <textarea x-model="scheduleJson" rows="22"
                class="w-full bg-bg border border-border rounded-md p-3 font-mono text-[11px] text-fg focus:outline-none focus:border-accent scrollbar-thin"></textarea>
      <div class="mt-3 flex items-center gap-3">
        <button @click="saveSchedule()" class="text-xs px-3 py-1.5 rounded-md bg-done text-bg font-medium hover:bg-done/90">save</button>
        <span class="text-[11px] text-muted" x-text="scheduleStatus"></span>
      </div>
    </div>
  </div>
</template>

<!-- Add modal -->
<template x-if="addForm">
  <div @keydown.escape.window="addForm = null">
    <div class="fixed inset-0 bg-black/70 backdrop-blur-sm z-10" @click="addForm = null"></div>
    <div class="fixed top-20 left-1/2 -translate-x-1/2 w-full max-w-2xl glass border border-border rounded-xl z-20 p-6 shadow-modal">
      <button class="absolute top-3 right-4 text-xs px-3 py-1.5 rounded border border-border hover:bg-surface" @click="addForm = null">close</button>
      <h2 class="text-sm font-semibold mb-4">Add feature / bug</h2>
      <form @submit.prevent="submitAdd()" class="grid grid-cols-[120px_1fr] gap-3 items-center text-xs">
        <label class="text-muted">kind</label>
        <select x-model="addForm.kind" class="bg-bg border border-border rounded-md px-2 py-1.5 focus:outline-none focus:border-accent">
          <option>feature</option><option>bug</option><option>refactor</option>
          <option>schema</option><option>security</option><option>incident</option><option>perf-revert</option>
        </select>
        <label class="text-muted">id</label>
        <input x-model="addForm.id" required placeholder="kebab-case-id" class="bg-bg border border-border rounded-md px-3 py-1.5 focus:outline-none focus:border-accent" />
        <label class="text-muted">name</label>
        <input x-model="addForm.name" required placeholder="Human-readable title" class="bg-bg border border-border rounded-md px-3 py-1.5 focus:outline-none focus:border-accent" />
        <label class="text-muted">priority</label>
        <select x-model="addForm.priority" class="bg-bg border border-border rounded-md px-2 py-1.5 focus:outline-none focus:border-accent">
          <option>critical</option><option>high</option><option selected>medium</option><option>low</option>
        </select>
        <label class="text-muted">repos</label>
        <input x-model="addForm.reposCsv" placeholder="hearth, hearth-desktop" class="bg-bg border border-border rounded-md px-3 py-1.5 focus:outline-none focus:border-accent" />
        <label class="text-muted">description</label>
        <textarea x-model="addForm.description" required rows="3" placeholder="What needs to be built / what's broken" class="bg-bg border border-border rounded-md px-3 py-2 focus:outline-none focus:border-accent"></textarea>
        <label class="text-muted">acceptance</label>
        <input x-model="addForm.acceptance" placeholder="Concrete done condition" class="bg-bg border border-border rounded-md px-3 py-1.5 focus:outline-none focus:border-accent" />
        <template x-if="addForm.kind === 'bug'">
          <label class="text-muted">repro cmd</label>
        </template>
        <template x-if="addForm.kind === 'bug'">
          <input x-model="addForm.repro" required placeholder="must fail today" class="bg-bg border border-border rounded-md px-3 py-1.5 focus:outline-none focus:border-accent" />
        </template>
        <div></div>
        <div class="flex justify-end">
          <button type="submit" class="px-4 py-1.5 rounded-md bg-done text-bg font-medium hover:bg-done/90">queue</button>
        </div>
      </form>
    </div>
  </div>
</template>

<script>
function kanban() {
  return {
    features: [],
    stats: null,
    config: null,
    costByFeature: {},
    history: {},
    analytics: null,
    addForm: null,
    schedule: null,
    scheduleJson: '',
    scheduleStatus: '',
    depGraph: null,
    snapshotPicker: null,
    diffFrom: '',
    diffTo: '',
    diffResult: null,
    filterText: '',
    kindFilter: '',
    riskFilter: '',
    openViews: false,
    savedViews: [],
    selectedId: null,
    toast: '',
    toastErr: false,
    lastRefresh: Date.now(),
    sinceLabel: '0',
    columns: [
      { key: 'pending',      label: 'Pending',      color: '#8b949e', match: ['pending'], escalated: false },
      { key: 'implementing', label: 'Implementing', color: '#58a6ff', match: ['implementing', 'researching', 'reviewing'], escalated: false },
      { key: 'blocked',      label: 'Blocked',      color: '#f85149', match: ['blocked'], escalated: false },
      { key: 'escalated',    label: 'Escalated',    color: '#bc8cff', match: ['blocked'], escalated: true },
      { key: 'done',         label: 'Done',         color: '#3fb950', match: ['done'], escalated: false },
    ],
    get blockedFeatures() { return this.features.filter(f => f.status === 'blocked'); },
    get escalatedFeatures() { return this.features.filter(f => f.status === 'blocked' && (f.heal_attempts || 0) >= 3); },
    get workerBadges() {
      const w = (this.stats && this.stats.workers) || {};
      return Object.keys(w).sort((a, b) => Number(a) - Number(b)).map(id => ({
        id, feature: w[id].feature || '', age: Number(w[id].age_sec) || 0,
      }));
    },
    init() {
      this.loadViews();
      this.refresh();
      // Poll every 30s as a backstop for SSE drops; SSE handles real-time.
      setInterval(() => this.refresh(), 30000);
      setInterval(() => { this.sinceLabel = Math.floor((Date.now() - this.lastRefresh) / 1000).toString(); }, 1000);
      // Subscribe to Server-Sent Events so transitions land instantly
      // instead of waiting for the next poll. On drop the browser
      // auto-reconnects; we also backfill any missed events via
      // /events/replay?from_ts so the board stays consistent across
      // a network partition instead of waiting 30s for the poll.
      try {
        let lastEventTs = new Date().toISOString();
        const src = new EventSource('/events');
        src.addEventListener('transition', async (e) => {
          try {
            const entry = JSON.parse(e.data);
            if (entry && entry.ts) lastEventTs = entry.ts;
          } catch (err) { /* ignore */ }
          this.refresh();
        });
        src.onopen = async () => {
          // Reconnect hook: pull any transitions we missed since
          // lastEventTs. Empty on first open. Cheap; /events/replay
          // just walks the on-disk JSONL with a ts cutoff.
          try {
            const r = await fetch('/events/replay?from_ts=' + encodeURIComponent(lastEventTs) + '&limit=100');
            if (r.ok) {
              const missed = await r.json();
              if (missed.length) {
                this.flash('reconnected — backfilled ' + missed.length + ' event(s)');
                this.refresh();
              }
            }
          } catch (err) { /* ignore */ }
        };
        src.onerror = () => { /* browser auto-reconnects; onopen fires backfill */ };
      } catch (e) { /* EventSource unsupported; polling fallback covers us */ }
      document.addEventListener('keydown', (e) => {
        const tag = (e.target && e.target.tagName || '').toLowerCase();
        if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        if (e.key === '/') { e.preventDefault(); document.querySelector('input[type="search"]')?.focus(); }
        else if (e.key === '?') { alert('/\\tfocus search\\nj/k\\tnavigate\\na\\tapprove\\nr\\tretry\\nn\\tnuke\\nd\\tdebate\\nEsc\\tclear'); }
        else if (e.key === 'j' || e.key === 'k') {
          const flat = this.columns.flatMap(c => this.featuresByColumn(c).map(f => f.id));
          if (!flat.length) return;
          const cur = flat.indexOf(this.selectedId);
          const next = e.key === 'j' ? Math.min(flat.length - 1, cur + 1) : Math.max(0, cur - 1);
          this.selectedId = flat[next] || flat[0];
        } else if (e.key === 'Escape') { this.selectedId = null; this.analytics = null; this.schedule = null; this.depGraph = null; this.addForm = null; }
        else if (this.selectedId && ['a','r','n','d'].includes(e.key)) {
          const f = this.features.find(x => x.id === this.selectedId);
          if (!f) return;
          if (e.key === 'a') this.act(f.id, 'approve');
          else if (e.key === 'r') this.act(f.id, 'retry');
          else if (e.key === 'n') this.confirmNuke(f);
          else if (e.key === 'd') fetch('/features/' + encodeURIComponent(f.id) + '/debate', {method:'POST'})
            .then(r => r.json()).then(b => this.flash('debate: ' + JSON.stringify(b).slice(0, 120)))
            .catch(e => this.flash('debate failed: ' + e.message, true));
        }
      });
    },
    async refresh() {
      try {
        const [fr, sr, cr, costR] = await Promise.all([
          fetch('/features'), fetch('/stats'), fetch('/config'), fetch('/cost-analytics')
        ]);
        if (!fr.ok) throw new Error('features HTTP ' + fr.status);
        this.features = await fr.json();
        if (sr.ok) this.stats = await sr.json();
        if (cr.ok) this.config = await cr.json();
        if (costR.ok) {
          const cd = await costR.json();
          const map = {};
          for (const row of (cd.top_features || [])) map[row.feature_id] = row.cost_usd;
          this.costByFeature = map;
        }
        this.lastRefresh = Date.now();
        this.sinceLabel = '0';
      } catch (e) { this.flash('refresh failed: ' + e.message, true); }
    },
    featuresByStatus(statuses) { return this.features.filter(f => statuses.includes(f.status)); },
    featuresByColumn(col) {
      let base = this.features.filter(f => col.match.includes(f.status));
      if (col.key === 'blocked') base = base.filter(f => (f.heal_attempts || 0) < 3);
      if (col.key === 'escalated') base = base.filter(f => (f.heal_attempts || 0) >= 3);
      if (this.kindFilter) base = base.filter(f => f.kind === this.kindFilter);
      if (this.riskFilter) base = base.filter(f => (f.risk_tier || 'low') === this.riskFilter);
      const q = this.filterText.trim().toLowerCase();
      if (!q) return base;
      // Search now also matches heal_hint substring so clicking a
      // block-reason chip in the header filters correctly.
      return base.filter(f =>
        (f.id || '').toLowerCase().includes(q) ||
        (f.name || '').toLowerCase().includes(q) ||
        (f.repos || []).some(r => r.toLowerCase().includes(q)) ||
        (f.heal_hint || '').toLowerCase().includes(q)
      );
    },
    async act(id, action) {
      try {
        const r = await fetch('/features/' + encodeURIComponent(id) + '/action', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action }),
        });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        this.flash(action + ': ' + id);
        await this.refresh();
      } catch (e) { this.flash(action + ' failed: ' + e.message, true); }
    },
    async bulkApproveBlocked() {
      const ids = this.blockedFeatures.map(f => f.id);
      if (!ids.length) return;
      if (!confirm('Approve ' + ids.length + ' blocked feature(s)?')) return;
      for (const id of ids) await this.act(id, 'approve');
      this.flash('approved ' + ids.length);
    },
    async bulkRetryEscalated() {
      const ids = this.escalatedFeatures.map(f => f.id);
      if (!ids.length) return;
      if (!confirm('Retry ' + ids.length + ' escalated feature(s)?')) return;
      for (const id of ids) await this.act(id, 'retry');
      this.flash('retried ' + ids.length);
    },
    confirmNuke(f) {
      if (!confirm('Nuke ' + f.id + '?')) return;
      this.act(f.id, 'nuke');
    },
    confirmCleanup(f) {
      if (!confirm('Delete origin branch feat/' + f.id + ' and any local worktree?')) return;
      this.act(f.id, 'cleanup_branch');
    },
    async replayRetry(f) {
      if (!confirm('Clear heal state + hint and re-queue ' + f.id + '?')) return;
      try {
        const r = await fetch('/features/' + encodeURIComponent(f.id) + '/replay-retry', { method: 'POST' });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        this.flash('fresh-retry: ' + f.id);
        await this.refresh();
      } catch (e) { this.flash('fresh-retry failed: ' + e.message, true); }
    },
    openAddModal() {
      this.addForm = { kind: 'feature', id: '', name: '', priority: 'medium', reposCsv: 'hearth', description: '', acceptance: '', repro: '' };
    },
    async submitAdd() {
      const f = this.addForm;
      const body = {
        id: f.id.trim(), name: f.name.trim(), description: f.description.trim(),
        priority: f.priority, kind: f.kind,
        repos: f.reposCsv.split(',').map(r => r.trim()).filter(Boolean),
        acceptance_criteria: f.acceptance.trim(),
      };
      if (f.kind === 'bug') body.repro_command = f.repro.trim();
      try {
        const r = await fetch('/features', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const resp = await r.json();
        if (!r.ok) throw new Error(resp.detail || 'HTTP ' + r.status);
        this.flash('queued ' + resp.id);
        this.addForm = null;
        await this.refresh();
      } catch (e) { this.flash('queue failed: ' + e.message, true); }
    },
    async loadAnalytics() {
      try {
        const r = await fetch('/prompt-analytics');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.analytics = await r.json();
      } catch (e) { this.flash('analytics fetch failed: ' + e.message, true); }
    },
    async loadSchedule() {
      try {
        const r = await fetch('/schedule');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        this.scheduleJson = JSON.stringify(data, null, 2);
        this.scheduleStatus = '';
        this.schedule = data;
      } catch (e) { this.flash('schedule fetch failed: ' + e.message, true); }
    },
    async saveSchedule() {
      let parsed;
      try { parsed = JSON.parse(this.scheduleJson); }
      catch (e) { this.scheduleStatus = 'invalid JSON: ' + e.message; return; }
      try {
        const r = await fetch('/schedule', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(parsed),
        });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        this.scheduleStatus = 'saved ' + body.count + ' entries at ' + new Date().toLocaleTimeString();
        this.flash('schedule saved');
      } catch (e) { this.scheduleStatus = 'save failed: ' + e.message; }
    },
    async loadDepGraph() {
      try {
        const r = await fetch('/dep-graph');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.depGraph = await r.json();
      } catch (e) { this.flash('dep-graph fetch failed: ' + e.message, true); }
    },
    get depGraphLayout() {
      // Simple layered layout: roots (no incoming edges) on left, then
      // fan out right. Topological-ish ordering with vertical spread.
      if (!this.depGraph) return { width: 0, height: 0, nodes: [], edges: [] };
      const nodes = this.depGraph.nodes.map(n => ({ ...n }));
      const edges = this.depGraph.edges;
      // Compute depth (longest path from root) per node.
      const incoming = {};
      nodes.forEach(n => { incoming[n.id] = 0; });
      edges.forEach(e => { if (incoming[e.to] !== undefined) incoming[e.to]++; });
      const depth = {};
      const queue = nodes.filter(n => incoming[n.id] === 0).map(n => n.id);
      queue.forEach(id => { depth[id] = 0; });
      const pending = {...incoming};
      while (queue.length) {
        const id = queue.shift();
        edges.filter(e => e.from === id).forEach(e => {
          pending[e.to]--;
          depth[e.to] = Math.max(depth[e.to] || 0, (depth[id] || 0) + 1);
          if (pending[e.to] === 0) queue.push(e.to);
        });
      }
      // Group by depth to assign x; within a depth assign y sequentially.
      const byDepth = {};
      nodes.forEach(n => {
        const d = depth[n.id] ?? 0;
        (byDepth[d] = byDepth[d] || []).push(n);
      });
      const colWidth = 180;
      const rowHeight = 44;
      const padding = 30;
      Object.entries(byDepth).forEach(([d, ns]) => {
        ns.forEach((n, i) => {
          n.x = padding + Number(d) * colWidth + 70;
          n.y = padding + i * rowHeight + 20;
        });
      });
      const maxDepth = Math.max(0, ...Object.keys(byDepth).map(Number));
      const maxPerCol = Math.max(1, ...Object.values(byDepth).map(v => v.length));
      const width = padding * 2 + (maxDepth + 1) * colWidth;
      const height = padding * 2 + maxPerCol * rowHeight;
      const nodeById = Object.fromEntries(nodes.map(n => [n.id, n]));
      const laidOutEdges = edges.map(e => {
        const a = nodeById[e.from], b = nodeById[e.to];
        if (!a || !b) return null;
        // Entry/exit at node rectangle edges.
        return { from: e.from, to: e.to, x1: a.x + 70, y1: a.y, x2: b.x - 70, y2: b.y };
      }).filter(Boolean);
      return { width, height, nodes, edges: laidOutEdges };
    },
    async loadSnapshots() {
      try {
        const r = await fetch('/backlog/snapshots');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.snapshotPicker = await r.json();
        if (!this.snapshotPicker.length) {
          this.flash('no snapshots yet — first one lands tomorrow', false);
          this.snapshotPicker = null;
        } else {
          // default: diff from day-before-latest to latest
          this.diffTo = this.snapshotPicker[this.snapshotPicker.length - 1];
          this.diffFrom = this.snapshotPicker[Math.max(0, this.snapshotPicker.length - 2)];
          this.diffResult = null;
        }
      } catch (e) { this.flash('snapshots failed: ' + e.message, true); }
    },
    async runDiff() {
      if (!this.diffFrom || !this.diffTo) return;
      try {
        const r = await fetch('/backlog/diff?from_date=' + encodeURIComponent(this.diffFrom) + '&to_date=' + encodeURIComponent(this.diffTo));
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.diffResult = await r.json();
      } catch (e) { this.flash('diff failed: ' + e.message, true); }
    },
    async toggleHistory(id) {
      if (this.history[id]) { delete this.history[id]; return; }
      try {
        const r = await fetch('/features/' + encodeURIComponent(id) + '/history');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const body = await r.json();
        this.history[id] = body.transitions || [];
      } catch (e) { this.flash('history fetch failed: ' + e.message, true); }
    },
    loadViews() {
      try {
        const raw = localStorage.getItem('hearth_saved_views');
        this.savedViews = raw ? JSON.parse(raw) : [];
      } catch (e) { this.savedViews = []; }
    },
    persistViews() {
      try { localStorage.setItem('hearth_saved_views', JSON.stringify(this.savedViews)); } catch (e) { /* ignore */ }
    },
    saveCurrentView() {
      const name = prompt('Name this view (e.g. "my-bugs", "q2-blockers"):');
      if (!name) return;
      this.savedViews = this.savedViews.filter(v => v.name !== name);
      this.savedViews.push({ name, filterText: this.filterText, kindFilter: this.kindFilter, riskFilter: this.riskFilter });
      this.persistViews();
      this.flash('saved view: ' + name);
    },
    applyView(v) {
      this.filterText = v.filterText || '';
      this.kindFilter = v.kindFilter || '';
      this.riskFilter = v.riskFilter || '';
    },
    deleteView(name) {
      this.savedViews = this.savedViews.filter(v => v.name !== name);
      this.persistViews();
    },
    ageLabel(iso) {
      if (!iso) return '';
      const delta = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
      if (delta < 60) return delta + 's';
      if (delta < 3600) return Math.floor(delta / 60) + 'm';
      if (delta < 86400) return Math.floor(delta / 3600) + 'h';
      return Math.floor(delta / 86400) + 'd';
    },
    fmtTs(iso) { if (!iso) return ''; return new Date(iso).toISOString().slice(5, 16).replace('T', ' '); },
    langfuseUrl(featureId) {
      const base = (this.config && this.config.urls && this.config.urls.langfuse_public) || '';
      if (!base) return '#';
      return base + '/project?search=' + encodeURIComponent('feature:' + featureId);
    },
    ghBranchUrl(f) {
      const repo = f.repos && f.repos.length ? f.repos[0] : 'hearth';
      return 'https://github.com/ghndrx/' + repo + '/tree/' + encodeURIComponent(f.branch);
    },
    flash(msg, err = false) {
      this.toast = msg; this.toastErr = err;
      setTimeout(() => { this.toast = ''; }, 3500);
    },
  };
}
</script>
</body>
</html>
"""
