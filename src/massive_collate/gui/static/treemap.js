/* Market map for the symbol switcher: stocks grouped by sector, then industry, each tile sized by
   market cap and coloured by the day's change (−3 % red, 0 grey, +3 % green, as market maps do).
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

  /* items: [{ticker, name, sector, industry, marketCap, changePct, close, icon}]
     opts: {current, isMatch(s), isWatched(s)} */
  function render(el, items, opts = {}) {
    const W = el.clientWidth, H = el.clientHeight;
    const SECTOR_HEAD = 16, INDUSTRY_HEAD = 13;
    const bySector = new Map();
    for (const s of items) {
      if (!(s.marketCap > 0)) continue;
      const sec = bySector.get(s.sector) || bySector.set(s.sector, new Map()).get(s.sector);
      (sec.get(s.industry) || sec.set(s.industry, []).get(s.industry)).push(s);
    }
    const sectors = [...bySector].map(([name, inds]) => ({
      name, inds, value: [...inds.values()].flat().reduce((a, s) => a + s.marketCap, 0),
    }));
    let html = '';
    for (const sr of squarify(sectors, 0, 0, W, H)) {
      const sec = sr.item;
      const head = sr.h > 40 && sr.w > 50 ? SECTOR_HEAD : 0;
      html += `<div class="map-sector" style="left:${sr.x}px;top:${sr.y}px;width:${sr.w - 2}px;height:${sr.h - 2}px">
        ${head ? `<span class="map-sector-name">${esc(sec.name)}</span>` : ''}</div>`;
      const inds = [...sec.inds].map(([name, list]) => ({ name, list, value: list.reduce((a, s) => a + s.marketCap, 0) }));
      for (const ir of squarify(inds, sr.x + 1, sr.y + head, sr.w - 3, sr.h - head - 3)) {
        const ind = ir.item;
        const ih = ir.h > 46 && ir.w > 70 && sec.inds.size > 1 ? INDUSTRY_HEAD : 0;
        if (ih) html += `<div class="map-industry" style="left:${ir.x}px;top:${ir.y}px;width:${ir.w - 1}px">${esc(ind.name)}</div>`;
        const stocks = ind.list.map((s) => ({ ...s, value: s.marketCap }));
        for (const r of squarify(stocks, ir.x, ir.y + ih, ir.w, ir.h - ih)) html += tileHtml(r.item, r, opts);
      }
    }
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

  window.MarketMap = { render, renderStrip, squarify, changeColor };
})();
