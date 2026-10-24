'use strict';
/* Markets view: a wireframe globe turned so the place where it is noon right now (the subsolar
   point) faces you, with the world's stock exchanges on it: open ones glow green, lunch breaks
   amber, closed ones grey. Night is shaded behind the terminator. Coastlines and country borders
   are drawn when vendor/world/countries-110m.json (Natural Earth, public domain) is present.
   The globe turns in real time (the sun's position is computed every frame, so the Earth
   rotates under it at 15 degrees an hour), wherever it has been dragged to. Drag to turn it;
   click the sun (or double-click anywhere) to glide back to noon.
   The Moon is drawn at its true size relative to the Earth, lit from the Sun's real direction,
   off the globe's edge in its real direction (in front of or behind the Earth); planets show
   where they are overhead, faintly. Behind the globe is the real sky in that direction: stars
   and constellation figures (vendor/sky, from d3-celestial) turning with the Earth. Beside it: every exchange by region
   with its local time, state, countdown, today's hours (lunch breaks included) and holidays. */
(() => {
  const canvas = document.getElementById('globe');
  const ctx = canvas.getContext('2d');
  // GPU renderer (globe-gl.js) for the sky, sphere, shading, lines and Moon; the 2D canvas above
  // keeps markers and labels. Without WebGL2 everything is drawn on the 2D canvas as before.
  let glr = null;
  try { glr = window.GlobeGL ? new window.GlobeGL(document.getElementById('globeGL')) : null; } catch (err) {
    console.warn('globe: GPU renderer unavailable, drawing on the CPU', err);
  }
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
  let news = { items: [], i: 0, shownAt: 0, fetchedAt: 0 };  // announcements ticker
  const ticker = document.getElementById('newsTicker');
  const TICK_MS = 6500;

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

  // Civil time zones: a 0.1-degree grid of IANA zone names (tools/build_timezones.py). Each
  // zone's offset right now comes from the browser's own time-zone database, so daylight saving
  // and every rule change are included.
  let tz = null;  // { res, width, height, grid: Uint16Array, zones: [name], offsets: [minutes], at }
  async function loadZones() {
    try {
      const res = await fetch('vendor/world/time-zones.json');
      if (!res.ok) return;
      const d = await res.json();
      const grid = new Uint16Array(d.width * d.height);
      d.rows.forEach((row, r) => {
        let c = r * d.width;
        for (let k = 0; k < row.length; k += 2) { grid.fill(row[k], c, c + row[k + 1]); c += row[k + 1]; }
      });
      tz = { res: d.res, width: d.width, height: d.height, grid, zones: d.zones, anchors: d.anchors, offsets: [], at: 0 };
      if (glr) glr.setZones(d.width, d.height, grid, d.res);
      refreshOffsets();
      zoneCache = null;
    } catch { /* fall back to solar bands */ }
  }

  function offsetMinutes(name, when) {
    if (name.startsWith('fixed:')) return Math.round(parseFloat(name.slice(6)) * 60);
    try {
      const part = new Intl.DateTimeFormat('en-US', { timeZone: name, timeZoneName: 'longOffset' })
        .formatToParts(when).find((x) => x.type === 'timeZoneName')?.value || 'GMT';
      const m = part.match(/GMT([+\-−])(\d{1,2})(?::(\d{2}))?/);
      return m ? (m[1] === '+' ? 1 : -1) * (Number(m[2]) * 60 + Number(m[3] || 0)) : 0;
    } catch { return null; }
  }

  function refreshOffsets() {
    if (!tz) return;
    const now = new Date();
    tz.offsets = [null, ...tz.zones.map((z) => offsetMinutes(z, now))];
    tz.at = Date.now();
    if (glr) glr.setZoneOffsets(tz.offsets);
  }

  function zoneAt(lon, lat) {
    if (!tz) return 0;
    const row = Math.min(tz.height - 1, Math.max(0, Math.floor((90 - lat) / tz.res)));
    const col = ((Math.floor((lon + 180) / tz.res) % tz.width) + tz.width) % tz.width;
    return tz.grid[row * tz.width + col];
  }

  function gmtLabel(min) {
    if (min === 0) return 'GMT';
    const a = Math.abs(min), h = Math.floor(a / 60), m = a % 60;
    return `GMT${min > 0 ? '+' : '−'}${h}${m ? `:${String(m).padStart(2, '0')}` : ''}`;
  }

  let topo50 = null;
  async function loadTopo50() {
    try {
      const res = await fetch('vendor/world/countries-50m.json');
      if (res.ok) { topo50 = decodeTopo(await res.json()); if (glr) glr.setLines('o50', topo50); }
    } catch { /* keep the 110m outlines */ }
  }

  // Finest coastlines and borders: GSHHG high resolution (~200 m) in 10-degree tiles
  // (tools/build_coastlines.py), loaded for the region on screen past HIRES_ZOOM and kept on the
  // GPU for the most recently used MAX_TILES.
  const HIRES_ZOOM = 12, MAX_TILES = 120;
  let hires = null;  // { tile, margin, names: Set }
  const tiles = new Map();  // name -> 'loading' | 'ready' (insertion order = use order)
  async function loadHiresIndex() {
    try {
      const res = await fetch('vendor/world/coast-h/index.json');
      if (res.ok) { const d = await res.json(); hires = { tile: d.tile, margin: d.margin, names: new Set(Object.keys(d.tiles)) }; }
    } catch { /* stay at 1:10m */ }
  }

  function decodeTile(buf, name) {
    const [lat0, lon0] = name.split('_').map(Number);
    const span = hires.tile + 2 * hires.margin;
    const dv = new DataView(buf);
    let off = 4;  // "GSH1"
    const lines = [];
    for (let section = 0; section < 2; section++) {
      const count = dv.getUint32(off, true); off += 4;
      for (let i = 0; i < count; i++) {
        const n = dv.getUint32(off, true); off += 4;
        const line = new Array(n);
        for (let k = 0; k < n; k++) {
          line[k] = [lon0 - hires.margin + (dv.getUint16(off, true) / 65535) * span,
                     lat0 - hires.margin + (dv.getUint16(off + 2, true) / 65535) * span];
          off += 4;
        }
        lines.push(line);
      }
    }
    return lines;
  }

  // Tiles whose area can be on screen: within the visible cap around the view centre.
  function visibleTiles(g) {
    const half = Math.hypot(g.w, g.h) / 2;
    const cap = Math.asin(Math.min(1, half / g.r)) / rad + hires.tile * 0.75;
    const c = unit(view.lon, view.lat);
    const out = [];
    for (let lat0 = -90; lat0 < 90; lat0 += hires.tile) {
      for (let lon0 = -180; lon0 < 180; lon0 += hires.tile) {
        const name = `${lat0}_${lon0}`;
        if (!hires.names.has(name)) continue;
        const d = Math.acos(Math.max(-1, Math.min(1, dot(unit(lon0 + hires.tile / 2, lat0 + hires.tile / 2), c)))) / rad;
        if (d <= cap) out.push(name);
      }
    }
    return out;
  }

  function hiresTiles(g) {
    if (!glr || zoom <= HIRES_ZOOM) return null;
    if (!hires) { loadHiresIndex(); return null; }
    const want = visibleTiles(g);
    let loading = 0;
    for (const name of want) {
      const state = tiles.get(name);
      if (state) { tiles.delete(name); tiles.set(name, state); continue; }  // mark as recently used
      if (loading >= 4) continue;
      tiles.set(name, 'loading');
      loading++;
      fetch(`vendor/world/coast-h/${name}.bin`).then((r) => (r.ok ? r.arrayBuffer() : null)).then((buf) => {
        if (!buf) { tiles.delete(name); return; }
        glr.setLines(`hires:${name}`, decodeTile(buf, name));
        tiles.set(name, 'ready');
        while (tiles.size > MAX_TILES) {  // evict the least recently used
          const [old] = tiles.keys();
          tiles.delete(old);
          glr.deleteLines(`hires:${old}`);
        }
      }).catch(() => tiles.delete(name));
    }
    const ready = want.filter((n) => tiles.get(n) === 'ready');
    // until every visible tile is in, draw the 1:10m outlines underneath as well
    return { names: ready.map((n) => `hires:${n}`), complete: ready.length === want.length };
  }

  let topo10 = null, loading10 = false;
  async function loadTopo10() {  // finest outlines, for close zoom (GPU only)
    if (loading10) return;
    loading10 = true;
    try {
      const res = await fetch('vendor/world/countries-10m.json');
      if (res.ok) { topo10 = decodeTopo(await res.json()); if (glr) glr.setLines('o10', topo10); }
    } catch { /* keep the 50m outlines */ }
  }

  async function loadTopo() {
    try {
      const res = await fetch('vendor/world/countries-110m.json');
      if (!res.ok) return;
      topo = decodeTopo(await res.json());
      if (glr) glr.setLines('o110', topo);
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

  // -- the sky ------------------------------------------------------------------------------
  // Greenwich mean sidereal time turns sky coordinates (RA/Dec) into Earth-fixed directions:
  // a body at RA/Dec is overhead at longitude RA - GMST, latitude Dec.
  function gmst(t = Date.now()) {
    const jd = t / 864e5 + 2440587.5;
    return (((280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360) + 360) % 360;
  }
  const unit = (lon, lat) => [Math.cos(lat * rad) * Math.cos(lon * rad), Math.cos(lat * rad) * Math.sin(lon * rad), Math.sin(lat * rad)];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  // The camera frame: C points from the Earth's centre at the viewer, E to screen-right, N up.
  function camera() {
    const l = view.lon * rad, p = view.lat * rad;
    return { C: [Math.cos(p) * Math.cos(l), Math.cos(p) * Math.sin(l), Math.sin(p)],
             E: [-Math.sin(l), Math.cos(l), 0], N: [-Math.sin(p) * Math.cos(l), -Math.sin(p) * Math.sin(l), Math.cos(p)] };
  }
  let skyData = null;  // { lines: [[ [ra,dec], ... ]], stars: [[ra, dec, mag]], names: [[ra, dec, name]] }

  async function loadSky() {
    try {
      const get = async (f) => { const r = await fetch(`vendor/sky/${f}`); return r.ok ? r.json() : null; };
      const [lines, stars, names] = await Promise.all([get('constellations.lines.json'), get('stars.6.json'), get('constellations.json')]);
      if (!lines && !stars) return;
      const ra = (x) => (x + 360) % 360;
      skyData = {  // (also handed to the GPU renderer below)
        lines: (lines?.features || []).flatMap((f) => f.geometry.coordinates.map((part) => part.map(([x, y]) => [ra(x), y]))),
        stars: (stars?.features || []).map((f) => [ra(f.geometry.coordinates[0]), f.geometry.coordinates[1], f.properties.mag]),
        names: (names?.features || []).filter((f) => (f.properties.rank ?? 1) <= 2)
          .map((f) => [ra(f.geometry.coordinates[0]), f.geometry.coordinates[1], f.properties.name || f.id]),
      };
      if (glr && skyData) glr.setSky(skyData.stars, skyData.lines);
    } catch { /* no star data: plain background */ }
  }

  // -- projection -------------------------------------------------------------------------
  const TOP = 64;  // the announcements ticker and the info line sit above the globe
  let zoom = 1;    // 1 = whole globe; wheel / pinch zooms in up to 12x
  function geometry() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    const r = Math.max(40, Math.min(w * 0.36, ((h - TOP - 28) / 2) * 0.78)) * zoom;  // room for the Moon's orbit
    return { w, h, cx: w / 2, cy: TOP + (h - TOP) / 2, r };
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
  // Shading layers are computed over the visible canvas at a quarter of its resolution, so they
  // stay sharp however far the globe is zoomed in. `shade(lon, lat)` returns [r, g, b, alpha 0-1].
  const LAYER_STEP = 4;
  function raster(g, shade) {
    const nx = Math.ceil(g.w / LAYER_STEP), ny = Math.ceil(g.h / LAYER_STEP);
    const off = document.createElement('canvas');
    off.width = nx; off.height = ny;
    const o = off.getContext('2d'), img = o.createImageData(nx, ny);
    for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
      const q = unproject((i + 0.5) * LAYER_STEP, (j + 0.5) * LAYER_STEP, g);
      if (!q) continue;
      const c = shade(q.lon, q.lat);
      if (!c) continue;
      const k = (j * nx + i) * 4;
      img.data[k] = c[0]; img.data[k + 1] = c[1]; img.data[k + 2] = c[2]; img.data[k + 3] = Math.round(c[3] * 255);
    }
    o.putImageData(img, 0, 0);
    return off;
  }

  function nightLayer(g) {
    // The night side moves with the sun: at most a few redraws a minute at this precision.
    const key = `${g.w}x${g.h}|${g.r.toFixed(0)}|${view.lon.toFixed(1)}|${view.lat.toFixed(1)}|${markets?.sun.lon.toFixed(1)}`;
    if (nightCache?.key === key) return nightCache.canvas;
    const sun = markets?.sun || { lon: view.lon, lat: view.lat };
    const sx = Math.cos(sun.lat * rad) * Math.cos(sun.lon * rad), sy = Math.cos(sun.lat * rad) * Math.sin(sun.lon * rad), sz = Math.sin(sun.lat * rad);
    const canvasOut = raster(g, (lon, lat) => {
      const d = Math.cos(lat * rad) * Math.cos(lon * rad) * sx + Math.cos(lat * rad) * Math.sin(lon * rad) * sy + Math.sin(lat * rad) * sz;
      return d < 0 ? [6, 10, 24, Math.min(1, -d * 4) * 0.55] : null;  // soft twilight edge
    });
    nightCache = { key, canvas: canvasOut };
    return canvasOut;
  }

  const STATE_COLOR = { open: '#26a69a', break: '#f0a020', closed: '#787b86', unknown: '#555' };
  const ZONES = 8;  // solar time zones shown either side of noon

  // Solar time zones: 15-degree bands centred on the meridian where it is noon now, the noon
  // band brightest and the 8 either side fading. One low-resolution layer, redrawn as they move.
  let zoneCache = null;
  function zoneLayer(g) {
    const sunLon = markets?.sun.lon ?? view.lon;
    const key = `${g.w}x${g.h}|${g.r.toFixed(0)}|${view.lon.toFixed(1)}|${view.lat.toFixed(1)}|${sunLon.toFixed(1)}|${tz ? tz.at : 0}|${new Date().getUTCMinutes()}`;
    if (zoneCache?.key === key) return zoneCache.canvas;
    const utcMin = new Date().getUTCHours() * 60 + new Date().getUTCMinutes();
    const canvasOut = raster(g, (lon, lat) => {
      let k;
      if (tz) {  // civil zones: hours between local clock time and noon
        const off = tz.offsets[zoneAt(lon, lat)];
        if (off == null) return null;
        k = Math.floor((((utcMin + off) % 1440) + 1440) % 1440 / 60) - 12;
      } else {   // until the zone grid loads: solar bands
        k = Math.round(wrap(lon - sunLon) / 15);
      }
      if (Math.abs(k) > ZONES) return null;
      const a = k === 0 ? 0.11 : 0.07 * (1 - (Math.abs(k) - 1) / ZONES) * (k % 2 ? 1 : 0.55);  // alternate so zones read apart
      return [240, 185, 11, a];
    });
    zoneCache = { key, canvas: canvasOut };
    return canvasOut;
  }

  function drawZones(g, dark) {
    ctx.save(); ctx.beginPath(); ctx.arc(g.cx, g.cy, g.r, 0, 2 * Math.PI); ctx.clip();
    ctx.drawImage(zoneLayer(g), 0, 0, g.w, g.h);
    ctx.restore();
    drawZoneLabels(g);
  }

  function drawZoneLabels(g) {
    const sunLon = markets.sun.lon;
    ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = 'center';
    if (tz) {
      if (Date.now() - tz.at > 60000) refreshOffsets();
      // each zone's offset right now (daylight saving included), at anchors inside the zone
      const placed = [];
      for (const [lon, lat] of tz.anchors) {
        if (lat < 55) continue;  // one row of labels, up towards the North Pole
        const p = project(lon, lat, g);
        if (!p.visible || p.depth < 0.2 || p.x < 0 || p.x > g.w || p.y < TOP) continue;
        const off = tz.offsets[zoneAt(lon, lat)];
        if (off == null) continue;
        const text = gmtLabel(off);
        if (placed.some((q) => Math.hypot(q.x - p.x, q.y - p.y) < 46)) continue;
        placed.push(p);
        ctx.fillStyle = 'rgba(240,185,11,0.7)';
        ctx.fillText(text, p.x, p.y);
      }
    } else {
      const labelLat = Math.min(70, view.lat + 52 / Math.sqrt(zoom));
      for (let k = -ZONES; k <= ZONES; k++) {
        const lon = sunLon + k * 15;
        const p = project(lon, labelLat, g);
        if (!p.visible || p.depth < 0.15 || p.x < 0 || p.x > g.w) continue;
        ctx.fillStyle = `rgba(240,185,11,${0.85 - Math.abs(k) * 0.07})`;
        ctx.fillText(gmtLabel(Math.round(wrap(lon) / 15) * 60), p.x, p.y);
      }
    }
    ctx.textAlign = 'start';
  }

  // Performance colour: a market's daily move around the colour wheel, green (up strongly)
  // through blue (about average) to red (down hard). +-2% is the full scale.
  const PERF_SCALE = 2;
  function perfColor(pct) {
    const z = Math.max(-1, Math.min(1, pct / PERF_SCALE));
    const hue = z >= 0 ? 220 - 100 * z : 220 + 140 * -z;  // 120 green ... 220 blue ... 360 red
    return `hsl(${hue % 360}, 75%, 55%)`;
  }

  // Background sky: the directions behind the globe, as a camera at the viewer's side sees them
  // (gnomonic projection towards -C). Stars by magnitude, constellation figures, planets.
  function skyXY(dir, fr, g, F) {
    const depth = -dot(dir, fr.C);
    if (depth < 0.08) return null;
    return { x: g.cx + (F * dot(dir, fr.E)) / depth, y: g.cy - (F * dot(dir, fr.N)) / depth };
  }

  // Constellation names and planets in the background sky (2D: a few dozen labels).
  function drawSkyNames(g, fr, g0, bodies, dark, { names = true, planets = true } = {}) {
    const F = g.r * 1.15;
    if (skyData && names) {
      ctx.font = '9px system-ui, sans-serif';
      ctx.fillStyle = dark ? 'rgba(140,160,210,0.45)' : 'rgba(60,80,140,0.45)';
      for (const [ra, dec, name] of skyData.names) {
        const q = skyXY(unit(ra - g0, dec), fr, g, F);
        if (q && q.x > 0 && q.y > TOP && q.x < g.w && q.y < g.h && Math.hypot(q.x - g.cx, q.y - g.cy) > g.r) ctx.fillText(name, q.x, q.y);
      }
    }
    if (!planets) return;
    ctx.font = '10px system-ui, sans-serif';
    for (const b of bodies) {
      if (b.id === 'sun' || b.id === 'moon') continue;
      const q = skyXY(b.dir, fr, g, F);
      if (!q || Math.hypot(q.x - g.cx, q.y - g.cy) < g.r) continue;
      ctx.fillStyle = dark ? 'rgba(255,220,160,0.7)' : 'rgba(140,90,20,0.7)';
      ctx.beginPath(); ctx.arc(q.x, q.y, 1.6, 0, 2 * Math.PI); ctx.fill();
      ctx.fillText(`${b.symbol} ${b.name}`, q.x + 4, q.y - 3);
    }
  }

  function drawSky(g, fr, g0, bodies, dark) {
    const F = g.r * 1.15;
    if (skyData) {
      ctx.beginPath();
      for (const line of skyData.lines) {
        let pen = false;
        for (const [ra, dec] of line) {
          const q = skyXY(unit(ra - g0, dec), fr, g, F);
          if (!q) { pen = false; continue; }
          if (!pen) { ctx.moveTo(q.x, q.y); pen = true; } else ctx.lineTo(q.x, q.y);
        }
      }
      ctx.strokeStyle = dark ? 'rgba(120,150,220,0.22)' : 'rgba(60,80,140,0.18)'; ctx.lineWidth = 0.8; ctx.stroke();
      for (const [ra, dec, mag] of skyData.stars) {
        const q = skyXY(unit(ra - g0, dec), fr, g, F);
        if (!q || q.x < 0 || q.y < 0 || q.x > g.w || q.y > g.h) continue;
        const size = Math.max(0.4, 2.2 - mag * 0.32);
        ctx.fillStyle = dark ? `rgba(230,235,255,${Math.max(0.15, 0.9 - mag * 0.12)})` : `rgba(40,50,90,${Math.max(0.12, 0.7 - mag * 0.1)})`;
        ctx.fillRect(q.x - size / 2, q.y - size / 2, size, size);
      }
    }
    drawSkyNames(g, fr, g0, bodies, dark, { planets: false });
    // planets in the background sky, as faint dots with their symbol
    ctx.font = '10px system-ui, sans-serif';
    for (const b of bodies) {
      if (b.id === 'sun' || b.id === 'moon') continue;
      const q = skyXY(b.dir, fr, g, F);
      if (!q) continue;
      ctx.fillStyle = dark ? 'rgba(255,220,160,0.7)' : 'rgba(140,90,20,0.7)';
      ctx.beginPath(); ctx.arc(q.x, q.y, 1.6, 0, 2 * Math.PI); ctx.fill();
      ctx.fillText(`${b.symbol} ${b.name}`, q.x + 4, q.y - 3);
    }
  }

  // The Moon at its true size (0.273 Earth radii), just off the globe in its real direction, as
  // it looks: the near side (which always faces the Earth) with its maria and bright craters at
  // their selenographic positions, lit from the Sun's real direction. Kept dim so it does not
  // compete with the markets.
  // [latitude, longitude, angular radius, darkness]: the large maria and two bright rayed craters.
  const MARIA = [
    [32.8, -15.6, 19, 0.42], [28.0, 17.5, 11, 0.40], [8.5, 31.4, 13, 0.42], [17.0, 59.1, 8, 0.40],
    [-7.8, 51.3, 12, 0.36], [-15.2, 35.5, 6, 0.36], [-21.3, -16.6, 11, 0.32], [-24.4, -38.6, 6, 0.34],
    [18.4, -57.4, 22, 0.38], [5.0, -45.0, 18, 0.30], [56.0, 1.4, 8, 0.28], [58.0, -25.0, 7, 0.24],
    [13.3, 3.6, 4, 0.28], [-10.0, -23.0, 5, 0.26], [7.5, -30.9, 8, 0.28], [-3.0, 3.0, 4, 0.22],
  ];
  const BRIGHT = [[-43.3, -11.2, 1.6, 0.5], [9.6, -20.1, 1.4, 0.35], [23.7, -47.4, 1.0, 0.45]];  // Tycho, Copernicus, Aristarchus

  // Small deterministic texture so the surface is not flat (value noise on the sphere).
  function hash(x, y, z) {
    let h = Math.sin(x * 127.1 + y * 311.7 + z * 74.7) * 43758.5453;
    return h - Math.floor(h);
  }
  function noise(v, scale) {
    const x = v[0] * scale, y = v[1] * scale, z = v[2] * scale;
    const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
    const xf = x - xi, yf = y - yi, zf = z - zi;
    const sm = (t) => t * t * (3 - 2 * t);
    let total = 0;
    for (let dx = 0; dx <= 1; dx++) for (let dy = 0; dy <= 1; dy++) for (let dz = 0; dz <= 1; dz++) {
      const w = (dx ? sm(xf) : 1 - sm(xf)) * (dy ? sm(yf) : 1 - sm(yf)) * (dz ? sm(zf) : 1 - sm(zf));
      total += w * hash(xi + dx, yi + dy, zi + dz);
    }
    return total;
  }

  function albedo(lat, lon, v) {
    let a = 0.78 + 0.10 * (noise(v, 9) - 0.5) + 0.06 * (noise(v, 23) - 0.5);
    for (const [mlat, mlon, r, dark] of MARIA) {
      const dist = Math.acos(Math.max(-1, Math.min(1, Math.sin(lat * rad) * Math.sin(mlat * rad)
        + Math.cos(lat * rad) * Math.cos(mlat * rad) * Math.cos((lon - mlon) * rad)))) / rad;
      const edge = r * (0.8 + 0.4 * noise(v, 5));  // ragged, soft shores
      if (dist < edge * 1.25) a -= dark * 0.8 * Math.min(1, Math.max(0, (edge * 1.25 - dist) / (edge * 0.7)));
    }
    for (const [clat, clon, r, glow] of BRIGHT) {
      const dist = Math.acos(Math.max(-1, Math.min(1, Math.sin(lat * rad) * Math.sin(clat * rad)
        + Math.cos(lat * rad) * Math.cos(clat * rad) * Math.cos((lon - clon) * rad)))) / rad;
      if (dist < r * 6) a += glow * 0.6 * Math.exp(-dist / r) * (dist < r ? 1 : 0.35);  // crater and its rays
    }
    return Math.max(0.15, Math.min(1, a));
  }

  let moonCache = null;
  function drawMoon(g, fr, moon, sunDir, dark) {
    const rM = Math.max(6, g.r * 0.2727);
    // A real 3D position: its true direction from the Earth, at a compressed distance (the real
    // one is 60 Earth radii), projected like the globe. It circles smoothly as the Earth turns,
    // and on the far side the (opaque) globe drawn after it hides it.
    const D = g.r * 1.22;
    const mx = g.cx + D * dot(moon.dir, fr.E), my = g.cy - D * dot(moon.dir, fr.N);
    // The Moon's own axes: its near side faces the Earth (f = -direction), north roughly
    // along the Earth's axis, selenographic east to the right as seen from the Earth.
    const m = moon.dir;
    const f = [-m[0], -m[1], -m[2]];
    let u = [0 - dot([0, 0, 1], f) * f[0], 0 - dot([0, 0, 1], f) * f[1], 1 - dot([0, 0, 1], f) * f[2]];
    const ul = Math.hypot(...u); u = u.map((x) => x / ul);
    const east = [m[1] * u[2] - m[2] * u[1], m[2] * u[0] - m[0] * u[2], m[0] * u[1] - m[1] * u[0]];  // m x u

    const size = Math.ceil(rM * 2);
    const key = `${size}|${sunDir.map((v) => v.toFixed(3))}|${fr.C.map((v) => v.toFixed(3))}|${m.map((v) => v.toFixed(3))}|${dark}`;
    if (moonCache?.key !== key) {
      const off = document.createElement('canvas');
      off.width = off.height = size;
      const o = off.getContext('2d'), img = o.createImageData(size, size);
      for (let j = 0; j < size; j++) for (let i = 0; i < size; i++) {
        const x = (i + 0.5) / rM - 1, y = 1 - (j + 0.5) / rM;
        const rr = x * x + y * y;
        if (rr > 1) continue;
        const z = Math.sqrt(1 - rr);
        const n = [x * fr.E[0] + y * fr.N[0] + z * fr.C[0], x * fr.E[1] + y * fr.N[1] + z * fr.C[1], x * fr.E[2] + y * fr.N[2] + z * fr.C[2]];
        const lat = Math.asin(Math.max(-1, Math.min(1, dot(n, u)))) / rad;
        const lon = Math.atan2(dot(n, east), dot(n, f)) / rad;
        const sun = dot(n, sunDir);
        // Lunar regolith barely darkens towards the limb: mostly flat lighting with a soft terminator.
        const light = sun > 0 ? Math.min(1, 0.25 + sun * 0.9) : Math.max(0, 0.04 + sun * 0.2);
        const v = 120 * albedo(lat, lon, n) * light;  // dim on purpose: background, not foreground
        const edge = Math.min(1, (1 - Math.sqrt(rr)) * rM * 0.8);  // anti-aliased rim
        const k = (j * size + i) * 4;
        img.data[k] = v; img.data[k + 1] = v * 0.98; img.data[k + 2] = v * 0.94;
        img.data[k + 3] = Math.round(255 * edge * (dark ? 0.42 : 0.32));
      }
      o.putImageData(img, 0, 0);
      moonCache = { key, canvas: off };
    }
    ctx.drawImage(moonCache.canvas, mx - rM, my - rM, size, size);
    ctx.font = '10px system-ui, sans-serif';
    ctx.fillStyle = dark ? 'rgba(170,175,190,0.3)' : 'rgba(60,60,80,0.3)';
    ctx.fillText(`☾ ${moon.phaseName} ${Math.round(moon.illuminated * 100)}%`, mx - rM, my + rM + 12);
  }

  // Moon parameters for the GPU: position (compressed distance, true direction), its own axes.
  function moonParams(g, fr, moon) {
    const m = moon.dir;
    const f = [-m[0], -m[1], -m[2]];
    const fz = f[2];
    let u = [-fz * f[0], -fz * f[1], 1 - fz * f[2]];
    const ul = Math.hypot(...u); u = u.map((x) => x / ul);
    const east = [m[1] * u[2] - m[2] * u[1], m[2] * u[0] - m[0] * u[2], m[0] * u[1] - m[1] * u[0]];
    const D = g.r * 1.22;
    return {
      x: g.cx + D * dot(m, fr.E), y: g.cy - D * dot(m, fr.N), r: Math.max(6, g.r * 0.2727),
      behind: dot(m, fr.C) < 0, f, u, east, maria: MARIA.flat(), bright: BRIGHT.flat(),
    };
  }

  function outlineSet() {
    if (zoom > 6 && !topo10) loadTopo10();
    if (zoom > 2 && !topo50) loadTopo50();
    return zoom > 6 && topo10 ? 'o10' : zoom > 2 && topo50 ? 'o50' : 'o110';
  }

  function draw() {
    if (document.body.dataset.view !== 'markets') return;
    const dpr = window.devicePixelRatio || 1;
    const g = geometry();
    if (canvas.width !== Math.round(g.w * dpr) || canvas.height !== Math.round(g.h * dpr)) {
      canvas.width = Math.round(g.w * dpr); canvas.height = Math.round(g.h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, g.w, g.h);
    const dark = !document.body.classList.contains('light');
    const line = dark ? 'rgba(160,170,200,' : 'rgba(40,50,80,';

    const fr = camera();
    const sky = markets?.sky;
    const g0 = gmst();
    const bodies = (sky?.bodies || []).map((b) => ({ ...b, dir: unit(b.ra - g0, b.dec) }));
    const sunDir = markets ? unit(markets.sun.lon, markets.sun.lat) : fr.C;
    const moon = bodies.find((b) => b.id === 'moon');

    if (glr) {
      const now = new Date();
      const hi = hiresTiles(g);
      glr.render({
        w: g.w, h: g.h, dpr, cx: g.cx, cy: g.cy, r: g.r, E: fr.E, N: fr.N, C: fr.C, S: sunDir, dark,
        gmst: g0, sunLon: markets?.sun.lon ?? view.lon, utcMin: now.getUTCHours() * 60 + now.getUTCMinutes() + now.getUTCSeconds() / 60,
        outlines: outlineSet(),
        tiles: hi && hi.names.length ? (hi.complete ? hi.names : [outlineSet(), ...hi.names]) : null,
        moon: moon ? moonParams(g, fr, moon) : null,
      });
      drawSkyNames(g, fr, g0, bodies, dark);
      if (markets) drawZoneLabels(g);
      if (moon && dot(moon.dir, fr.C) >= 0) {  // phase label under the Moon when it is in view
        const mp = moonParams(g, fr, moon);
        ctx.font = '10px system-ui, sans-serif';
        ctx.fillStyle = dark ? 'rgba(170,175,190,0.3)' : 'rgba(60,60,80,0.3)';
        ctx.fillText(`☾ ${moon.phaseName} ${Math.round(moon.illuminated * 100)}%`, mp.x - mp.r, mp.y + mp.r + 12);
      }
      return drawOverlay(g, fr, g0, bodies, moon, dark);
    }

    drawSky(g, fr, g0, bodies, dark);
    if (moon && dot(moon.dir, fr.C) < 0) drawMoon(g, fr, moon, sunDir, dark);  // behind the Earth

    // sphere
    ctx.beginPath(); ctx.arc(g.cx, g.cy, g.r, 0, 2 * Math.PI);
    ctx.fillStyle = dark ? '#0b0f18' : '#e9ecf2'; ctx.fill();  // opaque: the sky stays behind
    ctx.beginPath(); ctx.arc(g.cx, g.cy, g.r, 0, 2 * Math.PI);
    const grad = ctx.createRadialGradient(g.cx - g.r * 0.3, g.cy - g.r * 0.35, g.r * 0.1, g.cx, g.cy, g.r);
    grad.addColorStop(0, dark ? 'rgba(41,98,255,0.18)' : 'rgba(41,98,255,0.10)');
    grad.addColorStop(1, dark ? 'rgba(41,98,255,0.03)' : 'rgba(41,98,255,0.02)');
    ctx.fillStyle = grad; ctx.fill();
    ctx.strokeStyle = `${line}0.55)`; ctx.lineWidth = 1.2; ctx.stroke();

    // night side
    ctx.save(); ctx.beginPath(); ctx.arc(g.cx, g.cy, g.r, 0, 2 * Math.PI); ctx.clip();
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(nightLayer(g), 0, 0, g.w, g.h);
    ctx.restore();

    if (markets) drawZones(g, dark);

    // graticule
    ctx.beginPath();
    for (let lon = -180; lon < 180; lon += 15) polyline(Array.from({ length: 91 }, (_, i) => [lon, -90 + i * 2]), g);
    for (let lat = -75; lat <= 75; lat += 15) polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, lat]), g);
    ctx.strokeStyle = `${line}0.16)`; ctx.lineWidth = 0.7; ctx.stroke();
    ctx.beginPath(); polyline(Array.from({ length: 181 }, (_, i) => [-180 + i * 2, 0]), g);
    ctx.strokeStyle = `${line}0.30)`; ctx.stroke();

    // coastlines and borders
    const outlines = zoom > 2 && topo50 ? topo50 : topo;
    if (zoom > 2 && !topo50) loadTopo50();
    if (outlines) {
      ctx.beginPath();
      for (const arc of outlines) polyline(arc, g);
      ctx.strokeStyle = `${line}0.62)`; ctx.lineWidth = 0.8; ctx.stroke();
    }

    if (moon && dot(moon.dir, fr.C) >= 0) drawMoon(g, fr, moon, sunDir, dark);  // in front
    drawOverlay(g, fr, g0, bodies, moon, dark);
  }

  // Markers, labels and the legend: the 2D overlay, on top of either renderer.
  function drawOverlay(g, fr, g0, bodies, moon, dark) {
    if (!markets) return;
    // planets where they are overhead: small and faint, so they do not compete with markets
    ctx.font = '11px system-ui, sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (const b of bodies) {
      if (b.id === 'sun' || b.id === 'moon') continue;
      const p = project(b.ra - g0, b.dec, g);
      if (!p.visible) continue;
      ctx.fillStyle = dark ? 'rgba(200,190,255,0.55)' : 'rgba(80,60,140,0.55)';
      ctx.fillText(b.symbol, p.x, p.y);
    }
    ctx.textAlign = 'start'; ctx.textBaseline = 'alphabetic';
    if (moon && dot(moon.dir, fr.C) < 0) {  // behind: say where, faintly, at the limb
      const vx = dot(moon.dir, fr.E), vy = dot(moon.dir, fr.N), len = Math.hypot(vx, vy);
      if (len * g.r * 1.22 + g.r * 0.2727 < g.r) {  // wholly hidden
        const ux = len > 1e-3 ? vx / len : 0.7, uy = len > 1e-3 ? vy / len : 0.7;
        const lx = g.cx + ux * (g.r + 14), ly = g.cy - uy * (g.r + 14);
        ctx.font = '10px system-ui, sans-serif'; ctx.textAlign = ux < 0 ? 'end' : 'start';
        ctx.fillStyle = dark ? 'rgba(170,175,190,0.35)' : 'rgba(60,60,80,0.35)';
        ctx.fillText(`☾ behind the Earth · ${moon.phaseName} ${Math.round(moon.illuminated * 100)}%`, lx, ly);
        ctx.textAlign = 'start';
      }
    }
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

    // exchanges: back to front; labels (drawn after, on top) for the ones operating now
    const pulse = 0.5 + 0.5 * Math.sin(Date.now() / 400);
    const labelled = [];
    const items = markets.world.map((m) => ({ m, p: project(m.lon, m.lat, g) })).filter((x) => x.p.visible)
      .sort((a, b) => a.p.depth - b.p.depth);
    for (const { m, p } of items) {
      const perf = m.performance?.changePct;
      const color = perf != null ? perfColor(perf) : STATE_COLOR[m.state] || STATE_COLOR.unknown;
      const crypto = m.kind === 'crypto';
      if (m.state === 'open' && !crypto) {
        ctx.beginPath(); ctx.arc(p.x, p.y, 6 + 5 * pulse, 0, 2 * Math.PI);
        ctx.fillStyle = `rgba(38,166,154,${0.12 + 0.12 * (1 - pulse)})`; ctx.fill();
      }
      if (perf != null && Math.abs(perf) > 4) {  // shockwave: >4% small, >6% large (two rings)
        const big = Math.abs(perf) > 6;
        const reach = big ? 34 : 18;
        for (const lag of big ? [0, 0.5] : [0]) {
          const t = ((Date.now() / (big ? 1400 : 1700)) + lag + (m.lon + 180) / 360) % 1;
          ctx.beginPath(); ctx.arc(p.x, p.y, 5 + t * reach, 0, 2 * Math.PI);
          ctx.strokeStyle = color; ctx.globalAlpha = (1 - t) * (big ? 0.75 : 0.55);
          ctx.lineWidth = big ? 2 : 1.4; ctx.stroke();
        }
        ctx.globalAlpha = 1;
      }
      const featured = currentItem()?.exchange === m.code;
      if (featured && m.state !== 'closed') {  // the exchange the ticker is talking about: a gold beacon
        const beat = 0.5 + 0.5 * Math.sin(Date.now() / 250);
        ctx.beginPath(); ctx.arc(p.x, p.y, 10 + 8 * beat, 0, 2 * Math.PI);
        ctx.strokeStyle = `rgba(240,185,11,${0.35 + 0.4 * (1 - beat)})`; ctx.lineWidth = 2; ctx.stroke();
        ctx.beginPath(); ctx.arc(p.x, p.y, 7, 0, 2 * Math.PI);
        ctx.fillStyle = 'rgba(240,185,11,0.35)'; ctx.fill();
      } else if (featured) {  // closed: a steady ring, so a headline never looks like trading
        ctx.beginPath(); ctx.arc(p.x, p.y, 10, 0, 2 * Math.PI);
        ctx.strokeStyle = 'rgba(240,185,11,0.6)'; ctx.lineWidth = 1.5; ctx.stroke();
      }
      marker(p.x, p.y, m.kind, m === hover ? 5.5 : 4);
      ctx.globalAlpha = m.state === 'closed' && perf != null ? 0.7 : 1;
      ctx.fillStyle = color; ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = perf != null ? (STATE_COLOR[m.state] || STATE_COLOR.unknown) : (dark ? '#0b0e15' : '#fff');
      ctx.lineWidth = perf != null ? 1.6 : 1; ctx.stroke();
      if ((m.state === 'open' && !crypto) || m.state === 'break' || m === hover || featured) labelled.push({ m, p, perf });
    }
    drawLabels(labelled, g, dark);
    // colour legend
    const lw = 150, lx = g.w - lw - 18, ly = g.h - 30;
    const lg = ctx.createLinearGradient(lx, 0, lx + lw, 0);
    for (let i = 0; i <= 10; i++) lg.addColorStop(i / 10, perfColor(-PERF_SCALE + (2 * PERF_SCALE * i) / 10));
    ctx.fillStyle = lg; ctx.fillRect(lx, ly, lw, 6);
    ctx.font = '10px system-ui, sans-serif'; ctx.fillStyle = dark ? '#9aa0ae' : '#3a3f4b';
    ctx.fillText(`−${PERF_SCALE}%`, lx, ly - 4); ctx.textAlign = 'center'; ctx.fillText('day', lx + lw / 2, ly - 4);
    ctx.textAlign = 'end'; ctx.fillText(`+${PERF_SCALE}%`, lx + lw, ly - 4); ctx.textAlign = 'start';
    ctx.fillText('ring: open · lunch · closed', lx, ly + 18);
    ctx.fillText('● stocks  ■ FX centre  ◆ crypto', lx, ly - 18);

    const stocks = markets.world.filter((m) => (m.kind || 'stock') === 'stock');
    const open = stocks.filter((m) => m.state === 'open').length;
    const brk = stocks.filter((m) => m.state === 'break').length;
    const fxOpen = markets.world.filter((m) => m.kind === 'fx' && m.state === 'open').map((m) => m.city);
    const utc = new Date().toISOString().slice(11, 19);
    const infoHtml = `<b>${open}</b> exchanges open${brk ? `, <b>${brk}</b> at lunch` : ''}
      · FX ${fxOpen.length ? `<b>${esc(fxOpen.join(', '))}</b>` : 'centres closed'} · crypto 24/7 · ${utc} GMT${zoom > 1.01 ? ` · ${zoom >= 10 ? zoom.toFixed(0) : zoom.toFixed(1)}×` : ''}
      <div class="p-muted small">Sun overhead at ${Math.abs(markets.sun.lat).toFixed(1)}°${markets.sun.lat >= 0 ? 'N' : 'S'}
      ${Math.abs(markets.sun.lon).toFixed(1)}°${markets.sun.lon >= 0 ? 'E' : 'W'} · turning in real time
      ${following() && zoom <= 1.01 ? '' : ' · <a href="#" id="globeNoon">recentre</a>'}</div>`;
    if (infoHtml !== lastInfo) { info.innerHTML = infoHtml; lastInfo = infoHtml; }
  }
  let lastInfo = '';

  // Labels for the markets operating now (and the hovered or featured one), by one convention:
  // markers closer than CLUSTER px on screen (a city's exchanges and FX centre, or neighbours at
  // this zoom) share one box: the city (or cities) in bold, coloured by state, then a line per
  // market: its code (the exchange's MIC, or FX), its important indexes joined by " · " (for an
  // FX centre, its currency), and the day's move of the fund that tracks it. A box sits beside its
  // markers where it fits, else further out with a leader line; boxes never overlap.
  const CLUSTER = 26;
  const KIND_ORDER = { stock: 0, fx: 1, crypto: 2 };
  function drawLabels(items, g, dark) {
    const clusters = [];
    for (const it of items) {
      const c = clusters.find((k) => k.some((o) => Math.hypot(o.p.x - it.p.x, o.p.y - it.p.y) < CLUSTER));
      if (c) c.push(it); else clusters.push([it]);
    }
    const boxes = [];
    const text = dark ? '#d1d4dc' : '#131722', muted = dark ? '#9aa0ae' : '#5a606c';
    const stateColor = (s) => (s === 'open' ? (dark ? '#9ee7dc' : '#0b6b60') : s === 'break' ? '#f0a020' : text);
    for (const c of clusters) {
      c.sort((a, b) => (KIND_ORDER[a.m.kind] ?? 3) - (KIND_ORDER[b.m.kind] ?? 3) || a.m.code.localeCompare(b.m.code));
      const cx = c.reduce((s, i) => s + i.p.x, 0) / c.length, cy = c.reduce((s, i) => s + i.p.y, 0) / c.length;
      const title = [...new Set(c.map((i) => i.m.city))].join(' · ');
      const state = c.some((i) => i.m.state === 'open') ? 'open' : c.some((i) => i.m.state === 'break') ? 'break' : c[0].m.state;
      const rows = c.map(({ m, perf }) => ({
        code: m.kind === 'fx' ? 'FX' : m.code, what: m.index || m.name,
        move: perf != null ? `${perf >= 0 ? '+' : ''}${perf.toFixed(2)}%` : '', perf,
      }));
      ctx.font = '600 11px system-ui, sans-serif';
      let w = ctx.measureText(title).width;
      ctx.font = '10px system-ui, sans-serif';
      const codeW = Math.max(...rows.map((r) => ctx.measureText(r.code).width));
      for (const r of rows) w = Math.max(w, codeW + 6 + ctx.measureText(r.what).width + (r.move ? 6 + ctx.measureText(r.move).width : 0));
      w += 10;
      const h = 16 + rows.length * 12 + 2;
      const fits = (x, y) => x >= 2 && y >= 2 && x + w <= g.w - 2 && y + h <= g.h - 2
        && !boxes.some((b) => x < b.x + b.w + 3 && x + w + 3 > b.x && y < b.y + b.h + 3 && y + h + 3 > b.y);
      const near = [[cx + 10, cy - h / 2], [cx - 10 - w, cy - h / 2], [cx - w / 2, cy - 12 - h], [cx - w / 2, cy + 12]];
      const far = [];
      for (let d = 1; d <= 6; d++) {
        for (const [dx, dy] of [[1, 0], [-1, 0], [0, -1], [0, 1], [1, -1], [1, 1], [-1, -1], [-1, 1]]) {
          far.push([cx + dx * (10 + d * 22) - (dx < 0 ? w : dx === 0 ? w / 2 : 0), cy + dy * (8 + d * 16) - (dy < 0 ? h : dy === 0 ? h / 2 : 0)]);
        }
      }
      const spot = [...near, ...far].find(([x, y]) => fits(x, y)) || near[0];
      const [bx, by] = spot;
      boxes.push({ x: bx, y: by, w, h });
      if (!near.includes(spot)) {  // a leader line from the markers to the box
        const tx = Math.max(bx, Math.min(cx, bx + w)), ty = Math.max(by, Math.min(cy, by + h));
        ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(tx, ty);
        ctx.strokeStyle = dark ? 'rgba(160,170,200,0.45)' : 'rgba(40,50,80,0.4)'; ctx.lineWidth = 1; ctx.stroke();
      }
      ctx.fillStyle = dark ? 'rgba(19,23,34,0.82)' : 'rgba(238,240,244,0.92)';
      ctx.fillRect(bx, by, w, h);
      ctx.strokeStyle = stateColor(state); ctx.globalAlpha = 0.45; ctx.lineWidth = 1;
      ctx.strokeRect(bx + 0.5, by + 0.5, w - 1, h - 1); ctx.globalAlpha = 1;
      ctx.font = '600 11px system-ui, sans-serif'; ctx.fillStyle = stateColor(state);
      ctx.fillText(title, bx + 5, by + 12);
      rows.forEach((r, i) => {
        const y = by + 26 + i * 12;
        ctx.font = '600 10px system-ui, sans-serif'; ctx.fillStyle = muted; ctx.fillText(r.code, bx + 5, y);
        ctx.font = '10px system-ui, sans-serif'; ctx.fillStyle = text;
        ctx.fillText(r.what, bx + 5 + codeW + 6, y);
        if (r.move) {
          ctx.fillStyle = perfColor(r.perf);
          ctx.fillText(r.move, bx + 5 + codeW + 6 + ctx.measureText(r.what).width + 6, y);
        }
      });
    }
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

  // ● stock exchange, ■ FX dealing centre, ◆ crypto exchange (path only: the caller fills and strokes)
  function marker(x, y, kind, r) {
    ctx.beginPath();
    if (kind === 'fx') ctx.rect(x - r * 0.9, y - r * 0.9, r * 1.8, r * 1.8);
    else if (kind === 'crypto') { ctx.moveTo(x, y - r * 1.25); ctx.lineTo(x + r * 1.25, y); ctx.lineTo(x, y + r * 1.25); ctx.lineTo(x - r * 1.25, y); ctx.closePath(); }
    else ctx.arc(x, y, r, 0, 2 * Math.PI);
  }

  function renderList() {
    if (!markets || document.body.dataset.view !== 'markets') return;
    const regions = ['Asia-Pacific', 'Europe', 'Middle East & Africa', 'Americas', 'Foreign exchange', 'Crypto'];
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
            <div class="mk-main"><b${m.performance ? ` style="color:${perfColor(m.performance.changePct)}"` : ''}>${esc(m.city)}</b>
              <span class="p-muted small">${esc(m.index)}</span>
              ${m.performance ? `<span class="small" style="color:${perfColor(m.performance.changePct)}" title="Latest daily move: ${esc(m.performance.basis || `${m.performance.proxy}, a US-listed fund tracking this market (USD, ${m.performance.day})`)}">${m.performance.changePct >= 0 ? '+' : ''}${m.performance.changePct.toFixed(2)}% <span class="p-muted">${esc(m.performance.proxy)}</span></span>` : ''}
              <div class="p-muted small"><span${m.performance ? ` style="color:${perfColor(m.performance.changePct)}"` : ''}>${esc(m.name)}</span> · ${esc(localTime(m.timezone))}
                ${m.aroundTheClock ? ' · around the clock' : m.today ? ` · ${m.kind === 'fx' ? 'business hours ' : ''}${esc(m.today.open)}–${esc(m.today.close)}${m.today.break ? ` (lunch ${esc(m.today.break)})` : ''}` : m.state !== 'unknown' ? ` · ${m.kind === 'fx' ? 'not a business day' : 'no session today'}` : ''}</div>
              ${m.holidays?.length ? `<div class="small mk-hol">Holidays: ${m.holidays.map(esc).join(', ')}</div>` : ''}
              ${m.calendarEnds && m.calendarEnds < new Date(Date.now() + 60 * 864e5).toISOString().slice(0, 10)
                ? `<div class="small p-muted">holidays published to ${esc(m.calendarEnds)}</div>` : ''}
              ${m.error ? `<div class="small down">${esc(m.error)}</div>` : ''}
            </div>
            <div class="mk-when small">${m.until ? `${esc(EVENT[m.event] || m.event)}<br><b>${untilText(m.until)}</b>` : ''}</div>
          </div>`).join('')}`;
      }).join('')}`;
  }

  // -- announcements ticker ---------------------------------------------------------------
  function currentItem() { return news.items.length ? news.items[news.i % news.items.length] : null; }

  async function loadNews() {
    try {
      const res = await api('/api/announcements');
      const key = (x) => `${x.kind}|${x.text}`;
      const cur = currentItem();
      news.items = res.items;
      const at = cur ? news.items.findIndex((x) => key(x) === key(cur)) : -1;
      news.i = at >= 0 ? at : 0;
      news.fetchedAt = Date.now();
      renderTicker(false);
    } catch { /* keep the last items */ }
  }

  function ago(iso) {
    const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    return m < 1 ? 'now' : m < 60 ? `${m}m ago` : m < 1440 ? `${Math.round(m / 60)}h ago` : `${Math.round(m / 1440)}d ago`;
  }

  function renderTicker(slide) {
    const it = currentItem();
    if (!it) { ticker.innerHTML = ''; return; }
    const ex = markets?.world.find((m) => m.code === it.exchange);
    const text = esc(it.text) + (it.tickers?.length ? ` <span class="p-muted">${esc(it.tickers.join(' '))}</span>` : '');
    ticker.innerHTML = `<span class="src" title="Source">${esc(it.source)}</span>`
      + (it.url ? `<a class="txt" href="${esc(it.url)}" target="_blank" rel="noopener noreferrer">${text}</a>` : `<span class="txt">${text}</span>`)
      + `<span class="when">${ex ? esc(ex.city) + ' · ' : ''}${it.kind === 'news' ? ago(it.time) : 'now'}</span>`
      + `<span class="count">${(news.i % news.items.length) + 1}/${news.items.length}</span>`;
    ticker.classList.toggle('slide', slide);
    if (slide) { void ticker.offsetWidth; }
  }

  function tick() {
    if (!news.items.length || ticker.matches(':hover')) return;  // hovering holds the item
    if (Date.now() - news.shownAt < TICK_MS) return;
    news.i = (news.i + 1) % news.items.length;
    news.shownAt = Date.now();
    renderTicker(true);
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
    try { canvas.setPointerCapture(e.pointerId); } catch { /* pointer already gone */ }
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
  canvas.addEventListener('dblclick', () => { recentre(); zoom = 1; nightCache = null; zoneCache = null; });
  // Zoom with the wheel or a trackpad pinch, towards the point under the cursor.
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const g = geometry(), r = canvas.getBoundingClientRect();
    const at = unproject(e.clientX - r.left, e.clientY - r.top, g);
    const maxZoom = glr && hires ? 150 : 12;  // ~160 m per pixel at 150x: the GSHHG data's detail
    const next = Math.max(1, Math.min(maxZoom, zoom * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))));
    if (next === zoom) return;
    if (at && next > zoom) {  // turn a little towards the cursor so it stays roughly under it
      const t = 1 - zoom / next;
      const sun = subsolar();
      const lon = view.lon + wrap(at.lon - view.lon) * t, lat = view.lat + (at.lat - view.lat) * t;
      offset = { lon: wrap(lon - sun.lon), lat: Math.max(-85, Math.min(85, lat)) };
      glide = null;
    }
    zoom = next;
    nightCache = null; zoneCache = null;
  }, { passive: false });
  canvas.addEventListener('click', (e) => {
    if (justDragged) { justDragged = false; return; }
    if (sunHit(e)) { recentre(); return; }
    if (!hover) return;
    const row = list.querySelector(`[data-code="${hover.code}"]`);
    if (row) { row.scrollIntoView({ block: 'center', behavior: 'smooth' }); row.classList.add('flash'); setTimeout(() => row.classList.remove('flash'), 1200); }
  });
  info.addEventListener('click', (e) => {
    if (e.target.id === 'globeNoon') { e.preventDefault(); recentre(); zoom = 1; nightCache = null; zoneCache = null; }
  });

  // Animate while visible: the Earth turning, pulses, countdowns every second; market states
  // are re-fetched every 30 s.
  let lastList = 0;
  function frame() {
    if (document.body.dataset.view === 'markets') {
      if (Date.now() - fetchedAt > 30000) load();
      if (Date.now() - news.fetchedAt > 60000) loadNews();
      tick();
      updateView();
      draw();
      if (Date.now() - lastList > 1000) { renderList(); lastList = Date.now(); }
    }
    requestAnimationFrame(frame);
  }
  window.addEventListener('massive:view', (e) => { if (e.detail.view === 'markets') { load(); if (!topo) loadTopo(); } });
  window.addEventListener('resize', () => { nightCache = null; draw(); });
  // From the News view: feature an exchange (turn the globe to it and light it).
  window.addEventListener('massive:feature-exchange', (e) => {
    const m = markets?.world.find((x) => x.code === e.detail);
    const at = news.items.findIndex((x) => x.exchange === e.detail);
    if (at >= 0) { news.i = at; news.shownAt = Date.now(); renderTicker(true); }
    if (m) { offset = { lon: wrap(m.lon - subsolar().lon), lat: Math.max(-60, Math.min(60, m.lat)) }; }
  });
  window.Globe = { state: () => ({ view: { ...view }, offset: { ...offset }, sun: subsolar(), following: following() }), recentre, perfColor,
    // Turn the globe to a place (it keeps turning with the Earth from there) and zoom in.
    lookAt(lon, lat, z = zoom) {
      glide = null;
      offset = { lon: wrap(lon - subsolar().lon), lat: Math.max(-85, Math.min(85, lat)) };
      zoom = Math.max(1, Math.min(glr && hires ? 150 : 12, z));
      nightCache = null; zoneCache = null;
    } };
  requestAnimationFrame(frame);
  loadSky();
  if (glr) loadHiresIndex();
  loadZones();
  if (document.body.dataset.view === 'markets') { load(); loadTopo(); }
})();
