// Icons shared by the chart app: a symbol's logo (or a lettered badge, like TradingView), the
// data-state icon (fetching / backfilling / queued / new data / up to date) and the right rail's
// tool icons. Plain functions returning HTML strings; all text is escaped.
(() => {
  'use strict';

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // A link from a feed (a headline's URL) only if it is a web address: never javascript: or data:.
  const safeUrl = (u) => (/^https?:\/\//i.test(String(u ?? '').trim()) ? String(u).trim() : '');

  // Stable colour per ticker for lettered badges.
  const BADGE = ['#2962ff', '#00897b', '#7b1fa2', '#ef6c00', '#c2185b', '#455a64', '#5d4037', '#1565c0', '#2e7d32', '#ad1457'];
  function badgeColor(ticker) {
    let h = 0;
    for (const ch of ticker) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return BADGE[h % BADGE.length];
  }
  const KIND_GLYPH = { future: '⧗', fx: '¤', crypto: '₿', index: 'Σ' };

  // `sym`: { ticker, icon (bool), asset }. Size in px.
  function symbolIcon(sym, size = 20) {
    const t = sym?.ticker || '?';
    const style = `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.45)}px`;
    if (sym?.icon) {
      return `<span class="sym-icon" style="${style}" data-letter="${esc(t[0])}" data-color="${badgeColor(t)}"><img
        class="sym-img" src="/api/icon?ticker=${encodeURIComponent(t)}" alt="" loading="lazy"></span>`;
    }
    const glyph = KIND_GLYPH[sym?.asset] || t.replace(/[^A-Z0-9]/gi, '').slice(0, t.length > 3 ? 1 : 2) || '?';
    return `<span class="sym-icon letter" style="${style};background:${badgeColor(t)}" aria-hidden="true">${esc(glyph)}</span>`;
  }

  const SVG = (body, cls = '', size = 16) =>
    `<svg class="ico ${cls}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

  const PATHS = {
    sync: '<path d="M21 12a9 9 0 0 1-15.5 6.2L3 16"/><path d="M3 12a9 9 0 0 1 15.5-6.2L21 8"/><path d="M21 3v5h-5"/><path d="M3 21v-5h5"/>',
    check: '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.8L16.5 9.5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    download: '<path d="M12 4v11"/><path d="m7 10 5 5 5-5"/><path d="M5 20h14"/>',
    database: '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13"/><path d="M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
    analysis: '<path d="M3 3v18h18"/><path d="m7 15 4-5 3 3 5-7"/><path d="M15 6h4v4"/>',
    // the Analysis view: the definite integral, typeset: a calligraphic sign with ball terminals (an outline
    // drawn thick through the middle, thin at the ends), its limits and dx in an italic math serif
    integral: '<g fill="currentColor" stroke="none"><path d="M12.99 4.32 L12.97 4.27 L12.95 4.19 L12.92 4.1 L12.88 4 L12.85 3.88 L12.8 3.76 L12.76 3.63 L12.71 3.5 L12.66 3.37 L12.6 3.24 L12.53 3.12 L12.46 3 L12.38 2.89 L12.3 2.8 L12.2 2.72 L12.1 2.65 L12 2.58 L11.89 2.52 L11.77 2.46 L11.66 2.41 L11.54 2.36 L11.42 2.31 L11.3 2.28 L11.17 2.25 L11.05 2.23 L10.93 2.22 L10.8 2.22 L10.68 2.23 L10.57 2.26 L10.45 2.3 L10.35 2.36 L10.25 2.42 L10.15 2.49 L10.06 2.57 L9.97 2.65 L9.88 2.74 L9.8 2.84 L9.72 2.94 L9.64 3.04 L9.57 3.15 L9.49 3.25 L9.42 3.37 L9.35 3.49 L9.29 3.61 L9.22 3.73 L9.16 3.86 L9.11 3.99 L9.05 4.13 L9 4.27 L8.95 4.42 L8.89 4.57 L8.84 4.73 L8.78 4.9 L8.72 5.08 L8.66 5.27 L8.6 5.47 L8.53 5.69 L8.45 5.93 L8.38 6.18 L8.3 6.45 L8.22 6.73 L8.15 7.01 L8.07 7.3 L7.99 7.6 L7.92 7.89 L7.85 8.19 L7.78 8.49 L7.71 8.78 L7.65 9.06 L7.59 9.34 L7.53 9.62 L7.48 9.89 L7.42 10.17 L7.38 10.44 L7.33 10.71 L7.29 10.98 L7.25 11.25 L7.21 11.52 L7.17 11.8 L7.13 12.07 L7.1 12.34 L7.07 12.61 L7.04 12.88 L7.01 13.15 L6.99 13.43 L6.96 13.72 L6.94 14.01 L6.92 14.3 L6.9 14.59 L6.88 14.88 L6.87 15.17 L6.86 15.46 L6.84 15.73 L6.83 16 L6.82 16.27 L6.81 16.51 L6.79 16.75 L6.78 16.97 L6.77 17.18 L6.75 17.38 L6.74 17.57 L6.73 17.75 L6.71 17.92 L6.7 18.08 L6.69 18.23 L6.67 18.38 L6.65 18.52 L6.63 18.65 L6.61 18.78 L6.58 18.91 L6.55 19.05 L6.52 19.18 L6.48 19.31 L6.44 19.45 L6.4 19.58 L6.35 19.72 L6.31 19.85 L6.26 19.97 L6.2 20.09 L6.15 20.21 L6.09 20.32 L6.03 20.42 L5.97 20.51 L5.91 20.6 L5.85 20.67 L5.78 20.74 L5.72 20.8 L5.64 20.86 L5.56 20.91 L5.47 20.96 L5.38 21 L5.29 21.04 L5.19 21.07 L5.1 21.09 L5.01 21.1 L4.92 21.11 L4.83 21.11 L4.76 21.1 L4.69 21.08 L4.64 21.06 L4.6 21.03 L4.55 20.99 L4.49 20.92 L4.43 20.84 L4.37 20.74 L4.31 20.64 L4.25 20.53 L4.2 20.41 L4.14 20.3 L4.09 20.19 L4.05 20.09 L4.01 19.99 L3.97 19.91 L3.93 19.85 L3.57 20.05 L3.59 20.1 L3.63 20.17 L3.67 20.26 L3.71 20.36 L3.76 20.47 L3.82 20.59 L3.87 20.71 L3.94 20.84 L4.01 20.96 L4.08 21.07 L4.16 21.18 L4.25 21.28 L4.35 21.37 L4.46 21.44 L4.57 21.48 L4.68 21.51 L4.8 21.52 L4.92 21.53 L5.04 21.52 L5.16 21.51 L5.28 21.49 L5.41 21.47 L5.53 21.43 L5.65 21.39 L5.77 21.35 L5.89 21.29 L6.01 21.23 L6.12 21.16 L6.22 21.08 L6.33 20.99 L6.43 20.9 L6.52 20.8 L6.62 20.69 L6.71 20.57 L6.8 20.45 L6.89 20.33 L6.98 20.2 L7.06 20.07 L7.15 19.93 L7.23 19.8 L7.3 19.66 L7.38 19.52 L7.45 19.38 L7.52 19.24 L7.59 19.09 L7.65 18.95 L7.71 18.8 L7.76 18.65 L7.82 18.49 L7.88 18.33 L7.93 18.17 L7.98 18 L8.04 17.82 L8.1 17.63 L8.16 17.44 L8.22 17.23 L8.28 17 L8.35 16.76 L8.42 16.51 L8.48 16.25 L8.55 15.98 L8.62 15.7 L8.68 15.42 L8.74 15.14 L8.81 14.85 L8.87 14.56 L8.93 14.28 L8.98 14 L9.04 13.72 L9.09 13.45 L9.14 13.18 L9.18 12.91 L9.23 12.64 L9.27 12.37 L9.32 12.1 L9.35 11.83 L9.39 11.56 L9.43 11.29 L9.46 11.02 L9.5 10.75 L9.53 10.48 L9.56 10.2 L9.59 9.93 L9.61 9.66 L9.64 9.38 L9.67 9.08 L9.69 8.79 L9.72 8.49 L9.74 8.18 L9.76 7.88 L9.78 7.58 L9.8 7.28 L9.82 7 L9.84 6.72 L9.85 6.45 L9.87 6.19 L9.89 5.95 L9.9 5.73 L9.92 5.52 L9.94 5.32 L9.95 5.14 L9.97 4.97 L9.98 4.81 L10 4.66 L10.01 4.52 L10.03 4.39 L10.05 4.27 L10.07 4.16 L10.09 4.05 L10.12 3.94 L10.15 3.84 L10.18 3.73 L10.22 3.63 L10.26 3.52 L10.3 3.43 L10.34 3.33 L10.38 3.24 L10.43 3.16 L10.48 3.09 L10.53 3.02 L10.57 2.96 L10.62 2.9 L10.67 2.86 L10.72 2.82 L10.77 2.79 L10.82 2.77 L10.87 2.75 L10.94 2.74 L11.02 2.73 L11.11 2.73 L11.2 2.74 L11.3 2.76 L11.4 2.78 L11.5 2.81 L11.6 2.85 L11.69 2.89 L11.78 2.94 L11.87 2.99 L11.94 3.05 L12 3.1 L12.06 3.16 L12.11 3.23 L12.17 3.32 L12.22 3.43 L12.27 3.54 L12.32 3.65 L12.37 3.78 L12.41 3.9 L12.45 4.01 L12.48 4.13 L12.52 4.23 L12.55 4.33 L12.58 4.41 L12.61 4.48Z"/><circle cx="12.85" cy="4.5" r="1.2"/><circle cx="3.7" cy="19.85" r="1.2"/><text x="14.6" y="6.6" font-size="6.6" font-family=\"Cambria Math, STIX Two Math, Latin Modern Math, Times New Roman, serif\" font-style=\"italic\">b</text><text x="8.7" y="23.2" font-size="6.6" font-family=\"Cambria Math, STIX Two Math, Latin Modern Math, Times New Roman, serif\" font-style=\"italic\">a</text><text x="13.6" y="15.2" font-size="8.6" font-family=\"Cambria Math, STIX Two Math, Latin Modern Math, Times New Roman, serif\" font-style=\"italic\">dx</text></g>',
    // the Fetcher: a disk and its read/write arm (the platter turns while a run fetches: style.css)
    disk: '<g stroke-width="1.5"><g class="disk-platter"><circle cx="11" cy="11" r="8.5"/><path stroke-width="1.1" d="M5.36 8.95A6 6 0 0 1 12.55 5.20M16.64 13.05A6 6 0 0 1 9.45 16.80M9.15 14.20A3.7 3.7 0 0 1 7.97 8.88M12.85 7.80A3.7 3.7 0 0 1 14.03 13.12"/><circle cx="11" cy="11" r="1.3"/></g><g class="disk-arm"><circle cx="20.3" cy="20.3" r="1.6"/><path d="M19.2 19.1 14.2 15.2"/><path stroke-width="2" d="m13.2 16.3 2-2.2"/></g></g>',
    // the Gov view: a state house (its dome and lantern, the columns of its portico, its steps)
    statehouse: '<g stroke-width="1.5"><path d="M12 1.8v1.6"/><path d="M11 3.4h2v2.1h-2z"/><path d="M7.6 10a4.4 4.6 0 0 1 8.8 0"/><path d="M6.8 10h10.4v1.8H6.8z"/><path d="M3.5 13.2 12 11.8l8.5 1.4"/><path d="M4.2 13.2v6.1M19.8 13.2v6.1"/><path stroke-width="1.3" d="M7.4 14.6v4.7M10.5 14.6v4.7M13.5 14.6v4.7M16.6 14.6v4.7"/><path d="M3 19.3h18M2.2 21.4h19.6"/></g>',
    // the News view: a thermometer (the markets' temperature)
    thermometer: '<g stroke-width="1.5"><path d="M14 14.6V4.75a2 2 0 0 0-4 0v9.85a4 4 0 1 0 4 0z"/><path stroke-width="2" d="M12 8v8.3"/><circle cx="12" cy="17.75" r="1.7" fill="currentColor" stroke="none"/><path stroke-width="1.1" d="M16 5.5h2.2M16 8.25h1.4M16 11h2.2M16 13.75h1.4"/></g>',
    // the Datasets view (research series): a scroll
    scroll: '<g stroke-width="1.5"><path d="M5 3h12.5a2 2 0 0 1 0 4H5a2 2 0 0 1 0-4z"/><path d="M5 4.1a.9.9 0 1 0 0 1.8"/><path d="M6.5 7v10.5M17.5 7v10.5"/><path d="M6.5 17h12.5a2 2 0 0 1 0 4H6.5a2 2 0 0 1 0-4z"/><path d="M19 18.1a.9.9 0 1 1 0 1.8"/><path d="M9.25 10h5.5M9.25 12.5h5.5M9.25 15h3.5"/></g>',
    // the Trading view: two hands shaking
    trading: '<g stroke-width="1.5"><path d="M2 7.5h2.6v9H2z"/><path d="M22 7.5h-2.6v9H22z"/><path d="M19.4 9.2c-1.6-.9-3.4-1.4-5.2-1-1.3.3-2.6 1-3.6 1.9"/><path d="M4.6 9.6c1.6-.4 3.2-.6 4.6-.2l3.3 1.1"/><path d="M10.6 10.1 8.4 12.2c-.5.5-.4 1.3.2 1.6.5.3 1.1.2 1.5-.2l2.3-2.1"/><path d="M12.4 11.5l3 2.9c.5.5.5 1.2 0 1.6-.5.4-1.1.3-1.5-.1"/><path d="M14.6 13.6c.5.5.5 1.2 0 1.6-.5.4-1.1.3-1.5-.1l-1.1-1"/><path d="M12.9 15.2c.4.5.4 1.1 0 1.5-.4.4-1 .3-1.4-.1l-1-.9"/><path d="M10.9 16.6c.3.4.2 1-.2 1.3-.4.3-.9.2-1.2-.1L4.6 15.4"/><path d="M19.4 15.2c-1.3.8-2.8 1.3-4.3 1.5"/></g>',
    watchlist: '<path d="M9 6h12M9 12h12M9 18h7"/><path d="M4 6h.01M4 12h.01M4 18h.01"/><path d="m19 15 .9 1.8 2 .3-1.45 1.4.35 2-1.8-.95-1.8.95.35-2-1.45-1.4 2-.3z"/>',
    scan: '<path d="M4 8V5a1 1 0 0 1 1-1h3"/><path d="M16 4h3a1 1 0 0 1 1 1v3"/><path d="M20 16v3a1 1 0 0 1-1 1h-3"/><path d="M8 20H5a1 1 0 0 1-1-1v-3"/><path d="M7 12h10"/>',
    expand: '<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/>',
    news: '<rect x="3" y="4" width="15" height="16" rx="2"/><path d="M18 8h3v10a2 2 0 0 1-2 2"/><path d="M7 8h7"/><path d="M7 12h7"/><path d="M7 16h4"/>',
        globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18"/><path d="M12 3a14 14 0 0 0 0 18"/>',
    record: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="4" fill="currentColor"/>',
        sort: '<path d="M7 4v16"/><path d="m3 16 4 4 4-4"/><path d="M17 20V4"/><path d="m13 8 4-4 4 4"/>',
  };
  const icon = (name, size = 16, cls = '') => SVG(PATHS[name], cls, size);

  function progressRing(fraction, size) {
    const r = 9, c = 2 * Math.PI * r, f = Math.max(0.04, Math.min(1, fraction || 0));
    return `<svg class="ico" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="${r}" fill="none" stroke="currentColor" stroke-opacity=".25" stroke-width="3"/>
      <circle cx="12" cy="12" r="${r}" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"
        stroke-dasharray="${(c * f).toFixed(2)} ${c.toFixed(2)}" transform="rotate(-90 12 12)"/></svg>`;
  }

  const DATA_LABEL = {
    fetching: 'Fetching now', backfilling: 'Backfilling history', queued: 'Queued for download',
    new: 'New data should be available', current: 'Up to date', blocked: 'Not collected by this feed',
    closed: 'Closed: archive complete',
    retired: 'Retired: no longer listed, history only',
  };

  // `d`: { state, progress } from /api/activity or /api/symbols.
  function dataIcon(d, size = 14) {
    if (!d) return '';
    const label = DATA_LABEL[d.state] || d.state;
    const body = d.state === 'fetching' ? icon('sync', size, 'spin')
      : d.state === 'backfilling' ? progressRing(d.progress, size)
        : d.state === 'queued' ? icon('clock', size)
          : d.state === 'new' ? icon('download', size)
            : icon('check', size);
    const pctText = d.state === 'backfilling' && d.progress != null ? ` (${Math.round(d.progress * 100)}%)` : '';
    return `<span class="data-icon ${esc(d.state)}" title="${esc(label + pctText)}" role="img" aria-label="${esc(label + pctText)}">${body}</span>`;
  }

  // A logo that fails to load falls back to the lettered badge.
  document.addEventListener('error', (e) => {
    const img = e.target;
    if (!(img instanceof HTMLImageElement) || !img.classList.contains('sym-img')) return;
    const box = img.parentElement;
    box.classList.add('letter');
    box.style.background = box.dataset.color;
    img.replaceWith(document.createTextNode(box.dataset.letter || '?'));
  }, true);

  window.MassiveIcons = { symbolIcon, dataIcon, icon, DATA_LABEL, esc, safeUrl };
})();
