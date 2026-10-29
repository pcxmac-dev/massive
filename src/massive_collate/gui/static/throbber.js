'use strict';
/* The open symbol's logo as a throbber, after Internet Explorer 3's (the user, 2026-10-28: "when the
   current symbol is updating, use the old school explorer icon + the current symbol icon scheme, ie,
   the globe rotates and the icon is on the other side"; "keep the status on the bottom, but lets
   animate the current symbol icon").

   Wherever the logo shows (the symbol button's at the top left, the Stock rail button's) it is the
   front of a card; the back is the Markets view's globe (globeicon.js), turning. While the symbol
   updates the card turns about its upright axis, the logo and the turning globe in turn, and when it
   is done it finishes its turn and rests on the logo. Updating: its fetch queued or under way (the
   status bar's job, activity.js), or new bars just in (pulse: app.js, each stream message or poll that
   brought some; at least one whole turn). With reduced motion asked for it does not turn: a ring. */
(() => {
  const TURN_MS = 2400;          // one turn of the card (style.css throb-turn)
  const GLOBE_STEP = 15;         // degrees of the globe's turn a frame (24 frames a turn, each kept)
  const FRAME_MS = 80;
  const FETCHING = new Set(['waiting', 'downloading']);  // the symbol's fetch queued or under way
  const still = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  const hosts = new Map();       // host element -> its size (css px)
  let pulseUntil = 0;
  let lon = 0;

  // The card in `host`, built once; its front written only when it changes (the rail and the button are
  // drawn again on every activity poll: writing them would start the turn over each time).
  function face(host, html, size) {
    if (typeof host === 'string') host = document.getElementById(host);
    if (!host) return;
    let card = host.querySelector(':scope > .throb');
    if (!card) {
      host.innerHTML = `<span class="throb" style="--throb-size:${size}px"><span class="throb-card">
        <span class="throb-face throb-front"></span>
        <span class="throb-face throb-back" aria-hidden="true"><canvas class="throb-globe"></canvas></span></span></span>`;
      card = host.querySelector(':scope > .throb');
      card.querySelector('.throb-card').addEventListener('animationiteration', () => {
        if (!busy()) card.classList.remove('updating');  // done: rests on the logo at the turn's end
      });
    }
    hosts.set(host, size);
    const front = card.querySelector('.throb-front');
    if (front.dataset.html !== html) { front.innerHTML = html; front.dataset.html = html; }
  }

  // New bars in for the open symbol: at least one whole turn from now.
  function pulse() { pulseUntil = Math.max(pulseUntil, Date.now() + TURN_MS); }

  function busy() {
    const job = window.Activity?.job;
    const fetching = !!job && job.ticker === state.ticker && FETCHING.has(job.phase);
    return !!state.ticker && (fetching || Date.now() < pulseUntil);
  }

  function tick() {
    const on = busy();
    lon = (lon + GLOBE_STEP) % 360;
    for (const [host, size] of hosts) {
      if (!host.isConnected) { hosts.delete(host); continue; }
      const card = host.querySelector(':scope > .throb');
      if (!card) continue;
      if (on) card.classList.add('updating');
      else if (still?.matches) card.classList.remove('updating');  // no turn to finish
      host.setAttribute('aria-busy', String(on));
      if (!card.classList.contains('updating') || still?.matches) continue;
      // the back's globe, turning: a frame of the Markets globe (blue when the button is selected)
      const frame = window.GlobeIcon?.frame(lon - 180, host.classList.contains('active'));
      const canvas = card.querySelector('.throb-globe');
      if (frame && canvas) {
        const dpr = Math.min(4, Math.max(1, window.devicePixelRatio || 1)), n = Math.round(size * dpr);
        if (canvas.width !== n) { canvas.width = canvas.height = n; canvas.style.width = canvas.style.height = `${size}px`; }
        const g = canvas.getContext('2d');
        g.clearRect(0, 0, n, n);
        g.drawImage(frame, 0, 0, n, n);
      }
    }
  }
  setInterval(() => { if (!document.hidden) tick(); }, FRAME_MS);

  window.Throbber = { face, pulse, busy };
})();
