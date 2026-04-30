// Agentic Harness — operator dashboard interactions

(function(){
  const data = window.HARNESS_DATA;
  let state = {
    filter: "in-flight",
    selected: "HARN-2043",
    expanded: null,
    density: "default", // default | cozy
    accent: "#ff7a1a",
    shimmer: true,
    sortKey: "created",
  };

  // ------ filtering ------
  function visibleTickets() {
    const f = state.filter;
    return data.tickets.filter(t => {
      if (f === "all") return true;
      if (f === "in-flight") return t.status === "active";
      if (f === "stuck") return t.status === "warn" || t.status === "err";
      if (f === "done") return t.status === "ok";
      if (f === "queued") return t.status === "cool";
      return true;
    }).filter(t => {
      const q = (document.getElementById("search")?.value || "").trim().toLowerCase();
      if (!q) return true;
      return (t.id + " " + t.title + " " + t.profile + " " + t.author).toLowerCase().includes(q);
    });
  }

  // ------ rendering ------
  function statusPill(t) {
    return `<span class="pill ${t.status}"><span class="d"></span>${t.statusLabel}</span>`;
  }

  function phaseDots(t) {
    // 5 phases. failure replaces current phase with fail.
    const dots = [];
    for (let i = 0; i < 5; i++) {
      let cls = "pd";
      if (t.status === "err" && i === t.phase) cls += " fail";
      else if (i < t.phase) cls += " done";
      else if (i === t.phase && t.status === "active") cls += " active";
      else if (i === t.phase && t.status === "ok") cls += " done";
      dots.push(`<span class="${cls}"></span>`);
    }
    return `<div class="phase-dots" data-shimmer="${state.shimmer ? 'on' : 'off'}">${dots.join("")}</div>`;
  }

  function renderCounts() {
    const counts = {
      all: data.tickets.length,
      "in-flight": data.tickets.filter(t => t.status === "active").length,
      stuck: data.tickets.filter(t => t.status === "warn" || t.status === "err").length,
      queued: data.tickets.filter(t => t.status === "cool").length,
      done: data.tickets.filter(t => t.status === "ok").length,
    };
    document.querySelectorAll("[data-filter]").forEach(el => {
      const k = el.dataset.filter;
      const ctEl = el.querySelector(".ct");
      if (ctEl) ctEl.textContent = counts[k] ?? 0;
      el.classList.toggle("is-active", state.filter === k);
    });
    // sidebar also
    document.querySelectorAll("[data-nav-filter]").forEach(el => {
      const k = el.dataset.navFilter;
      const ctEl = el.querySelector(".ct");
      if (ctEl) ctEl.textContent = counts[k] ?? 0;
      el.classList.toggle("is-active", state.filter === k);
    });
  }

  function renderList() {
    const list = visibleTickets();
    const host = document.getElementById("rows");
    host.innerHTML = list.map(t => {
      const rowHtml = `
        <div class="row ${state.selected === t.id ? 'is-selected' : ''}" data-id="${t.id}" data-act="select">
          <div class="id">${t.id}</div>
          <div class="title"><span class="kind">${t.kind}</span>${t.title}</div>
          <div>${statusPill(t)}</div>
          <div>${phaseDots(t)}</div>
          <div class="elapsed">${t.elapsed}</div>
          <div class="author">${t.author} · ${t.created}</div>
          <div class="chev" data-act="expand" data-id="${t.id}">${state.expanded === t.id ? '▾' : '▸'}</div>
        </div>
      `;
      const detailHtml = state.expanded === t.id ? renderDetail(t) : '';
      return rowHtml + detailHtml;
    }).join("") || `<div style="padding:40px 24px; text-align:center; color: var(--ink-600); font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;">No tickets match current filter</div>`;
  }

  function renderDetail(t) {
    const phaseHtml = data.phases.map((p, i) => {
      let cls = "ph";
      if (t.status === "err" && i === t.phase) cls += " fail";
      else if (i < t.phase) cls += " done";
      else if (i === t.phase && t.status === "active") cls += " active";
      else if (i === t.phase && t.status === "ok") cls += " done";
      let stateLabel = "Queued";
      if (cls.includes("done")) stateLabel = "Complete";
      else if (cls.includes("active")) stateLabel = "In-flight";
      else if (cls.includes("fail")) stateLabel = "Failed";
      return `
        <div class="${cls}">
          <div class="l">${p.layer}</div>
          <div class="n">${p.name}</div>
          <div class="bar"></div>
          <div class="s">${stateLabel}</div>
        </div>
      `;
    }).join("");

    return `
      <div class="row-detail">
        <div class="detail-grid">
          <div><div class="dk">Profile</div><div class="dv">${t.profile}</div></div>
          <div><div class="dk">Size</div><div class="dv">${t.size}</div></div>
          <div><div class="dk">Elapsed / budget</div><div class="dv">${t.elapsed} / ${t.budget}</div></div>
          <div><div class="dk">Branch</div><div class="dv">${t.branch}</div></div>
          <div><div class="dk">Units</div><div class="dv">${t.unitsDone} of ${t.unitsTotal}</div></div>
          <div><div class="dk">Reviewer</div><div class="dv">${t.reviewer}</div></div>
          <div><div class="dk">Author</div><div class="dv">${t.author}</div></div>
          <div><div class="dk">Created</div><div class="dv">${t.created}</div></div>
        </div>
        <div class="phase-map">${phaseHtml}</div>
        <div class="detail-actions">
          <button class="btn primary">Open run</button>
          <button class="btn">View PR</button>
          <button class="btn">Logs</button>
          <button class="btn danger">${t.status === 'err' ? 'Retry' : 'Abort'}</button>
        </div>
      </div>
    `;
  }

  function renderRail() {
    const t = data.tickets.find(x => x.id === state.selected) || data.tickets[0];
    document.getElementById("rail-id").textContent = t.id;
    document.getElementById("rail-title").textContent = t.title;
    document.getElementById("rail-meta").innerHTML = `
      ${statusPill(t)}
      <span class="pill"><span class="d"></span>${t.profile}</span>
      <span class="pill"><span class="d"></span>${t.size}</span>
    `;
    // agents
    document.getElementById("rail-agents").innerHTML = data.agents.map(a =>
      `<div class="ag ${a.state}"><span class="d"></span><span class="h">${a.handle}</span><span class="r">${a.role}</span></div>`
    ).join("");

    // logs
    const lines = data.logs[t.id] || [];
    document.getElementById("log").innerHTML = lines.map(([ts, lv, msg]) =>
      `<div class="ln"><span class="t">${ts}</span><span class="lv ${lv}">${lv}</span><span class="msg">${msg}</span></div>`
    ).join("") + `<div class="ln divider"></div>`;
    const log = document.getElementById("log");
    log.scrollTop = log.scrollHeight;
  }

  // ------ live log ticker ------
  const tickerLines = [
    ["info", "unit-02 running pytest tests/test_figma_retry.py -v"],
    ["pass", "unit-02 → 6 tests green · committed <em>9e41bd</em>"],
    ["info", "merge-coordinator → merging ai/HARN-2043/unit-02"],
    ["info", "reviewer → scanning diff · 3 files"],
    ["info", "judge → reading src/figma/client.py with context"],
    ["pass", "judge → 2/3 findings upheld · 1 rejected (pre-existing)"],
    ["info", "qa → launching playwright · headless chromium"],
  ];
  let tickerIdx = 0;
  function pushLog() {
    if (state.selected !== "HARN-2043") return; // only stream for the active ticket
    if (tickerIdx >= tickerLines.length) return;
    const [lv, msg] = tickerLines[tickerIdx++];
    const now = new Date();
    const ts = `02:${String(23 + tickerIdx).padStart(2, '0')}.${String(Math.floor(Math.random() * 900 + 100))}`;
    const log = document.getElementById("log");
    if (!log) return;
    const div = document.createElement("div");
    div.className = "ln new";
    div.innerHTML = `<span class="t">${ts}</span><span class="lv ${lv}">${lv}</span><span class="msg">${msg}</span>`;
    // insert before the divider
    const divider = log.querySelector(".divider");
    if (divider) log.insertBefore(div, divider);
    else log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }
  setInterval(pushLog, 4200);

  // ------ events ------
  document.addEventListener("click", e => {
    const expandBtn = e.target.closest("[data-act='expand']");
    if (expandBtn) {
      e.stopPropagation();
      const id = expandBtn.dataset.id;
      state.expanded = state.expanded === id ? null : id;
      state.selected = id;
      renderList(); renderRail();
      return;
    }
    const row = e.target.closest("[data-act='select']");
    if (row) {
      state.selected = row.dataset.id;
      renderList(); renderRail();
      return;
    }
    const tab = e.target.closest("[data-filter]");
    if (tab) {
      state.filter = tab.dataset.filter;
      renderCounts(); renderList();
      return;
    }
    const nav = e.target.closest("[data-nav-filter]");
    if (nav) {
      state.filter = nav.dataset.navFilter;
      renderCounts(); renderList();
      return;
    }
  });

  document.getElementById("search")?.addEventListener("input", () => renderList());

  // ------ Tweaks integration ------
  const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
    "accent": "#ff7a1a",
    "density": "default",
    "shimmer": true,
    "showLog": true
  }/*EDITMODE-END*/;

  function applyTweaks(t) {
    document.documentElement.style.setProperty("--accent", t.accent);
    document.documentElement.style.setProperty("--signal-active", t.accent);
    document.documentElement.dataset.density = t.density;
    state.accent = t.accent;
    state.shimmer = !!t.shimmer;
    document.getElementById("rail")?.classList.toggle("is-hidden", !t.showLog);
    document.querySelector(".content").style.gridTemplateColumns = t.showLog ? "1fr 420px" : "1fr";
    // rerender so phase dot shimmer state updates
    renderList();
  }
  applyTweaks(TWEAK_DEFAULTS);

  // panel UI
  function buildTweakPanel(current) {
    const panel = document.getElementById("tweaks");
    panel.innerHTML = `
      <div class="hd">Tweaks <button class="x" id="tw-close">×</button></div>
      <div class="body">
        <div class="tw-row">
          <label>Accent hue</label>
          <div class="tw-opts" data-tw="accent">
            ${["#ff7a1a","#d26a5a","#9db48a","#6c8fb0","#e8c46a"].map(c =>
              `<button data-v="${c}" class="${current.accent===c?'on':''}"><span class="tw-swatch" style="background:${c}"></span>${c.slice(1).toUpperCase()}</button>`
            ).join("")}
          </div>
        </div>
        <div class="tw-row">
          <label>Density</label>
          <div class="tw-opts" data-tw="density">
            ${["default","cozy"].map(v =>
              `<button data-v="${v}" class="${current.density===v?'on':''}">${v}</button>`
            ).join("")}
          </div>
        </div>
        <div class="tw-row">
          <label>Phase shimmer</label>
          <div class="tw-opts" data-tw="shimmer">
            <button data-v="true" class="${current.shimmer?'on':''}">on</button>
            <button data-v="false" class="${!current.shimmer?'on':''}">off</button>
          </div>
        </div>
        <div class="tw-row">
          <label>Live log rail</label>
          <div class="tw-opts" data-tw="showLog">
            <button data-v="true" class="${current.showLog?'on':''}">show</button>
            <button data-v="false" class="${!current.showLog?'on':''}">hide</button>
          </div>
        </div>
      </div>
    `;
    panel.querySelector("#tw-close").onclick = () => {
      panel.classList.remove("on");
      window.parent.postMessage({ type: '__deactivate_edit_mode_ack' }, '*');
    };
    panel.querySelectorAll(".tw-opts").forEach(grp => {
      grp.addEventListener("click", e => {
        const btn = e.target.closest("button[data-v]");
        if (!btn) return;
        const key = grp.dataset.tw;
        let val = btn.dataset.v;
        if (val === "true") val = true;
        else if (val === "false") val = false;
        Object.assign(TWEAK_DEFAULTS, {[key]: val});
        applyTweaks(TWEAK_DEFAULTS);
        buildTweakPanel(TWEAK_DEFAULTS);
        window.parent.postMessage({ type: '__edit_mode_set_keys', edits: {[key]: val}}, '*');
      });
    });
  }

  // Listener FIRST, then announce
  window.addEventListener("message", e => {
    if (!e.data) return;
    if (e.data.type === "__activate_edit_mode") {
      buildTweakPanel(TWEAK_DEFAULTS);
      document.getElementById("tweaks").classList.add("on");
    } else if (e.data.type === "__deactivate_edit_mode") {
      document.getElementById("tweaks").classList.remove("on");
    }
  });
  window.parent.postMessage({ type: "__edit_mode_available" }, "*");

  // ------ KPI sparkline (tiny inline SVG) ------
  function sparkline(el, points, color) {
    const max = Math.max(...points), min = Math.min(...points);
    const w = 140, h = 24;
    const pts = points.map((v, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((v - min) / (max - min || 1)) * h;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    el.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:100%">
        <polyline fill="none" stroke="${color}" stroke-width="1.2" points="${pts}" />
        <polyline fill="${color}" fill-opacity="0.08" stroke="none" points="0,${h} ${pts} ${w},${h}" />
      </svg>
    `;
  }
  function renderKPIs() {
    const host = document.getElementById("kpis");
    host.innerHTML = data.kpis.map((k, i) =>
      `<div class="kpi">
        <div class="label">${k.label}</div>
        <div class="val">${k.val}${k.suffix ? `<em>${k.suffix}</em>` : ''}</div>
        <div class="sub">${k.sub}</div>
        <div class="spark" data-idx="${i}"></div>
      </div>`
    ).join("");
    const points = [
      [3,4,2,5,6,4,7,8,7,9,11,13,17],
      [18,16,14,15,13,12,11,11,10,12,11,11],
      [62,64,67,65,68,70,66,68,71,69,68,68],
      [1,0,2,3,3,2,2,3,2,2,2,2],
    ];
    host.querySelectorAll(".spark").forEach(s => {
      sparkline(s, points[s.dataset.idx], "currentColor");
    });
  }

  // init
  renderCounts();
  renderKPIs();
  renderList();
  renderRail();

  // ------ Theme toggle (after init so button exists) ------
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const lbl = document.getElementById("theme-label");
    if (lbl) lbl.textContent = theme === "light" ? "Light" : "Dark";
    try { localStorage.setItem("harness:theme", theme); } catch(e) {}
  }
  window.__toggleTheme = function() {
    const cur = document.documentElement.getAttribute("data-theme");
    setTheme(cur === "light" ? "dark" : "light");
  };
  let savedTheme = null;
  try { savedTheme = localStorage.getItem("harness:theme"); } catch(e) {}
  setTheme(savedTheme === "light" ? "light" : "dark");
})();
