// The Live rail icon comes alive while the open symbol is streaming, in one of two effects
// (chosen in the Live view, kept per viewer; the heartbeat unless another is chosen):
//  - ROYGBIV pulse: a red dot beating at the centre; rings spread out from it through red,
//    orange, yellow, green, blue, indigo and violet, brighter where they cross the icon's
//    disc and ring, darker elsewhere, fading to the background just past the outer ring.
//  - Heartbeat: an ECG trace drawn by a pen just ahead of the centre, flowing back and
//    fading once it passes the ring.
// Drawn on a canvas over the icon's own 24-unit box, never beyond it. With reduced motion
// asked for, one still frame.
(() => {
  'use strict';

  const EFFECTS = { heartbeat: 'Heartbeat', pulse: 'ROYGBIV pulse', off: 'Off' };
  const ROYGBIV = ['#ff2a2a', '#ff8c1a', '#ffe11a', '#2bd451', '#2f7bff', '#4b33d6', '#9b3cff'];
  const SIZE = 32;  // the icon is drawn 32 px wide (the rail's icon size) from a 24-unit box
  const OUTER = 9, DISC = 4, BAND = 1, EDGE = 10.8;  // the icon's ring and disc (units); fades end at EDGE
  const RED = '#ff3b3b';
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)');
  let effect = EFFECTS[prefs.get('liveEffect', 'heartbeat')] ? prefs.get('liveEffect', 'heartbeat') : 'heartbeat';
  let active = false, frame = 0, canvas = null, ctx = null, dpr = 1;
  const t0 = performance.now();

  function mount() {
    const button = document.getElementById('railLive');
    if (!button) return null;
    canvas = button.querySelector('canvas.live-fx');
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.className = 'live-fx';
      canvas.setAttribute('aria-hidden', 'true');
      button.append(canvas);
    }
    dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(SIZE * dpr)) {
      canvas.width = canvas.height = Math.round(SIZE * dpr);
      canvas.style.width = canvas.style.height = `${SIZE}px`;
    }
    ctx = canvas.getContext('2d');
    return button;
  }

  // Whether the effect runs: the open symbol is live, and an effect is chosen.
  function update(isLive) {
    const button = mount();
    if (!button) return;
    active = !!isLive && effect !== 'off';
    button.classList.toggle('fx', active);
    button.dataset.effect = effect;  // the heartbeat dims the icon behind its trace
    if (active && !frame) frame = requestAnimationFrame(draw);
    if (!active) {
      cancelAnimationFrame(frame);
      frame = 0;
      ctx?.clearRect(0, 0, canvas.width, canvas.height);
    }
  }

  function set(name) {
    if (!EFFECTS[name]) return;
    effect = name;
    prefs.set('liveEffect', name);
    update(typeof isLive === 'function' && isLive());
  }

  function draw(now) {
    frame = 0;
    if (!active || !ctx) return;
    const t = reduced?.matches ? 0.4 : (now - t0) / 1000;
    ctx.setTransform(dpr * SIZE / 24, 0, 0, dpr * SIZE / 24, 0, 0);
    ctx.clearRect(0, 0, 24, 24);
    ctx.translate(12, 12);
    if (effect === 'pulse') pulse(t);
    else heartbeat(t);
    if (!reduced?.matches) frame = requestAnimationFrame(draw);
  }

  // 1 inside the ring, falling to 0 at EDGE: past the outer line everything fades to the background.
  const fadeOut = (r) => (r <= OUTER ? 1 : Math.max(0, 1 - (r - OUTER) / (EDGE - OUTER)));

  function mix(a, b, f) {
    const p = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
    const [x, y] = [p(a), p(b)];
    return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * f)).join(',')})`;
  }

  function rainbow(r) {  // red at the centre to violet at the outer ring
    const x = Math.min(1, r / OUTER) * (ROYGBIV.length - 1);
    const i = Math.min(ROYGBIV.length - 2, Math.floor(x));
    return mix(ROYGBIV[i], ROYGBIV[i + 1], x - i);
  }

  function clipToIcon() {  // the icon's filled disc and its outer ring's band (even-odd)
    ctx.beginPath();
    ctx.arc(0, 0, DISC, 0, Math.PI * 2);
    ctx.moveTo(OUTER + BAND, 0); ctx.arc(0, 0, OUTER + BAND, 0, Math.PI * 2);
    ctx.moveTo(OUTER - BAND, 0); ctx.arc(0, 0, OUTER - BAND, 0, Math.PI * 2);
    ctx.clip('evenodd');
  }

  const PULSE = 1.8;  // seconds for a ring to travel from the centre to the edge
  const RINGS = 3;    // rings out at once

  function pulse(t) {
    for (let k = 0; k < RINGS; k++) {
      const r = ((t / PULSE + k / RINGS) % 1) * EDGE;
      const color = rainbow(r), fade = fadeOut(r);
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.3 * fade;  // dim over the empty parts of the icon
      ctx.lineWidth = 1.3;
      ctx.beginPath(); ctx.arc(0, 0, Math.max(0.1, r), 0, Math.PI * 2); ctx.stroke();
      ctx.save();
      clipToIcon();  // bright where it crosses the disc and the ring
      ctx.globalAlpha = fade;
      ctx.lineWidth = 2;
      ctx.shadowColor = color; ctx.shadowBlur = 3;
      ctx.beginPath(); ctx.arc(0, 0, Math.max(0.1, r), 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
    }
    // The red dot, beating as each ring leaves.
    const beat = Math.pow(0.5 + 0.5 * Math.cos(((t / PULSE) * RINGS % 1) * Math.PI * 2), 3);
    const g = ctx.createRadialGradient(0, 0, 0, 0, 0, DISC);
    g.addColorStop(0, '#fff2f2'); g.addColorStop(0.35, RED); g.addColorStop(1, 'rgba(255,59,59,0)');
    ctx.globalAlpha = 0.65 + 0.35 * beat;
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(0, 0, 2.6 + 1.4 * beat, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
  }

  const BEAT = 1.1;   // seconds per heartbeat
  const SPEED = 10;   // units the trace moves back per second
  const PEN = 1.2;    // the pen, just ahead of the centre

  function ecg(phase) {  // the trace's height in a beat (0..1): P wave, QRS complex, T wave
    const g = (c, w, a) => a * Math.exp(-((phase - c) ** 2) / (2 * w * w));
    return -(g(0.16, 0.03, 1.1) - g(0.31, 0.012, 1.3) + g(0.34, 0.013, 6.8) - g(0.37, 0.012, 2.3) + g(0.6, 0.05, 1.8));
  }

  function heartbeat(t) {
    const steps = 96, length = PEN + EDGE;
    ctx.lineWidth = 1.6; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.strokeStyle = '#ff7a7a'; ctx.shadowColor = RED; ctx.shadowBlur = 3;
    let px = PEN, py = ecg((((t / BEAT) % 1) + 1) % 1);
    for (let i = 1; i <= steps; i++) {  // from the pen back across the circle: older the further back
      const s = (i / steps) * length;
      const x = PEN - s;
      const y = ecg(((((t - s / SPEED) / BEAT) % 1) + 1) % 1);
      ctx.globalAlpha = fadeOut(Math.hypot(x, y)) * (1 - 0.4 * (s / length));
      ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(x, y); ctx.stroke();
      px = x; py = y;
    }
    ctx.shadowBlur = 0;
    ctx.globalAlpha = 1;
    ctx.fillStyle = '#fff2f2';  // the pen
    ctx.beginPath(); ctx.arc(PEN, ecg((((t / BEAT) % 1) + 1) % 1), 1.1, 0, Math.PI * 2); ctx.fill();
  }

  window.LiveIcon = { update, set, get effect() { return effect; }, EFFECTS };
})();
