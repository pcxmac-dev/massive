'use strict';
/* Preferences: the rail's sliders (above the profile), a pane beside the chart as the watchlist is.

     Sessions          the sessions an intraday stock chart shows (the toolbar's session picker: the
                       same setting, either one changes both)
     Time zone         the zone intraday times are read in (the switch under the price axis: the same
                       setting); with none picked, this computer's own (app.js LOCAL_ZONE)
     Session shading   each session's shade on the chart (timeaxis.js): pre-market (blue), after-hours
                       (grey) and overnight (violet). Its colour from a popup (the swatch: a palette, any
                       colour by the system's picker or its hex code, or back to the standard one), and
                       how strongly it is laid on, 0-200 % of its standard strength (100 %: the strength
                       set 2026-10-28, half the blue and violet before)

   Kept per profile, as the rest of the page's settings are (app.js prefs). */
(() => {
  const { esc } = window.MassiveIcons;
  const SHADES = [
    ['pre', 'Pre-market', '04:00-09:30, blue'],
    ['post', 'After-hours', '16:00-20:00, grey'],
    ['night', 'Overnight', '20:00-04:00 on Blue Ocean, violet'],
  ];
  // The standard colours (timeaxis.js SHADE_BASE's light ones: the dark theme's are deeper), and the palette.
  const STANDARD = { pre: '#143caa', post: '#787b86', night: '#6e3caa' };
  const PALETTE = ['#143caa', '#2962ff', '#00bcd4', '#26a69a', '#4caf50', '#cddc39', '#f0a030', '#ff7043',
                   '#ef5350', '#ec407a', '#ab47bc', '#6e3caa', '#7e57c2', '#787b86', '#9e9e9e', '#ffffff'];
  const HEX = /^#[0-9a-f]{6}$/i;
  const clamp = (v) => (Number.isFinite(v) ? Math.min(200, Math.max(0, Math.round(v))) : 100);
  const saved = prefs.get('shade', {});
  state.shade = Object.fromEntries(SHADES.map(([k]) => [k, clamp(Number(saved?.[k] ?? 100))]));
  const savedColors = prefs.get('shadeColor', {});
  // a colour picked, or null: the standard one (each theme its own)
  state.shadeColor = Object.fromEntries(SHADES.map(([k]) => [k, HEX.test(savedColors?.[k] || '') ? savedColors[k].toLowerCase() : null]));
  const colorOf = (k) => state.shadeColor[k] || STANDARD[k];

  const shown = () => document.body.dataset.pane === 'prefs';

  function render() {
    const box = $('pPrefs');
    if (!box) return;
    const fixed = !!state.meta?.aroundTheClock;  // fx and futures: no sessions to pick
    box.innerHTML = `
      <h3>Preferences</h3>
      <div class="p-sub">Sessions and time</div>
      <label class="pref-row pref-session" title="${esc($('sessionSelect').title)}"><span>Sessions shown</span>
        <select id="prefSession"${fixed ? ' disabled' : ''}>${[...$('sessionSelect').options].map((o) =>
          `<option value="${esc(o.value)}"${o.value === state.session ? ' selected' : ''}>${esc(o.textContent)}</option>`).join('')}
        </select></label>
      ${fixed ? `<div class="p-muted small">${esc(state.ticker || 'This symbol')} trades around the clock: no sessions to pick.</div>` : ''}
      <label class="pref-row pref-session" title="The zone the time axis, the crosshair, the current time's clock and the panels read intraday times in (the switch under the price axis: the same setting). Days, weeks and months keep the market's own dates."><span>Time zone</span>
        <select id="prefZone">${TIMEZONES.map((z) =>
          `<option value="${esc(z)}"${z === state.timezone ? ' selected' : ''}>${esc(z === 'exchange'
            ? `Exchange: New York (${gmtLabel(TZ)})` : zoneName(z))}${z === LOCAL_ZONE ? ' · this computer' : ''}</option>`).join('')}
        </select></label>
      <div class="p-sub">Session shading <span class="p-muted small">on intraday stock charts: its colour, and its strength (100 % standard)</span></div>
      ${SHADES.map(([k, name, what]) => `
        <div class="pref-row pref-shade" title="${esc(`${name}: ${what}`)}">
          <span><button type="button" class="pref-swatch" data-color="${k}" style="background:${colorOf(k)}"
            aria-haspopup="dialog" aria-expanded="false" title="${esc(name)}'s colour${state.shadeColor[k] ? ` (${colorOf(k)})` : ' (standard)'}: click to pick"></button>${esc(name)}</span>
          <input type="range" min="0" max="200" step="5" data-shade="${k}" value="${state.shade[k]}" aria-label="${esc(name)} shading strength">
          <output>${state.shade[k]} %</output></div>`).join('')}
      <button type="button" id="prefShadeReset" class="pref-reset">Standard colours and strength</button>`;
  }

  // One shade's strength or colour changed: kept, and the chart's sessions drawn again (a layout, no data read).
  function setShade(k, v) {
    state.shade[k] = clamp(v);
    prefs.set('shade', state.shade);
    window.TimeAxis?.redraw();
  }

  function setColor(k, hex) {
    state.shadeColor[k] = hex && HEX.test(hex) && hex.toLowerCase() !== STANDARD[k] ? hex.toLowerCase() : null;
    prefs.set('shadeColor', state.shadeColor);
    window.TimeAxis?.redraw();
    const sw = $('pPrefs').querySelector(`.pref-swatch[data-color="${k}"]`);
    if (sw) sw.style.background = colorOf(k);
    if (pop?.dataset.color === k) syncPop(k);
  }

  // -- the colour popup: under its swatch, in the side panel ---------------------------------
  let pop = null;
  function openPop(k, swatch) {
    closePop();
    const name = SHADES.find(([x]) => x === k)[1];
    pop = document.createElement('div');
    pop.className = 'pref-pop';
    pop.dataset.color = k;
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', `${name}'s colour`);
    pop.innerHTML = `
      <div class="pref-pop-head">${esc(name)}</div>
      <div class="pref-pop-grid">${PALETTE.map((c) =>
        `<button type="button" data-pick="${c}" style="background:${c}" title="${c}${Object.values(STANDARD).includes(c) ? ' (a standard colour)' : ''}" aria-label="${c}"></button>`).join('')}</div>
      <div class="pref-pop-row">
        <input type="color" class="pref-pop-any" aria-label="Any colour (the system's picker)" title="Any colour: the system's picker">
        <input type="text" class="pref-pop-hex" maxlength="7" spellcheck="false" aria-label="Colour as #rrggbb">
      </div>
      <div class="pref-pop-row">
        <button type="button" data-pick="" class="pref-reset">Standard</button>
        <button type="button" data-done class="pref-reset">Done</button>
      </div>`;
    swatch.closest('.pref-shade').append(pop);
    swatch.setAttribute('aria-expanded', 'true');
    syncPop(k);
    (pop.querySelector(`.pref-pop-grid [data-pick="${colorOf(k)}"]`) || pop.querySelector('.pref-pop-grid [data-pick]')).focus();
  }

  function syncPop(k) {
    if (!pop) return;
    const c = colorOf(k);
    pop.querySelector('.pref-pop-any').value = c;
    const hex = pop.querySelector('.pref-pop-hex');
    if (document.activeElement !== hex) hex.value = c;
    for (const b of pop.querySelectorAll('.pref-pop-grid [data-pick]')) b.classList.toggle('on', b.dataset.pick === c);
  }

  function closePop(focusSwatch = false) {
    if (!pop) return;
    const k = pop.dataset.color;
    pop.remove();
    pop = null;
    const sw = $('pPrefs').querySelector(`.pref-swatch[data-color="${k}"]`);
    sw?.setAttribute('aria-expanded', 'false');
    if (focusSwatch) sw?.focus();
  }

  $('pPrefs').addEventListener('input', (e) => {
    if (e.target.classList.contains('pref-pop-any')) { setColor(pop.dataset.color, e.target.value); return; }  // live
    if (e.target.classList.contains('pref-pop-hex')) {
      const v = e.target.value.trim();
      if (HEX.test(v)) setColor(pop.dataset.color, v);
      return;
    }
    const k = e.target.dataset?.shade;
    if (!k) return;
    setShade(k, Number(e.target.value));
    e.target.nextElementSibling.textContent = `${state.shade[k]} %`;
  });
  $('pPrefs').addEventListener('change', (e) => {
    if (e.target.id === 'prefZone') window.setChartTimezone(e.target.value);  // as the switch under the price axis
    if (e.target.id !== 'prefSession') return;
    $('sessionSelect').value = e.target.value;  // the toolbar's picker does the rest (app.js)
    $('sessionSelect').dispatchEvent(new Event('change'));
  });
  $('pPrefs').addEventListener('click', (e) => {
    const swatch = e.target.closest('.pref-swatch[data-color]');
    if (swatch) {
      if (pop?.dataset.color === swatch.dataset.color) closePop(true);
      else openPop(swatch.dataset.color, swatch);
      return;
    }
    const pick = e.target.closest('[data-pick]');
    if (pick && pop) { setColor(pop.dataset.color, pick.dataset.pick || null); return; }
    if (e.target.closest('[data-done]')) { closePop(true); return; }
    if (e.target.id !== 'prefShadeReset') return;
    for (const [k] of SHADES) { setShade(k, 100); setColor(k, null); }
    closePop();
    render();
  });
  // the popup goes on a click elsewhere, or Escape
  document.addEventListener('pointerdown', (e) => {
    if (pop && !pop.contains(e.target) && !e.target.closest('.pref-swatch')) closePop();
  });
  document.addEventListener('keydown', (e) => { if (pop && e.key === 'Escape') { e.stopPropagation(); closePop(true); } }, true);
  // the toolbar's picker changed, a symbol opened (around the clock or not), the pane shown
  const again = () => { if (shown()) { closePop(); render(); } };
  $('sessionSelect').addEventListener('change', again);
  window.addEventListener('massive:symbol', again);
  window.addEventListener('massive:timezone', again);  // picked under the price axis
  window.addEventListener('massive:view', (e) => { if (e.detail?.pane === 'prefs') render(); else closePop(); });
  window.Prefs = { render, get shade() { return { ...state.shade }; }, get colors() { return { ...state.shadeColor }; } };
})();
