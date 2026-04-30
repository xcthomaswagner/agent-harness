// View router & renderers for Home / Traces / Autonomy / Learning / Trace / PR
(function(){
  const VD = window.VIEWS_DATA;
  const host = document.getElementById("view-host");
  const tLayout = document.getElementById("tickets-layout");
  const state = { view: 'home', filter: 'all', lessonFilter: 'all', profileFilter: 'all', activeProfile: 'salesforce-apex' };

  // -------- helpers --------
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmtPct = v => (Math.round(v*100)) + '%';

  function statusPill(status, label) {
    const map = { 'in-flight': 'active', 'stuck': 'warn', 'queued': 'cool', 'done': 'ok' };
    const cls = map[status] || status;
    const lab = label || ({ 'in-flight':'In-flight','stuck':'Stuck','queued':'Queued','done':'Done' }[status] || status);
    return `<span class="pill ${cls}"><span class="d"></span>${lab}</span>`;
  }
  function sevPill(sev) {
    const cls = sev === 'major' ? 'err' : sev === 'minor' ? 'warn' : 'ok';
    return `<span class="pill ${cls}"><span class="d"></span>${sev}</span>`;
  }
  function stateDot(state){
    const m = { done:'ok', active:'active', pending:'cool', fail:'err', running:'active', idle:'cool' };
    return `<span class="pd ${m[state]||'cool'} ${state==='active'||state==='running'?'active':state==='done'?'done':state==='fail'?'fail':''}"></span>`;
  }

  function sparkline(points, color){
    const w=140,h=40, max=Math.max(...points), min=Math.min(...points);
    const pts = points.map((v,i)=>{
      const x=(i/(points.length-1))*w;
      const y=h-((v-min)/(max-min||1))*(h-4)-2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none">
      <polyline points="${pts}" fill="none" stroke="${color||'currentColor'}" stroke-width="1.5"/>
    </svg>`;
  }

  // =============== VIEWS ===============

  function viewHome(){
    const p = VD.profiles;
    return `
      <div class="vhead">
        <div>
          <div class="sup">Overview · home</div>
          <div class="vt">Mission control</div>
          <div class="sub">Every run, every profile, every lesson at a glance. Drill into a trace to see what agents are doing; drill into a lesson to teach the harness.</div>
        </div>
        <div class="stat-lg">
          <div class="stat-lg-n">8</div>
          <div class="stat-lg-l">Runs · 24h</div>
        </div>
      </div>

      <section class="vsection">
        <div class="sec-hd"><h2>Client profiles</h2><a class="sec-link" data-goto="autonomy">Autonomy report →</a></div>
        <div class="profile-grid">
          ${p.map(pr => `
            <div class="pcard" data-goto-profile="${pr.id}">
              <div class="pcard-hd">
                <h3>${pr.name}</h3>
                <span class="pcard-sample">${esc(pr.sample)}</span>
              </div>
              <div class="pcard-metrics">
                <div><div class="pm-l">FPA</div><div class="pm-v">${fmtPct(pr.fpa)}</div></div>
                <div><div class="pm-l">Escape</div><div class="pm-v">${fmtPct(pr.escape)}</div></div>
                <div><div class="pm-l">Auto</div><div class="pm-v">${fmtPct(pr.auto_merge)}</div></div>
              </div>
              <div class="pcard-ft">
                <div class="pf"><span class="pf-l">In-flight</span><span class="chip sm">${pr.in_flight}</span></div>
                <div class="pf"><span class="pf-l">24h done</span><span class="chip sm">${pr.completed_24h}</span></div>
              </div>
            </div>
          `).join('')}
        </div>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h2>Lessons</h2><a class="sec-link" data-goto="learning">Triage queue →</a></div>
        <div class="lesson-strip">
          ${[
            ['Proposed', VD.lessons.filter(l=>l.state==='proposed').length],
            ['Draft',    VD.lessons.filter(l=>l.state==='draft').length],
            ['Approved', VD.lessons.filter(l=>l.state==='approved').length],
            ['Applied',  VD.lessons.filter(l=>l.state==='applied').length],
            ['Snoozed',  VD.lessons.filter(l=>l.state==='snoozed').length],
            ['Rejected', VD.lessons.filter(l=>l.state==='rejected').length],
          ].map(([k,n]) => `
            <div class="ls-cell" data-goto="learning" data-lesson-filter="${k.toLowerCase()}">
              <div class="ls-n">${n}</div>
              <div class="ls-l">${k}</div>
            </div>
          `).join('')}
        </div>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h2>Recent runs</h2><a class="sec-link" data-goto="traces">All traces →</a></div>
        ${renderTracesTable(VD.traces.slice(0,6), true)}
      </section>
    `;
  }

  function renderTracesTable(rows, compact){
    return `
      <table class="tbl ${compact?'':'tbl-lg'}">
        <thead><tr>
          <th>Trace</th><th>Title</th><th>Profile</th><th>Status</th><th>Elapsed</th><th>Author</th><th>Started</th>
        </tr></thead>
        <tbody>
          ${rows.map(t => `
            <tr data-go-trace="${t.id}" class="${t.live?'row-live':''}">
              <td class="mono">${t.id}</td>
              <td class="tcell-title">${esc(t.title)}</td>
              <td>${esc(t.profile)}</td>
              <td>${statusPill(t.status)}</td>
              <td class="mono">${t.elapsed}</td>
              <td>${esc(t.author)}</td>
              <td class="mono muted">${t.started}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }

  function viewTraces(){
    return `
      <div class="vhead">
        <div>
          <div class="sup">Overview · traces</div>
          <div class="vt">Traces</div>
          <div class="sub">Every spawned run across every client profile. Click a row for the full timeline, session panels, and raw event stream.</div>
        </div>
        <div class="vh-right">
          <div class="search vh-search">
            <span class="icon">⌕</span><input placeholder="filter traces…" id="tr-search"><span class="k">/</span>
          </div>
        </div>
      </div>
      <div class="filter-row">
        <span class="chip sm is-on" data-tfilter="all">All <span class="chip-n">${VD.traces.length}</span></span>
        <span class="chip sm" data-tfilter="in-flight">In-flight <span class="chip-n">${VD.traces.filter(t=>t.status==='in-flight').length}</span></span>
        <span class="chip sm" data-tfilter="stuck">Stuck <span class="chip-n">${VD.traces.filter(t=>t.status==='stuck').length}</span></span>
        <span class="chip sm" data-tfilter="queued">Queued <span class="chip-n">${VD.traces.filter(t=>t.status==='queued').length}</span></span>
        <span class="chip sm" data-tfilter="done">Done <span class="chip-n">${VD.traces.filter(t=>t.status==='done').length}</span></span>
        <span class="filter-sep"></span>
        ${VD.profiles.map(p=>`<span class="chip sm" data-pfilter="${p.name}">${p.name}</span>`).join('')}
      </div>
      <section class="vsection">
        <div id="tr-body">${renderTracesTable(VD.traces, false)}</div>
      </section>
    `;
  }

  function viewAutonomy(){
    const ap = VD.profiles.find(p=>p.id===state.activeProfile) || VD.profiles[0];
    const spark1 = [0.62,0.65,0.68,0.66,0.71,0.73,0.72,0.78,0.80,0.79,0.81,0.81];
    const spark2 = [0.18,0.16,0.14,0.15,0.12,0.13,0.11,0.10,0.09,0.08,0.07,0.07];
    const spark3 = [0.28,0.32,0.38,0.44,0.48,0.52,0.55,0.57,0.59,0.60,0.61,0.62];
    return `
      <div class="vhead">
        <div>
          <div class="sup">Overview · autonomy report</div>
          <div class="vt">Autonomy report — ${esc(ap.name)}</div>
          <div class="sub">First-pass acceptance, escape rate, and catch rate by profile and ticket type. Signal drift early; ship lessons against the weakest surface.</div>
        </div>
        <div class="vh-right">
          <div class="profile-switcher">
            ${VD.profiles.map(p => `
              <button class="chip sm ${p.id===state.activeProfile?'is-on':''}" data-set-profile="${p.id}">${p.name}</button>
            `).join('')}
          </div>
        </div>
      </div>

      <section class="vsection">
        <div class="metric-row-lg">
          <div class="mlg">
            <div class="mlg-l">First-pass acceptance</div>
            <div class="mlg-v">${fmtPct(ap.fpa)}</div>
            <div class="mlg-s">of runs merged without human rework</div>
          </div>
          <div class="mlg">
            <div class="mlg-l">Escape rate</div>
            <div class="mlg-v">${fmtPct(ap.escape)}</div>
            <div class="mlg-s">defects that reached staging or prod</div>
          </div>
          <div class="mlg">
            <div class="mlg-l">Catch rate</div>
            <div class="mlg-v">${fmtPct(ap.catch)}</div>
            <div class="mlg-s">issues caught by L3 review before merge</div>
          </div>
          <div class="mlg">
            <div class="mlg-l">Auto-merge</div>
            <div class="mlg-v">${fmtPct(ap.auto_merge)}</div>
            <div class="mlg-s">PRs merged without human approval</div>
          </div>
        </div>
      </section>

      <section class="vsection two-col">
        <div>
          <div class="sec-hd"><h3>FPA · 12-week trend</h3><span class="muted sm">${esc(ap.name)}</span></div>
          <div class="trend-card">
            <div class="trend-hd"><div class="trend-v">${fmtPct(ap.fpa)}</div><div class="muted sm">▲ 9pts vs 12w ago</div></div>
            <div class="trend-spark" style="color: var(--signal-ok)">${sparkline(spark1)}</div>
          </div>
        </div>
        <div>
          <div class="sec-hd"><h3>Escape rate · 12-week trend</h3><span class="muted sm">${esc(ap.name)}</span></div>
          <div class="trend-card">
            <div class="trend-hd"><div class="trend-v">${fmtPct(ap.escape)}</div><div class="muted sm">▼ 11pts vs 12w ago</div></div>
            <div class="trend-spark" style="color: var(--signal-err)">${sparkline(spark2)}</div>
          </div>
        </div>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h3>Auto-merge adoption · 12-week trend</h3></div>
        <div class="trend-card">
          <div class="trend-hd"><div class="trend-v">${fmtPct(ap.auto_merge)}</div><div class="muted sm">▲ 34pts — most of this from Python svc</div></div>
          <div class="trend-spark" style="color: var(--accent)">${sparkline(spark3)}</div>
        </div>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h3>By ticket type</h3><span class="muted sm">${esc(ap.name)}</span></div>
        <table class="tbl">
          <thead><tr><th>Type</th><th class="right">Volume</th><th class="right">FPA</th><th class="right">Escape</th><th class="right">Auto-merge</th></tr></thead>
          <tbody>
            ${VD.byType.map(r=>`
              <tr>
                <td><b>${esc(r.type)}</b></td>
                <td class="right mono">${r.volume}</td>
                <td class="right mono">${fmtPct(r.fpa)}</td>
                <td class="right mono ${r.escape>0.2?'warn-text':''}">${fmtPct(r.escape)}</td>
                <td class="right mono">${fmtPct(r.auto_merge)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h3>Escaped defects · last 30d</h3><a class="sec-link">Export CSV</a></div>
        <table class="tbl">
          <thead><tr><th>ID</th><th>From trace</th><th>Severity</th><th>Where</th><th>Caught in</th><th>Note</th></tr></thead>
          <tbody>
            ${VD.escaped.map(d=>`
              <tr>
                <td class="mono">${d.id}</td>
                <td class="mono"><a class="lnk" data-go-trace="${d.trace}">${d.trace}</a></td>
                <td>${sevPill(d.severity)}</td>
                <td class="mono sm">${esc(d.where)}</td>
                <td>${esc(d.caught_in)}</td>
                <td class="muted">${esc(d.note)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </section>
    `;
  }

  function viewLearning(){
    const filter = state.lessonFilter;
    const filtered = filter==='all' ? VD.lessons : VD.lessons.filter(l=>l.state===filter);
    const states = ['all','proposed','draft','approved','applied','snoozed','rejected'];
    const counts = Object.fromEntries(states.map(s=>[s, s==='all'?VD.lessons.length:VD.lessons.filter(l=>l.state===s).length]));

    return `
      <div class="vhead">
        <div>
          <div class="sup">Overview · learning</div>
          <div class="vt">Lessons</div>
          <div class="sub">The harness proposes lessons from every trace. You triage them. Approved lessons get applied to future runs; rejected ones close the loop on noise.</div>
        </div>
        <div class="stat-lg">
          <div class="stat-lg-n">${VD.lessons.filter(l=>l.state==='proposed').length}</div>
          <div class="stat-lg-l">Awaiting triage</div>
        </div>
      </div>

      <div class="filter-row">
        ${states.map(s=>`
          <span class="chip sm ${state.lessonFilter===s?'is-on':''}" data-lfilter="${s}">
            ${s[0].toUpperCase()+s.slice(1)} <span class="chip-n">${counts[s]}</span>
          </span>
        `).join('')}
      </div>

      <section class="vsection">
        <table class="tbl tbl-lg">
          <thead><tr><th>ID</th><th>Lesson</th><th>Profile</th><th>Source</th><th class="right">Evidence</th><th class="right">Conf.</th><th>State</th><th class="right">Actions</th></tr></thead>
          <tbody>
            ${filtered.length ? filtered.map(l => `
              <tr>
                <td class="mono">${l.id}</td>
                <td>
                  <div><b>${esc(l.title)}</b></div>
                  ${l.body ? `<div class="muted sm" style="margin-top:4px; max-width:560px">${esc(l.body)}</div>` : ''}
                </td>
                <td>${esc(l.profile)}</td>
                <td class="mono sm"><a class="lnk" data-go-trace="${l.source_trace}">${l.source_trace}</a></td>
                <td class="right mono">${l.evidence}</td>
                <td class="right mono">${fmtPct(l.confidence)}</td>
                <td>${lessonStatePill(l.state)}</td>
                <td class="right">${lessonActions(l.state)}</td>
              </tr>
            `).join('') : `<tr><td colspan="8" class="pad-lg muted center">No lessons in this state.</td></tr>`}
          </tbody>
        </table>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h3>Unmatched issues</h3><span class="muted sm">Signals with no lesson rule — candidates for a new lesson</span></div>
        <table class="tbl">
          <thead><tr><th>ID</th><th>Trace</th><th>Signal</th><th class="right">Matches</th><th>Note</th><th class="right">Action</th></tr></thead>
          <tbody>
            ${VD.unmatched.map(u=>`
              <tr>
                <td class="mono">${u.id}</td>
                <td class="mono"><a class="lnk" data-go-trace="${u.trace}">${u.trace}</a></td>
                <td class="mono sm">${esc(u.signal)}</td>
                <td class="right mono">${u.matches}</td>
                <td class="muted">${esc(u.note)}</td>
                <td class="right"><button class="btn sm">Draft lesson</button></td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </section>
    `;
  }

  function lessonStatePill(s){
    const m = { proposed:'cool', draft:'cool', approved:'ok', applied:'active', snoozed:'warn', rejected:'err' };
    return `<span class="pill ${m[s]||'cool'}"><span class="d"></span>${s}</span>`;
  }
  function lessonActions(s){
    if (s==='proposed') return `<div class="action-row" style="justify-content:flex-end"><button class="btn sm primary">Approve</button><button class="btn sm">Edit</button><button class="btn sm ghost">Reject</button></div>`;
    if (s==='draft')    return `<div class="action-row" style="justify-content:flex-end"><button class="btn sm primary">Publish</button><button class="btn sm">Edit</button></div>`;
    if (s==='approved') return `<div class="action-row" style="justify-content:flex-end"><button class="btn sm">Apply now</button></div>`;
    if (s==='applied')  return `<div class="action-row" style="justify-content:flex-end"><button class="btn sm ghost">Retire</button></div>`;
    if (s==='snoozed')  return `<div class="action-row" style="justify-content:flex-end"><button class="btn sm">Unsnooze</button></div>`;
    if (s==='rejected') return `<div class="action-row" style="justify-content:flex-end"><button class="btn sm ghost">Revisit</button></div>`;
    return '';
  }

  function viewTrace(id){
    const td = VD.traceDetail; // only one deep detail
    if (id !== td.id) return viewEmpty(`Trace ${esc(id)} — no deep record loaded.`);

    const now = td.phases.findIndex(p=>p.state==='active');
    const phaseMap = td.phases.map((p,i)=>`
      <div class="tl-row ${p.state==='active'?'tl-active':''} ${p.state==='pending'?'tl-pending':''}">
        <div class="mono tl-time">${p.state==='pending'?'—':`+${p.dur}`}</div>
        <div class="mono sm muted">#${i+1}</div>
        <div class="tl-dot">${stateDot(p.state)}</div>
        <div class="tl-name">${esc(p.name)}</div>
        <div class="tl-note muted sm">${esc(p.tool)} · ${p.events} events</div>
        <div class="tl-dur mono sm">${esc(p.state)}</div>
      </div>
    `).join('');

    return `
      <div class="vhead vhead-detail">
        <div>
          <div class="sup"><span class="lnk" data-goto="traces">← Traces</span> · ${esc(td.id)}</div>
          <div class="vt">${esc(td.title)}</div>
          <div class="detail-meta">
            ${statusPill(td.status)}
            <span class="chip sm">${esc(td.profile)}</span>
            <span class="chip sm">branch · ${esc(td.branch)}</span>
            <a class="chip sm lnk" data-go-pr="${td.pr}">${esc(td.pr)}</a>
            <span class="muted sm">started ${esc(td.started)} · ${esc(td.author)}</span>
          </div>
        </div>
        <div class="vh-right">
          <button class="btn">Open worktree</button>
          <button class="btn primary">Stream live →</button>
        </div>
      </div>

      <section class="vsection">
        <div class="sec-hd"><h3>Phase timeline</h3><span class="muted sm">active at phase ${now+1} of ${td.phases.length}</span></div>
        <div class="timeline">${phaseMap}</div>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h3>Session panels</h3><span class="muted sm">spawned agents in this run</span></div>
        <table class="tbl">
          <thead><tr><th>Role</th><th>Agent</th><th>State</th><th>Last action</th><th class="right">At</th></tr></thead>
          <tbody>
            ${td.sessions.map(s=>`
              <tr>
                <td><b>${esc(s.role)}</b></td>
                <td class="mono">${esc(s.agent)}</td>
                <td>${statusPill(s.state==='running'?'in-flight':s.state==='done'?'done':'queued', s.state)}</td>
                <td>${esc(s.last)}</td>
                <td class="right mono muted">${esc(s.at)}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h3>Raw events</h3><span class="muted sm">${td.events.length} events · streaming</span></div>
        <div class="rawlog">
          ${td.events.map(e=>`
            <div class="rl">
              <span class="mono muted">${e.t}</span>
              <span class="mono rl-e">${esc(e.ev)}</span>
              <span>${esc(e.msg)} <span class="rl-p sm">· ${esc(e.p)}</span></span>
            </div>
          `).join('')}
        </div>
      </section>
    `;
  }

  function viewPR(id){
    const pr = VD.prDetail;
    if (!id || !id.includes(pr.id.split('-')[1])) {
      if (id !== pr.id && id !== '#1184' && id !== 'PR #1184') return viewEmpty(`PR ${esc(id)} — no deep record loaded.`);
    }
    const amClass = pr.auto_merge.decision==='hold' ? 'warn' : pr.auto_merge.decision==='merge' ? 'ok' : 'err';

    return `
      <div class="vhead vhead-detail">
        <div>
          <div class="sup"><span class="lnk" data-goto="traces">← Traces</span> · <span class="lnk" data-go-trace="${pr.trace}">${pr.trace}</span> · PR #1184</div>
          <div class="vt">${esc(pr.title)}</div>
          <div class="detail-meta">
            ${statusPill('in-flight','review')}
            <span class="chip sm">${esc(pr.profile)}</span>
            <span class="chip sm">${esc(pr.branch)} → ${esc(pr.target)}</span>
            <span class="muted sm">${pr.commits} commits · ${pr.files} files · +${pr.additions} −${pr.deletions} · ${esc(pr.author)}</span>
          </div>
        </div>
        <div class="vh-right">
          <button class="btn">View diff</button>
          <button class="btn primary">Approve + merge</button>
        </div>
      </div>

      <section class="vsection">
        <div class="sec-hd"><h3>CI checks</h3></div>
        <table class="tbl">
          <thead><tr><th>Check</th><th>State</th><th>Duration</th><th>Note</th></tr></thead>
          <tbody>
            ${pr.checks.map(c=>{
              const s = c.state==='pass'?'done':c.state==='warn'?'stuck':c.state==='fail'?'stuck':'queued';
              return `<tr><td class="mono">${esc(c.name)}</td><td>${statusPill(s,c.state)}</td><td class="mono">${c.dur}</td><td class="muted">${esc(c.note||'')}</td></tr>`;
            }).join('')}
          </tbody>
        </table>
      </section>

      <section class="vsection two-col">
        <div>
          <div class="sec-hd"><h3>Issues raised by L3 review</h3></div>
          <table class="tbl">
            <thead><tr><th>ID</th><th>Severity</th><th>Where</th><th>Matched lesson</th></tr></thead>
            <tbody>
              ${pr.issues.map(i=>`
                <tr>
                  <td class="mono">${i.id}</td>
                  <td>${sevPill(i.severity)}</td>
                  <td class="mono sm">${esc(i.where)}<div class="muted sm" style="margin-top:4px">${esc(i.note)}</div></td>
                  <td class="mono">${i.matched?`<span class="lnk" data-goto="learning">${i.matched}</span>`:'—'}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
        <div>
          <div class="sec-hd"><h3>Lesson matches</h3></div>
          <table class="tbl">
            <thead><tr><th>Lesson</th><th class="right">Conf.</th><th>Applied</th></tr></thead>
            <tbody>
              ${pr.matches.map(m=>`
                <tr>
                  <td><div class="mono">${m.lesson}</div><div class="muted sm">${esc(m.name)}</div></td>
                  <td class="right mono">${fmtPct(m.confidence)}</td>
                  <td>${m.applied?'<span class="pill ok"><span class="d"></span>applied</span>':'<span class="pill cool"><span class="d"></span>skipped</span>'}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      </section>

      <section class="vsection">
        <div class="sec-hd"><h3>Auto-merge decision</h3></div>
        <div class="trend-card">
          <div class="trend-hd">
            <div class="mlg-v-pill">${statusPill(pr.auto_merge.decision==='hold'?'stuck':pr.auto_merge.decision==='merge'?'done':'stuck', pr.auto_merge.decision.toUpperCase())}</div>
            <div class="muted sm">confidence ${fmtPct(pr.auto_merge.confidence)}</div>
          </div>
          <div class="muted sm" style="margin-bottom:8px">Reasons:</div>
          <ul style="margin:0 0 0 18px; color: var(--ink-800); font-size:13px; line-height:1.6">
            ${pr.auto_merge.reasons.map(r=>`<li>${esc(r)}</li>`).join('')}
          </ul>
        </div>
      </section>
    `;
  }

  function viewEmpty(msg){
    return `<div class="vhead"><div><div class="sup">—</div><div class="vt">Nothing loaded</div></div></div>
      <div class="empty-card">${esc(msg)}</div>`;
  }

  // =============== ROUTER ===============

  function go(view, params){
    state.view = view;
    if (params) Object.assign(state, params);
    const isTickets = view === 'tickets';
    tLayout.style.display = isTickets ? '' : 'none';
    host.style.display = isTickets ? 'none' : '';

    // nav active
    document.querySelectorAll('.topnav .tn').forEach(b=>{
      const active = b.dataset.topnav === (view==='trace'||view==='pr' ? 'traces' : view);
      b.classList.toggle('is-active', active);
    });
    document.querySelectorAll('.side .nav-item[data-topnav]').forEach(b=>{
      b.classList.toggle('is-active', b.dataset.topnav === view);
    });

    if (isTickets) return; // tickets layout handled by dashboard.js

    host.scrollTop = 0;
    if (view === 'home') host.innerHTML = viewHome();
    else if (view === 'traces') host.innerHTML = viewTraces();
    else if (view === 'autonomy') host.innerHTML = viewAutonomy();
    else if (view === 'learning') host.innerHTML = viewLearning();
    else if (view === 'trace') host.innerHTML = viewTrace(state.traceId);
    else if (view === 'pr') host.innerHTML = viewPR(state.prId);
    else host.innerHTML = viewEmpty('Unknown view');
  }

  // =============== EVENTS ===============

  document.addEventListener('click', e => {
    const tn = e.target.closest('[data-topnav]');
    if (tn) {
      const v = tn.dataset.topnav;
      if (v === 'tickets') go('tickets');
      else go(v);
      return;
    }
    const goLink = e.target.closest('[data-goto]');
    if (goLink) {
      const v = goLink.dataset.goto;
      const lf = goLink.dataset.lessonFilter;
      if (lf) state.lessonFilter = lf;
      go(v);
      return;
    }
    const trace = e.target.closest('[data-go-trace]');
    if (trace) {
      state.traceId = trace.dataset.goTrace;
      go('trace');
      return;
    }
    const pr = e.target.closest('[data-go-pr]');
    if (pr) {
      state.prId = pr.dataset.goPr;
      go('pr');
      return;
    }
    const lf = e.target.closest('[data-lfilter]');
    if (lf) {
      state.lessonFilter = lf.dataset.lfilter;
      go('learning');
      return;
    }
    const sp = e.target.closest('[data-set-profile]');
    if (sp) {
      state.activeProfile = sp.dataset.setProfile;
      go('autonomy');
      return;
    }
    const prc = e.target.closest('[data-goto-profile]');
    if (prc) {
      state.activeProfile = prc.dataset.gotoProfile;
      go('autonomy');
      return;
    }
    const tf = e.target.closest('[data-tfilter]');
    if (tf) {
      const f = tf.dataset.tfilter;
      const rows = f==='all' ? VD.traces : VD.traces.filter(t=>t.status===f);
      document.getElementById('tr-body').innerHTML = renderTracesTable(rows, false);
      document.querySelectorAll('[data-tfilter]').forEach(c=>c.classList.toggle('is-on', c===tf));
      return;
    }
    const pf = e.target.closest('[data-pfilter]');
    if (pf) {
      const f = pf.dataset.pfilter;
      const rows = VD.traces.filter(t=>t.profile===f);
      document.getElementById('tr-body').innerHTML = renderTracesTable(rows, false);
      document.querySelectorAll('[data-pfilter]').forEach(c=>c.classList.toggle('is-on', c===pf));
      return;
    }
  });

  // start on home
  go('home');
})();
