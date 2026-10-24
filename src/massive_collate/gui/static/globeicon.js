// The Markets rail icon: the Markets view's wireframe globe in miniature, turned as the view turns
// it, so the point where it is noon faces you: 24 frames, one per hour of UTC (the current
// hour's is shown). Its sphere, graticule, coastlines and borders (Natural Earth 1:50m) and limb,
// in its colours, at the screen's full pixel density.
(() => {
  'use strict';

  const SIZE = 35;  // css px (the rail's icons are 32)
  const rad = Math.PI / 180;
  let outlines = null;  // every arc of the topology (coastlines and borders): [[lon, lat], ...]
  let failed = false;
  const cache = new Map();  // `${hour}:${declination}:${dpr}` -> canvas

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

  // One frame: an orthographic view centred where it is noon at this hour, drawn like the Markets
  // view's globe (globe.js: sphere, graticule, outlines, limb).
  function render(hour, decl) {
    const dpr = Math.min(4, Math.max(1, window.devicePixelRatio || 1));
    const key = `${hour}:${decl}:${dpr}`;
    if (cache.has(key)) return cache.get(key);
    const n = Math.round(SIZE * dpr);
    const c = document.createElement('canvas');
    c.width = c.height = n;
    const g = c.getContext('2d');
    const lon0 = -15 * (hour + 0.5 - 12) * rad, lat0 = decl * rad;
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
    const hour = new Date().getUTCHours(), decl = declination();
    const frame = render(hour, decl);
    let canvas = button.querySelector('canvas.globe-icon');
    if (!canvas) {
      button.innerHTML = '';
      canvas = document.createElement('canvas');
      canvas.className = 'globe-icon';
      canvas.setAttribute('aria-hidden', 'true');
      button.append(canvas);
    }
    const key = `${hour}:${decl}:${frame.width}`;
    if (canvas.dataset.key !== key) {
      canvas.width = frame.width; canvas.height = frame.height;
      canvas.style.width = canvas.style.height = `${SIZE}px`;
      canvas.getContext('2d').drawImage(frame, 0, 0);
      canvas.dataset.key = key;
    }
    return true;
  }

  load();
  setInterval(update, 60_000);
  // frame(hour): one frame as a canvas (for a look), null before the outlines load
  window.GlobeIcon = { update, frame: (hour) => (outlines ? render(hour, declination()) : null) };
})();
