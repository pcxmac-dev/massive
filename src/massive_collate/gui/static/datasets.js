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
   latest dates. */
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

  let geom = null;  // what the hover layer needs: the x scale and each chart's lines, scales and marks
  function draw() {
    const host = $('dsCharts');
    host.replaceChildren();
    const all = present();
    const lines = shown();
    renderLegend(all);
    $('dsEmpty').hidden = !!lines.length;
    $('dsEmpty').textContent = all.length ? 'Everything is hidden: click a name above to show it.' : 'Pick series on the left.';
    if (!lines.length) { geom = null; renderTable([]); return; }
    const units = [...new Set(lines.map((l) => l.unit))];
    const W = Math.max(320, host.clientWidth);
    const H = Math.max(260, host.clientHeight);
    const axisH = 24;
    const h = Math.max(150, (H - axisH) / units.length);
    // direct end labels when every chart has four lines or fewer (the legend always carries identity)
    const ends = units.every((u) => lines.filter((l) => l.unit === u).length <= 4);
    const left = 64, right = ends && W > 640 ? 170 : 16, top = 20, bottom = 8;
    const lo = Math.min(...lines.map((l) => l.t[0])), hi = Math.max(...lines.map((l) => l.t[l.t.length - 1]));
    const x = (t) => left + (t - lo) / Math.max(1, hi - lo) * (W - left - right);
    geom = { x, lo, hi, left, right, W, charts: [] };
    units.forEach((unit, gi) => {
      const ls = lines.filter((l) => l.unit === unit);
      const last = gi === units.length - 1;
      const ch = h + (last ? axisH : 0);
      const svg = el('svg', { width: W, height: ch, class: 'ds-chart', role: 'img',
        'aria-label': `${ls.map((l) => l.label).join(', ')} (${unit}), ${day(lo)} to ${day(hi)}` }, host);
      const vs = ls.flatMap((l) => l.v).filter(Number.isFinite);
      let vlo = Math.min(...vs), vhi = Math.max(...vs);
      const log = logOn() && vlo > 0;
      let y;
      const y0 = top, y1 = h - bottom;
      if (log) {
        const a = Math.log10(vlo), b = Math.log10(vhi), pad = (b - a || 1) * 0.04;
        y = (v) => y1 - (Math.log10(v) - (a - pad)) / ((b + pad) - (a - pad)) * (y1 - y0);
      } else {
        const pad = (vhi - vlo || Math.abs(vhi) || 1) * 0.05;
        vlo -= pad; vhi += pad;
        y = (v) => y1 - (v - vlo) / (vhi - vlo) * (y1 - y0);
      }
      const ticks = log ? logTicks(Math.min(...vs), Math.max(...vs)) : niceTicks(vlo, vhi);
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
      for (const l of ls) {
        el('path', { d: pathOf(l, x, y), class: `ds-line s${l.slot}${l.dash ? ' dash' : ''}` }, svg);
      }
      if (ends && right > 16) {  // direct labels at the line ends, nudged apart
        const labs = ls.map((l) => ({ l, y: y(l.v[l.v.length - 1]) })).sort((a, b) => a.y - b.y);
        for (let i = 1; i < labs.length; i++) labs[i].y = Math.max(labs[i].y, labs[i - 1].y + 14);
        for (let i = labs.length - 1; i >= 0; i--) labs[i].y = Math.min(labs[i].y, y1 - (labs.length - 1 - i) * 14);
        for (const { l, y: yy } of labs) {
          const ex = x(l.t[l.t.length - 1]);
          el('line', { x1: ex + 3, x2: ex + 13, y1: yy, y2: yy, class: `ds-key s${l.slot}${l.dash ? ' dash' : ''}` }, svg);
          const tx = el('text', { x: ex + 17, y: yy + 4, class: 'ds-end' }, svg);
          const label = l.short || l.label;
          const name = label.length > 18 ? `${label.slice(0, 17)}…` : label;
          tx.textContent = `${fmt(l.v[l.v.length - 1], unit)} `;
          el('tspan', { class: 'muted' }, tx).textContent = name;
        }
      }
      const hover = el('g', { class: 'ds-hover', visibility: 'hidden' }, svg);
      const cross = el('line', { y1: y0, y2: y1, class: 'cross' }, hover);
      const dots = ls.map((l) => el('circle', { r: 4, class: `ds-dot s${l.slot}` }, hover));
      geom.charts.push({ svg, ls, y, hover, cross, dots, unit });
    });
    geom.allT = [...new Set(lines.flatMap((l) => l.t))].sort((a, b) => a - b);
    renderTable(lines);
    if (st.hoverT != null) showHover(st.hoverT);
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
        <span id="dsError" class="down small" role="alert"></span>
      </div>
      <div class="ds-body">
        <div class="ds-pick"><input id="dsSearch" type="search" autocomplete="off" aria-label="Search">
          <div id="dsList" class="ds-list" role="listbox" aria-label="Series"></div></div>
        <div id="dsMain" class="ds-main">
          <div id="dsLegend" class="ds-legend"></div>
          <div id="dsCharts" class="ds-charts" tabindex="0" aria-label="Chart: arrow keys move along the dates"></div>
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
      save(); renderPicker(); refresh();
    });
    const charts = $('dsCharts');
    charts.addEventListener('pointermove', (e) => {
      if (!geom) return;
      const r = charts.getBoundingClientRect();
      const px = e.clientX - r.left;
      if (px < geom.left || px > geom.W - geom.right) { hideHover(); return; }
      const t = geom.lo + (px - geom.left) / (geom.W - geom.left - geom.right) * (geom.hi - geom.lo);
      showHover(geom.allT[nearest(geom.allT, t)]);
    });
    charts.addEventListener('pointerleave', hideHover);
    charts.addEventListener('keydown', (e) => {
      if (!geom || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
      e.preventDefault();
      const i = st.hoverT == null ? geom.allT.length - 1 : nearest(geom.allT, st.hoverT);
      const j = e.key === 'Home' ? 0 : e.key === 'End' ? geom.allT.length - 1
        : Math.max(0, Math.min(geom.allT.length - 1, i + (e.key === 'ArrowLeft' ? -1 : 1) * (e.shiftKey ? 20 : 1)));
      showHover(geom.allT[j]);
    });
    charts.addEventListener('blur', hideHover);
    new ResizeObserver(() => { if (document.body.dataset.view === 'datasets') draw(); }).observe(charts);
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
  window.Datasets = { open, state: st };
})();
