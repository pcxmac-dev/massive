'use strict';
/* Markets view: a wireframe globe turned so the place where it is noon right now (the subsolar
   point) faces you, with the world's stock exchanges on it: open ones glow green, lunch breaks
   amber, closed ones grey. Night is shaded behind the terminator. Coastlines and country borders
   are drawn when vendor/world/countries-110m.json (Natural Earth, public domain) is present.
   The globe turns in real time (the sun's position is computed every frame, so the Earth
   rotates under it at 15 degrees an hour), wherever it has been dragged to. Drag to turn it;
   click the sun (or double-click anywhere) to glide back to noon. Beside it: every exchange by region
   with its local time, state, countdown, today's hours (lunch breaks included) and holidays. */
(() => {
  const canvas = document.getElementById('globe');
  const ctx = canvas.getContext('2d');
  const list = document.getElementById('marketsList');
  const info = document.getElementById('globeInfo');
  const esc = window.MassiveIcons.esc;
  const rad = Math.PI / 180;

  let markets = null, fetchedAt = 0;
  let view = { lon: 0, lat: 0 };   // centre of the globe, recomputed every frame from:
  let offset = { lon: 0, lat: null };  // where the view sits relative to the sun (lat null = the sun's)
  let glide = null;                // animation back to the sun: { from, start }
  let topo = null;                 // decoded boundary lines: [[ [lon,lat], ... ], ...]
  let hover = null;
  let nightCache = null;

  // -- data --------------------------------------------------------------------------------
  async function load() {
    try {
      markets = await api('/api/markets');
      fetchedAt = Date.now();
      nightCache = null;
      renderList();
      draw();
    } catch (err) { showError(err); }
  }

  async function loadTopo() {
    try {
      const res = await fetch('vendor/world/countries-110m.json');
      if (!res.ok) return;
      topo = decodeTopo(await res.json());
      draw();
    } catch { /* no boundary data: graticule only */ }
  }

  // TopoJSON arcs -> polylines in degrees (each shared border drawn once).
  function decodeTopo(t) {
    const [sx, sy] = t.transform ? t.transform.scale : [1, 1];
    const [tx, ty] = t.transform ? t.transform.translate : [0, 0];
    return t.arcs.map((arc) => {
      let x = 0, y = 0;
      return arc.map(([dx, dy]) => { x += dx; y += dy; return [x * sx + tx, y * sy + ty]; });
    });
  }

  // -- the sun ----------------------------------------------------------------------------
  // Where it is noon now (subsolar point), within about half a degree. Same formulas as the
  // server's world.subsolar.
  function subsolar(t = new Date()) {
    const start = Date.UTC(t.getUTCFullYear(), 0, 1);
    const day = Math.floor((t.getTime() - start) / 864e5) + 1;
    const hours = t.getUTCHours() + t.getUTCMinutes() / 60 + t.getUTCSeconds() / 3600 + t.getUTCMilliseconds() / 3.6e6;
    const g = (2 * Math.PI / 365) * (day - 1 + (hours - 12) / 24);
    const eqtime = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
      - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
    const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g) - 0.006758 * Math.cos(2 * g)
      + 0.000907 * Math.sin(2 * g) - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
    const lon = ((-15 * (hours - 12 + eqtime / 60)) + 540) % 360 - 180;
    return { lon, lat: decl / rad };
  }

  const following = () => !glide && offset.lon === 0 && offset.lat === null;
  const wrap = (d) => ((d + 540) % 360) - 180;

  // The view follows the sun as the Earth turns, keeping whatever offset the user dragged to.
  function updateView() {
    const sun = subsolar();
    if (markets) markets.sun = sun;
    if (glide) {
      const k = Math.min(1, (performance.now() - glide.start) / 700);
      const ease = 1 - Math.pow(1 - k, 3);
      view = { lon: sun.lon + wrap(glide.from.lon - sun.lon) * (1 - ease), lat: glide.from.lat + (sun.lat - glide.from.lat) * ease };
      if (k >= 1) { glide = null; offset = { lon: 0, lat: null }; }
      return;
    }
    view = { lon: sun.lon + offset.lon, lat: offset.lat ?? sun.lat };
  }

  function recentre() {
    glide = { from: { ...view }, start: performance.now() };
  }

  // -- projection -------------------------------------------------------------------------
  function geometry() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    return { w, h, cx: w / 2, cy: h / 2, r: Math.min(w, h) * 0.44 };
  }

  function project(lon, lat, g) {
    const l = (lon - view.lon) * rad, p = lat * rad, p0 = view.lat * rad;
    const cosc = Math.sin(p0) * Math.sin(p) + Math.cos(p0) * Math.cos(p) * Math.cos(l);
    const x = Math.cos(p) * Math.sin(l);
    const y = Math.cos(p0) * Math.sin(p) - Math.sin(p0) * Math.cos(p) * Math.cos(l);
    return { x: g.cx + g.r * x, y: g.cy - g.r * y, visible: cosc >= 0, depth: cosc };
  }

  function unproject(px, py, g) {
    const x = (px - g.cx) / g.r, y = (g.cy - py) / g.r;
    const rho = Math.hypot(x, y);
    if (rho > 1) return null;
    const c = Math.asin(rho), p0 = view.lat * rad;
    const lat = rho ? Math.asin(Math.cos(c) * Math.sin(p0) + (y * Math.sin(c) * Math.cos(p0)) / rho) : p0;
    const lon = view.lon * rad + Math.atan2(x * Math.sin(c), rho * Math.cos(c) * Math.cos(p0) - y * Math.sin(c) * Math.sin(p0));
    return { lon: lon / rad, lat: lat / rad };
  }

  function polyline(points, g) {
    let drawing = false;
    for (const [lon, lat] of points) {
      const p = project(lon, lat, g);
      if (!p.visible) { drawing = false; continue; }
      if (!drawing) { ctx.moveTo(p.x, p.y); drawing = true; } else ctx.lineTo(p.x, p.y);
    }
  }

  // Night shading at low resolution: each pixel's point on the globe vs the sun's direction.
  function nightLayer(g) {
    // The night side moves with the sun: at most a few redraws a minute at this precision.
    const key = `${g.w}x${g.h}|${view.lon.toFixed(1)}|${view.lat.toFixed(1)}|${markets?.sun.lon.toFixed(1)}`;
    if (nightCache?.key === key) return nightCache.canvas;
    const n = 180, off = document.createElement('canvas');
    off.width = off.height = n;
    const o = off.getContext('2d'), img = o.createImageData(n, n);
    const sun = markets?.sun || { lon: view.lon, lat: view.lat };
    const sx = Math.cos(sun.lat * rad) * Math.cos(sun.lon * rad), sy = Math.cos(sun.lat * rad) * Math.sin(sun.lon * rad), sz = Math.sin(sun.lat * rad);
    const mini = { cx: n / 2, cy: n / 2, r: n / 2 };
    for (let j = 0; j < n; j++) for (let i = 0; i < n; i++) {
      const q = unproject(i + 0.5, j + 0.5, mini);
      if (!q) continue;
      const d = Math.cos(q.lat * rad) * Math.cos(q.lon * rad) * sx + Math.cos(q.lat * rad) * Math.sin(q.lon * rad) * sy + Math.sin(q.lat * rad) * sz;
      const a = d < 0 ? Math.min(1, -d * 4) * 0.55 : 0;  // soft twilight edge
      const k = (j * n + i) * 4;
      img.data[k] = 6; img.data[k + 1] = 10; img.data[k + 2] = 24; img.data[k + 3] = Math.round(a * 255);
    }
    o.putImageData(img, 0, 0);
    nightCache = { key, canvas: off };
    return off;
  }

  const STATE_COLOR = { open: '#26a69a', break: '#f0a020', closed: '#787b86', unknown: '#555' };

  function draw() {
    if (document.body.dataset.view !== 'markets') return;
    const dpr = window.devicePixelRatio || 1;
    const g = geometry();
    if (canvas.width !== Math.round(g.w * dpr)) { canvas.width = Math.round(g.w * dpr); canvas.height = Math.round(g.h * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, g.w, g.h);
    const dark = !document.body.classList.contains('light');
    const line = dark ? 'rgba(160,170,200,' : 'rgba(40,50,80,';

    // sphere
    ctx.beginPath(); ctx.arc(g.cx, g.cy, g.r, 0, 2 * Math.PI);
    const grad = ctx.createRadialGradient(g.cx - g.r * 0.3, g.cy - g.r * 0.35, g.r * 0.1, g.cx, g.cy, g.r);
    grad.addColorStop(0, dark ? 'rgba(41,98,255,0.18)' : 'rgba(41,98,255,0.10)');
    grad.addColorStop(1, dark ? 'rgba(41,98,255,0.03)' : 'rgba(41,98,255,0.02)');
    ctx.fillStyle = grad; ctx.fill();
    ctx.strokeStyle = `${line}0.55)`; ctx.lineWidth = 1.2; ctx.stroke();

    // night side
    ctx.save(); ctx.beginPath(); ctx.arc(g.cx, g.cy, g.r, 0, 2 * Math.PI); ctx.clip();
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(nightLayer(g), g.cx - g.r, g.cy - g.r, g.r * 2, g.r * 2);
    ctx.restore();

    // graticule
    ctx.beginPath();
    for (let lon = -180; lon < 180; lon += 15) polyline(Array.from({ length: 91 }, (_, i) => [lon, -90 + i * 2]), g);
    for (let lat = -75; lat <= 75; lat += 15) polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, lat]), g);
    ctx.strokeStyle = `${line}0.16)`; ctx.lineWidth = 0.7; ctx.stroke();
    ctx.beginPath(); polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, 0]), g);
    ctx.strokeStyle = `${line}0.30)`; ctx.stroke();

    // coastlines and borders
    if (topo) {
      ctx.beginPath();
      for (const arc of topo) polyline(arc, g);
      ctx.strokeStyle = `${line}0.62)`; ctx.lineWidth = 0.8; ctx.stroke();
    }

    if (!markets) return;
    // noon marker (subsolar point)
    const sun = project(markets.sun.lon, markets.sun.lat, g);
    if (sun.visible) {
      ctx.beginPath(); ctx.arc(sun.x, sun.y, 7, 0, 2 * Math.PI);
      ctx.strokeStyle = 'rgba(240,185,11,0.9)'; ctx.lineWidth = 1.5; ctx.stroke();
      for (let k = 0; k < 8; k++) {
        const a = (k * Math.PI) / 4;
        ctx.beginPath(); ctx.moveTo(sun.x + Math.cos(a) * 10, sun.y + Math.sin(a) * 10); ctx.lineTo(sun.x + Math.cos(a) * 14, sun.y + Math.sin(a) * 14); ctx.stroke();
      }
    }

    // exchanges: back to front, labels for open ones (and the hovered one)
    const pulse = 0.5 + 0.5 * Math.sin(Date.now() / 400);
    const placed = [];
    const items = markets.world.map((m) => ({ m, p: project(m.lon, m.lat, g) })).filter((x) => x.p.visible)
      .sort((a, b) => a.p.depth - b.p.depth);
    for (const { m, p } of items) {
      const color = STATE_COLOR[m.state] || STATE_COLOR.unknown;
      if (m.state === 'open') {
        ctx.beginPath(); ctx.arc(p.x, p.y, 6 + 5 * pulse, 0, 2 * Math.PI);
        ctx.fillStyle = `rgba(38,166,154,${0.12 + 0.12 * (1 - pulse)})`; ctx.fill();
      }
      ctx.beginPath(); ctx.arc(p.x, p.y, m === hover ? 5.5 : 4, 0, 2 * Math.PI);
      ctx.fillStyle = color; ctx.fill();
      ctx.strokeStyle = dark ? '#0b0e15' : '#fff'; ctx.lineWidth = 1; ctx.stroke();
      if (m.state === 'open' || m.state === 'break' || m === hover) {
        const label = `${m.city} · ${m.index.split(' · ')[0]}`;
        ctx.font = '11px system-ui, sans-serif';
        const w = ctx.measureText(label).width;
        let lx = p.x + 8, ly = p.y - 6;
        if (placed.some((b) => Math.abs(b.x - lx) < w && Math.abs(b.y - ly) < 13)) ly += 14;  // simple de-overlap
        placed.push({ x: lx, y: ly });
        ctx.fillStyle = dark ? 'rgba(19,23,34,0.75)' : 'rgba(255,255,255,0.8)';
        ctx.fillRect(lx - 3, ly - 10, w + 6, 14);
        ctx.fillStyle = m.state === 'open' ? (dark ? '#9ee7dc' : '#0b6b60') : m.state === 'break' ? '#f0a020' : (dark ? '#d1d4dc' : '#131722');
        ctx.fillText(label, lx, ly);
      }
    }
    const open = markets.world.filter((m) => m.state === 'open').length;
    const brk = markets.world.filter((m) => m.state === 'break').length;
    const utc = new Date().toISOString().slice(11, 16);
    info.innerHTML = `<b>${open}</b> exchanges open${brk ? `, <b>${brk}</b> at lunch` : ''} · ${utc} UTC
      <div class="p-muted small">Noon is at ${Math.abs(markets.sun.lon).toFixed(1)}°${markets.sun.lon >= 0 ? 'E' : 'W'}
      · sun overhead at ${Math.abs(markets.sun.lat).toFixed(1)}°${markets.sun.lat >= 0 ? 'N' : 'S'}
      · turning in real time${following() ? '' : ' · <a href="#" id="globeNoon">back to noon</a>'}</div>`;
  }

  // -- list ----------------------------------------------------------------------------
  function untilText(iso) {
    if (!iso) return '';
    let s = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const m = Math.floor(s / 60); s %= 60;
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m ${String(s).padStart(2, '0')}s`;
  }
  const EVENT = { closes: 'closes in', 'lunch break': 'lunch in', reopens: 'reopens in', opens: 'opens in' };

  function localTime(tz) {
    try { return new Date().toLocaleTimeString('en-GB', { timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit' }); } catch { return ''; }
  }

  function renderList() {
    if (!markets || document.body.dataset.view !== 'markets') return;
    const regions = ['Asia-Pacific', 'Europe', 'Middle East & Africa', 'Americas'];
    const mine = markets.markets.map((m) => `
      <div class="mk-row${m.symbols.includes(state.ticker) ? ' mine' : ''}">
        <span class="mk-dot" style="background:${m.state === 'open' ? STATE_COLOR.open : ['pre-market', 'after-hours'].includes(m.state) ? STATE_COLOR.break : STATE_COLOR.closed}"></span>
        <div class="mk-main"><b>${esc(m.name)}</b> <span class="p-muted small">${esc(m.state)}</span>
          <div class="p-muted small">${m.until ? `until ${new Date(m.until).toLocaleString('en-US', { timeZone: 'America/New_York', weekday: 'short', hour: '2-digit', minute: '2-digit', hour12: false })} ET · in ${untilText(m.until)}`
            : m.next ? `${esc(m.next.state)} in ${untilText(m.next.at)}` : ''}${m.nextHoliday ? ` · next: ${esc(m.nextHoliday.name)} ${esc(m.nextHoliday.date)}` : ''}</div></div>
      </div>`).join('');
    list.innerHTML = `
      <h3>Your instruments</h3>${mine}
      ${regions.map((r) => {
        const rows = markets.world.filter((m) => m.region === r);
        return `<h3>${esc(r)} <span class="p-muted">${rows.filter((m) => m.state === 'open').length}/${rows.length} open</span></h3>
        ${rows.map((m) => `
          <div class="mk-row" data-code="${esc(m.code)}">
            <span class="mk-dot" style="background:${STATE_COLOR[m.state] || STATE_COLOR.unknown}"></span>
            <div class="mk-main"><b>${esc(m.city)}</b> <span class="p-muted small">${esc(m.index)}</span>
              <div class="p-muted small">${esc(m.name)} · ${esc(localTime(m.timezone))}
                ${m.today ? ` · ${esc(m.today.open)}–${esc(m.today.close)}${m.today.break ? ` (lunch ${esc(m.today.break)})` : ''}` : m.state !== 'unknown' ? ' · no session today' : ''}</div>
              ${m.holidays?.length ? `<div class="small mk-hol">Holidays: ${m.holidays.map(esc).join(', ')}</div>` : ''}
              ${m.calendarEnds && m.calendarEnds < new Date(Date.now() + 60 * 864e5).toISOString().slice(0, 10)
                ? `<div class="small p-muted">holidays published to ${esc(m.calendarEnds)}</div>` : ''}
              ${m.error ? `<div class="small down">${esc(m.error)}</div>` : ''}
            </div>
            <div class="mk-when small">${m.until ? `${esc(EVENT[m.event] || m.event)}<br><b>${untilText(m.until)}</b>` : ''}</div>
          </div>`).join('')}`;
      }).join('')}`;
  }

  // -- interaction -----------------------------------------------------------------------
  let dragging = null;
  function sunHit(e) {
    if (!markets) return false;
    const g = geometry(), r = canvas.getBoundingClientRect();
    const p = project(markets.sun.lon, markets.sun.lat, g);
    return p.visible && Math.hypot(p.x - (e.clientX - r.left), p.y - (e.clientY - r.top)) < 16;
  }
  canvas.addEventListener('pointerdown', (e) => {
    glide = null;
    dragging = { x: e.clientX, y: e.clientY, lon: view.lon, lat: view.lat };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', (e) => {
    const g = geometry();
    if (dragging && (dragging.moved || Math.hypot(e.clientX - dragging.x, e.clientY - dragging.y) > 3)) {
      dragging.moved = true;
      const k = 90 / g.r;
      const lon = dragging.lon - (e.clientX - dragging.x) * k;
      const lat = Math.max(-85, Math.min(85, dragging.lat + (e.clientY - dragging.y) * k));
      offset = { lon: wrap(lon - subsolar().lon), lat };
      updateView();
      draw();
      return;
    }
    if (sunHit(e)) { if (hover) { hover = null; draw(); } canvas.style.cursor = 'pointer'; canvas.title = 'Back to noon'; return; }
    canvas.title = '';
    const r = canvas.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    let best = null, bestD = 12;
    for (const m of markets?.world || []) {
      const p = project(m.lon, m.lat, g);
      const d = Math.hypot(p.x - x, p.y - y);
      if (p.visible && d < bestD) { best = m; bestD = d; }
    }
    if (best !== hover) { hover = best; canvas.style.cursor = best ? 'pointer' : 'grab'; draw(); }
  });
  let justDragged = false;  // the click that ends a drag is not a click on the sun or a market
  canvas.addEventListener('pointerup', () => { justDragged = !!dragging?.moved; dragging = null; });
  canvas.addEventListener('pointercancel', () => { dragging = null; });
  canvas.addEventListener('dblclick', recentre);
  canvas.addEventListener('click', (e) => {
    if (justDragged) { justDragged = false; return; }
    if (sunHit(e)) { recentre(); return; }
    if (!hover) return;
    const row = list.querySelector(`[data-code="${hover.code}"]`);
    if (row) { row.scrollIntoView({ block: 'center', behavior: 'smooth' }); row.classList.add('flash'); setTimeout(() => row.classList.remove('flash'), 1200); }
  });
  info.addEventListener('click', (e) => {
    if (e.target.id === 'globeNoon') { e.preventDefault(); recentre(); }
  });

  // Animate while visible: the Earth turning, pulses, countdowns every second; market states
  // are re-fetched every 30 s.
  let lastList = 0;
  function frame() {
    if (document.body.dataset.view === 'markets') {
      if (Date.now() - fetchedAt > 30000) load();
      updateView();
      draw();
      if (Date.now() - lastList > 1000) { renderList(); lastList = Date.now(); }
    }
    requestAnimationFrame(frame);
  }
  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'markets') { load(); if (!topo) loadTopo(); } });
  window.addEventListener('resize', () => { nightCache = null; draw(); });
  window.Globe = { state: () => ({ view: { ...view }, offset: { ...offset }, sun: subsolar(), following: following() }), recentre };
  requestAnimationFrame(frame);
  if (document.body.dataset.view === 'markets') { load(); loadTopo(); }
})();
