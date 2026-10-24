'use strict';
/* The right-hand icon rail, like TradingView's. Each icon switches the main window:

     Stock     the chart, with the symbol (its own logo), key statistics and business beside it
     Analysis  the chart, with the highlight's measurements and the bar under the cursor beside it
     Markets   the world's exchanges on a globe turned to where it is noon now (globe.js)
     News      every announcement the globe's ticker shows (news.js)
     Datasets  research series drawn on one time axis: factors, CAPE, COT positioning (datasets.js)
     Fetcher   historical data (sessions before the current one); coloured while a run is active
     Live      current-session feeds; a red "recording" dot while live data is being recorded

   Clicking Stock or Analysis again while it is showing hides or shows its side panel.
   Also: the cursor statistics box in the volume pane (on-screen max/min and the bar under the
   cursor). */
(() => {
  const { symbolIcon, icon } = window.MassiveIcons;
  const VIEWS = { stock: 'chart', analysis: 'chart', markets: 'markets', news: 'news', datasets: 'datasets', fetcher: 'fetcher', live: 'live' };

  // -- rail --------------------------------------------------------------------------------
  function showPane(name, { toggle = true } = {}) {
    if (!(name in VIEWS)) name = 'stock';
    const view = VIEWS[name];
    const same = document.body.dataset.pane === name && document.body.dataset.view === view;
    if (view === 'chart' && same && toggle) {
      setPanelHidden(!document.body.classList.contains('panel-hidden'));
      updateRail();
      return;
    }
    document.body.dataset.pane = name;
    document.body.dataset.view = view;
    prefs.set('pane', name);
    for (const v of document.querySelectorAll('main > .view')) v.hidden = v.dataset.view !== view;
    if (view === 'chart') {
      for (const p of document.querySelectorAll('#sidePanel .pane')) p.hidden = p.dataset.pane !== name;
      if (document.body.classList.contains('panel-hidden')) setPanelHidden(false);
      requestAnimationFrame(() => { chart.resize(); checkLod(); });
      if (name === 'stock') loadInfo();
      if (name === 'analysis') { renderHighlightPanel(); renderCursor(); }
    }
    updateRail();
    window.dispatchEvent(new CustomEvent('massive:view', { detail: { pane: name, view } }));
  }
  window.showPane = showPane;

  function updateRail() {
    const pane = document.body.dataset.pane;
    const chartPanelHidden = document.body.dataset.view === 'chart' && document.body.classList.contains('panel-hidden');
    for (const b of document.querySelectorAll('#rail button')) {
      const on = b.dataset.pane === pane;
      b.classList.toggle('active', on);
      b.classList.toggle('dim', on && chartPanelHidden);
      b.setAttribute('aria-pressed', String(on));
    }
  }

  function renderRailIcons(activity) {
    $('railStock').innerHTML = state.ticker ? symbolIcon(symbolInfo(state.ticker), 22) : icon('analysis', 20);
    $('railAnalysis').innerHTML = icon('analysis', 20);
    $('railMarkets').innerHTML = icon('globe', 20);
    $('railNews').innerHTML = icon('news', 20);
    $('railDatasets').innerHTML = icon('dataset', 20);
    const running = !!activity?.running;
    const fresh = activity?.newData || 0;
    $('railFetcher').innerHTML = `${icon('database', 20)}${running ? `<span class="rail-badge spin">${icon('sync', 11)}</span>` : ''}`
      + (!running && fresh ? `<span class="rail-count" title="${fresh} symbols should have new data">${fresh}</span>` : '');
    $('railFetcher').classList.toggle('busy', running);
    $('railFetcher').title = running
      ? `Fetcher: running${activity.live?.current ? `, fetching ${activity.live.current}` : ''}`
      : 'Fetcher: historical data (sessions before the current one)';
    const live = activity?.liveFeeds;
    $('railLive').innerHTML = icon('record', 20);
    $('railLive').classList.toggle('recording', !!live?.recording);
    $('railLive').classList.toggle('available', !!live?.available && !live?.recording);
    $('railLive').title = live?.recording ? 'Live feeds: recording the current session'
      : live?.available ? 'Live feeds: delayed data available, not recording' : 'Live feeds: none on your plan';
  }

  document.getElementById('rail').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-pane]');
    if (b) showPane(b.dataset.pane);
  });
  window.addEventListener('massive:activity', (e) => renderRailIcons(e.detail));
  window.addEventListener('massive:symbol', () => renderRailIcons(window.Activity?.data));
  window.addEventListener('massive:open-symbol', () => {
    if (document.body.dataset.view !== 'chart') showPane('stock', { toggle: false });
  });
  window.addEventListener('massive:run-finished', () => {
    // New data may have arrived: refresh symbols, the panel and the chart's newest bars.
    loadSymbols().then(renderSymbolButton).catch(() => {});
    if (typeof state.pollNow === 'function') state.pollNow();
    loadInfo();
  });
  window.addEventListener('keydown', (e) => {  // Escape returns to the chart from the other views
    if (e.key === 'Escape' && document.body.dataset.view !== 'chart' && !document.querySelector('dialog[open]')) showPane('stock', { toggle: false });
  });

  // -- cursor statistics ---------------------------------------------------------------
  // The bar under the crosshair (or the newest bar) and the high/low of what is on screen:
  // shown in a box in the volume pane and in the Analysis panel.
  const box = document.createElement('div');
  box.id = 'cursorBox';
  box.className = 'cursor-box';
  document.getElementById('chart').append(box);
  let cursorIndex = null;

  function visibleHighLow(list) {
    const { from, to } = chart.getVisibleRange();
    let hi = null, lo = null;
    for (let i = Math.max(0, from); i < Math.min(list.length, to); i++) {
      const b = list[i];
      if (!hi || b.high > hi.high) hi = b;
      if (!lo || b.low < lo.low) lo = b;
    }
    return { hi, lo };
  }

  function cursorStats() {
    const list = chart.getDataList();
    if (!list.length) return null;
    const i = cursorIndex != null && cursorIndex >= 0 && cursorIndex < list.length ? cursorIndex : list.length - 1;
    const b = list[i];
    const prev = i > 0 ? list[i - 1] : null;
    const change = prev ? b.close - prev.close : b.close - b.open;
    const base = prev ? prev.close : b.open;
    const next = i + 1 < list.length ? list[i + 1].timestamp : b.timestamp + 60000;
    return { bar: b, change, changePct: base ? (change / base) * 100 : null, range: b.high - b.low,
             vwap: b.volume ? (b.turnover || 0) / b.volume : null, ...visibleHighLow(list), live: cursorIndex == null,
             feeds: feedsIn(b.timestamp, next) };
  }

  // Which feed(s) the bar came from: the coverage spans (one feed per span) overlapping it.
  function feedsIn(a, b) {
    const att = state.meta?.attribution || {};
    const daily = ['day', 'week', 'month', 'quarter', 'year'].includes(state.period?.type) && att.day?.length;
    const spans = (daily ? att.day : att[state.meta?.base]) || [];
    return [...new Set(spans.filter(([s, e]) => s < b && e > a).map((x) => x[2]))];
  }

  function volumePane() {
    const ind = (chart.getIndicators?.({ name: 'VOL' }) || [])[0];
    if (!ind) return null;
    const size = chart.getSize(ind.paneId, 'main');
    return size ? { id: ind.paneId, ...size } : null;
  }

  const t = (ms) => fmtTime(ms);
  const isPrice = () => state.model.kind === 'price';
  const e = window.MassiveIcons.esc;
  function renderCursor() {
    const s = cursorStats();
    const pane = volumePane();
    if (!s) { box.hidden = true; $('pCursor').innerHTML = ''; return; }
    const b = s.bar;
    const chg = `<span class="${cls(s.change)}">${signedPx(s.change)}${isPrice() && s.changePct != null ? ` (${pct(s.changePct)})` : ''}</span>`;
    if (pane) {
      box.hidden = false;
      box.style.top = `${pane.top + 2}px`;
      box.style.right = `${(chart.getSize('candle_pane', 'yAxis')?.width || 92) + 6}px`;
      box.innerHTML = `
        <div><span class="p-muted">Max</span> ${priceText(s.hi?.high)} <span class="p-muted">${s.hi ? e(t(s.hi.timestamp)) : ''}</span>
          · <span class="p-muted">Min</span> ${priceText(s.lo?.low)} <span class="p-muted">${s.lo ? e(t(s.lo.timestamp)) : ''}</span></div>
        <div><span class="p-muted">${s.live ? 'Last' : e(t(b.timestamp))}</span>
          O ${priceText(b.open)} H ${priceText(b.high)} L ${priceText(b.low)} C ${priceText(b.close)} ${chg}
          · <span class="p-muted">Rng</span> ${priceText(s.range)} · <span class="p-muted">Vol</span> ${big(b.volume)}${s.vwap && isPrice() ? ` · <span class="p-muted">VWAP</span> ${priceText(s.vwap)}` : ''}${s.feeds.length ? ` · <span class="p-muted">Src</span> ${e(s.feeds.join('+'))}` : ''}</div>`;
    } else {
      box.hidden = true;
    }
    if (document.body.dataset.pane === 'analysis') {
      $('pCursor').innerHTML = `
        <h3>${s.live ? 'Last bar' : 'Bar under cursor'}</h3>
        ${kv([
          ['Time', e(t(b.timestamp))],
          ['Open', priceText(b.open)], ['High', priceText(b.high)], ['Low', priceText(b.low)], ['Close', priceText(b.close)],
          ['Change', chg], ['Range', priceText(s.range)], ['Volume', big(b.volume)],
          isPrice() && s.vwap != null && ['VWAP', priceText(s.vwap)],
          ['Trades', big(b.trades)],
          s.feeds.length && ['Source', e(s.feeds.join(' + '))],
        ])}
        <div class="p-sub">On screen</div>
        ${kv([
          ['Max', `${priceText(s.hi?.high)} <small>${s.hi ? e(t(s.hi.timestamp)) : ''}</small>`],
          ['Min', `${priceText(s.lo?.low)} <small>${s.lo ? e(t(s.lo.timestamp)) : ''}</small>`],
          ['Max − min', priceText(s.hi && s.lo ? s.hi.high - s.lo.low : null)],
        ])}`;
    }
  }
  window.renderCursor = renderCursor;

  let cursorFrame = 0;
  function scheduleCursor() {
    if (cursorFrame) return;
    cursorFrame = requestAnimationFrame(() => { cursorFrame = 0; renderCursor(); });
  }
  chart.subscribeAction('onCrosshairChange', (c) => {
    const n = chart.getDataList().length;
    cursorIndex = c && Number.isFinite(c.dataIndex) && c.dataIndex < n && c.kLineData ? c.dataIndex : null;
    scheduleCursor();
  });
  chart.subscribeAction('onVisibleRangeChange', scheduleCursor);
  document.getElementById('chart').addEventListener('mouseleave', () => { cursorIndex = null; scheduleCursor(); });

  // -- start --------------------------------------------------------------------------
  renderRailIcons(null);
  showPane(prefs.get('pane', 'stock'), { toggle: false });
})();
