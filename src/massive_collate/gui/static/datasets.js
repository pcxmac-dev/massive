'use strict';
/* Datasets view: the research datasets (datasets.py) as lines on one time axis.

   Pick a dataset, then up to eight of its series; for the COT report pick a market, whose trader
   groups' positions are derived (net = long - short). Series in different units never share an
   axis: each unit gets a chart of its own, stacked on the same time axis. Transforms: returns
   compounded into the growth of $1 from the start of the range, any series indexed to 100 at the
   start, COT net positions in contracts or as a share of open interest. A series keeps its colour
   while others are added or removed. The legend's entries show or hide what they name (a series;
   for the COT report a trader group, in every view and market); the axes fit what is shown.
   Hover (or the arrow keys) reads every series at a date; the table view lists the values of the
   latest dates.

   Zoom: the wheel (or + and −) zooms about the cursor, dragging pans, double-click or Reset (0)
   shows the whole range again; the y axes fit what is in the window. Zoom only magnifies: growth
   and indexed lines stay based at the start of the range buttons' range.
   Markup: trend lines, levels, date lines, date zones and notes, kept in the browser per dataset
   (per market for the COT report). Date lines and zones are dates only and show in every view and
   chart; lines, levels and notes are values of the view they were drawn in (its transform, and the
   range a rebased transform starts from) and show on the chart of their unit. Click a mark to
   select it, Delete removes it. */
(() => {
  const $ = (id) => document.getElementById(id);
  const MAX = 8;
  const DAY = 86_400_000;
  const NS = 'http://www.w3.org/2000/svg';
  const TZ = 'America/New_York';  // dataset dates are US/Eastern midnights (datasets.py)
  const RANGES = [['1Y', 365], ['5Y', 5 * 365], ['10Y', 10 * 365], ['30Y', 30 * 365], ['All', null]];
  const PRESETS = {
    'french-3-factors-daily': ['Mkt-RF', 'SMB', 'HML'],
    'french-3-factors-monthly': ['Mkt-RF', 'SMB', 'HML'],
    'french-5-factors-daily': ['Mkt-RF', 'SMB', 'HML', 'RMW', 'CMA'],
    'french-49-industries-daily': ['Chips', 'Softw', 'Banks', 'Oil'],
    'shiller-us-market-monthly': ['PE10'],
    'imf-commodities-monthly': ['All Commodity Price Index', 'Fuel Energy Index', 'Metals Price Index', 'Food Price Index'],
  };
  const COT = 'cftc-cot-legacy';
  const COT_DEFAULT = '13874A';  // E-mini S&P 500
  const GROUPS = [['noncommercial', 'Non-commercial (speculators)', 'Speculators'],
    ['commercial', 'Commercial (hedgers)', 'Hedgers'], ['small', 'Small traders (non-reportable)', 'Small traders']];
  const NOTES = {
    'shiller-us-market-monthly': 'PE10 is CAPE: the S&P composite over ten years of average real earnings. The mirror ends dividends, earnings, CPI and CAPE in 2023; the price runs to the latest month.',
    'imf-commodities-monthly': 'IMF primary commodity prices, frozen in 2017. Prices are in US dollars per each commodity\'s own unit: index them to compare.',
    [COT]: 'Positions as of each Tuesday, published the following Friday at 15:30 ET. Net = long − short; spreading positions are left out of net.',
  };

  const saved = prefs.get('datasets', {});
  const st = {
    status: [], name: saved.name || 'french-3-factors-daily', catalog: null, query: '',
    picked: saved.picked || {}, market: saved.market || COT_DEFAULT, range: saved.range || 'All',
    transform: saved.transform || {}, logs: saved.logs || {}, hidden: saved.hidden || {}, table: false,
    slots: {}, cache: new Map(), lines: [], hoverT: null, built: false, loading: 0,
    view: null,  // zoom window {lo, hi} in ms inside the range; null: all of it
    tool: 'pan', marks: prefs.get('datasets-marks', {}), selected: null, drag: null, drawing: null, clearArmed: false,
  };
  const save = () => prefs.set('datasets', { name: st.name, picked: st.picked, market: st.market, range: st.range,
    transform: st.transform, logs: st.logs, hidden: st.hidden });

  // -- formatting ------------------------------------------------------------------------
  const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 });
  function fmt(v, unit = '') {
    if (v == null || !Number.isFinite(v)) return '–';
    const a = Math.abs(v);
    const n = a >= 1e4 ? compact.format(v) : a >= 100 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : a === 0 ? '0' : v.toPrecision(3);
    if (unit === 'growth of $1') return `$${n}`;
    return unit.startsWith('%') || unit === '%' ? `${n}%` : n;
  }
  function fmtTick(v, unit, step) {  // axis labels: only the decimals the step needs
    if (Math.abs(v) >= 1e4) return fmt(v, unit);
    const d = Math.max(0, Math.min(4, -Math.floor(Math.log10(step) + 1e-9)));
    const n = v.toFixed(d);
    return unit === 'growth of $1' ? `$${n}` : unit === '%' || unit.startsWith('%') ? `${n}%` : n;
  }
  function day(t, freq) {
    const d = new Date(t);
    if (freq === 'month') return d.toLocaleDateString('en-US', { timeZone: TZ, month: 'short', year: 'numeric' });
    return d.toLocaleDateString('en-CA', { timeZone: TZ });
  }

  // -- data ----------------------------------------------------------------------------------
  async function getJSON(url) {
    const r = await fetch(url);
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || r.statusText);
    return body;
  }
  async function values(name, series) {
    const want = series.filter((s) => !st.cache.has(`${name}|${s}`));
    for (let i = 0; i < want.length; i += MAX) {
      const part = want.slice(i, i + MAX);
      const body = await getJSON(`/api/dataset/values?name=${encodeURIComponent(name)}&series=${encodeURIComponent(part.join(','))}`);
      for (const [s, v] of Object.entries(body.series)) st.cache.set(`${name}|${s}`, v);
    }
    return Object.fromEntries(series.map((s) => [s, st.cache.get(`${name}|${s}`)]));
  }

  const isCot = () => st.name === COT;
  const isReturns = () => st.name.startsWith('french-');
  function transforms() {
    if (isCot()) return [['net', 'Net positions (contracts)'], ['netpct', 'Net as % of open interest'],
      ['gross', 'Long and short positions'], ['oi', 'Open interest']];
    if (isReturns()) return [['growth', 'Growth of $1 (compounded)'], ['values', 'Returns (%)'], ['index', 'Indexed to 100 at the start']];
    return [['values', 'Values'], ['index', 'Indexed to 100 at the start']];
  }
  const transform = () => {
    const t = st.transform[st.name];
    return transforms().some(([k]) => k === t) ? t : transforms()[0][0];
  };
  // Log scale is remembered per transform; compounded growth starts on it (a century of growth
  // on a linear scale hides everything but the last decades).
  const logOn = () => st.logs[transform()] ?? transform() === 'growth';

  // Colour slots follow the series, not its rank: freed when it is removed, kept by the others.
  function slotOf(key) {
    const slots = (st.slots[st.name] ||= {});
    if (slots[key]) return slots[key];
    const used = new Set(Object.values(slots));
    for (let i = 1; i <= MAX; i++) if (!used.has(i)) return (slots[key] = i);
    return 1;
  }
  function freeSlot(key) { if (st.slots[st.name]) delete st.slots[st.name][key]; }

  function inRange(t, v, lo) {
    let i = 0;
    while (i < t.length && t[i] < lo) i++;
    return { t: t.slice(i), v: v.slice(i) };
  }
  function rangeStart(lines) {  // the range ends at the newest value of any series shown
    const end = Math.max(...lines.map((l) => l.t[l.t.length - 1] ?? -Infinity));
    const days = RANGES.find(([k]) => k === st.range)?.[1];
    return days ? end - days * DAY : -Infinity;
  }

  async function buildLines() {
    const lines = await seriesLines();
    if (!isCot()) return lines;
    const lo = rangeStart(lines);
    return lines.map((l) => ({ ...l, ...inRange(l.t, l.v, lo) }));
  }

  async function seriesLines() {
    const tf = transform();
    if (isCot()) {
      const code = st.market;
      const fields = ['open_interest', ...GROUPS.flatMap(([g]) => [`${g}_long`, `${g}_short`])];
      const got = await values(COT, fields.map((f) => `${code}:${f}`));
      const at = Object.fromEntries(fields.map((f) => {
        const s = got[`${code}:${f}`];
        return [f, new Map(s ? s.t.map((t, i) => [t, s.v[i]]) : [])];
      }));
      const ts = [...at.open_interest.keys()].sort((a, b) => a - b);
      const line = (key, label, slot, f, unit, dash = false, short = label) => {
        const t = [], v = [];
        for (const x of ts) { const y = f(x); if (Number.isFinite(y)) { t.push(x); v.push(y); } }
        return { key, label, short, slot, unit, t, v, freq: 'week', dash };
      };
      const of = (g, label, l) => ({ ...l, group: g, groupLabel: label });
      if (tf === 'oi') return [of('oi', 'Open interest', line('oi', 'Open interest', 1, (x) => at.open_interest.get(x), 'contracts'))];
      if (tf === 'gross') {
        return GROUPS.flatMap(([g, label, short], i) => [
          of(g, label, line(`${g}_long`, `${label}: long`, i + 1, (x) => at[`${g}_long`].get(x), 'contracts', false, `${short} long`)),
          of(g, label, line(`${g}_short`, `${label}: short`, i + 1, (x) => at[`${g}_short`].get(x), 'contracts', true, `${short} short`))]);
      }
      return GROUPS.map(([g, label, short], i) => of(g, label, line(g, `${label}: net`, i + 1, (x) => {
        const net = at[`${g}_long`].get(x) - at[`${g}_short`].get(x);
        return tf === 'netpct' ? net / at.open_interest.get(x) * 100 : net;
      }, tf === 'netpct' ? '% of open interest' : 'contracts', false, short)));
    }
    const picked = st.picked[st.name] || [];
    const got = await values(st.name, picked);
    const raw = picked.filter((s) => got[s]).map((s) => ({ key: s, label: got[s].title || s, slot: slotOf(s),
      unit: got[s].unit || '', t: got[s].t, v: got[s].v, freq: got[s].freq, group: s, groupLabel: got[s].title || s }));
    const lo = rangeStart(raw);
    return raw.map((l) => {
      const r = inRange(l.t, l.v, lo);
      if (tf === 'growth' && l.unit === '%') {
        let g = 1;
        return { ...l, ...r, v: r.v.map((x) => (g *= 1 + x / 100)), unit: 'growth of $1' };
      }
      if (tf === 'index') {
        const base = r.v.find((x) => x !== 0);
        return base > 0 ? { ...l, ...r, v: r.v.map((x) => x / base * 100), unit: 'index (start = 100)' } : { ...l, ...r };
      }
      return { ...l, ...r };
    });
  }

  // -- scales and ticks ------------------------------------------------------------------
  function niceTicks(lo, hi, n = 5) {
    const span = hi - lo || Math.abs(hi) || 1;
    const raw = span / n, mag = 10 ** Math.floor(Math.log10(raw)), err = raw / mag;
    const step = (err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1) * mag;
    const out = [];
    for (let x = Math.ceil(lo / step) * step; x <= hi + step * 1e-9; x += step) out.push(Math.abs(x) < step * 1e-9 ? 0 : x);
    return out;
  }
  function logTicks(lo, hi) {
    const out = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
      for (const m of (Math.log10(hi / lo) < 2.5 ? [1, 2, 5] : [1])) {
        const x = m * 10 ** e;
        if (x >= lo && x <= hi) out.push(x);
      }
    }
    return out;
  }
  function timeTicks(lo, hi) {
    const years = (hi - lo) / (365.25 * DAY);
    const step = years <= 2 ? 0.25 : years <= 6 ? 1 : years <= 15 ? 2 : years <= 40 ? 5 : years <= 80 ? 10 : 20;
    const out = [];
    const y0 = new Date(lo).getUTCFullYear();
    for (let y = Math.ceil(y0 / Math.max(1, step)) * Math.max(1, step); ; y += Math.max(1, step)) {
      for (const m of (step < 1 ? [0, 3, 6, 9] : [0])) {
        const t = Date.UTC(y, m, 1);
        if (t > hi) return out;
        if (t >= lo) out.push({ t, label: step < 1 ? new Date(t).toLocaleDateString('en-US', { timeZone: 'UTC', month: 'short', year: 'numeric' }) : String(y) });
      }
    }
  }

  // -- drawing ---------------------------------------------------------------------------
  const el = (name, attrs = {}, parent = null) => {
    const e = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (parent) parent.append(e);
    return e;
  };

  function pathOf(l, x, y) {
    // At most four points per pixel column (first, low, high, last): the shape is kept, the work is not.
    let d = '', col = null, first, lo, hi, last;
    const flush = () => {
      if (col == null) return;
      for (const p of [first, lo, hi, last].filter((p, i, a) => a.indexOf(p) === i).sort((a, b) => a.i - b.i)) {
        d += `${d ? 'L' : 'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;
      }
    };
    for (let i = 0; i < l.t.length; i++) {
      const px = x(l.t[i]), py = y(l.v[i]);
      if (!Number.isFinite(py)) continue;
      const c = Math.round(px), p = { x: px, y: py, i };
      if (c !== col) { flush(); col = c; first = lo = hi = last = p; continue; }
      if (py > lo.y) lo = p;
      if (py < hi.y) hi = p;
      last = p;
    }
    flush();
    return d;
  }

  function nearest(t, x) {  // index of the value of t[] nearest to x
    let a = 0, b = t.length - 1;
    if (b < 0) return -1;
    while (a < b) { const m = (a + b) >> 1; if (t[m] < x) a = m + 1; else b = m; }
    return a > 0 && Math.abs(t[a - 1] - x) <= Math.abs(t[a] - x) ? a - 1 : a;
  }

  // -- zoom: a window of time inside the range ---------------------------------------------
  const MIN_SPAN = 30 * DAY;
  function lower(t, x) {  // first index with t[i] >= x
    let a = 0, b = t.length;
    while (a < b) { const m = (a + b) >> 1; if (t[m] < x) a = m + 1; else b = m; }
    return a;
  }
  function windowed(l, lo, hi) {  // the points in [lo, hi] and one either side, so lines run on to the edges
    const a = Math.max(0, lower(l.t, lo) - 1), b = Math.min(l.t.length, lower(l.t, hi) + 1);
    return a === 0 && b === l.t.length ? l : { ...l, t: l.t.slice(a, b), v: l.v.slice(a, b) };
  }
  function lastInside(l, lo, hi) {  // index of the last point in [lo, hi], or -1
    const j = lower(l.t, hi + 1) - 1;
    return j >= 0 && l.t[j] >= lo ? j : -1;
  }
  const extent = (lines) => [Math.min(...lines.map((l) => l.t[0])), Math.max(...lines.map((l) => l.t[l.t.length - 1]))];
  // Lowest and highest value inside [lo, hi] (else of all values): a loop, as eight daily series of
  // a century are hundreds of thousands of values, far too many to spread into Math.min's arguments.
  function valueRange(ls, lo, hi) {
    for (const inWindow of [true, false]) {
      let a = Infinity, b = -Infinity;
      for (const l of ls) {
        for (let i = 0; i < l.v.length; i++) {
          const v = l.v[i];
          if (!Number.isFinite(v) || (inWindow && (l.t[i] < lo || l.t[i] > hi))) continue;
          if (v < a) a = v;
          if (v > b) b = v;
        }
      }
      if (a <= b) return [a, b];
    }
    return [0, 1];
  }
  function setView(lo, hi) {
    if (!geom) return;
    const [a, b] = geom.full;
    const span = Math.max(Math.min(MIN_SPAN, b - a), hi - lo);
    if (span >= b - a) st.view = null;
    else {
      const start = Math.max(a, Math.min(lo, b - span));
      st.view = { lo: start, hi: start + span };
    }
    hideHover();
    draw();
  }
  function zoomAt(t, factor) {
    if (geom) setView(t - (t - geom.lo) * factor, t + (geom.hi - t) * factor);
  }
  const centre = () => (st.hoverT != null ? st.hoverT : (geom.lo + geom.hi) / 2);
  function resetZoom() {
    st.view = null;
    hideHover();
    draw();
  }
  let frame = 0;  // pans at most once a frame
  function panTo(lo, hi) {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => setView(lo, hi));
  }

  // -- markup ----------------------------------------------------------------------------
  const ICON = (path) => `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  const TOOLS = [
    ['pan', 'Pan and zoom: drag to pan, wheel to zoom, double-click for the whole range',
      '<path d="M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/>'],
    ['line', 'Trend line: drag from one point to another', '<path d="M5 19 19 5"/><circle cx="5" cy="19" r="1.6"/><circle cx="19" cy="5" r="1.6"/>'],
    ['hline', 'Level: a horizontal line at a value (click)', '<path d="M3 12h18"/><path d="M3 8v8" opacity=".45"/>'],
    ['vline', 'Date line: a vertical line at a date (click); shows in every view and chart', '<path d="M12 3v18"/><path d="M8 3h8" opacity=".45"/>'],
    ['zone', 'Date zone: drag across a span of dates; shows in every view and chart',
      '<rect x="7" y="4" width="10" height="16" fill="currentColor" opacity=".25" stroke="none"/><path d="M7 4v16M17 4v16"/>'],
    ['note', 'Note: click where it goes, type, Enter', '<path d="M6 5h12M12 5v14"/>'],
  ];
  const ctx = () => (isCot() ? `${st.name}|${st.market}` : st.name);
  const valueView = () => `${transform()}|${['growth', 'index'].includes(transform()) ? st.range : ''}`;
  const markList = () => (st.marks[ctx()] ||= []);
  const timeOnly = (m) => m.type === 'vline' || m.type === 'zone';
  const visibleMarks = () => { const vv = valueView(); return markList().filter((m) => timeOnly(m) || m.view === vv); };
  function saveMarks() {
    for (const k of Object.keys(st.marks)) if (!st.marks[k].length) delete st.marks[k];
    prefs.set('datasets-marks', st.marks);
  }
  function addMark(m) {
    m.id = `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    markList().push(m);
    saveMarks();
    st.tool = 'pan';
    st.selected = m.id;
    draw();
  }
  function removeMark(id) {
    st.marks[ctx()] = markList().filter((m) => m.id !== id);
    if (st.selected === id) st.selected = null;
    saveMarks();
    draw();
  }

  function renderMarks(g, marks, c) {
    const L = geom.left, R = geom.W - geom.right;
    for (const m of marks) {
      if (!timeOnly(m) && m.unit !== c.unit) continue;
      const grp = el('g', { class: `ds-mark${m.id === st.selected ? ' sel' : ''}`, 'data-mark': m.id }, g);
      const seg = (x1, y1, x2, y2) => {
        el('line', { x1, y1, x2, y2, class: 'mk' }, grp);
        el('line', { x1, y1, x2, y2, class: 'hit' }, grp);  // a wider, invisible target to click
      };
      if (m.type === 'zone') {
        const a = geom.x(Math.min(m.t1, m.t2)), b = geom.x(Math.max(m.t1, m.t2));
        el('rect', { x: a, y: c.y0, width: Math.max(1, b - a), height: c.y1 - c.y0, class: 'zone' }, grp);
        el('rect', { x: a, y: c.y0, width: Math.max(4, b - a), height: 14, class: 'handle' }, grp);
        if (c.first) {
          const tx = el('text', { x: Math.max(L, a) + 4, y: c.y0 + 11, class: 'mk-label' }, grp);
          tx.textContent = `${day(Math.min(m.t1, m.t2))} → ${day(Math.max(m.t1, m.t2))}`;
          const bb = tx.getBBox();
          if (bb.x + bb.width + 4 > R) {  // near the right edge: the label ends at the zone's end instead
            tx.setAttribute('x', Math.min(R, b) - 4);
            tx.setAttribute('text-anchor', 'end');
          }
        }
      } else if (m.type === 'vline') {
        const xx = geom.x(m.t);
        seg(xx, c.y0, xx, c.y1);
        if (c.first) el('text', { x: xx + 4, y: c.y0 + 25, class: 'mk-label' }, grp).textContent = day(m.t);  // below zone labels
      } else if (m.type === 'hline') {
        const yy = c.y(m.v);
        if (!Number.isFinite(yy)) continue;
        seg(L, yy, R, yy);
        el('text', { x: R - 4, y: yy - 4, 'text-anchor': 'end', class: 'mk-label' }, grp).textContent = fmt(m.v, c.unit);
      } else if (m.type === 'line') {
        const pts = [geom.x(m.t1), c.y(m.v1), geom.x(m.t2), c.y(m.v2)];
        if (!pts.every(Number.isFinite)) continue;
        seg(...pts);
        el('circle', { cx: pts[0], cy: pts[1], r: 3, class: 'end' }, grp);
        el('circle', { cx: pts[2], cy: pts[3], r: 3, class: 'end' }, grp);
      } else if (m.type === 'note') {
        const xx = geom.x(m.t), yy = c.y(m.v);
        if (!Number.isFinite(yy)) continue;
        el('circle', { cx: xx, cy: yy, r: 3.5, class: 'pin' }, grp);
        const tx = el('text', { x: xx + 9, y: yy - 9, class: 'note' }, grp);
        tx.textContent = m.text;
        let bb = tx.getBBox();
        if (bb.x + bb.width + 4 > R) {  // no room on the right of the pin: the note goes on its left
          tx.setAttribute('x', xx - 9);
          tx.setAttribute('text-anchor', 'end');
          bb = tx.getBBox();
        }
        grp.insertBefore(el('rect', { x: bb.x - 4, y: bb.y - 2, width: bb.width + 8, height: bb.height + 4, rx: 3,
          class: 'note-bg' }), tx);
      }
    }
  }

  function renderTools() {
    const box = $('dsTools');
    if (!box) return;
    box.innerHTML = TOOLS.map(([k, title, path]) => `<button type="button" data-tool="${k}" class="${st.tool === k ? 'active' : ''}"
        aria-pressed="${st.tool === k}" title="${esc(title)}">${ICON(path)}</button>`).join('')
      + `<button type="button" data-act="remove" title="Remove the selected mark (Delete)" ${st.selected ? '' : 'disabled'}>${ICON('<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>')}</button>`
      + `<button type="button" data-act="clear" title="Remove every mark of this ${isCot() ? 'market' : 'dataset'}" ${markList().length ? '' : 'disabled'}>${st.clearArmed ? 'Clear all?' : 'Clear'}</button>`;
    $('dsCharts').classList.toggle('tool-draw', st.tool !== 'pan');
  }
  function renderZoom() {
    if (!$('dsZoomInfo')) return;
    $('dsZoomInfo').textContent = st.view && geom ? `${day(geom.lo)} → ${day(geom.hi)}` : '';
    $('dsZoomReset').disabled = !st.view;
  }

  function openNote(e, c, t, v) {  // an inline field (the embedded browser has no prompt())
    const main = $('dsMain').getBoundingClientRect();
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'ds-note-input';
    input.placeholder = 'Note, then Enter';
    input.maxLength = 200;
    input.style.left = `${Math.min(e.clientX - main.left + 8, main.width - 230)}px`;
    input.style.top = `${e.clientY - main.top - 14}px`;
    $('dsMain').append(input);
    let done = false;
    const finish = (keep) => {
      if (done) return;
      done = true;
      const text = input.value.trim();
      input.remove();
      if (keep && text) addMark({ type: 'note', unit: c.unit, view: valueView(), t, v, text });
      else { st.tool = 'pan'; draw(); }
    };
    input.addEventListener('keydown', (k) => {
      k.stopPropagation();  // typing here is not a shortcut of the app
      if (k.key === 'Enter') finish(true);
      if (k.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', () => finish(true));
    setTimeout(() => input.focus(), 0);
  }

  const hiddenKeys = () => new Set(st.hidden[st.name] || []);
  const present = () => st.lines.filter((l) => l.t.length);
  const shown = () => { const off = hiddenKeys(); return present().filter((l) => !off.has(l.group)); };
  function toggle(key) {
    const off = hiddenKeys();
    if (off.has(key)) off.delete(key); else off.add(key);
    st.hidden[st.name] = [...off];
    save();
    hideHover();
    draw();
  }

  let geom = null;  // what the hover and pointer layers need: the scales, the window and each chart
  function draw() {
    const host = $('dsCharts');
    host.replaceChildren();
    const all = present();
    const full = shown();
    renderLegend(all);
    renderTools();
    $('dsEmpty').hidden = !!full.length;
    $('dsEmpty').textContent = all.length ? 'Everything is hidden: click a name above to show it.' : 'Pick series on the left.';
    if (!full.length) { geom = null; renderTable([]); renderZoom(); return; }
    const [fa, fb] = extent(full);
    const lo = st.view ? Math.max(fa, st.view.lo) : fa;
    const hi = st.view ? Math.min(fb, st.view.hi) : fb;
    const lines = full.map((l) => windowed(l, lo, hi)).filter((l) => l.t.length);
    const units = [...new Set(full.map((l) => l.unit))];
    const W = Math.max(320, host.clientWidth);
    const H = Math.max(260, host.clientHeight);
    const axisH = 24;
    const h = Math.max(150, (H - axisH) / units.length);
    // direct end labels when every chart has four lines or fewer (the legend always carries identity)
    const ends = units.every((u) => full.filter((l) => l.unit === u).length <= 4);
    const left = 64, right = ends && W > 640 ? 170 : 16, top = 20, bottom = 8;
    const pw = W - left - right;
    const x = (t) => left + (t - lo) / Math.max(1, hi - lo) * pw;
    const invX = (px) => lo + (px - left) / pw * (hi - lo);
    geom = { x, invX, lo, hi, full: [fa, fb], left, right, W, charts: [], allT: [lo] };  // allT: set once drawn
    const marks = visibleMarks();
    units.forEach((unit, gi) => {
      const ls = lines.filter((l) => l.unit === unit);
      const last = gi === units.length - 1;
      const ch = h + (last ? axisH : 0);
      const svg = el('svg', { width: W, height: ch, class: 'ds-chart', role: 'img',
        'aria-label': `${ls.map((l) => l.label).join(', ')} (${unit}), ${day(lo)} to ${day(hi)}` }, host);
      const y0 = top, y1 = h - bottom;
      const clip = `dsClip${gi}`;
      el('rect', { x: left, y: y0 - 3, width: pw, height: y1 - y0 + 6 }, el('clipPath', { id: clip }, el('defs', {}, svg)));
      // the y axis fits the points inside the window
      const [dlo, dhi] = valueRange(ls, lo, hi);
      let vlo = dlo, vhi = dhi;
      const log = logOn() && vlo > 0;
      let y, invY;
      if (log) {
        const a = Math.log10(vlo), b = Math.log10(vhi), pad = (b - a || 1) * 0.04, A = a - pad, B = b + pad;
        y = (v) => y1 - (Math.log10(v) - A) / (B - A) * (y1 - y0);
        invY = (py) => 10 ** (A + (y1 - py) / (y1 - y0) * (B - A));
      } else {
        const pad = (vhi - vlo || Math.abs(vhi) || 1) * 0.05;
        vlo -= pad; vhi += pad;
        y = (v) => y1 - (v - vlo) / (vhi - vlo) * (y1 - y0);
        invY = (py) => vlo + (y1 - py) / (y1 - y0) * (vhi - vlo);
      }
      const ticks = log ? logTicks(dlo, dhi) : niceTicks(vlo, vhi);
      const step = ticks.length > 1 ? Math.min(...ticks.slice(1).map((t, i) => Math.abs(t - ticks[i]))) : 1;
      const grid = el('g', { class: 'ds-grid' }, svg);
      for (const v of ticks) {
        const yy = y(v);
        if (yy < y0 - 1 || yy > y1 + 1) continue;
        el('line', { x1: left, x2: W - right, y1: yy, y2: yy, class: v === 0 ? 'zero' : '' }, grid);
        el('text', { x: left - 6, y: yy + 4, 'text-anchor': 'end' }, grid).textContent = fmtTick(v, unit, log ? v : step);
      }
      el('text', { x: left, y: 12, class: 'ds-unit' }, svg).textContent = ls.length === 1 ? `${ls[0].label} · ${unit}` : unit;
      if (last) {
        for (const tk of timeTicks(lo, hi)) {
          const xx = x(tk.t);
          el('line', { x1: xx, x2: xx, y1: y1, y2: y1 + 4, class: 'tick' }, grid);
          el('text', { x: xx, y: y1 + 17, 'text-anchor': 'middle' }, grid).textContent = tk.label;
        }
      }
      const plot = el('g', { 'clip-path': `url(#${clip})` }, svg);
      for (const l of ls) {
        el('path', { d: pathOf(l, x, y), class: `ds-line s${l.slot}${l.dash ? ' dash' : ''}` }, plot);
      }
      const c = { svg, ls, y, invY, y0, y1, unit, first: gi === 0 };
      renderMarks(el('g', { class: 'ds-marks' }, plot), marks, c);
      if (ends && right > 16) {  // direct labels at the last point in the window, nudged apart
        const labs = ls.map((l) => ({ l, j: lastInside(l, lo, hi) })).filter((a) => a.j >= 0)
          .map((a) => ({ ...a, y: y(a.l.v[a.j]) })).sort((a, b) => a.y - b.y);
        for (let i = 1; i < labs.length; i++) labs[i].y = Math.max(labs[i].y, labs[i - 1].y + 14);
        for (let i = labs.length - 1; i >= 0; i--) labs[i].y = Math.min(labs[i].y, y1 - (labs.length - 1 - i) * 14);
        for (const { l, j, y: yy } of labs) {
          const ex = x(l.t[j]);
          el('line', { x1: ex + 3, x2: ex + 13, y1: yy, y2: yy, class: `ds-key s${l.slot}${l.dash ? ' dash' : ''}` }, svg);
          const tx = el('text', { x: ex + 17, y: yy + 4, class: 'ds-end' }, svg);
          const label = l.short || l.label;
          const name = label.length > 18 ? `${label.slice(0, 17)}…` : label;
          tx.textContent = `${fmt(l.v[j], unit)} `;
          el('tspan', { class: 'muted' }, tx).textContent = name;
        }
      }
      const hover = el('g', { class: 'ds-hover', visibility: 'hidden' }, svg);
      const cross = el('line', { y1: y0, y2: y1, class: 'cross' }, hover);
      const dots = ls.map((l) => el('circle', { r: 4, class: `ds-dot s${l.slot}` }, hover));
      geom.charts.push({ ...c, hover, cross, dots });
    });
    geom.allT = [...new Set(lines.flatMap((l) => l.t.filter((t) => t >= lo && t <= hi)))].sort((a, b) => a - b);
    if (!geom.allT.length) geom.allT = [lo];
    renderTable(full);
    renderZoom();
    if (st.hoverT != null && st.hoverT >= lo && st.hoverT <= hi) showHover(st.hoverT);
  }

  // One entry per identity (a series; a COT trader group with its net, long and short lines), each a
  // button that shows or hides it. A lone series needs none: its name heads the axis and the picker.
  function renderLegend(lines) {
    const ids = [];
    for (const l of lines) {
      let id = ids.find((x) => x.key === l.group);
      if (!id) ids.push(id = { key: l.group, label: l.groupLabel || l.label, lines: [] });
      id.lines.push(l);
    }
    const off = hiddenKeys();
    $('dsLegend').replaceChildren(...(ids.length < 2 ? [] : ids).map((id) => {
      const hidden = off.has(id.key);
      const b = document.createElement('button');
      b.type = 'button';
      b.className = `ds-legend-item${hidden ? ' off' : ''}`;
      b.dataset.key = id.key;
      b.setAttribute('aria-pressed', String(!hidden));
      b.title = `${hidden ? 'Show' : 'Hide'} ${id.label}`;
      const svg = el('svg', { width: 4 + 16 * id.lines.length, height: 10, 'aria-hidden': 'true' });
      id.lines.forEach((l, i) => el('line', { x1: 2 + 16 * i, x2: 15 + 16 * i, y1: 5, y2: 5,
        class: `ds-key s${l.slot}${l.dash ? ' dash' : ''}` }, svg));
      const text = document.createElement('span');
      text.textContent = id.label;
      b.append(svg, text);
      return b;
    }));
  }

  function showHover(t) {
    if (!geom) return;
    st.hoverT = t;
    const tip = $('dsTip');
    tip.replaceChildren();
    const head = document.createElement('div');
    head.className = 'ds-tip-date';
    const freq = st.lines[0]?.freq;
    head.textContent = isCot() ? `${day(t)} (Tuesday)` : day(t, freq);
    tip.append(head);
    for (const c of geom.charts) {
      const xx = geom.x(t);
      c.cross.setAttribute('x1', xx); c.cross.setAttribute('x2', xx);
      c.hover.setAttribute('visibility', 'visible');
      c.ls.forEach((l, i) => {
        const j = nearest(l.t, t);
        const ok = j >= 0 && Math.abs(l.t[j] - t) <= (l.freq === 'month' ? 20 : l.freq === 'week' ? 4 : 3) * DAY;
        c.dots[i].setAttribute('visibility', ok ? 'visible' : 'hidden');
        if (ok) { c.dots[i].setAttribute('cx', geom.x(l.t[j])); c.dots[i].setAttribute('cy', c.y(l.v[j])); }
        const row = document.createElement('div');
        row.className = 'ds-tip-row';
        const key = el('svg', { width: 14, height: 8, 'aria-hidden': 'true' });
        el('line', { x1: 1, x2: 13, y1: 4, y2: 4, class: `ds-key s${l.slot}${l.dash ? ' dash' : ''}` }, key);
        const b = document.createElement('b');
        b.textContent = ok ? fmt(l.v[j], l.unit) : '–';
        const name = document.createElement('span');
        name.textContent = l.label;
        row.append(key, b, name);
        tip.append(row);
      });
    }
    tip.hidden = false;
    const host = $('dsCharts').getBoundingClientRect(), main = $('dsMain').getBoundingClientRect();
    const px = host.left - main.left + geom.x(t);
    tip.style.left = `${px + 16 + tip.offsetWidth > main.width ? px - 16 - tip.offsetWidth : px + 16}px`;
    tip.style.top = `${host.top - main.top + 8}px`;
  }
  function hideHover() {
    st.hoverT = null;
    $('dsTip').hidden = true;
    for (const c of geom?.charts || []) c.hover.setAttribute('visibility', 'hidden');
  }

  function renderTable(lines) {
    const wrap = $('dsTableWrap');
    wrap.hidden = !st.table || !lines.length;
    if (wrap.hidden) return;
    const at = lines.map((l) => new Map(l.t.map((t, i) => [t, l.v[i]])));
    const dates = [...new Set(lines.flatMap((l) => l.t))].sort((a, b) => b - a).slice(0, 60);
    const table = document.createElement('table');
    table.className = 'act-table';
    const hr = table.createTHead().insertRow();
    for (const h of ['Date', ...lines.map((l) => `${l.label} (${l.unit})`)]) {
      const th = document.createElement('th');
      th.textContent = h;
      if (h !== 'Date') th.className = 'num';
      hr.append(th);
    }
    const body = table.createTBody();
    for (const t of dates) {
      const r = body.insertRow();
      r.insertCell().textContent = day(t, lines[0].freq);
      at.forEach((m, i) => { const c = r.insertCell(); c.className = 'num'; c.textContent = fmt(m.get(t), lines[i].unit); });
    }
    wrap.replaceChildren(table);
  }

  // -- picker ----------------------------------------------------------------------------
  function renderPicker() {
    const list = $('dsList');
    const q = st.query.trim().toLowerCase();
    if (isCot()) {
      $('dsSearch').placeholder = 'Search markets (name or code)';
      const markets = (st.catalog?.markets || []).filter((m) => !q || m.name.toLowerCase().includes(q) || m.code.toLowerCase().includes(q));
      const newest = Math.max(...(st.catalog?.markets || []).map((m) => m.last || 0));
      list.innerHTML = markets.slice(0, 300).map((m) => `
        <button type="button" class="ds-item${m.code === st.market ? ' on' : ''}" data-market="${esc(m.code)}" role="option" aria-selected="${m.code === st.market}">
          <span class="ds-name">${esc(m.name)}</span>
          <small>${esc(m.code)} · OI ${fmt(m.openInterest)} · ${m.last < newest - 21 * DAY ? `ended ${day(m.last).slice(0, 4)}` : `since ${day(m.first).slice(0, 4)}`}</small>
        </button>`).join('') + (markets.length > 300 ? `<div class="p-muted small">${markets.length - 300} more: narrow the search</div>` : '');
      return;
    }
    $('dsSearch').placeholder = 'Search series';
    const picked = st.picked[st.name] || [];
    picked.forEach(slotOf);  // colours in the order the series were picked, not the list's order
    const series = (st.catalog?.series || []).filter((s) => !q || s.series.toLowerCase().includes(q) || (s.title || '').toLowerCase().includes(q));
    list.innerHTML = series.map((s) => {
      const on = picked.includes(s.series);
      const slot = on ? slotOf(s.series) : 0;
      return `<label class="ds-item${on ? ' on' : ''}" title="${esc(s.title)} · ${esc(s.unit)} · ${s.n} values">
        <input type="checkbox" value="${esc(s.series)}" ${on ? 'checked' : ''} ${!on && picked.length >= MAX ? 'disabled' : ''}>
        <svg width="14" height="8" aria-hidden="true">${on ? `<line x1="1" x2="13" y1="4" y2="4" class="ds-key s${slot}"/>` : ''}</svg>
        <span class="ds-name">${esc(s.title || s.series)}</span>
        <small>${day(s.first).slice(0, 4)}–${day(s.last).slice(0, 4)}</small></label>`;
    }).join('') + (picked.length >= MAX ? `<div class="p-muted small">${MAX} series at most: remove one to add another</div>` : '');
  }

  function renderControls() {
    $('dsName').innerHTML = st.status.filter((d) => d.name !== 'sp500-constituents').map((d) =>
      `<option value="${esc(d.name)}" ${d.name === st.name ? 'selected' : ''} ${d.files ? '' : 'disabled'}>${esc(d.title)}${d.files ? '' : ' (not fetched)'}</option>`).join('');
    $('dsRanges').innerHTML = RANGES.map(([k]) => `<button type="button" data-range="${k}" class="${k === st.range ? 'active' : ''}" aria-pressed="${k === st.range}">${k}</button>`).join('');
    $('dsRanges').hidden = false;
    $('dsTransform').innerHTML = transforms().map(([k, label]) => `<option value="${k}" ${k === transform() ? 'selected' : ''}>${esc(label)}</option>`).join('');
    $('dsLog').classList.toggle('on', logOn());
    $('dsLog').setAttribute('aria-pressed', String(logOn()));
    $('dsTable').classList.toggle('on', st.table);
    $('dsTable').setAttribute('aria-pressed', String(st.table));
    const d = st.status.find((x) => x.name === st.name);
    $('dsSource').textContent = d ? `${d.provider} (${d.kind}) · ${d.licence} · quality ${d.quality == null ? '–' : d.quality.toFixed(2)}`
      + (d.fetched ? ` · fetched ${new Date(d.fetched).toLocaleDateString('en-CA')}` : '') : '';
    const tf = transform();
    const extra = tf === 'growth' ? 'Growth of $1: each return compounded from the start of the range, so lines start together. '
      : tf === 'index' ? 'Indexed: each series divided by its first value in the range, times 100. ' : '';
    const toggles = isCot() ? 'Click a trader group in the legend to hide or show it. ' : 'Click a name in the legend to hide or show it. ';
    $('dsNote').textContent = extra + toggles + (NOTES[st.name] || '');
  }

  async function refresh() {
    const n = ++st.loading;
    $('dsMain').classList.add('loading');  // keeps the previous frame, dimmed, while data loads
    try {
      const lines = await buildLines();
      if (n !== st.loading) return;
      st.lines = lines;
      $('dsError').textContent = '';
      draw();
    } catch (e) {
      $('dsError').textContent = String(e.message || e);
    } finally {
      if (n === st.loading) $('dsMain').classList.remove('loading');
    }
  }

  async function selectDataset(name) {
    st.name = name;
    st.view = null;
    st.selected = null;
    st.tool = 'pan';
    st.query = '';
    $('dsSearch').value = '';
    st.hoverT = null;
    save();
    st.catalog = null;
    renderControls();
    renderPicker();
    try {
      st.catalog = await getJSON(`/api/dataset/series?name=${encodeURIComponent(name)}`);
    } catch (e) {
      $('dsError').textContent = String(e.message || e);
      return;
    }
    if (!isCot() && !st.picked[name]) {
      const have = new Set(st.catalog.series.map((s) => s.series));
      st.picked[name] = (PRESETS[name] || []).filter((s) => have.has(s));
      if (!st.picked[name].length) st.picked[name] = st.catalog.series.slice(0, 3).map((s) => s.series);
      save();
    }
    if (isCot() && !st.catalog.markets.some((m) => m.code === st.market)) st.market = st.catalog.markets[0]?.code;
    renderPicker();
    await refresh();
  }

  // -- view --------------------------------------------------------------------------------
  function build() {
    if (st.built) return;
    st.built = true;
    $('viewDatasets').innerHTML = `
      <div class="view-head"><div><h2>Datasets</h2><div id="dsSource" class="p-muted small"></div></div></div>
      <div class="ds-bar" role="toolbar" aria-label="Dataset, range and transform">
        <select id="dsName" aria-label="Dataset"></select>
        <div id="dsRanges" class="group" role="group" aria-label="Range"></div>
        <select id="dsTransform" aria-label="Transform"></select>
        <button type="button" id="dsLog" class="toggle" title="Logarithmic scale (positive values only)">Log scale</button>
        <button type="button" id="dsTable" class="toggle" title="The values of the latest dates as a table">Table</button>
        <div id="dsTools" class="group ds-tools" role="group" aria-label="Markup"></div>
        <div class="group" role="group" aria-label="Zoom">
          <button type="button" id="dsZoomOut" title="Zoom out (−)">−</button>
          <button type="button" id="dsZoomIn" title="Zoom in (+)">+</button>
          <button type="button" id="dsZoomReset" title="The whole range (0, or double-click the chart)">Reset</button>
        </div>
        <span id="dsZoomInfo" class="p-muted small"></span>
        <span id="dsError" class="down small" role="alert"></span>
      </div>
      <div class="ds-body">
        <div class="ds-pick"><input id="dsSearch" type="search" autocomplete="off" aria-label="Search">
          <div id="dsList" class="ds-list" role="listbox" aria-label="Series"></div></div>
        <div id="dsMain" class="ds-main">
          <div id="dsLegend" class="ds-legend"></div>
          <div id="dsCharts" class="ds-charts" tabindex="0"
            aria-label="Chart: arrow keys move along the dates, + and − zoom, 0 shows the whole range, Delete removes the selected mark"></div>
          <div id="dsEmpty" class="p-muted ds-empty" hidden>Pick series on the left.</div>
          <div id="dsTip" class="ds-tip" hidden></div>
          <div id="dsNote" class="p-muted small ds-note"></div>
          <div id="dsTableWrap" class="table-wrap ds-table" hidden></div>
        </div>
      </div>`;
    $('dsName').addEventListener('change', (e) => selectDataset(e.target.value));
    $('dsRanges').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-range]');
      if (!b) return;
      st.range = b.dataset.range;
      st.view = null;
      save(); renderControls(); refresh();
    });
    $('dsTransform').addEventListener('change', (e) => { st.transform[st.name] = e.target.value; save(); renderControls(); refresh(); });
    $('dsLog').addEventListener('click', () => { st.logs[transform()] = !logOn(); save(); renderControls(); draw(); });
    $('dsTable').addEventListener('click', () => { st.table = !st.table; renderControls(); renderTable(shown()); });
    $('dsLegend').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-key]');
      if (b) toggle(b.dataset.key);
    });
    $('dsSearch').addEventListener('input', (e) => { st.query = e.target.value; renderPicker(); });
    $('dsList').addEventListener('change', (e) => {
      const box = e.target.closest('input[type=checkbox]');
      if (!box) return;
      const picked = (st.picked[st.name] ||= []);
      if (box.checked && picked.length < MAX) { picked.push(box.value); slotOf(box.value); }
      if (!box.checked) {
        st.picked[st.name] = picked.filter((s) => s !== box.value);
        freeSlot(box.value);
        st.hidden[st.name] = (st.hidden[st.name] || []).filter((k) => k !== box.value);  // picked again = shown
      }
      save(); renderPicker(); refresh();
    });
    $('dsList').addEventListener('click', (e) => {
      const b = e.target.closest('button[data-market]');
      if (!b) return;
      st.market = b.dataset.market;
      st.view = null;
      st.selected = null;
      save(); renderPicker(); refresh();
    });
    $('dsTools').addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.tool) {
        st.tool = st.tool === b.dataset.tool ? 'pan' : b.dataset.tool;  // a second click puts the tool down
        st.selected = null;
        draw();
      } else if (b.dataset.act === 'remove' && st.selected) {
        removeMark(st.selected);
      } else if (b.dataset.act === 'clear') {
        clearTimeout(st.clearTimer);
        if (!st.clearArmed) {  // a second click within 3 s confirms (no confirm(): the embedded browser has none)
          st.clearArmed = true;
          st.clearTimer = setTimeout(() => { st.clearArmed = false; renderTools(); }, 3000);
          renderTools();
          return;
        }
        st.clearArmed = false;
        st.marks[ctx()] = [];
        st.selected = null;
        saveMarks();
        draw();
      }
    });
    $('dsZoomIn').addEventListener('click', () => zoomAt(centre(), 1 / 1.5));
    $('dsZoomOut').addEventListener('click', () => zoomAt(centre(), 1.5));
    $('dsZoomReset').addEventListener('click', resetZoom);

    const charts = $('dsCharts');
    const chartOf = (e) => geom?.charts.find((c) => c.svg.contains(e.target)) || null;
    const at = (e, c) => { const r = c.svg.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    const clampX = (px) => Math.max(geom.left, Math.min(geom.W - geom.right, px));
    const capture = (e) => { try { charts.setPointerCapture(e.pointerId); } catch { /* no live pointer: drag without capture */ } };
    charts.addEventListener('pointerdown', (e) => {
      if (!geom || e.button !== 0) return;
      if (st.tool === 'pan') {
        const hit = e.target.closest('[data-mark]');
        if (hit) { st.selected = hit.dataset.mark; draw(); return; }
        if (st.selected) { st.selected = null; draw(); }
        st.drag = { x: e.clientX, lo: geom.lo, hi: geom.hi, moved: false };
        capture(e);
        return;
      }
      const c = chartOf(e);
      if (!c) return;
      const p = at(e, c);
      if (p.x < geom.left || p.x > geom.W - geom.right || p.y < c.y0 || p.y > c.y1) return;
      const t = geom.invX(p.x), v = c.invY(p.y);
      if (st.tool === 'hline') addMark({ type: 'hline', unit: c.unit, view: valueView(), v });
      else if (st.tool === 'vline') addMark({ type: 'vline', t });
      else if (st.tool === 'note') openNote(e, c, t, v);
      else {
        st.drawing = { type: st.tool, c, t, v, x: p.x, y: p.y, preview: null };
        capture(e);
      }
    });
    charts.addEventListener('pointermove', (e) => {
      if (!geom) return;
      if (st.drag) {  // pan: the window follows the pointer
        const dx = e.clientX - st.drag.x;
        if (!st.drag.moved && Math.abs(dx) < 3) return;
        st.drag.moved = true;
        charts.classList.add('panning');
        const dt = -dx / (geom.W - geom.left - geom.right) * (st.drag.hi - st.drag.lo);
        panTo(st.drag.lo + dt, st.drag.hi + dt);
        return;
      }
      if (st.drawing) {  // a live preview of the line or zone
        const d = st.drawing, p = at(e, d.c);
        const px = clampX(p.x), py = Math.max(d.c.y0, Math.min(d.c.y1, p.y));
        if (!d.preview) d.preview = el(d.type === 'zone' ? 'rect' : 'line', { class: 'ds-preview' }, d.c.svg);
        const attrs = d.type === 'zone'
          ? { x: Math.min(d.x, px), y: d.c.y0, width: Math.abs(px - d.x), height: d.c.y1 - d.c.y0 }
          : { x1: d.x, y1: d.y, x2: px, y2: py };
        for (const [k, v] of Object.entries(attrs)) d.preview.setAttribute(k, v);
        return;
      }
      const r = charts.getBoundingClientRect();
      const px = e.clientX - r.left;
      if (px < geom.left || px > geom.W - geom.right) { hideHover(); return; }
      showHover(geom.allT[nearest(geom.allT, geom.invX(px))]);
    });
    const release = (e) => {
      if (st.drag) {
        st.drag = null;
        charts.classList.remove('panning');
        return;
      }
      const d = st.drawing;
      if (!d) return;
      st.drawing = null;
      d.preview?.remove();
      if (e.type === 'pointercancel') return;
      const p = at(e, d.c);
      const px = clampX(p.x), py = Math.max(d.c.y0, Math.min(d.c.y1, p.y));
      if (d.type === 'zone' ? Math.abs(px - d.x) <= 4 : Math.hypot(px - d.x, py - d.y) <= 4) return;  // a click, not a drag
      if (d.type === 'zone') addMark({ type: 'zone', t1: d.t, t2: geom.invX(px) });
      else addMark({ type: 'line', unit: d.c.unit, view: valueView(), t1: d.t, v1: d.v, t2: geom.invX(px), v2: d.c.invY(py) });
    };
    charts.addEventListener('pointerup', release);
    charts.addEventListener('pointercancel', release);
    charts.addEventListener('pointerleave', () => { if (!st.drag && !st.drawing) hideHover(); });
    charts.addEventListener('wheel', (e) => {
      if (!geom) return;
      e.preventDefault();
      const r = charts.getBoundingClientRect();
      zoomAt(geom.invX(clampX(e.clientX - r.left)), e.deltaY > 0 ? 1.2 : 1 / 1.2);
    }, { passive: false });
    charts.addEventListener('dblclick', (e) => { if (st.tool === 'pan' && !e.target.closest('[data-mark]')) resetZoom(); });
    charts.addEventListener('keydown', (e) => {
      if (!geom) return;
      const zoom = { '+': 1 / 1.5, '=': 1 / 1.5, '-': 1.5, '_': 1.5 }[e.key];
      if (zoom || e.key === '0') {
        e.preventDefault();
        e.stopPropagation();  // the chart's keys here, not the price chart's
        if (zoom) zoomAt(centre(), zoom); else resetZoom();
        return;
      }
      if (e.key === 'Escape' && (st.tool !== 'pan' || st.selected)) {
        e.stopPropagation();  // put the tool down first; a second Escape leaves the view as before
        st.tool = 'pan';
        st.selected = null;
        draw();
        return;
      }
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
      e.preventDefault();
      const i = st.hoverT == null ? geom.allT.length - 1 : nearest(geom.allT, st.hoverT);
      const j = e.key === 'Home' ? 0 : e.key === 'End' ? geom.allT.length - 1
        : Math.max(0, Math.min(geom.allT.length - 1, i + (e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 20 : 1)));
      showHover(geom.allT[j]);
    });
    charts.addEventListener('blur', () => { if (!st.drag) hideHover(); });
    document.addEventListener('keydown', (e) => {  // Delete removes the selected mark, wherever the focus is
      if (document.body.dataset.view !== 'datasets' || !st.selected || !['Delete', 'Backspace'].includes(e.key)) return;
      if (e.target.closest?.('input, select, textarea')) return;
      e.preventDefault();
      removeMark(st.selected);
    });
    let resizing = 0;  // redraw on the next frame, not inside the observer (that resizes again)
    new ResizeObserver(() => {
      cancelAnimationFrame(resizing);
      resizing = requestAnimationFrame(() => { if (document.body.dataset.view === 'datasets') draw(); });
    }).observe(charts);
  }

  async function open() {
    build();
    try {
      st.status = (await getJSON('/api/datasets')).datasets || [];
    } catch (e) {
      $('dsError').textContent = String(e.message || e);
      return;
    }
    const usable = st.status.filter((d) => d.files && d.name !== 'sp500-constituents');
    if (!usable.some((d) => d.name === st.name)) st.name = usable[0]?.name || st.name;
    if (!usable.length) {
      renderControls();
      $('dsEmpty').hidden = false;
      $('dsEmpty').textContent = 'No datasets fetched yet: run python -m massive_collate datasets fetch';
      return;
    }
    if (st.catalog?.name !== st.name) await selectDataset(st.name);
    else { renderControls(); draw(); }
  }

  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'datasets') open(); });
  if (document.body.dataset.view === 'datasets') open();  // the page opened on this view (before this script listened)
  window.Datasets = { open, state: st };
})();
