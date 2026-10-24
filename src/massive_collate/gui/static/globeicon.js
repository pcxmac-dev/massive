// The Markets rail icon: the Earth as the Sun sees it, in 24 frames (one per hour of UTC; the
// frame for the current hour is shown), turned so the place where it is noon faces you, like the
// Markets view, from real imagery: NASA's Blue Marble (land, vegetation, ice, sea) by day and
// Black Marble (city lights) by night. While any exchange trades (forex and crypto do not count
// for now) it shows the day side; when none does (weekends, holidays) the globe is turned
// 12 hours and only the night side, lit by its cities, shows.
// The images (vendor/world/blue-marble.jpg, black-marble.jpg: public domain, see VERSION) are
// optional: without them the rail keeps its drawn globe.
(() => {
  'use strict';

  const SIZE = 44;           // css px (the rail's icons are 40)
  const FRAMES = 24;         // one per hour of UTC
  const MARKETS_MS = 300_000;
  const SKIP = new Set(['fx', 'crypto']);  // not markets for this icon (yet)
  const rad = Math.PI / 180;
  let textures = null;       // { day: ImageData, night: ImageData } once loaded
  let failed = false;
  let declination = 0;
  let trading = true;
  let marketsAt = 0;
  const cache = new Map();   // `${mode}:${hour}:${decl}` -> canvas

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = src;
    });
  }

  // Each image shrunk once to a small equirectangular map: plenty for a 22-px globe.
  function pixels(img, w = 720) {
    const c = document.createElement('canvas');
    c.width = w; c.height = w / 2;
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(img, 0, 0, c.width, c.height);
    return g.getImageData(0, 0, c.width, c.height);
  }

  async function load() {
    if (textures || failed) return;
    try {
      const [day, night] = await Promise.all([loadImage('vendor/world/blue-marble.jpg'), loadImage('vendor/world/black-marble.jpg')]);
      textures = { day: pixels(day), night: pixels(night) };
      update();
    } catch {
      failed = true;  // not downloaded: the drawn globe stays
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

  // One frame: an orthographic view centred on (lat0, lon0), each pixel sampled from the map.
  function render(mode, hour) {
    const key = `${mode}:${hour}:${declination}`;
    if (cache.has(key)) return cache.get(key);
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const n = Math.round(SIZE * dpr);
    const c = document.createElement('canvas');
    c.width = c.height = n;
    const g = c.getContext('2d');
    const out = g.createImageData(n, n);
    const map = mode === 'day' ? textures.day : textures.night;
    // Where the Sun is overhead at this hour (noon at 0 degrees at 12:00 UTC), or the opposite point.
    const lon0 = (mode === 'day' ? -15 * (hour + 0.5 - 12) : 180 - 15 * (hour + 0.5 - 12)) * rad;
    const lat0 = (mode === 'day' ? declination : -declination) * rad;
    const sinLat0 = Math.sin(lat0), cosLat0 = Math.cos(lat0);
    const r = n / 2 - 0.5;
    for (let py = 0; py < n; py++) {
      for (let px = 0; px < n; px++) {
        const x = (px + 0.5 - n / 2) / r, y = -(py + 0.5 - n / 2) / r;
        const rho = Math.hypot(x, y);
        if (rho > 1) continue;
        const cc = Math.asin(Math.min(1, rho)), sc = Math.sin(cc), cosc = Math.cos(cc);
        const lat = rho ? Math.asin(cosc * sinLat0 + (y * sc * cosLat0) / rho) : lat0;
        const lon = lon0 + Math.atan2(x * sc, rho * cosLat0 * cosc - y * sinLat0 * sc);
        const u = ((((lon / rad + 180) % 360) + 360) % 360) / 360;
        const v = (90 - lat / rad) / 180;
        const sx = Math.min(map.width - 1, Math.floor(u * map.width));
        const sy = Math.min(map.height - 1, Math.floor(v * map.height));
        const i = (sy * map.width + sx) * 4, o = (py * n + px) * 4;
        const z = Math.sqrt(Math.max(0, 1 - rho * rho));  // facing the viewer (and, by day, the Sun)
        // By day the Sun lights the disc from the centre; by night the cities shine over a faint
        // deep blue, so the globe still reads against a dark rail.
        const light = mode === 'day' ? 0.6 + 0.55 * z : 1.3 + 0.5 * z;
        const base = mode === 'day' ? [0, 0, 0] : [10, 18, 42];
        out.data[o] = Math.min(255, base[0] + map.data[i] * light);
        out.data[o + 1] = Math.min(255, base[1] + map.data[i + 1] * light);
        out.data[o + 2] = Math.min(255, base[2] + map.data[i + 2] * light);
        out.data[o + 3] = rho > 0.97 ? Math.round(255 * (1 - rho) / 0.03) : 255;  // a soft edge
      }
    }
    g.putImageData(out, 0, 0);
    if (mode === 'day') {  // a thin atmosphere
      g.strokeStyle = 'rgba(120,170,255,0.55)'; g.lineWidth = Math.max(1, dpr * 0.8);
      g.beginPath(); g.arc(n / 2, n / 2, r - g.lineWidth / 2, 0, Math.PI * 2); g.stroke();
    }
    cache.set(key, c);
    return c;
  }

  // Shown in the Markets rail button: true when the globe is drawn (the images are here).
  function update() {
    const button = document.getElementById('railMarkets');
    if (!button || !textures) return false;
    const mode = trading ? 'day' : 'night';
    const hour = new Date().getUTCHours();
    let canvas = button.querySelector('canvas.globe-icon');
    if (!canvas) {
      button.innerHTML = '';
      canvas = document.createElement('canvas');
      canvas.className = 'globe-icon';
      canvas.setAttribute('aria-hidden', 'true');
      button.append(canvas);
    }
    const frame = render(mode, hour);
    if (canvas.dataset.key !== `${mode}:${hour}:${declination}`) {
      canvas.width = frame.width; canvas.height = frame.height;
      canvas.style.width = canvas.style.height = `${SIZE}px`;
      canvas.getContext('2d').drawImage(frame, 0, 0);
      canvas.dataset.key = `${mode}:${hour}:${declination}`;
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
  // frame(mode, hour): one frame as a canvas (for a look at the day and night sides), null before the images load
  window.GlobeIcon = { update: tick, frame: (mode, hour) => (textures ? render(mode, hour) : null) };
})();
