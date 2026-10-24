// Fetcher and live-feed activity. One poll of /api/activity feeds the Fetcher view (historical
// data: sessions before the current one, with the Scan button), the Live view (current-session
// feeds and whether they are being recorded), the rail icons and the status bar.
//
// Opening a symbol asks every feed holding it to update it first: with no run going, a run for
// that symbol alone starts (gaps and new data on each feed, within each feed's limits); with one
// going, the running sync takes it next. The status bar at the bottom shows where that stands
// (like a browser's), then the chart picks up the new bars.
(() => {
  'use strict';

  const { dataIcon, symbolIcon, DATA_LABEL } = window.MassiveIcons;
  const POLL_FAST_MS = 1500;   // while a run is going or the Fetcher / Live view is open
  const POLL_SLOW_MS = 10000;
  const STATE_ORDER = { fetching: 0, new: 1, backfilling: 2, queued: 3, current: 4, retired: 5, closed: 6, blocked: 7 };
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
  let openTicker = null;  // the symbol open on the chart (sent with each poll for the live recorder)

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
  // Short form for the status bar: Fri 04:00, or 10-21 19:59 when not this week.
  const short = (iso) => {
    if (!iso) return '–';
    const t = new Date(iso), days = Math.abs(t - Date.now()) / 864e5;
    const o = { timeZone: 'America/New_York', hour: '2-digit', minute: '2-digit', hour12: false };
    return days < 6 ? t.toLocaleString('en-US', { ...o, weekday: 'short' }) : t.toLocaleString('en-US', { ...o, month: '2-digit', day: '2-digit' });
  };
  const day = (iso) => (iso ? iso.slice(0, 10) : '–');
  const viewOpen = (name) => document.body.dataset.view === name;
  function meter(fraction, cls = '') {
    const pct = Math.max(0, Math.min(100, (fraction || 0) * 100));
    return `<span class="meter ${cls}"><span style="width:${pct.toFixed(1)}%"></span></span>`;
  }

  // The Fetcher view shows one feed at a time; the choice is remembered on this machine.
  let feed = 'massive';
  try { feed = localStorage.getItem('fetcherFeed') || 'massive'; } catch { /* private window */ }

  async function poll() {
    clearTimeout(timer);
    try {
      // `open`: the chart's symbol, so the live recorder streams it (stocks, from IEX).
      const open = openTicker ? `&open=${encodeURIComponent(openTicker)}` : '';
      const res = await fetch(`/api/activity?lines=300&source=${encodeURIComponent(feed)}${open}`);
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
      const cap = live.source === data.source ? ` of ${num(data.plan.maxPerRun)}` : '';
      return `<span class="dot on"></span><b>Running ${esc(live.command)}</b> <span class="p-muted">(${esc(live.source)})</span>
        — ${live.current ? `fetching <b>${esc(live.current)}</b>` : 'starting'} · ${num(live.requests)}${cap} requests · ${dur(live.seconds)}`;
    }
    return `<span class="dot"></span><b>Idle</b>${last ? ` — last run ${ago(last.finished_at || last.started_at)}
      (${esc(last.status)}, ${num(last.requests)} requests, ${num(last.rows)} rows)` : ''}`;
  }

  // -- automatic check when a symbol is opened, and the status bar ----------------------
  async function checkSymbol(ticker) {
    if (!ticker) return;
    if (!data) await poll();
    job = { ticker, phase: 'checking', since: Date.now() };
    renderStatusBar();
    await askFirst(ticker);
    renderStatusBar();
  }

  // Where the symbol stands on the Fetcher's feed (when nothing is being fetched for it).
  function standing(ticker) {
    const s = data?.symbols?.find((x) => x.ticker === ticker);
    if (!s) return null;
    if (s.state === 'current') return { ticker, phase: 'current', nextDue: s.nextDue, last: s.last, since: Date.now() };
    return { ticker, phase: s.state, progress: s.progress, since: Date.now() };  // backfilling / queued / ...
  }

  async function askFirst(ticker) {
    try {
      const res = await fetch(`/api/priority?ticker=${encodeURIComponent(ticker)}`, { method: 'POST', headers: { 'X-Massive-Charts': '1' } });
      const body = await res.json().catch(() => ({}));
      if (res.status === 404) { job = null; return; }  // not on the watchlist: nothing to fetch
      if (!res.ok) throw new Error(body.error || res.statusText);
      const feeds = body.feeds || [];
      if (body.state === 'started') {
        job = { ticker, phase: 'downloading', feeds, since: Date.now() };
        setTimeout(poll, 400);
      } else if (body.state === 'queued') job = { ticker, phase: 'waiting', feeds, since: Date.now() };
      else job = standing(ticker);  // a run for it went in the last few minutes
    } catch (e) {
      job = { ticker, phase: 'error', message: String(e.message || e), since: Date.now() };
    }
  }

  function advanceJob() {
    if (!job || !data) return;
    if (job.phase === 'waiting' && !data.running) askFirst(job.ticker).then(renderStatusBar);  // the run ended: its turn
  }

  function finishJob() {
    if (!job || job.phase !== 'downloading') return;
    // The symbol's run went over every feed holding it: one run record per feed.
    const runs = data.runs.filter((r) => String(r.command).startsWith('sync') && Date.parse(r.started_at) >= job.since - 5000);
    const bad = runs.find((r) => r.status !== 'ok');
    job = !runs.length || bad
      ? { ticker: job.ticker, phase: 'error', message: bad ? `${bad.source}: ${bad.status} ${bad.message || ''}` : 'run ended', since: Date.now() }
      : { ticker: job.ticker, phase: 'done', rows: runs.reduce((n, r) => n + (r.rows || 0), 0),
          requests: runs.reduce((n, r) => n + (r.requests || 0), 0), feedCount: runs.length, since: Date.now() };
    if (job.phase === 'done' && job.rows) window.dispatchEvent(new CustomEvent('massive:symbol-updated', { detail: job.ticker }));
  }

  function renderStatusBar() {
    const text = el('netText'), bar = el('netBar');
    let msg = '', progress = null, busy = false, cls = '';
    const running = data?.running;
    if (job && job.ticker === state.ticker) {
      const t = esc(job.ticker);
      if (job.phase === 'checking') { msg = `${t}: checking…`; busy = true; }
      else if (job.phase === 'waiting') {
        msg = `${t}: next in line · ${data?.live?.source ? `${esc(data.live.source)} is fetching` : 'the fetcher is busy'}${waitNote()}`;
        busy = true;
      } else if (job.phase === 'downloading') {
        msg = running ? `${t}: updating on ${esc(data.live.source)}${job.feeds.length > 1 ? ` (${job.feeds.length} feeds)` : ''} · ${num(data.live.requests)} req${waitNote()}`
          : `${t}: starting…`;
        busy = true;
      } else if (job.phase === 'done') {
        msg = `${t}: +${num(job.rows)} rows from ${job.feedCount} feed${job.feedCount === 1 ? '' : 's'}`;
        cls = 'ok';
      }
      else if (job.phase === 'current') { msg = `${t} ✓ to ${short(job.last)} · next ${short(job.nextDue)}`; }
      else if (job.phase === 'backfilling') { msg = `${t}: backfill ${Math.round((job.progress || 0) * 100)}%`; progress = job.progress || 0; }
      else if (job.phase === 'queued') { msg = `${t}: queued`; }
      else if (job.phase === 'fetching') { msg = `${t}: fetching`; busy = true; }
      else if (job.phase === 'error') { msg = `${t}: fetch failed`; cls = 'bad'; text.title = job.message; }
    } else if (running) {
      msg = `Fetcher${data.live.current ? `: ${esc(data.live.current)}` : ''} · ${num(data.live.requests)} req`;
      busy = true;
    }
    // Frames built from finer bars (derive.py): what, from what, for which series, how far along.
    const d = data?.derive;
    if (d && (!msg || !busy)) {
      msg = `Building ${esc(d.to.join(', '))} from ${esc(d.from)} bars <span class="net-derived">*derived</span> · `
        + `${esc(d.source)}:${esc(d.ticker)} (${num(d.n)} of ${num(d.of)})`;
      progress = d.of ? d.n / d.of : null;
      busy = true;
      cls = '';
    }
    text.innerHTML = msg ? `${busy ? '<span class="net-spin"></span>' : ''}${msg}` : '';
    text.className = cls;
    bar.hidden = progress == null && !busy;
    bar.classList.toggle('indeterminate', progress == null && busy);
    bar.firstElementChild.style.width = progress == null ? '' : `${Math.round(progress * 100)}%`;
  }

  // A wait in progress on the running feed: its limit (or the server's Retry-After) and how long.
  function waitNote() {
    const src = data?.live?.source;
    const w = (data?.limits || []).find((l) => l.source === src)?.wait;
    if (!w) return '';
    const left = Math.max(0, Math.round(w.until - Date.now() / 1000));
    return left ? ` · waiting ${left} s (${esc(w.reason)})` : '';
  }

  window.addEventListener('massive:symbol', (e) => { openTicker = e.detail; checkSymbol(e.detail); });

  // Every server's limits beside what this machine keeps to and has used.
  function limitsHtml(rows) {
    const t = Date.now() / 1000;
    return rows.map((l) => {
      const w = l.window;
      const full = w.allowed && w.used >= w.allowed;
      const wait = l.wait ? Math.max(0, Math.round(l.wait.until - t)) : 0;
      const next = wait ? `<span class="warn">in ${wait} s</span> <div class="sub">${esc(l.wait.reason)}</div>`
        : full ? `<span class="warn">in ${Math.ceil(w.freeIn)} s</span> <div class="sub">window full</div>` : 'now';
      const live = data.running && data.live.source === l.source;
      return `<tr class="${live ? 'live' : ''}"><td><b>${esc(l.source)}</b><div class="sub">${esc(l.plan)}</div></td>
        <td>${esc(l.published)}</td><td>${esc(l.keptTo)}</td>
        <td>${w.allowed ? `${meter(w.used / w.allowed, full ? 'warn' : '')}<div class="sub">${num(w.used)} of ${num(w.allowed)} in the last ${w.periodSeconds >= 3600 ? 'hour' : 'minute'}</div>` : '<span class="sub">no limit</span>'}</td>
        <td>${next}</td>
        <td class="num">${num(l.today)} <span class="sub">/ ${num(l.perDay)}</span></td><td class="num">${num(l.perRun)}</td></tr>`;
    }).join('');
  }

  // -- Fetcher view --------------------------------------------------------------------
  function logHtml(lines) {
    return lines.map((line) => {
      const cls = /error|refused|stopped|cap .*reached|not available|could not/i.test(line) ? 'bad'
        : /: source=|requests used|^.{20}scan:/.test(line) ? 'head' : /: [1-9][\d,]* rows|new data|icon saved/.test(line) ? 'good' : '';
      return `<div class="${cls}">${esc(line)}</div>`;
    }).join('');
  }

  function renderFetcherView() {
    loadDeps();
    loadDatasets();
    if (!data) { el('actStatus').innerHTML = statusLine(); return; }
    const { plan, today, backfill } = data;
    el('actStatus').innerHTML = statusLine();
    el('actFeeds').innerHTML = (data.feeds || []).map((f) => `<button type="button" data-feed="${esc(f.name)}"
        class="${f.name === feed ? 'active' : ''}" title="${esc(f.plan)}${f.archive ? ' · public archive: fills history once per symbol, then closed' : ''}${f.keys === false ? (f.name === 'alpaca'
          ? ' · keys not set: US stocks (SIP from 2016, IEX live), options and news wait for ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY; crypto needs no key and is collected'
          : ' · key not set') : ''}${f.configured ? '' : ' · not configured here'}">${esc(f.name)}${f.closed ? ' <span class="chip">closed</span>' : f.keys === false ? ` <span class="chip">${f.name === 'alpaca' ? 'stocks need keys' : 'no key'}</span>` : ''}</button>`).join('');
    loadAudits();
    el('actLimits').innerHTML = limitsHtml(data.limits || []);
    el('scanBtn').disabled = !!data.running;
    el('scanNote').innerHTML = scanNote ? esc(scanNote) : '';
    const mine = data.running && data.live.source === data.source;  // a run of another feed is not this feed's
    const runReq = mine ? data.live.requests : 0;
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
      ${data.source !== 'massive' ? '' : `<div class="tile"><h5>Directory icons</h5><div class="big">${num(data.directory?.icons)} <small>/ ${num(data.directory?.companies)} companies</small></div>
        ${meter(data.directory?.companies ? data.directory.iconsChecked / data.directory.companies : 0, 'good')}
        <small>${data.directory?.companies ? `Nasdaq + NYSE · ≈${num(data.directory.requestsLeft)} requests left (~${data.directory.days} days, leftover budget only)` : 'listing arrives with the next sync'}</small></div>`}
      ${data.ticks ? `<div class="tile"><h5>Ticks</h5><div class="big">${(data.ticks.bytes / 1e9).toFixed(1)} <small>GB · ${num(data.ticks.series)} symbols</small></div>
        <small>${data.ticks.paused ? `<span class="down">backfill paused: ${esc(data.ticks.paused)}</span>` : `backfill ${num(data.ticks.days)} days · new ticks always`} · ${num(Math.round(data.ticks.freeBytes / 1e9))} GB free</small></div>` : ''}
      <div class="tile"><h5>Plan</h5><div class="big">${esc(plan.tier)}</div>
        <small>history from ${esc(plan.historyFloor)}</small></div>`;

    const counts = { all: data.symbols.length };
    for (const s of data.symbols) counts[s.state] = (counts[s.state] || 0) + 1;
    el('actFilters').innerHTML = ['all', 'fetching', 'new', 'backfilling', 'queued', 'current', 'retired', 'closed', 'blocked']
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
        <td>${dataIcon(s, 14)} <span class="state ${s.state}">${esc(DATA_LABEL[s.state])}</span>${s.note ? `<div class="sub">${esc(s.note)}</div>` : ''}</td>
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

  el('actFeeds').addEventListener('click', (e) => {
    const b = e.target.closest('[data-feed]');
    if (!b || b.dataset.feed === feed) return;
    feed = b.dataset.feed;
    try { localStorage.setItem('fetcherFeed', feed); } catch { /* not kept */ }
    poll();
  });

  // Audits: box plots of the differences, refreshed every minute while the Fetcher view is open.
  let audits = { at: 0 };
  async function loadAudits() {
    if (Date.now() - audits.at < 60000) return;
    audits.at = Date.now();
    try { audits = { ...(await (await fetch('/api/audits')).json()), at: Date.now() }; } catch { return; }
    renderAudits();
  }
  function boxSvg(b, tol) {
    if (!b) return '<span class="p-muted">–</span>';
    const w = 96, h = 14, lim = Math.max(Math.abs(b.min), Math.abs(b.max), tol * 2, 1e-9);
    const x = (v) => (w / 2) + (v / lim) * (w / 2 - 2);
    return `<svg class="boxplot" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true">
      <rect class="tol" x="${x(-tol)}" y="0" width="${x(tol) - x(-tol)}" height="${h}"/>
      <line class="zero" x1="${w / 2}" x2="${w / 2}" y1="0" y2="${h}"/>
      <line class="whisker" x1="${x(b.min)}" x2="${x(b.max)}" y1="${h / 2}" y2="${h / 2}"/>
      <rect class="box" x="${x(b.q1)}" y="3" width="${Math.max(1, x(b.q3) - x(b.q1))}" height="${h - 6}"/>
      <line class="median" x1="${x(b.median)}" x2="${x(b.median)}" y1="2" y2="${h - 2}"/></svg>
      <span class="small">${b.median >= 0 ? '+' : ''}${b.median.toFixed(2)} <span class="p-muted">p95 ${b.p95.toFixed(2)}</span></span>`;
  }
  function renderAudits() {
    const rows = audits.audits || [];
    const vcls = { pass: 'fresh', warn: 'backfilling', fail: 'run-error', 'no-data': '' };
    el('auditScores').textContent = Object.entries(audits.scores || {}).map(([f, s]) => `${f}: ${s.passed}/${s.audits} passed`).join(' · ')
      + (audits.queued ? ` · ${audits.queued} queued` : '');
    el('actAudits').innerHTML = rows.length ? rows.slice(0, 20).map((a) => {
      const r = a.report || {}, bx = r.box || {}, tol = r.tolerance || {};
      const span = new Date(a.start_ms).toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
      const tim = r.timing ? (r.timing.bestShiftSeconds ? `<span class="down">shifted ${r.timing.bestShiftSeconds}s</span>` : 'aligned')
        + (r.timing.stampsOffTheGrid ? ` · ${r.timing.stampsOffTheGrid} off-grid` : '') : '–';
      return `<tr title="${esc(a.reason || '')}${r.worst?.length ? ` · worst: ${esc(r.worst.map((w) => `${w.field} ${w.bps} bps`).join(', '))}` : ''}">
        <td>${ago(a.at)}</td><td><b>${esc(a.ticker)}</b> <span class="sub">${esc(span)} ET</span></td>
        <td>${esc(a.owner)} → ${esc(a.auditor)}</td><td><span class="state ${vcls[a.verdict] || ''}">${esc(a.verdict)}</span></td>
        <td class="num">${num(a.matched)}<div class="sub">${num(a.only_owner)}/${num(a.only_auditor)} one-sided</div>${r.note ? `<div class="sub" title="${esc(r.note)}">cross-venue</div>` : ''}${r.ownerOrigin ? `<div class="sub" title="${esc(r.ownerOrigin)}">from ticks</div>` : ''}</td>
        <td>${boxSvg(bx.c, tol.price_bps || 5)}</td><td>${boxSvg(bx.range, 2 * (tol.price_bps || 5))}</td>
        <td>${boxSvg(bx.v, tol.volume_pct || 5)}</td><td class="sub">${tim}</td></tr>`;
    }).join('') : '<tr><td colspan="9" class="empty">No audits yet: they run after new data arrives and at random, once a second feed watches the same symbols (or run <code>python -m massive_collate audit</code>).</td></tr>';
  }

  // Dependency health (deps.py), refreshed every few minutes while the Fetcher view is open.
  let deps = { rows: [], at: 0 };
  async function loadDeps() {
    if (Date.now() - deps.at < 180000) return;
    deps.at = Date.now();
    try { deps.rows = (await (await fetch('/api/deps')).json()).checks || []; } catch { return; }
    renderDeps();
  }
  function renderDeps() {
    const cls = { OK: 'fresh', WARN: 'backfilling', FAIL: 'run-error' };
    const order = { FAIL: 0, WARN: 1, OK: 2 };
    el('depsRows').innerHTML = [...deps.rows].sort((a, b) => order[a.status] - order[b.status]).map((c) => `
      <tr><td class="sub">${esc(c.area)}</td><td><b>${esc(c.item)}</b></td>
        <td><span class="state ${cls[c.status] || ''}">${esc(c.status)}</span></td>
        <td>${esc(c.found)}</td><td class="sub">${esc(c.action || '')}</td></tr>`).join('');
  }

  // Research datasets (datasets.py): refreshed every few minutes while the Fetcher view is open.
  let datasets = { rows: [], at: 0 };
  async function loadDatasets() {
    if (Date.now() - datasets.at < 180000) return;
    datasets.at = Date.now();
    try { datasets.rows = (await (await fetch('/api/datasets')).json()).datasets || []; } catch { return; }
    renderDatasets();
  }
  function renderDatasets() {
    const day = (ms) => (ms == null ? '–' : new Date(ms).toLocaleDateString('en-CA', { timeZone: 'America/New_York' }));
    el('datasetRows').innerHTML = datasets.rows.map((d) => `
      <tr><td><b>${esc(d.name)}</b><div class="sub">${esc(d.title)}</div></td>
        <td>${esc(d.provider)} <span class="sub">(${esc(d.kind)})</span><div class="sub">${esc(d.licence)}</div></td>
        <td>${d.files ? (d.members ? 'current members' : `${day(d.first)} → ${day(d.last)}`) : '–'}</td>
        <td class="num">${d.files ? (d.members ? `${num(d.members)} members` : `${num(d.values)}<div class="sub">${num(d.series)} series</div>`) : '–'}</td>
        <td>${d.files ? `${num(d.files)}${d.closed ? ` <span class="chip">${num(d.closed)} closed</span>` : ''}<div class="sub">${(d.bytes / 1e6).toFixed(1)} MB${d.due ? ` · ${num(d.due)} due` : ''}</div>`
          : '<span class="sub">not fetched: <code>datasets fetch ' + esc(d.name) + '</code></span>'}</td>
        <td>${d.fetched ? ago(d.fetched) : '–'}</td>
        <td class="num">${d.quality == null ? '–' : d.quality.toFixed(2)}</td></tr>`).join('');
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
        <div class="p-muted small">The current session, as it happens. The fetcher handles everything before it.</div></div>
        ${window.LiveIcon ? `<div class="live-effect" role="group" aria-label="Rail icon effect while the open symbol streams">
          <span class="p-muted small">Icon while streaming</span>
          ${Object.entries(window.LiveIcon.EFFECTS).map(([k, label]) => `<button type="button" data-effect="${k}"
            class="${window.LiveIcon.effect === k ? 'active' : ''}">${esc(label)}</button>`).join('')}</div>` : ''}</div>
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
        </tbody></table></div>
      ${liveStreamsHtml(live)}
      <h4>Exchange checks</h4>
      <div class="table-wrap"><table class="act-table">
        <thead><tr><th>Check</th><th>Found</th><th>Last</th><th>Next</th><th>Cost</th></tr></thead>
        <tbody>${(live.checks || []).map((c) => `
          <tr><td><b>${esc(c.name)}</b></td><td>${esc(c.found)}</td>
            <td>${c.last ? (c.last.length > 10 ? ago(c.last) : esc(c.last)) : '<span class="p-muted">not yet</span>'}</td>
            <td class="sub">${c.next && /^\d{4}-/.test(c.next) ? (c.next.length > 10 ? when(c.next) : esc(c.next)) : esc(c.next || '')}</td>
            <td class="sub">${esc(c.cost)}</td></tr>`).join('')}
        </tbody></table></div>
      <h4>News</h4>
      <div id="liveNews" class="news-list">${liveNewsHtml()}</div>`;
    loadLiveNews();
  }

  // The live recorder's streams (python -m massive_collate live): connection, and per symbol the
  // trades recorded, the last price and how long ago it traded. The stock stream (IEX) follows the
  // open chart and the watchlists.
  function liveStreamsHtml(live) {
    const streams = Object.entries(live.streams || {});
    if (!streams.length) {
      return `<h4>Live streams</h4><div class="empty">No live recorder running. It starts with the hourly sync, or now with
        <code>python -m massive_collate live start</code>.</div>`;
    }
    const rows = streams.flatMap(([src, s]) => {
      const pairs = Object.entries(s.pairs || {}).sort(([a], [b]) => a.localeCompare(b));
      const head = `<tr><td><b>${esc(src)}</b></td><td colspan="4">${s.connected
        ? `<span class="rec on"></span> connected${s.since ? ` · since ${when(s.since)}` : ''}${s.symbols
          ? ` · ${s.symbols.length} of 30 symbols` : ''}`
        : `<span class="state run-error">down</span> <span class="sub">${esc(s.error || '')}</span>`}</td></tr>`;
      return [head, ...pairs.map(([t, p]) => `
        <tr class="has-data" data-ticker="${esc(t)}"><td></td><td><b>${esc(t)}</b>${live.liveTickers?.includes(t) ? ' <span class="state fresh">live</span>' : ''}</td>
          <td class="num">${p.price == null ? '–' : esc(String(p.price))}</td><td class="num">${num(p.trades)}</td>
          <td>${p.last ? ago(new Date(p.last).toISOString()) : '–'}</td></tr>`)];
    });
    return `<h4>Live streams <span class="p-muted small">crypto: public exchange feeds, no key · stocks: IEX's trades
      through Alpaca (your keys; one exchange, a small share of volume) · each feed's trades in its own tick file</span></h4>
      <div class="table-wrap"><table class="act-table">
        <thead><tr><th>Feed</th><th>Symbol</th><th class="num">Last price</th><th class="num">Trades recorded</th><th>Last trade</th></tr></thead>
        <tbody>${rows.join('')}</tbody></table></div>`;
  }

  // The newest newsworthy items (headlines, unscheduled closures, early closes, big moves).
  let liveNews = { items: [], at: 0 };
  async function loadLiveNews() {
    if (Date.now() - liveNews.at < 60000) return;
    liveNews.at = Date.now();
    try { liveNews.items = (await (await fetch('/api/announcements')).json()).items || []; } catch { return; }
    const box = el('liveNews');
    if (box) box.innerHTML = liveNewsHtml();
  }
  function liveNewsHtml() {
    if (!liveNews.items.length) return '<div class="empty">Nothing newsworthy right now. Headlines arrive with the fetcher’s runs.</div>';
    return liveNews.items.slice(0, 15).map((it) => `
      <div class="news-row ${esc(it.kind)}">
        <span class="src">${esc(it.source)}</span>
        <div class="news-main">${it.url ? `<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${esc(it.text)}</a>` : `<span>${esc(it.text)}</span>`}
          <div class="p-muted small">${it.kind === 'news' ? ago(it.time) : 'now'} · ${esc(it.exchange)}${(it.tickers || []).length ? ` · ${esc(it.tickers.join(' '))}` : ''}</div></div>
      </div>`).join('');
  }

  document.addEventListener('click', (e) => {
    if (e.target.closest('#scanBtn')) { startScan(); return; }
    const fx = e.target.closest('#viewLive .live-effect button[data-effect]');
    if (fx) {
      window.LiveIcon.set(fx.dataset.effect);
      for (const b of fx.parentElement.querySelectorAll('button')) b.classList.toggle('active', b === fx);
      return;
    }
    const item = e.target.closest('#actSymbols tr.has-data, #viewLive tr.has-data');
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
