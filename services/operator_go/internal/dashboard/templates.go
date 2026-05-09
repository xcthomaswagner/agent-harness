package dashboard

const shellHTML = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="operator-backend" content="{{.Backend}}">
  <title>Agentic Harness - Operator Go</title>
  <link rel="stylesheet" href="/operator/operator-go.css">
</head>
<body>
  <div id="app"></div>
  <script src="/operator/operator-go.js"></script>
</body>
</html>`

const appCSS = `
:root {
  --bg: #f6f8fb;
  --panel: #ffffff;
  --ink: #07101f;
  --muted: #60708a;
  --line: #cbd6e6;
  --line-soft: #e4eaf3;
  --accent: #315eea;
  --ok: #2f8f46;
  --warn: #c87516;
  --err: #d3382d;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--sans); }
a { color: inherit; text-decoration: none; }
button, input, select, textarea { font: inherit; }
.shell { min-height: 100vh; display: grid; grid-template-columns: 220px minmax(0, 1fr); }
.side { position: sticky; top: 0; height: 100vh; border-right: 1px solid var(--line); padding: 20px 18px; display: flex; flex-direction: column; gap: 28px; background: #f9fbfe; }
.brand { display: flex; align-items: center; gap: 10px; font-family: var(--mono); font-size: 12px; letter-spacing: .12em; text-transform: uppercase; font-weight: 700; }
.glyph { width: 20px; height: 20px; background: #0b1322; position: relative; }
.glyph:after { content: ""; position: absolute; width: 6px; height: 6px; left: 7px; top: 7px; background: var(--accent); }
.navGroup { display: flex; flex-direction: column; gap: 3px; }
.navHd { padding: 5px 8px; color: var(--muted); font: 10px var(--mono); letter-spacing: .14em; text-transform: uppercase; }
.navItem { padding: 8px 10px; border-left: 2px solid transparent; color: #263348; font: 11px var(--mono); letter-spacing: .05em; text-transform: uppercase; }
.navItem:hover, .navItem.on { background: var(--panel); color: var(--ink); }
.navItem.on { border-left-color: var(--accent); }
.who { margin-top: auto; padding-top: 18px; border-top: 1px solid var(--line); color: var(--muted); font: 10px var(--mono); line-height: 1.5; }
.main { min-width: 0; }
.top { position: sticky; top: 0; z-index: 5; height: 52px; border-bottom: 1px solid var(--line); background: rgba(246,248,251,.96); display: flex; align-items: center; gap: 12px; padding: 0 24px; }
.crumb { font: 10px var(--mono); letter-spacing: .14em; text-transform: uppercase; color: var(--muted); }
.live { margin-left: auto; font: 10px var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); display: flex; align-items: center; gap: 8px; }
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.content { padding: 32px 40px 48px; max-width: 1600px; }
.head { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; padding-bottom: 28px; border-bottom: 1px solid var(--line); margin-bottom: 32px; }
.sup { display: block; margin-bottom: 10px; font: 10px var(--mono); color: var(--muted); letter-spacing: .14em; text-transform: uppercase; }
h1 { margin: 0; font-size: 40px; line-height: 1.05; letter-spacing: -.01em; }
.sub { margin-top: 14px; color: #40506a; font-size: 14px; max-width: 560px; }
.metric { border-left: 1px solid var(--line); padding-left: 28px; display: flex; align-items: baseline; gap: 18px; }
.metric b { font-size: 54px; font-weight: 400; }
.metric span { color: #40506a; font: 10px var(--mono); letter-spacing: .16em; text-transform: uppercase; }
.section { margin-bottom: 40px; }
.sectionHd { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; color: var(--muted); font: 11px var(--mono); letter-spacing: .12em; text-transform: uppercase; }
.grid2 { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(280px, .85fr); gap: 28px; align-items: start; margin-bottom: 40px; }
.cardGrid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); border-top: 1px solid var(--line); border-left: 1px solid var(--line); }
.card { background: rgba(255,255,255,.45); border-right: 1px solid var(--line); border-bottom: 1px solid var(--line); padding: 18px; }
.cardTitle { font: 11px/1.25 var(--mono); letter-spacing: .06em; text-transform: uppercase; margin-bottom: 14px; overflow-wrap: anywhere; }
.kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
.kpi small { display: block; color: var(--muted); font: 9px var(--mono); letter-spacing: .12em; text-transform: uppercase; }
.kpi b { display: block; margin-top: 4px; font-size: 20px; font-weight: 500; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
.chip, .btn { border: 1px solid #91a1bb; background: transparent; color: #263348; padding: 6px 10px; font: 11px var(--mono); letter-spacing: .08em; text-transform: uppercase; cursor: pointer; }
.chip.on { color: var(--accent); border-color: var(--accent); background: rgba(49,94,234,.06); }
.btn.dark { background: #07101f; color: #fff; border-color: #07101f; }
.btn.err { color: var(--err); border-color: var(--err); }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.tableWrap { border: 1px solid var(--line); background: rgba(255,255,255,.35); overflow-x: auto; }
.tools { display: flex; align-items: center; justify-content: space-between; padding: 10px 12px; border-bottom: 1px solid var(--line-soft); color: var(--muted); font: 11px var(--mono); }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; background: rgba(255,255,255,.45); border-bottom: 1px solid var(--line); color: var(--muted); padding: 10px 12px; font: 10px var(--mono); letter-spacing: .12em; text-transform: uppercase; font-weight: 400; }
td { border-bottom: 1px solid var(--line-soft); padding: 13px 12px; font-size: 13px; vertical-align: top; }
tr.selectable { cursor: pointer; }
tr.selectable:hover { background: rgba(255,255,255,.55); }
.mono { font-family: var(--mono); }
.muted { color: var(--muted); }
.pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); padding: 4px 8px; font: 10px var(--mono); letter-spacing: .1em; text-transform: uppercase; white-space: nowrap; }
.pill:before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
.pill.ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 60%, var(--line)); }
.pill.warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 60%, var(--line)); }
.pill.err { color: var(--err); border-color: color-mix(in srgb, var(--err) 60%, var(--line)); }
.pill.active { color: var(--accent); border-color: var(--accent); }
.runsLayout { display: grid; grid-template-columns: minmax(680px, 1fr) minmax(360px, .72fr); border: 1px solid var(--line); }
.rail { border-left: 1px solid var(--line); padding: 20px; display: flex; flex-direction: column; gap: 18px; min-width: 0; }
.railTitle { font-size: 20px; }
.phaseBar { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
.phase { border-top: 3px solid var(--line); padding-top: 8px; color: var(--muted); font: 11px var(--mono); }
.phase.done { border-color: var(--ok); color: var(--ok); }
.phase.active { border-color: var(--accent); color: var(--accent); }
.phase.fail { border-color: var(--err); color: var(--err); }
.events { display: grid; gap: 8px; }
.event { border: 1px solid var(--line-soft); padding: 10px; background: rgba(255,255,255,.35); font-size: 12px; }
.eventHead { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font: 10px var(--mono); margin-bottom: 6px; }
.notice { border: 1px solid var(--line); padding: 12px; color: var(--muted); font: 12px var(--mono); }
.notice.err { color: var(--err); border-color: var(--err); }
.formGrid { display: grid; grid-template-columns: 240px minmax(280px, 1fr); gap: 12px; align-items: end; margin-bottom: 18px; }
label { display: grid; gap: 6px; color: var(--muted); font: 10px var(--mono); letter-spacing: .12em; text-transform: uppercase; }
input, select, textarea { width: 100%; border: 1px solid var(--line); background: rgba(255,255,255,.6); padding: 9px 10px; color: var(--ink); }
textarea { min-height: 520px; font: 12px/1.55 var(--mono); text-transform: none; letter-spacing: 0; }
@media (max-width: 1280px) { .runsLayout, .grid2 { grid-template-columns: 1fr; } .rail { border-left: 0; border-top: 1px solid var(--line); } }
@media (max-width: 860px) { .shell { grid-template-columns: 1fr; } .side { position: relative; height: auto; } .content { padding: 24px; } .head { align-items: flex-start; flex-direction: column; } .metric { border-left: 0; padding-left: 0; } .formGrid { grid-template-columns: 1fr; } }
`

const appJS = `
(function () {
  const app = document.getElementById('app');
  const state = {
    route: routeFromPath(location.pathname),
    runs: [],
    bucket: 'attention',
    includeHidden: false,
    selectedRun: null,
    profiles: [],
    activeProfile: '',
    lessons: [],
    lessonFilter: 'all',
    workflow: { options: null, draft: null, draftProfile: '', draftRepoPath: '', profile: '', repoPath: '', editor: '' },
    message: ''
  };

  const nav = [
    { group: 'Operate', items: [
      { label: 'Command Center', path: '/operator/' },
      { label: 'Runs', path: '/operator/runs' }
    ]},
    { group: 'Improve', items: [
      { label: 'Client Health', path: '/operator/autonomy' },
      { label: 'Learning', path: '/operator/learning' }
    ]},
    { group: 'Setup', items: [
      { label: 'Repo Workflow', path: '/operator/repo-workflow' }
    ]}
  ];

  function boot() {
    render();
    loadRoute();
    window.addEventListener('popstate', function () {
      state.route = routeFromPath(location.pathname);
      render();
      loadRoute();
    });
    document.addEventListener('click', onClick);
    document.addEventListener('change', onChange);
    document.addEventListener('input', onInput);
  }

  function onClick(event) {
    const link = event.target.closest('[data-nav]');
    if (link) {
      event.preventDefault();
      navigate(link.getAttribute('href'));
      return;
    }
    const action = event.target.closest('[data-action]');
    if (!action) return;
    const name = action.getAttribute('data-action');
    if (name === 'bucket') { state.bucket = action.getAttribute('data-value'); state.selectedRun = null; renderRuns(); return; }
    if (name === 'toggle-hidden') { state.includeHidden = !state.includeHidden; state.selectedRun = null; loadRuns(renderRuns); return; }
    if (name === 'select-run') { state.selectedRun = action.getAttribute('data-id'); renderRuns(); return; }
    if (name === 'trace-state') { postTraceState(action.getAttribute('data-id'), action.getAttribute('data-state')); return; }
    if (name === 'lesson-filter') { state.lessonFilter = action.getAttribute('data-value'); loadLearning(); return; }
    if (name === 'lesson-action') { postLesson(action.getAttribute('data-id'), action.getAttribute('data-transition')); return; }
    if (name === 'workflow-draft') { generateWorkflow(); return; }
    if (name === 'workflow-save') { saveWorkflow(); return; }
  }

  function onChange(event) {
    if (event.target.id === 'workflow-profile') {
      state.workflow.profile = event.target.value;
      const option = (state.workflow.options ? state.workflow.options.profiles : []).find(function (p) { return p.client_profile === state.workflow.profile; });
      state.workflow.repoPath = option ? option.repo_path : '';
      state.workflow.draft = null;
      state.workflow.draftProfile = '';
      state.workflow.draftRepoPath = '';
      state.workflow.editor = '';
      renderRepoWorkflow();
    }
  }

  function onInput(event) {
    if (event.target.id === 'workflow-repo') {
      state.workflow.repoPath = event.target.value;
      updateWorkflowDraftStatus();
    }
    if (event.target.id === 'workflow-editor') {
      state.workflow.editor = event.target.value;
      updateWorkflowDraftStatus();
    }
  }

  function navigate(path) {
    history.pushState({}, '', path);
    state.route = routeFromPath(location.pathname);
    render();
    loadRoute();
  }

  function routeFromPath(path) {
    const parts = path.replace(/^\/operator\/?/, '').split('/').filter(Boolean);
    if (parts[0] === 'runs') return { name: 'runs' };
    if (parts[0] === 'tickets') return { name: 'runs' };
    if (parts[0] === 'traces' && parts[1]) return { name: 'trace-detail', id: parts[1] };
    if (parts[0] === 'traces') return { name: 'runs' };
    if (parts[0] === 'autonomy') return { name: 'autonomy', profile: parts[1] || '' };
    if (parts[0] === 'learning') return { name: 'learning' };
    if (parts[0] === 'repo-workflow') return { name: 'repo-workflow' };
    return { name: 'home' };
  }

  function render() {
    const crumbs = crumbFor(state.route);
    app.innerHTML = '<div class="shell">' + sidebar() + '<main class="main"><div class="top"><span class="crumb">' + esc(crumbs) + '</span><span class="live"><span class="dot"></span>GO FRONTEND</span></div><div class="content" id="view"></div></main></div>';
    if (state.route.name === 'home') renderHome();
    if (state.route.name === 'runs') renderRuns();
    if (state.route.name === 'trace-detail') renderTraceDetail();
    if (state.route.name === 'autonomy') renderAutonomy();
    if (state.route.name === 'learning') renderLearning();
    if (state.route.name === 'repo-workflow') renderRepoWorkflow();
  }

  function sidebar() {
    let out = '<aside class="side"><div class="brand"><span class="glyph"></span><span>Agentic Harness</span></div>';
    nav.forEach(function (group) {
      out += '<nav class="navGroup"><div class="navHd">' + group.group + '</div>';
      group.items.forEach(function (item) {
        out += '<a data-nav class="navItem ' + (navOn(item.path) ? 'on' : '') + '" href="' + item.path + '">' + item.label + '</a>';
      });
      out += '</nav>';
    });
    return out + '<div class="who"><b>Operator</b>Go frontend<br>proxy mode</div></aside>';
  }

  function navOn(path) {
    if (path === '/operator/' && state.route.name === 'home') return true;
    if (path === '/operator/runs' && (state.route.name === 'runs' || state.route.name === 'trace-detail')) return true;
    if (path === '/operator/autonomy' && state.route.name === 'autonomy') return true;
    if (path === '/operator/learning' && state.route.name === 'learning') return true;
    if (path === '/operator/repo-workflow' && state.route.name === 'repo-workflow') return true;
    return false;
  }

  function crumbFor(route) {
    if (route.name === 'home') return 'Operate / Command Center';
    if (route.name === 'runs') return 'Operate / Runs';
    if (route.name === 'trace-detail') return 'Operate / Runs / ' + route.id;
    if (route.name === 'autonomy') return 'Improve / Client Health';
    if (route.name === 'learning') return 'Improve / Learning';
    if (route.name === 'repo-workflow') return 'Setup / Repo Workflow';
    return 'Operator';
  }

  function loadRoute() {
    if (state.route.name === 'home') { loadRuns(renderHome); loadProfiles(renderHome); loadLessonCounts(renderHome); }
    if (state.route.name === 'runs') loadRuns(renderRuns);
    if (state.route.name === 'trace-detail') loadTraceDetail();
    if (state.route.name === 'autonomy') loadProfiles(renderAutonomy);
    if (state.route.name === 'learning') loadLearning();
    if (state.route.name === 'repo-workflow') loadWorkflowOptions();
  }

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ headers: { Accept: 'application/json' }, credentials: 'same-origin' }, options || {}));
    if (!res.ok) throw new Error(res.status + ': ' + await res.text());
    if (res.status === 204) return {};
    return res.json();
  }

  async function loadRuns(done) {
    try {
      const data = await api('/api/operator/traces?limit=500&offset=0&include_hidden=' + (state.includeHidden ? 'true' : 'false'));
      state.runs = data.traces || [];
      if (done) done();
    } catch (error) { showError(error); }
  }

  async function loadProfiles(done) {
    try {
      const data = await api('/api/operator/profiles');
      state.profiles = data.profiles || [];
      if (!state.activeProfile && state.profiles[0]) state.activeProfile = state.profiles[0].id;
      if (done) done();
    } catch (error) { showError(error); }
  }

  async function loadLessonCounts(done) {
    try {
      state.lessonCounts = await api('/api/operator/lessons/counts');
      if (done) done();
    } catch (error) { showError(error); }
  }

  function renderHome() {
    const counts = runCounts(state.runs);
    const attention = filterRuns(state.runs, 'attention').slice(0, 5);
    const active = filterRuns(state.runs, 'active').slice(0, 5);
    const successful = filterRuns(state.runs, 'successful').slice(0, 5);
    view().innerHTML = head('Operate / command center', 'Command Center', 'Current work, intervention points, and improvement signals.', counts.attention, 'Need attention') +
      section('Needs attention', '<a data-nav href="/operator/runs">All runs -></a>', runTable(attention, false)) +
      '<div class="grid2"><div>' + section('Active runs', active.length + ' active', runTable(active, false)) + '</div><div>' + lessonSummary() + '</div></div>' +
      section('Client health', state.profiles.length + ' total', profileCards()) +
      section('Recent successful runs', '<a data-nav href="/operator/runs">All runs -></a>', runTable(successful, false));
  }

  function renderRuns() {
    const counts = runCounts(state.runs);
    const rows = filterRuns(state.runs, state.bucket);
    if (!state.selectedRun && rows[0]) state.selectedRun = rows[0].id;
    const selected = rows.find(function (row) { return row.id === state.selectedRun; });
    view().innerHTML = head('Operate / runs', 'Runs', 'Operational queue and completed run evidence.', counts.attention, 'Need attention') +
      chips(counts) +
      '<div class="runsLayout"><div>' + tools('Showing ' + rows.length + ' of ' + rows.length) + runTable(rows, true) + '</div><aside class="rail">' + runRail(selected) + '</aside></div>';
  }

  function renderTraceDetail() {
    view().innerHTML = head('Runs / ' + esc(state.route.id), 'Run Detail', 'Loading run evidence.', '-', 'Status') + '<div class="notice">Loading trace...</div>';
  }

  async function loadTraceDetail() {
    try {
      const id = state.route.id;
      const detail = await api('/api/operator/traces/' + encodeURIComponent(id));
      const agents = await safeApi('/api/operator/tickets/' + encodeURIComponent(id) + '/agents');
      const activity = await safeApi('/api/operator/tickets/' + encodeURIComponent(id) + '/activity-summary');
      const label = runLabel(detail);
      view().innerHTML = head('Runs / ' + esc(detail.id), detail.title || detail.id, 'Run started ' + emptyDash(detail.started_at) + ' / elapsed ' + emptyDash(detail.elapsed), label, 'Outcome') +
        traceActions(detail) +
        section('Phase timeline', '', phaseTimeline(detail.phases || [])) +
        section('Activity summary', '', activitySummary(activity)) +
        section('Session panels', (agents.agents || []).length + ' teammates', agentCards(agents.agents || [])) +
        section('Raw events', (detail.events || []).length + ' events', eventList(detail.events || []));
    } catch (error) { showError(error); }
  }

  async function safeApi(path) {
    try { return await api(path); } catch (error) { return {}; }
  }

  function renderAutonomy() {
    const active = state.route.profile || state.activeProfile || (state.profiles[0] ? state.profiles[0].id : '');
    state.activeProfile = active;
    view().innerHTML = head('Improve / client health', active ? 'Client Health - ' + active : 'Client Health', 'Delivery quality and automation over 30 days.', state.profiles.length, 'Profiles') +
      '<div class="chips">' + state.profiles.map(function (p) { return '<button class="chip ' + (p.id === active ? 'on' : '') + '" data-nav href="/operator/autonomy/' + enc(p.id) + '">' + esc(p.id) + '</button>'; }).join('') + '</div>' +
      '<div id="autonomy-detail" class="notice">Select a profile to inspect quality trends.</div>';
    if (active) loadAutonomyDetail(active);
  }

  async function loadAutonomyDetail(profile) {
    try {
      const data = await api('/api/operator/autonomy/' + encodeURIComponent(profile));
      const byType = data.by_type || [];
      document.getElementById('autonomy-detail').outerHTML =
        '<div class="cardGrid">' + metricCard('First-pass', pct(data.fpa), 'first-pass acceptance') + metricCard('Escapes', pct(data.escape), 'escaped defects') + metricCard('Catch rate', pct(data.catch), 'self-review coverage') + metricCard('Auto-merge', pct(data.auto_merge), 'merged automatically') + '</div>' +
        section('By ticket type', byType.length + ' types', simpleRows(['Type', 'Volume', 'First-pass', 'Escape'], byType.map(function (r) { return [r.ticket_type, r.volume, pct(r.fpa), pct(r.escape)]; })));
    } catch (error) { showError(error); }
  }

  async function loadLearning() {
    try {
      await loadLessonCounts();
      const query = state.lessonFilter === 'all' ? '' : '&status=' + encodeURIComponent(state.lessonFilter);
      const data = await api('/api/learning/candidates?limit=200&offset=0' + query);
      state.lessons = data.candidates || [];
      renderLearning();
    } catch (error) { showError(error); }
  }

  function renderLearning() {
    const counts = lessonCounts();
    view().innerHTML = head('Improve / learning', 'Lessons', 'Reusable harness improvements awaiting operator triage.', counts.proposed + counts.draft_ready, 'Awaiting triage') +
      lessonChips(counts) +
      tools('Showing ' + state.lessons.length + ' lessons') +
      simpleRows(['Lesson', 'Pattern', 'Profile', 'Impact', 'Freq', 'State', 'Actions'], state.lessons.map(function (l) {
        return [l.lesson_id, l.pattern_key, l.client_profile || '-', impact(l), l.frequency || 0, pill(l.status || 'proposed', 'cool'), lessonActions(l)];
      }));
  }

  async function loadWorkflowOptions() {
    try {
      const data = await api('/api/operator/repo-workflow/options');
      state.workflow.options = data;
      if (!state.workflow.profile && data.profiles && data.profiles[0]) {
        state.workflow.profile = data.profiles[0].client_profile;
        state.workflow.repoPath = data.profiles[0].repo_path;
      }
      renderRepoWorkflow();
    } catch (error) { showError(error); }
  }

  function renderRepoWorkflow() {
    const options = state.workflow.options ? state.workflow.options.profiles || [] : [];
    const canSave = state.workflow.editor && workflowDraftMatches();
    view().innerHTML = head('Setup / repo workflow', 'Repo Workflow', 'Generate and maintain repo-local WORKFLOW.md overlays.', options.length, 'Profiles') +
      '<div class="formGrid"><label>Client profile<select id="workflow-profile">' + options.map(function (p) { return '<option value="' + esc(p.client_profile) + '"' + (p.client_profile === state.workflow.profile ? ' selected' : '') + '>' + esc(p.client_profile) + '</option>'; }).join('') + '</select></label>' +
      '<label>Repository path<input id="workflow-repo" value="' + esc(state.workflow.repoPath || '') + '" placeholder="/path/to/client/repo"></label></div>' +
      '<div class="chips"><button class="btn dark" data-action="workflow-draft">Generate Draft</button><button id="workflow-save" class="btn" data-action="workflow-save" ' + (!canSave ? 'disabled' : '') + '>Save WORKFLOW.md</button></div>' +
      '<div id="workflow-summary">' + workflowSummary() + '</div>' +
      '<textarea id="workflow-editor" spellcheck="false">' + esc(state.workflow.editor || '') + '</textarea>';
  }

  async function generateWorkflow() {
    try {
      syncWorkflowForm();
      const data = await api('/api/operator/repo-workflow/draft', jsonPost({ client_profile: state.workflow.profile, repo_path: state.workflow.repoPath }));
      state.workflow.draft = data;
      state.workflow.draftProfile = state.workflow.profile;
      state.workflow.draftRepoPath = state.workflow.repoPath;
      state.workflow.editor = data.existing_text || data.draft_text || '';
      renderRepoWorkflow();
    } catch (error) { showError(error); }
  }

  async function saveWorkflow() {
    try {
      syncWorkflowForm();
      if (!workflowDraftMatches()) throw new Error('Regenerate the draft for the current repository before saving.');
      await api('/api/operator/repo-workflow', jsonPut({ client_profile: state.workflow.profile, repo_path: state.workflow.repoPath, content: state.workflow.editor }));
      state.message = 'WORKFLOW.md saved.';
      await generateWorkflow();
    } catch (error) { showError(error); }
  }

  async function postTraceState(id, nextState) {
    try {
      await api('/api/operator/traces/' + encodeURIComponent(id) + '/state', jsonPost({ state: nextState, reason: 'Updated from Go operator frontend', exclude_metrics: nextState === 'misfire' || nextState === 'suppressed' }));
      await loadRuns(renderRuns);
      if (state.route.name === 'trace-detail') loadTraceDetail();
    } catch (error) { showError(error); }
  }

  async function postLesson(id, transition) {
    const payload = transition === 'snooze' ? { days: 7, reason: 'Operator snoozed from Go frontend' } : { reason: 'Operator action from Go frontend' };
    try {
      await api('/api/learning/candidates/' + encodeURIComponent(id) + '/' + transition, jsonPost(payload));
      await loadLearning();
    } catch (error) { showError(error); }
  }

  function jsonPost(body) { return { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body), credentials: 'same-origin' }; }
  function jsonPut(body) { return { method: 'PUT', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body), credentials: 'same-origin' }; }

  function head(sup, title, sub, num, label) {
    return '<header class="head"><div><span class="sup">' + esc(sup) + '</span><h1>' + esc(title) + '</h1><div class="sub">' + esc(sub) + '</div></div><div class="metric"><b>' + esc(String(num)) + '</b><span>' + esc(label) + '</span></div></header>';
  }

  function section(label, right, body) {
    return '<section class="section"><div class="sectionHd"><span>' + esc(label) + '</span><span>' + right + '</span></div>' + body + '</section>';
  }

  function tools(text) { return '<div class="tools"><span>' + esc(text) + '</span></div>'; }
  function view() { return document.getElementById('view'); }
  function showError(error) { view().innerHTML = '<div class="notice err">' + esc(error.message || String(error)) + '</div>'; }

  function chips(counts) {
    const buckets = [['active','Active'], ['attention','Needs attention'], ['recent','Recent'], ['successful','Successful'], ['failed','Failed']];
    if (state.includeHidden) buckets.push(['hidden','Hidden']);
    return '<div class="chips">' + buckets.map(function (b) { return '<button class="chip ' + (state.bucket === b[0] ? 'on' : '') + '" data-action="bucket" data-value="' + b[0] + '">' + b[1] + ' <span class="mono">' + counts[b[0]] + '</span></button>'; }).join('') +
      '<button class="btn" data-action="toggle-hidden">' + (state.includeHidden ? 'Hide hidden' : 'Show hidden') + '</button></div>';
  }

  function runTable(rows, selectable) {
    if (!rows || rows.length === 0) return '<div class="notice">No runs in this bucket.</div>';
    return simpleRows(['Ticket', 'Title', 'Outcome', 'Phase', 'Elapsed'], rows.map(function (r) {
      const open = selectable ? ' data-action="select-run" data-id="' + esc(r.id) + '"' : ' data-nav href="/operator/traces/' + enc(r.id) + '"';
      return ['<span class="mono" ' + open + '>' + esc(r.id) + '</span>', esc(r.title || '-'), pill(runLabel(r), runTone(r), r.raw_status), esc(r.phase || '-'), esc(r.elapsed || '-')];
    }), selectable ? 'selectable' : '');
  }

  function runRail(row) {
    if (!row) return '<div class="notice">Select a run to inspect details.</div>';
    return '<div><div class="mono muted">' + esc(row.id) + '</div><div class="railTitle">' + esc(row.title || '-') + '</div></div>' +
      '<div class="chips">' + pill(runLabel(row), runTone(row), row.raw_status) + (row.phase ? pill('phase / ' + row.phase, 'cool') : '') + '</div>' +
      '<div class="chips"><a data-nav class="btn" href="/operator/traces/' + enc(row.id) + '">Run detail -></a>' + traceActionButtons(row) + '</div>' +
      '<div class="notice">' + (isLive(row) ? 'Live run. Use Run detail for full evidence and stream context.' : 'Archived run. Live stream closed.') + '</div>';
  }

  function traceActions(row) {
    return '<div class="chips">' + pill(runLabel(row), runTone(row), row.raw_status) + traceActionButtons(row) + '</div>';
  }

  function traceActionButtons(row) {
    const id = esc(row.id);
    const hidden = row.hidden || row.status === 'hidden';
    return '<button class="btn" data-action="trace-state" data-id="' + id + '" data-state="' + (hidden ? 'open' : 'suppressed') + '">' + (hidden ? 'Restore' : 'Hide') + '</button>' +
      '<button class="btn err" data-action="trace-state" data-id="' + id + '" data-state="misfire">Misfire</button>' +
      '<button class="btn" data-action="trace-state" data-id="' + id + '" data-state="stale">Mark stale</button>';
  }

  function phaseTimeline(phases) {
    if (!phases.length) return '<div class="notice">No phase events recorded.</div>';
    return '<div class="phaseBar">' + phases.map(function (p) { return '<div class="phase ' + esc(p.state || '') + '"><b>' + esc(p.name || p.key) + '</b><br>' + esc(p.state || 'pending') + '</div>'; }).join('') + '</div>';
  }

  function activitySummary(data) {
    if (!data || (!data.highlights && !data.warnings && !data.summary)) return '<div class="notice">No finished activity summary available.</div>';
    return '<div class="card"><div>' + esc(data.summary || '') + '</div>' + list('Highlights', data.highlights || []) + list('Warnings', data.warnings || []) + '</div>';
  }

  function agentCards(agents) {
    if (!agents.length) return '<div class="notice">No agents spawned for this run.</div>';
    return '<div class="cardGrid">' + agents.map(function (a) { return '<div class="card"><div class="cardTitle">' + esc(a.role || a.name || 'agent') + '</div><div class="muted">' + esc(a.status || '') + '</div></div>'; }).join('') + '</div>';
  }

  function eventList(events) {
    if (!events.length) return '<div class="notice">No raw events.</div>';
    return '<div class="events">' + events.slice(-200).map(function (e) { return '<div class="event"><div class="eventHead"><span>' + esc(e.t || '') + '</span><span>' + esc(e.phase || '-') + '</span></div><b>' + esc(e.ev || '') + '</b><div>' + esc(e.msg || '-') + '</div></div>'; }).join('') + '</div>';
  }

  function lessonSummary() {
    const counts = lessonCounts();
    return section('Lessons', '<a data-nav href="/operator/learning">Review lessons -></a>', '<div class="cardGrid">' + ['proposed','draft_ready','approved','applied','snoozed','rejected'].map(function (k) { return metricCard(labelFor(k), counts[k] || 0, ''); }).join('') + '</div>');
  }

  function lessonChips(counts) {
    const filters = [['all','All'], ['proposed','Proposed'], ['draft_ready','Draft'], ['approved','Approved'], ['applied','Applied'], ['snoozed','Snoozed'], ['rejected','Rejected']];
    return '<div class="chips">' + filters.map(function (f) { const n = f[0] === 'all' ? Object.keys(counts).reduce(function (s, k) { return s + (counts[k] || 0); }, 0) : counts[f[0]] || 0; return '<button class="chip ' + (state.lessonFilter === f[0] ? 'on' : '') + '" data-action="lesson-filter" data-value="' + f[0] + '">' + f[1] + ' <span class="mono">' + n + '</span></button>'; }).join('') + '</div>';
  }

  function lessonActions(l) {
    if (l.status === 'proposed') return '<button class="btn dark" data-action="lesson-action" data-transition="draft" data-id="' + esc(l.lesson_id) + '">Draft</button> <button class="btn" data-action="lesson-action" data-transition="snooze" data-id="' + esc(l.lesson_id) + '">Snooze</button> <button class="btn err" data-action="lesson-action" data-transition="reject" data-id="' + esc(l.lesson_id) + '">Reject</button>';
    if (l.status === 'draft_ready') return '<button class="btn dark" data-action="lesson-action" data-transition="approve" data-id="' + esc(l.lesson_id) + '">Approve</button>';
    return '';
  }

  function workflowSummary() {
    const d = state.workflow.draft;
    if (!d) return '<div class="notice">Generate a draft to inspect repo evidence and edit WORKFLOW.md.</div>';
    if (!workflowDraftMatches()) return '<div class="notice err">Repository or profile changed. Regenerate the draft before saving WORKFLOW.md.</div>';
    return '<div class="cardGrid">' + metricCard('Workflow', d.workflow_exists ? 'exists' : 'missing', d.workflow_path || '') + metricCard('Validation', (d.validation || []).length, '') + metricCard('Warnings', (d.warnings || []).length, '') + metricCard('Frameworks', (d.detected && d.detected.frameworks || []).join(', ') || '-', '') + '</div>';
  }

  function profileCards() {
    if (!state.profiles.length) return '<div class="notice">No profiles configured.</div>';
    return '<div class="cardGrid">' + state.profiles.map(function (p) {
      return '<a data-nav class="card" href="/operator/autonomy/' + enc(p.id) + '"><div class="cardTitle">' + esc(p.name || p.id) + '</div><div class="kpis">' + metricMini('First-pass', pct(p.fpa)) + metricMini('Escapes', pct(p.escape)) + metricMini('Auto-merge', pct(p.auto_merge)) + '</div><div class="muted mono">' + esc(p.in_flight || 0) + ' in flight / ' + esc(p.completed_24h || 0) + ' done 24h</div></a>';
    }).join('') + '</div>';
  }

  function simpleRows(headers, rows, rowClass) {
    return '<div class="tableWrap"><table><thead><tr>' + headers.map(function (h) { return '<th>' + esc(h) + '</th>'; }).join('') + '</tr></thead><tbody>' + rows.map(function (row) { return '<tr class="' + (rowClass || '') + '">' + row.map(function (cell) { return '<td>' + cell + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></div>';
  }

  function metricCard(label, value, note) { return '<div class="card"><div class="cardTitle">' + esc(label) + '</div><div style="font-size:28px">' + esc(String(value)) + '</div><div class="muted">' + esc(note || '') + '</div></div>'; }
  function metricMini(label, value) { return '<div class="kpi"><small>' + esc(label) + '</small><b>' + esc(String(value)) + '</b></div>'; }
  function list(label, items) { if (!items.length) return ''; return '<div class="cardTitle">' + esc(label) + '</div><ul>' + items.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('') + '</ul>'; }

  function runCounts(rows) {
    const counts = { active: 0, attention: 0, recent: 0, successful: 0, failed: 0, hidden: 0 };
    (rows || []).forEach(function (r) {
      if (isHidden(r)) { counts.hidden++; return; }
      counts.recent++;
      if (isActive(r)) counts.active++;
      if (needsAttention(r)) counts.attention++;
      if (isSuccessful(r)) counts.successful++;
      if (isFailed(r)) counts.failed++;
    });
    return counts;
  }

  function filterRuns(rows, bucket) {
    return (rows || []).filter(function (r) {
      if (bucket === 'active') return !isHidden(r) && isActive(r);
      if (bucket === 'attention') return !isHidden(r) && needsAttention(r);
      if (bucket === 'successful') return !isHidden(r) && isSuccessful(r);
      if (bucket === 'failed') return !isHidden(r) && isFailed(r);
      if (bucket === 'hidden') return isHidden(r);
      return !isHidden(r);
    });
  }

  function runLabel(r) {
    if (isHidden(r)) return 'Hidden';
    if (isActive(r)) return r.status === 'queued' ? 'Queued' : 'Active';
    if (isFailed(r)) return 'Failed';
    if (needsAttention(r)) return 'Needs attention';
    if (isSuccessful(r)) return 'Successful';
    return titleCase(r.raw_status || r.status || 'Run');
  }
  function runTone(r) { if (isHidden(r) || isFailed(r)) return 'err'; if (isActive(r)) return 'active'; if (needsAttention(r)) return 'warn'; if (isSuccessful(r)) return 'ok'; return 'cool'; }
  function isLive(r) { return !isHidden(r) && isActive(r); }
  function isHidden(r) { return r.hidden || r.status === 'hidden'; }
  function isActive(r) { return r.status === 'in-flight' || r.status === 'queued'; }
  function needsAttention(r) { return r.status === 'stuck' || r.lifecycle_state === 'stale' || isFailed(r) || (raw(r) === 'submitted' && String(r.elapsed || '').indexOf('>24h') >= 0); }
  function isSuccessful(r) { const v = raw(r); return !isFailed(r) && r.status === 'done' && ['complete','completed','pr created','merged','closed'].indexOf(v) >= 0; }
  function isFailed(r) { const v = raw(r); return v.indexOf('fail') >= 0 || v.indexOf('skip') >= 0 || v.indexOf('escalat') >= 0 || v.indexOf('error') >= 0; }
  function raw(r) { return String(r.raw_status || r.status || '').trim().toLowerCase(); }

  function lessonCounts() {
    const raw = state.lessonCounts && state.lessonCounts.counts || {};
    return Object.assign({ proposed: 0, draft_ready: 0, approved: 0, applied: 0, snoozed: 0, rejected: 0, reverted: 0, stale: 0 }, raw);
  }
  function impact(l) {
    const text = [l.pattern_key, l.detector_name, l.status_reason, l.proposed_delta_json].join(' ').toLowerCase();
    if (/auth|secret|credential|security|token|deploy|merge|ci|failed delivery/.test(text)) return pill('Critical', 'err');
    if ((l.frequency || 0) >= 3 || /review|qa|mcp|workflow|validation/.test(text)) return pill('High', 'warn');
    return pill('Medium', 'cool');
  }

  function pill(text, tone, title) { return '<span title="' + esc(title || text) + '" class="pill ' + esc(tone || 'cool') + '">' + esc(text) + '</span>'; }
  function pct(v) { return v === null || v === undefined || v === '' ? '-' : Math.round(Number(v) * 100) + '%'; }
  function workflowDraftMatches() { return state.workflow.draft && state.workflow.draftProfile === state.workflow.profile && state.workflow.draftRepoPath === state.workflow.repoPath; }
  function updateWorkflowDraftStatus() {
    const save = document.getElementById('workflow-save');
    const summary = document.getElementById('workflow-summary');
    const canSave = state.workflow.editor && workflowDraftMatches();
    if (save) save.disabled = !canSave;
    if (summary && state.workflow.draft) summary.innerHTML = workflowSummary();
  }
  function syncWorkflowForm() {
    const repo = document.getElementById('workflow-repo');
    const editor = document.getElementById('workflow-editor');
    if (repo) state.workflow.repoPath = repo.value;
    if (editor) state.workflow.editor = editor.value;
  }
  function emptyDash(v) { return v ? String(v) : '-'; }
  function labelFor(k) { return ({ draft_ready: 'Draft' }[k] || titleCase(k)); }
  function titleCase(v) { return String(v).split(/[\\s_-]+/).filter(Boolean).map(function (p) { return p.charAt(0).toUpperCase() + p.slice(1).toLowerCase(); }).join(' '); }
  function enc(v) { return encodeURIComponent(v || ''); }
  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  boot();
})();
`
