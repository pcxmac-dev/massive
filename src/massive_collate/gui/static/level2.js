// Virtual level 2 (Analysis view): the volume and trades at each price across the chart's bars in
// view, or across the highlight's bars when one is drawn, so the cursor's price, wherever it is on
// the chart, has a row of its own; with the stretch's major prices marked: the point of control
// (the most traded price), the value area holding 70% of the volume (its high and low) and the
// other high-volume nodes. Without bars to cover, the newest bar's, with its day's range and the
// last price. It is not an order book: it shows what changed hands, not what was resting. Prices summed into
// segments (every $0.10, $1 ...) or estimated from bars are derived data: each carries an
// asterisk, and the panel says how they were derived.
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = window.MassiveIcons.esc;
  const STEPS = ['auto', '0.01', '0.05', '0.1', '0.25', '0.5', '1', '5', '10', '50', '100', '500'];
  const LIVE_MS = 3000;   // the newest bar of a live symbol is read again this often
  const IDLE_MS = 30000;  // otherwise
  const COVER_LIVE_MS = 10000;  // the bars in view (or the highlight's) while they reach a live symbol's newest bar
  let step = STEPS.includes(prefs.get('l2Step', 'auto')) ? prefs.get('l2Step', 'auto') : 'auto';
  let at = null;  // the bar shown (its timestamp) when there are no bars to cover; null: the newest
  let cursorPrice = null;  // the price under the cursor on the price pane: its row is marked (null: off it)
  let lastProfile = null;
  let token = 0, debounce = 0, lastLoad = 0, shownFor = '';
  let busy = false, again = false;  // one request at a time: scrolling asks again once it is answered
  let lastCover = '';  // the bars last asked for (keyOf)

  const shown = () => document.body.dataset.pane === 'analysis' && !document.body.classList.contains('panel-hidden');
  const key = () => `${state.ticker}|${state.source}|${state.period?.type}|${state.period?.span}|${state.session}|${state.adjust}`;

  // The bars the ladder covers: the highlight's, else those in view; in a replay none it has not
  // closed yet. {from, to, over} (bar timestamps), or null without bars.
  function cover() {
    const hl = highlightSpan();
    if (hl) return { ...hl, over: 'highlight' };
    const data = chart.getDataList();
    if (!data.length) return null;
    const r = chart.getVisibleRange();
    const a = data[Math.max(0, r.from)], b = data[Math.min(data.length - 1, r.to - 1)];
    if (!a || !b) return null;
    const rp = state.replay;
    const to = rp ? Math.min(b.timestamp, rp.lastClosed ?? rp.start - 1) : b.timestamp;
    return to >= a.timestamp ? { from: a.timestamp, to, over: 'view' } : null;
  }
  const keyOf = (c) => (c ? `${c.over}:${c.from}-${c.to}` : '');
  const coverKey = () => keyOf(cover());
  const overText = (p) => (!p.range ? 'this bar' : p.over === 'view' ? 'the bars in view' : 'the highlight');
  // a stretch's bars change only while it reaches the newest bar of a live symbol
  const growing = (c) => { const d = chart.getDataList(); return isLive() && d.length > 0 && c.to >= d[d.length - 1].timestamp; };

  function schedule(ms = 200) {
    clearTimeout(debounce);
    debounce = setTimeout(load, ms);
  }

  async function load() {
    if (!shown() || !state.ticker || !state.period) return;
    if (busy) { again = true; return; }
    busy = true;
    const my = ++token;
    lastLoad = Date.now();
    const c = cover();
    lastCover = keyOf(c);
    try {
      const res = await api('/api/profile', {
        source: state.source, ticker: state.ticker, period: state.period.type, span: state.period.span,
        session: state.session, adjust: state.adjust,
        ...(c ? { from: c.from, to: c.to } : at != null ? { at } : {}), ...(step !== 'auto' ? { step } : {}),
      });
      if (my === token) render(c && res.range ? { ...res, over: c.over } : res);
    } catch (err) {
      if (my === token) $('pLevel2').innerHTML = `<h3>Virtual level 2</h3><div class="p-muted">${esc(err.message || err)}</div>`;
    } finally {
      busy = false;
      if (again) { again = false; schedule(0); }
    }
  }

  const fmtTrades = (n) => (Number.isInteger(n) ? Number(n).toLocaleString() : `≈${n.toFixed(1)}`);
  // Volumes from thousands of shares down to fractions of a coin.
  const fmtVol = (v) => (v >= 1000 ? big(v) : v >= 1 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 })
    : v > 0 ? Number(v.toPrecision(3)).toString() : '0');
  const money = (s) => (s === 'auto' ? 'auto' : `$${Number(s) < 1 ? Number(s).toFixed(2) : s}`);

  // Within a day, times in the zone picked under the price axis (app.js pickedZone), named as it is
  // now (EDT, PST, GMT+9), as the crosshair reads them; days are the market's own (New York, or UTC
  // for crypto and forex) whatever the pick.
  function when(p) {
    const intraday = ['second', 'minute', 'hour'].includes(p.period);
    const zone = pickedZone();
    const f = new Intl.DateTimeFormat('en-US', intraday
      ? { timeZone: zone, hour: '2-digit', minute: '2-digit', ...(p.period === 'second' ? { second: '2-digit' } : {}), hourCycle: 'h23' }
      : { timeZone: ['crypto', 'fx'].includes(state.meta?.asset) ? 'UTC' : TZ, month: 'short', day: 'numeric', year: 'numeric' });
    return intraday ? `${f.format(new Date(p.bar.from))}–${f.format(new Date(p.bar.to))} ${zoneShort(zone)}` : f.format(new Date(p.bar.from));
  }

  // A stretch's time: "Oct 14 09:30 → 16:00 EDT", "Oct 14 09:30 → Oct 16 16:00 EDT", "Oct 1, 2026 → Oct 16, 2026".
  function spanText(p) {
    const intraday = ['second', 'minute', 'hour'].includes(p.period);
    const zone = pickedZone();
    const day = new Intl.DateTimeFormat('en-US', { timeZone: intraday ? zone : ['crypto', 'fx'].includes(state.meta?.asset) ? 'UTC' : TZ,
      month: 'short', day: 'numeric', ...(intraday ? {} : { year: 'numeric' }) });
    const a = new Date(p.range.from), b = new Date(p.range.to);
    if (!intraday) return `${day.format(a)} → ${day.format(new Date(p.range.to - 1))}`;
    const time = new Intl.DateTimeFormat('en-US', { timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
    return `${day.format(a)} ${time.format(a)} → ${day.format(a) === day.format(b) ? '' : `${day.format(b)} `}${time.format(b)} ${zoneShort(zone)}`;
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
    const range = p.range;  // a stretch of bars (in view, or the highlight's) rather than one bar
    if (!p.rows?.length) {
      box.innerHTML = `<h3>Virtual level 2 ${select}</h3><div class="p-muted">Nothing traded in ${overText(p)}.</div>`;
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
        ? `${p.over === 'view' ? 'In view' : 'Highlight'} · ${esc(bars(range.bars))} · ${esc(spanText(p))}`
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
    else if (row) wrap.scrollTop = row.offsetTop - wrap.clientHeight / 2;  // new bars: their POC (one bar: its price) in the middle
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
  // marked and kept in view (the edge row nearest it when it is beyond what traded in the bars).
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
    if (label) label.textContent = `Cursor ${priceText(cursorPrice)}${beyond ? ` · ${beyond} what traded in ${overText(p)}` : ''}`;
    const wrap = box.querySelector('.l2-wrap'), head = wrap.querySelector('thead')?.offsetHeight || 0;
    if (tr.offsetTop < wrap.scrollTop + head || tr.offsetTop + tr.offsetHeight > wrap.scrollTop + wrap.clientHeight) {
      wrap.scrollTop = tr.offsetTop - wrap.clientHeight / 2;
    }
  }
  window.addEventListener('massive:symbol', () => { at = null; schedule(300); });
  window.addEventListener('massive:timezone', () => { if (lastProfile) render(lastProfile); });  // its times relabelled
  window.addEventListener('massive:view', (e) => { if (e.detail?.pane === 'analysis') schedule(50); });
  let lastKey = '';
  // A highlight drawn, moved or taken off (app.js scheduleHighlightPanel), or the chart scrolled or
  // zoomed: read once it settles.
  function coverMoved() {
    if (!shown() || coverKey() === lastCover) return;
    lastCover = coverKey();
    schedule(400);
  }
  window.addEventListener('massive:highlight', coverMoved);
  chart.subscribeAction('onVisibleRangeChange', coverMoved);
  setInterval(() => {  // follows interval changes, the bars covered, and the newest bar as it grows
    if (!shown()) return;
    if (key() !== lastKey) { lastKey = key(); lastCover = coverKey(); at = null; schedule(300); return; }
    if (coverKey() !== lastCover) { coverMoved(); return; }
    const since = Date.now() - lastLoad, c = cover();
    if (c ? growing(c) && since >= COVER_LIVE_MS : at == null && since >= (isLive() ? LIVE_MS : IDLE_MS)) load();
  }, 1000);
  // for inspection and checks: the profile shown, the cursor's price, and the cursor's handling
  window.Level2 = { get profile() { return lastProfile; }, get cursorPrice() { return cursorPrice; },
                    follow(c) { followCursor(c); markCursor(); } };
})();
