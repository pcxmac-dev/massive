// Virtual level 2 (Analysis view): the volume and trades at each price in the chart's newest bar,
// with the bar's and its day's range and the last price; with a highlight drawn, across the
// highlight's bars instead, with its major prices marked: the point of control (the most traded
// price), the value area holding 70% of the volume (its high and low) and the other high-volume
// nodes. It is not an order book: it shows what changed hands, not what was resting. Prices summed into
// segments (every $0.10, $1 ...) or estimated from bars are derived data: each carries an
// asterisk, and the panel says how they were derived.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = window.MassiveIcons.esc;
  const STEPS = ['auto', '0.01', '0.05', '0.1', '0.25', '0.5', '1', '5', '10', '50', '100', '500'];
  const LIVE_MS = 3000;   // the newest bar of a live symbol is read again this often
  const IDLE_MS = 30000;  // otherwise
  let step = STEPS.includes(prefs.get('l2Step', 'auto')) ? prefs.get('l2Step', 'auto') : 'auto';
  let at = null;  // the bar shown (its timestamp); null: the newest
  let cursorPrice = null;  // the price under the cursor on the price pane: its row is marked (null: off it)
  let lastProfile = null;
  let token = 0, debounce = 0, lastLoad = 0, shownFor = '';

  const shown = () => document.body.dataset.pane === 'analysis' && !document.body.classList.contains('panel-hidden');
  const key = () => `${state.ticker}|${state.source}|${state.period?.type}|${state.period?.span}|${state.session}|${state.adjust}`;
  const hlKey = () => { const h = highlightSpan(); return h ? `${h.from}-${h.to}` : ''; };  // the highlight's bars

  function schedule(ms = 200) {
    clearTimeout(debounce);
    debounce = setTimeout(load, ms);
  }

  async function load() {
    if (!shown() || !state.ticker || !state.period) return;
    const my = ++token;
    lastLoad = Date.now();
    const hl = highlightSpan();
    try {
      const res = await api('/api/profile', {
        source: state.source, ticker: state.ticker, period: state.period.type, span: state.period.span,
        session: state.session, adjust: state.adjust,
        ...(hl ? { from: hl.from, to: hl.to } : at != null ? { at } : {}), ...(step !== 'auto' ? { step } : {}),
      });
      if (my === token) render(res);
    } catch (err) {
      if (my === token) $('pLevel2').innerHTML = `<h3>Virtual level 2</h3><div class="p-muted">${esc(err.message || err)}</div>`;
    }
  }

  const fmtTrades = (n) => (Number.isInteger(n) ? Number(n).toLocaleString() : `≈${n.toFixed(1)}`);
  // Volumes from thousands of shares down to fractions of a coin.
  const fmtVol = (v) => (v >= 1000 ? big(v) : v >= 1 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 })
    : v > 0 ? Number(v.toPrecision(3)).toString() : '0');
  const money = (s) => (s === 'auto' ? 'auto' : `$${Number(s) < 1 ? Number(s).toFixed(2) : s}`);

  function when(p) {
    const intraday = ['second', 'minute', 'hour'].includes(p.period);
    const f = new Intl.DateTimeFormat('en-US', intraday
      ? { timeZone: TZ, hour: '2-digit', minute: '2-digit', ...(p.period === 'second' ? { second: '2-digit' } : {}), hourCycle: 'h23' }
      : { timeZone: ['crypto', 'fx'].includes(state.meta?.asset) ? 'UTC' : TZ, month: 'short', day: 'numeric', year: 'numeric' });
    return intraday ? `${f.format(new Date(p.bar.from))}–${f.format(new Date(p.bar.to))} ET` : f.format(new Date(p.bar.from));
  }

  // The highlight's stretch: "Oct 14 09:30 → 16:00 ET", "Oct 14 09:30 → Oct 16 16:00 ET", "Oct 1, 2026 → Oct 16, 2026".
  function stretch(p) {
    const intraday = ['second', 'minute', 'hour'].includes(p.period);
    const day = new Intl.DateTimeFormat('en-US', { timeZone: !intraday && ['crypto', 'fx'].includes(state.meta?.asset) ? 'UTC' : TZ,
      month: 'short', day: 'numeric', ...(intraday ? {} : { year: 'numeric' }) });
    const a = new Date(p.range.from), b = new Date(p.range.to);
    if (!intraday) return `${day.format(a)} → ${day.format(new Date(p.range.to - 1))}`;
    const time = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    return `${day.format(a)} ${time.format(a)} → ${day.format(a) === day.format(b) ? '' : `${day.format(b)} `}${time.format(b)} ET`;
  }

  // The major prices' marks on a row: its classes and tags (the point of control, the value area's
  // high and low, a high-volume node; inside the value area shaded).
  function marksOf(r, m) {
    if (m?.poc == null) return { cls: [], tags: [] };
    const near = (x) => Math.abs(r.price - x) < 1e-9;
    const cls = [], tags = [];
    if (r.price >= m.val - 1e-9 && r.price <= m.vah + 1e-9) cls.push('va');
    if (near(m.poc)) { cls.push('poc'); tags.push('POC'); }
    if (near(m.vah)) { cls.push('vah'); tags.push('VAH'); }
    if (near(m.val)) { cls.push('val'); tags.push('VAL'); }
    if (m.nodes?.some(near)) { cls.push('hvn'); tags.push('HVN'); }
    return { cls, tags };
  }

  function render(p) {
    const box = $('pLevel2');
    const select = `<select id="l2Step" title="Price segment: rows sum the volume in each (derived)">${STEPS.map((s) =>
      `<option value="${s}"${s === step ? ' selected' : ''}>${money(s)}</option>`).join('')}</select>`;
    const range = p.range;  // a highlight's stretch rather than one bar
    if (!p.rows?.length) {
      box.innerHTML = `<h3>Virtual level 2 ${select}</h3><div class="p-muted">Nothing traded in ${range ? 'the highlight' : 'this bar'}.</div>`;
      lastProfile = null;
      return;
    }
    const derived = !!p.derived;
    const star = derived ? '<sup>*</sup>' : '';
    const ref = range ? range.close : p.newest ? p.last : p.bar.close;
    const m = range ? p.marks : null;
    let maxV = 0;
    for (const r of p.rows) maxV = Math.max(maxV, r.volume);
    // The row holding the reference price: its own price, or the segment it falls in.
    const inRow = (r) => (p.step ? ref >= r.price - 1e-9 && ref < r.price + p.step - 1e-9 : Math.abs(r.price - ref) < 1e-9);
    const tip = derived ? `derived: ${p.derived} (not level 2 data)` : 'as traded';
    const d = p.day;
    const id = `${key()}|${at}|${step}|${range ? `${range.from}-${range.to}` : ''}`;
    const kept = shownFor === id ? box.querySelector('.l2-wrap')?.scrollTop : null;  // a refresh keeps the scroll
    const bars = (n) => `${n.toLocaleString()} ${p.period === 'day' && p.span === 1 ? 'daily' : `${p.span}-${p.period}`} bar${n === 1 ? '' : 's'}`;
    box.innerHTML = `
      <h3>Virtual level 2${star} ${select}</h3>
      <div class="p-muted small">${range
        ? `Highlight · ${esc(bars(range.bars))} · ${esc(stretch(p))}`
        : `${esc(p.period === 'day' ? 'Day' : `${p.span} ${p.period}`)} bar · ${esc(when(p))}${p.newest ? ' · newest' : ''}`}</div>
      <div class="p-muted small l2-cursor"></div>
      ${kv(range ? [
        ['Open → close', `${priceText(range.open)} → ${priceText(range.close)}`],
        ['High · low', `${priceText(range.high)} · ${priceText(range.low)}`],
        ['Volume', `${fmtVol(range.volume)} · ${fmtTrades(range.trades)} trade${range.trades === 1 ? '' : 's'}`],
        m?.poc != null && [`Point of control${derived ? '*' : ''}`, priceText(m.poc)],
        m?.poc != null && [`Value area${derived ? '*' : ''} (${Math.round(m.valueArea * 100)}%)`, `${priceText(m.val)} – ${priceText(m.vah)}`],
        m?.poc != null && [`Volume nodes${derived ? '*' : ''}`, m.nodes.length ? m.nodes.map(priceText).join(' · ') : 'none besides the POC'],
      ] : [
        [p.newest ? 'Last' : 'Close', priceText(ref)],
        ['Bar high · low', `${priceText(p.bar.high)} · ${priceText(p.bar.low)}`],
        d && ['Day high · low', `${priceText(d.high)} · ${priceText(d.low)}`],
        ['Bar volume', `${fmtVol(p.bar.volume)} · ${fmtTrades(p.bar.trades)} trade${p.bar.trades === 1 ? '' : 's'}`],
      ])}
      <div class="table-wrap l2-wrap"><table class="l2">
        <thead><tr><th>Price${star}</th><th>Volume</th><th class="num">Trades</th></tr></thead>
        <tbody>${p.rows.map((r) => { const k = marksOf(r, m); return `<tr class="${[inRow(r) ? 'at' : '', ...k.cls].join(' ').trim()}">
          <td title="${esc(tip)}">${priceText(r.price)}${star}${k.tags.map((t) => `<span class="l2-tag ${t.toLowerCase()}">${t}</span>`).join('')}</td>
          <td class="l2-vol"><span class="l2-bar" style="width:${maxV ? Math.max(1, Math.round(r.volume / maxV * 100)) : 0}%"></span><span>${fmtVol(r.volume)}</span></td>
          <td class="num">${fmtTrades(r.trades)}</td></tr>`; }).join('')}</tbody></table></div>
      ${m?.poc != null ? `<div class="p-sub l2-note">POC: the most traded price. VAH–VAL: the value area around it holding ${Math.round(m.valueArea * 100)}% of the volume (shaded). HVN: another peak with at least half the POC's volume.</div>` : ''}
      <div class="p-sub l2-note">${esc(p.note)}</div>
      ${derived ? `<div class="p-sub l2-note">* Derived data, not actual level 2 data: ${esc(p.derived)}.</div>` : ''}
      <div class="p-sub l2-note">From ${esc(p.basis)}.</div>`;
    const wrap = box.querySelector('.l2-wrap'), row = box.querySelector(range ? 'tr.poc' : 'tr.at');
    if (kept != null) wrap.scrollTop = kept;
    else if (row) wrap.scrollTop = row.offsetTop - wrap.clientHeight / 2;  // a new bar: its price (a highlight: its POC) in the middle
    shownFor = id;
    lastProfile = p;
    markCursor();
  }

  $('pLevel2').addEventListener('change', (e) => {
    if (e.target.id !== 'l2Step') return;
    step = e.target.value;
    prefs.set('l2Step', step);
    load();
  });
  // The ladder follows the cursor's price, not its time: the row at the price under the cursor is
  // marked and kept in view (the edge row nearest it when it is beyond what traded in the bar).
  let markFrame = 0;
  function followCursor(c) {
    let price = null;
    if (c?.paneId === 'candle_pane' && Number.isFinite(c.y)) {
      const [pt] = chart.convertFromPixel([{ x: c.x, y: c.y }], { paneId: 'candle_pane' });
      price = Number.isFinite(pt?.value) ? pt.value : null;
    }
    if (price === cursorPrice) return;
    cursorPrice = price;
    if (!markFrame) markFrame = requestAnimationFrame(() => { markFrame = 0; markCursor(); });
  }
  chart.subscribeAction('onCrosshairChange', followCursor);
  $('chart').addEventListener('mouseleave', () => { cursorPrice = null; markCursor(); });

  function markCursor() {
    const box = $('pLevel2'), p = lastProfile;
    box.querySelectorAll('tr.cursor').forEach((tr) => tr.classList.remove('cursor', 'beyond'));
    const label = box.querySelector('.l2-cursor');
    if (!p?.rows?.length || cursorPrice == null) { if (label) label.textContent = ''; return; }
    const step = p.step || 0;
    let best = 0;  // the row holding the price (its segment, or the exact price nearest it)
    p.rows.forEach((r, i) => {
      const d = Math.abs(r.price + step / 2 - cursorPrice), b = Math.abs(p.rows[best].price + step / 2 - cursorPrice);
      if (d < b) best = i;
    });
    const top = p.rows[0].price + step, bottom = p.rows[p.rows.length - 1].price;
    const beyond = cursorPrice > top + 1e-9 ? 'above' : cursorPrice < bottom - 1e-9 ? 'below' : '';
    const tr = box.querySelectorAll('tbody tr')[best];
    if (!tr) return;
    tr.classList.add('cursor');
    if (beyond) tr.classList.add('beyond');
    if (label) label.textContent = `Cursor ${priceText(cursorPrice)}${beyond ? ` · ${beyond} what traded in ${p.range ? 'the highlight' : 'this bar'}` : ''}`;
    const wrap = box.querySelector('.l2-wrap'), head = wrap.querySelector('thead')?.offsetHeight || 0;
    if (tr.offsetTop < wrap.scrollTop + head || tr.offsetTop + tr.offsetHeight > wrap.scrollTop + wrap.clientHeight) {
      wrap.scrollTop = tr.offsetTop - wrap.clientHeight / 2;
    }
  }
  window.addEventListener('massive:symbol', () => { at = null; schedule(50); });
  window.addEventListener('massive:view', (e) => { if (e.detail?.pane === 'analysis') schedule(50); });
  let lastKey = '', lastHl = '';
  // A highlight drawn, moved or taken off (app.js scheduleHighlightPanel): read once it settles.
  window.addEventListener('massive:highlight', () => { if (shown() && hlKey() !== lastHl) { lastHl = hlKey(); schedule(300); } });
  setInterval(() => {  // follows interval changes, the highlight, and the newest bar as it grows
    if (!shown()) return;
    if (key() !== lastKey) { lastKey = key(); lastHl = hlKey(); at = null; load(); return; }
    if (hlKey() !== lastHl) { lastHl = hlKey(); schedule(300); return; }
    const since = Date.now() - lastLoad;
    // a highlight's bars change only while it reaches the newest bar of a live symbol
    if (lastHl ? isLive() && since >= IDLE_MS : at == null && since >= (isLive() ? LIVE_MS : IDLE_MS)) load();
  }, 1000);
  // for inspection and checks: the profile shown, the cursor's price, and the cursor's handling
  window.Level2 = { get profile() { return lastProfile; }, get cursorPrice() { return cursorPrice; },
                    follow(c) { followCursor(c); markCursor(); } };
})();
