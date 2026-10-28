'use strict';
/* Plugins view (rail icon: a plug): every data provider plugin, a feed or source (gui/plugins.py).
   On the left the plugins, those with keys and the public ones, each with its state and its plan;
   in the middle the one picked: its plan and where that came from (set by you, found by its scan,
   the default), its limits, what it can do on that plan, its live streams and its last capability
   scan with a Rescan; on the right its part in the delivery scheme (the eras it delivers or stands
   by for), its requests today, the symbols it watches, its keys (set or not, never their values).
   Each plugin refreshes its own plan info: a rescan asks the provider what the key really gets.
   Asked again every minute while shown, every few seconds while a rescan runs. */
(() => {
  const box = document.getElementById('viewPlugins');
  const { esc } = window.MassiveIcons;
  let data = null, sel = null, note = '', timer = null;

  const STATE = { ready: 'ready', partly: 'keys not set', 'no key': 'no key', killed: 'killed' };
  const ORIGIN = { you: 'set by you', scan: 'found by its scan', default: 'default' };
  const JOBS = { day: 'daily bars', minute: '1-minute', second: '1-second' };
  const num = (n) => (n == null ? '–' : Number(n).toLocaleString('en-US', { maximumFractionDigits: 1 }));
  const ago = (iso) => {
    if (!iso) return 'never';
    const h = (Date.now() - Date.parse(iso)) / 36e5;
    return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
  };
  const rate = (r) => {
    if (!r || !r[0]) return 'no limit';
    const [n, s] = r;
    return Math.abs(s - 3600) < 120 ? `${num(n)} an hour` : Math.abs(s - 60) < 5 ? `${num(n)} a minute` : `${num(n)} per ${num(s)} s`;
  };
  const days = (d) => (d < 1 / 24 ? 'under an hour' : d < 1 ? `${(d * 24).toFixed(1)} h` : `${num(d)} days`);
  const chip = (state) => `<span class="ex-state pl-${esc(state.replace(' ', '-'))}">${esc(STATE[state] || state)}</span>`;
  const tile = (title, big, small) => `<div class="tile"><h5>${title}</h5><div class="big">${big}</div><small>${small}</small></div>`;
  const plugin = () => data?.plugins.find((p) => p.name === sel) || null;
  const tierOf = (p) => p.plan.vars.find((v) => v.choices.length) || null;

  async function load() {
    clearTimeout(timer);
    if (document.body.dataset.view !== 'plugins' || document.hidden) return;
    try {
      data = await api('/api/plugins');
      if (!sel || !data.plugins.some((p) => p.name === sel)) sel = data.plugins[0]?.name || null;
    } catch (err) { showError(err); }
    render();
    timer = setTimeout(load, data?.running ? 4_000 : 60_000);
  }

  async function rescan(name) {
    note = '';
    try {
      const q = name ? `?name=${encodeURIComponent(name)}` : '';
      const res = await fetch(`/api/plugins/rescan${q}`, { method: 'POST', headers: { 'X-Massive-Charts': '1' } });
      const out = await res.json().catch(() => ({}));
      note = res.ok ? `Rescan started: ${name ? plugin()?.title || name : 'every plugin set up here'} (a few requests each).`
        : out.error || res.statusText;
    } catch (err) { note = String(err.message || err); }
    render();
    setTimeout(load, 1_000);
  }

  // -- left: the plugins ---------------------------------------------------------------------
  function listHtml() {
    const groups = data.groups.map((g) => {
      const ps = data.plugins.filter((p) => p.group === g);
      if (!ps.length) return '';
      return `<h4 class="ex-group">${esc(g)}</h4>` + ps.map((p) => {
        const t = tierOf(p);
        const plan = t ? `${t.value} · ${ORIGIN[t.origin]}` : p.plan.tier;
        return `<button type="button" class="ex-venue${p.name === sel ? ' active' : ''}" data-plugin="${esc(p.name)}" title="${esc(p.about)}">
          <span class="ex-name">${esc(p.title)}</span>${chip(p.state)}
          <span class="ex-count">${esc(plan)}${p.scan.running ? ' · scanning…' : ''}</span></button>`;
      }).join('');
    }).join('');
    const left = data.errors.length ? `<h4 class="ex-group">Left out</h4>${data.errors.map((e) => `
      <div class="pl-error"><b>${esc(e.where)}</b><div class="p-muted small">${esc(e.why)}</div></div>`).join('')}` : '';
    return groups + left;
  }

  // -- middle: the plugin picked -------------------------------------------------------------
  function planHtml(p) {
    const name = p.name;
    const rows = p.plan.vars.map((v) => `<tr><td><code>${esc(v.name)}</code></td><td><b>${esc(v.value ?? '–')}</b></td>
        <td class="pl-origin pl-${esc(v.origin)}">${esc(ORIGIN[v.origin])}</td>
        <td class="p-muted">${esc(v.choices.join(' · '))}</td></tr>`).join('');
    const differs = p.plan.differs.map((d) => `<div class="pl-warn">Your <code>${esc(d.name)}</code> is <b>${esc(d.yours)}</b>,
        but its scan found <b>${esc(d.found)}</b>. Yours is used; change it if the scan is right.</div>`).join('');
    const found = Object.entries(p.plan.found || {});
    return `<h4>Plan</h4>
      ${rows ? `<table class="act-table ex-mini pl-plan"><thead><tr><th>Variable</th><th>Value</th><th>From</th><th>Choices</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<div class="p-muted small">One fixed plan: ${esc(p.plan.tier)}.</div>`}
      ${found.length ? `<div class="p-muted small">Its last scan found ${found.map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>${
        p.plan.alike?.[k]?.length ? ` (or ${p.plan.alike[k].map(esc).join(', ')}: their answers are the same, so the lower limits are used)` : ''}`).join(', ')}:
        used wherever you have not set it yourself.</div>` : ''}
      ${differs}
      ${p.plan.note ? `<div class="pl-note">${esc(p.plan.note)}.</div>` : ''}
      ${rows ? `<div class="p-muted small">To set a plan or a key: <code>${esc(data.keyScript)} ${esc(name)}</code></div>` : ''}`;
  }

  function limitsHtml(p) {
    const l = p.limits;
    if (l.requests_a_day == null) return `<h4>Limits</h4><div class="p-muted small">${esc(l.published || 'Not set up here.')}</div>`;
    return `<h4>Limits</h4><div class="tiles ex-tiles pl-tiles">
        ${tile('Requests a day', num(l.requests_a_day), l.per_day ? `cap ${num(l.per_day)}` : 'its rate allows')}
        ${tile('Rate kept', rate(l.rate), 'a little inside its server\'s')}
        ${tile('A run', num(l.per_run), 'requests at most')}
        ${tile('Symbols a month', l.symbols_a_month ? num(l.symbols_a_month) : 'any', l.symbols_a_month ? 'different symbols' : 'no monthly cap')}
      </div>${l.published ? `<div class="p-muted small">Its server publishes: ${esc(l.published)}</div>` : ''}`;
  }

  function capabilitiesHtml(p) {
    if (!p.capabilities.length) return '';
    return `<h4>What it can do on this plan</h4><table class="act-table ex-mini"><thead><tr><th>Asset</th><th>Bars</th>
        <th>Market</th><th>From</th><th>Fresh</th><th class="num">A request</th></tr></thead><tbody>
      ${p.capabilities.map((c) => `<tr><td>${esc(c.asset)}</td><td>${esc(JOBS[c.resolution] || c.resolution)}</td><td>${esc(c.market)}</td>
        <td>${esc(c.since)}</td><td>${esc(c.fresh)}</td><td class="num">${c.perRequest == null ? 'whole history' : `${num(c.perRequest)} bars`}</td></tr>`).join('')}
      </tbody></table>
      ${p.streams.length ? `<h4>Live</h4>${p.streams.map((s) => `<div class="small"><b>${esc(s.asset)}</b> ${esc(s.what)}</div>`).join('')}` : ''}`;
  }

  function scanHtml(p) {
    if (!p.scan.has) return `<h4>Capability scan</h4><div class="p-muted small">${esc(p.title)} has none: a public feed with one fixed plan.</div>`;
    const can = p.configured && p.state !== 'no key' && !data.offline && !data.running;
    const why = data.offline ? 'This offline copy of the app sends no requests' : !p.configured || p.state === 'no key'
      ? 'Its keys are not set' : data.running ? 'A rescan is running' : 'Ask the provider again what the key gets (a few requests)';
    const rows = p.scan.results.map((r) => `<tr><td>${esc(r.what)}</td>
        <td>${r.available ? '<span class="up">yes</span>' : '<span class="down">no</span>'}</td><td class="p-muted">${esc(r.detail)}</td></tr>`).join('');
    return `<h4 class="pl-scan-head"><span>Capability scan</span>
        <button type="button" data-rescan="${esc(p.name)}"${can ? '' : ' disabled'} title="${esc(why)}">${p.scan.running ? 'Scanning…' : 'Rescan'}</button></h4>
      <div class="p-muted small">${p.scan.at ? `Last asked ${ago(p.scan.at)}` : 'Not scanned yet'}: on each start of the app, weekly in the sync, and here.</div>
      ${rows ? `<table class="act-table ex-mini"><tbody>${rows}</tbody></table>` : ''}`;
  }

  function mainHtml() {
    const p = plugin();
    if (!p) return '<div class="empty">No plugin found.</div>';
    return `${planHtml(p)}${limitsHtml(p)}${capabilitiesHtml(p)}${scanHtml(p)}
      ${p.notes.length ? `<h4>Notes</h4>${p.notes.map((n) => `<div class="small">${esc(n)}</div>`).join('')}` : ''}`;
  }

  // -- right: its part in the delivery scheme, its usage, its keys ---------------------------
  function sideHtml() {
    const p = plugin();
    if (!p) return '';
    const u = p.usage;
    const used = u.perDay ? Math.min(100, (u.today / u.perDay) * 100) : 0;
    const first = p.delivery.filter((e) => e.first), standby = p.delivery.filter((e) => !e.first);
    const era = (e) => `<tr><td>${esc(JOBS[e.job] || e.job)}</td><td>${esc(e.asset)}</td><td>${esc(e.start)} → ${esc(e.end || 'now')}</td>
        <td class="p-muted">${e.unique ? 'only source' : e.first ? `then ${esc(e.others.join(', '))}` : `after ${esc(e.others[0] || '')}`}</td></tr>`;
    const keys = p.keys.length ? p.keys.map((k) => `<div class="small"><code>${esc(k.name)}</code> ${k.set ? '<span class="up">set</span>' : '<span class="down">not set</span>'}</div>`).join('')
      : '<div class="p-muted small">Needs no key.</div>';
    return `<h3>${esc(p.title)} ${chip(p.state)}</h3>
      <div class="p-muted small">${esc(p.about)}${p.origin !== 'built in' ? ` · ${esc(p.origin)}` : ''}</div>
      ${p.kill ? `<div class="pl-warn">Killed until ${esc(p.kill.until?.slice(0, 16).replace('T', ' '))} UTC: ${esc(p.kill.why)}</div>` : ''}
      <div class="tiles ex-tiles">
        ${tile('Requests today', num(u.today), `of ${num(u.perDay)} a day`)}
        ${tile('Symbols', num(p.watched), 'watched on it')}
        ${tile('Its load', days(p.load), 'of its limits for what it delivers')}
        ${tile('Delivers', num(first.length), `eras first · ${num(standby.length)} standing by`)}
      </div>
      <div class="pl-usage" title="${num(u.today)} of ${num(u.perDay)} requests today"><i style="width:${used.toFixed(1)}%"></i></div>
      ${first.length ? `<h4>What it delivers</h4><table class="act-table ex-mini"><tbody>${first.map(era).join('')}</tbody></table>` : ''}
      ${standby.length ? `<h4>Standing by for</h4><table class="act-table ex-mini"><tbody>${standby.map(era).join('')}</tbody></table>` : ''}
      <h4>Keys</h4>${keys}
      ${p.signup ? `<div class="p-muted small">From ${esc(p.signup)}</div>` : ''}`;
  }

  function render() {
    if (!data) { box.innerHTML = '<div class="empty">Loading the plugins…</div>'; return; }
    const p = plugin();
    const scrolled = box.querySelector('.ex-list')?.scrollTop || 0;
    const setUp = data.plugins.filter((x) => x.configured && x.state !== 'no key').length;
    box.innerHTML = `
      <div class="ex-venues"><div class="view-head"><div><h2>Plugins</h2>
        <div class="p-muted small">${num(data.plugins.length)} providers · ${num(setUp)} set up</div></div></div>
        <button type="button" class="pl-all" data-rescan="*"${data.offline || data.running ? ' disabled' : ''}
          title="${data.offline ? 'This offline copy of the app sends no requests' : 'Every plugin set up here asks its provider again (a few requests each)'}">${data.running ? 'Scanning…' : 'Rescan all'}</button>
        ${note ? `<div class="p-muted small pl-note-line">${esc(note)}</div>` : ''}
        ${listHtml()}</div>
      <div class="ex-list"><div class="view-head"><div><h2>${esc(p?.title || '')}</h2>
        <div class="p-muted small">${esc(p ? `${p.archive ? 'closed archive' : 'feed'} · ${p.kind} data · ${p.assets.join(', ')}` : '')}</div></div></div>${mainHtml()}</div>
      <aside class="ex-side">${sideHtml()}</aside>`;
    box.querySelector('.ex-list').scrollTop = scrolled;
  }

  box.addEventListener('click', (e) => {
    const t = e.target.closest('[data-plugin],[data-rescan]');
    if (!t || t.disabled) return;
    if (t.dataset.plugin && t.dataset.plugin !== sel) {
      sel = t.dataset.plugin; render();
    } else if (t.dataset.rescan) {
      rescan(t.dataset.rescan === '*' ? null : t.dataset.rescan);
    }
  });
  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'plugins') load(); else clearTimeout(timer); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  if (document.body.dataset.view === 'plugins') load();  // the page opened on this view (before this script listened)
})();
