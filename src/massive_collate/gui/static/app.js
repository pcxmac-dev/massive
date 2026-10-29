'use strict';
/* Massive Charts: TradingView-style front end over the local data API. */

const kc = window.klinecharts;
const TZ = 'America/New_York';
const MERGED = '*';
const PAGE = 20000;  // bars per request; zooming/scrolling out loads older pages as needed
const POLL_MS = 60_000;
const LIVE_POLL_MS = 2_000;  // a symbol streaming live (live.py) is polled this often

// -- per-profile preferences (localStorage; the app works without it) --------
// The profile signed in (its cookie, set on the login page): each keeps its own preferences, the
// first those from before profiles ('mc:'), any other under a prefix of its own (a new one starts
// with none).
const PROFILE = Number((document.cookie.match(/(?:^|;\s*)mc_profile=(\d+)/) || [])[1] || 1);
const PREFS_PREFIX = PROFILE === 1 ? 'mc:' : `mc:p${PROFILE}:`;
const prefs = {
  get(key, fallback) {
    try { const v = localStorage.getItem(PREFS_PREFIX + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem(PREFS_PREFIX + key, JSON.stringify(value)); } catch { /* ignore */ } },
};

const state = {
  ticker: null,
  source: MERGED,
  period: prefs.get('period', { type: 'minute', span: 1 }),
  session: prefs.get('session', 'regular'),
  unfiltered: prefs.get('unfiltered', false) === true,  // all data, bad data included (the "All data" switch)
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

// Sanity check for every request the page reads with: the same request is never sent again while
// one is on its way (callers share its answer), and an endpoint that keeps failing (no answer, or
// the server busy or broken) is not asked again until a pause has passed: 2 s, doubling up to
// 5 min, ended by its next answer. Without it a loop that asked every frame piled up thousands of
// requests while the server was busy, until the browser had none left
// (net::ERR_INSUFFICIENT_RESOURCES) and every other request on the page failed with it.
const apiInflight = new Map();  // url -> promise of its answer
const apiFailing = new Map();   // path -> { count, until, message }
const API_PAUSE_MS = [2_000, 300_000];

function apiFailed(path, message) {
  const f = apiFailing.get(path) || { count: 0 };
  f.message = message;
  if (f.until && Date.now() < f.until) return;  // requests sent before the pause began: the same outage
  f.count += 1;
  f.until = Date.now() + Math.min(API_PAUSE_MS[1], API_PAUSE_MS[0] * 2 ** (f.count - 1));
  apiFailing.set(path, f);
}

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null) url.searchParams.set(k, v);
  const key = url.toString();
  if (apiInflight.has(key)) return apiInflight.get(key);
  const f = apiFailing.get(url.pathname);
  if (f && Date.now() < f.until) {
    throw new Error(`${url.pathname}: ${f.message} (${f.count} failure${f.count > 1 ? 's' : ''} in a row; trying again in ${Math.ceil((f.until - Date.now()) / 1000)} s)`);
  }
  const answer = (async () => {
    let res;
    try {
      res = await fetch(url);
    } catch (err) {
      apiFailed(url.pathname, 'no answer from the chart app');
      throw err;
    }
    const body = await res.json().catch(() => ({}));
    if (res.status >= 500 || res.status === 429) apiFailed(url.pathname, body.error || res.statusText);
    else apiFailing.delete(url.pathname);  // an answer, even a refusal: the endpoint works
    if (!res.ok) throw new Error(body.error || res.statusText);
    return body;
  })().finally(() => apiInflight.delete(key));
  apiInflight.set(key, answer);
  return answer;
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
let highlightShown = '';  // what the Highlight panel was last scheduled for (the measure overlay)

function signed(v, digits) { return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`; }

function change(from, to, precision) {
  const pct = from ? ((to - from) / from) * 100 : 0;
  return `${signed(to - from, precision)} (${signed(pct, 2)}%)`;
}

// The zone a period's labels are read in. Days, weeks, months and years: the market's own days (New
// York; UTC for crypto and forex, whose days are stamped 00:00 UTC, the previous evening in New
// York), whatever zone is picked, or a day would read as the evening before. Within a day: the zone
// picked with the switch under the price axis (state.timezone), the exchange's (New York) by default.
// The exchange's zone (New York), then places around the world, each in its own zone so its clock
// keeps its daylight saving (Los Angeles: GMT−8 in winter, GMT−7 in summer). The list was fixed
// offsets ('-08:00'), each a place's standard time all year: an hour off for half of it.
const ZONE_CITIES = {
  'Etc/GMT+12': 'Baker Island', 'Pacific/Pago_Pago': 'Pago Pago', 'Pacific/Honolulu': 'Honolulu',
  'America/Anchorage': 'Anchorage', 'America/Los_Angeles': 'Los Angeles, Vancouver', 'America/Phoenix': 'Phoenix',
  'America/Denver': 'Denver, Calgary', 'America/Chicago': 'Chicago, Winnipeg', 'America/Mexico_City': 'Mexico City',
  'America/New_York': 'New York, Toronto', 'America/Caracas': 'Caracas', 'America/Halifax': 'Halifax',
  'America/Sao_Paulo': 'São Paulo', 'America/Argentina/Buenos_Aires': 'Buenos Aires',
  'Atlantic/South_Georgia': 'South Georgia', 'Atlantic/Azores': 'Azores', 'Atlantic/Cape_Verde': 'Cape Verde',
  UTC: 'UTC', 'Europe/London': 'London, Lisbon', 'Europe/Paris': 'Paris, Frankfurt',
  'Africa/Johannesburg': 'Johannesburg', 'Europe/Athens': 'Athens, Helsinki', 'Europe/Istanbul': 'Istanbul',
  'Europe/Moscow': 'Moscow', 'Asia/Dubai': 'Dubai', 'Asia/Karachi': 'Karachi, Tashkent', 'Asia/Kolkata': 'Mumbai, New Delhi',
  'Asia/Dhaka': 'Dhaka', 'Asia/Bangkok': 'Bangkok, Jakarta', 'Asia/Singapore': 'Singapore, Hong Kong',
  'Asia/Tokyo': 'Tokyo, Seoul', 'Australia/Brisbane': 'Brisbane', 'Australia/Sydney': 'Sydney, Melbourne',
  'Pacific/Noumea': 'Nouméa, Solomon Islands', 'Pacific/Auckland': 'Auckland', 'Pacific/Fiji': 'Fiji',
  'Pacific/Tongatapu': 'Tonga', 'Pacific/Kiritimati': 'Kiritimati' };
// A fixed offset picked before: the place it stood for, now with its daylight saving ('-08:00' was
// the user's Pacific time, an hour off from March to November).
const OFFSET_ZONES = {
  '-12:00': 'Etc/GMT+12', '-11:00': 'Pacific/Pago_Pago', '-10:00': 'Pacific/Honolulu', '-09:00': 'America/Anchorage',
  '-08:00': 'America/Los_Angeles', '-07:00': 'America/Denver', '-06:00': 'America/Chicago', '-05:00': 'America/New_York',
  '-04:00': 'America/Halifax', '-03:00': 'America/Sao_Paulo', '-02:00': 'Atlantic/South_Georgia', '-01:00': 'Atlantic/Azores',
  '+00:00': 'Europe/London', '+01:00': 'Europe/Paris', '+02:00': 'Europe/Athens', '+03:00': 'Europe/Moscow',
  '+04:00': 'Asia/Dubai', '+05:00': 'Asia/Karachi', '+06:00': 'Asia/Dhaka', '+07:00': 'Asia/Bangkok',
  '+08:00': 'Asia/Singapore', '+09:00': 'Asia/Tokyo', '+10:00': 'Australia/Sydney', '+11:00': 'Pacific/Noumea',
  '+12:00': 'Pacific/Auckland', '+13:00': 'Pacific/Tongatapu', '+14:00': 'Pacific/Kiritimati' };

// Minutes east of UTC in a zone at `when` (its daylight saving counted).
function zoneOffset(zone, when = new Date()) {
  try {
    const v = new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'longOffset' }).formatToParts(when)
      .find((p) => p.type === 'timeZoneName')?.value || '';
    const [, sign, hh, mm] = v.match(/GMT([+-])(\d\d):(\d\d)/) || [];
    return sign ? (sign === '-' ? -1 : 1) * (Number(hh) * 60 + Number(mm)) : 0;
  } catch { return 0; }
}

// Round the world by their offset now (west to east), so the wheel stays in order as clocks change.
const TIMEZONES = ['exchange', ...Object.keys(ZONE_CITIES).sort((a, b) => zoneOffset(a) - zoneOffset(b))];
const savedZone = prefs.get('timezone');
state.timezone = TIMEZONES.includes(savedZone) ? savedZone : OFFSET_ZONES[savedZone] || 'exchange';
if (savedZone && state.timezone !== savedZone) prefs.set('timezone', state.timezone);  // kept as the place

function pickedZone() {
  return state.timezone === 'exchange' ? TZ : state.timezone;
}

// "GMT−7", "GMT+5:30", "GMT": a zone's offset from UTC now, its daylight saving in it.
function gmtLabel(zone) {
  try {
    const v = new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'shortOffset' }).formatToParts(new Date())
      .find((p) => p.type === 'timeZoneName')?.value;
    return v ? v.replace('-', '−') : zone;
  } catch { return zone; }
}

// Whether a zone's clock changes during the year (its offset in January and July differ).
function keepsDaylightSaving(zone) {
  const y = new Date().getUTCFullYear();
  return zoneOffset(zone, new Date(Date.UTC(y, 0, 1))) !== zoneOffset(zone, new Date(Date.UTC(y, 6, 1)));
}

function zoneCities(zone) {
  return ZONE_CITIES[zone] || '';
}

function labelZone(period) {
  const daily = ['day', 'week', 'month', 'year'].includes(period?.type);
  if (daily) return ['crypto', 'fx'].includes(state.meta?.asset) ? 'UTC' : TZ;
  return pickedZone();
}

// "EDT", "UTC", "GMT+9": a zone's short name now.
function zoneShort(zone) {
  try {
    return new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'short' }).formatToParts(new Date())
      .find((p) => p.type === 'timeZoneName')?.value || zone;
  } catch { return zone; }
}

function fmtTime(ts) {
  const p = chart.getPeriod() || { type: 'minute' };
  const opts = { timeZone: labelZone(p), month: 'short', day: '2-digit', hourCycle: 'h23' };
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
  const sym = chart.getSymbol() || {};
  return `${sym.source}:${sym.ticker}|${state.adjust}|${p.type}:${p.span}|${state.session}|${JSON.stringify(modelQuery())}`
    + `|${pa.timestamp}|${pb.timestamp}`;
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
        from: pa.timestamp, to: pb.timestamp, adjust: state.adjust, ...modelQuery(),
      });
      chart.overrideOverlay({ id: overlay.id, extendData: { ...(overlay.extendData || {}), key, stats } });
      scheduleHighlightPanel();  // also when the highlight is off-screen and not redrawn
    } catch (err) { showError(err); }
  }, 150));
}

// Everything the highlight shows, computed once: start, center and end points, the change
// between them, and the server's exact statistics for the bars inside it (when arrived).
function highlightSummary(pa, pb, st) {
  const chg = (a, b) => ({ abs: b - a, pct: a ? ((b - a) / a) * 100 : null });
  const midPrice = (pa.value + pb.value) / 2;
  const midTime = Math.round((pa.timestamp + pb.timestamp) / 2);
  const indexBars = pa.dataIndex != null && pb.dataIndex != null ? Math.abs(pb.dataIndex - pa.dataIndex) + 1 : null;
  return {
    up: pb.value >= pa.value,
    start: { price: pa.value, time: pa.timestamp },
    mid: { price: midPrice, time: midTime, fromStart: chg(pa.value, midPrice), toEnd: chg(midPrice, pb.value) },
    end: { price: pb.value, time: pb.timestamp, change: chg(pa.value, pb.value),
           bars: st ? st.bars : (indexBars ?? '…'), duration: pb.timestamp - pa.timestamp },
    stats: st && st.bars ? st : null,
  };
}

kc.registerOverlay({
  name: 'measure',
  totalStep: 3,
  needDefaultPointFigure: true,
  needDefaultXAxisFigure: true,
  needDefaultYAxisFigure: true,
  // Graphics only: every number is shown in the side panel's Highlight section.
  createPointFigures: ({ overlay, coordinates }) => {
    if (coordinates.length < 2 || overlay.points.length < 2) return [];
    const [a, b] = coordinates;
    const [pa, pb] = overlay.points;
    if (pa.value == null || pb.value == null || pa.timestamp == null || pb.timestamp == null) return [];
    const left = Math.min(a.x, b.x), right = Math.max(a.x, b.x);
    const top = Math.min(a.y, b.y), bottom = Math.max(a.y, b.y);
    const cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2;

    const key = statsKey(pa, pb);
    const ext = overlay.extendData || {};
    if (ext.key !== key) requestRangeStats(overlay, key, pa, pb);
    // The panel follows live while the highlight is drawn or dragged: rebuilt when what it shows
    // changed, not on every redraw (each crosshair move redraws the chart, and the rebuilt panel lost
    // clicks on its Remove button).
    const shown = `${overlay.id}|${pa.value}|${pb.value}|${key}|${ext.key === key && !!ext.stats}|${state.timezone}|${prec()}`;
    if (shown !== highlightShown) { highlightShown = shown; scheduleHighlightPanel(); }
    const up = pb.value >= pa.value;
    return [
      { type: 'rect', attrs: { x: left, y: top, width: right - left, height: Math.max(1, bottom - top) },
        styles: { style: 'fill', color: up ? 'rgba(38,166,154,0.15)' : 'rgba(239,83,80,0.15)' } },
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: left, y: cy }, { x: right, y: cy }] },
        styles: { style: 'dashed', dashedValue: [5, 4], color: MID, size: 1 } },
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x: cx, y: top }, { x: cx, y: bottom }] },
        styles: { style: 'dashed', dashedValue: [2, 4], color: MID, size: 1 } },
      { type: 'circle', ignoreEvent: true, attrs: { x: cx, y: cy, r: 3.5 }, styles: { style: 'fill', color: MID } },
    ];
  },
});

// -- themes ----------------------------------------------------------------------
// Each theme is the page's colours (style.css, body[data-theme]) and the chart's: one of the chart
// library's two built-in styles, with grid, axis, crosshair and tooltip colours on top. Light-based
// themes also get body.light (the light page rules and the light steps of the series palette).
// Text contrast on each page colour: body text 12:1 or more, secondary text 4.5:1 or more.
const THEMES = {
  dark: { base: 'dark', label: 'Dark' },
  midnight: { base: 'dark', label: 'Midnight', chart: {
    grid: { horizontal: { color: '#18213a' }, vertical: { color: '#18213a' } },
    xAxis: { axisLine: { color: '#2a3656' }, tickLine: { color: '#2a3656' }, tickText: { color: '#8f9bb8' } },
    yAxis: { axisLine: { color: '#2a3656' }, tickLine: { color: '#2a3656' }, tickText: { color: '#8f9bb8' } },
    separator: { color: '#1f2a44' },
  } },
  graphite: { base: 'dark', label: 'Graphite', chart: {
    grid: { horizontal: { color: '#2a2d32' }, vertical: { color: '#2a2d32' } },
    xAxis: { axisLine: { color: '#3d4148' }, tickLine: { color: '#3d4148' }, tickText: { color: '#959aa3' } },
    yAxis: { axisLine: { color: '#3d4148' }, tickLine: { color: '#3d4148' }, tickText: { color: '#959aa3' } },
    separator: { color: '#353940' },
  } },
  light: { base: 'light', label: 'Light' },
  paper: { base: 'light', label: 'Paper', chart: {
    grid: { horizontal: { color: '#ddd3c1' }, vertical: { color: '#ddd3c1' } },
    xAxis: { axisLine: { color: '#a99b82' }, tickLine: { color: '#a99b82' }, tickText: { color: '#3a3226' } },
    yAxis: { axisLine: { color: '#a99b82' }, tickLine: { color: '#a99b82' }, tickText: { color: '#3a3226' } },
    separator: { color: '#c8bba3' },
    crosshair: {
      horizontal: { line: { color: '#6b5d47' }, text: { backgroundColor: '#3a3226', borderColor: '#3a3226' } },
      vertical: { line: { color: '#6b5d47' }, text: { backgroundColor: '#3a3226', borderColor: '#3a3226' } },
    },
    candle: { tooltip: { text: { color: '#241e14' } } },
    indicator: { tooltip: { text: { color: '#241e14' } } },
  } },
};
if (!THEMES[state.theme]) state.theme = 'dark';
const themeBase = () => THEMES[state.theme].base;

function setTheme(name) {
  state.theme = THEMES[name] ? name : 'dark';
  document.body.dataset.theme = state.theme;
  document.body.classList.toggle('light', themeBase() === 'light');
  $('themeSelect').value = state.theme;
}
setTheme(state.theme);

// -- chart ---------------------------------------------------------------------
// Bars may shrink far below a pixel each, so any interval can be zoomed out to months or years
// of data (KLineChart's default stops at 1 px per bar, ~700 bars on screen).
const MIN_BAR_SPACE = 0.002;  // ~350k bars across a 700 px chart: two years of 1-minute futures
const chart = kc.init('chart', {
  timezone: TZ, styles: themeBase(), locale: 'en-US',
  layout: { barSpaceLimit: { min: MIN_BAR_SPACE, max: 80 } },
});

chart.setDataLoader({
  getBars: async ({ type, timestamp, symbol, period, callback }) => {
    if (type === 'update') { callback([], { backward: false }); return; }
    // A symbol pick sets the interval, then the symbol, and each loads the chart again: the first would
    // be the old symbol's at the new interval, a heavy request whose answer is dropped. Only the
    // symbol's own load goes (the chart's next reset starts it).
    if (type === 'init' && (state.settingPeriod || symbol.ticker !== state.ticker || symbol.source !== state.source)) return;
    // A replay (replay.js) shows the bars before its start and plays the rest in: nothing newer here.
    if (type === 'init' && state.replay && window.Replay?.keyChanged()) window.Replay.stop(false);
    if (type === 'backward' && state.replay) { callback([], { backward: false }); return; }
    const token = type === 'init' ? ++state.loadToken : state.loadToken;
    const params = {
      source: symbol.source, ticker: symbol.ticker, period: period.type, span: period.span,
      session: state.session, limit: state.meta?.auto ? autoPage() : PAGE, adjust: state.adjust, lod: lodFor(),
      ...typeQuery(), ...modelQuery(),
    };
    if (type === 'forward' && timestamp) params.to = timestamp;
    if (type === 'backward' && timestamp) params.after = timestamp;  // newer bars, after the newest loaded
    if (type === 'init' && state.lodSwitch?.to) params.to = state.lodSwitch.to;  // a level change keeps its window
    if (type === 'init' && state.replay) params.to = state.replay.start;
    try {
      const res = await api('/api/bars', params);
      if (token !== state.loadToken) return; // a newer symbol/interval was chosen meanwhile
      const zone = labelZone(period);  // the time axis and crosshair read the bars' own days
      if (chart.getTimezone() !== zone) chart.setTimezone(zone);
      const replaying = !!state.replay;
      callback(res.bars, { forward: type === 'backward' ? undefined : res.more,
                           backward: type === 'forward' ? undefined : !replaying && !!res.newer });
      if (type === 'init' && replaying) requestAnimationFrame(() => window.Replay?.loaded());  // then it plays on
      if (type === 'init' && res.level?.fixed && (res.level.type !== period.type || res.level.span !== period.span)) {
        // a price-based chart is built from a level of its own (gui/window.py bricks): its times, once more
        state.period = { type: res.level.type, span: res.level.span };
        renderPeriods();
        chart.setPeriod(state.period);  // from the server's cache
        return;
      }
      if (type === 'init' && !res.bars.length && res.model?.hidden && state.meta?.auto && skipEmptyLevel()) return;
      if (type === 'init' || type === 'backward') state.hasNewer = !!res.newer;  // a window in the past: no live tail
      if (type === 'init' && state.fitAll) {  // a symbol opens on its whole history
        state.fitAll = false;
        requestAnimationFrame(() => fitWhole(res.bars.length));
      }
      showError(null);
      if (type === 'init') {
        state.styleUsed = res.style;
        state.approximate = res.approximate || null;
        if (state.model.kind === 'fourier') renderModelControls();  // a fit cut short says so
        state.derivedFrom = res.derivedFrom || null;
        state.baseTotal = (res.total || 0) * lodFor();  // bars at full detail
        if (applyModelPrecision(res)) return;  // reloads once with the right precision
        // A new model or chart type can change the value range completely; KLineChart only
        // re-fits the price scale on a symbol change, so ask it to re-fit now.
        if (typeof chart._resetYAxisAutoCalcTickFlag === 'function') chart._resetYAxisAutoCalcTickFlag();
        requestAnimationFrame(drawModelGuides);
        renderTypeParams();
        requestAnimationFrame(drawRolls);
        const keep = state.lodSwitch && !state.lodSwitch.atLatest;  // the same window, back in time
        if (state.lodSwitch) finishLodSwitch();
        else requestAnimationFrame(checkLod);  // new symbol/interval/type at a far zoom: pick the right detail
        // After the first render: overlays created in the same tick as the data skew the price scale.
        requestAnimationFrame(() => requestAnimationFrame(restoreDrawings));
        if (!keep) requestAnimationFrame(applyNowPosition);
        updateStatus(res.bars.length);
      }
    } catch (err) {
      if (token !== state.loadToken) return;  // a newer symbol/interval's load fills the chart: not blanked
      showError(err);
      if (type === 'init') {
        callback([], { forward: false, backward: false });
        retryLoad(token);
        return;
      }
      // A page of history (older, or newer after a window in the past) that failed because the app was
      // busy or out of reach is still there: the chart keeps asking for it (the next scroll to that edge,
      // or retryPage once the pause is over). A refusal would be the same answer again: no more there.
      const failing = apiFailing.has('/api/bars');
      callback([], type === 'forward' ? { forward: failing } : { backward: failing });
      if (failing) retryPage(token);
    }
  },
  subscribeBar: ({ symbol, period, callback }) => {
    clearInterval(state.poll);
    state.barCallback = callback;  // a replay plays its bars in through it (replay.js)
    // Each subscription is one load of the chart: a poll still in flight when the symbol, interval,
    // model or chart type changes belongs to the old one and must not add its bars to the new data.
    const sub = state.subscription = (state.subscription || 0) + 1;
    // Also run at once when a fetch run finishes (panels.js), not only every POLL_MS.
    state.pollNow = async () => {
      const list = chart.getDataList();
      if (!list.length || state.polling || state.hasNewer) return;  // one poll at a time; only at the newest bars
      state.polling = true;
      try {
        if (PRICE_BASED.has(state.chartType)) return; // path-dependent: refreshed by reloading
        const kind = state.model.kind;
        const res = await api('/api/bars', {
          source: symbol.source, ticker: symbol.ticker, period: period.type, span: period.span,
          session: state.session, since: list[list.length - 1].timestamp, adjust: state.adjust, lod: lodFor(),
          ...typeQuery(), ...modelQuery(),
        });
        if (sub !== state.subscription || kind !== state.model.kind) return;
        res.bars.forEach((bar) => callback(bar));
        // A forming bar changes in place (no scroll): refresh the side panel's last bar and the time handle.
        if (res.bars.length) requestAnimationFrame(() => { window.renderCursor?.(); placeNowHandle(); });
      } catch { /* the next poll will retry */ } finally { state.polling = false; }
    };
    // A live symbol listens to the server's stream: new and growing bars arrive as the recorder
    // stores them, the chart follows. Without a stream (or if it fails) live symbols are polled
    // every 2 s, the rest every minute.
    closeStream();
    state.streamFailed = false;
    if (state.modelDue) { clearTimeout(state.modelDue.timer); Object.assign(state.modelDue, { timer: 0, stale: false }); }
    const openStream = () => {
      const list = chart.getDataList();
      if (!list.length || PRICE_BASED.has(state.chartType) || state.hasNewer) return;
      const kind = state.model.kind;
      const q = new URLSearchParams(Object.entries({
        source: symbol.source, ticker: symbol.ticker, period: period.type, span: period.span,
        session: state.session, since: list[list.length - 1].timestamp, adjust: state.adjust, lod: lodFor(),
        ...typeQuery(), ...modelQuery(),
      }).filter(([, v]) => v !== undefined && v !== null));
      const es = state.stream = new EventSource(`/api/stream?${q}`);
      es.onmessage = (e) => {
        if (sub !== state.subscription || kind !== state.model.kind) return;
        const msg = JSON.parse(e.data);
        if (msg.reload) { modelMoved(); return; }  // a model: its values moved back in time
        if ((msg.model || 'price') !== kind) return;
        const last = chart.getDataList().at(-1)?.timestamp ?? 0;
        const bars = msg.bars.filter((b) => b.timestamp >= last);
        bars.forEach((bar) => callback(bar));
        if (bars.length) requestAnimationFrame(() => { window.renderCursor?.(); placeNowHandle(); });
      };
      es.addEventListener('end', () => { closeStream(); if (sub === state.subscription) openStream(); });
      es.onerror = () => {
        if (es.readyState === EventSource.CLOSED) { closeStream(); state.streamFailed = true; }  // poll instead
      };
    };
    state.lastPoll = Date.now();
    state.poll = setInterval(() => {
      if (state.replay) { closeStream(); return; }  // the replay is the chart's only source of new bars
      const live = isLive();
      if (live && !state.stream && !state.streamFailed) openStream();
      if (!live && state.stream) closeStream();
      if (state.stream) return;  // pushed
      if (live || Date.now() - state.lastPoll >= POLL_MS) { state.lastPoll = Date.now(); state.pollNow(); }
    }, LIVE_POLL_MS);
  },
  unsubscribeBar: () => { clearInterval(state.poll); closeStream(); },
});

// A chart whose first page failed because the app was busy, restarting or out of reach (not a
// refusal) loads again when the pause on /api/bars is over, instead of staying empty until
// something else is picked.
function retryLoad(token) {
  const f = apiFailing.get('/api/bars');
  if (!f) return;  // refused (an unknown symbol ...): asking again would get the same answer
  setTimeout(() => {
    if (token === state.loadToken && !chart.getDataList().length) chart.resetData();
  }, Math.max(1_000, f.until - Date.now() + 50));
}

// A page of history that failed the same way is asked for again when the pause is over, if the chart
// is still at that edge: a scroll by nothing makes the chart look at its edges (and load what is
// beyond one it shows) without moving.
function retryPage(token) {
  const f = apiFailing.get('/api/bars');
  if (!f) return;
  clearTimeout(state.pageRetry);  // one at a time: a scroll along the edge during the pause asks every frame
  state.pageRetry = setTimeout(() => { if (token === state.loadToken) chart.scrollByDistance(0); },
                               Math.max(1_000, f.until - Date.now() + 50));
}

// Whether the newest bar is on screen (the chart then follows it): only then may a resize put it
// back where the "now" handle was left; a chart scrolled into history stays where it is.
function atNewest() {
  const n = chart.getDataList().length;
  return !n || chart.getVisibleRange().to >= n;
}

window.addEventListener('resize', () => {
  chart.resize();
  if (atNewest()) applyNowPosition(); else placeNowHandle();
  placeHighlightMask();
});

function closeStream() {
  state.stream?.close();
  state.stream = null;
}

// A model's values move back in time as bars are stored (a centred window; a Fourier fit, all of
// it), and the stream says so with every write of a live symbol, every couple of seconds. The chart
// loads again at most once a bar of its level, and never sooner than MODEL_RELOAD_MS[0] nor later
// than MODEL_RELOAD_MS[1] after the last, keeping its zoom as a model switch does. Scrolled back into
// history (or hidden behind another view) it waits until the newest bar is in view again. Reloading on
// every write held a heavy request every 2 s and jumped the view back to now.
const MODEL_RELOAD_MS = [10_000, 60_000];

function modelMoved() {
  const due = state.modelDue ??= { at: 0, timer: 0, stale: false };  // at: the last reload
  due.stale = true;
  if (due.timer) return;
  const bar = (LEVEL_MS[state.period.type] || 6e4) * (state.period.span || 1);
  const every = Math.min(MODEL_RELOAD_MS[1], Math.max(MODEL_RELOAD_MS[0], bar));
  due.timer = setTimeout(reloadMovedModel, Math.max(0, due.at + every - Date.now()));
}

function reloadMovedModel() {
  const due = state.modelDue;
  due.timer = 0;
  if (!due.stale || state.model.kind === 'price' || state.replay || state.lodSwitch) return;
  if (!atNewest() || !chartShown()) return;  // asked again when the newest bar is back in view
  due.stale = false;
  due.at = Date.now();
  state.lodSwitch = { barSpace: chart.getBarSpace().bar, anchor: null, anchorX: null, atLatest: true };  // the same zoom
  chart.resetData();
}

const modelBackInView = () => { if (state.modelDue?.stale && !state.modelDue.timer && atNewest()) modelMoved(); };
chart.subscribeAction('onVisibleRangeChange', modelBackInView);
window.addEventListener('massive:view', () => requestAnimationFrame(modelBackInView));

// Whether the open symbol is streaming live right now (the activity poll's live tickers).
function isLive() {
  return !!state.ticker && (window.Activity?.data?.liveFeeds?.liveTickers || []).includes(state.ticker);
}

// -- the current time's place on screen -----------------------------------------------------
// The newest bar can be kept anywhere across the chart (half way, 80 % to the right ...): drag the
// "now" handle on the time axis; double-click it to put the newest bar back at the right edge. While
// the chart is at the newest bar it follows it, so history moves left as time goes on. The handle
// (with its clock) and its line show only while the symbol streams live: for a symbol that is not
// moving (its market closed, no live feed) the crosshair's own time label is the time shown.
const DEFAULT_RIGHT_OFFSET = 80;  // the chart library's own default
state.nowAt = prefs.get('nowAt', null);  // fraction of the plot width, or null: the right edge

function plotWidth() {
  return chart.getSize('candle_pane', 'main')?.width || 0;
}

function applyNowPosition() {
  const w = plotWidth();
  if (!w) return;
  const at = isLive() ? state.nowAt : null;  // not moving: no room kept for time that is not coming
  state.nowApplied = isLive();
  chart.setOffsetRightDistance(at == null ? DEFAULT_RIGHT_OFFSET : Math.max(8, w * (1 - at)));
  placeNowHandle();
}

const nowHandle = document.createElement('div');
nowHandle.id = 'nowHandle';
nowHandle.className = 'now-handle';
nowHandle.title = 'The current time, at the newest bar: drag to place it on screen; double-click to put it back at the right edge';
// The clock in the time axis's zone, with the zone's name (EDT, EST ...).
const NOW_CLOCK = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit',
                                                       hourCycle: 'h23', timeZoneName: 'short' });
const tickNowClock = () => { nowHandle.textContent = NOW_CLOCK.format(new Date()); };
tickNowClock();
setInterval(tickNowClock, 1000);
$('chart').append(nowHandle);
// A light grey dashed line from the handle up through the price pane and every indicator pane.
const nowLine = document.createElement('div');
nowLine.className = 'now-line';
$('chart').append(nowLine);

function placeNowHandle() {
  const data = chart.getDataList();
  const last = data[data.length - 1];
  const x = last ? chart.convertToPixel({ timestamp: last.timestamp }, { paneId: 'candle_pane' }).x : NaN;
  const w = plotWidth();
  const show = isLive() && Number.isFinite(x) && x >= 0 && x <= w;
  nowHandle.hidden = nowLine.hidden = !show;
  if (show) {
    nowHandle.style.left = nowLine.style.left = `${x}px`;
    nowLine.style.height = `${chart.getSize('x_axis_pane')?.top ?? 0}px`;  // down to the time axis
  }
  nowHandle.classList.toggle('live', isLive());
}

nowHandle.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  nowHandle.setPointerCapture(e.pointerId);
  chart.scrollToRealTime(0);
  const box = $('chart').getBoundingClientRect();
  const move = (ev) => {
    const w = plotWidth();
    const x = Math.min(w - 8, Math.max(w * 0.1, ev.clientX - box.left));
    state.nowAt = x / w;
    chart.setOffsetRightDistance(w - x);
    placeNowHandle();
  };
  // The drag ends however the pointer goes: let go, taken by the browser (a touch turned into a
  // scroll) or its capture lost. Ended on pointerup alone, a cancelled drag kept its listeners.
  const ENDS = ['pointerup', 'pointercancel', 'lostpointercapture'];
  const up = () => {
    nowHandle.removeEventListener('pointermove', move);
    for (const ev of ENDS) nowHandle.removeEventListener(ev, up);
    prefs.set('nowAt', state.nowAt);
  };
  nowHandle.addEventListener('pointermove', move);
  for (const ev of ENDS) nowHandle.addEventListener(ev, up);
});
nowHandle.addEventListener('dblclick', () => {
  state.nowAt = null;
  prefs.set('nowAt', null);
  applyNowPosition();
  goLatest();
});
chart.subscribeAction('onVisibleRangeChange', () => requestAnimationFrame(() => { placeNowHandle(); placeHighlightMask(); }));
window.addEventListener('massive:activity', () => requestAnimationFrame(() => {  // live or not, again
  if (state.nowAt != null && state.nowApplied !== isLive() && atNewest()) applyNowPosition();
  else placeNowHandle();
}));

// -- level of detail -------------------------------------------------------------------
// Zoomed out past half a pixel per bar, the chart is fed exact aggregates of k consecutive
// bars (k a power of two keeping candles 1-4 px wide). Far fewer bars for the chart to
// process each frame, the same visible envelope; statistics still use every bar.
const LOD_TYPES = new Set(['candles', 'hollow', 'bars', 'volume_candles', 'line', 'line_markers', 'step', 'area',
                           'hlc_area', 'baseline', 'columns', 'high_low',
                           'heikin_ashi', 'renko', 'line_break', 'range']);
const MAX_LOD = 4096;
state.lod = 1;
state.lodSwitch = null;

function lodFor() {
  return 1;  // auto-ranging moves to a coarser level instead of aggregating k bars (checkLod below)
}

function resetLod() {
  state.lod = 1;
  state.lodSwitch = null;
}

// The chart's plot width; while it is hidden (another view showing: no size, or a negative one)
// the last width it had, so levels and fits are worked out for the screen it comes back to.
function chartWidth() {
  const w = chart.getSize('candle_pane', 'main')?.width;
  if (w > 0) state.chartW = w;
  return state.chartW || Math.max(400, window.innerWidth - 420);
}

const chartShown = () => chart.getSize('candle_pane', 'main')?.width > 0;

// -- auto-ranging: the level follows the zoom ------------------------------------------------
// No interval to pick: a symbol opens on its whole history at the finest level whose candles fit
// the chart (autoMinPx each), and as the chart zooms in it moves to finer levels (days, hours,
// minutes, seconds) while the data has them for the window on screen; where it has none finer,
// the candles just grow. Zooming out moves to coarser levels the same way. The level shows where
// the interval menu was ("auto: 1 hour"). A level change keeps the window: the same moment in the
// centre, the same span across the chart (finishLodSwitch).
const AUTO_SLACK = 0.15;  // hysteresis: a level is left only 15 % past its fit, so zooming does not flicker
state.autoDetail = Math.min(100, Math.max(0, Number(prefs.get('autoDetail', 50)) || 0));

// The narrowest candle (px) before the chart moves to a coarser level, from the Detail slider: 2 px in
// the middle, halved every quarter to the right (0.5 px at the end: finer levels sooner, thinner
// candles), doubled every quarter to the left (8 px at the end: coarser levels, bigger candles).
function autoMinPx() {
  return 2 * Math.pow(2, (50 - state.autoDetail) / 25);
}

// Bars per request: two screens at the narrowest (a level change's page ends a window past the
// right edge; more load as the chart scrolls), at least 1,000, at most what the server gives. A
// page of 4-hour bars is minutes to read: 3,000 of them were six years of minutes.
function autoPage() {
  return Math.min(20000, Math.max(1000, Math.ceil(2 * chartWidth() / autoMinPx())));
}
const LEVEL_NAMES = { second: 'sec', minute: 'min', hour: 'hour', day: 'day', week: 'week', month: 'month', year: 'year' };
const LEVEL_MS = { second: 1e3, minute: 6e4, hour: 36e5, day: 864e5, week: 6048e5, month: 2629746e3, year: 31556952e3 };

// Bars a calendar day holds at a level (as gui/window.py per_day): for series served whole
// (continuous futures, the composite) whose metadata has no levels.
// The sessions a stock's chart shows, in the order the ETH button steps through them: regular
// hours; extended (pre-market and after-hours, 04:00-20:00); all four (the overnight session too,
// 20:00-04:00 on Blue Ocean, from its own feed).
const SESSIONS = ['regular', 'extended', 'all'];
const SESSION_TEXT = {
  regular: ['RTH', 'regular hours 09:30-16:00'],
  extended: ['ETH', 'extended hours: pre-market 04:00, after-hours to 20:00'],
  all: ['ETH+ON', 'all four sessions: overnight 20:00-04:00 (Blue Ocean), pre-market, regular, after-hours'],
};

function perDayGuess(asset, p, session) {
  const days = asset === 'stock' || asset === 'index' ? 252 / 365 : asset === 'crypto' ? 1 : 5 / 7;
  if (['day', 'week', 'month', 'year'].includes(p.type)) {
    const per = asset === 'stock' || asset === 'index' ? { day: 1, week: 1 / 5, month: 1 / 21, year: 1 / 252 }[p.type] * days
      : { day: 1, week: 1 / 7, month: 1 / 30.44, year: 1 / 365.25 }[p.type];
    return per / p.span;
  }
  const stock = { regular: 390, all: 1440 }[session] || 960;  // all four sessions: the overnight too
  const minutes = asset === 'stock' ? stock : asset === 'index' ? 390 : asset === 'crypto' ? 1440 : 1380;
  return days * minutes * 6e4 / (LEVEL_MS[p.type] * p.span);
}

function autoLevels() {
  const m = state.meta;
  if (!m) return [];
  if (m.levels) return m.levels;
  const first = Date.parse(m.first), last = Date.parse(m.last);
  return (m.allPeriods || m.periods || []).filter((p) => p.available !== false).map((p) => ({
    type: p.type, span: p.span, first, last, label: `${p.span} ${LEVEL_NAMES[p.type]}`,
    perDay: Object.fromEntries(SESSIONS.map((s) => [s, perDayGuess(m.asset, p, s)])),
  }));
}

function currentLevel() {
  return autoLevels().find((lv) => lv.type === state.period.type && lv.span === state.period.span) || null;
}

const levelPerDay = (lv) => lv.perDay[state.session] || lv.perDay.extended;

// The finest level whose candles for `days` of history fit the chart and whose data reaches back
// to `fromMs` (a level with nothing that old is not shown for that window: candles grow instead).
function chooseLevel(days, fromMs, current = null) {
  const levels = autoLevels();
  if (!levels.length) return null;
  const fit = chartWidth() / autoMinPx();
  const oldest = Math.min(...levels.map((lv) => lv.first));
  const from = Math.max(Number.isFinite(fromMs) ? fromMs : oldest, oldest);
  const empty = state.model.kind === 'price' ? null : emptyLevels();
  for (const lv of levels) {
    if (lv.first > from + LEVEL_MS.day || empty?.has(`${lv.type}:${lv.span}`)) continue;
    const same = current && lv.type === current.type && lv.span === current.span;
    if (days * levelPerDay(lv) <= fit * (same ? 1 + AUTO_SLACK : 1 - AUTO_SLACK)) return lv;
  }
  return levels[levels.length - 1];
}

// The window on screen: its span in days (from the level's bars a day) and its first bar's time.
function visibleWindow() {
  const data = chart.getDataList();
  const r = chart.getVisibleRange();
  const lo = r.realFrom ?? r.from, hi = r.realTo ?? r.to;
  const cur = currentLevel();
  const bars = Math.max(1, hi - lo);
  return { bars, days: cur ? bars / levelPerDay(cur) : 0, fromMs: data[Math.max(0, Math.min(data.length - 1, lo))]?.timestamp,
           centre: data[Math.max(0, Math.min(data.length - 1, Math.floor((lo + hi) / 2)))]?.timestamp, atLatest: hi >= data.length };
}

function checkLod() {
  if (state.replay) return;  // a replay keeps its level: zoom only widens or narrows the candles
  if (state.lodSwitch || !state.meta || !chart.getDataList().length) return;
  if (!chartShown()) return;  // a hidden chart: decided when it is shown (showPane calls this)
  if (state.fitPending) {  // opened while hidden: its whole history now that it has a width
    const n = state.fitPending;
    state.fitPending = 0;
    fitWhole(n);
    return;
  }
  if (PRICE_BASED.has(state.chartType)) return;  // bricks are built from one level, whatever the zoom
  const cur = currentLevel();
  if (!cur) return;
  const w = visibleWindow();
  const lv = chooseLevel(w.days, w.fromMs, cur);
  if (!lv || (lv.type === cur.type && lv.span === cur.span)) { prefetchFiner(w, cur); return; }
  const newBars = Math.max(1, w.days * levelPerDay(lv));
  state.lodSwitch = {
    barSpace: chartWidth() / newBars, anchor: w.centre ?? null, anchorX: chartWidth() / 2, atLatest: w.atLatest,
    // a window in the past: its page ends a window past its right edge (older and newer load on scroll)
    to: w.atLatest || w.centre == null ? null : Math.round(w.centre + w.days * 864e5 * 1.5),
  };
  state.period = { type: lv.type, span: lv.span };
  renderPeriods();
  chart.setPeriod(state.period);
}

// Levels where the model draws nothing, for this symbol, session and model: every session there is
// shorter than its window (an hour's bars in a regular session: 7 a day; the default window 13).
function emptyLevels() {
  const ctx = `${state.source}:${state.ticker}|${state.session}|${JSON.stringify(modelQuery())}`;
  if (state.emptyLevels?.ctx !== ctx) state.emptyLevels = { ctx, set: new Set() };
  return state.emptyLevels.set;
}

// Passes over the level just loaded when the model drew nothing there: to the level that fits the
// window the chart was moving to (as the zoom would choose it), else the next coarser.
function skipEmptyLevel() {
  const levels = autoLevels();
  const i = levels.findIndex((lv) => lv.type === state.period.type && lv.span === state.period.span);
  if (i < 0) return false;
  const was = levels[i];
  const empty = emptyLevels();
  empty.add(`${was.type}:${was.span}`);
  const sw = state.lodSwitch;
  let next;
  if (sw) {
    const days = chartWidth() / sw.barSpace / levelPerDay(was);
    next = chooseLevel(days, sw.anchor != null ? sw.anchor - days * LEVEL_MS.day / 2 : NaN);
  } else {
    next = levels.slice(i + 1).find((lv) => !empty.has(`${lv.type}:${lv.span}`));
  }
  if (!next || (next.type === was.type && next.span === was.span)) return false;
  if (sw) state.lodSwitch = { ...sw, barSpace: sw.barSpace * levelPerDay(was) / levelPerDay(next) };  // same span of time
  state.period = { type: next.type, span: next.span };
  renderPeriods();
  chart.setPeriod(state.period);
  return true;
}

// Within one zoom step (half the span) of a finer level built from 30-minute bins (hours and 30
// minutes, gui/window.py _half_hours), that level's window is asked for once in the background, so
// the server has read its minutes before the zoom gets there: days to 4 hours took a second or
// more. Plain fetch: a failure here must not pause the chart's own requests (api).
function prefetchFiner(w, cur) {
  const left = w.centre == null ? w.fromMs : w.centre - w.days * LEVEL_MS.day / 4;  // zoomed in on the centre
  const next = chooseLevel(w.days / 2, left, null);
  if (!next || (next.type === cur.type && next.span === cur.span) || PRICE_BASED.has(state.chartType)) return;
  if (!(next.type === 'hour' || (next.type === 'minute' && next.span === 30))) return;
  const to = w.atLatest || w.centre == null ? null : Math.round(w.centre + w.days * LEVEL_MS.day * 1.5);
  const key = `${state.source}:${state.ticker}|${state.session}|${next.type}${next.span}|${to == null ? 'now' : Math.round(to / LEVEL_MS.month)}`;
  state.prefetched ??= new Set();
  if (state.prefetched.has(key)) return;
  state.prefetched.add(key);
  const q = { source: state.source, ticker: state.ticker, period: next.type, span: next.span, session: state.session,
              limit: autoPage(), adjust: state.adjust, lod: 1, ...typeQuery(), ...(to == null ? {} : { to }) };
  fetch(`/api/bars?${new URLSearchParams(q)}`).catch(() => {});
}

// A symbol opens on its whole history: every bar of the opening level across the chart.
function fitWhole(n) {
  if (!n) return;
  if (!chartShown()) { state.fitPending = n; return; }  // done when the chart is shown
  chart.setBarSpace(Math.max(MIN_BAR_SPACE, (chartWidth() - DEFAULT_RIGHT_OFFSET) / n));
  chart.scrollToRealTime(0);
}

let autoTimer = null;
const autoSoon = () => { clearTimeout(autoTimer); autoTimer = setTimeout(checkLod, 200); };  // once a zoom or scroll settles

function finishLodSwitch() {
  const sw = state.lodSwitch;
  if (!sw) return;
  state.lodSwitch = null;
  chart.setBarSpace(Math.max(MIN_BAR_SPACE, sw.barSpace));
  if (sw.atLatest || sw.anchor == null) {
    chart.scrollToRealTime(0);
  } else {
    // Put the anchor bar back where it was (model switch) or in the centre (detail-level switch).
    chart.scrollToTimestamp(sw.anchor, 0);
    const x = chart.convertToPixel({ timestamp: sw.anchor }, { paneId: 'candle_pane' }).x;
    const target = Number.isFinite(sw.anchorX) ? sw.anchorX : chartWidth() / 2;
    if (Number.isFinite(x)) chart.scrollByDistance(target - x, 0);
  }
  updateStatus(chart.getDataList().length);
}

chart.subscribeAction('onZoom', autoSoon);
// scrolled (or moved) to where the finer data ends or starts: at full zoom no zoom event comes
chart.subscribeAction('onVisibleRangeChange', autoSoon);

// The newest bars: scrolled to when they are loaded; loaded first when the chart shows a window in
// the past (its page ends before them), at the level it is at.
function goLatest(ms = 200) {
  if (state.hasNewer) {
    state.lodSwitch = null;
    chart.resetData();
    return;
  }
  chart.scrollToRealTime(ms);
}

// -- chart types -------------------------------------------------------------------
// Native KLineChart candle types; transformed types come back from the server as OHLC
// boxes and render as candles; the rest are drawn by the CHART_STYLE renderer below.
const NATIVE = { candles: 'candle_solid', bars: 'ohlc', area: 'area', heikin_ashi: 'candle_solid',
                 renko: 'candle_solid', line_break: 'candle_solid', range: 'candle_solid' };
const TRANSFORMED = new Set(['heikin_ashi', 'renko', 'line_break', 'kagi', 'point_figure', 'range']);
// Types the server builds (transformed, or time bars with fields of their own: box plots' quartiles):
// asked for by name, and reloaded when chosen or left.
const SERVER_TYPES = new Set([...TRANSFORMED, 'box']);
const PRICE_BASED = new Set(['renko', 'line_break', 'kagi', 'point_figure', 'range']);
const LINE = '#2962ff';
const CLEAR = 'rgba(0,0,0,0)';

function typeQuery() {
  // the auto-ranging chart: the server reads only the window asked for (gui/window.py); all data or the filtered
  const auto = { auto: 1, ...(state.unfiltered ? { unfiltered: 1 } : {}) };
  if (!SERVER_TYPES.has(state.chartType)) return auto;
  const p = state.typeParams[state.chartType] || {};
  return { ...auto, type: state.chartType, box: p.box || undefined, reversal: p.reversal || undefined,
           lines: p.lines || undefined };
}

// -- models: price, 1st / 2nd derivative, integral, Fourier low-pass (computed by the server) ----
const MODEL_DEFAULTS = { kind: 'price', basis: 'price', per: 'hour', detail: 80, gaps: true };
state.model = { ...MODEL_DEFAULTS, ...prefs.get('model', {}) };

// The Stock and Analysis views show the same symbol (and feed, session and futures adjustment) but
// each has its own chart content: interval, chart type and settings, model, indicators and their
// pane heights, zoom and position, and drawings. The active one lives in `state`; the other waits
// in state.workspaces until its view is shown (see switchWorkspace).
const WORKSPACES = ['stock', 'analysis', 'trading'];
state.workspace = WORKSPACES.includes(prefs.get('workspace')) ? prefs.get('workspace') : 'stock';
state.workspaces = prefs.get('workspaces', {});
// Chart types that only make sense for prices (bricks, Heikin Ashi, Kagi, P&F).
const PRICE_ONLY_TYPES = new Set([...TRANSFORMED]);
const CANDLE_LIKE = new Set(['candles', 'bars', 'hollow', 'volume_candles', 'box']);

// Detail slider: 100 = a 5-bar Savitzky-Golay window (full resolution), 0 = 401 bars (very smooth).
function windowForDetail(detail) {
  const w = Math.round(5 * Math.pow(401 / 5, (100 - detail) / 100));
  return w % 2 ? w : w + 1;
}

function modelQuery() {
  const m = state.model;
  if (m.kind === 'price') return {};
  return { model: m.kind, basis: m.basis, per: m.per, window: windowForDetail(m.detail), gaps: m.gaps ? 1 : 0 };
}

// Drawings are kept per model (a trend line on prices means nothing on a slope chart) and per view.
function drawingsKey() {
  const m = state.model;
  const key = m.kind === 'price' ? state.ticker : `${state.ticker}|${m.kind}|${m.basis}|${m.per}`;
  return state.workspace === 'stock' ? key : `${state.workspace}:${key}`;
}

function windowSpan() {
  const p = state.period || { type: 'minute', span: 1 };
  const seconds = { second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2592000 }[p.type] * p.span;
  return fmtDuration(windowForDetail(state.model.detail) * seconds * 1000);
}

function renderModelControls() {
  const m = state.model;
  $('modelSelect').value = m.kind;
  $('modelParams').hidden = m.kind === 'price';
  $('modelBasis').value = m.basis;
  $('modelPer').value = m.per;
  $('modelDetail').value = m.detail;
  $('modelGaps').checked = m.gaps;
  $('modelDetail').disabled = m.kind === 'integral';
  const note = $('modelWindow');
  const hidden = m.kind === 'd1' || m.kind === 'd2' ? state.modelHidden || 0 : 0;
  note.textContent = m.kind === 'integral' ? ''
    : m.kind === 'fourier' ? `cycles of ${windowForDetail(m.detail)}+ bars (≈ ${windowSpan()}) kept`
      : `${windowForDetail(m.detail)} bars ≈ ${windowSpan()}`
    + (hidden ? ` · ${compact(hidden)} bars not drawn` : '');
  const cut = m.kind === 'fourier' && state.approximate;  // a market without gaps: fitted around the window
  if (cut) note.textContent += ' · fitted around the window';
  note.classList.toggle('warn', hidden > 0 || !!cut);
  note.title = cut ? state.approximate : hidden ? 'Stretches of data shorter than the smoothing window are not drawn: '
    + 'fitted to a whole short stretch, the slope would be a single polynomial, not the price\'s.' : '';
  for (const opt of $('typeSelect').options) opt.disabled = m.kind !== 'price' && PRICE_ONLY_TYPES.has(opt.value);
}

function setModel(patch) {
  saveDrawings();
  const wasPrice = state.model.kind === 'price';
  state.model = { ...state.model, ...patch };
  prefs.set('model', state.model);
  if (wasPrice && state.model.kind !== 'price' && (CANDLE_LIKE.has(state.chartType) || PRICE_ONLY_TYPES.has(state.chartType))) {
    state.priceChartType = state.chartType;  // a value series reads best as a line; restored on return
    state.chartType = 'line';
  } else if (!wasPrice && state.model.kind === 'price' && state.priceChartType) {
    state.chartType = state.priceChartType;
    state.priceChartType = null;
  }
  $('typeSelect').value = state.chartType;
  markDirty();
  state.restoredFor = null;
  chart.removeOverlay({ groupId: 'user' });
  // Keep the same time window: same zoom (and detail level) and the same moment in the centre,
  // or pinned to the latest bar. Model series have exactly the same bars as the price.
  const data = chart.getDataList();
  const { to } = chart.getVisibleRange();
  const edge = data[Math.min(data.length - 1, Math.max(0, to - 1))];  // right-most visible bar
  state.lodSwitch = {
    barSpace: chart.getBarSpace().bar, anchor: edge ? edge.timestamp : null,
    anchorX: edge ? chart.convertToPixel({ timestamp: edge.timestamp }, { paneId: 'candle_pane' }).x : null,
    atLatest: !data.length || to >= data.length,
  };
  renderModelControls();
  applyChartType();
  renderTypeParams();
  reloadWithModelPrecision();
}

// Load once: learn the new values' decimals first (a 1-bar request), then set them together
// with the reload, instead of loading, discovering the precision and loading again.
async function reloadWithModelPrecision() {
  const sym = chart.getSymbol();
  let want = state.meta?.pricePrecision;
  // the auto-ranging chart's first page is as quick as the probe and says its decimals (applyModelPrecision)
  if (state.meta?.auto && state.model.kind !== 'price') { chart.resetData(); return; }
  if (sym && state.model.kind !== 'price') {
    try {
      const p = chart.getPeriod();
      const model = JSON.stringify(modelQuery());
      const res = await api('/api/bars', { source: sym.source, ticker: sym.ticker, period: p.type, span: p.span,
                                           session: state.session, limit: 1, adjust: state.adjust, ...modelQuery() });
      const now = chart.getSymbol();
      // another symbol or model meanwhile (the slider dragged on): its own load, with its own decimals
      if (now?.ticker !== sym.ticker || now?.source !== sym.source || JSON.stringify(modelQuery()) !== model) return;
      want = res.model?.precision ?? want;
      state.modelUnit = res.model?.unit ?? null;
      state.modelHidden = res.model?.hidden ?? 0;
      renderModelControls();
    } catch (err) { showError(err); }
  }
  if (sym && want != null && sym.pricePrecision !== want) chart.setSymbol({ ...sym, pricePrecision: want });  // reloads
  else chart.resetData();
}

// Model values need their own decimals; prices go back to the symbol's precision.
function applyModelPrecision(res) {
  const sym = chart.getSymbol();
  if (!sym || !state.meta) return false;
  const want = res.model ? res.model.precision : state.meta.pricePrecision;
  state.modelUnit = res.model ? res.model.unit : null;
  if (res.model && (state.modelHidden || 0) !== (res.model.hidden || 0)) {
    state.modelHidden = res.model.hidden || 0;
    renderModelControls();
  }
  // Zooming through levels with one model keeps its decimals unless a level needs more: setting
  // them reloads the chart, which doubled every level switch.
  const key = `${sym.source}:${sym.ticker}|${JSON.stringify(modelQuery())}`;
  const zooming = res.model && state.meta?.auto && state.precisionKey === key;
  state.precisionKey = key;
  if (sym.pricePrecision === want || (zooming && want < sym.pricePrecision)) return false;
  chart.setSymbol({ ...sym, pricePrecision: want });  // reloads once
  return true;
}

// Zero line for derivatives: above = rising (accelerating), below = falling (decelerating).
function drawModelGuides() {
  chart.removeOverlay({ groupId: 'model' });
  if (state.model.kind !== 'd1' && state.model.kind !== 'd2') return;
  const d = chart.getDataList();
  if (!d.length) return;
  chart.createOverlay({ name: 'horizontalStraightLine', groupId: 'model', lock: true,
                        points: [{ timestamp: d[0].timestamp, value: 0 }],
                        styles: { line: { style: 'dashed', dashedValue: [4, 4], color: '#787b86', size: 1 } } });
}

$('modelSelect').addEventListener('change', (e) => setModel({ kind: e.target.value }));
$('modelBasis').addEventListener('change', (e) => setModel({ basis: e.target.value }));
$('modelPer').addEventListener('change', (e) => setModel({ per: e.target.value }));
$('modelGaps').addEventListener('change', (e) => setModel({ gaps: e.target.checked }));
// The chart follows the slider while it is dragged (at most ~4 reloads a second, always ending
// on the position it was let go at), not only when it is released.
const detailDrag = { last: 0, timer: 0, applied: null };
function applyDetail(detail) {
  clearTimeout(detailDrag.timer);
  detailDrag.last = performance.now();
  if (detailDrag.applied === detail) return;
  detailDrag.applied = detail;
  setModel({ detail });
}
$('modelDetail').addEventListener('input', (e) => {
  const detail = Number(e.target.value);
  state.model.detail = detail;
  $('modelWindow').textContent = `${windowForDetail(detail)} bars ≈ ${windowSpan()}`;
  clearTimeout(detailDrag.timer);
  const wait = Math.max(0, 250 - (performance.now() - detailDrag.last));
  detailDrag.timer = setTimeout(() => applyDetail(detail), wait);
});
$('modelDetail').addEventListener('change', (e) => applyDetail(Number(e.target.value)));

// The theme's own candle colours, captured before any chart type hides them.
const DEFAULT_CANDLE = (() => {
  const c = chart.getStyles().candle;
  return { bar: { ...c.bar }, priceMark: { high: { ...c.priceMark.high }, low: { ...c.priceMark.low } },
           area: JSON.parse(JSON.stringify(c.area)) };
})();

// -- GPU rendering -------------------------------------------------------------------
// Candle-shaped chart types are drawn by WebGL2 (gpu.js) underneath KLineChart's canvas,
// which keeps axes, crosshair, indicators and drawings. KLineChart's own candles are
// switched to an invisible area line so it does no per-candle work.
const GPU_TYPES = new Set(['candles', 'bars', 'heikin_ashi', 'renko', 'line_break', 'range', 'volume_candles',
                           'line', 'line_markers', 'step', 'area']);
const GPU_INFO = window.GpuBars ? window.GpuBars.probe() : { supported: false, renderer: null };
state.gpu = GPU_INFO.supported && prefs.get('gpu', true);
let gpuBars = null;
state.gpuFrameMs = null;

function gpuActive() {
  return state.gpu && GPU_TYPES.has(state.chartType) && !gpuBars?.lost;
}

// A lost GPU context (a driver reset, the GPU process restarting) took every candle with it, for
// good: the chart's own candles come back meanwhile (applyChartType draws on the CPU while it is
// lost), and the GPU's again once the browser restores the context.
function ensureGpu() {
  if (gpuBars) return gpuBars;
  try {
    gpuBars = new window.GpuBars({ onLost: applyChartType, onRestored: applyChartType });
  } catch (err) {
    state.gpu = false;
    showError(`GPU rendering unavailable: ${err.message}`);
  }
  return gpuBars;
}

// KLineChart (v10) places bar i at x = floor(a + i*bar + 0.5); the shader applies the same
// rounding, so GPU bars land on exactly the chart's pixels. Falls back to an estimate from the
// public API (within one pixel) if the internals ever change.
function xMapping(c, xAxis, count) {
  const store = c._chartStore;
  const bar = c.getBarSpace().bar;
  if (store && typeof store._totalBarSpace === 'number' && typeof store._lastBarRightSideDiffBarCount === 'number') {
    return [store._totalBarSpace - (count + store._lastBarRightSideDiffBarCount - 0.5) * bar, bar];
  }
  return [xAxis.convertToPixel(count - 1) - (count - 1) * bar, bar];
}

// The y axis maps price -> round((1 - (price - realFrom) / realRange) * height); pass the
// unrounded line and let the shader round the same way.
function yMapping(yAxis) {
  const r = yAxis.getRange();
  const height = yAxis.getBounding().height;
  const realRange = r.realRange || 1;
  const sign = yAxis.reverse ? 1 : -1;
  const scale = (sign * height) / realRange;
  return [(yAxis.reverse ? 0 : height) - r.realFrom * scale, scale];
}

kc.registerIndicator({
  name: 'GPU_BARS',
  shortName: '',
  series: 'price',
  figures: [],
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: () => ({ name: '', calcParamsText: '', features: [], legends: [] }),
  draw: ({ chart: c, bounding, xAxis, yAxis }) => {
    const g = ensureGpu();
    if (!g || !gpuActive()) return true;
    const host = c.getDom('candle_pane', 'main');
    if (host) {
      if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
      g.attach(host);
    }
    const data = c.getDataList();
    g.upload(data);
    const { from, to } = c.getVisibleRange();
    state.gpuFrameMs = g.render({
      width: bounding.width, height: bounding.height, from: from - 1, to: to + 1,
      xMap: xMapping(c, xAxis, data.length), yMap: yMapping(yAxis),
      bodyWidth: Math.max(1, c.getBarSpace().gapBar), style: state.chartType,
      upColor: DEFAULT_CANDLE.bar.upColor, downColor: DEFAULT_CANDLE.bar.downColor, lineColor: LINE,
    });
    return true;
  },
});

// The button shows the setting; when the current chart type is drawn on the CPU anyway it is
// marked "partial" and its tooltip says so.
function renderGpuButton() {
  const b = $('gpuBtn');
  b.classList.toggle('on', state.gpu && GPU_INFO.supported);
  b.classList.toggle('partial', state.gpu && (!GPU_TYPES.has(state.chartType) || !!gpuBars?.lost));
  b.setAttribute('aria-pressed', String(!!state.gpu));
  b.disabled = !GPU_INFO.supported;
  b.title = !GPU_INFO.supported ? 'GPU rendering not available in this browser (no WebGL2)'
    : `GPU rendering ${state.gpu ? 'on' : 'off'} — ${GPU_INFO.renderer}${GPU_INFO.software ? ' (software, no hardware acceleration)' : ''}`
      + (state.gpu && !GPU_TYPES.has(state.chartType) ? '. This chart type is drawn on the CPU.' : '')
      + (state.gpu && gpuBars?.lost ? '. The GPU context was lost: drawn on the CPU until the browser gives it back.' : '');
}

$('gpuBtn').addEventListener('click', () => {
  state.gpu = !state.gpu;
  prefs.set('gpu', state.gpu);
  applyChartType();
});

// A fixed price-axis width: with "auto" the axis widens for long values (an integral's
// −46,479.70) and the plot shifts sideways, so switching models would move the time window.
const Y_AXIS_WIDTH = 92;

// The chart library's light theme has pale grey axis text and grid; darker here so labels read on
// the grey page as well as the body text does.
const LIGHT_CHART = {
  grid: { horizontal: { color: '#cfd4de' }, vertical: { color: '#cfd4de' } },
  xAxis: { axisLine: { color: '#9aa1b0' }, tickLine: { color: '#9aa1b0' }, tickText: { color: '#262a35' } },
  yAxis: { axisLine: { color: '#9aa1b0' }, tickLine: { color: '#9aa1b0' }, tickText: { color: '#262a35' } },
  separator: { color: '#b3b9c6' },
  crosshair: {
    horizontal: { line: { color: '#5a6070' }, text: { backgroundColor: '#2b303c', borderColor: '#2b303c' } },
    vertical: { line: { color: '#5a6070' }, text: { backgroundColor: '#2b303c', borderColor: '#2b303c' } },
  },
  candle: { tooltip: { text: { color: '#1b1f29' } } },
  indicator: { tooltip: { text: { color: '#1b1f29' } } },
};

// Panes a little apart: a gap between the price pane and each indicator pane, and the same gap above
// the time axis (axisGap, behind the chart: the axis's labels and the crosshair's time stay on top),
// in the window's grey (the theme's --panel, as the toolbar and side panel), not the plot's colour:
// the same colour as the panes showed no gap at all. The crosshair's dashed line stays in the panes
// (the library draws it pane by pane): nothing is drawn in the gaps. As wide as the page's gap between
// panels (style.css --gap: under the toolbar too); 14 px stood out at the chart's foot.
const PANE_GAP = parseFloat(getComputedStyle(document.body).getPropertyValue('--gap')) || 6;
const axisGap = Object.assign(document.createElement('div'), { className: 'axis-gap' });
$('chart').append(axisGap);

const gapColor = () => getComputedStyle(document.body).getPropertyValue('--frame').trim() || '#2a2e39';  // the page's grey (style.css, "Panels")

function applyPaneSpacing() {
  chart.setStyles({ separator: { size: PANE_GAP, color: gapColor() }, xAxis: { tickText: { marginStart: PANE_GAP + 4 } } });
  placeAxisGap();
}

function placeAxisGap() {
  const axis = chart.getSize('x_axis_pane');
  if (!axis) return;
  Object.assign(axisGap.style, { top: `${axis.top}px`, height: `${PANE_GAP}px`, width: `${axis.width}px`, background: gapColor() });
  placeTimezone();  // on the date row below the band
}
new ResizeObserver(() => requestAnimationFrame(placeAxisGap)).observe($('chart'));  // panes added, dragged, resized

// The time zone switch, in the corner under the price axis (as TradingView has it): the zone the
// time axis and crosshair read intraday times in. Its list is a wheel: TZ_ROWS zones in view, the
// mouse wheel turns it a zone a notch, round and round (past GMT+14 comes the exchange's zone, then
// GMT−12 again).
const TZ_ROWS = 6;
const tzBtn = Object.assign(document.createElement('button'), { type: 'button', className: 'tz-btn', id: 'tzBtn' });
const tzMenu = Object.assign(document.createElement('div'), { className: 'tz-menu', hidden: true });
$('chart').append(tzBtn, tzMenu);
let tzTop = 0;  // the zone at the top of the wheel's window (an index into TIMEZONES)

function zoneName(z) {
  return z === 'exchange' ? 'Exchange' : `${zoneCities(z) || z} (${gmtLabel(z)})`;
}

// The switch sits on the time axis's date row, as tall as it and centred on the dates' line (the
// axis draws its dates from the top of their margin, in its tick-text font): below the gap band,
// under the price axis. Its list opens upward from it.
const TZ_INSET = 8;  // px between the plot and the time zone switch

function placeTimezone() {
  const axis = chart.getSize('x_axis_pane');
  if (!axis) return;
  const text = chart.getStyles().xAxis.tickText;
  const mid = (text.marginStart ?? 4) + (text.size ?? 12) / 2;  // the dates' centre, from the axis's top
  const height = 2 * (mid - PANE_GAP);
  // The whole corner under the price axis, below the gap band (its highlight fills it), set in from the
  // plot by TZ_INSET; its text on the dates' row.
  const corner = Math.max(height, axis.height - PANE_GAP);
  Object.assign(tzBtn.style, { width: `${Y_AXIS_WIDTH - TZ_INSET}px`, top: `${axis.top + PANE_GAP}px`, bottom: 'auto',
                               height: `${corner}px`, lineHeight: `${height}px`, paddingBottom: `${corner - height}px`,
                               fontSize: `${text.size ?? 12}px`, color: text.color });
  tzMenu.style.bottom = `${$('chart').clientHeight - (axis.top + PANE_GAP) + 4}px`;
}

function renderTimezone() {
  tzBtn.textContent = zoneShort(pickedZone());  // "PDT" in summer, "PST" in winter: its daylight saving
  tzBtn.title = `Time zone of the time axis and crosshair: ${zoneName(state.timezone)}. Days, weeks and months keep `
    + 'the market\'s own dates. Click to change; the mouse wheel turns the list.';
  placeTimezone();
  if (tzMenu.hidden) return;
  const n = TIMEZONES.length;
  const clock = (z) => new Intl.DateTimeFormat('en-US', { timeZone: z === 'exchange' ? TZ : z, hour: '2-digit',
                                                            minute: '2-digit', hourCycle: 'h23' }).format(new Date());
  tzMenu.innerHTML = Array.from({ length: TZ_ROWS }, (_, i) => TIMEZONES[(((tzTop + i) % n) + n) % n]).map((z) =>
    `<button type="button" data-tz="${esc(z)}" class="${z === state.timezone ? 'on' : ''}"`
    + ` title="${esc(z === 'exchange' ? 'New York, with its daylight saving'
      : `${z}: ${keepsDaylightSaving(z) ? 'with its daylight saving' : 'no daylight saving'}`)}">`
    + `<span class="tz-name">${esc(z === 'exchange' ? 'Exchange' : gmtLabel(z))}</span>`
    + `<span class="tz-city p-muted">${esc(z === 'exchange' ? 'New York' : zoneCities(z))}</span>`
    + `<span class="tz-clock p-muted">${esc(clock(z))}</span></button>`).join('');
}

tzBtn.addEventListener('click', (e) => {
  e.stopPropagation();
  tzMenu.hidden = !tzMenu.hidden;
  tzTop = TIMEZONES.indexOf(state.timezone) - Math.floor(TZ_ROWS / 2);  // the zone in use in the middle
  renderTimezone();
});
tzMenu.addEventListener('wheel', (e) => {
  e.preventDefault();  // the wheel turns the list, never the page or the chart
  tzTop = ((tzTop + Math.sign(e.deltaY)) % TIMEZONES.length + TIMEZONES.length) % TIMEZONES.length;
  renderTimezone();
}, { passive: false });
tzMenu.addEventListener('click', (e) => {
  const b = e.target.closest('[data-tz]');
  if (!b) return;
  state.timezone = b.dataset.tz;
  prefs.set('timezone', state.timezone);
  tzMenu.hidden = true;
  renderTimezone();
  const zone = labelZone(chart.getPeriod());
  if (chart.getTimezone() !== zone) chart.setTimezone(zone);  // the axis and crosshair relabel, no reload
  window.renderCursor?.();
});
document.addEventListener('click', (e) => { if (!tzMenu.hidden && !tzMenu.contains(e.target)) tzMenu.hidden = true; });
window.addEventListener('resize', () => requestAnimationFrame(renderTimezone));
requestAnimationFrame(renderTimezone);

chart.subscribeAction('onCrosshairChange', placeAxisGap);  // follows a pane added or dragged (cheap)

function applyChartType() {
  const custom = !NATIVE[state.chartType];
  chart.setStyles(themeBase());
  if (themeBase() === 'light') chart.setStyles(LIGHT_CHART);
  if (THEMES[state.theme].chart) chart.setStyles(THEMES[state.theme].chart);
  chart.setStyles({ yAxis: { size: Y_AXIS_WIDTH } });
  applyPaneSpacing();
  chart.setStyles({ candle: { type: NATIVE[state.chartType] || 'candle_solid', ...DEFAULT_CANDLE } });
  chart.removeIndicator({ name: 'GPU_BARS' });
  if (gpuActive() && ensureGpu()) {
    chart.setStyles({ candle: { type: 'area', area: {
      lineColor: CLEAR, backgroundColor: [{ offset: 0, color: CLEAR }, { offset: 1, color: CLEAR }],
      point: { show: false },
    } } });
    chart.createIndicator({ name: 'GPU_BARS', paneId: 'candle_pane' }, true);
  } else if (gpuBars) {
    gpuBars.detach();
  }
  renderGpuButton();
  const gpuDraws = gpuActive() && !!gpuBars;
  if (custom && !gpuDraws) {
    // Keep the (invisible) candles for scaling, crosshair and tooltip; draw the type ourselves.
    chart.setStyles({ candle: {
      bar: { upColor: CLEAR, downColor: CLEAR, noChangeColor: CLEAR, upBorderColor: CLEAR, downBorderColor: CLEAR,
             noChangeBorderColor: CLEAR, upWickColor: CLEAR, downWickColor: CLEAR, noChangeWickColor: CLEAR },
      priceMark: { high: { show: false }, low: { show: false } },
    } });
  }
  if (state.chartType === 'area' && !gpuDraws) chart.setStyles({ candle: { area: { lineColor: LINE } } });
  chart.removeIndicator({ name: 'CHART_STYLE' });
  if (custom && !gpuDraws) chart.createIndicator({ name: 'CHART_STYLE', paneId: 'candle_pane' }, true);
  chart.removeIndicator({ name: 'DERIVED_MARKS' });
  chart.createIndicator({ name: 'DERIVED_MARKS', paneId: 'candle_pane' }, true);
}

function px(xAxis, i) { return xAxis.convertToPixel(i); }

// Derived candles: built here (from ticks, or finer bars) where the feed sent none at this interval.
// When the candles on screen mix the feed's own with derived ones, each derived one gets a small
// ring under it; when all are derived, the chip beside the interval menu says so instead.
kc.registerIndicator({
  name: 'DERIVED_MARKS',
  shortName: '',
  series: 'price',
  figures: [],
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: () => ({ name: '', calcParamsText: '', features: [], legends: [] }),
  draw: ({ ctx, chart: c, xAxis, yAxis }) => {
    const data = c.getDataList();
    const { from, to } = c.getVisibleRange();
    if (!data.length || to <= from || state.model.kind !== 'price') { renderDerivedChip(0, 0); return true; }
    let some = 0, interp = 0;
    for (let i = from; i < to; i++) {
      if (data[i]?.interp) interp++;
      else if (data[i]?.derived) some++;
    }
    renderDerivedChip(some + interp, to - from, interp);
    ctx.save();
    if (interp) {
      // Interpolated history (no data at this interval): a dashed violet line, a style used nowhere else.
      ctx.strokeStyle = INTERP_COLOR; ctx.lineWidth = 1.6; ctx.setLineDash([6, 4]); ctx.globalAlpha = 0.95;
      ctx.beginPath();
      let pen = false;
      for (let i = Math.max(0, from - 1); i < Math.min(data.length, to + 1); i++) {
        const d = data[i];
        if (!d?.interp) { pen = false; continue; }
        const x = px(xAxis, i), y = yAxis.convertToPixel(d.close);
        if (pen) ctx.lineTo(x, y); else ctx.moveTo(x, y);
        pen = true;
      }
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (some && some + interp < to - from) {
      ctx.strokeStyle = '#9aa0a6'; ctx.globalAlpha = 0.85; ctx.lineWidth = 1;
      for (let i = from; i < to; i++) {
        const d = data[i];
        if (!d?.derived || d.interp) continue;
        ctx.beginPath(); ctx.arc(px(xAxis, i), yAxis.convertToPixel(d.low) + 6, 2.5, 0, Math.PI * 2); ctx.stroke();
      }
    }
    ctx.restore();
    return true;
  },
});

const INTERP_COLOR = '#b07cff';  // interpolated history only

function renderDerivedChip(some, total, interp = 0) {
  let chip = $('derivedChip');
  if (!chip) {
    chip = document.createElement('span');
    chip.id = 'derivedChip';
    chip.className = 'derived-chip';
    $('periods').append(chip);
  }
  const label = periodLabel(state.period);
  const shown = `${some}|${total}|${interp}|${label}|${state.derivedFrom}`;  // drawn every frame: write on change
  if (chip.dataset.shown === shown) return;
  chip.dataset.shown = shown;
  chip.hidden = !some;
  chip.textContent = interp ? 'interpolated*' : some === total ? 'derived*' : '◦ derived*';
  if (interp) {
    chip.title = `* Interpolated data: where no ${label} data exists, the dashed violet line is drawn between the `
      + '1-minute bars\' volume-weighted prices (derived, not trades); candles only where real data exists.';
    return;
  }
  chip.title = some === total && state.derivedFrom
    ? `* Derived data: no feed sends ${label} candles for this symbol; these are built here from ${state.derivedFrom}.`
    : some === total
      ? '* Derived data: every candle on screen was built here (from the feed\'s trades as they stream, or its finer '
        + `bars); the feed's own ${label} candles replace them when they arrive.`
      : `* Candles marked ◦ (${some} of ${total} on screen) are derived: built here from finer data where the feed sent `
        + 'none at this interval. The feed\'s own replace them when they arrive.';
}

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
    } else if (type === 'box') {
      // Whiskers high to low, the box from the 25th to the 75th percentile of the price traded in
      // the bar, a bar across at the median; open and close ticks left and right. Up or down by close.
      const cap = Math.max(1, half * 0.5);
      for (let i = lo; i < hi; i++) {
        const d = data[i], x = px(xAxis, i) + 0.5;
        const color = d.close >= d.open ? UP : DOWN;
        ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x, y(d.high)); ctx.lineTo(x, y(d.low));
        ctx.moveTo(x - cap, y(d.high)); ctx.lineTo(x + cap, y(d.high));
        ctx.moveTo(x - cap, y(d.low)); ctx.lineTo(x + cap, y(d.low));
        ctx.moveTo(x - half, y(d.open)); ctx.lineTo(x, y(d.open));
        ctx.moveTo(x, y(d.close)); ctx.lineTo(x + half, y(d.close));
        ctx.stroke();
        if (d.q1 == null) continue;  // nothing finer inside this bar
        const top = y(d.q3), bottom = y(d.q1);
        ctx.globalAlpha = 0.3; ctx.fillRect(x - half, top, half * 2, Math.max(1, bottom - top)); ctx.globalAlpha = 1;
        ctx.strokeRect(x - half, top, half * 2, Math.max(1, bottom - top));
        ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x - half, y(d.med)); ctx.lineTo(x + half, y(d.med)); ctx.stroke();
      }
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
  if (state.approximate && (t === 'range' || t === 'box')) {
    const s = document.createElement('span');
    s.className = 'approx'; s.textContent = t === 'box' ? 'derived*' : 'approx.'; s.title = state.approximate;
    box.append(s);
  }
}

$('typeSelect').value = state.chartType;
$('typeSelect').addEventListener('change', (e) => {
  const wasServerBuilt = SERVER_TYPES.has(state.chartType);
  state.chartType = e.target.value;
  prefs.set('chartType', state.chartType); markDirty();
  state.styleUsed = null;
  applyChartType();
  renderTypeParams();
  renderPeriods();  // bricks: no Detail (one level whatever the zoom)
  // Between two time-based types the same bars (and detail level) are reused; anything
  // involving a type the server builds reloads, and the detail level is chosen again after loading.
  if (wasServerBuilt || SERVER_TYPES.has(state.chartType)) {
    resetLod();
    chart.resetData();
  }
});

// -- symbol selection ----------------------------------------------------------
// Picking a symbol (the search dialog and its map, the watchlist, the news, the Fetcher) opens it
// on the symbol page at once; the Analysis view keeps its own chart. The list of symbols is read
// again only for one it does not know yet (a symbol added since): reading all of them first kept
// a pick waiting for the whole list (half a minute with thousands of symbols).
function openSymbol(ticker, source = null) {
  if (!['stock', 'analysis'].includes(document.body.dataset.pane)) window.showPane?.('stock', { toggle: false });
  state.picked = String(ticker).toUpperCase();  // yours: it goes in your register (activity.js), not one opened by itself
  const known = state.symbols.some((s) => s.ticker === String(ticker).toUpperCase());
  return (known ? selectSymbol(ticker, source) : loadSymbols().then(() => selectSymbol(ticker, source))).catch(showError);
}

window.addEventListener('massive:open-symbol', (e) => openSymbol(e.detail));

async function loadSymbols() {
  state.symbols = await api('/api/symbols');
  state.symbolsLoaded = true;
}

function symbolInfo(ticker) {
  return state.symbols.find((s) => s.ticker === ticker) || { ticker, feeds: [] };
}

// The symbol opened when nothing is saved (a new profile): the first with data stored. The first of
// the list ("0A1!", a future only queued) opened nothing but "no data stored".
function firstStored() {
  return state.symbols.find((s) => s.feeds?.some((f) => f.data?.length)) || null;
}

// A symbol opens on every feed merged (each bar from the best feed that has it: the reference
// exchange first, every other fetcher's history where it has none) unless a feed is asked for.
async function selectSymbol(ticker, source = null) {
  source = source ?? MERGED;
  const pick = state.selectToken = (state.selectToken || 0) + 1;
  const meta = await api('/api/meta', { source, ticker, adjust: state.adjust, auto: 1 });
  if (pick !== state.selectToken) return;  // another symbol was picked while this one was asked for
  resetLod();
  $('sessionSelect').hidden = meta.aroundTheClock;  // fx and futures trade around the clock
  $('adjustSelect').hidden = meta.asset !== 'future';
  saveDrawings();
  state.ticker = meta.ticker;
  state.source = source;
  state.meta = meta;
  state.restoredFor = null;
  prefs.set('last', { ticker: meta.ticker, source });
  markDirty();

  // Whole history on screen: the finest level whose candles for all of it fit the chart.
  const levels = autoLevels();
  if (levels.length) {
    const first = Math.min(...levels.map((lv) => lv.first)), last = Math.max(...levels.map((lv) => lv.last));
    const lv = chooseLevel((last - first) / 864e5, first);
    state.period = { type: lv.type, span: lv.span };
    state.fitAll = true;
  }
  const info = symbolInfo(meta.ticker);
  renderSymbolButton();
  renderFeeds(info);
  renderPeriods();
  chart.removeOverlay({ groupId: 'user' });
  state.settingPeriod = true;  // its load is skipped (getBars): the symbol's, next, is the one wanted
  try { chart.setPeriod(state.period); } finally { state.settingPeriod = false; }
  chart.setSymbol({
    ticker: meta.ticker, source, name: info.name || meta.ticker,
    pricePrecision: meta.pricePrecision, volumePrecision: meta.volumePrecision,
  });
  loadInfo();  // side panel: symbol, statistics, business details
  window.dispatchEvent(new CustomEvent('massive:symbol', { detail: meta.ticker }));
}

function renderFeeds(info) {
  const sel = $('feedSelect');
  sel.innerHTML = '';
  const feeds = info.feeds || [];
  const m = info.models;
  const add = (value, label) => { const o = document.createElement('option'); o.value = value; o.textContent = label; sel.append(o); };
  if (m) {  // an asset on several exchanges: its models first
    const q = Object.fromEntries((m.exchanges || []).map((x) => [x.feed, x.quality]));
    const ex = Object.fromEntries((m.exchanges || []).map((x) => [x.feed, x.exchange]));
    const name = (feed) => (ex[feed] && ex[feed] !== feed ? `${ex[feed]} (via ${feed})` : feed);
    if (m.reference) add(m.reference, `Reference: ${name(m.reference)} (gold standard)`);
    if (m.composite) add('composite', `Composite: ${m.exchanges.length} exchanges, with deviation`);
    if (m.consolidated) add(m.consolidated, `Consolidated: ${m.consolidated}`);
    feeds.filter((f) => f.source !== m.reference && f.source !== m.consolidated)
      .forEach((f) => add(f.source, `Exchange: ${name(f.source)}${q[f.source] != null ? `, quality ${q[f.source].toFixed(2)}` : ''}`));
    add(MERGED, 'All feeds, merged');
  } else {
    add(MERGED, feeds.length > 1 ? `All feeds (${feeds.map((f) => f.source).join(' > ')})` : `Feed: ${feeds[0]?.source || '-'}`);
    if (feeds.length > 1) feeds.forEach((f) => add(f.source, `Only ${f.source}`));
  }
  sel.value = state.source;
  sel.hidden = !feeds.length;
  chart.removeIndicator({ name: 'XDEV' });
  if (state.source === 'composite') chart.createIndicator({ name: 'XDEV', paneId: 'candle_pane' }, true);
}

// Composite model: the deviation across exchanges around each bar - a faint envelope from the
// lowest low to the highest high of any exchange, and a band of one standard deviation of the
// exchanges' closes around the composite close.
kc.registerIndicator({
  name: 'XDEV',
  shortName: 'Exchanges',
  series: 'price',
  figures: [],
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: ({ indicator, crosshair, chart: c }) => {
    const d = c.getDataList()[crosshair.dataIndex ?? -1];
    const text = d && d.dev_sd != null
      ? `${d.venues_n} exchanges · σ ${(d.dev_sd / d.close * 1e4).toFixed(1)} bps · range ${d.env_low?.toFixed?.(2)}–${d.env_high?.toFixed?.(2)}`
      : '';
    return { name: 'Exchanges', calcParamsText: '', features: [], legends: text ? [{ title: '', value: { text, color: '#9aa0ae' } }] : [] };
  },
  draw: ({ ctx, chart: c, xAxis, yAxis }) => {
    const data = c.getDataList();
    const { from, to } = c.getVisibleRange();
    const lo = Math.max(0, from - 1), hi = Math.min(data.length, to + 1);
    const pts = [];
    for (let i = lo; i < hi; i++) if (data[i].env_high != null) pts.push(i);
    if (!pts.length) return true;
    const x = (i) => xAxis.convertToPixel(i), y = (v) => yAxis.convertToPixel(v);
    const band = (top, bottom, fill) => {
      ctx.beginPath();
      pts.forEach((i, k) => ctx[k ? 'lineTo' : 'moveTo'](x(i), y(top(data[i]))));
      for (let k = pts.length - 1; k >= 0; k--) ctx.lineTo(x(pts[k]), y(bottom(data[pts[k]])));
      ctx.closePath(); ctx.fillStyle = fill; ctx.fill();
    };
    ctx.save();
    band((d) => d.env_high, (d) => d.env_low, 'rgba(120,123,134,0.12)');
    band((d) => d.close + (d.dev_sd || 0), (d) => d.close - (d.dev_sd || 0), 'rgba(41,98,255,0.22)');
    ctx.restore();
    return true;
  },
});

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
    return [  // a roll says only the contract the series becomes there (the side panel has the rest)
      { type: 'line', ignoreEvent: true, attrs: { coordinates: [{ x, y: 0 }, { x, y: bounding.height }] },
        styles: { style: 'dashed', dashedValue: [3, 3], color: '#9598a1', size: 1 } },
      { type: 'text', ignoreEvent: true, attrs: { x: x + 4, y: 4, text: r.to, align: 'left', baseline: 'top' },
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

// The symbol button shows the symbol's logo, ticker, exchange and its data state, like
// TradingView; the picker is an app switcher: a grid of tiles with logos, last price, change and
// average dollar volume, sorted (by default) by dollar volume ascending, with keyboard navigation.
function renderSymbolButton() {
  const { symbolIcon, dataIcon, esc: e } = window.MassiveIcons;
  const info = symbolInfo(state.ticker);
  const data = window.Activity?.data?.symbols?.find((x) => x.ticker === state.ticker) || info.data;
  $('symbolLabel').innerHTML = state.ticker
    ? `${symbolIcon(info, 20)}<b>${e(state.ticker)}</b>${info.exchange ? `<span class="p-muted">${e(info.exchangeName || info.exchange)}</span>` : ''}${dataIcon(data, 14)}`
    : 'Select symbol';
}
window.addEventListener('massive:activity', renderSymbolButton);

const SORTS = [
  { key: 'dv-asc', label: '$ volume ↑', title: 'Average daily dollar volume (30 sessions), ascending' },
  { key: 'dv-desc', label: '$ volume ↓', title: 'Average daily dollar volume (30 sessions), descending' },
  { key: 'name', label: 'A–Z', title: 'Ticker, A to Z' },
  { key: 'change', label: 'Change', title: 'Last session change, highest first' },
];
state.switcherSort = prefs.get('switcherSort', 'dv-asc');

function compactMoney(v) {
  if (v == null) return '–';
  const a = Math.abs(v);
  return a >= 1e12 ? `$${(v / 1e12).toFixed(1)}T` : a >= 1e9 ? `$${(v / 1e9).toFixed(1)}B`
    : a >= 1e6 ? `$${(v / 1e6).toFixed(0)}M` : `$${Math.round(v).toLocaleString()}`;
}

function sortedSymbols(list) {
  const key = state.switcherSort;
  const dv = (s) => s.market?.dollarVolume ?? -1;
  const out = [...list];
  if (key === 'name') out.sort((a, b) => a.ticker.localeCompare(b.ticker));
  else if (key === 'change') out.sort((a, b) => (b.market?.changePct ?? -1e9) - (a.market?.changePct ?? -1e9));
  else out.sort((a, b) => (key === 'dv-asc' ? dv(a) - dv(b) : dv(b) - dv(a)) || a.ticker.localeCompare(b.ticker));
  // Symbols with no daily data yet (still downloading) go last whatever the order.
  return [...out.filter((s) => s.market), ...out.filter((s) => !s.market)];
}

function openSymbolDialog() {
  const dlg = $('symbolDialog');
  $('symbolSearch').value = '';
  dlg.showModal();  // open first: the market map is laid out to the dialog's size
  renderSymbolList('');
  $('symbolSearch').focus();
  loadSymbols().then(() => { if (dlg.open) renderSymbolList($('symbolSearch').value); }).catch(() => {});
}

// Directory scopes: every Nasdaq/NYSE listing and futures product as placeholders (no market
// data unless added to the watchlist). Without a search the 300 biggest are shown.
state.switcherScope = prefs.get('switcherScope', 'watch');
let directory = { items: [], at: 0 };
async function loadDirectory() {
  if (Date.now() - directory.at < 600000) return;
  directory = { items: (await api('/api/directory')).items, at: Date.now() };
}

function renderDirectoryList(query) {
  const { symbolIcon, esc: e } = window.MassiveIcons;
  const list = $('symbolList');
  const kind = state.switcherScope === 'futures' ? 'future' : 'stock';
  const q = query.trim().toLowerCase();
  let rows = directory.items.filter((r) => r.kind === kind
    && (!q || [r.ticker, r.name, r.exchange, r.type].some((v) => v && String(v).toLowerCase().includes(q))));
  if (!directory.at) { list.innerHTML = '<div class="empty">Loading the directory…</div>'; return; }
  if (!rows.length) {
    list.innerHTML = `<div class="empty">${directory.items.length ? 'No listings match.' : 'The directory arrives with the fetcher\u2019s next sync (about 10 requests, weekly).'}</div>`;
    return;
  }
  const key = state.switcherSort;
  rows.sort((a, b) => key === 'name' ? a.ticker.localeCompare(b.ticker)
    : ((key === 'dv-asc' ? 1 : -1) * ((a.dollarVolume ?? -1) - (b.dollarVolume ?? -1))) || a.ticker.localeCompare(b.ticker));
  if (key === 'dv-asc') rows = [...rows.filter((r) => r.dollarVolume != null), ...rows.filter((r) => r.dollarVolume == null)];
  const total = rows.length;
  rows = rows.slice(0, q ? 600 : 300);
  list.innerHTML = `<h4>${kind === 'future' ? 'Futures products' : 'Nasdaq & NYSE'} <span class="p-muted">${total.toLocaleString()}${total > rows.length ? ` · showing ${rows.length}, search for more` : ''}</span></h4>
    <div class="tiles-grid">${rows.map((r) => `
      <div class="tile-btn dir${r.watched ? ' watched' : ''}" role="option" tabindex="0" data-ticker="${e(r.ticker)}" data-watched="${r.watched ? 1 : 0}"
           title="${e(r.name || r.ticker)}${r.exchange ? ` · ${e(r.exchange)}` : ''}${r.type ? ` · ${e(r.type)}` : ''}">
        <span class="tile-top">${symbolIcon({ ticker: r.ticker, icon: r.icon, asset: r.kind === 'future' ? 'future' : 'stock' }, 30)}
          ${r.watched ? '<span class="chip">watched</span>' : `<button type="button" class="watch-btn" data-watch="${e(r.ticker)}" title="Add to the watchlist: the fetcher will collect it">+ Watch</button>`}</span>
        <b class="tile-tk">${e(r.ticker)}</b>
        <span class="tile-nm">${e(r.name || '')}</span>
        <span class="tile-px">${r.close != null ? Number(r.close).toFixed(2) : '<span class="p-muted">–</span>'} <span class="p-muted">${e(r.exchange || '')}</span></span>
        <span class="tile-dv p-muted">${r.dollarVolume != null ? `${compactMoney(r.dollarVolume)}/day` : e(r.type || '')}</span>
      </div>`).join('')}</div>`;
}

// Market map: stocks by sector and industry, sized by market cap, coloured by the day's change.
state.switcherView = prefs.get('switcherView', 'map');
let mapPick = null;  // a directory company picked on the map (not watched yet)

function mapMatch(q) {
  return (s) => !q || [s.ticker, s.name, s.sector, s.industry].some((v) => v && String(v).toLowerCase().includes(q));
}

// What a search matches, best first: the ticker itself, a ticker starting with it, a name starting
// with it, a ticker holding it, a word of the name starting with it, then a name, sector or industry
// holding it; the biggest (market cap, else dollar volume) first within each.
function rankedMatches(items, q) {
  const Q = q.trim().toUpperCase();
  if (!Q) return [];
  const rank = (s) => {
    const t = s.ticker.toUpperCase(), n = String(s.name || '').toUpperCase();
    if (t === Q) return 0;
    if (t.startsWith(Q)) return 1;
    if (n.startsWith(Q)) return 2;
    if (t.includes(Q)) return 3;
    if (n.split(/[\s.,&/-]+/).some((w) => w.startsWith(Q))) return 4;
    return [n, s.sector, s.industry].some((v) => v && String(v).toUpperCase().includes(Q)) ? 5 : -1;
  };
  const size = (s) => s.marketCap || s.dollarVolume || 0;
  return items.map((s) => [rank(s), s]).filter(([r]) => r >= 0)
    .sort((a, b) => a[0] - b[0] || size(b[1]) - size(a[1]) || a[1].ticker.localeCompare(b[1].ticker)).map(([, s]) => s);
}

const MAP_HITS = 24;  // the matches shown above the map: a search found them, the map shows where they sit

function renderMapList(query) {
  const { esc: e, symbolIcon } = window.MassiveIcons;
  const list = $('symbolList');
  const q = query.trim().toLowerCase();
  const dir = state.switcherScope === 'stocks';
  const toItem = (s) => ({ ticker: s.ticker, name: s.name, sector: s.sector, industry: s.industry, marketCap: s.marketCap,
    changePct: s.changePct ?? s.market?.changePct ?? null, close: s.close ?? s.market?.close ?? null, icon: s.icon,
    asset: s.asset || (s.kind === 'future' ? 'future' : 'stock'), dollarVolume: s.dollarVolume ?? s.market?.dollarVolume ?? null,
    exchange: s.exchange, watched: dir ? s.watched : true });
  const source = dir ? directory.items.filter((r) => r.kind === 'stock') : state.symbols.filter((s) => s.asset === 'stock');
  const everyMapped = source.filter((s) => s.marketCap > 0 && s.sector).map(toItem);
  const everyOther = dir ? [] : state.symbols.filter((s) => s.asset !== 'stock').map(toItem);
  const unclassified = source.length - everyMapped.length;
  // A search: the matches as tiles of their own (those off the map too: no reference data yet), and
  // the map drawn again with only the companies that match, large enough to read (it dimmed the rest).
  const hits = q ? rankedMatches(source.map(toItem).concat(everyOther), q) : [];
  const match = mapMatch(q);
  const mapped = q ? everyMapped.filter(match) : everyMapped;
  const others = q ? everyOther.filter(match) : everyOther;
  const pct = (p) => (p == null ? '' : `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`);
  const hitHtml = (s) => `<button type="button" class="tile-btn map-hit${s.ticker === state.ticker ? ' current' : ''}${s.watched ? '' : ' unwatched'}"
      role="option" data-ticker="${e(s.ticker)}" style="--heat:${window.MarketMap.changeColor(s.changePct)}"
      title="${e(s.name || s.ticker)}${s.sector ? ` · ${e(s.sector)}` : ''}${s.watched ? '' : ' · not collected yet: pick it, then + Watch'}">
      <span class="hit-top">${symbolIcon({ ticker: s.ticker, icon: s.icon, asset: s.asset }, 20)}<b>${e(s.ticker)}</b>
        <span class="hit-pct">${pct(s.changePct)}</span></span>
      <span class="tile-nm">${e(s.name || s.exchange || s.asset || '')}</span>
      <span class="hit-px">${s.close != null ? Number(s.close).toFixed(2) : ''}${s.watched ? '' : ' <span class="chip">not collected</span>'}</span>
    </button>`;
  const hitsHtml = !q ? '' : hits.length
    ? `<h4>Matches <span class="p-muted">${hits.length.toLocaleString()}${hits.length > MAP_HITS ? ` · the first ${MAP_HITS}` : ''} · Enter opens the first</span></h4>
       <div class="tiles-grid map-hits">${hits.slice(0, MAP_HITS).map(hitHtml).join('')}</div>`
    : `<div class="empty">Nothing matches “${e(query.trim())}”${dir ? '' : ': try Nasdaq & NYSE for every listing'}.</div>`;
  list.innerHTML = `${hitsHtml}${q && !mapped.length ? '' : `${q ? '<h4>On the market map</h4>' : ''}<div id="marketMap" class="market-map${q ? ' searched' : ''}" role="listbox" aria-label="Market map"></div>`}
    ${others.length ? `<h4>Indices, futures, forex and crypto</h4><div id="marketStrip" class="market-map strip"></div>` : ''}
    <div class="map-foot">
      <span class="map-legend">${[-3, -2, -1, 0, 1, 2, 3].map((p) => `<span style="background:${window.MarketMap.changeColor(p)}">${p > 0 ? '+' : ''}${p}%</span>`).join('')}</span>
      <span class="p-muted small">${mapped.length.toLocaleString()} ${q ? `of ${everyMapped.length.toLocaleString()} ` : ''}companies by market cap · colour: last session's change${unclassified > 0
        ? ` · ${unclassified.toLocaleString()} ${dir ? 'listings' : 'symbols'} without reference data yet (${q ? 'among the matches, not on the map' : 'not shown'})` : ''}</span>
      <span id="mapPick" class="map-pick" ${mapPick ? '' : 'hidden'}></span>
    </div>`;
  if (!dir && !source.length && !everyOther.length) {
    list.innerHTML = '<div class="empty">No symbols yet. Add one with: python -m massive_collate watch add TICKER::EXCHANGE</div>';
    return;
  }
  const opts = { current: state.ticker, isWatched: dir ? (s) => s.watched : null };
  const map = $('marketMap');
  if (map && !map.clientWidth) {  // not laid out yet (a closed dialog): drawn once it has a size
    if ($('symbolDialog').open) requestAnimationFrame(() => renderMapList(query));
    return;
  }
  if (map) window.MarketMap.render(map, mapped, opts);
  if (others.length) window.MarketMap.renderStrip($('marketStrip'), others, opts);
  renderMapPick();
}

function renderMapPick() {
  const el = $('mapPick');
  if (!el) return;
  const { esc: e } = window.MassiveIcons;
  el.hidden = !mapPick;
  if (!mapPick) return;
  const p = mapPick;
  el.innerHTML = `<b>${e(p.ticker)}</b> <span class="p-muted">${e(p.name || '')}</span>
    <button type="button" class="primary" data-watch="${e(p.ticker)}" title="Add to the watchlist: the fetcher will collect it">+ Watch</button>`;
}

function renderSymbolList(query) {
  for (const b of $('scopeTabs').querySelectorAll('button')) b.classList.toggle('active', b.dataset.scope === state.switcherScope);
  const mapOk = state.switcherScope !== 'futures';
  const useMap = mapOk && state.switcherView === 'map';
  $('switcherView').hidden = !mapOk;
  $('switcherView').textContent = state.switcherView === 'map' ? 'Tiles' : 'Map';
  $('switcherView').title = state.switcherView === 'map' ? 'Show tiles' : 'Show the market map (sector, industry, market cap)';
  $('sortBtn').hidden = useMap;
  $('symbolDialog').classList.toggle('map-view', useMap);
  if (useMap) {
    if (state.switcherScope === 'stocks' && !directory.at) {
      $('symbolList').innerHTML = '<div class="empty">Loading the directory…</div>';
      loadDirectory().then(() => renderSymbolList($('symbolSearch').value)).catch(showError);
      return;
    }
    renderMapList(query);
    return;
  }
  if (state.switcherScope !== 'watch') {
    const sort = SORTS.find((x) => x.key === state.switcherSort) || SORTS[0];
    $('sortBtn').textContent = sort.label;
    renderDirectoryList(query);
    if (!directory.at) loadDirectory().then(() => renderDirectoryList($('symbolSearch').value)).catch(showError);
    return;
  }
  const { symbolIcon, dataIcon, esc: e } = window.MassiveIcons;
  const sort = SORTS.find((x) => x.key === state.switcherSort) || SORTS[0];
  $('sortBtn').textContent = sort.label;
  $('sortBtn').title = `${sort.title} (click to change)`;
  const q = query.trim().toLowerCase();
  const list = $('symbolList');
  const matches = state.symbols.filter((s) =>
    !q || [s.ticker, s.name, s.exchange, s.exchangeName].some((v) => v && v.toLowerCase().includes(q)));
  if (!matches.length) {
    list.innerHTML = '<div class="empty">No symbols match. Add one with: python -m massive_collate watch add TICKER::EXCHANGE</div>';
    return;
  }
  const groups = [
    ['Stocks', sortedSymbols(matches.filter((s) => s.asset === 'stock'))],
    ['Futures, indices, forex and crypto', matches.filter((s) => s.asset !== 'stock').sort((a, b) => a.ticker.localeCompare(b.ticker))],
  ];
  list.innerHTML = groups.filter(([, items]) => items.length).map(([title, items]) => `
    <h4>${e(title)} <span class="p-muted">${items.length}</span></h4>
    <div class="tiles-grid">${items.map((s) => {
      const m = s.market;
      const chg = m?.changePct;
      return `<button type="button" class="tile-btn${s.ticker === state.ticker ? ' current' : ''}" role="option" data-ticker="${e(s.ticker)}"
          title="${e(s.name || s.ticker)}${s.exchangeName ? ` · ${e(s.exchangeName)}` : ''}">
        <span class="tile-top">${symbolIcon(s, 30)}${dataIcon(s.data, 13)}</span>
        <b class="tile-tk">${e(s.ticker)}</b>
        <span class="tile-nm">${e(s.name || s.kind || '')}</span>
        <span class="tile-px">${m ? `${Number(m.close).toFixed(2)} <span class="${chg == null ? '' : chg >= 0 ? 'up' : 'down'}">${chg == null ? '' : `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`}</span>` : '<span class="p-muted">–</span>'}</span>
        <span class="tile-dv p-muted">${m ? `${compactMoney(m.dollarVolume)}/day` : e(s.asset)}</span>
      </button>`;
    }).join('')}</div>`).join('');
}

function moveTileFocus(dx, dy) {
  const tiles = [...$('symbolList').querySelectorAll('.tile-btn')];
  if (!tiles.length) return;
  const i = Math.max(0, tiles.indexOf(document.activeElement));
  const top = tiles[i].offsetTop;
  const cols = tiles.filter((t) => t.offsetTop === tiles[0].offsetTop).length || 1;
  let j = i + dx + dy * cols;
  if (document.activeElement === $('symbolSearch')) j = 0;
  j = Math.max(0, Math.min(tiles.length - 1, j));
  tiles[j].focus();
  tiles[j].scrollIntoView({ block: 'nearest' });
  void top;
}

$('symbolBtn').addEventListener('click', openSymbolDialog);
$('symbolSearch').addEventListener('input', (e) => renderSymbolList(e.target.value));
$('sortBtn').addEventListener('click', () => {
  const i = SORTS.findIndex((x) => x.key === state.switcherSort);
  state.switcherSort = SORTS[(i + 1) % SORTS.length].key;
  prefs.set('switcherSort', state.switcherSort);
  renderSymbolList($('symbolSearch').value);
});
$('switcherView').addEventListener('click', () => {
  state.switcherView = state.switcherView === 'map' ? 'tiles' : 'map';
  prefs.set('switcherView', state.switcherView);
  mapPick = null;
  renderSymbolList($('symbolSearch').value);
});
window.addEventListener('resize', () => { if ($('symbolDialog').open && state.switcherView === 'map') renderSymbolList($('symbolSearch').value); });
$('symbolList').addEventListener('click', async (e) => {
  const tile = e.target.closest('.map-tile, .map-hit');
  if (tile) {
    const t = tile.dataset.ticker;
    const item = state.switcherScope === 'stocks' ? directory.items.find((r) => r.ticker === t) : null;
    if (item && !item.watched) { mapPick = item; renderMapPick(); return; }  // pick, then + Watch
    $('symbolDialog').close();
    openSymbol(t);
    return;
  }
  const add = e.target.closest('[data-watch]');
  if (add) {  // directory: add to the watchlist; the fetcher collects it from its next run
    e.stopPropagation();
    add.disabled = true;
    try {
      const res = await fetch(`/api/watch?ticker=${encodeURIComponent(add.dataset.watch)}`, { method: 'POST', headers: { 'X-Massive-Charts': '1' } });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
      const item = directory.items.find((r) => r.ticker === add.dataset.watch);
      if (item) item.watched = true;
      if (mapPick && mapPick.ticker === add.dataset.watch) { mapPick = null; renderSymbolList($('symbolSearch').value); }
      add.replaceWith(Object.assign(document.createElement('span'), { className: 'chip', textContent: 'watched' }));
      loadSymbols().catch(() => {});
    } catch (err) { add.disabled = false; showError(err); }
    return;
  }
  const t = e.target.closest('.tile-btn');
  if (!t) return;
  if (t.classList.contains('dir') && t.dataset.watched !== '1') return;  // placeholder: nothing collected yet
  $('symbolDialog').close();
  openSymbol(t.dataset.ticker);
});
$('scopeTabs').addEventListener('click', (e) => {
  const b = e.target.closest('[data-scope]');
  if (!b) return;
  state.switcherScope = b.dataset.scope;
  prefs.set('switcherScope', state.switcherScope);
  renderSymbolList($('symbolSearch').value);
  $('symbolSearch').focus();
});
$('symbolDialog').addEventListener('keydown', (e) => {
  const moves = { ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowDown: [0, 1], ArrowUp: [0, -1] };
  if (moves[e.key] && !(e.target === $('symbolSearch') && ['ArrowLeft', 'ArrowRight'].includes(e.key))) {
    e.preventDefault();
    moveTileFocus(...moves[e.key]);
  } else if (e.key === 'Enter' && e.target === $('symbolSearch')) {
    e.preventDefault();
    // map: the best match (its matches' tiles come first); tiles: the first one
    const first = $('symbolList').querySelector('.map-hit') || $('symbolList').querySelector('.map-tile')
      || $('symbolList').querySelector('.tile-btn');
    if (first) first.click();
  }
});

// -- intervals and session -----------------------------------------------------
const PERIOD_LABEL = { second: 's', minute: 'm', hour: 'h', day: 'D', week: 'W', month: 'M', year: 'Y' };

// The interval menu: every interval is offered; one no source of this symbol can build is greyed out.
const periodLabel = (p) => (p.type === 'day' || p.type === 'week' || p.type === 'month' ? PERIOD_LABEL[p.type]
  : `${p.span}${PERIOD_LABEL[p.type]}`);
const PERIOD_NAMES = { second: 'second', minute: 'minute', hour: 'hour', day: 'day', week: 'week', month: 'month', year: 'year' };

// The Detail slider beside the level: how narrow candles may get before the level changes (autoMinPx).
function detailControl() {
  const label = document.createElement('label');
  label.className = 'auto-detail';
  const input = document.createElement('input');
  Object.assign(input, { id: 'autoDetail', type: 'range', min: '0', max: '100', step: '1', value: String(state.autoDetail) });
  input.addEventListener('input', () => {
    state.autoDetail = Number(input.value);
    prefs.set('autoDetail', state.autoDetail);
    renderPeriods();
    autoSoon();  // the level for the zoom on screen, once the slider settles
  });
  label.append('coarse', input, 'fine');
  return label;
}

function renderPeriods() {
  const box = $('periods');
  let tag = $('autoLevel');
  if (!tag) {
    tag = document.createElement('span');
    tag.id = 'autoLevel';
    tag.className = 'auto-level';
    box.replaceChildren(tag, detailControl());
  }
  const bricks = PRICE_BASED.has(state.chartType);
  $('autoDetail').disabled = bricks;
  $('autoDetail').parentElement.title = bricks ? 'Detail: bricks come from one level whatever the zoom'
    : `Detail: the narrowest candle before the chart moves to a coarser level, now ${+autoMinPx().toFixed(2)} px `
      + '(2 px in the middle). Right: finer levels sooner, more and thinner candles. Left: coarser levels, bigger candles.';
  const lv = currentLevel();
  const label = lv ? lv.label : periodLabel(state.period);
  tag.textContent = `auto: ${label}`;
  if (state.model.kind !== 'price') renderModelControls();  // the model window's span in time is the level's
  tag.title = PRICE_BASED.has(state.chartType)
    ? `Built from ${label} bars over the whole history: bricks follow price, not time, and each depends on every `
      + 'one before it, so they come from one level, the finest that builds in a moment; zooming does not change it.'
    : 'Auto-ranging: the candles follow the zoom. A symbol opens on its whole history; zooming in moves to '
      + 'finer levels (days, hours, minutes, seconds) while the data has them for the window on screen, and where it '
      + 'has none finer the candles grow. This is the level shown now.';
}

// The sessions shown, picked by name (a button cycling through them read "ETH" while it showed the
// regular hours only, and the overnight choice was two clicks away, unseen).
function renderSession() {
  $('sessionSelect').value = state.session;
  $('unfilteredBtn').classList.toggle('on', state.unfiltered);
}

// Filtered (the target data: bad data left out, docs/ARCHITECTURE.md "Bad data") or unfiltered (all
// of it, what each feed's pruned list holds included).
$('unfilteredBtn').addEventListener('click', () => {
  state.unfiltered = !state.unfiltered;
  prefs.set('unfiltered', state.unfiltered);
  renderSession();
  chart.resetData();
});

$('sessionSelect').addEventListener('change', (e) => {
  state.session = SESSIONS.includes(e.target.value) ? e.target.value : 'regular';
  prefs.set('session', state.session); markDirty();
  renderSession();
  chart.resetData();
});

// -- Fourier cycles: the strongest cycles of the bars in view, refitted as you zoom and scroll ----
// The closes on screen, their linear trend taken out, decomposed into whole cycles across the view
// (a discrete Fourier transform); the FOURIER_CYCLES strongest, with the trend back in, are drawn over
// the price, and the legend gives each one's length in bars and time. A fit to what is in view, not a
// forecast: zoom in or out and it is fitted again.
const FOURIER_CYCLES = 3;
const FOURIER_MAX_HARMONIC = 400;  // a cycle of at least view / 400 bars: plenty for any screen
let fourierFit = { key: '', from: 0, to: 0, values: [], periods: [] };

// The bars the Fourier fit is made from: a highlight's on the Analysis view, else those in view.
function fourierRange(c) {
  const data = c.getDataList();
  const span = highlightSpan();
  if (span) return [barIndexAt(data, span.from), barIndexAt(data, span.to + 1)];
  const { from, to } = c.getVisibleRange();
  return [Math.max(0, from), to];
}

function fitFourier(data, from, to, k = FOURIER_CYCLES) {
  const key = `${from}:${to}:${data[to - 1]?.timestamp}:${data[to - 1]?.close}:${k}`;
  if (fourierFit.key === key) return fourierFit;
  const y = [];
  for (let i = from; i < to; i++) y.push(data[i].close);
  const n = y.length;
  if (n < 8) return (fourierFit = { key, from, to, values: [], periods: [] });
  const mx = (n - 1) / 2;
  const my = y.reduce((a, v) => a + v, 0) / n;
  let sxy = 0, sxx = 0;
  y.forEach((v, t) => { sxy += (t - mx) * (v - my); sxx += (t - mx) ** 2; });
  const slope = sxx ? sxy / sxx : 0;
  const trend = (t) => my + slope * (t - mx);
  const rest = y.map((v, t) => v - trend(t));
  const cycles = [];
  for (let h = 1; h <= Math.min(Math.floor(n / 2), FOURIER_MAX_HARMONIC); h++) {
    let a = 0, b = 0;
    const w = 2 * Math.PI * h / n;
    for (let t = 0; t < n; t++) { a += rest[t] * Math.cos(w * t); b += rest[t] * Math.sin(w * t); }
    const scale = h === n / 2 ? 1 / n : 2 / n;
    cycles.push({ h, a: a * scale, b: b * scale, power: a * a + b * b });
  }
  const top = cycles.sort((p, q) => q.power - p.power).slice(0, k);
  const values = y.map((_, t) => trend(t) + top.reduce((s, c) => s + c.a * Math.cos(2 * Math.PI * c.h * t / n)
    + c.b * Math.sin(2 * Math.PI * c.h * t / n), 0));
  return (fourierFit = { key, from, to, values, periods: top.map((c) => n / c.h) });
}

kc.registerIndicator({
  name: 'FOURIER',
  shortName: 'Fourier',
  series: 'price',
  figures: [],
  calc: (list) => list.map(() => ({})),
  createTooltipDataSource: ({ chart: c }) => {
    const fit = fitFourier(c.getDataList(), ...fourierRange(c));
    const p = state.period || { type: 'minute', span: 1 };
    const barMs = ({ second: 1, minute: 60, hour: 3600, day: 86400, week: 604800, month: 2629800, year: 31557600 }[p.type] || 60)
      * p.span * 1000 * lodFor();
    const text = fit.periods.length
      ? `${fit.periods.length} strongest cycles ${highlightSpan() ? 'in the highlight' : 'in view'}: ${fit.periods.map((b) => `${b.toFixed(b < 10 ? 1 : 0)} bars (${fmtDuration(b * barMs)})`).join(' · ')}`
      : 'too few bars in view';
    return { name: 'Fourier', calcParamsText: '', features: [], legends: [{ title: '', value: { text, color: '#ab47bc' } }] };
  },
  draw: ({ ctx, chart: c, xAxis, yAxis }) => {
    const fit = fitFourier(c.getDataList(), ...fourierRange(c));
    if (!fit.values.length) return true;
    ctx.save();
    ctx.strokeStyle = '#ab47bc'; ctx.lineWidth = 2; ctx.lineJoin = 'round';
    ctx.beginPath();
    fit.values.forEach((v, t) => {
      const x = xAxis.convertToPixel(fit.from + t), y = yAxis.convertToPixel(v);
      if (t) ctx.lineTo(x, y); else ctx.moveTo(x, y);
    });
    ctx.stroke();
    ctx.restore();
    return true;
  },
});

// -- indicators ----------------------------------------------------------------
const MAIN_INDICATORS = [['MA', 'Moving average'], ['EMA', 'Exponential MA'], ['SMA', 'Smoothed MA'],
  ['BOLL', 'Bollinger bands'], ['SAR', 'Parabolic SAR'], ['AVP', 'Average price (VWAP-like)'], ['BBI', 'Bull and bear index'],
  ['FOURIER', 'Fourier cycles (fitted to the bars in view)']];
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
        if (cb.checked) { addIndicator(name); state.indicators.push(name); drawAside(name); }
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
    onRemoved: (e) => {
      state.selectedOverlay = null;
      if (e?.overlay?.name === 'measure') scheduleHighlightPanel();
      setTimeout(saveDrawings, 0);  // the library calls this before taking it off the chart: saved without it
    },
    onSelected: (e) => { state.selectedOverlay = e.overlay.id; },
    onDeselected: () => { state.selectedOverlay = null; },
    // a right-click opens the drawing's menu (copy, cut, paste, duplicate, delete: drawclip.js); the
    // library would otherwise delete it
    onRightClick: (e) => { e.preventDefault?.(); window.DrawingClipboard?.rightClick(e.overlay); },
  };
}

function startDrawing(name) {
  cancelDrawing();
  if (name === 'cursor') { setActiveTool('cursor'); return; }
  // One highlight at a time: a new one replaces the previous one.
  if (name === 'measure') chart.removeOverlay({ groupId: 'user', name: 'measure' });
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
    if (confirm(`Delete all drawings on ${state.ticker}?`)) {
      state.drawingsAside = [];  // those waiting for their indicator's pane too
      chart.removeOverlay({ groupId: 'user' });
      saveDrawings();
    }
  });
  setActiveTool('cursor');
}

// A drawing on an indicator's pane is kept with that indicator's name (`pane`; pane ids are made
// afresh each session) and goes back on its pane: every drawing came back on the price pane before.
// The chart keeps a drawing whose indicator was taken off (undrawn, on a pane that is gone): its
// pane's name is remembered.
const paneNames = new Map();  // pane id -> the name of the indicator drawn in it

function paneIndicator(paneId) {
  if (!paneId || paneId === 'candle_pane') return null;
  const name = chart.getIndicators({ paneId })[0]?.name;
  if (name) paneNames.set(paneId, name);
  return name ?? paneNames.get(paneId) ?? null;
}

// The pane an indicator is drawn in below the price, or null when it is not on the chart.
function indicatorPane(name) {
  return chart.getIndicators({ name }).find((i) => i.paneId !== 'candle_pane')?.paneId ?? null;
}

// Drawings belong to the current layout, per ticker, as (timestamp, price) points so they
// line up on every interval. One whose indicator is not on the chart now waits aside, kept as it was
// (state.drawingsAside), and comes back with the indicator.
function saveDrawings() {
  if (!state.ticker || state.restoredFor !== drawingsKey() || !layout.current) return;
  const user = chart.getOverlays({ groupId: 'user' }).filter((o) => o.id !== state.drawingId);
  const drawings = layout.current.data.drawings || (layout.current.data.drawings = {});
  drawings[drawingsKey()] = [...user.map((o) => ({
    name: o.name, pane: paneIndicator(o.paneId) ?? undefined, lock: o.lock, visible: o.visible,
    extendData: o.extendData ?? null, styles: o.styles ?? null,
    points: o.points.map((p) => ({ timestamp: p.timestamp, value: p.value })),
  })), ...(state.drawingsAside || [])];
  markDirty();
}

function restoreDrawings() {
  if (state.restoredFor === drawingsKey()) return;
  chart.removeOverlay({ groupId: 'user' });
  const saved = layout.current?.data.drawings?.[drawingsKey()] || [];
  const lastHighlight = saved.map((d) => d.name).lastIndexOf('measure');  // one highlight at a time
  state.drawingsAside = [];
  for (const [i, d] of saved.entries()) {
    if (d.name === 'measure' && i !== lastHighlight) continue;
    const paneId = d.pane ? indicatorPane(d.pane) : undefined;  // undefined: the price pane
    if (d.pane && !paneId) { state.drawingsAside.push(d); continue; }
    drawSaved(d, paneId);
  }
  state.restoredFor = drawingsKey();
  scheduleHighlightPanel();
}

function drawSaved(d, paneId) {
  if (paneId) paneNames.set(paneId, d.pane);
  chart.createOverlay({
    name: d.name, groupId: 'user', paneId, points: d.points, lock: d.lock, visible: d.visible,
    extendData: d.extendData ?? undefined, styles: d.styles ?? undefined, ...overlayEvents(),
  });
}

// An indicator turned on brings back the drawings waiting aside for its pane.
function drawAside(name) {
  const paneId = indicatorPane(name);
  const back = paneId ? (state.drawingsAside || []).filter((d) => d.pane === name) : [];
  if (!back.length) return;
  state.drawingsAside = state.drawingsAside.filter((d) => d.pane !== name);
  for (const d of back) drawSaved(d, paneId);
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
  generation: 0,  // changes counted by markDirty: a save leaves the layout clean only if none came since
  autosave: prefs.get('autosave', true),
  timer: null,
};

// `keepalive`: the request outlives the page (closing it); the browser takes at most 64 KiB so.
// Writes say which profile this page loaded as: signed in as another in another tab since (or
// signed out), the server refuses them (409, reload) rather than write over that profile's own.
const WRITE_HEADERS = { 'Content-Type': 'application/json', 'X-Massive-Charts': '1', 'X-Massive-Profile': String(PROFILE) };

async function apiWrite(method, path, body, { keepalive = false } = {}) {
  const res = await fetch(path, {
    method, keepalive, headers: WRITE_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json();
  if (res.status === 409 && data.reload) location.reload();  // this page's profile is not the one signed in now
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function snapshot() {
  return {
    version: 2,  // 2: each chart view's own content in `workspaces` (the top-level fields: the active one)
    symbol: state.ticker ? { ticker: state.ticker, source: state.source } : null,
    period: state.period,
    session: state.session,
    chartType: state.chartType,
    typeParams: state.typeParams,
    adjust: state.adjust,
    model: { ...state.model },
    indicators: [...state.indicators],
    drawings: layout.current?.data.drawings || {},
    workspace: state.workspace,
    workspaces: { ...state.workspaces, [state.workspace]: captureWorkspace() },
  };
}

// -- workspaces: the chart views' own content ---------------------------------------------
const samePeriod = (a, b) => !!a && !!b && a.type === b.type && a.span === b.span;

function captureWorkspace() {
  const data = chart.getDataList();
  const { to } = chart.getVisibleRange();
  const edge = data[Math.min(data.length - 1, Math.max(0, to - 1))];  // right-most visible bar
  const paneHeight = Object.fromEntries((chart.getPaneOptions() || []).map((p) => [p.id, p.height]));
  const heights = {};
  for (const ind of chart.getIndicators() || []) {
    if (state.indicators.includes(ind.name) && ind.paneId !== 'candle_pane' && paneHeight[ind.paneId]) {
      heights[ind.name] = paneHeight[ind.paneId];
    }
  }
  const x = edge ? chart.convertToPixel({ timestamp: edge.timestamp }, { paneId: 'candle_pane' }).x : null;
  return {
    period: state.period, chartType: state.chartType, typeParams: { ...state.typeParams }, model: { ...state.model },
    indicators: [...state.indicators], heights, lod: state.lod,
    view: data.length ? { barSpace: chart.getBarSpace().bar, anchor: edge ? edge.timestamp : null,
                          anchorX: Number.isFinite(x) ? x : null, atLatest: to >= data.length } : null,
  };
}

// A view shown for the first time: the same interval and chart type, plain prices, no indicators.
function defaultWorkspace() {
  return { period: state.period, chartType: state.chartType, typeParams: { ...state.typeParams },
           model: { ...MODEL_DEFAULTS }, indicators: [], heights: {}, lod: 1, view: null };
}

function applyWorkspace(ws) {
  const before = { period: state.period, model: JSON.stringify(state.model) };
  for (const name of state.indicators) chart.removeIndicator({ name });
  state.indicators = [...(ws.indicators || [])];
  state.indicators.forEach(addIndicator);
  for (const [name, height] of Object.entries(ws.heights || {})) {
    const ind = (chart.getIndicators() || []).find((i) => i.name === name && i.paneId !== 'candle_pane');
    if (ind) chart.setPaneOptions({ id: ind.paneId, height });
  }
  if (ws.period && (!state.meta || state.meta.periods.some((p) => samePeriod(p, ws.period)))) state.period = ws.period;
  if (ws.chartType) state.chartType = ws.chartType;
  state.typeParams = { ...(ws.typeParams || {}) };
  state.model = { ...MODEL_DEFAULTS, ...(ws.model || {}) };
  prefs.set('period', state.period); prefs.set('chartType', state.chartType); prefs.set('typeParams', state.typeParams);
  prefs.set('model', state.model); prefs.set('indicators', state.indicators);
  $('typeSelect').value = state.chartType;
  state.styleUsed = null;
  renderPeriods();
  renderModelControls();
  applyChartType();
  renderTypeParams();
  renderIndicatorMenu();
  state.restoredFor = null;
  chart.removeOverlay({ groupId: 'user' });  // the view's own drawings come back after the reload
  state.lod = ws.lod || 1;
  state.lodSwitch = ws.view ? { ...ws.view } : null;  // its zoom and position, restored once loaded
  if (!state.ticker) return;
  if (!samePeriod(before.period, state.period)) chart.setPeriod(state.period);
  else if (before.model !== JSON.stringify(state.model)) reloadWithModelPrecision();
  else chart.resetData();
}

function switchWorkspace(to) {
  if (!WORKSPACES.includes(to) || to === state.workspace) return;
  saveDrawings();  // under the outgoing view's key
  state.workspaces[state.workspace] = captureWorkspace();
  const ws = state.workspaces[to] || defaultWorkspace();
  state.workspace = to;
  prefs.set('workspace', to);
  applyWorkspace(ws);
  prefs.set('workspaces', state.workspaces);
  markDirty();
}
window.switchWorkspace = switchWorkspace;

function markDirty() {
  if (!layout.current || state.applyingLayout) return;
  layout.dirty = true;
  layout.generation += 1;
  renderLayoutButton();
  if (layout.autosave) {
    clearTimeout(layout.timer);
    layout.timer = setTimeout(() => saveLayout().catch(showError), 1500);
  }
}

// The answer is the server's copy of what was sent; the local layout stays as it is (drawings changed
// while the save was on its way went into it: taking the server's copy lost them), with only the
// server's name and time taken. It is clean only if nothing changed since the snapshot.
const KEEPALIVE_MAX = 60 * 1024;  // the browser's 64 KiB for a request that outlives the page, less the headers

async function saveLayout({ leaving = false } = {}) {
  const target = layout.current;
  if (!target) return;
  clearTimeout(layout.timer);
  const gen = layout.generation;
  target.data = snapshot();
  const body = { data: target.data };
  const keepalive = leaving && new TextEncoder().encode(JSON.stringify(body)).length <= KEEPALIVE_MAX;
  layout.sent = gen;  // on its way: leaving the page does not send it again
  let saved;
  try {
    saved = await apiWrite('PUT', `/api/layouts/${target.id}`, body, { keepalive });
  } catch (err) {
    if (layout.sent === gen) layout.sent = null;
    throw err;
  }
  Object.assign(target, { name: saved.name, updated_at: saved.updated_at });
  if (layout.current === target && layout.generation === gen) layout.dirty = false;
  renderLayoutButton();
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
    const saved = l.data || {};
    state.workspaces = { ...(saved.workspaces || {}) };  // every chart view's content, this one's applied below
    prefs.set('workspaces', state.workspaces);
    const d = { ...saved, ...(saved.workspaces?.[state.workspace] || {}) };
    layout.current = { ...l, data: { drawings: {}, ...saved } };
    layout.dirty = false;
    prefs.set('layoutId', l.id);
    if (d.period) state.period = d.period;
    if (d.session) state.session = d.session;
    if (d.chartType) state.chartType = d.chartType;
    if (d.typeParams) state.typeParams = d.typeParams;
    if (d.adjust) state.adjust = d.adjust;
    state.model = { ...MODEL_DEFAULTS, ...(d.model || {}) };
    renderModelControls();
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
    // the saved symbol, if the symbol list (still on its way at start: trusted till then) has it
    const known = (t) => !state.symbolsLoaded || state.symbols.some((s) => s.ticker === t);
    const sym = d.symbol && known(d.symbol.ticker) ? d.symbol
      : state.ticker ? { ticker: state.ticker, source: state.source }
      : firstStored() ? { ticker: firstStored().ticker, source: MERGED } : null;
    // A layout saved on a reference exchange only because that was once the default opens merged.
    if (sym && sym.source === symbolInfo(sym.ticker).models?.reference) sym.source = MERGED;
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
  const target = layout.current;
  const saved = await apiWrite('PUT', `/api/layouts/${target.id}`, { name });
  Object.assign(target, { name: saved.name, updated_at: saved.updated_at });  // its data stays the local one (saveLayout)
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
// Best effort: keep the last changes if the window closes before autosave fires. With autosave on,
// saved as soon as the page is hidden (another tab or window, or closing: hidden comes first); in
// any case when it goes; once per change. A save that outlives the page may carry at most 64 KiB: a
// bigger layout goes as an ordinary save, which completes when the page is only hidden and may be cut
// short when it closes. (The page-close save alone, always outliving the page, failed silently over 64 KiB.)
function saveOnLeave(closing) {
  if (!layout.dirty || !layout.current || layout.sent === layout.generation) return;
  if (!closing && !layout.autosave) return;  // only hidden, and autosave off: the user saves
  saveLayout({ leaving: true }).catch(() => {});
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') saveOnLeave(false); });
window.addEventListener('pagehide', () => saveOnLeave(true));

// First run: create "Default" from what this browser had before layouts existed (the first profile
// only: another starts with no markups).
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
    for (let i = 0; PROFILE === 1 && i < localStorage.length; i++) {
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
$('latestBtn').addEventListener('click', () => goLatest());
$('shotBtn').addEventListener('click', () => {
  const bg = getComputedStyle(document.body).getPropertyValue('--bg').trim();
  const a = document.createElement('a');
  a.href = chart.getConvertPictureUrl(true, 'png', bg);
  a.download = `${state.ticker || 'chart'}-${state.period.span}${state.period.type}.png`;
  a.click();
});
$('themeSelect').addEventListener('change', (e) => {
  setTheme(e.target.value);
  prefs.set('theme', state.theme);
  applyChartType();
  window.dispatchEvent(new CustomEvent('massive:theme', { detail: state.theme }));
});

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault();
    saveLayout().catch(showError);
    return;
  }
  if (e.target.matches('input, select, textarea') || document.querySelector('dialog[open]')) return;
  if (e.key === 'Escape') { cancelDrawing(); setActiveTool('cursor'); }
  else if (e.key === 'h' && e.altKey) { e.preventDefault(); startDrawing('measure'); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && state.selectedOverlay && document.body.dataset.view === 'chart') {
    chart.removeOverlay({ id: state.selectedOverlay });  // only the chart on screen: not a hidden one's drawing
  } else if (e.key === '/' || (e.key.length === 1 && /[a-z]/i.test(e.key) && !e.ctrlKey && !e.metaKey)) {
    e.preventDefault();
    openSymbolDialog();
    if (e.key !== '/') { $('symbolSearch').value = e.key; renderSymbolList(e.key); }
  } else if (e.key === '+' || e.key === '=') chart.zoomAtCoordinate(1.25, undefined, 150);
  else if (e.key === '-') chart.zoomAtCoordinate(0.8, undefined, 150);
  else if (e.key === 'End') goLatest();
});

// -- status bar ------------------------------------------------------------------
// Status bar: one short line (the full wording is in its tooltip).
function updateStatus(loaded) {
  const m = state.meta;
  if (!m) return;
  const info = symbolInfo(m.ticker);
  const mo = state.model;
  const k = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(n));
  const d = (iso) => iso.slice(2, 10);  // 26-06-12
  const base = { trades: 'ticks', second: '1s', minute: '1m', hour: '1h', day: '1D' }[m.base] || m.base;
  const lod = state.lod > 1 && LOD_TYPES.has(state.chartType);
  const short = [
    m.ticker,
    ...(mo.kind === 'price' ? [] : [`${mo.kind}${mo.kind === 'integral' ? '' : ` w${windowForDetail(mo.detail)}`}`]),
    `${m.feeds.join('>')} ${base}${m.hasDaily ? '+D' : ''}`,
    `${k(m.rows)} rows`,
    `${d(m.first)}→${d(m.last)}`,
    `${m.pricePrecision}dp`,
    SESSION_TEXT[state.session]?.[0] || 'RTH',
    'ET',
    ...(lod ? [`×${state.lod}`] : []),
    ...(loaded !== undefined ? [`${k(loaded)} bars`] : []),
  ];
  const full = [
    `${m.ticker}${info.name ? ' · ' + info.name : ''}`,
    ...(mo.kind === 'price' ? [] : [`model: ${$('modelSelect').selectedOptions[0].text} in ${state.modelUnit || '…'}`
      + (mo.kind === 'integral' ? '' : `, Savitzky-Golay window ${windowForDetail(mo.detail)} bars`)]),
    `feed: ${m.feeds.join(' > ')} (${m.base} data${m.hasDaily ? ' + official daily' : ''})`,
    `${m.rows.toLocaleString()} rows, ${m.first.slice(0, 10)} to ${m.last.slice(0, 10)}`,
    `price precision ${m.pricePrecision} decimals`,
    SESSION_TEXT[state.session]?.[1] || 'regular hours 09:30-16:00',
    'times US/Eastern',
    ...(lod ? [`zoomed out: each candle = ${state.lod} consecutive bars`] : []),
    ...(loaded !== undefined ? [`${loaded.toLocaleString()} bars loaded (scroll left for more)`] : []),
  ];
  $('statusText').textContent = short.join(' · ');
  $('statusText').title = full.join('\n');
}

// -- side panel (like TradingView's right-hand panel) ------------------------------------
// Symbol header, the highlight's full statistics, key stats from local daily bars,
// business details (saved by the weekly listing check) and data coverage. Resizable.
const PANEL_MIN = 220;
state.info = null;
state.hl = null;

function panelMax() { return Math.max(PANEL_MIN, Math.min(760, window.innerWidth * 0.6)); }

function setPanelWidth(w, save = true) {
  const width = Math.round(Math.min(panelMax(), Math.max(PANEL_MIN, w)));
  document.documentElement.style.setProperty('--panel-w', `${width}px`);
  if (save) prefs.set('panelWidth', width);
  requestAnimationFrame(() => chart.resize());
}

function setPanelHidden(hidden) {
  document.body.classList.toggle('panel-hidden', hidden);
  prefs.set('panelHidden', hidden);
  requestAnimationFrame(() => chart.resize());
}

(function initPanel() {
  setPanelWidth(prefs.get('panelWidth', 320), false);
  setPanelHidden(prefs.get('panelHidden', false));
  const grip = $('panelResizer');
  grip.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add('dragging');
    const right = $('sidePanel').getBoundingClientRect().right;
    const move = (ev) => setPanelWidth(right - ev.clientX);
    const up = () => { grip.classList.remove('dragging'); grip.removeEventListener('pointermove', move); };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up, { once: true });
    grip.addEventListener('pointercancel', up, { once: true });
  });
  grip.addEventListener('keydown', (e) => {
    const w = $('sidePanel').getBoundingClientRect().width;
    if (e.key === 'ArrowLeft') setPanelWidth(w + 16);
    else if (e.key === 'ArrowRight') setPanelWidth(w - 16);
  });
  window.addEventListener('resize', () => setPanelWidth($('sidePanel').getBoundingClientRect().width || 320, false));
})();

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
function prec() { return chart.getSymbol()?.pricePrecision ?? state.meta?.pricePrecision ?? 2; }
function priceText(v) { return v == null ? '–' : Number(v).toFixed(prec()); }
function signedPx(v) { return v == null ? '–' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(prec())}`; }
function pct(v) { return v == null ? '–' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`; }
function cls(v) { return v == null ? '' : v >= 0 ? 'up' : 'down'; }
function big(v) { return v == null ? '–' : compact(v); }
// A daily bar's date: crypto and forex day bars are UTC days (stamped 00:00 UTC, which is the
// previous evening in New York), every other market's are New York sessions.
function dateOnly(ms) {
  const zone = ['crypto', 'fx'].includes(state.meta?.asset) ? 'UTC' : TZ;
  return ms == null ? '–' : new Intl.DateTimeFormat('en-US', { timeZone: zone, year: 'numeric', month: 'short', day: '2-digit' }).format(new Date(ms));
}
function kv(rows) {
  return `<dl class="kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('')}</dl>`;
}

async function loadInfo() {
  if (!state.ticker) return;
  const ask = { source: state.source, ticker: state.ticker, adjust: state.adjust };
  const current = () => state.source === ask.source && state.ticker === ask.ticker && state.adjust === ask.adjust;
  try {
    const info = await api('/api/info', ask);
    if (!current()) return;  // the symbol changed meanwhile: its own load fills the panel
    state.info = info;
  } catch (err) {
    if (!current()) return;
    state.info = null;
    showError(err);
  }
  renderPanel();
}
setInterval(() => { if (!document.body.classList.contains('panel-hidden')) loadInfo(); }, POLL_MS);

// -- what the symbol holds, feed by feed and frame by frame (inventory.py) ------------------------
// ● sent by the feed (prime data); * built here from that feed's finer bars (derived); ○ a frame
// it should have built from them but has not yet. Columns in the order a merged chart takes the
// feeds. Read when a symbol opens and every minute while the panel shows.
const INVENTORY_MS = 60_000;
async function loadInventory() {
  if (!state.ticker || document.body.classList.contains('panel-hidden')) return;
  const ticker = state.ticker;
  let inv = null;
  try { inv = await api('/api/inventory', { ticker }); } catch { /* the panel shows nothing for it */ }
  if (ticker !== state.ticker) return;  // another symbol meanwhile: its own load fills the panel
  state.inventory = inv;
  renderInventory();
}
setInterval(loadInventory, INVENTORY_MS);
window.addEventListener('massive:symbol', () => { state.inventory = null; renderInventory(); loadInventory(); });

function renderInventory() {
  const box = $('pInventory');
  const inv = state.inventory;
  if (!inv?.feeds?.length) { box.innerHTML = ''; return; }
  const when = (iso) => iso.slice(0, 16).replace('T', ' ');
  const span = (p) => `${when(p.first)} → ${when(p.last)} UTC · ${p.rows != null ? `${p.rows.toLocaleString()} bars` : 'bars being counted'}`;
  const cols = inv.feeds.map((f) => ({ f, frames: Object.fromEntries(f.frames.map((x) => [x.frame, x])), missing: new Set(f.notBuilt) }));
  const rows = inv.frames.filter((label) => cols.some((c) => c.frames[label] || c.missing.has(label)));
  const cell = ({ f, frames, missing }, label) => {
    const x = frames[label];
    const sent = x?.sent ? `Sent by ${f.feed}: ${span(x.sent)}` : '';
    const built = x?.derived ? `*Derived here from ${f.feed}'s ${x.derived.how?.from || 'finer'} bars`
      + `${x.derived.how?.built ? ` (built ${when(x.derived.how.built)} UTC)` : ''}: ${span(x.derived)}` : '';
    if (x?.sent) return `<td class="inv-sent" title="${esc([sent, built && `${built}, where it sent none`].filter(Boolean).join('\n'))}">●${x.derived ? '<span class="inv-star">*</span>' : ''}</td>`;
    if (x?.derived) return `<td class="inv-derived" title="${esc(built)}">*</td>`;
    if (missing.has(label)) return `<td class="inv-missing" title="Not built yet: derived from the finer bars ${esc(f.feed)} holds">○</td>`;
    return '<td></td>';
  };
  const head = ({ f }) => {
    const w = f.watch || {};
    const about = [f.model ? `${f.feed}: a model built from several exchanges (derived)` : f.feed,
      w.resolution ? `collects ${w.resolution}${w.backfilled_to ? `, history from ${w.backfilled_to}` : ''}` : 'not collected here by this feed',
      f.ticks ? `trades ${when(f.ticks.first)} → ${when(f.ticks.last)} UTC` : '',
      f.contracts ? `contracts ${f.contracts[0]} … ${f.contracts.at(-1)} (${f.contracts.length}, its roll schedule)` : '']
      .filter(Boolean).join('\n');
    return `<th title="${esc(about)}">${esc(f.feed)}${f.model ? '*' : ''}</th>`;
  };
  const pending = inv.feeds.some((f) => f.notBuilt.length);
  box.innerHTML = `
    <h3>Data by feed${pending ? ` <button type="button" id="invBuild" title="Build ${esc(inv.ticker)}'s missing derived frames now, from what is stored (no requests to any feed)">Build missing</button>` : ''}</h3>
    <div class="table-wrap"><table class="inv-table">
      <thead><tr><th></th>${cols.map(head).join('')}</tr></thead>
      <tbody>${rows.map((label) => `<tr><th>${esc(label)}</th>${cols.map((c) => cell(c, label)).join('')}</tr>`).join('')}</tbody>
    </table></div>
    <div class="p-muted small">● sent by the feed · * derived here from finer data · ○ not built yet</div>`;
}

// -- bad data: each feed's pruned list (baddata.py; docs/ARCHITECTURE.md "Bad data") --------------
// What the rules took out of the filtered view, newest first; kept, never deleted ("All data" shows
// it on the chart). Read with the inventory.
const PRUNED_RULES = { malformed: 'malformed', spike: 'spike without volume',
                       inconsistent: 'outside its own finer bars', rebuilt: 'rebuilt (wrong rules)',
                       out_of_line: 'out of line with other feeds' };
async function loadPruned() {
  if (!state.ticker || document.body.classList.contains('panel-hidden')) return;
  const ticker = state.ticker;
  let pruned = null;
  try { pruned = await api('/api/pruned', { ticker }); } catch { /* nothing shown for it */ }
  if (ticker !== state.ticker) return;
  state.pruned = pruned;
  renderPruned();
}
setInterval(loadPruned, INVENTORY_MS);
window.addEventListener('massive:symbol', () => { state.pruned = null; renderPruned(); loadPruned(); });

function prunedWhy(e) {
  const ev = e.evidence || {};
  const n = (v) => (v == null ? '?' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 4 }));
  if (e.rule === 'spike') {
    return `range ${n(ev.range)} = ${ev.range_x ?? '∞'}× its ${ev.neighbours} neighbours' (${ev.part}), volume `
      + `${n(ev.volume)} = ${ev.volume_x ?? '∞'}× theirs` + (ev.trades?.length ? `; ${ev.trades.length} prints outside their range` : '');
  }
  if (e.rule === 'malformed') return (ev.failed || []).join(', ');
  if (e.rule === 'inconsistent') {
    return `high ${n(ev.high)}, low ${n(ev.low)}; its ${ev.finer_bars} ${ev.finer} bars ${n(ev.finer_low)} to ${n(ev.finer_high)}`;
  }
  if (e.rule === 'rebuilt') {
    const now = ev.now;
    return now ? `now high ${n(now.high)}, low ${n(now.low)} (was ${n(e.high)}, ${n(e.low)})` : 'no longer built';
  }
  return '';
}

function renderPruned() {
  const box = $('pPruned');
  const p = state.pruned;
  const feeds = Object.entries(p?.counts || {});
  if (!feeds.length) { box.innerHTML = ''; return; }
  const fmt = (ms) => new Date(ms).toLocaleString('en-US', { timeZone: TZ, month: 'short', day: '2-digit', year: 'numeric',
                                                              hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  const counts = feeds.map(([feed, c]) => `${esc(feed)}: ${Object.entries(c).map(([rule, k]) =>
    `${k.toLocaleString()} ${esc(PRUNED_RULES[rule] || rule)}`).join(', ')}`).join('<br>');
  const rows = (p.entries || []).slice(0, 50).map((e) => `
    <tr class="${e.releasedAt ? 'pr-released' : ''}" title="${esc(JSON.stringify(e.evidence, null, 1))}">
      <td>${esc(fmt(e.ts))}</td><td>${esc(e.source)}</td><td>${esc(`${e.multiplier} ${e.timespan}`)}</td>
      <td>${esc(PRUNED_RULES[e.rule] || e.rule)}${e.releasedAt ? ' (released)' : ''}</td>
      <td>${esc(prunedWhy(e))}</td></tr>`).join('');
  box.innerHTML = `
    <h3>Bad data (pruned)</h3>
    <div class="p-muted small">${counts}</div>
    <div class="table-wrap"><table class="pr-table"><tbody>${rows}</tbody></table></div>
    <div class="p-muted small">Left out of the filtered view, never deleted: "All data" shows them on the chart.</div>`;
}

$('pInventory').addEventListener('click', async (e) => {
  const btn = e.target.closest('#invBuild');
  if (!btn || !state.ticker) return;
  btn.disabled = true;
  btn.textContent = 'Building…';
  try {
    await apiWrite('POST', `/api/derive?ticker=${encodeURIComponent(state.ticker)}`);
    setTimeout(loadInventory, 3_000);  // a few seconds for most symbols; the minute's refresh catches the rest
  } catch (err) {
    btn.textContent = 'Build missing';
    btn.disabled = false;
    btn.title = String(err.message || err);
  }
});

function renderPanel() {
  const info = state.info;
  if (!info) { ['pSymbol', 'pStats', 'pBusiness', 'pData'].forEach((id) => { $(id).innerHTML = ''; }); return; }
  const { symbol: sy, stats: st = {}, details: dt = {}, data } = info;
  const chips = [sy.asset, sy.exchangeName || sy.exchange, sy.inverted ? `inverse of ${sy.sourceTicker}` : null]
    .filter(Boolean).map((c) => `<span class="p-chip">${esc(c)}</span>`).join('');
  $('pSymbol').innerHTML = `
    <div class="p-ticker">${esc(sy.ticker)}</div>
    <div class="p-name">${esc(sy.name || dt.name || '')}</div>
    <div>${chips}</div>
    <div class="p-price">${priceText(st.last)} <small class="${cls(st.change)}">${signedPx(st.change)} (${pct(st.changePct)})</small></div>
    <div class="p-muted">last session ${dateOnly(st.asOf)}${info.futures ? ` · contract ${esc(info.futures.contract)}` : ''}</div>`;

  const perf = st.performance || {};
  $('pStats').innerHTML = st.last == null ? '' : `
    <h3>Key statistics</h3>
    ${kv([
      ['Open', priceText(st.day?.open)], ['Day range', `${priceText(st.day?.low)} – ${priceText(st.day?.high)}`],
      ['Volume', big(st.day?.volume)], ['Avg volume (30)', big(st.avgVolume30)],
      ['Prev close', priceText(st.prevClose)],
      ['52-week range', `${priceText(st.range52w?.low)} – ${priceText(st.range52w?.high)}${st.range52w?.sessions < 252 ? ` <small>(${st.range52w.sessions} sessions)</small>` : ''}`],
      ['All-time high', `${priceText(st.rangeAll?.high)} <small>${dateOnly(st.rangeAll?.highTime)}</small>`],
      ['All-time low', `${priceText(st.rangeAll?.low)} <small>${dateOnly(st.rangeAll?.lowTime)}</small>`],
      ['Volatility (30d, ann.)', st.volatility30 == null ? '–' : `${st.volatility30.toFixed(1)}%`],
    ])}
    <div class="p-sub">Performance</div>
    <div class="perf">${['1W', '1M', '3M', '6M', 'YTD', '1Y', 'All'].map((k) =>
      `<div class="${cls(perf[k])}"><span>${k}</span>${pct(perf[k])}</div>`).join('')}</div>`;

  const addr = dt.address ? [dt.address.address1, dt.address.city, dt.address.state, dt.address.postal_code].filter(Boolean).join(', ') : null;
  const site = dt.homepage_url && /^https?:\/\//i.test(dt.homepage_url) ? `<a href="${esc(dt.homepage_url)}" target="_blank" rel="noopener noreferrer">${esc(dt.homepage_url.replace(/^https?:\/\//i, ''))}</a>` : null;
  const fut = info.futures;
  $('pBusiness').innerHTML = `
    <h3>${sy.asset === 'stock' ? 'Company' : sy.asset === 'future' ? 'Contract' : 'About'}</h3>
    ${dt.description ? `<p class="p-desc clamp" id="pDesc">${esc(dt.description)}</p>` : ''}
    ${kv([
      dt.sic_description && ['Industry', esc(dt.sic_description)],
      dt.market_cap && ['Market cap', `${big(dt.market_cap)} ${esc((dt.currency_name || '').toUpperCase())}`],
      dt.share_class_shares_outstanding && ['Shares outstanding', big(dt.share_class_shares_outstanding)],
      dt.total_employees && ['Employees', Number(dt.total_employees).toLocaleString()],
      (sy.listed || dt.list_date) && ['Listed', esc(sy.listed || dt.list_date)],
      site && ['Website', site],
      addr && ['Address', esc(addr)],
      dt.phone_number && ['Phone', esc(dt.phone_number)],
      (sy.figi || dt.composite_figi) && ['FIGI', esc(sy.figi || dt.composite_figi)],
      dt.cik && ['CIK', esc(dt.cik)],
      fut && ['Front contract', esc(fut.contract)],
      fut && ['Rolls', `${fut.rolls} · last ${esc(fut.lastRoll.session)} (${esc(fut.lastRoll.from)} → ${esc(fut.lastRoll.to)})`],
      fut && ['Prices', fut.adjust === 'none' ? 'as traded' : `back-adjusted (${esc(fut.adjust)})`],
      sy.sourceTicker && sy.sourceTicker !== sy.ticker && ['Source ticker', esc(sy.sourceTicker)],
      dt.unavailable && ['Reference data', `<span class="p-muted">${esc(dt.unavailable)}</span>`],
    ])}
    ${info.detailsUpdated ? `<div class="p-muted">reference data as of ${esc(info.detailsUpdated.slice(0, 10))}</div>` : ''}`;
  $('pDesc')?.addEventListener('click', (e) => e.currentTarget.classList.toggle('clamp'));

  $('pData').innerHTML = `
    <h3>Data</h3>
    ${kv([
      ['Feed', feedPicker(sy.ticker, data.feeds)], ['Resolution', esc(data.base) + (data.officialDaily ? ' + official daily' : '')],
      ['Rows', data.rows != null ? data.rows.toLocaleString() : 'counting…'], ['From', esc(data.first.slice(0, 16).replace('T', ' '))],
      ['To', esc(data.last.slice(0, 16).replace('T', ' '))], ['Price precision', `${data.pricePrecision} dp`],
      ['Times', 'US/Eastern'],
    ])}
    ${attributionHtml(info.attribution, data.base)}
    ${auditsHtml(info.audits, info.symbol.ticker)}`;
}

// The feed the chart shows, as a menu of every feed that holds this symbol's data (best first, as a
// merged chart takes them) and all of them merged: picking one opens the symbol on it.
function feedPicker(ticker, shown) {
  const held = (symbolInfo(ticker).feeds || []).map((f) => f.source);
  const feeds = [...new Set([...held, ...shown.filter((f) => f !== MERGED)])];
  if (feeds.length < 2 && state.source !== MERGED) return esc(shown.join(' > '));
  const opt = (value, label) => `<option value="${esc(value)}"${value === state.source ? ' selected' : ''}>${esc(label)}</option>`;
  return `<select id="pFeed" class="p-feed" aria-label="Feed" title="Feeds that hold ${esc(ticker)}'s data: pick one to chart it alone">
    ${opt(MERGED, feeds.length > 1 ? `All merged (${feeds.join(' > ')})` : feeds[0] || 'merged')}
    ${feeds.length > 1 ? feeds.map((f) => opt(f, f)).join('') : ''}
    ${state.source !== MERGED && !feeds.includes(state.source) ? opt(state.source, state.source) : ''}</select>`;
}

$('pData').addEventListener('change', (e) => {
  if (e.target.id === 'pFeed' && e.target.value !== state.source) selectSymbol(state.ticker, e.target.value).catch(showError);
});

// Where each span of the series came from (one feed per span), newest first.
function attributionHtml(att, base) {
  const spans = (att && att[base]) || [];
  if (!spans.length) return '';
  const d = (ms) => new Date(ms).toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
  const rows = [...spans].reverse().slice(0, 8).map(([a, b, src]) => [esc(src), `${d(a)} → ${b > Date.now() - 864e5 ? 'latest' : d(b)}`]);
  return `<div class="p-sub">Sources (${esc(base)} bars)</div>${kv(rows)}`;
}

function auditsHtml(au, ticker) {
  const rows = (au?.audits || []).filter((a) => a.ticker === ticker);
  if (!rows.length) return '';
  const n = (v) => rows.filter((a) => a.verdict === v).length;
  const last = rows[0], c = last.report?.box?.c;
  return `<div class="p-sub">Audits</div>${kv([
    ['Verdicts', `${n('pass')} pass · ${n('warn')} warn · ${n('fail')} fail`],
    ['Latest', `${esc(last.owner)} vs ${esc(last.auditor)}: ${esc(last.verdict)}${c ? ` · close ${c.median >= 0 ? '+' : ''}${c.median.toFixed(2)} bps` : ''}`],
  ])}`;
}

// Highlight section: built from the highlight itself (its points and the server's statistics),
// with the same summary the chart labels use, so it shows even when the highlight is off-screen.
let hlFrame = 0;
function scheduleHighlightPanel() {
  if (hlFrame) return;
  hlFrame = requestAnimationFrame(() => {
    hlFrame = 0;
    renderHighlightPanel();
    window.dispatchEvent(new Event('massive:highlight'));  // level 2 reads the highlight's prices (level2.js)
  });
}

function currentHighlight() {
  const o = chart.getOverlays({ groupId: 'user', name: 'measure' }).find((x) => x.points?.length >= 2);
  if (!o) return null;
  const [pa, pb] = o.points;
  if (pa.value == null || pb.value == null || pa.timestamp == null || pb.timestamp == null) return null;
  const key = statsKey(pa, pb);
  const ext = o.extendData || {};
  if (ext.key !== key) requestRangeStats(o, key, pa, pb);  // e.g. interval changed while off-screen
  return highlightSummary(pa, pb, ext.key === key ? ext.stats : null);
}

// -- Analysis: a highlight focuses the indicators --------------------------------------------
// On the Analysis view, with a highlight drawn, everything outside its time span is greyed out
// across the price and every indicator pane, so the indicators are read for the boxed bars only;
// the Fourier fit is made from those bars alone (fourierRange).
const hlMask = [document.createElement('div'), document.createElement('div')];
hlMask.forEach((m) => { m.className = 'hl-mask'; m.hidden = true; $('chart').append(m); });

function highlightSpan() {
  if (state.workspace !== 'analysis') return null;
  const o = chart.getOverlays({ groupId: 'user', name: 'measure' })
    .find((x) => x.points?.length >= 2 && x.points.every((p) => p.timestamp != null));
  if (!o) return null;
  const [a, b] = o.points.map((p) => p.timestamp);
  return { from: Math.min(a, b), to: Math.max(a, b) };
}

// The bar under the crosshair: {index, bar} in the loaded data, or null. This chart's crosshair
// event carries the pointer's x and y only (no bar of its own), so the bar comes from the x.
function crosshairBar(c) {
  if (!c || !Number.isFinite(c.x)) return null;
  const data = chart.getDataList();
  const [p] = chart.convertFromPixel([{ x: c.x, y: Number.isFinite(c.y) ? c.y : 0 }], { paneId: 'candle_pane' });
  const i = p?.dataIndex;
  return Number.isInteger(i) && i >= 0 && i < data.length ? { index: i, bar: data[i] } : null;
}

// The first bar at or after `ts` in the loaded data (its length when none is).
function barIndexAt(data, ts) {
  let lo = 0, hi = data.length;
  while (lo < hi) { const mid = (lo + hi) >> 1; if (data[mid].timestamp < ts) lo = mid + 1; else hi = mid; }
  return lo;
}

function placeHighlightMask() {
  const span = highlightSpan();
  const data = chart.getDataList();
  const w = plotWidth();
  if (!span || !w || !data.length) { hlMask.forEach((m) => { m.hidden = true; }); return; }
  const half = chart.getBarSpace().bar / 2;
  const x = (i) => chart.convertToPixel({ dataIndex: i }, { paneId: 'candle_pane' }).x;
  const i1 = barIndexAt(data, span.from), i2 = Math.min(data.length - 1, barIndexAt(data, span.to + 1) - 1);
  const x1 = Math.max(0, Math.min(w, x(i1) - half)), x2 = Math.max(0, Math.min(w, x(i2) + half));
  const h = `${chart.getSize('x_axis_pane')?.top ?? 0}px`;
  const [left, right] = hlMask;
  Object.assign(left.style, { left: '0px', width: `${x1}px`, height: h });
  Object.assign(right.style, { left: `${x2}px`, width: `${Math.max(0, w - x2)}px`, height: h });
  left.hidden = !(x1 > 0);
  right.hidden = !(x2 < w);
}

function renderHighlightPanel() {
  placeHighlightMask();
  const s = currentHighlight();
  const box = $('pHighlight');
  if (!s) {
    box.innerHTML = `<h3>Highlight</h3><div class="p-muted">Draw a highlight (Alt+H) to see its statistics here.</div>`;
    return;
  }
  // Percent change is only meaningful for prices (a slope or an area can cross zero).
  const isPrice = state.model.kind === 'price';
  const chgCell = (c) => `<span class="${cls(c.abs)}">${signedPx(c.abs)}${isPrice ? ` (${pct(c.pct)})` : ''}</span>`;
  const st = s.stats;
  box.innerHTML = `
    <h3>Highlight${state.model.kind !== 'price' && state.modelUnit ? ` <span class="p-muted">(${esc(state.modelUnit)})</span>` : ''}
      <button id="hlRemove" title="Remove the highlight">Remove</button></h3>
    ${kv([
      ['Start', `${priceText(s.start.price)} <small>${esc(fmtTime(s.start.time))}</small>`],
      ['Mid', `<span class="mid">${priceText(s.mid.price)}</span> <small>${esc(fmtTime(s.mid.time))}</small>`],
      ['Start → mid', chgCell(s.mid.fromStart)],
      ['Mid → end', chgCell(s.mid.toEnd)],
      ['End', `${priceText(s.end.price)} <small>${esc(fmtTime(s.end.time))}</small>`],
      ['Change', chgCell(s.end.change)],
      ['Bars', String(s.end.bars)],
      ['Duration', fmtDuration(s.end.duration)],
    ])}
    <div class="p-sub">Bars inside the highlight${st ? '' : ' <span class="p-muted">(calculating…)</span>'}</div>
    ${st ? kv([
      ['Open', priceText(st.open)],
      ['High', `${priceText(st.high)} <small>${esc(fmtTime(st.highTime))}</small>`],
      ['Low', `${priceText(st.low)} <small>${esc(fmtTime(st.lowTime))}</small>`],
      ['Close', priceText(st.close)],
      ['Open → close', `<span class="${cls(st.change)}">${signedPx(st.change)}${isPrice ? ` (${pct(st.changePct)})` : ''}</span>`],
      isPrice && ['High/low range', st.rangePct == null ? '–' : `${st.rangePct.toFixed(2)}%`],
      !isPrice && ['High − low', priceText(st.high - st.low)],
      ['Volume', big(st.volume)],
      isPrice && ['VWAP', priceText(st.vwap)],
      ['Trades', big(st.trades)],
    ]) : ''}`;
}
// one listener for the section, whatever it holds when clicked
$('pHighlight').addEventListener('click', (e) => {
  if (e.target.closest('#hlRemove')) chart.removeOverlay({ groupId: 'user', name: 'measure' });
});

// -- start -------------------------------------------------------------------------
(async function start() {
  renderHighlightPanel();
  renderTools();
  renderIndicatorMenu();
  renderSession();
  renderModelControls();
  applyChartType();
  renderTypeParams();
  state.indicators.forEach(addIndicator);
  try {
    // The chart opens the saved symbol at once; the symbol list (every symbol with its feeds and
    // state: a second or more) arrives alongside and fills in the symbol button and feed menu. The
    // chart waited for it first: three seconds and more on every page load.
    const symbols = loadSymbols().then(() => {
      if (!state.symbols.length) {
        $('statusText').textContent = 'No data stored yet. Add a ticker with: python -m massive_collate watch add TICKER::EXCHANGE, then sync.';
        return;
      }
      if (state.ticker) {
        renderSymbolButton();
        renderFeeds(symbolInfo(state.ticker));
      }
    });
    await Promise.all([ensureLayout(), symbols]);  // the layout opens its symbol; nothing saved: the first below
    if (!state.ticker && !state.selectToken && firstStored()) await selectSymbol(firstStored().ticker);
  } catch (err) {
    showError(err);
  }
})();
