'use strict';
/* Plugins view (rail icon: a plug): every data provider plugin, a feed or source (gui/plugins.py).
   On the left the plugins, those with keys and the public ones, each with its state and its plan;
   in the middle the one picked: its plan and where that came from (set by you, found by its scan,
   the default), its limits, what it can do on that plan, its live streams and its last capability
   scan with a Rescan; on the right its part in the delivery scheme (the eras it delivers or stands
   by for), its requests today, the symbols it watches, its keys (set or not, never their values),
   its version, the plugin API it was written for and where it came from (built in, or a package).
   Each plugin refreshes its own plan info: a rescan asks the provider what the key really gets;
   while it runs, each plugin it has still to ask says "scanning" in place of its state.
   A plugin with several feeds (Alpaca: alpaca, and its nights on Blue Ocean, alpaca (blue ocean);
   Tiingo: tiingo and tiingo (forex)) lists them, and names the feed on each capability, scan answer
   and era.
   A plan its scan cannot tell (Tiingo's Power from Commercial) is chosen here: each plan variable
   is a choice, saved like the key script saves it (POST /api/plugins/plan); keys stay in the script.
   Below them the publishers (news and publications: publications.py), each an agency's or a
   vendor's plugin: its feeds with their last and next reads, its release schedule, its scrape
   checks, its limits as last verified with a Recheck, and a Pause.
   Asked again every minute while shown, every few seconds while a rescan or recheck runs. */
(() => {
  const box = document.getElementById('viewPlugins');
  const { esc, safeUrl } = window.MassiveIcons;
  let data = null, sel = null, note = '', timer = null;

  const STATE = { ready: 'ready', scanning: 'scanning…', partly: 'keys not set', 'no key': 'no key', killed: 'killed', paused: 'paused',
                  refused: 'refused', failing: 'failing', waiting: 'waiting', off: 'not set up' };
  const FEED_STATE = { ok: 'read', unchanged: 'unchanged', waiting: 'waiting', failed: 'failed', refused: 'refused',
                       'not asked yet': 'not asked yet' };
  const PUB = 'pub:';  // a publisher's id in the list (its name may be a feed's too)
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
  const publisher = () => (sel?.startsWith(PUB) ? data?.publishers?.find((p) => PUB + p.name === sel) || null : null);
  const etTime = (iso) => new Date(iso).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit' });
  const etDay = (iso) => new Date(iso).toLocaleDateString('en-US', { timeZone: 'America/New_York', weekday: 'short', month: 'short', day: 'numeric' });
  const until = (iso) => {
    if (!iso) return '–';
    const m = (Date.parse(iso) - Date.now()) / 6e4;
    return m <= 1 ? 'now' : m < 90 ? `in ${Math.round(m)} min` : m < 48 * 60 ? `in ${(m / 60).toFixed(1)} h` : `in ${Math.round(m / 1440)} days`;
  };
  const every = (m) => (m >= 60 ? `${num(m / 60)} h` : `${num(m)} min`);
  const tierOf = (p) => p.plan.vars.find((v) => v.choices.length) || null;
  // a plugin's version, the plugin API it was written for and where it came from
  const release = (p) => `<div class="p-muted small pl-release">Version ${esc(p.version || 'unversioned')} · plugin API ${esc(p.api)} · ${esc(p.origin)}</div>`;

  async function load() {
    clearTimeout(timer);
    if (document.body.dataset.view !== 'plugins' || document.hidden) return;
    try {
      data = await api('/api/plugins');
      if (!sel || !(data.plugins.some((p) => p.name === sel) || publisher())) sel = data.plugins[0]?.name || null;
    } catch (err) { showError(err); }
    render();
    timer = setTimeout(load, data?.running || data?.rechecking ? 4_000 : 60_000);
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

  async function choosePlan(select) {
    const p = data.plugins.find((x) => x.name === select.dataset.planPlugin);
    const variable = select.dataset.var, value = select.value || null;
    const ask = value
      ? `Use ${variable}=${value}? ${p.title} will keep to that plan's limits, in the app and the sync: choose only the plan you pay for.`
      : `Let ${p.title}'s scan or its default decide ${variable} again?`;
    if (!window.confirm(ask)) { render(); return; }
    note = '';
    try {
      const res = await fetch('/api/plugins/plan', { method: 'POST', headers: WRITE_HEADERS,
        body: JSON.stringify({ plugin: p.name, variable, value }) });
      const out = await res.json().catch(() => ({}));
      note = res.ok ? `${variable} ${value ? `set to ${value}` : 'left to its scan or default'}: the app uses it now, the sync from its next run.`
        : out.error || res.statusText;
    } catch (err) { note = String(err.message || err); }
    await load();
  }

  async function post(path, done) {
    note = '';
    try {
      const res = await fetch(path, { method: 'POST', headers: WRITE_HEADERS, body: '{}' });
      const out = await res.json().catch(() => ({}));
      note = res.ok ? done : out.error || res.statusText;
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
          <span class="ex-count">${esc(plan)}${p.feeds.length > 1 ? ` · ${p.feeds.length} feeds` : ''}</span></button>`;
      }).join('');
    }).join('');
    const pubs = (data.publishers || []).map((p) => `<button type="button" class="ex-venue${PUB + p.name === sel ? ' active' : ''}" data-plugin="${esc(PUB + p.name)}" title="${esc(p.about)}">
          <span class="ex-name">${esc(p.title)}</span>${chip(p.state)}
          <span class="ex-count">${esc(p.kind === 'vendor' ? `on ${p.account}'s key` : `${p.feeds.length} feed${p.feeds.length === 1 ? '' : 's'}`)} · every ${esc(every(Math.min(...p.feeds.map((f) => f.every))))}</span></button>`).join('');
    const news = pubs ? `<h4 class="ex-group">News and publications</h4>${pubs}` : '';
    const left = data.errors.length ? `<h4 class="ex-group">Left out</h4>${data.errors.map((e) => `
      <div class="pl-error"><b>${esc(e.where)}</b><div class="p-muted small">${esc(e.why)}</div></div>`).join('')}` : '';
    return groups + news + left;
  }

  // -- middle: the plugin picked -------------------------------------------------------------
  // a plan variable with choices is a choice: "automatic" leaves it to the scan or the default
  function planCell(p, v) {
    if (!v.choices.length) return `<b>${esc(v.value ?? '–')}</b>`;
    const found = p.plan.found?.[v.name];
    const auto = `automatic: ${found || v.choices[0]} (${found ? 'its scan' : 'the default'})`;
    const mine = v.origin === 'you';
    return `<select class="pl-choose" data-plan-plugin="${esc(p.name)}" data-var="${esc(v.name)}"${data.offline
      ? ' disabled title="This offline copy of the app saves no settings"' : ''}>
        <option value=""${mine ? '' : ' selected'}>${esc(auto)}</option>
        ${v.choices.map((c) => `<option value="${esc(c)}"${mine && v.value === c ? ' selected' : ''}>${esc(c)}</option>`).join('')}
      </select>`;
  }

  function planHtml(p) {
    const name = p.name;
    const rows = p.plan.vars.map((v) => `<tr><td><code>${esc(v.name)}</code></td><td>${planCell(p, v)}</td>
        <td class="pl-origin pl-${esc(v.origin)}">${esc(ORIGIN[v.origin])}</td></tr>`).join('');
    const differs = p.plan.differs.map((d) => `<div class="pl-warn">Your <code>${esc(d.name)}</code> is <b>${esc(d.yours)}</b>,
        but its scan found <b>${esc(d.found)}</b>. Yours is used; change it if the scan is right.</div>`).join('');
    const found = Object.entries(p.plan.found || {});
    return `<h4>Plan</h4>
      ${rows ? `<table class="act-table ex-mini pl-plan"><thead><tr><th>Variable</th><th>Plan</th><th>From</th></tr></thead><tbody>${rows}</tbody></table>`
        : `<div class="p-muted small">One fixed plan: ${esc(p.plan.tier)}.</div>`}
      ${found.length ? `<div class="p-muted small">Its last scan found ${found.map(([k, v]) => `<code>${esc(k)}=${esc(v)}</code>${
        p.plan.alike?.[k]?.length ? ` (or ${p.plan.alike[k].map(esc).join(', ')}: their answers are the same, so the lower limits are used)` : ''}`).join(', ')}:
        used wherever you have not set it yourself.</div>` : ''}
      ${differs}
      ${p.plan.note ? `<div class="pl-note">${esc(p.plan.note)}.</div>` : ''}
      ${rows ? `<div class="p-muted small">Choose a plan above: it is saved as your user environment variable. Keys are set
        with the key script: <code>${esc(data.keyScript)} ${esc(name)}</code></div>` : ''}`;
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

  // a plugin with several feeds (alpaca, and alpaca (blue ocean) on its keys): each listed by the
  // plugin's name, with its state ("the source isn't blue ocean its alpaca w/ blue ocean")
  const several = (p) => p.feeds.length > 1;
  function feedsHtml(p) {
    if (!several(p)) return '';
    return `<h4>Feeds</h4><table class="act-table ex-mini pl-feedlist"><tbody>${p.feeds.map((f) => `<tr>
        <td><b>${esc(f.label)}</b><div class="p-muted small">${esc(f.about)}</div></td>
        <td>${chip(f.state)}</td></tr>`).join('')}</tbody></table>
      <div class="p-muted small">Each a feed of its own (its own bars, symbols and requests), on this plugin's keys and limits.</div>`;
  }

  function capabilitiesHtml(p) {
    if (!p.capabilities.length) return '';
    const feed = (c) => (several(p) ? `<td>${esc(c.feed)}</td>` : '');
    return `<h4>What it can do on this plan</h4><table class="act-table ex-mini"><thead><tr>${several(p) ? '<th>Feed</th>' : ''}<th>Asset</th><th>Bars</th>
        <th>Market</th><th>From</th><th>Fresh</th><th class="num">A request</th></tr></thead><tbody>
      ${p.capabilities.map((c) => `<tr>${feed(c)}<td>${esc(c.asset)}</td><td>${esc(JOBS[c.resolution] || c.resolution)}</td><td>${esc(c.market)}</td>
        <td>${esc(c.since)}</td><td>${esc(c.fresh)}</td><td class="num">${c.perRequest == null ? 'whole history' : `${num(c.perRequest)} bars`}</td></tr>`).join('')}
      </tbody></table>
      ${p.streams.length ? `<h4>Live</h4>${p.streams.map((s) => `<div class="small"><b>${esc(s.asset)}</b> ${esc(s.what)}</div>`).join('')}` : ''}`;
  }

  function scanHtml(p) {
    if (!p.scan.has) return `<h4>Capability scan</h4><div class="p-muted small">${esc(p.title)} has none: a public feed with one fixed plan.</div>`;
    const can = p.configured && p.state !== 'no key' && !data.offline && !data.running;
    const why = data.offline ? 'This offline copy of the app sends no requests' : !p.configured || p.state === 'no key'
      ? 'Its keys are not set' : data.running ? 'A rescan is running' : 'Ask the provider again what the key gets (a few requests)';
    const rows = p.scan.results.map((r, i) => `${several(p) && r.feed !== p.scan.results[i - 1]?.feed
        ? `<tr class="pl-feedrow"><th colspan="3">${esc(r.feed)}</th></tr>` : ''}<tr><td>${esc(r.what)}</td>
        <td>${r.available ? '<span class="up">yes</span>' : '<span class="down">no</span>'}</td><td class="p-muted">${esc(r.detail)}</td></tr>`).join('');
    return `<h4 class="pl-scan-head"><span>Capability scan</span>
        <button type="button" data-rescan="${esc(p.name)}"${can ? '' : ' disabled'} title="${esc(why)}">${p.scan.running ? 'Scanning…' : 'Rescan'}</button></h4>
      <div class="p-muted small">${p.scan.at ? `Last asked ${ago(p.scan.at)}` : 'Not scanned yet'}: on each start of the app, weekly in the sync, and here${
        several(p) ? ` (its ${p.feeds.length} feeds together)` : ''}.</div>
      ${rows ? `<table class="act-table ex-mini"><tbody>${rows}</tbody></table>` : ''}`;
  }

  // -- a publisher (news and publications) ------------------------------------------------------
  function pubMainHtml(p) {
    const feeds = p.feeds.map((f) => `<tr><td><b>${esc(f.title)}</b><div class="p-muted small">${esc(f.host || f.agency)}</div></td>
        <td>${esc(every(f.every))}</td><td><span class="pl-fstate pl-f-${esc(f.state.replace(/ /g, '-'))}">${esc(FEED_STATE[f.state] || f.state)}</span></td>
        <td>${f.at ? ago(f.at) : 'never'}</td><td>${p.state === 'paused' ? 'paused' : f.next ? until(f.next) : '–'}</td>
        <td class="p-muted pl-msg">${esc(f.message || '')}</td></tr>`).join('');
    const moments = p.moments.length ? `<div class="small">Today's release reads (ET): ${p.moments.map((m) => esc(etTime(m))).join(' · ')}</div>` : '';
    const cal = p.calendar.length ? `<table class="act-table ex-mini"><tbody>${p.calendar.map((c) => `<tr><td>${esc(etDay(c.when))}</td>
        <td>${esc(etTime(c.when))} ET</td><td>${esc(c.what)}</td></tr>`).join('')}</tbody></table>` : '';
    const pol = p.policy;
    const robots = p.robots.map((r) => `<div class="small"><code>${esc(r.host)}</code> robots.txt: ${r.status == null ? 'not read yet'
      : r.status === 200 ? `read ${ago(r.at)}${r.delayMinutes ? `, asks ${num(r.delayMinutes)} min between requests` : ''}`
      : r.answered ? `none published to this reader (HTTP ${r.status}), read as allowing all · ${ago(r.at)}`
      : `did not answer (${r.status ? `HTTP ${r.status}` : 'no answer'}) ${ago(r.at)}: nothing read from the host until it does`}${r.allows === false ? ' · <span class="down">disallows a feed: not read</span>' : ''}</div>`).join('');
    const checks = [
      pol.hosts.length ? `Only ${pol.hosts.map((h) => `<code>${esc(h)}</code>`).join(', ')}, over HTTPS; a redirect elsewhere is not followed` : `Through ${esc(p.account)}'s client: its rate window, daily cap and kills`,
      `Never these sites, whose terms bar programs: ${pol.blocked.map(esc).join(', ')}`,
      'Feeds and APIs only: an answer that is not XML or JSON is refused, never read as a page',
      `A feed at most every ${esc(every(p.floorMinutes))}${p.floorMinutes > pol.minMinutes ? ` (its hosts ask for that; its own is ${esc(every(pol.minMinutes))})` : ''}, whatever its schedule asks`,
      `At most ${num(pol.perDay)} requests a day, robots.txt included: ${num(p.requestsToday)} today`,
      'Conditional requests (an unchanged feed sends nothing back); a refusal (401, 403, 429) holds the whole publisher off 6 h, or the Retry-After if longer; a Retry-After on any answer is honoured',
      'A crawl-delay its robots.txt asks for is kept between any two requests to that host; a feed that keeps failing is asked less and less often, never more than its schedule',
      pol.contact ? (pol.contactSet ? 'Your GOV_CONTACT goes in its User-Agent, as it asks (to it only)' : 'It asks automated readers to identify: waiting for GOV_CONTACT (your name and email)')
        : 'No contact sent: it does not ask for one',
    ].map((c) => `<li>${c}</li>`).join('');
    const rc = p.recheck;
    const seen = rc?.limitsSeen ? Object.entries(rc.limitsSeen) : [];
    const acct = rc?.account ? `<div class="small">${esc(rc.account.title)}'s limits: ${esc(rate(rc.account.rate))}, ${num(rc.account.perDay)} a day${rc.account.published ? ` (its server: ${esc(rc.account.published)})` : ''}</div>` : '';
    const busy = data.rechecking?.name === p.name;
    const waitingContact = p.policy.contact && !p.policy.contactSet;
    const can = !data.offline && !data.rechecking && !['off', 'paused', 'refused'].includes(p.state) && !p.held && !waitingContact;
    const why = data.offline ? 'This offline copy of the app sends no requests'
      : waitingContact ? 'Nothing is sent to it until GOV_CONTACT is set'
      : p.state === 'paused' || p.state === 'refused' || p.held ? 'Nothing is sent to it while it is paused, stopped or held'
      : 'Read its robots.txt again (unless read within the hour) and ask each feed once (never within its floor, refused or held), keeping the rate-limit headers its servers send; pressed again within 15 min it sends nothing';
    const err = p.error ? `<div class="pl-warn">Its plugin failed here: ${esc(p.error)}</div>` : '';
    return `${err}<h4>Feeds</h4><table class="act-table ex-mini pl-feeds"><thead><tr><th>Feed</th><th>Every</th><th>State</th><th>Last read</th><th>Next</th><th></th></tr></thead>
        <tbody>${feeds}</tbody></table>
      ${moments || cal ? `<h4>Schedule</h4>${moments}${cal}` : ''}
      <h4>Scrape checks</h4><ul class="pl-checks small">${checks}</ul>${robots}
      <h4 class="pl-scan-head"><span>Limits, verified</span>
        <button type="button" data-recheck="${esc(p.name)}"${can ? '' : ' disabled'} title="${esc(why)}">${busy ? 'Rechecking…' : 'Recheck'}</button></h4>
      <div class="p-muted small">${rc ? `Last verified ${ago(rc.at)}` : 'Not verified yet'}: weekly, and here.</div>
      ${acct}${(rc?.notes || []).map((n) => `<div class="small">${esc(n)}</div>`).join('')}
      ${seen.length ? `<table class="act-table ex-mini"><tbody>${seen.map(([k, h]) => `<tr><td>${esc(k)}</td><td class="p-muted">${
        esc(Object.entries(h).filter(([n]) => n !== 'at').map(([n, v]) => `${n}: ${v}`).join(' · '))}</td></tr>`).join('')}</tbody></table>` : ''}`;
  }

  function pubSideHtml(p) {
    const whose = p.kill?.whose;
    const paused = p.state === 'paused' && whose === 'own';  // its own pause: Resume undoes it here
    const until = (iso) => esc(String(iso || '').slice(0, 16).replace('T', ' '));
    const stop = !p.kill ? ''
      : paused ? 'Paused by you'
      : whose === 'fetching' ? `${esc(p.kill.why)}: resume fetching there`
      : whose === 'account' ? `Stopped with ${esc(p.account)}'s account until ${until(p.kill.until)} UTC: ${esc(p.kill.why)}`
      : `Stopped until ${until(p.kill.until)} UTC: ${esc(p.kill.why)}`;
    const used = p.policy.perDay ? Math.min(100, (p.requestsToday / p.policy.perDay) * 100) : 0;
    return `<h3>${esc(p.title)} ${chip(p.state)}</h3>
      <div class="p-muted small">${esc(p.about)}</div>${release(p)}
      ${stop ? `<div class="pl-warn">${stop}</div>` : ''}
      ${p.held ? `<div class="pl-warn">Nothing sent until ${until(p.held.until)} UTC: ${esc(p.held.why)}</div>` : ''}
      <div class="tiles ex-tiles">
        ${tile('Requests today', num(p.requestsToday), `of ${num(p.policy.perDay)} a day`)}
        ${tile('Floor', every(p.floorMinutes), 'between reads of a feed')}
        ${tile('Feeds', num(p.feeds.length), p.kind === 'vendor' ? `on ${esc(p.account)}'s key` : 'official, keyless')}
        ${tile('Verified', p.recheck ? ago(p.recheck.at) : 'never', 'its limits')}
      </div>
      <div class="pl-usage" title="${num(p.requestsToday)} of ${num(p.policy.perDay)} requests today"><i style="width:${used.toFixed(1)}%"></i></div>
      <h4>Reading</h4>
      <button type="button" data-pause="${esc(p.name)}" data-on="${paused ? '0' : '1'}"${data.offline || (p.kill && !paused) ? ' disabled' : ''}>${paused ? 'Resume' : 'Pause'}</button>
      <div class="p-muted small">${paused ? 'Nothing is read until you resume it.' : 'Pausing stops its reads until you resume it.'}</div>
      ${p.policy.terms ? `<h4>Terms</h4><div class="small">${esc(p.policy.terms)}</div>` : ''}
      ${safeUrl(p.policy.termsUrl) ? `<div class="p-muted small"><a href="${esc(safeUrl(p.policy.termsUrl))}" target="_blank" rel="noopener noreferrer">${esc(p.policy.termsUrl.replace(/^https:\/\//, ''))}</a></div>` : ''}`;
  }

  function mainHtml() {
    const pub = publisher();
    if (pub) return pubMainHtml(pub);
    const p = plugin();
    if (!p) return '<div class="empty">No plugin found.</div>';
    return `${feedsHtml(p)}${planHtml(p)}${limitsHtml(p)}${capabilitiesHtml(p)}${scanHtml(p)}
      ${p.notes.length ? `<h4>Notes</h4>${p.notes.map((n) => `<div class="small">${esc(n)}</div>`).join('')}` : ''}`;
  }

  // -- right: its part in the delivery scheme, its usage, its keys ---------------------------
  function sideHtml() {
    const pub = publisher();
    if (pub) return pubSideHtml(pub);
    const p = plugin();
    if (!p) return '';
    const u = p.usage;
    const used = u.perDay ? Math.min(100, (u.today / u.perDay) * 100) : 0;
    const first = p.delivery.filter((e) => e.first), standby = p.delivery.filter((e) => !e.first);
    const era = (e) => `<tr><td>${esc(JOBS[e.job] || e.job)}</td><td>${esc(e.asset)}${several(p) ? `<div class="p-muted">${esc(e.feed)}</div>` : ''}</td><td>${esc(e.start)} → ${esc(e.end || 'now')}</td>
        <td class="p-muted">${e.unique ? 'only source' : e.first ? `then ${esc(e.others.join(', '))}` : `after ${esc(e.others[0] || '')}`}</td></tr>`;
    const keys = p.keys.length ? p.keys.map((k) => `<div class="small"><code>${esc(k.name)}</code> ${k.set ? '<span class="up">set</span>' : '<span class="down">not set</span>'}</div>`).join('')
      : '<div class="p-muted small">Needs no key.</div>';
    return `<h3>${esc(p.title)} ${chip(p.state)}</h3>
      <div class="p-muted small">${esc(p.about)}</div>${release(p)}
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
    const p = plugin(), pub = publisher();
    const scrolled = box.querySelector('.ex-list')?.scrollTop || 0;
    const setUp = data.plugins.filter((x) => x.configured && x.state !== 'no key').length;
    const head = pub ? `publisher · ${pub.kind === 'vendor' ? `a vendor's headlines, on ${pub.account}'s key` : 'official feeds'} · ${pub.feeds.length} feed${pub.feeds.length === 1 ? '' : 's'}`
      : p ? `${p.archive ? 'closed archive' : 'feed'} · ${p.kind} data · ${p.assets.join(', ')}` : '';
    box.innerHTML = `
      <div class="ex-venues"><div class="view-head"><div><h2>Plugins</h2>
        <div class="p-muted small">${num(data.plugins.length)} providers · ${num(setUp)} set up · ${num((data.publishers || []).length)} publishers · plugin API ${esc(data.api)}</div></div></div>
        <button type="button" class="pl-all" data-rescan="*"${data.offline || data.running ? ' disabled' : ''}
          title="${data.offline ? 'This offline copy of the app sends no requests' : 'Every plugin set up here asks its provider again (a few requests each)'}">${data.running ? 'Scanning…' : 'Rescan all'}</button>
        ${note ? `<div class="p-muted small pl-note-line">${esc(note)}</div>` : ''}
        ${listHtml()}</div>
      <div class="ex-list"><div class="view-head"><div><h2>${esc(pub?.title || p?.title || '')}</h2>
        <div class="p-muted small">${esc(head)}</div></div></div>${mainHtml()}</div>
      <aside class="ex-side">${sideHtml()}</aside>`;
    box.querySelector('.ex-list').scrollTop = scrolled;
  }

  box.addEventListener('change', (e) => {
    if (e.target.matches('select.pl-choose')) choosePlan(e.target);
  });
  box.addEventListener('click', (e) => {
    const t = e.target.closest('[data-plugin],[data-rescan],[data-recheck],[data-pause]');
    if (!t || t.disabled) return;
    if (t.dataset.plugin && t.dataset.plugin !== sel) {
      sel = t.dataset.plugin; render();
    } else if (t.dataset.rescan) {
      rescan(t.dataset.rescan === '*' ? null : t.dataset.rescan);
    } else if (t.dataset.recheck) {
      post(`/api/plugins/recheck?name=${encodeURIComponent(t.dataset.recheck)}`, `Recheck started: ${publisher()?.title || t.dataset.recheck}.`);
    } else if (t.dataset.pause) {
      const on = t.dataset.on === '1';
      post(`/api/plugins/pause?name=${encodeURIComponent(t.dataset.pause)}&on=${on ? 1 : 0}`, on ? 'Paused.' : 'Resumed.');
    }
  });
  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'plugins') load(); else clearTimeout(timer); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  if (document.body.dataset.view === 'plugins') load();  // the page opened on this view (before this script listened)
})();
