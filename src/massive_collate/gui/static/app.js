'use strict';
/* Massive Charts: TradingView-style front end over the local data API. */

const kc = window.klinecharts;
const TZ = 'America/New_York';
const MERGED = '*';
const PAGE = 20000;  // bars per request; zooming/scrolling out loads older pages as needed
const POLL_MS = 60_000;

// -- per-viewer preferences (localStorage; the app works without it) --------
const prefs = {
  get(key, fallback) {
    try { const v = localStorage.getItem('mc:' + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem('mc:' + key, JSON.stringify(value)); } catch { /* ignore */ } },
};

const state = {
  ticker: null,
  source: MERGED,
  period: prefs.get('period', { type: 'minute', span: 1 }),
  session: prefs.get('session', 'regular'),
  theme: prefs.get('theme', 'dark'),
  indicators: prefs.get('indicators', ['VOL']),
  chartType: prefs.get('chartType', 'candles'),
  adjust: prefs.get('adjust', 'none'),
  typeParams: prefs.get('typeParams', {}),  // per type: { box, reversal, lines }
  styleUsed: null,
  approximate: null,
  magnet: prefs.get('magnet', 'normal'),
  meta: null,
  symbols: [],
  selectedOverlay: null,
  drawingId: null,
  loadToken: 0,
  restoredFor: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null) url.searchParams.set(k, v);
  const res = await fetch(url);
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function showError(err) {
  $('error').textContent = err ? String(err.message || err) : '';
}

// -- custom overlay: measure (price change, %, bars, time) ------------------
function fmtDuration(ms) {
  const s = Math.abs(ms) / 1000;
  if (s < 60) return `${s.toFixed(s % 1 ? 3 : 0)}s`;
  const m = s / 60; if (m < 60) return `${m.toFixed(0)}m`;
  const h = m / 60; if (h < 48) return `${h.toFixed(1)}h`;
  return `${(h / 24).toFixed(1)}d`;
}

// Highlight: box between two points with a center line, start/center/end labels,
// and exact statistics for the bars inside it (computed by the server).
const UP = '#26a69a';
const DOWN = '#ef5350';
const MID = '#f0b90b';
const statsTimers = new Map();

function signed(v, digits) { return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`; }

function change(from, to, precision) {
  const pct = from ? ((to - from) / from) * 100 : 0;
  return `${signed(to - from, precision)} (${signed(pct, 2)}%)`;
}

function fmtTime(ts) {
  const p = chart.getPeriod() || { type: 'minute' };
  const opts = { timeZone: TZ, month: 'short', day: '2-digit', hourCycle: 'h23' };
  if (p.type === 'second' || p.type === 'minute' || p.type === 'hour') Object.assign(opts, { hour: '2-digit', minute: '2-digit' });
  if (p.type === 'second') opts.second = '2-digit';
  if (p.type === 'month' || p.type === 'week') opts.year = 'numeric';
  return new Intl.DateTimeFormat('en-US', opts).format(new Date(ts));
}

function compact(v) {
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 3 }).format(v);
}

function statsKey(pa, pb) {
  const p = chart.getPeriod() || {};
  return `${p.type}:${p.span}|${state.session}|${pa.timestamp}|${pb.timestamp}`;
}

function requestRangeStats(overlay, key, pa, pb) {
  clearTimeout(statsTimers.get(overlay.id));
  statsTimers.set(overlay.id, setTimeout(async () => {
    const p = chart.getPeriod();
    const sym = chart.getSymbol();
    if (!p || !sym) return;
    try {
      const stats = await api('/api/range', {
        source: sym.source, ticker: sym.ticker, period: p.type, span: p.span, session: state.session,
        from: pa.timestamp, to: pb.timestamp, adjust: state.adjust,
      });
      chart.overrideOverlay({ id: overlay.id, extendData: { ...(overlay.extendData || {}), key, stats } });
    } catch (err) { showError(err); }
  }, 150));
}

const LABEL_SIZE = 12;
const LABEL_PAD = 5;

function textWidth(text) {
  return (kc.utils?.calcTextWidth ? kc.utils.calcTextWidth(text, LABEL_SIZE) : text.length * 7) + LABEL_PAD * 2;
}

const LINE_H = 17;

// A block of text lines, positioned relative to (x, y) and kept inside the pane.
function label(x, y, lines, bounding, { align = 'left', color = '#ffffff', bg = 'rgba(19,23,34,0.85)', baseline = 'middle' } = {}) {
  const w = Math.max(...lines.map(textWidth));
  const h = LINE_H * lines.length;
  let left = align === 'left' ? x : align === 'right' ? x - w : x - w / 2;
  let top = baseline === 'bottom' ? y - h : baseline === 'top' ? y : y - h / 2;
  left = Math.min(Math.max(left, 2), Math.max(2, bounding.width - w - 2));
  top = Math.min(Math.max(top, 2), Math.max(2, bounding.height - h - 2));
  return { left, top, w, h, lines, color, bg };
}

// Place blocks in order; a block overlapping one already placed moves below it
// (or above, if there is no room below), so labels never cover each other.
function layoutLabels(blocks, bounding) {
  const placed = [];
  const hits = (b) => placed.find((p) => b.left < p.left + p.w && p.left < b.left + b.w && b.top < p.top + p.h && p.top < b.top + b.h);
  for (const b of blocks) {
    for (let i = 0, p; i < 12 && (p = hits(b)); i++) {
      const below = p.top + p.h + 2;
      b.top = below + b.h <= bounding.height ? below : Math.max(2, p.top - b.h - 2);
    }
    placed.push(b);
  }
  return placed.flatMap((b) => b.lines.map((text, i) => ({
    type: 'text',
    ignoreEvent: true,
    attrs: { x: b.left, y: b.top + i * LINE_H, text, align: 'left', baseline: 'top' },
    styles: { color: b.color, backgroundColor: b.bg, size: LABEL_SIZE, borderRadius: 2,
              paddingLeft: LABEL_PAD, paddingRight: LABEL_PAD, paddingTop: 2, paddingBottom: 2 },
  })));
}

kc.registerOverlay({
  name: 'measure',
  totalStep: 3,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  createPointFigures: ({ chart: c, overlay, coordinates, bounding }) => {
    if (coordinates.length < 2 || overlay.points.length < 2) return [];
    const [a, b] = coordinates;
    const [pa, pb] = overlay.points;
    if (pa.value == null || pb.value == null || pa.timestamp == null || pb.timestamp == null) return [];
    const prec = c.getSymbol()?.pricePrecision ?? 2;
    const up = pb.value >= pa.value;
    const color = up ? UP : DOWN;
    const left = Math.min(a.x, b.x), right = Math.max(a.x, b.x);
    const top = Math.min(a.y, b.y), bottom = Math.max(a.y, b.y);
    const cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;
    const midPrice = (pa.value + pb.value) / 2;
    const midTime = Math.round((pa.timestamp + pb.timestamp) / 2);

    const key = statsKey(pa, pb);
    const ext = overlay.extendData || {};
    if (ext.key !== key) requestRangeStats(overlay, key, pa, pb);
    const st = ext.key === key ? ext.stats : null;
    // Exact count from the server once known; until then from the chart's indexes.
    const bars = st ? st.bars
      : (pa.dataIndex != null && pb.dataIndex != null ? Math.abs(pb.dataIndex - pa.dataIndex) + 1 : '…');

    const aLeft = a.x <= b.x;
    const shapes = [
      { type: 'rect', attrs: { x: left, y: top, width: right - left, height: bottom - top },
        styles: { style: 'fill', color: up ? 'rgba(38,166,154,0.15)' : 'rgba(239,83,80,0.15)' } },
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: left, y: cy }, { x: right, y: cy }] },
        styles: { style: 'dashed', dashedValue: [5, 4], color: MID, size: 1 } },
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: cx, y: top }, { x: cx, y: bottom }] },
        styles: { style: 'dashed', dashedValue: [2, 4], color: MID, size: 1 } },
      { type: 'circle', ignoreEvent: true, attrs: { x: cx, y: cy, r: 3.5 }, styles: { style: 'fill', color: MID } },
    ];
    if (right < 0 || left > bounding.width) return shapes; // scrolled out of view: no labels
    // Placement order = priority: the statistics line first, then start, end, center.
    const blocks = [];
    if (st && st.bars) {
      const above = top - 6 - 2 * LINE_H > 0;
      blocks.push(label(cx, above ? top - 6 : bottom + 6, [
        `O ${st.open.toFixed(prec)}  H ${st.high.toFixed(prec)} (${fmtTime(st.highTime)})  `
          + `L ${st.low.toFixed(prec)} (${fmtTime(st.lowTime)})  C ${st.close.toFixed(prec)}`,
        `${st.bars} bars  ·  Vol ${compact(st.volume)}  ·  VWAP ${st.vwap != null ? st.vwap.toFixed(prec) : '-'}`
          + `  ·  Trades ${compact(st.trades)}  ·  H/L range ${st.rangePct != null ? st.rangePct.toFixed(2) + '%' : '-'}`,
      ], bounding, { align: 'center', baseline: above ? 'bottom' : 'top' }));
    }
    blocks.push(label(a.x + (aLeft ? -8 : 8), a.y + (a.y <= cy ? 4 : -4), [
      `Start  ${pa.value.toFixed(prec)}`, fmtTime(pa.timestamp),
    ], bounding, { align: aLeft ? 'right' : 'left', baseline: a.y <= cy ? 'top' : 'bottom' }));
    blocks.push(label(b.x + (aLeft ? 8 : -8), b.y + (b.y <= cy ? 4 : -4), [
      `End  ${pb.value.toFixed(prec)}`, fmtTime(pb.timestamp),
      change(pa.value, pb.value, prec),
      `${bars} bars · ${fmtDuration(pb.timestamp - pa.timestamp)}`,
    ], bounding, { align: aLeft ? 'left' : 'right', color, baseline: b.y <= cy ? 'top' : 'bottom' }));
    blocks.push(label(cx + 8, cy, [
      `Mid  ${midPrice.toFixed(prec)}  ·  ${fmtTime(midTime)}`,
      `start→mid ${change(pa.value, midPrice, prec)}`,
      `mid→end ${change(midPrice, pb.value, prec)}`,
    ], bounding, { color: MID }));
    return [...shapes, ...layoutLabels(blocks, bounding)];
  },
});

// -- chart ---------------------------------------------------------------------
document.body.classList.toggle('light', state.theme === 'light');
// Bars may shrink far below a pixel each, so any interval can be zoomed out to months or years
// of data (KLineChart's default stops at 1 px per bar, ~700 bars on screen).
const MIN_BAR_SPACE = 0.002;  // ~350k bars across a 700 px chart: two years of 1-minute futures
const chart = kc.init('chart', {
  timezone: TZ, styles: state.theme, locale: 'en-US',
  layout: { barSpaceLimit: { min: MIN_BAR_SPACE, max: 80 } },
});

chart.setDataLoader({
  getBars: async ({ type, timestamp, symbol, period, callback }) => {
    if (type === 'backward' || type === 'update') { callback([], { backward: false }); return; }
    const token = type === 'init' ? ++state.loadToken : state.loadToken;
    const params = {
      source: symbol.source, ticker: symbol.ticker, period: period.type, span: period.span,
      session: state.session, limit: PAGE, adjust: state.adjust, ...typeQuery(),
    };
    if (type === 'forward' && timestamp) params.to = timestamp;
    try {
      const res = await api('/api/bars', params);
      if (token !== state.loadToken) return; // a newer symbol/interval was chosen meanwhile
      callback(res.bars, { forward: res.more, backward: false });
      showError(null);
      if (type === 'init') {
        state.styleUsed = res.style;
        state.approximate = res.approximate || null;
        renderTypeParams();
        requestAnimationFrame(drawRolls);
        // After the first render: overlays created in the same tick as the data skew the price scale.
        requestAnimationFrame(() => requestAnimationFrame(restoreDrawings));
        updateStatus(res.bars.length);
      }
    } catch (err) {
      showError(err);
      callback([], { forward: false, backward: false });
    }
  },
  subscribeBar: ({ symbol, period, callback }) => {
    clearInterval(state.poll);
    state.poll = setInterval(async () => {
      const list = chart.getDataList();
      if (!list.length) return;
      try {
        if (PRICE_BASED.has(state.chartType)) return; // path-dependent: refreshed by reloading
        const res = await api('/api/bars', {
          source: symbol.source, ticker: symbol.ticker, period: period.type, span: period.span,
          session: state.session, since: list[list.length - 1].timestamp, adjust: state.adjust, ...typeQuery(),
        });
        res.bars.forEach((bar) => callback(bar));
      } catch { /* the next poll will retry */ }
    }, POLL_MS);
  },
  unsubscribeBar: () => clearInterval(state.poll),
});

window.addEventListener('resize', () => chart.resize());

// -- chart types -------------------------------------------------------------------
// Native KLineChart candle types; transformed types come back from the server as OHLC
// boxes and render as candles; the rest are drawn by the CHART_STYLE renderer below.
const NATIVE = { candles: 'candle_solid', bars: 'ohlc', area: 'area', heikin_ashi: 'candle_solid',
                 renko: 'candle_solid', line_break: 'candle_solid', range: 'candle_solid' };
const TRANSFORMED = new Set(['heikin_ashi', 'renko', 'line_break', 'kagi', 'point_figure', 'range']);
const PRICE_BASED = new Set(['renko', 'line_break', 'kagi', 'point_figure', 'range']);
const LINE = '#2962ff';
const CLEAR = 'rgba(0,0,0,0)';

function typeQuery() {
  if (!TRANSFORMED.has(state.chartType)) return {};
  const p = state.typeParams[state.chartType] || {};
  return { type: state.chartType, box: p.box || undefined, reversal: p.reversal || undefined, lines: p.lines || undefined };
}

// The theme's own candle colours, captured before any chart type hides them.
const DEFAULT_CANDLE = (() => {
  const c = chart.getStyles().candle;
  return { bar: { ...c.bar }, priceMark: { high: { ...c.priceMark.high }, low: { ...c.priceMark.low } } };
})();

function applyChartType() {
  const custom = !NATIVE[state.chartType];
  chart.setStyles(state.theme);
  chart.setStyles({ candle: { type: NATIVE[state.chartType] || 'candle_solid', ...DEFAULT_CANDLE } });
  if (custom) {
    // Keep the (invisible) candles for scaling, crosshair and tooltip; draw the type ourselves.
    chart.setStyles({ candle: {
      bar: { upColor: CLEAR, downColor: CLEAR, noChangeColor: CLEAR, upBorderColor: CLEAR, downBorderColor: CLEAR,
             noChangeBorderColor: CLEAR, upWickColor: CLEAR, downWickColor: CLEAR, noChangeWickColor: CLEAR },
      priceMark: { high: { show: false }, low: { show: false } },
    } });
  }
  if (state.chartType === 'area') chart.setStyles({ candle: { area: { lineColor: LINE } } });
  chart.removeIndicator({ name: 'CHART_STYLE' });
  if (custom) chart.createIndicator({ name: 'CHART_STYLE', paneId: 'candle_pane' }, true);
}

function px(xAxis, i) { return xAxis.convertToPixel(i); }

kc.registerIndicator({
  name: 'CHART_STYLE',
  shortName: '',
  series: 'price',
  figures: [],
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: () => ({ name: '', calcParamsText: '', features: [], legends: [] }),
  draw: ({ ctx, chart: c, bounding, xAxis, yAxis }) => {
    const data = c.getDataList();
    const { from, to } = c.getVisibleRange();
    if (!data.length || to <= from) return true;
    const bar = c.getBarSpace().bar;
    const half = Math.max(1, bar * 0.4);
    const y = (v) => yAxis.convertToPixel(v);
    const type = state.chartType;
    ctx.save();
    ctx.setLineDash([]);  // the chart leaves its own dash pattern set on the shared context
    ctx.globalAlpha = 1;
    ctx.lineJoin = 'round';
    const lo = Math.max(0, from - 1), hi = Math.min(data.length, to + 1);

    const polyline = (values, color, width = 1.5, step = false) => {
      ctx.beginPath();
      for (let i = lo; i < hi; i++) {
        const x = px(xAxis, i), yy = y(values(data[i]));
        if (i === lo) ctx.moveTo(x, yy);
        else if (step) { ctx.lineTo(x, y(values(data[i - 1]))); ctx.lineTo(x, yy); }
        else ctx.lineTo(x, yy);
      }
      ctx.strokeStyle = color; ctx.lineWidth = width; ctx.stroke();
    };
    const dirColor = (i) => (i > 0 && data[i].close < data[i - 1].close ? DOWN : UP);

    if (type === 'line' || type === 'line_markers' || type === 'step') {
      polyline((d) => d.close, LINE, 1.5, type === 'step');
      if (type === 'line_markers') {
        ctx.fillStyle = LINE;
        for (let i = lo; i < hi; i++) { ctx.beginPath(); ctx.arc(px(xAxis, i), y(data[i].close), 2.5, 0, Math.PI * 2); ctx.fill(); }
      }
    } else if (type === 'hlc_area') {
      ctx.beginPath();
      for (let i = lo; i < hi; i++) ctx[i === lo ? 'moveTo' : 'lineTo'](px(xAxis, i), y(data[i].high));
      for (let i = hi - 1; i >= lo; i--) ctx.lineTo(px(xAxis, i), y(data[i].low));
      ctx.closePath(); ctx.fillStyle = 'rgba(41,98,255,0.15)'; ctx.fill();
      polyline((d) => d.high, 'rgba(38,166,154,0.8)', 1);
      polyline((d) => d.low, 'rgba(239,83,80,0.8)', 1);
      polyline((d) => d.close, LINE, 1.5);
    } else if (type === 'baseline') {
      const range = yAxis.getRange();
      const base = (range.from + range.to) / 2;  // TradingView default: 50% of the visible scale
      const by = y(base);
      for (const [clipTop, clipH, fill, stroke] of [[0, by, 'rgba(38,166,154,0.18)', UP], [by, bounding.height - by, 'rgba(239,83,80,0.18)', DOWN]]) {
        ctx.save();
        ctx.beginPath(); ctx.rect(0, clipTop, bounding.width, clipH); ctx.clip();
        ctx.beginPath();
        for (let i = lo; i < hi; i++) ctx[i === lo ? 'moveTo' : 'lineTo'](px(xAxis, i), y(data[i].close));
        ctx.lineTo(px(xAxis, hi - 1), by); ctx.lineTo(px(xAxis, lo), by); ctx.closePath();
        ctx.fillStyle = fill; ctx.fill();
        polyline((d) => d.close, stroke, 1.5);
        ctx.restore();
      }
      ctx.setLineDash([4, 4]); ctx.strokeStyle = '#787b86'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, by); ctx.lineTo(bounding.width, by); ctx.stroke(); ctx.setLineDash([]);
    } else if (type === 'columns') {
      for (let i = lo; i < hi; i++) {
        const x = px(xAxis, i), top = y(data[i].close);
        ctx.fillStyle = dirColor(i); ctx.globalAlpha = 0.7;
        ctx.fillRect(x - half, top, half * 2, bounding.height - top);
      }
      ctx.globalAlpha = 1;
    } else if (type === 'high_low') {
      ctx.fillStyle = LINE;
      for (let i = lo; i < hi; i++) {
        const x = px(xAxis, i), t = y(data[i].high), b = y(data[i].low);
        ctx.fillRect(x - half, t, half * 2, Math.max(1, b - t));
      }
    } else if (type === 'hollow' || type === 'volume_candles') {
      let maxVol = 0;
      if (type === 'volume_candles') for (let i = lo; i < hi; i++) maxVol = Math.max(maxVol, data[i].volume || 0);
      for (let i = lo; i < hi; i++) {
        const d = data[i], x = px(xAxis, i);
        const color = type === 'hollow' ? dirColor(i) : (d.close >= d.open ? UP : DOWN);
        const w = type === 'volume_candles' && maxVol ? Math.max(1, half * 2 * (d.volume || 0) / maxVol) : half * 2;
        const top = y(Math.max(d.open, d.close)), bottom = y(Math.min(d.open, d.close));
        ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x + 0.5, y(d.high)); ctx.lineTo(x + 0.5, top);
        ctx.moveTo(x + 0.5, bottom); ctx.lineTo(x + 0.5, y(d.low)); ctx.stroke();
        if (type === 'hollow' && d.close > d.open) ctx.strokeRect(x - w / 2 + 0.5, top + 0.5, Math.max(1, w - 1), Math.max(1, bottom - top - 1));
        else ctx.fillRect(x - w / 2, top, w, Math.max(1, bottom - top));
      }
    } else if (type === 'kagi') {
      for (let i = lo; i < hi; i++) {
        const d = data[i], x = px(xAxis, i);
        if (i > 0) {  // shoulder/waist connector from the previous line
          ctx.strokeStyle = data[i - 1].yang ? UP : DOWN; ctx.lineWidth = data[i - 1].yang ? 3 : 1;
          ctx.beginPath(); ctx.moveTo(px(xAxis, i - 1), y(d.open)); ctx.lineTo(x, y(d.open)); ctx.stroke();
        }
        const segs = d.switch != null
          ? [[d.open, d.switch, d.yang_start], [d.switch, d.close, d.yang]]
          : [[d.open, d.close, d.yang]];
        for (const [a, b, yang] of segs) {
          ctx.strokeStyle = yang ? UP : DOWN; ctx.lineWidth = yang ? 3 : 1;
          ctx.beginPath(); ctx.moveTo(x, y(a)); ctx.lineTo(x, y(b)); ctx.stroke();
        }
      }
    } else if (type === 'point_figure') {
      const box = state.styleUsed?.box;
      if (box) {
        const size = Math.max(3, Math.min(half, Math.abs(y(0) - y(box)) / 2 - 1));
        ctx.lineWidth = 1.5;
        for (let i = lo; i < hi; i++) {
          const d = data[i], x = px(xAxis, i);
          ctx.strokeStyle = d.up ? UP : DOWN;
          for (let v = d.low; v <= d.high + box / 2; v += box) {
            const yy = y(v);
            ctx.beginPath();
            if (d.up) { ctx.moveTo(x - size, yy - size); ctx.lineTo(x + size, yy + size); ctx.moveTo(x + size, yy - size); ctx.lineTo(x - size, yy + size); }
            else ctx.arc(x, yy, size, 0, Math.PI * 2);
            ctx.stroke();
          }
        }
      }
    }
    ctx.restore();
    return true;
  },
});

function renderTypeParams() {
  const box = $('typeParams');
  box.innerHTML = '';
  const t = state.chartType;
  const used = state.styleUsed || {};
  const p = state.typeParams[t] || {};
  const input = (key, label, placeholder, step) => {
    const l = document.createElement('label');
    l.textContent = label;
    const i = document.createElement('input');
    i.type = 'number'; i.min = '0'; i.step = step; i.placeholder = placeholder; i.value = p[key] ?? '';
    i.title = `${label} (empty = ${placeholder})`;
    i.addEventListener('change', () => {
      state.typeParams[t] = { ...(state.typeParams[t] || {}), [key]: i.value ? Number(i.value) : undefined };
      prefs.set('typeParams', state.typeParams); markDirty();
      chart.resetData();
    });
    l.append(i);
    box.append(l);
  };
  if (t === 'renko' || t === 'range' || t === 'point_figure') {
    input('box', t === 'range' ? 'Range' : 'Box', used.box != null ? `ATR ${used.box}` : 'ATR(14)', 'any');
  }
  if (t === 'kagi') input('reversal', 'Reversal', used.reversal != null ? `ATR ${used.reversal}` : 'ATR(14)', 'any');
  if (t === 'point_figure') input('reversal', 'Reversal boxes', '3', '1');
  if (t === 'line_break') input('lines', 'Lines', '3', '1');
  if (state.approximate && t === 'range') {
    const s = document.createElement('span');
    s.className = 'approx'; s.textContent = 'approx.'; s.title = state.approximate;
    box.append(s);
  }
}

$('typeSelect').value = state.chartType;
$('typeSelect').addEventListener('change', (e) => {
  const wasTransformed = TRANSFORMED.has(state.chartType);
  state.chartType = e.target.value;
  prefs.set('chartType', state.chartType); markDirty();
  state.styleUsed = null;
  applyChartType();
  renderTypeParams();
  if (wasTransformed || TRANSFORMED.has(state.chartType)) chart.resetData();
});

// -- symbol selection ----------------------------------------------------------
async function loadSymbols() {
  state.symbols = await api('/api/symbols');
}

function symbolInfo(ticker) {
  return state.symbols.find((s) => s.ticker === ticker) || { ticker, feeds: [] };
}

async function selectSymbol(ticker, source = MERGED) {
  const meta = await api('/api/meta', { source, ticker, adjust: state.adjust });
  $('ethBtn').hidden = meta.aroundTheClock;  // fx and futures trade around the clock
  $('adjustSelect').hidden = meta.asset !== 'future';
  saveDrawings();
  state.ticker = meta.ticker;
  state.source = source;
  state.meta = meta;
  state.restoredFor = null;
  prefs.set('last', { ticker: meta.ticker, source });
  markDirty();

  if (!meta.periods.some((p) => p.type === state.period.type && p.span === state.period.span)) {
    state.period = meta.periods.find((p) => p.type === 'minute' && p.span === 1) || meta.periods[0];
  }
  const info = symbolInfo(meta.ticker);
  $('symbolBtn').textContent = info.exchange ? `${meta.ticker} · ${info.exchangeName || info.exchange}` : meta.ticker;
  renderFeeds(info);
  renderPeriods();
  chart.removeOverlay({ groupId: 'user' });
  chart.setPeriod(state.period);
  chart.setSymbol({
    ticker: meta.ticker, source, name: info.name || meta.ticker,
    pricePrecision: meta.pricePrecision, volumePrecision: meta.volumePrecision,
  });
}

function renderFeeds(info) {
  const sel = $('feedSelect');
  sel.innerHTML = '';
  const feeds = info.feeds || [];
  const add = (value, label) => { const o = document.createElement('option'); o.value = value; o.textContent = label; sel.append(o); };
  add(MERGED, feeds.length > 1 ? `All feeds (${feeds.map((f) => f.source).join(' > ')})` : `Feed: ${feeds[0]?.source || '-'}`);
  if (feeds.length > 1) feeds.forEach((f) => add(f.source, `Only ${f.source}`));
  sel.value = state.source;
  sel.hidden = !feeds.length;
}

$('feedSelect').addEventListener('change', (e) => selectSymbol(state.ticker, e.target.value).catch(showError));

$('adjustSelect').value = state.adjust;
$('adjustSelect').addEventListener('change', (e) => {
  state.adjust = e.target.value;
  prefs.set('adjust', state.adjust); markDirty();
  selectSymbol(state.ticker, state.source).catch(showError);
});

// Continuous futures: a dashed line and label at every contract roll.
kc.registerOverlay({
  name: 'rollMarker',
  totalStep: 2,
  lock: true,
  needDefaultPointFigure: false,
  createPointFigures: ({ overlay, coordinates, bounding }) => {
    if (!coordinates.length) return [];
    const x = coordinates[0].x;
    const r = overlay.extendData || {};
    const gap = r.gap == null ? '' : `  gap ${r.gap >= 0 ? '+' : ''}${Number(r.gap).toPrecision(6)}`;
    return [
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x, y: 0 }, { x, y: bounding.height }] },
        styles: { style: 'dashed', dashedValue: [3, 3], color: '#9598a1', size: 1 } },
      { type: 'text', ignoreEvent: true, attrs: { x: x + 4, y: 4, text: `roll ${r.from} → ${r.to}${gap}`, align: 'left', baseline: 'top' },
        styles: { color: '#d1d4dc', backgroundColor: 'rgba(19,23,34,0.8)', size: 11, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 } },
    ];
  },
});

function drawRolls() {
  chart.removeOverlay({ groupId: 'rolls' });
  for (const r of state.meta?.rolls || []) {
    chart.createOverlay({ name: 'rollMarker', groupId: 'rolls', lock: true, extendData: r,
                          points: [{ timestamp: r.time, value: 0 }] });
  }
}

function openSymbolDialog() {
  const dlg = $('symbolDialog');
  $('symbolSearch').value = '';
  renderSymbolList('');
  dlg.showModal();
  $('symbolSearch').focus();
}

function renderSymbolList(query) {
  const q = query.trim().toLowerCase();
  const list = $('symbolList');
  list.innerHTML = '';
  const matches = state.symbols.filter((s) =>
    !q || [s.ticker, s.name, s.exchange, s.exchangeName].some((v) => v && v.toLowerCase().includes(q)));
  if (!matches.length) {
    list.innerHTML = '<div class="empty">No stored symbols match. Add one with: python -m massive_collate watch add TICKER::EXCHANGE</div>';
    return;
  }
  const byExchange = new Map();
  for (const s of matches) {
    const key = s.exchangeName || s.exchange || 'Other';
    if (!byExchange.has(key)) byExchange.set(key, []);
    byExchange.get(key).push(s);
  }
  for (const [exchange, items] of [...byExchange].sort((a, b) => a[0].localeCompare(b[0]))) {
    const h = document.createElement('h4');
    h.textContent = exchange;
    list.append(h);
    for (const s of items) {
      const row = document.createElement('div');
      row.className = 'sym-row';
      row.tabIndex = 0;
      const tk = document.createElement('span'); tk.className = 'tk'; tk.textContent = s.ticker;
      const nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = s.name || '';
      row.append(tk, nm);
      for (const chipText of [s.kind, ...s.feeds.map((f) => `${f.source}: ${f.data.join('/')}`)]) {
        if (!chipText) continue;
        const c = document.createElement('span'); c.className = 'chip'; c.textContent = chipText; row.append(c);
      }
      const choose = () => { $('symbolDialog').close(); selectSymbol(s.ticker, MERGED).catch(showError); };
      row.addEventListener('click', choose);
      row.addEventListener('keydown', (e) => { if (e.key === 'Enter') choose(); });
      list.append(row);
    }
  }
}

$('symbolBtn').addEventListener('click', openSymbolDialog);
$('symbolSearch').addEventListener('input', (e) => renderSymbolList(e.target.value));
$('symbolSearch').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { const first = $('symbolList').querySelector('.sym-row'); if (first) first.click(); }
});

// -- intervals and session -----------------------------------------------------
const PERIOD_LABEL = { second: 's', minute: 'm', hour: 'h', day: 'D', week: 'W', month: 'M' };

function renderPeriods() {
  const box = $('periods');
  box.innerHTML = '';
  for (const p of state.meta?.periods || []) {
    const b = document.createElement('button');
    b.textContent = p.type === 'day' || p.type === 'week' || p.type === 'month' ? PERIOD_LABEL[p.type] : `${p.span}${PERIOD_LABEL[p.type]}`;
    b.title = `${p.span} ${p.type}${p.span > 1 ? 's' : ''}`;
    b.classList.toggle('active', p.type === state.period.type && p.span === state.period.span);
    b.addEventListener('click', () => {
      state.period = p;
      prefs.set('period', p); markDirty();
      renderPeriods();
      chart.setPeriod(p);
    });
    box.append(b);
  }
}

function renderSession() {
  $('ethBtn').classList.toggle('on', state.session === 'extended');
}

$('ethBtn').addEventListener('click', () => {
  state.session = state.session === 'extended' ? 'regular' : 'extended';
  prefs.set('session', state.session); markDirty();
  renderSession();
  chart.resetData();
});

// -- indicators ----------------------------------------------------------------
const MAIN_INDICATORS = [['MA', 'Moving average'], ['EMA', 'Exponential MA'], ['SMA', 'Smoothed MA'],
  ['BOLL', 'Bollinger bands'], ['SAR', 'Parabolic SAR'], ['AVP', 'Average price (VWAP-like)'], ['BBI', 'Bull and bear index']];
const SUB_INDICATORS = [['VOL', 'Volume'], ['MACD', 'MACD'], ['RSI', 'RSI'], ['KDJ', 'KDJ'], ['OBV', 'On-balance volume'],
  ['CCI', 'CCI'], ['WR', 'Williams %R'], ['DMI', 'DMI'], ['TRIX', 'TRIX'], ['MTM', 'Momentum'], ['ROC', 'Rate of change']];
const isMain = (name) => MAIN_INDICATORS.some(([n]) => n === name);

function addIndicator(name) {
  if (isMain(name)) chart.createIndicator({ name, paneId: 'candle_pane' }, true);
  else chart.createIndicator(name);
}

function renderIndicatorMenu() {
  const menu = $('indMenu');
  menu.innerHTML = '';
  for (const [title, items] of [['On price', MAIN_INDICATORS], ['Below price', SUB_INDICATORS]]) {
    const h = document.createElement('h4'); h.textContent = title; menu.append(h);
    for (const [name, label] of items) {
      const l = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = state.indicators.includes(name);
      cb.addEventListener('change', () => {
        if (cb.checked) { addIndicator(name); state.indicators.push(name); }
        else { chart.removeIndicator({ name }); state.indicators = state.indicators.filter((n) => n !== name); }
        prefs.set('indicators', state.indicators); markDirty();
      });
      l.append(cb, document.createTextNode(`${name}  ${label}`));
      menu.append(l);
    }
  }
}

$('indBtn').addEventListener('click', (e) => { e.stopPropagation(); $('indMenu').hidden = !$('indMenu').hidden; });
document.addEventListener('click', (e) => { if (!$('indMenu').contains(e.target)) $('indMenu').hidden = true; });

// -- drawing tools ---------------------------------------------------------------
const ICON = {
  cursor: '<path d="M5 3l12 8-5 1.5L9.5 18z"/>',
  segment: '<path d="M4 17L16 5"/><circle cx="4" cy="17" r="1.6"/><circle cx="16" cy="5" r="1.6"/>',
  rayLine: '<path d="M4 17L20 3"/><circle cx="4" cy="17" r="1.6"/>',
  straightLine: '<path d="M1 19L19 1"/>',
  horizontalStraightLine: '<path d="M1 10h18"/>',
  horizontalRayLine: '<path d="M4 10h15"/><circle cx="4" cy="10" r="1.6"/>',
  verticalStraightLine: '<path d="M10 1v18"/>',
  priceLine: '<path d="M2 10h11"/><path d="M13 7h6v6h-6z"/>',
  parallelStraightLine: '<path d="M2 13L14 3M6 18L18 8"/>',
  priceChannelLine: '<path d="M2 12L18 4M2 17L18 9"/><path d="M2 14.5L18 6.5" stroke-dasharray="2 2"/>',
  fibonacciLine: '<path d="M2 4h16M2 8h16M2 12h16M2 16h16"/>',
  measure: '<rect x="2.5" y="4.5" width="15" height="11" fill="currentColor" fill-opacity=".25"/>'
    + '<path d="M2.5 10h15" stroke-dasharray="2 1.5"/><circle cx="10" cy="10" r="1.3" fill="currentColor"/>',
  simpleAnnotation: '<path d="M4 4h12v9H9l-3 3v-3H4z"/>',
  magnet: '<path d="M5 3v7a5 5 0 0010 0V3h-3v7a2 2 0 01-4 0V3z"/>',
  lock: '<rect x="4" y="9" width="12" height="9" rx="1"/><path d="M7 9V6a3 3 0 016 0v3"/>',
  eye: '<path d="M1 10s3.5-6 9-6 9 6 9 6-3.5 6-9 6-9-6-9-6z"/><circle cx="10" cy="10" r="2.5"/>',
  trash: '<path d="M3 5h14M8 5V3h4v2M5 5l1 13h8l1-13"/>',
};
const TOOLS = [
  ['cursor', 'Cursor (Esc)'],
  ['measure', 'Highlight (Alt+H): box with center line and start / center / end statistics'], null,
  ['segment', 'Trend line'], ['rayLine', 'Ray'], ['straightLine', 'Extended line'],
  ['horizontalStraightLine', 'Horizontal line'], ['horizontalRayLine', 'Horizontal ray'],
  ['verticalStraightLine', 'Vertical line'], ['priceLine', 'Price line'], null,
  ['parallelStraightLine', 'Parallel channel'], ['priceChannelLine', 'Price channel'],
  ['fibonacciLine', 'Fibonacci retracement'],
  ['simpleAnnotation', 'Note'], null,
];

function svg(name) {
  return `<svg viewBox="0 0 20 20" aria-hidden="true">${ICON[name]}</svg>`;
}

function overlayEvents() {
  return {
    onDrawEnd: () => { state.drawingId = null; setActiveTool('cursor'); saveDrawings(); },
    onPressedMoveEnd: saveDrawings,
    onRemoved: () => { state.selectedOverlay = null; saveDrawings(); },
    onSelected: (e) => { state.selectedOverlay = e.overlay.id; },
    onDeselected: () => { state.selectedOverlay = null; },
    onRightClick: (e) => { chart.removeOverlay({ id: e.overlay.id }); },
  };
}

function startDrawing(name) {
  cancelDrawing();
  if (name === 'cursor') { setActiveTool('cursor'); return; }
  const create = { name, groupId: 'user', mode: state.magnet, ...overlayEvents() };
  if (name === 'simpleAnnotation') {
    const text = prompt('Note text');
    if (!text) return;
    create.extendData = text;
  }
  state.drawingId = chart.createOverlay(create);
  setActiveTool(name);
}

function cancelDrawing() {
  if (state.drawingId) { chart.removeOverlay({ id: state.drawingId }); state.drawingId = null; }
}

function setActiveTool(name) {
  document.querySelectorAll('#tools button[data-tool]').forEach((b) => b.classList.toggle('active', b.dataset.tool === name));
}

const MAGNET_MODES = ['normal', 'weak_magnet', 'strong_magnet'];

function renderTools() {
  const nav = $('tools');
  nav.innerHTML = '';
  for (const t of TOOLS) {
    if (!t) { nav.append(document.createElement('hr')); continue; }
    const [name, title] = t;
    const b = document.createElement('button');
    b.dataset.tool = name; b.title = title; b.setAttribute('aria-label', title); b.innerHTML = svg(name);
    b.addEventListener('click', () => startDrawing(name));
    nav.append(b);
  }
  const action = (icon, title, fn) => {
    const b = document.createElement('button');
    b.title = title; b.setAttribute('aria-label', title); b.innerHTML = svg(icon);
    b.addEventListener('click', () => fn(b));
    nav.append(b);
    return b;
  };
  const magnet = action('magnet', '', (b) => {
    state.magnet = MAGNET_MODES[(MAGNET_MODES.indexOf(state.magnet) + 1) % MAGNET_MODES.length];
    prefs.set('magnet', state.magnet);
    paintMagnet(b);
  });
  const paintMagnet = (b) => {
    b.classList.toggle('active', state.magnet !== 'normal');
    b.title = `Magnet: ${state.magnet.replace('_', ' ')} (snaps to open/high/low/close)`;
  };
  paintMagnet(magnet);
  action('lock', 'Lock / unlock all drawings', () => {
    const user = chart.getOverlays({ groupId: 'user' });
    const lock = !user.every((o) => o.lock);
    user.forEach((o) => chart.overrideOverlay({ id: o.id, lock }));
    saveDrawings();
  });
  action('eye', 'Show / hide all drawings', () => {
    const user = chart.getOverlays({ groupId: 'user' });
    const visible = !user.every((o) => o.visible);
    user.forEach((o) => chart.overrideOverlay({ id: o.id, visible }));
    saveDrawings();
  });
  action('trash', 'Delete all drawings on this symbol', () => {
    if (confirm(`Delete all drawings on ${state.ticker}?`)) { chart.removeOverlay({ groupId: 'user' }); saveDrawings(); }
  });
  setActiveTool('cursor');
}

// Drawings belong to the current layout, per ticker, as (timestamp, price) points so they
// line up on every interval.
function saveDrawings() {
  if (!state.ticker || state.restoredFor !== state.ticker || !layout.current) return;
  const user = chart.getOverlays({ groupId: 'user' }).filter((o) => o.id !== state.drawingId);
  const drawings = layout.current.data.drawings || (layout.current.data.drawings = {});
  drawings[state.ticker] = user.map((o) => ({
    name: o.name, lock: o.lock, visible: o.visible, extendData: o.extendData ?? null, styles: o.styles ?? null,
    points: o.points.map((p) => ({ timestamp: p.timestamp, value: p.value })),
  }));
  markDirty();
}

function restoreDrawings() {
  if (state.restoredFor === state.ticker) return;
  chart.removeOverlay({ groupId: 'user' });
  for (const d of layout.current?.data.drawings?.[state.ticker] || []) {
    chart.createOverlay({
      name: d.name, groupId: 'user', points: d.points, lock: d.lock, visible: d.visible,
      extendData: d.extendData ?? undefined, styles: d.styles ?? undefined, ...overlayEvents(),
    });
  }
  state.restoredFor = state.ticker;
}

// -- layouts (saved markups), like TradingView's layout menu ------------------------
// A layout is the whole chart setup: symbol, feed, interval, chart type and settings,
// session, futures adjustment, indicators and every symbol's drawings. Stored by the
// local server in the database; up to 5.
const layout = {
  current: null,  // { id, name, data }
  list: [],
  max: 5,
  dirty: false,
  autosave: prefs.get('autosave', true),
  timer: null,
};

async function apiWrite(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Massive-Charts': '1' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function snapshot() {
  return {
    version: 1,
    symbol: state.ticker ? { ticker: state.ticker, source: state.source } : null,
    period: state.period,
    session: state.session,
    chartType: state.chartType,
    typeParams: state.typeParams,
    adjust: state.adjust,
    indicators: [...state.indicators],
    drawings: layout.current?.data.drawings || {},
  };
}

function markDirty() {
  if (!layout.current || state.applyingLayout) return;
  layout.dirty = true;
  renderLayoutButton();
  if (layout.autosave) {
    clearTimeout(layout.timer);
    layout.timer = setTimeout(() => saveLayout().catch(showError), 1500);
  }
}

async function saveLayout() {
  if (!layout.current) return;
  clearTimeout(layout.timer);
  layout.current.data = snapshot();
  const saved = await apiWrite('PUT', `/api/layouts/${layout.current.id}`, { data: layout.current.data });
  layout.current = saved;
  layout.dirty = false;
  await refreshLayoutList();
}

async function refreshLayoutList() {
  const res = await api('/api/layouts');
  layout.list = res.layouts;
  layout.max = res.max;
  renderLayoutButton();
}

function renderLayoutButton() {
  $('layoutName').textContent = layout.current ? layout.current.name : 'Layout';
  $('layoutDirty').hidden = !layout.dirty;
}

async function applyLayout(l) {
  state.applyingLayout = true;
  try {
    const d = l.data || {};
    layout.current = { ...l, data: { drawings: {}, ...d } };
    layout.dirty = false;
    prefs.set('layoutId', l.id);
    if (d.period) state.period = d.period;
    if (d.session) state.session = d.session;
    if (d.chartType) state.chartType = d.chartType;
    if (d.typeParams) state.typeParams = d.typeParams;
    if (d.adjust) state.adjust = d.adjust;
    if (Array.isArray(d.indicators)) {
      for (const name of state.indicators) chart.removeIndicator({ name });
      state.indicators = [...d.indicators];
      state.indicators.forEach(addIndicator);
    }
    $('typeSelect').value = state.chartType;
    $('adjustSelect').value = state.adjust;
    renderSession();
    applyChartType();
    renderTypeParams();
    renderIndicatorMenu();
    renderLayoutButton();
    state.restoredFor = null;
    const sym = d.symbol && state.symbols.some((s) => s.ticker === d.symbol.ticker) ? d.symbol
      : state.ticker ? { ticker: state.ticker, source: state.source }
      : state.symbols.length ? { ticker: state.symbols[0].ticker, source: MERGED } : null;
    if (sym) await selectSymbol(sym.ticker, sym.source);
  } finally {
    state.applyingLayout = false;
  }
}

async function loadLayout(id) {
  if (layout.dirty) await saveLayout();
  await applyLayout(await api(`/api/layouts/${id}`));
}

function askName(message, fallback) {
  const name = prompt(message, fallback);
  return name && name.trim() ? name.trim().slice(0, 60) : null;
}

async function createLayout(copy) {
  if (layout.list.length >= layout.max) {
    alert(`You can keep up to ${layout.max} layouts. Delete one first.`);
    return;
  }
  const name = askName(copy ? 'Name for the copy' : 'Name for the new layout',
                       copy ? `${layout.current?.name || 'Layout'} copy` : `Layout ${layout.list.length + 1}`);
  if (!name) return;
  if (layout.dirty) await saveLayout();
  const data = copy ? snapshot() : { ...snapshot(), drawings: {} };
  const made = await apiWrite('POST', '/api/layouts', { name, data });
  await refreshLayoutList();
  await applyLayout(made);
}

async function renameLayout() {
  if (!layout.current) return;
  const name = askName('Rename layout', layout.current.name);
  if (!name) return;
  layout.current = await apiWrite('PUT', `/api/layouts/${layout.current.id}`, { name });
  await refreshLayoutList();
}

async function deleteLayout(id) {
  const target = layout.list.find((l) => l.id === id);
  if (!target || !confirm(`Delete layout "${target.name}" and its drawings?`)) return;
  await apiWrite('DELETE', `/api/layouts/${id}`);
  await refreshLayoutList();
  if (layout.current?.id === id) {
    layout.current = null;
    if (layout.list.length) await loadLayout(layout.list[0].id);
    else await ensureLayout();
  }
}

function menuItem(label, onClick, key) {
  const b = document.createElement('button');
  b.className = 'menu-item';
  b.append(document.createTextNode(label));
  if (key) { const k = document.createElement('span'); k.className = 'key'; k.textContent = key; b.append(k); }
  b.addEventListener('click', () => { $('layoutMenu').hidden = true; onClick(); });
  return b;
}

function renderLayoutMenu() {
  const menu = $('layoutMenu');
  menu.innerHTML = '';
  const run = (fn) => () => fn().catch(showError);
  menu.append(menuItem('Save layout', run(saveLayout), 'Ctrl+S'));
  const auto = document.createElement('label');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = layout.autosave;
  cb.addEventListener('change', () => {
    layout.autosave = cb.checked;
    prefs.set('autosave', cb.checked);
    if (cb.checked && layout.dirty) saveLayout().catch(showError);
  });
  auto.append(cb, document.createTextNode('Autosave'));
  menu.append(auto);
  menu.append(menuItem('Rename…', run(renameLayout)));
  menu.append(menuItem('Make a copy…', run(() => createLayout(true))));
  menu.append(menuItem('Create new layout…', run(() => createLayout(false))));
  const sep = document.createElement('hr'); sep.className = 'menu-sep'; menu.append(sep);
  const h = document.createElement('h4');
  h.textContent = `Layouts (${layout.list.length}/${layout.max})`;
  menu.append(h);
  for (const l of layout.list) {
    const row = document.createElement('div');
    row.className = 'layout-row' + (l.id === layout.current?.id ? ' current' : '');
    const open = menuItem(l.name, run(() => loadLayout(l.id)));
    const when = document.createElement('span');
    when.className = 'key';
    when.textContent = new Date(l.updated_at).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    open.append(when);
    const del = document.createElement('button');
    del.className = 'del'; del.title = `Delete "${l.name}"`; del.setAttribute('aria-label', del.title); del.textContent = '✕';
    del.addEventListener('click', (e) => { e.stopPropagation(); $('layoutMenu').hidden = true; deleteLayout(l.id).catch(showError); });
    row.append(open, del);
    menu.append(row);
  }
}

$('layoutBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  const menu = $('layoutMenu');
  if (menu.hidden) renderLayoutMenu();
  menu.hidden = !menu.hidden;
  if (!menu.hidden) {
    // Open towards whichever side has room (the button moves when the toolbar wraps).
    menu.style.left = menu.style.right = '';
    if (menu.getBoundingClientRect().left < 0) { menu.style.left = '0'; menu.style.right = 'auto'; }
  }
});
document.addEventListener('click', (e) => { if (!$('layoutMenu').contains(e.target)) $('layoutMenu').hidden = true; });
window.addEventListener('beforeunload', () => {
  if (layout.dirty && layout.current) {
    // Best effort: keep the last changes if the window closes before autosave fires.
    fetch(`/api/layouts/${layout.current.id}`, {
      method: 'PUT', keepalive: true,
      headers: { 'Content-Type': 'application/json', 'X-Massive-Charts': '1' },
      body: JSON.stringify({ data: snapshot() }),
    });
  }
});

// First run: create "Default" from what this browser had before layouts existed.
async function ensureLayout() {
  await refreshLayoutList();
  if (layout.list.length) {
    const want = prefs.get('layoutId', null);
    const id = layout.list.some((l) => l.id === want) ? want : layout.list[0].id;
    await applyLayout(await api(`/api/layouts/${id}`));
    return;
  }
  const drawings = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && key.startsWith('mc:drawings:')) drawings[key.slice(12)] = JSON.parse(localStorage.getItem(key));
    }
  } catch { /* no browser storage: start empty */ }
  const last = prefs.get('last', null);
  const made = await apiWrite('POST', '/api/layouts', {
    name: 'Default',
    data: { ...snapshot(), symbol: last, drawings },
  });
  await refreshLayoutList();
  await applyLayout(made);
}

// -- top bar actions and keys ----------------------------------------------------
$('zoomIn').addEventListener('click', () => chart.zoomAtCoordinate(1.25, undefined, 150));
$('zoomOut').addEventListener('click', () => chart.zoomAtCoordinate(0.8, undefined, 150));
$('latestBtn').addEventListener('click', () => chart.scrollToRealTime(200));
$('shotBtn').addEventListener('click', () => {
  const bg = getComputedStyle(document.body).getPropertyValue('--bg').trim();
  const a = document.createElement('a');
  a.href = chart.getConvertPictureUrl(true, 'png', bg);
  a.download = `${state.ticker || 'chart'}-${state.period.span}${state.period.type}.png`;
  a.click();
});
$('themeBtn').addEventListener('click', () => {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  prefs.set('theme', state.theme);
  document.body.classList.toggle('light', state.theme === 'light');
  applyChartType();
});

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    saveLayout().catch(showError);
    return;
  }
  if (e.target.matches('input, select, textarea') || $('symbolDialog').open) return;
  if (e.key === 'Escape') { cancelDrawing(); setActiveTool('cursor'); }
  else if (e.key === 'h' && e.altKey) { e.preventDefault(); startDrawing('measure'); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && state.selectedOverlay) {
    chart.removeOverlay({ id: state.selectedOverlay });
  } else if (e.key === '/' || (e.key.length === 1 && /[a-z]/i.test(e.key) && !e.ctrlKey && !e.metaKey)) {
    e.preventDefault();
    openSymbolDialog();
    if (e.key !== '/') { $('symbolSearch').value = e.key; renderSymbolList(e.key); }
  } else if (e.key === '+' || e.key === '=') chart.zoomAtCoordinate(1.25, undefined, 150);
  else if (e.key === '-') chart.zoomAtCoordinate(0.8, undefined, 150);
  else if (e.key === 'End') chart.scrollToRealTime(200);
});

// -- status bar ------------------------------------------------------------------
function updateStatus(loaded) {
  const m = state.meta;
  if (!m) return;
  const info = symbolInfo(m.ticker);
  const parts = [
    `${m.ticker}${info.name ? ' · ' + info.name : ''}`,
    `feed: ${m.feeds.join(' > ')} (${m.base} data${m.hasDaily ? ' + official daily' : ''})`,
    `${m.rows.toLocaleString()} rows`,
    `${m.first.slice(0, 10)} → ${m.last.slice(0, 10)}`,
    `price precision ${m.pricePrecision} dp`,
    state.session === 'extended' ? 'extended hours' : 'regular hours 09:30–16:00',
    'times US/Eastern',
  ];
  if (loaded !== undefined) parts.push(`${loaded.toLocaleString()} bars loaded (scroll left for more)`);
  $('statusText').textContent = parts.join('  |  ');
}

// -- start -------------------------------------------------------------------------
(async function start() {
  renderTools();
  renderIndicatorMenu();
  renderSession();
  applyChartType();
  renderTypeParams();
  state.indicators.forEach(addIndicator);
  try {
    await loadSymbols();
    if (!state.symbols.length) {
      $('statusText').textContent = 'No data stored yet. Add a ticker with: python -m massive_collate watch add TICKER::EXCHANGE, then sync.';
      return;
    }
    await ensureLayout();
  } catch (err) {
    showError(err);
  }
})();
