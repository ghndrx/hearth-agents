"""Static HTML for the kanban UI.

Served from ``GET /kanban``. Alpine.js + inline CSS, no build step.
Columns: pending | implementing (incl. researching, reviewing) | blocked | done.

The iterative-kanban pattern (research #3793) treats ``blocked`` as a
self-correction opportunity, not a terminal state — so the UI surfaces the
heal-hint and offers retry/approve actions inline rather than forcing a
full roundtrip through the backend log.
"""

KANBAN_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>hearth-agents — kanban</title>
<script defer src="https://unpkg.com/alpinejs@3.14.1/dist/cdn.min.js"></script>
<style>
  :root {
    --bg: #0e1116; --fg: #e6edf3; --muted: #9da7b1;
    --card: #161b22; --border: #30363d;
    --pending: #6e7681; --impl: #1f6feb; --blocked: #da3633; --done: #238636;
    --pri-critical: #f85149; --pri-high: #f0883e;
    --pri-medium: #d29922; --pri-low: #6e7681;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--fg); margin: 0; font: 14px/1.4 -apple-system,SFMono-Regular,Menlo,monospace; }
  header { display: flex; align-items: center; justify-content: space-between; padding: 12px 20px; border-bottom: 1px solid var(--border); }
  header h1 { margin: 0; font-size: 16px; font-weight: 600; }
  header .meta { color: var(--muted); font-size: 12px; }
  .board { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; padding: 16px; height: calc(100vh - 58px); overflow: hidden; }
  .col.escalated .col-head .dot { background: #8957e5; }
  .col { background: var(--card); border: 1px solid var(--border); border-radius: 6px; display: flex; flex-direction: column; min-height: 0; }
  .col-head { padding: 10px 12px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }
  .col-head .dot { width: 10px; height: 10px; border-radius: 50%; }
  .col-head .title { font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.04em; }
  .col-head .count { color: var(--muted); font-size: 12px; margin-left: auto; }
  .col-body { overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 8px; }
  .card { background: #0d1117; border: 1px solid var(--border); border-radius: 5px; padding: 10px; font-size: 13px; }
  .card .row { display: flex; align-items: center; gap: 6px; margin-bottom: 4px; flex-wrap: wrap; }
  .card .name { font-weight: 600; color: var(--fg); flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .card .id { color: var(--muted); font-size: 11px; font-family: SFMono-Regular,Menlo,monospace; }
  .pill { display: inline-flex; align-items: center; font-size: 10px; padding: 2px 6px; border-radius: 3px; text-transform: uppercase; letter-spacing: 0.03em; }
  .pri-critical { background: var(--pri-critical); color: #fff; }
  .pri-high { background: var(--pri-high); color: #fff; }
  .pri-medium { background: var(--pri-medium); color: #000; }
  .pri-low { background: var(--pri-low); color: #fff; }
  .repo { background: #21262d; color: var(--muted); font-size: 10px; padding: 2px 6px; border-radius: 3px; }
  .si { background: #6f42c1; color: #fff; font-size: 9px; padding: 1px 5px; border-radius: 3px; }
  .heal { background: #3d2810; color: #d29922; font-size: 11px; padding: 6px 8px; border-radius: 3px; margin-top: 6px; border-left: 2px solid #d29922; white-space: pre-wrap; max-height: 60px; overflow: hidden; }
  .heal:hover { max-height: 300px; overflow: auto; }
  .actions { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
  .actions button, .actions a.btn { background: #21262d; color: var(--fg); border: 1px solid var(--border); padding: 4px 8px; border-radius: 3px; font-size: 11px; cursor: pointer; text-decoration: none; }
  .actions button:hover, .actions a.btn:hover { background: #30363d; }
  .actions .approve { background: var(--done); border-color: var(--done); }
  .actions .retry { background: var(--impl); border-color: var(--impl); }
  .actions .nuke { background: var(--blocked); border-color: var(--blocked); }
  .empty { color: var(--muted); font-size: 12px; padding: 16px; text-align: center; }
  .bulk { display: flex; gap: 6px; align-items: center; }
  .bulk button { background: var(--done); color: #fff; border: 0; padding: 6px 10px; border-radius: 3px; font-size: 12px; cursor: pointer; }
  .bulk button[disabled] { opacity: 0.4; cursor: not-allowed; }
  .toast { position: fixed; bottom: 16px; right: 16px; background: var(--card); border: 1px solid var(--border); padding: 10px 14px; border-radius: 4px; font-size: 12px; max-width: 320px; }
  .toast.err { border-color: var(--blocked); }
  .reasons { padding: 8px 16px; background: var(--card); border-bottom: 1px solid var(--border); font-size: 11px; color: var(--muted); display: flex; gap: 12px; flex-wrap: wrap; }
  .reasons .label { color: var(--fg); font-weight: 600; }
  .reasons .reason { background: #0d1117; border: 1px solid var(--border); padding: 2px 8px; border-radius: 3px; }
  .reasons .reason b { color: var(--blocked); margin-right: 4px; }
  .age { color: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
  .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10; }
  .modal { position: fixed; inset: 48px; background: var(--card); border: 1px solid var(--border); border-radius: 6px; z-index: 11; padding: 20px; overflow: auto; }
  .modal h2 { margin-top: 0; font-size: 14px; }
  .modal table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .modal th, .modal td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); font-family: SFMono-Regular,Menlo,monospace; }
  .modal th { color: var(--muted); font-weight: 500; }
  .modal .rate { font-weight: 700; }
  .modal .rate.low { color: var(--blocked); }
  .modal .rate.high { color: var(--done); }
  .modal .reasons { color: var(--muted); font-size: 11px; line-height: 1.4; }
  .modal .close { position: absolute; top: 8px; right: 12px; background: transparent; color: var(--fg); border: 1px solid var(--border); padding: 4px 10px; border-radius: 3px; cursor: pointer; }
  .history-box { margin-top: 6px; padding: 6px 8px; background: #0a0d12; border-radius: 3px; font-size: 10px; color: var(--muted); border-left: 2px solid var(--impl); max-height: 160px; overflow: auto; }
  .history-box .row { margin: 0; padding: 2px 0; display: block; }
  .history-box .from-to { color: var(--fg); font-weight: 600; }
  .history-box .ts { color: var(--muted); margin-right: 6px; }
</style>
</head>
<body x-data="kanban()" x-init="init()">
  <header>
    <h1>hearth-agents <span style="color: var(--muted); font-weight: 400;">kanban</span></h1>
    <div class="bulk">
      <input type="search" placeholder="filter id / name / repo..." x-model="filterText" style="background:#0d1117;color:var(--fg);border:1px solid var(--border);border-radius:3px;padding:4px 8px;font-size:12px;width:220px;" />
      <span class="meta" x-text="'refreshed ' + sinceLabel + 's ago'"></span>
      <select x-model="kindFilter" style="background:#0d1117;color:var(--fg);border:1px solid var(--border);padding:4px;font-size:12px;">
        <option value="">all kinds</option>
        <option value="feature">feature</option>
        <option value="bug">bug</option>
        <option value="refactor">refactor</option>
        <option value="schema">schema</option>
        <option value="security">security</option>
        <option value="incident">incident</option>
        <option value="perf-revert">perf-revert</option>
      </select>
      <select x-model="riskFilter" style="background:#0d1117;color:var(--fg);border:1px solid var(--border);padding:4px;font-size:12px;">
        <option value="">all risk</option>
        <option value="low">low</option>
        <option value="medium">medium</option>
        <option value="high">high</option>
      </select>
      <button @click="refresh()">refresh</button>
      <button @click="openAddModal()" style="background: var(--done); color: #fff; border-color: var(--done);">+ add</button>
      <button @click="loadAnalytics()">analytics</button>
      <button @click="loadSchedule()">schedule</button>
      <button :disabled="!blockedFeatures.length" @click="bulkApproveBlocked()" title="Mark all currently blocked as human-approved">approve blocked</button>
      <button :disabled="!escalatedFeatures.length" @click="bulkRetryEscalated()" title="Reset heal_attempts and re-queue every escalated feature">retry escalated</button>
    </div>
  </header>

  <div class="reasons" x-show="stats && stats.block_reasons_top10 && stats.block_reasons_top10.length">
    <span class="label">blocked by reason:</span>
    <template x-for="r in (stats && stats.block_reasons_top10) || []" :key="r.reason">
      <span class="reason" :title="r.reason"><b x-text="r.count"></b><span x-text="r.reason.slice(0, 50)"></span></span>
    </template>
  </div>

  <div class="board">
    <template x-for="col in columns" :key="col.key">
      <div class="col" :class="col.key">
        <div class="col-head">
          <span class="dot" :style="'background:' + col.color"></span>
          <span class="title" x-text="col.label"></span>
          <span class="count" x-text="featuresByColumn(col).length"></span>
        </div>
        <div class="col-body">
          <template x-for="f in featuresByColumn(col)" :key="f.id">
            <div class="card">
              <div class="row">
                <span class="name" :title="f.name" x-text="f.name"></span>
                <span class="pill" :class="'pri-' + f.priority" x-text="f.priority"></span>
              </div>
              <div class="row">
                <span class="id" x-text="f.id"></span>
                <template x-if="f.self_improvement"><span class="si">self</span></template>
                <template x-for="r in f.repos" :key="r"><span class="repo" x-text="r"></span></template>
                <template x-if="f.heal_attempts > 0">
                  <span class="repo" x-text="'heal ' + f.heal_attempts + '/3'"></span>
                </template>
                <template x-if="f.depends_on && f.depends_on.length">
                  <span class="repo" :title="'depends on: ' + f.depends_on.join(', ')" style="background: #6e7b8f; color: #fff;" x-text="'↘ ' + f.depends_on.length + ' dep' + (f.depends_on.length === 1 ? '' : 's')"></span>
                </template>
                <template x-if="f.kind && f.kind !== 'feature'">
                  <span class="repo" :style="kindColor(f.kind)" x-text="f.kind"></span>
                </template>
                <template x-if="f.risk_tier && f.risk_tier !== 'low'">
                  <span class="repo" :style="'background:'+(f.risk_tier === 'high' ? 'var(--blocked)' : 'var(--pri-high)')+';color:#fff;'" x-text="'risk: ' + f.risk_tier"></span>
                </template>
                <span class="age" :title="'created ' + f.created_at + ' · updated ' + f.updated_at" x-text="ageLabel(f.updated_at)"></span>
              </div>
              <template x-if="f.heal_hint">
                <div class="heal" x-text="f.heal_hint"></div>
              </template>
              <div class="actions">
                <template x-if="f.status === 'blocked'">
                  <button class="approve" @click="act(f.id, 'approve')">approve</button>
                </template>
                <template x-if="f.status === 'blocked'">
                  <button class="retry" @click="act(f.id, 'retry')">retry</button>
                </template>
                <a class="btn" :href="langfuseUrl(f.id)" target="_blank">trace</a>
                <a class="btn" :href="ghBranchUrl(f)" target="_blank" x-show="f.status !== 'pending'">branch</a>
                <button @click="toggleHistory(f.id)" x-text="history[f.id] ? 'hide history' : 'history'"></button>
                <a class="btn" :href="'/replay/' + encodeURIComponent(f.id)" target="_blank">replay</a>
                <template x-if="f.status === 'blocked' || (f.heal_attempts || 0) > 0">
                  <button @click="replayRetry(f)" title="Clear hint + heal_attempts and re-queue from scratch">fresh retry</button>
                </template>
                <template x-if="f.status === 'done'">
                  <button @click="confirmCleanup(f)" title="Delete origin branch + local worktree">cleanup</button>
                </template>
                <button class="nuke" @click="confirmNuke(f)">nuke</button>
              </div>
              <template x-if="history[f.id]">
                <div class="history-box">
                  <template x-for="h in history[f.id]" :key="h.ts + h.to">
                    <span class="row">
                      <span class="ts" x-text="fmtTs(h.ts)"></span>
                      <span class="from-to" x-text="(h.from || '—') + ' → ' + h.to"></span>
                      <span x-show="h.actor" x-text="' [' + h.actor + ']'"></span>
                      <span x-show="h.reason" x-text="' — ' + (h.reason || '').slice(0, 120)"></span>
                    </span>
                  </template>
                  <div x-show="!history[f.id].length">no transitions recorded yet (pre-608d1ff features won't have any).</div>
                </div>
              </template>
            </div>
          </template>
          <div class="empty" x-show="!featuresByStatus(col.match).length">nothing here</div>
        </div>
      </div>
    </template>
  </div>

  <div class="toast" x-show="toast" x-text="toast" :class="toastErr ? 'err' : ''"></div>

  <template x-if="schedule !== null">
    <div>
      <div class="modal-backdrop" @click="schedule = null"></div>
      <div class="modal" style="inset: 8% 8%;">
        <button class="close" @click="schedule = null">close</button>
        <h2>Scheduled recurring features</h2>
        <p style="font-size: 11px; color: var(--muted);">
          Edit the JSON below and save. Scheduler re-reads every 60s; no restart needed.<br>
          Format: <code>[{"name":"...","every_hours":168,"last_fire_ts":0,"feature":{"id_prefix":"...","name":"...","description":"...","kind":"security","priority":"medium","repos":["hearth"]}}]</code>
        </p>
        <textarea x-model="scheduleJson" rows="22" style="width:100%;background:#0d1117;color:var(--fg);border:1px solid var(--border);padding:8px;font-family:SFMono-Regular,Menlo,monospace;font-size:11px;"></textarea>
        <div style="margin-top:8px;">
          <button style="background:var(--done);color:#fff;border:0;padding:6px 12px;border-radius:3px;cursor:pointer;" @click="saveSchedule()">save</button>
          <span class="meta" x-text="scheduleStatus"></span>
        </div>
      </div>
    </div>
  </template>

  <template x-if="addForm">
    <div>
      <div class="modal-backdrop" @click="addForm = null"></div>
      <div class="modal" style="inset: 10% 20%;">
        <button class="close" @click="addForm = null">close</button>
        <h2>Add feature / bug</h2>
        <form @submit.prevent="submitAdd()" style="display: grid; grid-template-columns: 120px 1fr; gap: 8px 12px; align-items: center; font-size: 12px;">
          <label>kind</label>
          <select x-model="addForm.kind" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px;">
            <option value="feature">feature</option>
            <option value="bug">bug</option>
          </select>
          <label>id (kebab-case)</label>
          <input x-model="addForm.id" required placeholder="my-new-thing" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px 8px;" />
          <label>name</label>
          <input x-model="addForm.name" required placeholder="Human-readable title" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px 8px;" />
          <label>priority</label>
          <select x-model="addForm.priority" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px;">
            <option>critical</option><option>high</option><option selected>medium</option><option>low</option>
          </select>
          <label>repos (csv)</label>
          <input x-model="addForm.reposCsv" placeholder="hearth" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px 8px;" />
          <label>description</label>
          <textarea x-model="addForm.description" required rows="4" placeholder="What needs to be built / what's broken" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px 8px; font-family: inherit;"></textarea>
          <label>acceptance</label>
          <input x-model="addForm.acceptance" placeholder="Concrete done condition (what proves it works)" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px 8px;" />
          <template x-if="addForm.kind === 'bug'">
            <label>repro command</label>
          </template>
          <template x-if="addForm.kind === 'bug'">
            <input x-model="addForm.repro" required placeholder="go test ./... -run TestFoo (must fail today)" style="background: #0d1117; color: var(--fg); border: 1px solid var(--border); padding: 4px 8px;" />
          </template>
          <div></div>
          <div><button type="submit" style="background: var(--done); color: #fff; border: 0; padding: 6px 14px; border-radius: 3px; cursor: pointer;">queue</button></div>
        </form>
      </div>
    </div>
  </template>

  <template x-if="analytics">
    <div>
      <div class="modal-backdrop" @click="analytics = null"></div>
      <div class="modal">
        <button class="close" @click="analytics = null">close</button>
        <h2>
          Prompt version analytics
          <span style="color: var(--muted); font-weight: 400; font-size: 12px;">
            (<span x-text="analytics.total_transitions"></span> transitions ·
            best trusted: <span x-text="analytics.best_trusted_version || 'n/a'"></span>
            <span x-show="analytics.best_trusted_done_rate !== null">
              @ <span x-text="(analytics.best_trusted_done_rate * 100).toFixed(1) + '%'"></span>
            </span>)
          </span>
        </h2>
        <table>
          <thead>
            <tr>
              <th>version</th>
              <th>first_seen</th>
              <th>last_seen</th>
              <th>features</th>
              <th>done</th>
              <th>blocked</th>
              <th>done_rate</th>
              <th>top failure reasons</th>
            </tr>
          </thead>
          <tbody>
            <template x-for="v in analytics.versions" :key="v.prompts_version">
              <tr>
                <td x-text="v.prompts_version"></td>
                <td x-text="v.first_seen ? v.first_seen.slice(0, 16).replace('T', ' ') : '—'"></td>
                <td x-text="v.last_seen ? v.last_seen.slice(0, 16).replace('T', ' ') : '—'"></td>
                <td x-text="v.feature_count"></td>
                <td x-text="v.terminal_done"></td>
                <td x-text="v.terminal_blocked"></td>
                <td>
                  <span class="rate" :class="v.done_rate >= 0.75 ? 'high' : v.done_rate < 0.5 ? 'low' : ''" x-text="(v.done_rate * 100).toFixed(1) + '%'"></span>
                  <span x-show="v.low_confidence" style="color: var(--muted); font-size: 10px;"> (n&lt;10)</span>
                </td>
                <td class="reasons">
                  <template x-for="r in v.top_reasons" :key="r.reason">
                    <div><b x-text="r.count"></b> <span x-text="r.reason"></span></div>
                  </template>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </div>
  </template>

<script>
function kanban() {
  return {
    features: [],
    stats: null,
    config: null,
    history: {},
    analytics: null,
    addForm: null,
    schedule: null,
    scheduleJson: '',
    scheduleStatus: '',
    filterText: '',
    kindFilter: '',
    riskFilter: '',
    toast: '',
    toastErr: false,
    lastRefresh: Date.now(),
    sinceLabel: '0',
    // Ordered left→right as the natural flow. Escalated sits between
    // blocked and done to make it obvious these need human attention.
    columns: [
      { key: 'pending',      label: 'Pending',      color: 'var(--pending)', match: ['pending'], escalated: false },
      { key: 'implementing', label: 'Implementing', color: 'var(--impl)',    match: ['implementing', 'researching', 'reviewing'], escalated: false },
      { key: 'blocked',      label: 'Blocked',      color: 'var(--blocked)', match: ['blocked'], escalated: false },
      { key: 'escalated',    label: 'Escalated',    color: '#8957e5',        match: ['blocked'], escalated: true },
      { key: 'done',         label: 'Done',         color: 'var(--done)',    match: ['done'], escalated: false },
    ],
    get blockedFeatures() { return this.features.filter(f => f.status === 'blocked'); },
    get escalatedFeatures() { return this.features.filter(f => f.status === 'blocked' && (f.heal_attempts || 0) >= 3); },
    init() {
      this.refresh();
      setInterval(() => this.refresh(), 10000);
      setInterval(() => {
        this.sinceLabel = Math.floor((Date.now() - this.lastRefresh) / 1000).toString();
      }, 1000);
    },
    async refresh() {
      try {
        const [fr, sr, cr] = await Promise.all([fetch('/features'), fetch('/stats'), fetch('/config')]);
        if (!fr.ok) throw new Error('features HTTP ' + fr.status);
        this.features = await fr.json();
        if (sr.ok) this.stats = await sr.json();
        if (cr.ok) this.config = await cr.json();
        this.lastRefresh = Date.now();
        this.sinceLabel = '0';
      } catch (e) {
        this.flash('refresh failed: ' + e.message, true);
      }
    },
    ageLabel(iso) {
      if (!iso) return '';
      const delta = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
      if (delta < 60) return delta + 's';
      if (delta < 3600) return Math.floor(delta / 60) + 'm';
      if (delta < 86400) return Math.floor(delta / 3600) + 'h';
      return Math.floor(delta / 86400) + 'd';
    },
    fmtTs(iso) {
      if (!iso) return '';
      const d = new Date(iso);
      return d.toISOString().slice(5, 16).replace('T', ' ');
    },
    openAddModal() {
      this.addForm = {
        kind: 'feature',
        id: '',
        name: '',
        priority: 'medium',
        reposCsv: 'hearth',
        description: '',
        acceptance: '',
        repro: '',
      };
    },
    async submitAdd() {
      const f = this.addForm;
      const body = {
        id: f.id.trim(),
        name: f.name.trim(),
        description: f.description.trim(),
        priority: f.priority,
        kind: f.kind,
        repos: f.reposCsv.split(',').map(r => r.trim()).filter(Boolean),
        acceptance_criteria: f.acceptance.trim(),
      };
      if (f.kind === 'bug') body.repro_command = f.repro.trim();
      try {
        const r = await fetch('/features', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const resp = await r.json();
        if (!r.ok) throw new Error(resp.detail || 'HTTP ' + r.status);
        this.flash('queued ' + resp.id);
        this.addForm = null;
        await this.refresh();
      } catch (e) {
        this.flash('queue failed: ' + e.message, true);
      }
    },
    kindColor(kind) {
      const map = {
        bug: 'background:#da3633;color:#fff;',
        refactor: 'background:#8957e5;color:#fff;',
        schema: 'background:#1f6feb;color:#fff;',
        security: 'background:#f85149;color:#fff;',
        incident: 'background:#ff6f00;color:#fff;',
        'perf-revert': 'background:#0891a3;color:#fff;',
      };
      return map[kind] || 'background:#21262d;color:var(--muted);';
    },
    async replayRetry(f) {
      if (!confirm('Clear heal state + hint and re-queue ' + f.id + '? Next attempt has no prior context.')) return;
      try {
        const r = await fetch('/features/' + encodeURIComponent(f.id) + '/replay-retry', { method: 'POST' });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        this.flash('fresh-retry: ' + f.id);
        await this.refresh();
      } catch (e) {
        this.flash('fresh-retry failed: ' + e.message, true);
      }
    },
    async loadSchedule() {
      try {
        const r = await fetch('/schedule');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const data = await r.json();
        this.scheduleJson = JSON.stringify(data, null, 2);
        this.scheduleStatus = '';
        this.schedule = data;
      } catch (e) {
        this.flash('schedule fetch failed: ' + e.message, true);
      }
    },
    async saveSchedule() {
      let parsed;
      try {
        parsed = JSON.parse(this.scheduleJson);
      } catch (e) {
        this.scheduleStatus = 'invalid JSON: ' + e.message;
        return;
      }
      try {
        const r = await fetch('/schedule', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(parsed),
        });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        this.scheduleStatus = 'saved ' + body.count + ' entries at ' + new Date().toLocaleTimeString();
        this.flash('schedule saved');
      } catch (e) {
        this.scheduleStatus = 'save failed: ' + e.message;
      }
    },
    async loadAnalytics() {
      try {
        const r = await fetch('/prompt-analytics');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        this.analytics = await r.json();
      } catch (e) {
        this.flash('analytics fetch failed: ' + e.message, true);
      }
    },
    async toggleHistory(id) {
      if (this.history[id]) {
        delete this.history[id];
        return;
      }
      try {
        const r = await fetch('/features/' + encodeURIComponent(id) + '/history');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const body = await r.json();
        this.history[id] = body.transitions || [];
      } catch (e) {
        this.flash('history fetch failed: ' + e.message, true);
      }
    },
    featuresByStatus(statuses) {
      return this.features.filter(f => statuses.includes(f.status));
    },
    // Splits blocked features: escalated = heal_attempts >= 3 (healer gave
    // up), blocked = everything else that's still in the heal rotation.
    // Search filter is applied on every column — case-insensitive match
    // against id, name, and repo list.
    featuresByColumn(col) {
      let base = this.features.filter(f => col.match.includes(f.status));
      if (col.key === 'blocked') base = base.filter(f => (f.heal_attempts || 0) < 3);
      if (col.key === 'escalated') base = base.filter(f => (f.heal_attempts || 0) >= 3);
      if (this.kindFilter) base = base.filter(f => f.kind === this.kindFilter);
      if (this.riskFilter) base = base.filter(f => (f.risk_tier || 'low') === this.riskFilter);
      const q = this.filterText.trim().toLowerCase();
      if (!q) return base;
      return base.filter(f =>
        (f.id || '').toLowerCase().includes(q)
        || (f.name || '').toLowerCase().includes(q)
        || (f.repos || []).some(r => r.toLowerCase().includes(q))
      );
    },
    async act(id, action) {
      try {
        const r = await fetch('/features/' + encodeURIComponent(id) + '/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action }),
        });
        const body = await r.json();
        if (!r.ok) throw new Error(body.detail || 'HTTP ' + r.status);
        this.flash(action + ': ' + id);
        await this.refresh();
      } catch (e) {
        this.flash(action + ' failed: ' + e.message, true);
      }
    },
    async bulkApproveBlocked() {
      const ids = this.blockedFeatures.map(f => f.id);
      if (!ids.length) return;
      if (!confirm('Mark ' + ids.length + ' blocked feature(s) as approved/done?')) return;
      for (const id of ids) {
        await this.act(id, 'approve');
      }
      this.flash('approved ' + ids.length + ' features');
    },
    async bulkRetryEscalated() {
      const ids = this.escalatedFeatures.map(f => f.id);
      if (!ids.length) return;
      if (!confirm('Reset heal_attempts and re-queue ' + ids.length + ' escalated feature(s)?')) return;
      for (const id of ids) {
        await this.act(id, 'retry');
      }
      this.flash('retried ' + ids.length + ' escalated features');
    },
    confirmNuke(f) {
      if (!confirm('Nuke ' + f.id + ' (' + f.name + ')? This removes it from the backlog.')) return;
      this.act(f.id, 'nuke');
    },
    confirmCleanup(f) {
      if (!confirm('Delete origin branch feat/' + f.id + ' and any local worktree? Use after the PR is merged.')) return;
      this.act(f.id, 'cleanup_branch');
    },
    langfuseUrl(featureId) {
      // Prefer the server-configured langfuse_public_url (from /config);
      // falls back to localhost for local dev. Empty URL produces a JS
      // link to "#" which is a no-op — the button stays present but
      // harmless until the operator configures the URL.
      const base = (this.stats && this.stats.langfuse_public) || (this.config && this.config.urls && this.config.urls.langfuse_public) || '';
      if (!base) return '#';
      return base + '/project?search=' + encodeURIComponent('feature:' + featureId);
    },
    ghBranchUrl(f) {
      const repo = f.repos && f.repos.length ? f.repos[0] : 'hearth';
      return 'https://github.com/ghndrx/' + repo + '/tree/' + encodeURIComponent(f.branch);
    },
    flash(msg, err = false) {
      this.toast = msg;
      this.toastErr = err;
      setTimeout(() => { this.toast = ''; }, 3500);
    },
  };
}
</script>
</body>
</html>
"""
