// Fetcher activity: a live chip in the toolbar and a dialog showing what the collectors are
// gathering — the running sync, plan usage, each symbol's backfill and the log. Read-only.
(() => {
  'use strict';

  const POLL_OPEN_MS = 2000;
  const POLL_CLOSED_MS = 10000;
  const STATE_ORDER = { fetching: 0, backfilling: 1, queued: 2, due: 3, fresh: 4 };
  const STATE_LABEL = {
    fetching: 'fetching now', backfilling: 'backfilling', queued: 'queued',
    due: 'due for refresh', fresh: 'up to date',
  };
  const el = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const num = (n) => (n == null ? '–' : Number(n).toLocaleString());

  let data = null;
  let filter = 'all';
  let timer = null;
  let followLog = true;

  function ago(iso) {
    if (!iso) return 'never';
    const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 0) return `in ${dur(-s)}`;
    return `${dur(s)} ago`;
  }
  function dur(s) {
    if (s == null) return '–';
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
  }
  const day = (iso) => (iso ? iso.slice(0, 10) : '–');
  const when = (iso) => (iso ? new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '–');

  async function poll() {
    clearTimeout(timer);
    try {
      const res = await fetch('/api/activity?lines=300');
      if (!res.ok) throw new Error(`${res.status}`);
      data = await res.json();
      renderChip();
      if (el('activityDialog').open) renderDialog();
    } catch (e) {
      el('fetchChip').classList.remove('running');
      el('fetchChipText').textContent = 'Fetchers: unavailable';
    }
    timer = setTimeout(poll, el('activityDialog').open ? POLL_OPEN_MS : POLL_CLOSED_MS);
  }

  function renderChip() {
    const chip = el('fetchChip');
    chip.classList.toggle('running', data.running);
    const t = data.today;
    if (data.running) {
      const live = data.live;
      el('fetchChipText').textContent = `${live.current || 'starting'} · ${num(live.requests)} req`;
    } else {
      const b = data.backfill;
      el('fetchChipText').textContent = b.done < b.total ? `Idle · ${b.done}/${b.total} backfilled` : 'Idle · all current';
    }
    chip.title = `Fetchers — ${num(t.requests)} of ${num(data.plan.maxPerDay)} requests used today. Click for details.`;
  }

  function bar(fraction, cls = '') {
    const pct = Math.max(0, Math.min(100, fraction * 100));
    return `<span class="meter ${cls}"><span style="width:${pct.toFixed(1)}%"></span></span>`;
  }

  function renderDialog() {
    const { plan, today, live, backfill } = data;
    const last = data.runs.find((r) => r.status !== 'running');
    el('actStatus').innerHTML = data.running
      ? `<span class="dot on"></span><b>Running ${esc(live.command)}</b> — ${live.current ? `fetching <b>${esc(live.current)}</b>` : 'starting'}
         · ${num(live.requests)} of ${num(plan.maxPerRun)} requests · ${dur(live.seconds)}`
      : `<span class="dot"></span><b>Idle</b>${last ? ` — last run ${ago(last.finished_at || last.started_at)}
         (${esc(last.status)}, ${num(last.requests)} requests, ${num(last.rows)} rows)` : ''}`;

    const runReq = data.running ? live.requests : 0;
    el('actTiles').innerHTML = `
      <div class="tile"><h5>Requests today</h5><div class="big">${num(today.requests)} <small>/ ${num(plan.maxPerDay)}</small></div>
        ${bar(today.requests / plan.maxPerDay, today.requests / plan.maxPerDay > 0.9 ? 'warn' : '')}
        <small>${num(today.remaining)} left · resets at 00:00 UTC</small></div>
      <div class="tile"><h5>This run</h5><div class="big">${num(runReq)} <small>/ ${num(plan.maxPerRun)}</small></div>
        ${bar(runReq / plan.maxPerRun)}<small>${plan.requestsPerMinute ? `${plan.requestsPerMinute} requests / minute` : 'no rate limit'}</small></div>
      <div class="tile"><h5>Backfill</h5><div class="big">${num(backfill.done)} <small>/ ${num(backfill.total)} symbols</small></div>
        ${bar(backfill.total ? backfill.done / backfill.total : 1, 'good')}
        <small>${backfill.requestsLeft ? `≈${num(backfill.requestsLeft)} requests left · ~${backfill.runs} hourly run${backfill.runs === 1 ? '' : 's'}` : 'complete'}</small></div>
      <div class="tile"><h5>Plan</h5><div class="big">${esc(plan.tier)}</div>
        <small>history from ${esc(plan.historyFloor)}<br>${plan.refreshHours ? `each symbol refreshed every ${plan.refreshHours} h` : 'refreshed every run'}</small></div>`;

    const counts = { all: data.symbols.length };
    for (const s of data.symbols) counts[s.state] = (counts[s.state] || 0) + 1;
    el('actFilters').innerHTML = ['all', 'fetching', 'backfilling', 'queued', 'due', 'fresh']
      .filter((k) => k === 'all' || counts[k])
      .map((k) => `<button data-filter="${k}" class="${k === filter ? 'active' : ''}">${k === 'all' ? 'All' : STATE_LABEL[k]} <span class="chip">${counts[k] || 0}</span></button>`)
      .join('');

    const rows = data.symbols
      .filter((s) => filter === 'all' || s.state === filter)
      .sort((a, b) => STATE_ORDER[a.state] - STATE_ORDER[b.state] || a.ticker.localeCompare(b.ticker));
    el('actSymbols').innerHTML = rows.length ? rows.map((s) => `
      <tr data-ticker="${esc(s.ticker)}" class="${s.first ? 'has-data' : ''}" title="${s.first ? 'Open in the chart' : 'No data stored yet'}">
        <td><b>${esc(s.exchange ? `${s.ticker}::${s.exchange}` : s.ticker)}</b><div class="sub">${esc(s.name || s.asset)}</div></td>
        <td><span class="state ${s.state}">${STATE_LABEL[s.state]}</span></td>
        <td class="prog">${bar(s.progress, s.progress >= 1 ? 'good' : '')}
          <div class="sub">${s.first ? `${day(s.first)} → ${day(s.last)}` : 'nothing stored yet'}${s.progress < 1 ? ` · target ${day(s.target)}` : ''}</div></td>
        <td>${esc(s.resolution)}</td>
        <td class="num">${s.requestsLeft ? `≈${num(s.requestsLeft)}` : '–'}</td>
        <td>${s.syncedAt ? ago(s.syncedAt) : '–'}<div class="sub">${s.nextRefresh ? `next ${when(s.nextRefresh)}` : ''}</div></td>
      </tr>`).join('') : '<tr><td colspan="6" class="empty">No symbols in this state.</td></tr>';

    el('actRuns').innerHTML = data.runs.slice(0, 12).map((r) => `
      <tr><td>${when(r.started_at)}</td><td>${esc(r.command)}</td>
        <td><span class="state run-${esc(r.status)}">${esc(r.status)}</span></td>
        <td class="num">${num(r.requests)}</td><td class="num">${num(r.rows)}</td><td class="num">${dur(r.seconds)}</td>
        <td class="sub">${esc(r.message || '')}</td></tr>`).join('');

    const log = el('actLog');
    log.innerHTML = data.log.map((line) => {
      const cls = /error|refused|stopped|cap .*reached|not available/i.test(line) ? 'bad'
        : /: source=|requests used/.test(line) ? 'head' : /: [1-9][\d,]* rows/.test(line) ? 'good' : '';
      return `<div class="${cls}">${esc(line)}</div>`;
    }).join('');
    if (followLog) log.scrollTop = log.scrollHeight;
  }

  function open() {
    el('activityDialog').showModal();
    if (data) renderDialog();
    poll();
  }

  document.addEventListener('DOMContentLoaded', () => {
    el('fetchChip').addEventListener('click', open);
    el('activityDialog').addEventListener('close', poll);  // back to the slower rate
    el('actFilters').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-filter]');
      if (!b) return;
      filter = b.dataset.filter;
      renderDialog();
    });
    el('actSymbols').addEventListener('click', (e) => {
      const tr = e.target.closest('tr.has-data');
      if (!tr) return;
      el('activityDialog').close();
      window.dispatchEvent(new CustomEvent('massive:open-symbol', { detail: tr.dataset.ticker }));
    });
    el('actLog').addEventListener('scroll', (e) => {
      const l = e.target;
      followLog = l.scrollHeight - l.scrollTop - l.clientHeight < 24;  // stick to the end unless scrolled up
    });
    poll();
  });
})();
