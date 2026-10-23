// Fetcher and live-feed activity. One poll of /api/activity feeds the Fetcher view (historical
// data: sessions before the current one, with the Scan button), the Live view (current-session
// feeds and whether they are being recorded), the rail icons and the status bar.
//
// Opening a symbol checks it at once: if its plan should have published a session it does not
// have yet, that symbol alone is fetched in the background, and the status bar at the bottom
// shows the check and the download (like a browser's), then the chart picks up the new bars.
(() => {
  'use strict';

  const { dataIcon, symbolIcon, DATA_LABEL } = window.MassiveIcons;
  const POLL_FAST_MS = 1500;   // while a run is going or the Fetcher / Live view is open
  const POLL_SLOW_MS = 10000;
  const STATE_ORDER = { fetching: 0, new: 1, backfilling: 2, queued: 3, current: 4 };
  const UPDATE_REQUESTS = 4;  // a one-symbol update: newest-bar check, new bars, daily bars, listing
  const el = (id) => document.getElementById(id);
  const esc = window.MassiveIcons.esc;
  const num = (n) => (n == null ? '–' : Number(n).toLocaleString());

  let data = null;
  let filter = 'all';
  let timer = null;
  let followLog = true;
  let wasRunning = false;
  let scanNote = '';
  // The status bar's job: { ticker, phase: 'checking' | 'waiting' | 'downloading' | 'done' | 'current' | 'error', ... }
  let job = null;

  function dur(s) {
    if (s == null) return '–';
    s = Math.round(s);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  }
  function ago(iso) {
    if (!iso) return 'never';
    const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    return s < 0 ? `in ${dur(-s)}` : `${dur(s)} ago`;
  }
  const when = (iso) => (iso ? new Date(iso).toLocaleString([], { timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '–');
  const day = (iso) => (iso ? iso.slice(0, 10) : '–');
  const viewOpen = (name) => document.body.dataset.view === name;
  function meter(fraction, cls = '') {
    const pct = Math.max(0, Math.min(100, (fraction || 0) * 100));
    return `<span class="meter ${cls}"><span style="width:${pct.toFixed(1)}%"></span></span>`;
  }

  async function poll() {
    clearTimeout(timer);
    try {
      const res = await fetch('/api/activity?lines=300');
      if (!res.ok) throw new Error(String(res.status));
      data = await res.json();
      window.dispatchEvent(new CustomEvent('massive:activity', { detail: data }));
      if (wasRunning && !data.running) {
        window.dispatchEvent(new CustomEvent('massive:run-finished', { detail: data }));
        finishJob();
      }
      wasRunning = data.running;
      advanceJob();
      render();
    } catch {
      data = null;
      render();
    }
    const fast = (data && data.running) || job?.phase === 'waiting' || viewOpen('fetcher') || viewOpen('live');
    timer = setTimeout(poll, fast ? POLL_FAST_MS : POLL_SLOW_MS);
  }

  function render() {
    renderStatusBar();
    if (viewOpen('fetcher')) renderFetcherView();
    if (viewOpen('live')) renderLiveView();
  }

  function statusLine() {
    if (!data) return '<span class="dot"></span><b>Unavailable</b>';
    const last = data.runs.find((r) => r.status !== 'running');
    if (data.running) {
      const live = data.live;
      return `<span class="dot on"></span><b>Running ${esc(live.command)}</b>
        — ${live.current ? `fetching <b>${esc(live.current)}</b>` : 'starting'} · ${num(live.requests)} of ${num(data.plan.maxPerRun)} requests · ${dur(live.seconds)}`;
    }
    return `<span class="dot"></span><b>Idle</b>${last ? ` — last run ${ago(last.finished_at || last.started_at)}
      (${esc(last.status)}, ${num(last.requests)} requests, ${num(last.rows)} rows)` : ''}`;
  }

  // -- automatic check when a symbol is opened, and the status bar ----------------------
  async function checkSymbol(ticker) {
    if (!ticker) return;
    if (!data) await poll();
    const s = data?.symbols?.find((x) => x.ticker === ticker);
    if (!s) { job = null; renderStatusBar(); return; }
    if (s.state === 'new') {
      job = { ticker, phase: 'checking', since: Date.now() };
      renderStatusBar();
      await startFetch(ticker);
    } else if (s.state === 'current') {
      job = { ticker, phase: 'current', nextDue: s.nextDue, last: s.last, since: Date.now() };
    } else {
      job = { ticker, phase: s.state, progress: s.progress, since: Date.now() };  // backfilling / queued / fetching
    }
    renderStatusBar();
  }

  async function startFetch(ticker) {
    try {
      const res = await fetch(`/api/fetch?ticker=${encodeURIComponent(ticker)}`, { method: 'POST', headers: { 'X-Massive-Charts': '1' } });
      if (res.status === 409) { job = { ticker, phase: 'waiting', since: Date.now() }; return; }  // fetcher busy: retry after its run
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
      job = { ticker, phase: 'downloading', since: Date.now(), runId: null };
      setTimeout(poll, 400);
    } catch (e) {
      job = { ticker, phase: 'error', message: String(e.message || e), since: Date.now() };
    }
  }

  function advanceJob() {
    if (!job || !data) return;
    if (job.phase === 'downloading' && data.running && data.live.command.startsWith('scan') && !job.runId) job.runId = data.live.id;
    if (job.phase === 'waiting' && !data.running) startFetch(job.ticker);
  }

  function finishJob() {
    if (!job || job.phase !== 'downloading') return;
    const run = data.runs.find((r) => r.id === job.runId) || data.runs.find((r) => String(r.command).startsWith('scan'));
    const s = data.symbols.find((x) => x.ticker === job.ticker);
    job = run && run.status === 'ok'
      ? { ticker: job.ticker, phase: 'done', rows: run.rows, requests: run.requests, last: s?.last, since: Date.now() }
      : { ticker: job.ticker, phase: 'error', message: run ? `${run.status}: ${run.message || ''}` : 'run ended', since: Date.now() };
  }

  function renderStatusBar() {
    const text = el('netText'), bar = el('netBar');
    let msg = '', progress = null, busy = false, cls = '';
    const running = data?.running;
    if (job && job.ticker === state.ticker) {
      const t = esc(job.ticker);
      if (job.phase === 'checking') { msg = `Checking ${t} for new data…`; busy = true; }
      else if (job.phase === 'waiting') { msg = `${t}: new data available — waiting for the fetcher (${esc(data?.live?.current || 'busy')})…`; busy = true; }
      else if (job.phase === 'downloading') {
        const req = running ? data.live.requests : 0;
        msg = running ? `Downloading ${t} — ${num(req)} of ~${UPDATE_REQUESTS} requests` : `Starting download of ${t}…`;
        progress = running ? Math.min(0.95, req / UPDATE_REQUESTS) : 0.05;
      } else if (job.phase === 'done') { msg = `${t}: done — ${num(job.rows)} new rows, ${num(job.requests)} requests${job.last ? ` · data through ${when(job.last)}` : ''}`; cls = 'ok'; }
      else if (job.phase === 'current') { msg = `${t} is up to date${job.last ? ` (through ${when(job.last)})` : ''} · next new data ${when(job.nextDue)}`; }
      else if (job.phase === 'backfilling') { msg = `${t}: history is being downloaded by the fetcher (${Math.round((job.progress || 0) * 100)}%)`; progress = job.progress || 0; }
      else if (job.phase === 'queued') { msg = `${t}: queued for download by the fetcher`; }
      else if (job.phase === 'fetching') { msg = `${t}: being fetched now`; busy = true; }
      else if (job.phase === 'error') { msg = `${t}: could not fetch — ${esc(job.message)}`; cls = 'bad'; }
    } else if (running) {
      msg = `Fetcher: ${esc(data.live.command)}${data.live.current ? ` — ${esc(data.live.current)}` : ''} · ${num(data.live.requests)} requests`;
      busy = true;
    }
    text.innerHTML = msg ? `${busy ? '<span class="net-spin"></span>' : ''}${msg}` : '';
    text.className = cls;
    bar.hidden = progress == null && !busy;
    bar.classList.toggle('indeterminate', progress == null && busy);
    bar.firstElementChild.style.width = progress == null ? '' : `${Math.round(progress * 100)}%`;
  }

  window.addEventListener('massive:symbol', (e) => checkSymbol(e.detail));

  // -- Fetcher view --------------------------------------------------------------------
  function logHtml(lines) {
    return lines.map((line) => {
      const cls = /error|refused|stopped|cap .*reached|not available|could not/i.test(line) ? 'bad'
        : /: source=|requests used|^.{20}scan:/.test(line) ? 'head' : /: [1-9][\d,]* rows|new data|icon saved/.test(line) ? 'good' : '';
      return `<div class="${cls}">${esc(line)}</div>`;
    }).join('');
  }

  function renderFetcherView() {
    if (!data) { el('actStatus').innerHTML = statusLine(); return; }
    const { plan, today, backfill } = data;
    el('actStatus').innerHTML = statusLine();
    el('scanBtn').disabled = !!data.running;
    el('scanNote').innerHTML = scanNote ? esc(scanNote) : '';
    const runReq = data.running ? data.live.requests : 0;
    el('actTiles').innerHTML = `
      <div class="tile"><h5>Requests today</h5><div class="big">${num(today.requests)} <small>/ ${num(plan.maxPerDay)}</small></div>
        ${meter(today.requests / plan.maxPerDay, today.requests / plan.maxPerDay > 0.9 ? 'warn' : '')}
        <small>${num(today.remaining)} left · resets at 00:00 UTC</small></div>
      <div class="tile"><h5>This run</h5><div class="big">${num(runReq)} <small>/ ${num(plan.maxPerRun)}</small></div>
        ${meter(runReq / plan.maxPerRun)}<small>${plan.requestsPerMinute ? `${plan.requestsPerMinute} requests / minute` : 'no rate limit'}</small></div>
      <div class="tile"><h5>Backfill</h5><div class="big">${num(backfill.done)} <small>/ ${num(backfill.total)} symbols</small></div>
        ${meter(backfill.total ? backfill.done / backfill.total : 1, 'good')}
        <small>${backfill.requestsLeft ? `≈${num(backfill.requestsLeft)} requests left · ~${backfill.runs} run${backfill.runs === 1 ? '' : 's'}` : 'complete'}</small></div>
      <div class="tile"><h5>New data</h5><div class="big">${num(data.newData)} <small>symbols</small></div>
        <small>should be available now (plan calendar)</small></div>
      <div class="tile"><h5>Plan</h5><div class="big">${esc(plan.tier)}</div>
        <small>history from ${esc(plan.historyFloor)}</small></div>`;

    const counts = { all: data.symbols.length };
    for (const s of data.symbols) counts[s.state] = (counts[s.state] || 0) + 1;
    el('actFilters').innerHTML = ['all', 'fetching', 'new', 'backfilling', 'queued', 'current']
      .filter((k) => k === 'all' || counts[k])
      .map((k) => `<button data-filter="${k}" class="${k === filter ? 'active' : ''}">${k === 'all' ? 'All' : DATA_LABEL[k]} <span class="chip">${counts[k] || 0}</span></button>`)
      .join('');
    const rows = data.symbols
      .filter((s) => filter === 'all' || s.state === filter)
      .sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.ticker.localeCompare(b.ticker));
    el('actSymbols').innerHTML = rows.length ? rows.map((s) => `
      <tr data-ticker="${esc(s.ticker)}" class="${s.first ? 'has-data' : ''}" title="${s.first ? 'Open in the chart' : 'No data stored yet'}">
        <td><span class="sym-cell">${symbolIcon(s, 20)}<span><b>${esc(s.exchange ? `${s.ticker}::${s.exchange}` : s.ticker)}</b>
          <div class="sub">${esc(s.name || s.asset)}</div></span></span></td>
        <td>${dataIcon(s, 14)} <span class="state ${s.state}">${esc(DATA_LABEL[s.state])}</span></td>
        <td class="prog">${meter(s.progress, s.progress >= 1 ? 'good' : '')}
          <div class="sub">${s.first ? `${day(s.first)} → ${day(s.last)}` : 'nothing stored yet'}${s.progress < 1 ? ` · target ${day(s.target)}` : ''}</div></td>
        <td>${esc(s.resolution)}</td>
        <td class="num">${s.requestsLeft ? `≈${num(s.requestsLeft)}` : '–'}</td>
        <td>${s.syncedAt ? ago(s.syncedAt) : '–'}<div class="sub">${s.nextDue ? `new data ${when(s.nextDue)}` : ''}</div></td>
      </tr>`).join('') : '<tr><td colspan="6" class="empty">No symbols in this state.</td></tr>';
    el('actRuns').innerHTML = data.runs.slice(0, 12).map((r) => `
      <tr><td>${when(new Date(r.started_at).toISOString())}</td><td>${esc(r.command)}</td>
        <td><span class="state run-${esc(r.status)}">${esc(r.status)}</span></td>
        <td class="num">${num(r.requests)}</td><td class="num">${num(r.rows)}</td><td class="num">${dur(r.seconds)}</td>
        <td class="sub">${esc(r.message || '')}</td></tr>`).join('');
    const log = el('actLog');
    log.innerHTML = logHtml(data.log);
    if (followLog) log.scrollTop = log.scrollHeight;
  }

  async function startScan() {
    scanNote = '';
    try {
      const res = await fetch('/api/scan', { method: 'POST', headers: { 'X-Massive-Charts': '1' } });
      if (!res.ok) scanNote = (await res.json().catch(() => ({}))).error || res.statusText;
      else scanNote = 'Scan started: checking every watched symbol.';
    } catch (e) {
      scanNote = String(e.message || e);
    }
    setTimeout(poll, 500);
  }

  // -- Live view -----------------------------------------------------------------------
  function renderLiveView() {
    const box = el('viewLive');
    if (!data) { box.innerHTML = '<div class="view-head"><h2>Live feeds</h2></div><div class="p-muted">Unavailable.</div>'; return; }
    const live = data.liveFeeds;
    const badge = (f) => f.realtime ? '<span class="state fresh">real time</span>'
      : f.available ? `<span class="state backfilling">${esc(f.delay)}</span>` : '<span class="state">end of day only</span>';
    const names = { stock: 'Stocks', index: 'Indices', fx: 'Forex', future: 'Futures' };
    box.innerHTML = `
      <div class="view-head"><div><h2>Live feeds</h2>
        <div class="p-muted small">The current session, as it happens. The fetcher handles everything before it.</div></div></div>
      <div class="act-status"><span class="rec ${live.recording ? 'on' : ''}"></span>
        <b>${live.recording ? 'Recording' : 'Not recording'}</b>
        <span class="p-muted">${live.recording ? 'current-session data is captured as it arrives'
          : live.available ? 'delayed intraday data is available on your plan; nothing records it live yet, so it arrives with the fetcher\'s runs'
            : 'your plan has no current-session feed'}</span></div>
      <div class="table-wrap"><table class="act-table">
        <thead><tr><th>Feed</th><th>Symbols</th><th>Latency on your plan</th><th>Recording</th></tr></thead>
        <tbody>${live.feeds.map((f) => `
          <tr><td><b>${esc(names[f.asset] || f.asset)}</b>${f.note ? `<div class="sub">${esc(f.note)}</div>` : ''}</td>
            <td><span class="sym-cell">${f.symbols.slice(0, 12).map((t) => symbolIcon({ ticker: t, asset: f.asset, icon: symbolInfo(t).icon }, 18)).join('')}</span>
              <div class="sub">${esc(f.symbols.join(', '))}</div></td>
            <td>${badge(f)}</td>
            <td>${f.recording ? '<span class="rec on"></span> recording' : '<span class="p-muted">idle</span>'}</td></tr>`).join('')}
        </tbody></table></div>`;
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('#scanBtn')) { startScan(); return; }
    const item = e.target.closest('#actSymbols tr.has-data');
    if (item) window.dispatchEvent(new CustomEvent('massive:open-symbol', { detail: item.dataset.ticker }));
  });
  el('actFilters').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-filter]');
    if (!b) return;
    filter = b.dataset.filter;
    renderFetcherView();
  });
  el('actLog').addEventListener('scroll', (e) => {
    const l = e.target;
    followLog = l.scrollHeight - l.scrollTop - l.clientHeight < 24;
  });
  window.addEventListener('massive:view', () => { render(); poll(); });
  window.Activity = { poll, get data() { return data; } };
  poll();
})();
