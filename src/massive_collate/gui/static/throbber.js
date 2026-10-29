'use strict';
/* The open symbol's logo as a throbber, after Internet Explorer 3's: its logo held still, and half a
   globe turning round it in its place (the user, 2026-10-28: "use the old school explorer icon + the
   current symbol icon scheme"; "keep the status on the bottom, but lets animate the current symbol icon";
   "i need the appearance of a sphere rotating"; "its gotta look just as smooth"; "blue and gold like, but
   make sure that it centers on the current time zone so that it fades on both sides of the user's
   location"; "use night and day, to reflect the current time"; "like night globe with cities lights or
   day globe like the IE 3 one"; "the symbol icon doesn't really move, its more or less the globe rotating
   in its place"; "the globe when flying by didn't completely obscure the icon, it should"; "half icon
   time half globe moving time"; "the icon needs to show both at the same time, its half a globe rotating
   over the icon, and the leading and trailing part are faded").

   Wherever the logo shows (the symbol button at the top left, the Stock rail button), while the symbol
   updates the logo stays and half a globe, a shell of the Earth's half centred on the picked time zone's
   meridian (homeLon), turns round it once every TURN_MS as the Earth turns: from behind the logo it comes
   round over it from the left, covers it whole when it faces us (the zone's meridian in the middle), and
   goes round behind it to the right, so the logo and the globe show together, the globe over half of
   it on the whole. Its leading and trailing edges fade (EDGE_FADE). By day in Explorer's style, blue
   oceans and gold land, glossy (drawn here from the Markets view's map: Explorer's own picture is
   Microsoft's); by night where the Sun is down now, dark, its cities lit; lit from the front so it is
   brightest in the middle. Every screen frame is drawn pixel by pixel with smooth edges, the angle from
   the time. It starts and stops with the half-globe behind the logo: when the update is done the turn
   finishes first. Updating: its fetch queued or under way (the status bar's job, activity.js), or new
   bars just in (pulse: app.js, each stream message or poll that brought some; at least one whole turn).
   With reduced motion asked for nothing turns: a ring. */
(() => {
  const TURN_MS = 2300;          // one turn, as Explorer's loop (the user's reference: tenor.com, 2.3 s)
  const EDGE_FADE = 35 * Math.PI / 180;  // the half-globe's leading and trailing edges fade over this much of it
  // By day Explorer's globe, blue and gold: deep royal-blue oceans, gold land, a white gloss. By night
  // (where the Sun is down now: "use night and day, to reflect the current time") dark, its cities lit.
  const OCEAN = [16, 52, 150], LAND = [226, 176, 62], ICE = [246, 226, 168];
  const NIGHT_OCEAN = [4, 9, 26], NIGHT_LAND = [22, 20, 18], CITY = [255, 206, 120];
  const TWILIGHT = 0.1;          // the Sun's height (sine) over which day turns to night: about 6 degrees either side
  const RAD = Math.PI / 180;
  // The world's large cities, for the night side's lights: [lat, lon, people in millions], rounded.
  const CITIES = [
    [35.7, 139.7, 37], [28.6, 77.2, 32], [31.2, 121.5, 29], [23.8, 90.4, 23], [-23.6, -46.6, 22], [19.4, -99.1, 22],
    [30.0, 31.2, 22], [39.9, 116.4, 21], [19.1, 72.9, 21], [34.7, 135.5, 19], [29.6, 106.6, 17], [24.9, 67.0, 17],
    [40.7, -74.0, 19], [-34.6, -58.4, 15], [41.0, 29.0, 16], [22.6, 88.4, 15], [6.5, 3.4, 15], [14.6, 121.0, 14],
    [23.1, 113.3, 14], [-22.9, -43.2, 13], [39.1, 117.2, 14], [-4.3, 15.3, 16], [34.1, -118.2, 13], [55.8, 37.6, 13],
    [22.5, 114.1, 13], [31.5, 74.3, 13], [13.0, 77.6, 13], [48.9, 2.4, 11], [4.7, -74.1, 11], [-6.2, 106.8, 11],
    [13.1, 80.3, 11], [-12.0, -77.0, 11], [13.8, 100.5, 11], [37.6, 127.0, 10], [35.2, 136.9, 9], [17.4, 78.5, 10],
    [51.5, -0.1, 9], [35.7, 51.4, 9], [41.9, -87.6, 9], [30.7, 104.1, 9], [32.1, 118.8, 9], [30.6, 114.3, 8],
    [10.8, 106.7, 9], [-8.8, 13.2, 9], [23.0, 72.6, 8], [3.1, 101.7, 8], [34.3, 108.9, 8], [22.3, 114.2, 7.5],
    [30.3, 120.2, 8], [41.8, 123.4, 8], [24.7, 46.7, 7.5], [33.3, 44.4, 7.5], [-33.4, -70.6, 7], [21.2, 72.8, 7.5],
    [40.4, -3.7, 6.7], [18.5, 73.9, 7], [45.8, 126.6, 6.5], [29.8, -95.4, 6.5], [32.8, -96.8, 6.5], [43.7, -79.4, 6.3],
    [-6.8, 39.3, 7], [25.8, -80.2, 6.2], [-19.9, -43.9, 6.1], [1.4, 103.8, 6], [40.0, -75.2, 5.8], [33.7, -84.4, 6.1],
    [33.6, 130.4, 5.5], [15.6, 32.5, 6], [41.4, 2.2, 5.6], [-26.2, 28.0, 6], [59.9, 30.3, 5.5], [36.1, 120.4, 6],
    [38.9, 121.6, 5], [38.9, -77.0, 5.4], [16.8, 96.2, 5.5], [31.2, 29.9, 5.4], [36.7, 117.0, 5], [20.7, -103.3, 5.3],
    [42.4, -71.1, 4.9], [37.8, -122.4, 4.7], [47.6, -122.3, 4], [33.4, -112.1, 4.9], [52.5, 13.4, 3.6], [41.9, 12.5, 4.3],
    [45.5, 9.2, 3.2], [50.1, 8.7, 2.7], [52.4, 4.9, 2.5], [-33.9, 151.2, 5.3], [-37.8, 145.0, 5.1], [25.3, 55.3, 3.5],
    [45.5, -73.6, 4.3], [49.3, -123.1, 2.7], [39.7, -105.0, 3], [44.98, -93.3, 3.7], [53.5, -2.2, 2.8], [50.4, 30.5, 3],
    [52.2, 21.0, 3.1], [48.2, 16.4, 2], [-1.3, 36.8, 5], [5.6, -0.2, 4.5], [9.0, 38.7, 5.2], [-15.8, -47.9, 4.8],
  ];
  // lit from the front, a little above: brightest at the middle (the user's place), fading to both sides
  const LIGHT = (() => { const l = [0, 0.22, 1], m = Math.hypot(...l); return l.map((c) => c / m); })();
  const HALF = (() => { const h = [LIGHT[0], LIGHT[1], LIGHT[2] + 1], m = Math.hypot(...h); return h.map((c) => c / m); })();
  const FETCHING = new Set(['waiting', 'downloading']);  // the symbol's fetch queued or under way
  const still = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const hosts = new Map();       // host element -> { size, logo: {key, canvas}, globe: canvas }
  const views = new Map();       // n -> each disc pixel's place on the sphere and its light (they do not turn)
  let land = null;               // the Earth: land cover, 0..1, on a 256 x 128 longitude/latitude grid
  let lights = null;             // its cities' light at night, 0..1, on the same grid
  let pulseUntil = 0;
  let turning = false, frame = 0, began = 0, held = 0;  // held: the last TURN_MS it was updating in

  // The picked time zone's standard meridian (degrees): its offset from UTC out of daylight saving, 15
  // degrees an hour (Los Angeles: -120). The Earth's half is centred on it.
  const homes = new Map();
  function homeLon() {
    const zone = preview?.zone || (typeof pickedZone === 'function' ? pickedZone() : 'America/New_York');
    if (!homes.has(zone)) {
      const y = new Date().getUTCFullYear();
      const off = typeof zoneOffset === 'function'
        ? Math.min(zoneOffset(zone, new Date(Date.UTC(y, 0, 1))), zoneOffset(zone, new Date(Date.UTC(y, 6, 1)))) : -300;
      homes.set(zone, off / 4);
    }
    return homes.get(zone);
  }

  // -- the Earth: land from the Markets view's map (world-atlas), on a longitude/latitude grid ----------
  const LW = 256, LH = 128;
  async function loadLand() {
    try {
      const topo = await (await fetch('vendor/world/countries-50m.json')).json();
      const [kx, ky] = topo.transform.scale, [dx, dy] = topo.transform.translate;
      const arcs = topo.arcs.map((arc) => { let x = 0, y = 0; return arc.map(([ax, ay]) => { x += ax; y += ay; return [x * kx + dx, y * ky + dy]; }); });
      const ring = (ids) => {
        const pts = [];
        for (const a of ids) {
          const arc = a >= 0 ? arcs[a] : arcs[~a].slice().reverse();
          pts.push(...(pts.length ? arc.slice(1) : arc));
        }
        return pts;
      };
      const W = LW * 4, H = LH * 4, c = document.createElement('canvas');
      c.width = W; c.height = H;
      const g = c.getContext('2d');
      g.fillStyle = '#fff';
      const polygons = (topo.objects.land?.geometries || []).flatMap((geo) =>
        geo.type === 'Polygon' ? [geo.arcs] : geo.type === 'MultiPolygon' ? geo.arcs : []);
      for (const shift of [-W, 0, W]) {  // rings across the 180th meridian, unwrapped, drawn either side
        g.beginPath();
        for (const poly of polygons) {
          for (const ids of poly) {
            let off = 0, prev = null;
            ring(ids).forEach(([lon, lat], i) => {
              if (prev != null && Math.abs(lon - prev) > 180) off -= Math.sign(lon - prev) * 360;
              prev = lon;
              const x = ((lon + off + 180) / 360) * W + shift, y = ((90 - lat) / 180) * H;
              if (i) g.lineTo(x, y); else g.moveTo(x, y);
            });
            g.closePath();
          }
        }
        g.fill('evenodd');
      }
      g.fillRect(0, (175 / 180) * H, W, H);  // the pole Antarctica's ring goes round
      const px = g.getImageData(0, 0, W, H).data;
      land = new Float32Array(LW * LH);  // each cell the share of its 4 x 4 pixels that are land
      for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) land[(y >> 2) * LW + (x >> 2)] += px[(y * W + x) * 4 + 3] / 255 / 16;
    } catch { land = new Float32Array(LW * LH); }  // no map: an ocean globe
  }
  loadLand();

  // The cities' light at night: each a soft spot, brighter and wider the more people live there.
  lights = (() => {
    const grid = new Float32Array(LW * LH);
    for (const [lat, lon, people] of CITIES) {
      const cx = (lon + 180) / 360 * LW, cy = (90 - lat) / 180 * LH, s = 0.7 + 0.12 * Math.sqrt(people), peak = Math.min(1, 0.35 + people / 30);
      for (let dy = -3; dy <= 3; dy++) {
        for (let dx = -3; dx <= 3; dx++) {
          const x = Math.floor(cx) + dx, y = Math.floor(cy) + dy;
          if (y < 0 || y >= LH) continue;
          const dist2 = (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2;
          const k = ((x % LW) + LW) % LW + y * LW;
          grid[k] = Math.min(1, grid[k] + peak * Math.exp(-dist2 / (2 * s * s)));
        }
      }
    }
    return grid;
  })();

  // A grid's value at (lon, lat) in degrees, bilinear between its cells.
  function gridAt(grid, lon, lat) {
    const fx = ((((lon + 180) % 360) + 360) % 360) / 360 * LW - 0.5, fy = Math.min(LH - 1, Math.max(0, (90 - lat) / 180 * LH - 0.5));
    const x0 = Math.floor(fx), y0 = Math.floor(fy), tx = fx - x0, ty = fy - y0;
    const xa = ((x0 % LW) + LW) % LW, xb = (xa + 1) % LW, y1 = Math.min(LH - 1, y0 + 1);
    return (grid[y0 * LW + xa] * (1 - tx) + grid[y0 * LW + xb] * tx) * (1 - ty)
         + (grid[y1 * LW + xa] * (1 - tx) + grid[y1 * LW + xb] * tx) * ty;
  }

  // -- the view: each disc pixel's place on the sphere and its light, for a size (they do not turn) -----
  function view(n) {
    if (views.has(n)) return views.get(n);
    const r = n / 2, cells = [];
    for (let j = 0; j < n; j++) {
      for (let i = 0; i < n; i++) {
        const x = (i + 0.5 - r) / r, y = (r - j - 0.5) / r, rr = x * x + y * y;
        const edge = Math.min(1, Math.max(0, (1 - Math.sqrt(rr)) * r + 0.5));  // the limb's coverage: smooth
        if (edge <= 0) continue;
        const k = Math.min(1, rr), z = Math.sqrt(1 - k), cl = Math.sqrt(1 - y * y);
        const diffuse = Math.max(0, x * LIGHT[0] + y * LIGHT[1] + z * LIGHT[2]);
        const spec = Math.pow(Math.max(0, x * HALF[0] + y * HALF[1] + z * HALF[2]), 28);
        cells.push({ o: (j * n + i) * 4, lon: Math.atan2(x, z), lat: Math.asin(Math.max(-1, Math.min(1, y))) * 180 / Math.PI,
                     sinLat: y, cl, v: (1 - y) / 2, edge, light: 0.38 + 0.78 * diffuse, spec, rim: Math.pow(1 - z, 3) });
      }
    }
    views.set(n, cells);
    return cells;
  }

  // -- the logo, flat, at n x n device pixels (the image once it has loaded, or the lettered badge) ------
  function logoCanvas(front, n) {
    const c = document.createElement('canvas');
    c.width = c.height = n;
    const g = c.getContext('2d');
    const badge = front.querySelector('.sym-icon');
    const img = front.querySelector('img.sym-img');
    g.beginPath(); g.arc(n / 2, n / 2, n / 2, 0, 2 * Math.PI); g.clip();
    if (img?.complete && img.naturalWidth) {
      g.drawImage(img, 0, 0, n, n);
    } else if (badge) {
      const cs = getComputedStyle(badge);
      g.fillStyle = cs.backgroundColor && cs.backgroundColor !== 'rgba(0, 0, 0, 0)' ? cs.backgroundColor : (badge.dataset.color || '#2962ff');
      g.fillRect(0, 0, n, n);
      g.fillStyle = cs.color || '#fff';
      g.font = `${cs.fontWeight || 600} ${Math.round(n * 0.45)}px ${cs.fontFamily || 'system-ui, sans-serif'}`;
      g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText((badge.textContent || badge.dataset.letter || '').trim(), n / 2, n / 2 + n * 0.03);
    }
    return c;
  }

  // One frame: the logo, flat, and over it the half-globe turned `v` radians from facing us (0: its middle,
  // the time zone's meridian, in front; ±π: behind the logo, out of sight).
  function drawSphere(canvas, entry, front, v) {
    const dpr = Math.min(4, Math.max(1, window.devicePixelRatio || 1));
    const n = Math.round(entry.size * dpr);
    if (canvas.width !== n || canvas.height !== n) { canvas.width = canvas.height = n; canvas.style.width = canvas.style.height = `${entry.size}px`; }
    const key = `${n}|${front.dataset.html}|${front.querySelector('img.sym-img')?.complete ? 1 : 0}`;
    if (entry.logo?.key !== key) entry.logo = { key, canvas: logoCanvas(front, n) };
    const g = canvas.getContext('2d');
    g.clearRect(0, 0, n, n);
    g.drawImage(entry.logo.canvas, 0, 0);  // the logo, still, under it
    if (entry.globe?.width !== n) { entry.globe = document.createElement('canvas'); entry.globe.width = entry.globe.height = n; }
    const eg = entry.globe.getContext('2d');
    const img = eg.createImageData(n, n), px = img.data;
    const home = homeLon();                // its middle the picked time zone's meridian
    const sun = sunNow();                  // where the Sun is overhead now: day and night
    const col = [0, 0, 0];
    for (const c of view(n)) {
      let p = (v + c.lon) % (2 * Math.PI);  // the pixel's place on the half-globe, from its middle
      if (p > Math.PI) p -= 2 * Math.PI; else if (p < -Math.PI) p += 2 * Math.PI;
      const k = Math.min(1, (Math.PI / 2 - Math.abs(p)) / EDGE_FADE);
      if (k <= 0) continue;                 // the other half: the logo shows
      const cover = k * k * (3 - 2 * k);    // its leading and trailing edges faded
      const lon = home + (p * 180) / Math.PI;
      const l = land ? gridAt(land, lon, c.lat) : 0;
      const ice = Math.abs(c.lat) > 72 ? Math.min(1, (Math.abs(c.lat) - 72) / 8) : 0;
      // the Sun's height here (the sine of its elevation): day above, night below, twilight between
      const h = c.sinLat * sun.sinLat + c.cl * sun.cosLat * Math.cos((lon - sun.lon) * RAD);
      const day = Math.min(1, Math.max(0, (h + TWILIGHT) / (2 * TWILIGHT)));
      for (let ch = 0; ch < 3; ch++) {
        const lit = (OCEAN[ch] * (1 - l) + LAND[ch] * l) * (1 - ice) + ICE[ch] * ice;  // by day: blue and gold
        const dark = NIGHT_OCEAN[ch] * (1 - l) + NIGHT_LAND[ch] * l;
        col[ch] = lit * day + dark * (1 - day);
      }
      const shine = c.spec * (0.75 - 0.5 * l) * day;            // the ocean glossier than the land, by day
      const glow = (1 - day) * gridAt(lights, lon, c.lat);      // its cities lit at night
      const o = c.o;
      for (let ch = 0; ch < 3; ch++) {
        const atmosphere = ch === 2 ? 120 : ch === 1 ? 70 : 30;  // a blue haze at the limb
        px[o + ch] = Math.min(255, col[ch] * c.light + 255 * shine + atmosphere * c.rim * 0.6 + CITY[ch] * glow);
      }
      px[o + 3] = Math.round(255 * c.edge * cover);
    }
    eg.putImageData(img, 0, 0);
    g.drawImage(entry.globe, 0, 0);  // over the logo: where it is whole, it hides it
  }

  // Where the Sun is overhead now (globeicon.js: noon's longitude, today's declination), or at a preview's time.
  function sunNow() {
    const t = preview?.at || new Date();
    const s = window.GlobeIcon?.sun?.(t) || { lon: -15 * (t.getUTCHours() + t.getUTCMinutes() / 60 - 12), lat: 0 };
    return { lon: s.lon, sinLat: Math.sin(s.lat * RAD), cosLat: Math.cos(s.lat * RAD) };
  }

  // A look at it (the demos the user asked for: a zone at a time): { at: Date, zone, turn: 0..1 (0: its
  // middle the zone's meridian) } holds every logo in front of the globe at that turn; null lets it go.
  let preview = null;
  function setPreview(p) {
    preview = p || null;
    if (preview) {
      for (const [host, entry] of hosts) {
        const box = host.querySelector(':scope > .throb');
        if (box) drawSphere(box.querySelector('.throb-sphere'), entry, box.querySelector('.throb-front'), -2 * Math.PI * (preview.turn ?? 0));
      }
      show(true);
    } else if (!turning) show(false);
  }

  // -- the hosts ----------------------------------------------------------------------------------------
  // The logo in `host`, built once; written only when it changes (the rail and the button are drawn again
  // on every activity poll: writing them would start the turn over each time).
  function face(host, html, size) {
    if (typeof host === 'string') host = document.getElementById(host);
    if (!host) return;
    let box = host.querySelector(':scope > .throb');
    if (!box) {
      host.innerHTML = `<span class="throb" style="--throb-size:${size}px"><span class="throb-front"></span>`
        + '<canvas class="throb-sphere" aria-hidden="true"></canvas></span>';
      box = host.querySelector(':scope > .throb');
    }
    const entry = hosts.get(host) || { size, logo: null };
    entry.size = size;
    hosts.set(host, entry);
    const front = box.querySelector('.throb-front');
    if (front.dataset.html !== html) { front.innerHTML = html; front.dataset.html = html; entry.logo = null; }
    watch();
  }

  // New bars in for the open symbol: at least one whole turn from now.
  function pulse() { pulseUntil = Math.max(pulseUntil, Date.now() + TURN_MS); watch(); }

  function busy() {
    if (typeof state === 'undefined') return false;  // app.js not run yet: the page still loading
    const job = window.Activity?.job;
    const fetching = !!job && job.ticker === state.ticker && FETCHING.has(job.phase);
    return !!state.ticker && (fetching || Date.now() < pulseUntil);
  }

  function show(sphere) {
    for (const host of hosts.keys()) {
      host.querySelector(':scope > .throb')?.classList.toggle('updating', sphere);
      host.setAttribute('aria-busy', String(sphere));
    }
  }

  // Every screen frame while it runs: the first half of each TURN_MS the globe over the logo, the second
  // the logo; the phase from the time, so it is as smooth as the screen draws.
  function draw(now) {
    frame = 0;
    if (preview) { turning = false; return; }  // a look at it holds its frame
    if (document.hidden) { turning = false; show(false); return; }
    const run = Math.max(0, now - began);  // a frame's time can be a little before the start
    const cycle = Math.floor(run / TURN_MS), t = (run % TURN_MS) / TURN_MS;
    if (busy()) held = cycle;
    else if (cycle > held) { turning = false; show(false); return; }  // done: a turn ended, the half-globe behind the logo
    // one turn: from behind the logo round over it (its middle, the zone's meridian, in front halfway) and
    // behind again; the Earth turns eastward: the surface moves left to right
    const v = Math.PI * (1 - 2 * t);
    for (const [host, entry] of hosts) {
      if (!host.isConnected) { hosts.delete(host); continue; }
      const box = host.querySelector(':scope > .throb');
      if (box) drawSphere(box.querySelector('.throb-sphere'), entry, box.querySelector('.throb-front'), v);
    }
    show(true);
    frame = requestAnimationFrame(draw);
  }

  // Started when it is updating (looked at on each pulse, face and a slow check of the fetch).
  function watch() {
    const on = busy();
    if (still?.matches) {  // nothing turns: a ring while it updates
      for (const host of hosts.keys()) host.querySelector(':scope > .throb')?.classList.toggle('ringed', on);
      return;
    }
    if (on && !turning && !document.hidden && !preview) {
      turning = true;
      began = performance.now();
      held = 0;
      frame = requestAnimationFrame(draw);
    }
  }
  setInterval(watch, 250);

  window.Throbber = { face, pulse, busy, preview: setPreview };
})();
