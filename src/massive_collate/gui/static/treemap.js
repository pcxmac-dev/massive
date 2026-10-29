/* Market maps for the symbol switcher and the watchlist: stocks grouped by exchange, sector and
   industry (futures by exchange, family and type), each tile sized by market cap (or dollar volume)
   and coloured by the day's change (−3 % red, 0 grey, +3 % green, as market maps do).
   Squarified layout (Bruls, Huizing & van Wijk): rows of tiles kept as close to square as
   possible. Tiles large enough show the company's logo. */
(function () {
  'use strict';

  const SCALE = [[-3, [246, 53, 56]], [-2, [191, 64, 69]], [-1, [139, 68, 78]], [0, [65, 69, 84]],
    [1, [53, 118, 78]], [2, [47, 158, 79]], [3, [48, 204, 90]]];
  function changeColor(pct) {
    if (pct == null || !Number.isFinite(pct)) return 'rgb(58,62,74)';
    const p = Math.max(-3, Math.min(3, pct));
    for (let i = 1; i < SCALE.length; i++) {
      const [x1, c1] = SCALE[i];
      if (p <= x1) {
        const [x0, c0] = SCALE[i - 1];
        const t = (p - x0) / (x1 - x0);
        return `rgb(${c0.map((v, k) => Math.round(v + (c1[k] - v) * t)).join(',')})`;
      }
    }
    return 'rgb(48,204,90)';
  }

  // Squarify `items` (with .value, any order) into the rectangle; returns [{item, x, y, w, h}].
  function squarify(items, x, y, w, h) {
    const list = items.filter((d) => d.value > 0).sort((a, b) => b.value - a.value);
    const total = list.reduce((s, d) => s + d.value, 0);
    const out = [];
    if (!list.length || w <= 0 || h <= 0) return out;
    const scale = (w * h) / total;
    let rect = { x, y, w, h };
    let row = [];
    const worst = (r, side) => {
      const s = r.reduce((a, d) => a + d.value * scale, 0);
      const mx = Math.max(...r.map((d) => d.value * scale)), mn = Math.min(...r.map((d) => d.value * scale));
      return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
    };
    const layRow = (r) => {
      const s = r.reduce((a, d) => a + d.value * scale, 0);
      if (rect.w >= rect.h) {  // a column on the left
        const cw = s / rect.h;
        let yy = rect.y;
        for (const d of r) { const hh = (d.value * scale) / cw; out.push({ item: d, x: rect.x, y: yy, w: cw, h: hh }); yy += hh; }
        rect = { x: rect.x + cw, y: rect.y, w: rect.w - cw, h: rect.h };
      } else {  // a row on top
        const rh = s / rect.w;
        let xx = rect.x;
        for (const d of r) { const ww = (d.value * scale) / rh; out.push({ item: d, x: xx, y: rect.y, w: ww, h: rh }); xx += ww; }
        rect = { x: rect.x, y: rect.y + rh, w: rect.w, h: rect.h - rh };
      }
    };
    for (const d of list) {
      const side = Math.min(rect.w, rect.h);
      if (!row.length || worst([...row, d], side) <= worst(row, side)) row.push(d);
      else { layRow(row); row = [d]; }
    }
    if (row.length) layRow(row);
    return out;
  }

  const esc = (s) => window.MassiveIcons.esc(String(s ?? ''));
  const money = (v) => (v >= 1e12 ? `$${(v / 1e12).toFixed(2)}T` : v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : `$${(v / 1e6).toFixed(0)}M`);
  const pctText = (p) => (p == null ? '–' : `${p >= 0 ? '+' : ''}${p.toFixed(2)}%`);

  function tileHtml(s, r, opts) {
    const pct = s.changePct;
    const fs = Math.max(8, Math.min(34, Math.min(r.w / (Math.max(3, s.ticker.length) * 0.66), r.h / 2.6)));
    const showPct = r.h > fs * 2.2 && r.w > 34;
    const logo = r.w >= 74 && r.h >= 66 && s.icon;
    const logoSize = Math.min(44, Math.round(Math.min(r.w, r.h) * 0.3));
    const cls = ['map-tile', opts.isMatch && !opts.isMatch(s) ? 'dim' : '', s.ticker === opts.current ? 'current' : '',
      opts.isWatched && !opts.isWatched(s) ? 'unwatched' : ''].filter(Boolean).join(' ');
    const title = `${s.ticker} · ${s.name || ''}\n${s.sector || ''}${s.industry ? ` / ${s.industry}` : ''}\n`
      + `${s.close != null ? `${Number(s.close).toFixed(2)} ` : ''}${pctText(pct)}${s.marketCap ? ` · ${money(s.marketCap)}` : ''}`;
    return `<div class="${cls}" role="option" tabindex="-1" data-ticker="${esc(s.ticker)}" title="${esc(title)}"
        style="left:${r.x.toFixed(1)}px;top:${r.y.toFixed(1)}px;width:${Math.max(0, r.w - 1).toFixed(1)}px;height:${Math.max(0, r.h - 1).toFixed(1)}px;background:${changeColor(pct)}">
      ${logo ? `<img class="map-logo" src="/api/icon?ticker=${encodeURIComponent(s.ticker)}" alt="" width="${logoSize}" height="${logoSize}" loading="lazy">` : ''}
      ${r.w > 16 && r.h > 10 ? `<b style="font-size:${fs.toFixed(1)}px">${esc(s.ticker)}</b>` : ''}
      ${showPct ? `<span style="font-size:${Math.max(8, fs * 0.55).toFixed(1)}px">${pctText(pct)}</span>` : ''}
    </div>`;
  }

  // The exchanges' names on the maps (MICs as the directory and the watchlist give them).
  const EXCHANGES = { XNAS: 'Nasdaq', XNYS: 'NYSE', XASE: 'NYSE American', ARCX: 'NYSE Arca', BATS: 'Cboe BZX',
    IEXG: 'IEX', XCME: 'CME', XCBT: 'CBOT', XNYM: 'NYMEX', XCEC: 'COMEX' };
  const exchangeName = (mic) => (mic ? EXCHANGES[mic] || mic : 'Other');

  // Futures: each product's type (Massive's: crude_oil, us_index ...) in its family, as futures maps
  // divide them (the user, 2026-10-28: "heat maps for futures all the different exchanges").
  const FAMILIES = {
    Equity: ['us_index', 'sel_sector_index', 'intl_index', 'ssf'],
    Energy: ['crude_oil', 'refined_products', 'nat_gas', 'nat_gas_liq_petro', 'electricity', 'biofuels', 'coal', 'emissions'],
    Metals: ['precious', 'base'],
    Rates: ['long_term_gov', 'short_term_gov', 'short_term_non_gov', 'long_term_non_gov'],
    Currencies: ['majors', 'minors', 'cross_rates'],
    Agriculture: ['grains_and_oilseeds', 'livestock', 'dairy', 'softs', 'fertilizer', 'forestry'],
    Freight: ['wet_bulk', 'dry_bulk'],
    'Commodity indices': ['bbg_ci', 'dj_ubs_ci', 'sandp_gsci', 'custom_index'],
    Weather: ['us', 'europe', 'asia', 'wind'],
    Events: ['sports_index'],
  };
  const FAMILY = Object.fromEntries(Object.entries(FAMILIES).flatMap(([f, types]) => types.map((t) => [t, f])));
  const TYPE_NAMES = { us_index: 'US indices', sel_sector_index: 'Sector indices', intl_index: 'International indices',
    ssf: 'Single stocks', nat_gas_liq_petro: 'NGLs and petrochemicals', long_term_gov: 'Government bonds',
    short_term_gov: 'Government bills', short_term_non_gov: 'Short-term rates', long_term_non_gov: 'Swaps and credit',
    bbg_ci: 'Bloomberg', dj_ubs_ci: 'DJ-UBS', sandp_gsci: 'S&P GSCI', custom_index: 'Custom', us: 'US weather',
    europe: 'European weather', asia: 'Asian weather', sports_index: 'Sports' };
  const futuresFamily = (type) => FAMILY[type] || 'Other';
  const futuresTypeName = (type) => TYPE_NAMES[type] || (type ? type[0].toUpperCase() + type.slice(1).replace(/_/g, ' ') : 'Other');

  /* A map divided into groups, each into the groups of the next level, the last into tiles: the
     user, 2026-10-28: "heat maps for all the exchanges (and relevent sectors divided in) as well as
     watchlist(s)". items: [{ticker, name, sector, industry, exchange, marketCap, changePct, close, icon}]
     opts: {current, isMatch(s), isWatched(s), levels: [s => group name, ...] (default sector, then
     industry), size: s => tile area (default market cap; 0 leaves it out), label: (depth, name) =>
     the group's heading}. Each group's heading carries its move, its tiles' size-weighted mean. */
  function render(el, items, opts = {}) {
    const W = el.clientWidth, H = el.clientHeight;
    const TOP_HEAD = 18, GROUP_HEAD = 16, INDUSTRY_HEAD = 13;
    const levels = opts.levels || [(s) => s.sector, (s) => s.industry];
    const size = opts.size || ((s) => s.marketCap);
    const label = opts.label || ((depth, name) => name);
    const root = new Map();
    for (const s of items) {
      const value = size(s);
      if (!(value > 0)) continue;
      let node = root;
      levels.forEach((key, i) => {
        const name = key(s) ?? '–';
        if (i < levels.length - 1) node = node.get(name) || node.set(name, new Map()).get(name);
        else (node.get(name) || node.set(name, []).get(name)).push({ ...s, value });
      });
    }
    const sum = (n) => (Array.isArray(n) ? n : [...n.values()].flatMap(flat)).reduce((a, s) => a + s.value, 0);
    const flat = (n) => (Array.isArray(n) ? n : [...n.values()].flatMap(flat));
    const move = (n) => {  // the group's move: its tiles' changes, weighted by their size
      let w = 0, m = 0;
      for (const s of flat(n)) if (s.changePct != null && Number.isFinite(s.changePct)) { w += s.value; m += s.value * s.changePct; }
      return w ? m / w : null;
    };
    let html = '';
    const draw = (node, depth, x, y, w, h) => {
      const groups = [...node].map(([name, child]) => ({ name, child, value: sum(child) }));
      const tiles = depth === levels.length - 1;  // the last level's groups hold tiles
      for (const r of squarify(groups, x, y, w, h)) {
        const g = r.item, chg = move(g.child), name = label(depth, g.name);
        const heading = `${esc(name)}${chg == null ? '' : ` <span class="map-move">${pctText(chg)}</span>`}`;
        const tip = `${name}: ${flat(g.child).length.toLocaleString()} · ${pctText(chg)}`;
        if (tiles) {
          const ih = r.h > 46 && r.w > 70 && node.size > 1 ? INDUSTRY_HEAD : 0;
          if (ih) html += `<div class="map-industry" title="${esc(tip)}" style="left:${r.x}px;top:${r.y}px;width:${r.w - 1}px">${heading}</div>`;
          for (const t of squarify(g.child, r.x, r.y + ih, r.w, r.h - ih)) html += tileHtml(t.item, t, opts);
        } else {
          const top = depth === 0 && levels.length > 2;  // the exchanges round their sectors
          const head = r.h > 40 && r.w > 50 ? (top ? TOP_HEAD : GROUP_HEAD) : 0;
          html += `<div class="map-sector${top ? ' map-top' : ''}" title="${esc(tip)}" style="left:${r.x}px;top:${r.y}px;width:${r.w - 2}px;height:${r.h - 2}px">
            ${head ? `<span class="map-sector-name">${heading}</span>` : ''}</div>`;
          draw(g.child, depth + 1, r.x + 1, r.y + head, r.w - 3, r.h - head - 3);
        }
      }
    };
    draw(root, 0, 0, 0, W, H);
    el.innerHTML = html;
  }

  // A strip of equal tiles (instruments without a market cap: indices, futures, FX, crypto).
  function renderStrip(el, items, opts = {}) {
    const W = el.clientWidth, h = 46;
    const w = Math.max(70, Math.min(140, W / Math.max(1, items.length)));
    const perRow = Math.max(1, Math.floor(W / w));
    el.style.height = `${Math.ceil(items.length / perRow) * h}px`;
    el.innerHTML = items.map((s, i) => tileHtml(s, { x: (i % perRow) * w, y: Math.floor(i / perRow) * h, w, h }, opts)).join('');
  }

  window.MarketMap = { render, renderStrip, squarify, changeColor, exchangeName, futuresFamily, futuresTypeName };
})();
