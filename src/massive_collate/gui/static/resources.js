'use strict';
/* Resources view (rail icon: a chip): what the app's processes use (CPU, memory, reads and writes,
   network connections), the machine's CPU, memory and bandwidth, the disks and the data files
   (resources.py). The chart app samples every 30 s in the background; while this view is open it
   asks at the server's interval (2 s, stretched if sampling costs more than its budget), and stops
   asking when another view is shown or the page is hidden. */
(() => {
  const box = document.getElementById('viewResources');
  const { esc } = window.MassiveIcons;
  let data = null, span = 'recent', timer = null;
  const open = new Set();  // file folders shown file by file
  const sparks = new Map();  // sparkline key -> { t, values, fmt } (for its hover)
  const tip = document.createElement('div');
  tip.className = 'res-tip';
  tip.hidden = true;
  document.body.appendChild(tip);

  const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  function bytes(n) {
    if (n == null || !Number.isFinite(n)) return '–';
    let v = Math.abs(n), i = 0;
    while (v >= 1024 && i < UNITS.length - 1) { v /= 1024; i++; }
    return `${n < 0 ? '−' : ''}${i === 0 || v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${UNITS[i]}`;
  }
  const rate = (n) => (n == null ? '–' : `${bytes(n)}/s`);
  const pct = (f) => (f == null || !Number.isFinite(f) ? '–' : `${(f * 100).toFixed(0)}%`);
  const core = (f) => (f == null ? '–' : `${(f * 100).toFixed(f < 0.1 ? 1 : 0)}%`);  // of one core
  const growth = (n) => (n == null ? '–' : `${n > 0 ? '+' : ''}${bytes(n)}/h`);
  const linkText = (bps) => (!bps ? '' : bps >= 1e9 ? `${(bps / 1e9).toFixed(1)} Gb/s link` : `${Math.round(bps / 1e6)} Mb/s link`);
  const when = (iso) => (iso ? `${new Date(iso).toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })} ET` : '–');
  const clock = (s) => new Date(s * 1000).toLocaleTimeString('en-US', { timeZone: 'America/New_York', hour12: false });

  // A sparkline of a series over the range shown, scaled to `max` (or its own highest); hovering
  // shows the value at that time.
  function spark(key, values, fmt, { w = 120, h = 26, max = null } = {}) {
    sparks.set(key, { t: data.series.t, values, fmt });
    const nums = values.filter((v) => v != null);
    const head = `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" data-spark="${esc(key)}" aria-hidden="true">`;
    if (nums.length < 2) return `${head}</svg>`;
    const top = (max ?? Math.max(...nums)) || 1;
    let d = '', pen = false;
    values.forEach((v, i) => {
      if (v == null) { pen = false; return; }  // a gap (a process not running then): not joined
      const x = (i / (values.length - 1)) * (w - 2) + 1, y = h - 2 - (Math.min(v, top) / top) * (h - 4);
      d += `${pen ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
      pen = true;
    });
    return `${head}<path d="${d}"/></svg>`;
  }
  const meter = (f) => `<span class="meter${f > 0.9 ? ' warn' : ''}"><span style="width:${Math.min(100, f * 100).toFixed(1)}%"></span></span>`;
  const tile = (title, big, small, extra = '') => `<div class="tile"><h5>${title}</h5><div class="big">${big}</div><small>${small}</small>${extra}</div>`;

  async function load() {
    clearTimeout(timer);
    if (document.body.dataset.view !== 'resources' || document.hidden) return;
    try {
      data = await api('/api/resources', { span });
    } catch (err) { showError(err); }
    render();
    clearTimeout(timer);  // one loop, however many loads overlapped
    if (document.body.dataset.view === 'resources' && !document.hidden) timer = setTimeout(load, Math.max(1, data?.interval || 2) * 1000);
  }

  function processRows() {
    const s = data.series;
    return data.processes.map((p) => {
      const ser = s.processes[p.key] || { cpu: [], memory: [] };
      const c = p.connections || { established: 0, listening: 0, other: 0 };
      const ends = (p.remotes || []).map((r) => `<span class="res-remote${r.local ? ' local' : ''}" title="${esc(r.state)}">${esc(r.remote)}${r.count > 1 ? ` ×${r.count}` : ''}${r.local ? ' (this machine)' : ''}</span>`).join('');
      const more = p.remoteCount > (p.remotes || []).length ? ` (the busiest ${p.remotes.length} of ${p.remoteCount})` : '';
      return `<tr>
          <td><b>${esc(p.label)}</b>${p.self ? ' <span class="chip">this app</span>' : ''}<div class="sub" title="${esc(p.command)}">${esc(p.command || '')}</div></td>
          <td class="num">${p.pids.map(esc).join(' · ')}</td>
          <td class="num">${core(p.cpu)}</td><td>${spark(`cpu:${p.key}`, ser.cpu, core)}</td>
          <td class="num">${bytes(p.memory)}<div class="sub">${bytes(p.private)} private</div></td><td>${spark(`mem:${p.key}`, ser.memory, bytes)}</td>
          <td class="num">${rate(p.readRate)}<div class="sub">${rate(p.writeRate)} written</div></td>
          <td class="num">${c.established}${c.listening ? `<div class="sub">${c.listening} listening</div>` : ''}</td>
          <td class="num">${p.threads} · ${p.handles}</td>
          <td>${esc(when(p.started))}</td></tr>
        ${ends ? `<tr class="res-sub"><td colspan="10"><span class="p-muted small">Connected to${more}:</span> ${ends}</td></tr>` : ''}`;
    }).join('');
  }

  function fileRows(files) {
    return files.groups.map((g) => {
      const shown = open.has(g.name);
      const rows = shown ? g.files.map((f) => `<tr class="res-file"><td>${esc(f.path)}</td><td class="num">${bytes(f.size)}</td>
          <td class="num">${growth(f.growth)}</td><td>${esc(when(f.modified))}</td></tr>`).join('') : '';
      return `<tr class="has-data" data-group="${esc(g.name)}"><td>${shown ? '▾' : '▸'} <b>${esc(g.name === '.' ? '(top folder)' : `${g.name}/`)}</b>
          <span class="p-muted small">${g.count.toLocaleString()} files</span></td>
          <td class="num">${bytes(g.size)}</td><td class="num">${growth(g.growth)}</td><td></td></tr>${rows}`;
    }).join('');
  }

  function render() {
    if (document.body.dataset.view !== 'resources') return;
    if (!data) { box.innerHTML = '<div class="empty">Loading…</div>'; return; }
    sparks.clear();
    const { machine: m, network: net, connections: links, monitor: mon, files } = data;
    const s = data.series;
    const dataDisk = files?.disks.find((d) => d.holds.includes('data'));
    const grown = files?.groups.every((g) => g.growth != null) ? files.groups.reduce((a, g) => a + g.growth, 0) : null;
    const scrolled = box.scrollTop;  // drawn again every couple of seconds: the reader stays where they were
    const tiles = [
      m ? tile('CPU', pct(m.cpu), `of all ${m.cpus} cores`, spark('cpu', s.cpu, pct, { max: 1 })) : '',
      m ? tile('Memory', bytes(m.memoryUsed), `of ${bytes(m.memoryTotal)} · ${pct(m.memoryUsed / m.memoryTotal)}`,
               spark('mem', s.memory, bytes, { max: m.memoryTotal })) : '',
      net ? tile('Bandwidth', `↓ ${rate(net.in)}<span class="res-out">↑ ${rate(net.out)}</span>`,
                 `${esc(net.adapters.join(', ') || 'no adapter up')}${net.link ? ` · ${linkText(net.link)}` : ''}`,
                 `<div class="res-pair"><span>in</span>${spark('in', s.in, rate)}<span>out</span>${spark('out', s.out, rate)}</div>`) : '',
      links ? tile('Connections', String(links.app), `the app's open TCP connections · ${links.established} on the machine, ${links.listening} listening`) : '',
      dataDisk ? tile('Data disk', `${bytes(dataDisk.free)} free`, `of ${bytes(dataDisk.total)} · ${esc(dataDisk.path)}`, meter(dataDisk.used / dataDisk.total)) : '',
      files ? tile('Data files', bytes(files.total), `${files.count.toLocaleString()}${files.capped ? '+' : ''} files · ${grown == null ? 'growth after a minute' : growth(grown)}`) : '',
    ].join('');
    box.innerHTML = `
      <div class="view-head"><div><h2>Resources</h2>
        <div class="p-muted small">The app's processes, the machine, its network and the data files: sampled every ${data.cadence.viewing} s
          while this view is open, every ${data.cadence.background} s otherwise. This monitor: ${core(mon.cpu)} of a core
          (its limit ${core(mon.limit)}) and ${bytes(mon.memory)} of samples (at most ${bytes(mon.memoryCap)}).</div>
        ${mon.error ? `<div class="down small">Last sample failed: ${esc(mon.error)}</div>` : ''}
        ${data.windows ? '' : '<div class="p-muted small">Process details, the machine and its bandwidth are read from Windows: here, this process, the disks and the files only.</div>'}</div>
        <div class="view-actions filters">${[['recent', 'Last 10 min'], ['day', 'Last day']].map(([k, l]) => `<button data-span="${k}" class="${k === span ? 'active' : ''}">${l}</button>`).join('')}</div></div>
      <div class="tiles res-tiles">${tiles}</div>
      <h3 class="res-h">Processes</h3>
      <div class="table-wrap"><table class="act-table res-table"><thead><tr><th>Program</th><th class="num">PIDs</th>
        <th class="num">CPU</th><th>of one core</th><th class="num">Memory</th><th>in RAM</th><th class="num">Reads · writes</th>
        <th class="num">Connections</th><th class="num">Threads · handles</th><th>Started</th></tr></thead>
        <tbody>${data.processes.length ? processRows() : '<tr><td colspan="10" class="empty">No sample yet.</td></tr>'}</tbody></table></div>
      <div class="p-muted small res-note">Bandwidth is the machine's, from its network adapters: Windows counts network bytes per process
        only with administrator tracing, so each program shows its connections instead. Reads and writes are its file and disk I/O.</div>
      ${files ? `<h3 class="res-h">Disks</h3>
      <div class="table-wrap"><table class="act-table res-table"><thead><tr><th>Disk</th><th>Holds</th><th></th><th class="num">Used</th>
        <th class="num">Free</th><th class="num">Size</th></tr></thead><tbody>
        ${files.disks.map((d) => `<tr><td>${esc(d.path)}</td><td>${d.holds.map(esc).join(', ')}</td><td class="res-meter">${meter(d.used / d.total)}</td>
          <td class="num">${bytes(d.used)}</td><td class="num">${bytes(d.free)}</td><td class="num">${bytes(d.total)}</td></tr>`).join('')}</tbody></table></div>
      <h3 class="res-h">Data files <span class="p-muted small">${esc(files.root)}</span></h3>
      <div class="table-wrap"><table class="act-table res-table"><thead><tr><th>Folder · file</th><th class="num">Size</th>
        <th class="num">Growth</th><th>Modified</th></tr></thead><tbody>${fileRows(files)}</tbody></table></div>
      <div class="p-muted small res-note">Growth is over the last hour (from a minute after the app started); a folder opens to its largest files.</div>` : ''}`;
    box.scrollTop = scrolled;
  }

  box.addEventListener('click', (e) => {
    const b = e.target.closest('[data-span]');
    if (b && b.dataset.span !== span) { span = b.dataset.span; load(); return; }
    const g = e.target.closest('[data-group]');
    if (g) {
      if (open.has(g.dataset.group)) open.delete(g.dataset.group); else open.add(g.dataset.group);
      render();
    }
  });
  box.addEventListener('mousemove', (e) => {
    const svg = e.target.closest('svg[data-spark]');
    const sp = svg && sparks.get(svg.dataset.spark);
    if (!sp || sp.values.length < 2) { tip.hidden = true; return; }
    const r = svg.getBoundingClientRect();
    const i = Math.max(0, Math.min(sp.values.length - 1, Math.round(((e.clientX - r.left) / r.width) * (sp.values.length - 1))));
    tip.textContent = `${clock(sp.t[i])} ET · ${sp.values[i] == null ? 'not running' : sp.fmt(sp.values[i])}`;
    tip.hidden = false;
    tip.style.left = `${Math.min(e.clientX + 12, innerWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${e.clientY + 14}px`;
  });
  box.addEventListener('mouseleave', () => { tip.hidden = true; });
  window.addEventListener('massive:view', (e) => {
    if (e.detail.view === 'resources') load(); else { clearTimeout(timer); tip.hidden = true; }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  if (document.body.dataset.view === 'resources') load();  // the page opened on this view (before this script listened)
})();
