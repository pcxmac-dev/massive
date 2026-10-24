// The Markets rail icon: the Markets view's wireframe globe in miniature, in 24 frames (one per hour
// of UTC; the current hour's is shown): the point where it is noon faces you, as in the view, with
// its sphere, graticule, coastlines and borders (Natural Earth 1:50m) and noon marker, in its
// colours. While any exchange trades (forex and crypto do not count for now) it shows that day
// side; when none does (weekends, holidays) the globe is turned 12 hours and the night side
// shows, shaded. Drawn at the screen's full pixel density.
(() => {
  'use strict';

  const SIZE = 35;           // css px (the rail's icons are 32)
  const MARKETS_MS = 300_000;
  const SKIP = new Set(['fx', 'crypto']);  // not markets for this icon (yet)
  const rad = Math.PI / 180;
  let outlines = null;       // every arc of the topology (coastlines and borders): [[lon, lat], ...]
  let failed = false;
  let declination = 0;
  let trading = true;
  let marketsAt = 0;
  const cache = new Map();   // `${mode}:${hour}:${decl}:${dpr}` -> canvas

  // TopoJSON (world-atlas) arcs in degrees, as the Markets view draws them.
  function arcs(topo) {
    const [kx, ky] = topo.transform.scale, [dx, dy] = topo.transform.translate;
    return topo.arcs.map((arc) => {
      let x = 0, y = 0;
      return arc.map(([ax, ay]) => { x += ax; y += ay; return [x * kx + dx, y * ky + dy]; });
    });
  }

  async function load() {
    if (outlines || failed) return;
    try {
      outlines = arcs(await (await fetch('vendor/world/countries-50m.json')).json());
      update();
    } catch {
      failed = true;  // no outlines: the rail keeps its drawn globe
    }
  }

  async function refreshMarkets() {
    if (Date.now() - marketsAt < MARKETS_MS) return;
    marketsAt = Date.now();
    try {
      const m = await (await fetch('/api/markets')).json();
      trading = (m.world || []).some((x) => !SKIP.has(x.kind) && x.state && x.state !== 'closed');
      if (m.sun?.lat != null) declination = Math.round(m.sun.lat);
      update();
    } catch { /* try again later */ }
  }

  // One frame: an orthographic view centred where it is noon at this hour (or the opposite point),
  // drawn like the Markets view's globe (globe.js: sphere, graticule, outlines, noon marker).
  function render(mode, hour) {
    const dpr = Math.min(4, Math.max(1, window.devicePixelRatio || 1));
    const key = `${mode}:${hour}:${declination}:${dpr}`;
    if (cache.has(key)) return cache.get(key);
    const n = Math.round(SIZE * dpr);
    const c = document.createElement('canvas');
    c.width = c.height = n;
    const g = c.getContext('2d');
    const day = mode === 'day';
    const lon0 = (day ? -15 * (hour + 0.5 - 12) : 180 - 15 * (hour + 0.5 - 12)) * rad;
    const lat0 = (day ? declination : -declination) * rad;
    const s0 = Math.sin(lat0), c0 = Math.cos(lat0);
    const cx = n / 2, cy = n / 2, r = n / 2 - dpr;
    const project = (lon, lat) => {  // [x, y] on the canvas, or null on the far side
      const la = lat * rad, dl = lon * rad - lon0;
      const cl = Math.cos(la), sl = Math.sin(la), cd = Math.cos(dl);
      if (s0 * sl + c0 * cl * cd < 0) return null;
      return [cx + r * cl * Math.sin(dl), cy - r * (c0 * sl - s0 * cl * cd)];
    };
    const polyline = (pts) => {
      let pen = false;
      for (const [lon, lat] of pts) {
        const p = project(lon, lat);
        if (!p) { pen = false; continue; }
        if (pen) g.lineTo(p[0], p[1]); else g.moveTo(p[0], p[1]);
        pen = true;
      }
    };
    const line = 'rgba(160,170,200,';  // the Markets view's line colour (dark)

    // sphere
    g.beginPath(); g.arc(cx, cy, r, 0, 2 * Math.PI);
    g.fillStyle = '#0b0f18'; g.fill();
    const grad = g.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.1, cx, cy, r);
    grad.addColorStop(0, 'rgba(41,98,255,0.18)');
    grad.addColorStop(1, 'rgba(41,98,255,0.03)');
    g.fillStyle = grad; g.fill();

    // graticule, every 15 degrees; the equator brighter
    g.beginPath();
    for (let lon = -180; lon < 180; lon += 15) polyline(Array.from({ length: 91 }, (_, i) => [lon, -90 + i * 2]));
    for (let lat = -75; lat <= 75; lat += 15) polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, lat]));
    g.strokeStyle = `${line}0.16)`; g.lineWidth = 0.5 * dpr; g.stroke();
    g.beginPath(); polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, 0]));
    g.strokeStyle = `${line}0.30)`; g.stroke();

    // coastlines and borders
    g.beginPath();
    for (const arc of outlines) polyline(arc);
    g.strokeStyle = `${line}0.75)`; g.lineWidth = 0.6 * dpr; g.lineJoin = 'round'; g.stroke();

    // the night side (all of it, turned away from the Sun), shaded as in the view
    if (!day) {
      g.beginPath(); g.arc(cx, cy, r, 0, 2 * Math.PI);
      g.fillStyle = 'rgba(2,4,10,0.5)'; g.fill();
    }

    // limb, and by day the noon point (the view's sun marker)
    g.beginPath(); g.arc(cx, cy, r, 0, 2 * Math.PI);
    g.strokeStyle = `${line}0.55)`; g.lineWidth = 0.9 * dpr; g.stroke();
    if (day) {
      g.beginPath(); g.arc(cx, cy, 2.2 * dpr, 0, 2 * Math.PI);
      g.strokeStyle = 'rgba(240,185,11,0.9)'; g.lineWidth = 1.1 * dpr; g.stroke();
    }
    cache.set(key, c);
    return c;
  }

  // Shown in the Markets rail button: true when the globe is drawn (the outlines are here).
  function update() {
    const button = document.getElementById('railMarkets');
    if (!button || !outlines) return false;
    const mode = trading ? 'day' : 'night';
    const hour = new Date().getUTCHours();
    const frame = render(mode, hour);
    let canvas = button.querySelector('canvas.globe-icon');
    if (!canvas) {
      button.innerHTML = '';
      canvas = document.createElement('canvas');
      canvas.className = 'globe-icon';
      canvas.setAttribute('aria-hidden', 'true');
      button.append(canvas);
    }
    const key = `${mode}:${hour}:${declination}:${frame.width}`;
    if (canvas.dataset.key !== key) {
      canvas.width = frame.width; canvas.height = frame.height;
      canvas.style.width = canvas.style.height = `${SIZE}px`;
      canvas.getContext('2d').drawImage(frame, 0, 0);
      canvas.dataset.key = key;
    }
    button.title = trading ? 'Markets: trading now (the day side, where it is noon)'
      : 'Markets: no exchange trading (forex and crypto aside): the night side';
    return true;
  }

  function tick() {
    refreshMarkets();
    return update();
  }

  load();
  setInterval(tick, 60_000);
  // frame(mode, hour): one frame as a canvas (for a look at the day and night sides), null before the outlines load
  window.GlobeIcon = { update: tick, frame: (mode, hour) => (outlines ? render(mode, hour) : null) };
})();
