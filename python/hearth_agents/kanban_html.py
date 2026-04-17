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
  .board { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; padding: 16px; height: calc(100vh - 58px); overflow: hidden; }
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
      <span class="meta" x-text="'refreshed ' + sinceLabel + 's ago'"></span>
      <button @click="refresh()">refresh</button>
      <button :disabled="!blockedFeatures.length" @click="bulkApproveBlocked()" title="Mark all currently blocked as human-approved">approve all blocked</button>
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
      <div class="col">
        <div class="col-head">
          <span class="dot" :style="'background:' + col.color"></span>
          <span class="title" x-text="col.label"></span>
          <span class="count" x-text="featuresByStatus(col.match).length"></span>
        </div>
        <div class="col-body">
          <template x-for="f in featuresByStatus(col.match)" :key="f.id">
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
                <span class="age" :title="f.created_at" x-text="ageLabel(f.created_at)"></span>
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

<script>
function kanban() {
  return {
    features: [],
    stats: null,
    history: {},
    toast: '',
    toastErr: false,
    lastRefresh: Date.now(),
    sinceLabel: '0',
    columns: [
      { key: 'pending',      label: 'Pending',      color: 'var(--pending)', match: ['pending'] },
      { key: 'implementing', label: 'Implementing', color: 'var(--impl)',    match: ['implementing', 'researching', 'reviewing'] },
      { key: 'blocked',      label: 'Blocked',      color: 'var(--blocked)', match: ['blocked'] },
      { key: 'done',         label: 'Done',         color: 'var(--done)',    match: ['done'] },
    ],
    get blockedFeatures() { return this.features.filter(f => f.status === 'blocked'); },
    init() {
      this.refresh();
      setInterval(() => this.refresh(), 10000);
      setInterval(() => {
        this.sinceLabel = Math.floor((Date.now() - this.lastRefresh) / 1000).toString();
      }, 1000);
    },
    async refresh() {
      try {
        const [fr, sr] = await Promise.all([fetch('/features'), fetch('/stats')]);
        if (!fr.ok) throw new Error('features HTTP ' + fr.status);
        this.features = await fr.json();
        if (sr.ok) this.stats = await sr.json();
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
    confirmNuke(f) {
      if (!confirm('Nuke ' + f.id + ' (' + f.name + ')? This removes it from the backlog.')) return;
      this.act(f.id, 'nuke');
    },
    langfuseUrl(featureId) {
      // Langfuse filters traces by metadata/tag. If the hostname isn't live yet,
      // the link 404s harmlessly — intentional, deploy-then-deeplink.
      const base = window.location.hostname.includes('walleye-frog')
        ? 'https://langfuse.walleye-frog.ts.net'
        : 'http://localhost:3000';
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
