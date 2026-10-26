'use strict';
/* Copy, cut and paste of drawings (the drawing tools' lines, channels, notes, highlights ...): Ctrl+C,
   Ctrl+X and Ctrl+V (Cmd on a Mac), and a menu on right-click: on a drawing Copy, Cut, Paste,
   Duplicate and Delete; on the chart elsewhere Paste. What is copied is kept in the browser
   (localStorage), so it pastes into the other chart views, another symbol or another tab. A paste
   lands at the pointer (the drawing's first point there, its shape kept in bars and price), or, with
   the pointer off the chart, PASTE_BARS bars to the right of where it was copied from. */
(() => {
  const KEY = 'massive:drawings:clipboard';
  const PASTE_BARS = 5;
  const chartEl = $('chart');
  let pointer = null;  // the pointer over the chart, in the chart's own pixels
  let memory = null;  // the clipboard when the browser keeps no storage (a private window)
  let rightClicked = null;  // the drawing a right-click landed on (the chart's own event), and when

  chartEl.addEventListener('pointermove', (e) => {
    const b = chartEl.getBoundingClientRect();
    pointer = { x: e.clientX - b.left, y: e.clientY - b.top };
  });
  chartEl.addEventListener('pointerleave', () => { pointer = null; });

  const clip = () => {
    try { return JSON.parse(localStorage.getItem(KEY) || 'null') ?? memory; } catch { return memory; }
  };
  function keep(value) {
    memory = value;
    try { localStorage.setItem(KEY, JSON.stringify(value)); } catch { /* kept in memory only */ }
  }

  const selected = () => (state.selectedOverlay ? chart.getOverlays({ id: state.selectedOverlay })[0] ?? null : null);
  // `pane`: the indicator a drawing below the price was on (app.js paneIndicator): the pane it pastes
  // into, in this chart or another tab's (pane ids are each chart's own).
  const snapshot = (o) => ({
    name: o.name, paneId: o.paneId || 'candle_pane', pane: paneIndicator(o.paneId), extendData: o.extendData ?? null,
    styles: o.styles ?? null, points: o.points.map((p) => ({ timestamp: p.timestamp, value: p.value })),
  });
  // Where an item pastes: the price pane, its indicator's pane, or (copied before `pane` was kept) the
  // same pane if this chart has it; null when the chart shows no such pane.
  const paneFor = (it) => (!it.paneId || it.paneId === 'candle_pane' ? 'candle_pane'
    : (it.pane && indicatorPane(it.pane)) || (chart.getIndicators({ paneId: it.paneId }).length ? it.paneId : null));
  const NAMES = { measure: 'highlight', simpleAnnotation: 'note' };
  const label = (it) => NAMES[it.name] || it.name.replace(/([A-Z])/g, ' $1').toLowerCase().trim();

  // -- bars: a drawing keeps its shape in bars (and price) wherever it is pasted -------------------
  function stepMs(data) {
    const d = [];
    for (let i = Math.max(1, data.length - 50); i < data.length; i++) d.push(data[i].timestamp - data[i - 1].timestamp);
    d.sort((a, b) => a - b);
    return d[d.length >> 1] || 60_000;
  }
  function indexOf(data, ts, step) {  // beyond the bars loaded: counted on at the usual step
    const n = data.length;
    if (ts > data[n - 1].timestamp) return n - 1 + Math.round((ts - data[n - 1].timestamp) / step);
    if (ts < data[0].timestamp) return Math.round((ts - data[0].timestamp) / step);
    return barIndexAt(data, ts);
  }
  function timeAt(data, i, step) {
    const n = data.length;
    if (i < 0) return data[0].timestamp + i * step;
    if (i >= n) return data[n - 1].timestamp + (i - n + 1) * step;
    return data[i].timestamp;
  }

  // -- the actions -----------------------------------------------------------------------------
  function copy(o = selected()) {
    if (!o) return false;
    keep({ items: [snapshot(o)], ticker: state.ticker, at: Date.now() });
    toast(`Copied the ${label(o)}`);
    return true;
  }

  function cut(o = selected()) {
    if (!copy(o)) return false;
    chart.removeOverlay({ id: o.id });  // its onRemoved saves the drawings
    toast(`Cut the ${label(o)}`);
    return true;
  }

  // Draws `items` again with their first point at `at` (chart pixels) where it is on the price pane,
  // else `PASTE_BARS` bars on from where they were. An item whose pane this chart does not show is
  // left out (the chart would put it on the price pane, at an indicator's values). Whether any was drawn.
  function place(items, at) {
    const data = chart.getDataList();
    if (!items?.length || !data.length) return false;
    const step = stepMs(data);
    const first = items[0].points[0];
    let bars = PASTE_BARS, price = 0;
    if (at && items.every((it) => it.paneId === 'candle_pane')) {
      const [p] = chart.convertFromPixel([{ x: at.x, y: at.y }], { paneId: 'candle_pane' });
      if (Number.isFinite(p?.dataIndex) && Number.isFinite(p?.value)) {
        bars = p.dataIndex - indexOf(data, first.timestamp, step);
        price = p.value - first.value;
      }
    }
    let made = 0;
    for (const it of items) {
      const paneId = paneFor(it);
      if (!paneId) continue;
      if (it.name === 'measure') chart.removeOverlay({ groupId: 'user', name: 'measure' });  // one highlight at a time
      const id = chart.createOverlay({
        name: it.name, groupId: 'user', paneId, extendData: it.extendData ?? undefined, styles: it.styles ?? undefined,
        points: it.points.map((pt) => ({ timestamp: timeAt(data, indexOf(data, pt.timestamp, step) + bars, step),
                                         value: pt.value + price })),
        ...overlayEvents(),
      });
      if (id) made++;
    }
    if (!made) return false;
    saveDrawings();
    if (items.some((it) => it.name === 'measure')) scheduleHighlightPanel();
    return true;
  }

  function paste(at = pointer) {
    const c = clip();
    if (!c?.items?.length) return false;
    if (!place(c.items, at)) {
      const pane = c.items.find((it) => it.pane)?.pane;
      if (pane && chart.getDataList().length) toast(`Not pasted: the ${label(c.items[0])} was on ${pane}, not on this chart`);
      return false;
    }
    toast(`Pasted the ${label(c.items[0])}`);
    return true;
  }

  const duplicate = (o) => place([snapshot(o)], null);

  // -- the right-click menu ----------------------------------------------------------------------
  const menu = document.createElement('div');
  menu.className = 'dropdown draw-menu';
  menu.hidden = true;
  menu.setAttribute('role', 'menu');
  chartEl.append(menu);
  let menuFor = null, menuAt = null;

  function openMenu(o, at) {
    menuFor = o; menuAt = at;
    const can = !!clip()?.items?.length;
    const item = (act, text, key, enabled = true) =>
      `<button type="button" class="menu-item" role="menuitem" data-act="${act}"${enabled ? '' : ' disabled'}>${text}<span class="key">${key}</span></button>`;
    menu.innerHTML = (o
      ? item('copy', 'Copy', 'Ctrl+C') + item('cut', 'Cut', 'Ctrl+X') + item('paste', 'Paste', 'Ctrl+V', can)
        + '<hr class="menu-sep">' + item('duplicate', 'Duplicate', '') + item('delete', 'Delete', 'Del')
      : item('paste', can ? `Paste the ${label(clip().items[0])}` : 'Paste (nothing copied)', 'Ctrl+V', can));
    menu.hidden = false;
    const w = chartEl.clientWidth, h = chartEl.clientHeight;
    menu.style.left = `${Math.min(at.x, w - menu.offsetWidth - 4)}px`;
    menu.style.top = `${Math.min(at.y, h - menu.offsetHeight - 4)}px`;
  }
  const closeMenu = () => { menu.hidden = true; menuFor = null; };

  menu.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b || b.disabled) return;
    const o = menuFor, at = menuAt;
    closeMenu();
    ({ copy: () => copy(o), cut: () => cut(o), paste: () => paste(at), duplicate: () => duplicate(o),
       delete: () => chart.removeOverlay({ id: o.id }) })[b.dataset.act]?.();
  });
  // the chart's own event says which drawing it was; the browser's opens the menu (in either order)
  chartEl.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    const b = chartEl.getBoundingClientRect();
    const at = { x: e.clientX - b.left, y: e.clientY - b.top };
    setTimeout(() => {
      const o = rightClicked && Date.now() - rightClicked.at < 500 ? rightClicked.overlay : null;
      rightClicked = null;
      openMenu(o, at);
    }, 0);
  });
  document.addEventListener('pointerdown', (e) => { if (!menu.hidden && !menu.contains(e.target)) closeMenu(); }, true);
  chartEl.addEventListener('wheel', closeMenu, { passive: true });

  // -- keys --------------------------------------------------------------------------------------
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !menu.hidden) { closeMenu(); return; }
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
    if (e.target.closest?.('input, select, textarea, [contenteditable="true"]') || document.querySelector('dialog[open]')) return;
    if (document.body.dataset.view !== 'chart') return;
    const k = e.key.toLowerCase();
    const done = k === 'c' ? copy() : k === 'x' ? cut() : k === 'v' ? paste() : false;
    if (done) e.preventDefault();  // nothing selected or copied: the browser's own copy and paste
  });

  // -- a short note of what happened ---------------------------------------------------------------
  const note = document.createElement('div');
  note.className = 'draw-toast';
  note.hidden = true;
  chartEl.append(note);
  let noteTimer = 0;
  function toast(text) {
    note.textContent = text;
    note.hidden = false;
    clearTimeout(noteTimer);
    noteTimer = setTimeout(() => { note.hidden = true; }, 1600);
  }

  window.DrawingClipboard = {
    rightClick(overlay) { rightClicked = { overlay, at: Date.now() }; },  // app.js overlayEvents
    copy, cut, paste, duplicate,
  };
})();
