'use strict';
/* Replay on the Analysis view (docs/ARCHITECTURE.md, "Schemes and replay"): a symbol's bars from a
   start point, as if they were arriving live. Replay, then click the bar to start from: the chart
   shows only what was known before it, and plays on at 1×, 2×, 4×, 8×, 16× or 32× real market time
   (a gap longer than GAP_SKIP_MS, nights and weekends, passes at once). The forming bar builds up
   from the finest bars held (1-second bars where there are any, else minutes), each shown once its
   own time has passed, and becomes the chart's own bar when it closes. The level stays as it was
   while replaying (zoom changes only the candles' width). Pages of finer bars are read ahead from
   /api/replay, never the rest of the history. A scheme chosen in the Schemes section runs as the
   replay goes: what it decided appears when the bar that decided it closes (schemes.js). */
(() => {
  const esc = window.MassiveIcons.esc;
  const SPEEDS = [1, 2, 4, 8, 16, 32];
  const GAP_SKIP_MS = 15 * 60_000;
  const AHEAD = 600;  // finer bars still to play before the next page is asked for
  const TICK_MS = 50;
  const FINE_MS = { second: 1e3, minute: 6e4, day: 864e5 };
  let picking = false;
  const R = window.Replay = { active: () => !!state.replay };

  // -- the controls ------------------------------------------------------------------------------
  const btn = $('replayBtn');
  const bar = document.createElement('div');
  bar.id = 'replayBar';
  bar.className = 'replay-bar';
  bar.hidden = true;
  $('chart').append(bar);
  const hint = document.createElement('div');
  hint.className = 'replay-hint';
  hint.hidden = true;
  hint.textContent = 'Replay: click the bar to start from · Esc to cancel';
  $('chart').append(hint);

  // The controls are built again only when one of them changes (playing, speed, the finer bars, the
  // end); while playing only the clock's text moves, at most every CLOCK_MS. Built every tick, a
  // button pressed while playing was gone before it was let go: the click was lost.
  const CLOCK_MS = 250;
  let built = '';  // the controls' state as last built

  function renderBar() {
    const r = state.replay;
    bar.hidden = !r;
    btn.classList.toggle('on', !!r || picking);
    if (!r) { built = ''; return; }
    const shape = [r.playing, r.speed, r.fine?.label, r.done].join('|');
    if (shape !== built) {
      built = shape;
      bar.innerHTML = `
        <button data-act="restart" title="Back to the start point">⏮</button>
        <button data-act="play" class="primary" title="${r.playing ? 'Pause (space)' : 'Play (space)'}">${r.playing ? '❚❚' : '▶'}</button>
        <button data-act="fine" title="One ${esc(r.fine?.label || 'finer bar')} on">›</button>
        <button data-act="bar" title="To the close of the forming bar">»</button>
        <span class="replay-speeds">${SPEEDS.map((s) => `<button data-speed="${s}" class="${s === r.speed ? 'on' : ''}">${s}×</button>`).join('')}</span>
        <span class="replay-clock" title="The replay's time${r.fine ? `, built from ${esc(r.fine.label)} bars` : ''}"></span>
        <button data-act="stop" title="Leave the replay: back to the chart as it is now">✕</button>`;
    }
    renderClock();
  }

  function renderClock() {
    const r = state.replay;
    const el = r && bar.querySelector('.replay-clock');
    if (!el) return;
    const text = `${r.clock != null ? fmtTime(r.clock) : ''}${r.done ? ' · end of the data' : ''}`;
    if (el.textContent !== text) el.textContent = text;
  }

  bar.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b || !state.replay) return;
    if (b.dataset.speed) { state.replay.speed = Number(b.dataset.speed); prefs.set('replaySpeed', state.replay.speed); renderBar(); return; }
    ({ restart: () => begin(state.replay.start), play: toggle, fine: () => step('fine'), bar: () => step('bar'),
       stop: () => stop() })[b.dataset.act]?.();
  });

  // A replay plays plain price bars in: not into a model, nor a chart type the server builds (Heikin
  // Ashi, bricks, box plots ...), whose bars it would mix with plain ones.
  const playable = () => !SERVER_TYPES.has(state.chartType) && state.model.kind === 'price';

  btn.addEventListener('click', () => {
    if (state.replay) { stop(); return; }
    if (!playable()) {
      showError(new Error('Replay draws prices: choose Price and a chart type of plain bars (candles, bars, lines ...)'));
      return;
    }
    picking = !picking;
    hint.hidden = !picking;
    $('chart').classList.toggle('picking', picking);
    renderBar();
  });

  // The bar under a click (its start time), from where it is on the chart.
  function barAt(e) {
    const box = $('chart').getBoundingClientRect();
    const [p] = chart.convertFromPixel([{ x: e.clientX - box.left, y: e.clientY - box.top }], { paneId: 'candle_pane' });
    const data = chart.getDataList();
    const i = Number.isFinite(p?.dataIndex) ? Math.max(0, Math.min(data.length - 1, p.dataIndex)) : -1;
    return data[i]?.timestamp ?? null;
  }

  $('chart').addEventListener('click', (e) => {
    if (!picking || e.target.closest('#replayBar')) return;
    const ts = barAt(e);
    if (ts == null) return;
    picking = false;
    hint.hidden = true;
    $('chart').classList.remove('picking');
    if (playable()) begin(ts);  // the type or model may have changed while picking
    else renderBar();
  }, true);
  window.addEventListener('keydown', (e) => {
    if (e.target.closest?.('input, select, textarea')) return;
    if (e.key === 'Escape' && picking) { picking = false; hint.hidden = true; $('chart').classList.remove('picking'); renderBar(); }
    else if (e.key === ' ' && state.replay) { e.preventDefault(); toggle(); }
  });

  // -- playing ---------------------------------------------------------------------------------
  function begin(start) {
    const old = state.replay;
    if (old) clearInterval(old.timer);
    state.replay = {
      start, clock: start, speed: old?.speed ?? prefs.get('replaySpeed', 1), playing: false,
      queue: [], next: start, more: true, loading: null, complete: new Map(), forming: null, fine: null, done: false,
      key: replayKey(),
    };
    window.Schemes?.reveal(start - 1, null);
    renderBar();
    chart.resetData();  // the loader reads up to the start (app.js getBars) and asks for nothing newer
  }

  // app.js calls this when the chart has loaded the bars before the start
  R.loaded = () => {
    const r = state.replay;
    if (!r) return;
    const data = chart.getDataList();
    window.Schemes?.reveal(data.at(-1)?.timestamp ?? r.start - 1, null);
    loadAhead().then(() => { if (state.replay === r) { r.playing = true; schedule(); renderBar(); runScheme(); } });
  };

  async function loadAhead() {
    const r = state.replay;
    if (!r || !r.more || r.loading) return r?.loading;
    r.loading = (async () => {
      try {
        const res = await api('/api/replay', {
          source: state.source, ticker: state.ticker, period: state.period.type, span: state.period.span,
          session: state.session, from: r.next, ...(state.unfiltered ? { unfiltered: 1 } : {}),
        });
        if (state.replay !== r) return;
        r.fine = res.fine;
        r.queue.push(...res.bars);
        for (const b of res.complete) r.complete.set(b.timestamp, b);
        r.more = res.more;
        if (res.next != null) r.next = res.next + 1;
        if (res.complete.length) runScheme();
      } catch (err) {
        showError(err);
        r.playing = false;
      } finally {
        r.loading = null;
        renderBar();
      }
    })();
    return r.loading;
  }

  function schedule() {
    const r = state.replay;
    clearInterval(r.timer);
    r.last = performance.now();
    r.timer = setInterval(tick, TICK_MS);
  }

  function toggle() {
    const r = state.replay;
    if (!r) return;
    r.playing = !r.playing && !r.done;
    if (r.playing) schedule(); else clearInterval(r.timer);
    renderBar();
  }

  function tick() {
    const r = state.replay;
    if (!r || !r.playing) return;
    const now = performance.now();
    r.clock += (now - r.last) * r.speed;
    r.last = now;
    let shown = 0;
    while (r.queue.length) {
      const f = r.queue[0];
      if (f.timestamp - r.clock > GAP_SKIP_MS) r.clock = f.timestamp;  // the market was shut: on at once
      if (f.timestamp + (FINE_MS[r.fine?.type] || 6e4) > r.clock) break;  // its time has not passed yet
      show(r.queue.shift());
      if (++shown > 2000) break;
    }
    if (r.queue.length < AHEAD) loadAhead();
    if (!r.queue.length && !r.more && !r.loading) finish();
    if (!r.playing) renderBar();  // it ended: the controls change
    else if (now - (r.clockAt || 0) >= CLOCK_MS) { r.clockAt = now; renderClock(); }
  }

  function step(what) {
    const r = state.replay;
    if (!r || !r.queue.length) { loadAhead().then(renderBar); return; }
    if (r.playing) toggle();
    const bin = r.queue[0].bin;
    do {
      const f = r.queue.shift();
      r.clock = f.timestamp + (FINE_MS[r.fine?.type] || 6e4);
      show(f);
    } while (what === 'bar' && r.queue.length && r.queue[0].bin === bin);
    if (what === 'bar' && r.queue.length) closeForming();
    if (r.queue.length < AHEAD) loadAhead();
    renderBar();
  }

  // A finer bar's time has passed: it joins the forming bar, or closes it and starts the next.
  function show(f) {
    const r = state.replay;
    if (r.forming && r.forming.timestamp !== f.bin) closeForming();
    if (!r.forming) {
      r.forming = { timestamp: f.bin, open: f.open, high: f.high, low: f.low, close: f.close, volume: f.volume,
                    turnover: f.turnover || 0, trades: f.trades || 0, derived: true };
    } else {
      const b = r.forming;
      b.high = Math.max(b.high, f.high); b.low = Math.min(b.low, f.low); b.close = f.close;
      b.volume += f.volume; b.turnover += f.turnover || 0; b.trades += f.trades || 0;
    }
    state.barCallback?.({ ...r.forming });
    window.Schemes?.reveal(r.lastClosed ?? r.start - 1, r.forming.timestamp);
  }

  function closeForming() {
    const r = state.replay;
    if (!r.forming) return;
    const own = r.complete.get(r.forming.timestamp);  // the chart's own bar, now that it has closed
    if (own) state.barCallback?.(own);
    r.lastClosed = r.forming.timestamp;
    r.complete.delete(r.forming.timestamp);
    r.forming = null;
    window.Schemes?.reveal(r.lastClosed, null);
  }

  function finish() {
    const r = state.replay;
    closeForming();
    r.done = true;
    r.playing = false;
    clearInterval(r.timer);
  }

  // The scheme over the replayed stretch and the bars loaded ahead of it, all closed in the data:
  // what it decided is shown as the replay reaches each bar (schemes.js), never sooner.
  R.range = () => {
    const r = state.replay;
    if (!r) return null;
    const closed = [...r.complete.keys()];  // loaded and closed in the data, not shown yet
    return { from: r.start, to: closed.length ? Math.max(...closed) : (r.lastClosed ?? r.start), over: 'the replay' };
  };

  function runScheme() {
    const r = state.replay, S = window.Schemes;
    if (!r || (!S?.result && !S?.armed)) return;
    S.armed = true;
    S.run(R.range(), { quiet: true });
  }

  function stop(reload = true) {
    const r = state.replay;
    picking = false;
    hint.hidden = true;
    $('chart').classList.remove('picking');
    if (!r) { renderBar(); return; }
    clearInterval(r.timer);
    state.replay = null;
    if (window.Schemes) { window.Schemes.armed = false; window.Schemes.reveal(null, null); }
    renderBar();
    if (reload) chart.resetData();  // back to the chart as it is now
  }
  R.stop = stop;
  R.begin = begin;

  // Another symbol, level or view ends the replay (its bars are for this one).
  window.addEventListener('massive:symbol', () => stop());
  window.addEventListener('massive:view', (e) => { if (e.detail.pane !== 'analysis') stop(); });
  window.addEventListener('massive:scheme', () => {
    const r = state.replay;
    if (r && window.Schemes) window.Schemes.reveal(r.lastClosed ?? chart.getDataList().at(-1)?.timestamp ?? r.start - 1,
                                                   r.forming?.timestamp ?? null);
  });
  // What the replay's bars are for: a chart loaded for anything else ends it (app.js getBars). The
  // model, a chart type the server builds and the futures adjustment too: a switch to a model or to
  // Heikin Ashi went on appending price bars into it. (Between types of plain bars, candles to a line,
  // the same bars are drawn: it plays on.)
  function replayKey() {
    return `${state.source}:${state.ticker}|${state.period.type}:${state.period.span}|${state.session}|${state.unfiltered}`
      + `|${state.model.kind}|${SERVER_TYPES.has(state.chartType) ? state.chartType : 'plain'}|${state.adjust}`;
  }
  R.keyChanged = () => {
    const r = state.replay;
    return r && r.key !== replayKey();
  };
  renderBar();
})();
