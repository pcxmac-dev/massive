// The Markets rail icon: the Markets view's wireframe globe in miniature, turning once a minute
// (eastward, as the Earth turns): its sphere, graticule, coastlines and borders (Natural Earth
// 1:50m) and limb, in its colours, at the screen's full pixel density. A frame per degree of
// turn, each drawn once and kept. With reduced motion asked for it stands still, turned so the
// point where it is noon faces you.
(() => {
  'use strict';

  const SIZE = 35;              // css px (the rail's icons are 32)
  const TURN_MS = 60_000;       // one turn a minute
  const TICK_MS = 1000 / 6;     // six frames a second: a degree each
  const rad = Math.PI / 180;
  const still = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  let outlines = null;  // every arc of the topology (coastlines and borders): [[lon, lat], ...]
  let failed = false;
  const cache = new Map();  // `${lon}:${declination}:${dpr}` -> canvas

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

  // The Sun's latitude today, to the nearest degree (the orbit's eccentricity included: within
  // about 0.2 degrees).
  function declination(t = new Date()) {
    const day = (t - Date.UTC(t.getUTCFullYear(), 0, 0)) / 86_400_000;
    const w = 2 * Math.PI / 365.24;
    return Math.round(Math.asin(Math.sin(-23.44 * rad) * Math.cos(w * (day + 10) + 2 * 0.0167 * Math.sin(w * (day - 2)))) / rad);
  }

  // Where it is noon now (longitude, degrees).
  const noon = (t = new Date()) => -15 * (t.getUTCHours() + t.getUTCMinutes() / 60 - 12);

  // The longitude facing you now: turning eastward from the noon point once a minute (the meridian
  // facing you moves west as the Earth turns east), or the noon point itself when still.
  function facing(t = new Date()) {
    const turned = still?.matches ? 0 : ((t.getTime() % TURN_MS) / TURN_MS) * 360;
    const lon = Math.round(noon(t) - turned);
    return ((lon % 360) + 540) % 360 - 180;  // -180 .. 179
  }

  // One frame: an orthographic view centred on (decl, lon), drawn like the Markets view's globe
  // (globe.js: sphere, graticule, outlines, limb).
  // Grey unless the Markets view is selected, then blue, like every rail icon.
  const PALETTES = {
    grey: { line: 'rgba(150,155,165,', glow: ['rgba(150,155,165,0.10)', 'rgba(150,155,165,0.02)'] },
    blue: { line: 'rgba(130,165,255,', glow: ['rgba(41,98,255,0.22)', 'rgba(41,98,255,0.04)'] },
  };

  function render(lon, decl, selected = false) {
    const dpr = Math.min(4, Math.max(1, window.devicePixelRatio || 1));
    const key = `${lon}:${decl}:${dpr}:${selected}`;
    if (cache.has(key)) return cache.get(key);
    const colours = selected ? PALETTES.blue : PALETTES.grey;
    const n = Math.round(SIZE * dpr);
    const c = document.createElement('canvas');
    c.width = c.height = n;
    const g = c.getContext('2d');
    const lon0 = lon * rad, lat0 = decl * rad;
    const s0 = Math.sin(lat0), c0 = Math.cos(lat0);
    const cx = n / 2, cy = n / 2, r = n / 2 - dpr;
    const project = (lo, la) => {  // [x, y] on the canvas, or null on the far side
      const p = la * rad, dl = lo * rad - lon0;
      const cl = Math.cos(p), sl = Math.sin(p), cd = Math.cos(dl);
      if (s0 * sl + c0 * cl * cd < 0) return null;
      return [cx + r * cl * Math.sin(dl), cy - r * (c0 * sl - s0 * cl * cd)];
    };
    const polyline = (pts) => {
      let pen = false;
      for (const [lo, la] of pts) {
        const p = project(lo, la);
        if (!p) { pen = false; continue; }
        if (pen) g.lineTo(p[0], p[1]); else g.moveTo(p[0], p[1]);
        pen = true;
      }
    };
    const line = colours.line;

    // sphere
    g.beginPath(); g.arc(cx, cy, r, 0, 2 * Math.PI);
    g.fillStyle = '#0b0f18'; g.fill();
    const grad = g.createRadialGradient(cx - r * 0.3, cy - r * 0.35, r * 0.1, cx, cy, r);
    grad.addColorStop(0, colours.glow[0]);
    grad.addColorStop(1, colours.glow[1]);
    g.fillStyle = grad; g.fill();

    // graticule, every 15 degrees; the equator brighter
    g.beginPath();
    for (let lo = -180; lo < 180; lo += 15) polyline(Array.from({ length: 91 }, (_, i) => [lo, -90 + i * 2]));
    for (let la = -75; la <= 75; la += 15) polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, la]));
    g.strokeStyle = `${line}0.16)`; g.lineWidth = 0.5 * dpr; g.stroke();
    g.beginPath(); polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, 0]));
    g.strokeStyle = `${line}0.30)`; g.stroke();

    // coastlines and borders
    g.beginPath();
    for (const arc of outlines) polyline(arc);
    g.strokeStyle = `${line}0.75)`; g.lineWidth = 0.6 * dpr; g.lineJoin = 'round'; g.stroke();

    // limb
    g.beginPath(); g.arc(cx, cy, r, 0, 2 * Math.PI);
    g.strokeStyle = `${line}0.55)`; g.lineWidth = 0.9 * dpr; g.stroke();
    cache.set(key, c);
    return c;
  }

  // Shown in the Markets rail button: true when the globe is drawn (the outlines are here).
  function update() {
    const button = document.getElementById('railMarkets');
    if (!button || !outlines) return false;
    const lon = facing(), decl = declination(), selected = button.classList.contains('active');
    const frame = render(lon, decl, selected);
    let canvas = button.querySelector('canvas.globe-icon');
    if (!canvas) {
      button.innerHTML = '';
      canvas = document.createElement('canvas');
      canvas.className = 'globe-icon';
      canvas.setAttribute('aria-hidden', 'true');
      button.append(canvas);
    }
    const key = `${lon}:${decl}:${frame.width}:${selected}`;
    if (canvas.dataset.key !== key) {
      canvas.width = frame.width; canvas.height = frame.height;
      canvas.style.width = canvas.style.height = `${SIZE}px`;
      canvas.getContext('2d').drawImage(frame, 0, 0);
      canvas.dataset.key = key;
    }
    return true;
  }

  load();
  setInterval(() => { if (!document.hidden) update(); }, TICK_MS);
  // frame(lon): one frame as a canvas (for a look), null before the outlines load
  window.GlobeIcon = { update, frame: (lon) => (outlines ? render(lon, declination()) : null) };
})();
