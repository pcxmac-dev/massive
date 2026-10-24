// Icons shared by the chart app: a symbol's logo (or a lettered badge, like TradingView), the
// data-state icon (fetching / backfilling / queued / new data / up to date) and the right rail's
// tool icons. Plain functions returning HTML strings; all text is escaped.
(() => {
  'use strict';

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

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
    dataset: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/><path d="m7 17 3-4 3 2 4-4"/>',
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

  window.MassiveIcons = { symbolIcon, dataIcon, icon, DATA_LABEL, esc };
})();
