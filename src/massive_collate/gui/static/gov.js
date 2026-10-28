'use strict';
/* Gov view (rail icon: a state house): what the government and the regulators publish (gov.py).
   The current regulatory parameters first (policy rates with their dates, rule parameters with the
   rule and its newest Federal Register document), then the agencies' newest, filtered by agency
   (FINRA: its rule filings, from the Federal Register), the next scheduled releases, then each
   source's state and pages to read at the agencies. Each agency is a publisher plugin, read by the
   recorder on its own schedule under its scrape checks (the Plugins view); read here every minute. */
(() => {
  const box = document.getElementById('viewGov');
  const { esc, safeUrl } = window.MassiveIcons;
  const FILTERS = [['all', 'All'], ['Fed', 'Fed'], ['SEC', 'SEC'], ['FINRA', 'FINRA'], ['BLS', 'BLS'],
                   ['CFTC', 'CFTC'], ['Treasury', 'Treasury'], ['rules', 'Federal Register']];
  const STATE_CLASS = { ok: 'ok', unchanged: 'ok', waiting: 'wait', failed: 'bad', refused: 'bad' };
  let data = null, fetchedAt = 0, filter = 'all', query = '';

  async function load(force = false) {
    if (!force && data && Date.now() - fetchedAt < 60000) return render();
    try {
      data = await api('/api/gov');
      fetchedAt = Date.now();
    } catch (err) { showError(err); }
    render();
  }

  const link = (text, url) => (safeUrl(url) ? `<a href="${esc(safeUrl(url))}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text));
  // The Federal Register dates its documents by day only (stored at 00:00 UTC): shown as a date.
  function when(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return iso.endsWith('T00:00:00+00:00')
      ? d.toLocaleDateString('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric', year: 'numeric' })
      : `${d.toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })} ET`;
  }
  const matches = (it, f) => (f === 'all' || (f === 'FINRA' ? it.tags.includes('FINRA') : f === 'rules' ? it.source === 'fedreg' : it.agency === f));

  function parameter(p) {
    const levels = p.levels ? `<div class="gov-levels">${p.levels.map((l) => `<span>L${l.level} −${l.down}%: <b>${priceText(l.at)}</b></span>`).join('')}</div>
      <div class="p-muted small">derived: from the S&amp;P 500's close of ${esc(p.levelsFrom.day)} (${priceText(p.levelsFrom.close)})</div>` : '';
    const latest = p.latest ? `<div class="p-muted small">newest on this rule: ${link(p.latest.title, p.latest.url)} · ${esc(when(p.latest.published))}</div>` : '';
    return `<div class="gov-param">
      <div class="gov-param-name">${esc(p.name)}</div>
      <div class="gov-param-value${p.value.length > 40 ? ' long' : ''}">${esc(p.value)}</div>
      ${levels}
      <div class="p-muted small">${p.link ? link(p.source, p.link) : esc(p.source)}${p.asOf ? ` · as of ${esc(p.asOf)}` : ''}</div>
      ${latest}</div>`;
  }

  function render() {
    if (document.body.dataset.view !== 'gov') return;
    if (!data) { box.innerHTML = '<div class="empty">Loading…</div>'; return; }
    const q = query.trim().toLowerCase();
    const counts = Object.fromEntries(FILTERS.map(([k]) => [k, data.items.filter((it) => matches(it, k)).length]));
    const rows = data.items.filter((it) => matches(it, filter) && (!q || [it.title, it.agency, it.kind, ...it.tags].some((v) => v && v.toLowerCase().includes(q))));
    const waiting = data.sources.filter((s) => s.state === 'waiting');
    box.innerHTML = `
      <div class="view-head"><div><h2>Government &amp; regulators</h2>
        <div class="p-muted small">The Fed, the SEC, BLS, the Federal Register (SEC with FINRA's and the exchanges' rule filings, CFTC, Fed,
          Treasury) and the New York Fed's rates: official feeds, each agency a plugin read on its own schedule (Plugins view).</div></div>
        <div class="view-actions"><input id="govSearch" type="search" placeholder="Filter" value="${esc(query)}"></div></div>
      <h3 class="gov-h">Current regulatory parameters</h3>
      <div class="gov-params">${data.parameters.map(parameter).join('')}</div>
      <h3 class="gov-h">Newest</h3>
      <div class="filters">${FILTERS.map(([k, l]) => `<button data-gf="${k}" class="${k === filter ? 'active' : ''}">${l} <span class="chip">${counts[k]}</span></button>`).join('')}</div>
      <div class="news-list gov-list">${rows.length ? rows.map((it) => `
        <div class="news-row">
          <span class="src gov-src">${esc(it.agency)}${it.tags.map((t) => ` <span class="chip">${esc(t)}</span>`).join('')}</span>
          <div class="news-main">${link(it.title, it.url)}
            <div class="p-muted small">${esc(it.kind || '')} · ${esc(when(it.published))}</div></div>
        </div>`).join('') : `<div class="empty">${data.items.length ? 'Nothing matches.' : "Nothing yet: the recorder reads the agencies' feeds shortly."}</div>`}</div>
      ${data.upcoming?.length ? `<h3 class="gov-h">Scheduled</h3><div class="table-wrap"><table class="gov-sources"><tbody>
        ${data.upcoming.map((c) => `<tr><td>${esc(when(c.when))}</td><td>${esc(c.what)}</td><td class="p-muted">${esc(c.publisher)}</td></tr>`).join('')}</tbody></table></div>` : ''}
      <h3 class="gov-h">Sources</h3>
      ${waiting.length ? `<div class="p-muted small gov-note">${esc(waiting[0].message)} (as a user environment variable, like the keys).</div>` : ''}
      <div class="table-wrap"><table class="gov-sources"><thead><tr><th>Agency</th><th>Feed</th><th>State</th><th>Last answered</th></tr></thead><tbody>
        ${data.sources.map((s) => `<tr><td>${esc(s.agency)}</td><td>${esc(s.title)}</td>
          <td><span class="gov-state ${STATE_CLASS[s.state] || ''}">${esc(s.state)}</span>${s.message && s.state !== 'waiting' ? ` <span class="p-muted small">${esc(s.message)}</span>` : ''}</td>
          <td>${s.okAt ? esc(when(s.okAt)) : '–'}</td></tr>`).join('')}</tbody></table></div>
      <div class="p-muted small gov-note">To read at the agencies (FINRA's site is not read by the app: its terms bar automated readers):
        ${data.links.map((l) => link(l.title, l.url)).join(' · ')}</div>`;
    document.getElementById('govSearch').addEventListener('input', (e) => {
      // drawn again as typed: the new box gets the caret (or selection) where it was, not at the start
      const { selectionStart: from, selectionEnd: to } = e.target;
      query = e.target.value;
      render();
      const box = document.getElementById('govSearch');
      box.focus();
      box.setSelectionRange(from, to);
    });
  }

  box.addEventListener('click', (e) => {
    const f = e.target.closest('[data-gf]');
    if (f) { filter = f.dataset.gf; render(); }
  });
  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'gov') load(); });
  setInterval(() => { if (document.body.dataset.view === 'gov') load(); }, 60000);
  if (document.body.dataset.view === 'gov') load();  // the page opened on this view (before this script listened)
})();
