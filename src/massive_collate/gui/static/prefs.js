'use strict';
/* Preferences: the rail's sliders (above the profile), a pane beside the chart as the watchlist is.

     Sessions          the sessions an intraday stock chart shows (the toolbar's session picker: the
                       same setting, either one changes both)
     Time zone         the zone intraday times are read in (the switch under the price axis: the same
                       setting); with none picked, this computer's own (app.js LOCAL_ZONE)
     Session shading   how strongly each session is shaded on the chart (timeaxis.js): pre-market
                       (blue), after-hours (grey) and overnight (violet), each 0-200 % of its standard
                       strength (100 %: the strength set 2026-10-28, half the blue and violet before)

   Kept per profile, as the rest of the page's settings are (app.js prefs). */
(() => {
  const { esc } = window.MassiveIcons;
  const SHADES = [
    ['pre', 'Pre-market', '04:00-09:30, blue'],
    ['post', 'After-hours', '16:00-20:00, grey'],
    ['night', 'Overnight', '20:00-04:00 on Blue Ocean, violet'],
  ];
  const clamp = (v) => (Number.isFinite(v) ? Math.min(200, Math.max(0, Math.round(v))) : 100);
  const saved = prefs.get('shade', {});
  state.shade = Object.fromEntries(SHADES.map(([k]) => [k, clamp(Number(saved?.[k] ?? 100))]));

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
      <div class="p-sub">Session shading <span class="p-muted small">on intraday stock charts, 100 % standard</span></div>
      ${SHADES.map(([k, name, what]) => `
        <label class="pref-row pref-shade" title="${esc(`${name}: ${what}`)}"><span><i class="pref-swatch ${k}"></i>${esc(name)}</span>
          <input type="range" min="0" max="200" step="5" data-shade="${k}" value="${state.shade[k]}" aria-label="${esc(name)} shading">
          <output>${state.shade[k]} %</output></label>`).join('')}
      <button type="button" id="prefShadeReset" class="pref-reset">Standard strength</button>`;
  }

  // One shade changed: kept, and the chart's sessions drawn again (a layout, no data read).
  function setShade(k, v) {
    state.shade[k] = clamp(v);
    prefs.set('shade', state.shade);
    window.TimeAxis?.redraw();
  }

  $('pPrefs').addEventListener('input', (e) => {
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
    if (e.target.id !== 'prefShadeReset') return;
    for (const [k] of SHADES) setShade(k, 100);
    render();
  });
  // the toolbar's picker changed, a symbol opened (around the clock or not), the pane shown
  $('sessionSelect').addEventListener('change', () => { if (shown()) render(); });
  window.addEventListener('massive:symbol', () => { if (shown()) render(); });
  window.addEventListener('massive:timezone', () => { if (shown()) render(); });  // picked under the price axis
  window.addEventListener('massive:view', (e) => { if (e.detail?.pane === 'prefs') render(); });
  window.Prefs = { render, get shade() { return { ...state.shade }; } };
})();
