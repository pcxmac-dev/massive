'use strict';
/* The right-hand icon rail, like TradingView's. Each icon switches the main window:

     Stock     the chart, with the symbol (its own logo), key statistics and business beside it
     Analysis  the same symbol's chart with content of its own (interval, chart type, model,
               indicators and their panes, zoom, drawings), the highlight's measurements and the
               bar under the cursor beside it; schemes and replay (schemes.js, replay.js)
     Trading   the same symbol's chart with content of its own, the paper account and the brokers beside it
               (trading.js); live trading is off
     Markets   the world's exchanges on a globe turned to where it is noon now (globe.js)
     News      every announcement the globe's ticker shows (news.js)
     Gov       the government and the regulators: current regulatory parameters, the Fed's, the SEC's,
               BLS's and the Federal Register's newest (gov.js)
     Datasets  research series drawn on one time axis: factors, CAPE, COT positioning (datasets.js)
     Fetcher   historical data (sessions before the current one); coloured while a run is active
     Live      current-session feeds; a red "recording" dot while live data is being recorded

   Clicking Stock or Analysis again while it is showing hides or shows its side panel.
   Also: the cursor statistics box in the price window's upper right (on-screen max/min and the bar under the
   cursor). */
(() => {
  const { symbolIcon, icon, esc } = window.MassiveIcons;
  // The watchlist sits beside whichever chart was showing (it has no chart content of its own).
  const VIEWS = { stock: 'chart', analysis: 'chart', trading: 'chart', watchlist: 'chart', markets: 'markets', news: 'news',
                  gov: 'gov', datasets: 'datasets', fetcher: 'fetcher', live: 'live' };

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
    if (view !== 'chart') state.selectedOverlay = null;  // Delete on another view deleted the hidden chart's drawing
    prefs.set('pane', name);
    if (view === 'chart' && name !== 'watchlist') window.switchWorkspace?.(name);  // each chart view keeps its own content
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
    window.LiveIcon?.redraw();  // the selected one is drawn blue
    window.GlobeIcon?.update();
  }

  const RAIL_ICON = 32;  // px; the buttons (style.css #rail) leave room around it

  function renderRailIcons(activity) {
    $('railStock').innerHTML = state.ticker ? symbolIcon(symbolInfo(state.ticker), RAIL_ICON + 4) : icon('analysis', RAIL_ICON);
    $('railAnalysis').innerHTML = icon('integral', RAIL_ICON);
    $('railWatch').innerHTML = icon('watchlist', RAIL_ICON);
    $('railTrading').innerHTML = icon('trading', RAIL_ICON);
    if (!window.GlobeIcon?.update()) $('railMarkets').innerHTML = icon('globe', RAIL_ICON);  // the Earth, once its images are here
    $('railNews').innerHTML = icon('thermometer', RAIL_ICON);
    $('railGov').innerHTML = icon('statehouse', RAIL_ICON);
    $('railDatasets').innerHTML = icon('scroll', RAIL_ICON);
    const running = !!activity?.running;
    const fresh = activity?.newData || 0;
    // its platter turns while a run fetches (.busy): redrawn only when it changes, or each poll would
    // restart the turn from the top
    const fetcherIcon = icon('disk', RAIL_ICON)
      + (!running && fresh ? `<span class="rail-count" title="${esc(fresh)} symbols should have new data">${esc(fresh)}</span>` : '');
    if ($('railFetcher').dataset.drawn !== fetcherIcon) {
      $('railFetcher').innerHTML = fetcherIcon;
      $('railFetcher').dataset.drawn = fetcherIcon;
    }
    $('railFetcher').classList.toggle('busy', running);
    $('railFetcher').title = running
      ? `Fetcher: running${activity.live?.current ? `, fetching ${activity.live.current}` : ''}`
      : 'Fetcher: historical data (sessions before the current one)';
    const live = activity?.liveFeeds;
    if (!$('railLive').querySelector('svg')) $('railLive').insertAdjacentHTML('afterbegin', icon('record', RAIL_ICON));
    const streaming = live?.liveTickers || [];
    window.LiveIcon?.update(streaming.length > 0);  // live data arriving: the icon's effect runs
    $('railLive').classList.toggle('recording', !!live?.recording);
    $('railLive').classList.toggle('available', !!live?.available && !live?.recording);
    $('railLive').title = isLive() ? `Live feeds: ${state.ticker} is streaming now`
      : streaming.length ? `Live feeds: ${streaming.length} symbol${streaming.length > 1 ? 's' : ''} streaming now`
      : live?.recording ? 'Live feeds: recording the current session'
        : live?.available ? 'Live feeds: delayed data available, not recording' : 'Live feeds: none on your plan';
  }

  document.getElementById('rail').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-pane]');
    if (b) showPane(b.dataset.pane);
  });
  window.addEventListener('massive:activity', (e) => renderRailIcons(e.detail));
  window.addEventListener('massive:symbol', () => renderRailIcons(window.Activity?.data));
  window.addEventListener('massive:run-finished', () => {
    // New data may have arrived: refresh symbols, the panel and the chart's newest bars.
    loadSymbols().then(renderSymbolButton).catch(() => {});
    if (typeof state.pollNow === 'function') state.pollNow();
    loadInfo();
  });
  window.addEventListener('massive:symbol-updated', (e) => {
    if (e.detail === state.ticker) chart.resetData();  // filled gaps are anywhere, not only after the newest bar
  });
  window.addEventListener('keydown', (e) => {  // Escape returns to the chart from the other views
    if (e.key === 'Escape' && document.body.dataset.view !== 'chart' && !document.querySelector('dialog[open]')) showPane('stock', { toggle: false });
  });

  // -- cursor statistics ---------------------------------------------------------------
  // The bar under the crosshair (or the newest bar) and the high/low of what is on screen:
  // shown in a box in the price window's upper right and in the Analysis panel.
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

  const t = (ms) => fmtTime(ms);
  const isPrice = () => state.model.kind === 'price';
  const e = window.MassiveIcons.esc;
  // The box sits in the price window's upper right corner, below the roll labels of continuous futures.
  function renderCursor() {
    const s = cursorStats();
    const pane = chart.getSize('candle_pane', 'main');
    if (!s) { box.hidden = true; $('pCursor').innerHTML = ''; return; }
    const b = s.bar;
    const chg = `<span class="${cls(s.change)}">${signedPx(s.change)}${isPrice() && s.changePct != null ? ` (${pct(s.changePct)})` : ''}</span>`;
    if (pane) {
      box.hidden = false;
      box.style.top = `${pane.top + (state.meta?.rolls?.length ? 26 : 6)}px`;
      box.style.right = `${(chart.getSize('candle_pane', 'yAxis')?.width || 92) + 6}px`;
      box.innerHTML = `
        <div><span class="p-muted">Max</span> ${priceText(s.hi?.high)} <span class="p-muted">${s.hi ? e(t(s.hi.timestamp)) : ''}</span>
          · <span class="p-muted">Min</span> ${priceText(s.lo?.low)} <span class="p-muted">${s.lo ? e(t(s.lo.timestamp)) : ''}</span></div>
        <div><span class="p-muted">${s.live ? 'Last' : e(t(b.timestamp))}</span>${b.interp
          ? ' <span class="derived-tag interp" title="Interpolated: no data at this interval here; a line between the 1-minute bars\' volume-weighted prices">interpolated*</span>'
          : b.derived ? ' <span class="derived-tag" title="Derived: built here from finer data; no feed sent this candle at this interval">derived*</span>' : ''}
          O ${priceText(b.open)} H ${priceText(b.high)} L ${priceText(b.low)} C ${priceText(b.close)} ${chg}
          ${b.q1 != null ? `· <span class="p-muted" title="${e(state.approximate || '')}">Q1*</span> ${priceText(b.q1)} <span class="p-muted">Med*</span> ${priceText(b.med)} <span class="p-muted">Q3*</span> ${priceText(b.q3)} ` : ''}· <span class="p-muted">Rng</span> ${priceText(s.range)} · <span class="p-muted">Vol</span> ${big(b.volume)}${s.vwap && isPrice() ? ` · <span class="p-muted">VWAP</span> ${priceText(s.vwap)}` : ''}${s.feeds.length ? ` · <span class="p-muted">Src</span> ${e(s.feeds.join('+'))}` : ''}</div>`;
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
    cursorIndex = crosshairBar(c)?.index ?? null;  // the bar under the cursor (app.js)
    scheduleCursor();
  });
  chart.subscribeAction('onVisibleRangeChange', scheduleCursor);
  document.getElementById('chart').addEventListener('mouseleave', () => { cursorIndex = null; scheduleCursor(); });

  // -- start --------------------------------------------------------------------------
  renderRailIcons(null);
  showPane(prefs.get('pane', 'stock'), { toggle: false });
})();
