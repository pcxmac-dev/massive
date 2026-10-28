'use strict';
// The login page: the profiles (the most recently signed in first), a click signs in (no
// passwords) and opens the app; a new one starts with an empty watchlist, no markups and its own
// preferences. The icon: Leonardo's Vitruvian Man (icons.js).
(() => {
  const { icon, esc } = window.MassiveIcons;
  const $ = (id) => document.getElementById(id);
  const WRITE = { 'Content-Type': 'application/json', 'X-Massive-Charts': '1' };

  $('loginIcon').innerHTML = icon('vitruvian', 140);

  const ago = (iso) => {
    if (!iso) return 'not signed in yet';
    const s = (Date.now() - Date.parse(iso)) / 1000;
    if (s < 3600) return `here ${Math.max(1, Math.round(s / 60))} min ago`;
    if (s < 86400) return `here ${Math.round(s / 3600)} h ago`;
    return `here ${Math.round(s / 86400)} days ago`;
  };

  function failed(err) {
    $('loginError').textContent = String(err.message || err);
  }

  async function post(path, body) {
    const res = await fetch(path, { method: 'POST', headers: WRITE, body: JSON.stringify(body) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }

  async function signIn(id) {
    await post('/api/profiles/sign-in', { id });
    location.replace('/');  // the app, as this profile
  }

  async function load() {
    try {
      const res = await fetch('/api/profiles');
      const { profiles = [], current } = await res.json();
      $('profileList').innerHTML = profiles.map((p) => `
        <button type="button" role="listitem" data-id="${p.id}" class="login-profile${p.id === current ? ' current' : ''}">
          <span class="login-avatar">${esc(p.name.slice(0, 1).toUpperCase())}</span>
          <span class="login-name">${esc(p.name)}</span>
          <span class="login-seen p-muted">${esc(ago(p.seen_at))}</span>
        </button>`).join('');
    } catch (err) { failed(err); }
  }

  $('profileList').addEventListener('click', (e) => {
    const b = e.target.closest('[data-id]');
    if (b) signIn(Number(b.dataset.id)).catch(failed);
  });
  $('newProfile').addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = $('newName').value.trim();
    if (!name) return;
    try {
      const made = await post('/api/profiles', { name });
      await signIn(made.id);
    } catch (err) { failed(err); }
  });
  load();
})();
