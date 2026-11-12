'use strict';

/* Photos library browser — talks to the FastAPI backend in app/server.py. */

const $ = (sel) => document.querySelector(sel);

const state = {
  lib: null,
  view: 'all',        // all | favorite | video | live | screenshot | album:N
  albumId: null,
  query: '',
  sort: 'newest',
  year: '',
  size: 's',
  items: [],
  total: 0,
  offset: 0,
  limit: 300,
  loading: false,
  done: false,
  viewerIndex: -1,
  timeline: {},
  lastMonth: '',
  aiQuery: '',
};

const PAGE = 300;

/**
 * Media URLs carry the library token. Asset ids are per-library database keys,
 * so id 846 is a different photo in a different library - without the token the
 * browser's immutable cache would keep serving the previous library's images
 * after a switch.
 */
function mediaUrl(kind, id, extra) {
  const parts = [];
  if (extra) parts.push(extra);
  if (state.lib && state.lib.token) parts.push(`lib=${state.lib.token}`);
  const q = parts.length ? `?${parts.join('&')}` : '';
  return `/api/${kind}/${id}${q}`;
}

/* ------------------------------------------------------------- helpers */

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, isErr) {
  const el = $('#status');
  el.textContent = msg;
  el.classList.remove('hidden');
  el.classList.toggle('err', !!isErr);
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), isErr ? 7000 : 2600);
}

function fmtDuration(sec) {
  if (!sec) return '';
  const s = Math.round(sec);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return m > 0 ? `${m}:${String(r).padStart(2, '0')}` : `0:${String(r).padStart(2, '0')}`;
}

function fmtBytes(n) {
  if (!n) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}

function fmtDate(iso) {
  if (!iso) return 'Unknown date';
  const d = new Date(iso);
  if (isNaN(d)) return 'Unknown date';
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

function fmtShutter(v) {
  if (!v) return null;
  if (v >= 1) return `${v.toFixed(1)}s`;
  return `1/${Math.round(1 / v)}s`;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/* ---------------------------------------------------------- library UI */

/**
 * Switch the UI to a library. All per-library state is reset here so nothing
 * from the previous library leaks in and blanks the new one - a stale year or
 * search term would filter the new library down to zero items.
 */
async function applyLibrary(info) {
  state.lib = info;
  state.query = '';
  state.year = '';
  state.albumId = null;
  state.view = 'all';
  state.items = [];
  state.total = 0;
  state.offset = 0;
  state.done = false;
  state.loading = false;
  state.timeline = {};
  state.lastMonth = '';
  state.viewerIndex = -1;
  closeViewer();

  // Clear the controls that carried the old library's filters.
  $('#search').value = '';
  $('#grid').innerHTML = '';

  $('#libName').textContent = info.name;
  $('#dropzone').classList.add('hidden');
  $('#scroller').classList.remove('hidden');

  renderSidebar(info);
  renderYears(info.stats.years || {}, false);
  (info.warnings || []).forEach((w) => toast(w, true));

  await setView('all');
  refreshAiBox();
}

async function openLibrary(path) {
  toast('Opening library…');
  try {
    const body = path ? { path } : {};
    const info = await api('/api/library/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    await applyLibrary(info);
    toast(`${info.stats.visible} items loaded`);
  } catch (err) {
    toast(err.message, true);
  }
}

function renderSidebar(info) {
  const s = info.stats;
  $('#cAll').textContent = s.visible;
  $('#cFav').textContent = s.favorites || '';
  $('#cVid').textContent = s.videos || '';
  $('#cLive').textContent = s.live || '';
  $('#cShot').textContent = s.screenshots || '';

  $('#libStats').innerHTML =
    `${s.visible} items · ${fmtBytes(s.sizeBytes)}<br>` +
    (s.schemaVersion ? `schema ${s.schemaVersion}` : '') +
    (s.missingOriginals ? `<br>${s.missingOriginals} missing originals` : '');

  const list = $('#albumList');
  list.innerHTML = '';
  const albums = (info.albums || []).filter((a) => a.kind !== 'folder');
  const folders = (info.albums || []).filter((a) => a.kind === 'folder');

  if (!albums.length && !folders.length) {
    const p = el('div', 'album-empty', 'This library has no user albums.');
    list.appendChild(p);
    return;
  }

  [...folders, ...albums].forEach((a) => {
    const item = el('a', 'nav-item');
    item.href = '#';
    item.dataset.view = `album:${a.id}`;
    item.appendChild(el('span', 'ico', a.kind === 'folder' ? '▸' : '▤'));
    const label = el('span', 'nav-text', a.title);
    item.appendChild(label);
    if (a.count) item.appendChild(el('em', null, a.count));
    item.addEventListener('click', (e) => {
      e.preventDefault();
      setView(`album:${a.id}`);
    });
    list.appendChild(item);
  });
}

function renderYears(years, keepSelection = true) {
  const sel = $('#year');
  const cur = keepSelection ? sel.value : '';
  sel.innerHTML = '<option value="">All years</option>';
  Object.keys(years).forEach((y) => {
    const o = el('option', null, `${y} (${years[y]})`);
    o.value = y;
    sel.appendChild(o);
  });
  // A year from the previous library may not exist here; fall back to "all".
  if (cur && years && Object.prototype.hasOwnProperty.call(years, cur)) {
    sel.value = cur;
  } else {
    sel.value = '';
  }
  return sel.value;
}

/* ------------------------------------------------------------- browsing */

function setView(view) {
  state.view = view;
  state.albumId = view.startsWith('album:') ? Number(view.split(':')[1]) : null;
  document.querySelectorAll('.nav-item').forEach((n) => {
    n.classList.toggle('active', n.dataset.view === view);
  });

  const titles = {
    all: 'All Photos', favorite: 'Favorites', video: 'Videos',
    live: 'Live Photos', screenshot: 'Screenshots',
    'ai-search': 'Search by content',
    'ai-picks': 'Best picks',
    'ai-portrait': 'Best portraits',
  };
  if (state.albumId) {
    const alb = (state.lib.albums || []).find((a) => a.id === state.albumId);
    $('#title').textContent = alb ? alb.title : 'Album';
  } else {
    $('#title').textContent = titles[view] || 'Photos';
  }

  const isAi = view.startsWith('ai-');
  $('#aiBar').classList.toggle('hidden', view !== 'ai-search');
  if (view === 'ai-search') $('#aiQuery').focus();

  reload();
}

function kindParam() {
  return ['favorite', 'video', 'live', 'screenshot'].includes(state.view) ? state.view : 'all';
}

function buildQuery(offset) {
  const p = new URLSearchParams();
  if (state.albumId) p.set('album', String(state.albumId));
  p.set('kind', kindParam());
  p.set('sort', state.sort);
  if (state.query) p.set('q', state.query);
  if (state.year) p.set('year', state.year);
  p.set('offset', String(offset));
  p.set('limit', String(PAGE));
  return p.toString();
}

async function reload() {
  if (!state.lib) return;
  state.items = [];
  state.offset = 0;
  state.done = false;
  state.timeline = {};
  state.lastMonth = '';
  $('#grid').innerHTML = '';
  $('#empty').classList.add('hidden');

  if (state.view.startsWith('ai-')) {
    await loadAi();
    return;
  }
  await loadMore(true);
  await loadTimeline();
}

/* ------------------------------------------------------------ AI views */

async function loadAi() {
  const grid = $('#grid');
  $('#subtitle').textContent = '';
  $('#aiHint').textContent = '';

  const status = await api('/api/ai/status').catch(() => null);
  if (!status || !status.available || !status.available.clip) {
    showEmpty('The AI extras are not installed. Run: pip install -r requirements-ai.txt');
    return;
  }

  const indexed = status.index && status.index.count > 0;
  if (!indexed) {
    showEmpty('No index for this library yet. Use “Build index” in the sidebar — '
      + 'it is a one-time pass and writes nothing into the library.');
    return;
  }

  if (state.view === 'ai-search') {
    const q = state.aiQuery.trim();
    if (!q) {
      showEmpty('Type a description above to search the photo contents.');
      return;
    }
    $('#aiHint').textContent = 'searching…';
    let data;
    try {
      data = await api(`/api/ai/search?q=${encodeURIComponent(q)}&limit=200`);
    } catch (err) {
      showEmpty(err.message);
      $('#aiHint').textContent = '';
      return;
    }
    state.items = data.items;
    state.done = true;
    renderTiles(data.items, { scores: true });
    $('#subtitle').textContent = `${data.count} result${data.count === 1 ? '' : 's'} for “${q}”`;
    // The score is a ranking signal, not a confidence: CLIP will still return
    // look-alikes for a concept the library does not contain.
    const best = data.items.length ? data.items[0].score : 0;
    $('#aiHint').textContent = data.count
      ? `best match ${(best * 100).toFixed(0)}/100 · lower scores may be look-alikes`
      : 'nothing scored above the threshold';
    $('#empty').classList.toggle('hidden', data.count > 0);
    return;
  }

  // Best picks / best portraits
  const portrait = state.view === 'ai-portrait';
  let data;
  try {
    data = await api(`/api/ai/picks?limit=300${portrait ? '&portrait_only=true' : ''}`);
  } catch (err) {
    showEmpty(err.message);
    return;
  }
  state.items = data.items;
  state.done = true;
  renderTiles(data.items, { scores: true, picks: true });
  $('#subtitle').textContent =
    `${data.count} of ${data.total} scored${portrait ? ' · portraits' : ''}`;
  $('#empty').classList.toggle('hidden', data.count > 0);
}

function showEmpty(msg) {
  const el0 = $('#empty');
  el0.textContent = msg;
  el0.classList.remove('hidden');
  $('#subtitle').textContent = '';
}

async function loadTimeline() {
  try {
    const p = new URLSearchParams();
    if (state.albumId) p.set('album', String(state.albumId));
    const data = await api('/api/timeline?' + p.toString());
    state.timeline = data.years || {};
    renderScrubber();
  } catch (_) { /* non-critical */ }
}

function renderScrubber() {
  const bar = $('#scrubber');
  const years = Object.keys(state.timeline || {}).sort().reverse();
  bar.innerHTML = '';
  if (years.length < 2) {
    bar.classList.add('hidden');
    return;
  }
  bar.classList.remove('hidden');
  years.forEach((y) => {
    const b = el('div', 'scrub-item', y);
    b.dataset.year = y;
    b.addEventListener('click', () => jumpToYear(y));
    bar.appendChild(b);
  });
}

async function jumpToYear(year) {
  // Load forward until the year's first month heading exists, then scroll to it.
  // Images above keep loading and change layout height, so the scroll is
  // repeated on the next frames until the heading actually sticks.
  let guard = 0;
  while (guard++ < 40) {
    const hit = document.querySelector(`.month-head[data-month^="${year}-"]`);
    if (hit) {
      await settleScroll(hit);
      return;
    }
    if (state.done) return;
    await loadMore(false);
  }
}

function settleScroll(target) {
  return new Promise((resolve) => {
    let attempts = 0;
    const tick = () => {
      target.scrollIntoView({ block: 'start' });
      attempts += 1;
      const top = target.getBoundingClientRect().top;
      // Within a few px of the container top means it landed.
      if (attempts >= 3 && top >= 0 && top < 24) return resolve();
      if (attempts > 14) return resolve();
      requestAnimationFrame(() => setTimeout(tick, 90));
    };
    tick();
  });
}

async function loadMore(reset) {
  if (state.loading || state.done) return;
  state.loading = true;
  try {
    const data = await api('/api/assets?' + buildQuery(state.offset));
    state.total = data.total;
    if (reset) {
      state.items = [];
      $('#grid').innerHTML = '';
    }
    state.items.push(...data.items);
    appendTiles(data.items, reset ? 0 : state.offset);
    state.offset += data.items.length;
    if (state.offset >= data.total || data.items.length === 0) state.done = true;

    $('#subtitle').textContent =
      `${data.total} item${data.total === 1 ? '' : 's'}` +
      (state.query ? ` · “${state.query}”` : '') +
      (state.year ? ` · ${state.year}` : '');
    $('#empty').classList.toggle('hidden', state.total > 0);
    updateTimelineLabel();
  } catch (err) {
    toast(err.message, true);
    state.done = true;
  } finally {
    state.loading = false;
  }
}

function appendTiles(items, startIndex) {
  renderTiles(items, { offset: startIndex });
}

function renderTiles(items, opts) {
  const { offset = 0, scores = false, picks = false } = opts || {};
  const grid = $('#grid');
  const frag = document.createDocumentFragment();
  items.forEach((it, i) => {
    const globalIndex = offset + i;

    // Photos groups by month; emit a sticky heading whenever the month changes.
    // AI result lists are ranked, so headings would be meaningless there.
    if (!scores) {
      const monthKey = it.date ? it.date.slice(0, 7) : '';
      if (monthKey && monthKey !== state.lastMonth) {
        state.lastMonth = monthKey;
        frag.appendChild(monthHeading(it.date));
      }
    }

    const tile = el('div', 'tile');
    tile.dataset.index = globalIndex;
    tile.dataset.date = it.date || '';
    tile.title = `${it.name}\n${fmtDate(it.date)}`;
    if (picks && typeof it.score === 'number' && it.score >= 0.75) {
      tile.classList.add('pick-top');
    }

    const img = document.createElement('img');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.alt = it.name || '';
    img.src = mediaUrl('thumb', it.id, `size=${state.size}`);
    img.addEventListener('load', () => img.classList.add('loaded'), { once: true });
    img.addEventListener('error', () => img.classList.add('loaded'), { once: true });
    tile.appendChild(img);

    if (scores && typeof it.score === 'number') {
      const s = el('div', 'score' + (it.score >= 0.7 ? ' hi' : ''),
                   Math.round(it.score * 100));
      s.title = `AI score ${(it.score * 100).toFixed(0)}/100`;
      tile.appendChild(s);
    }

    const badges = [];
    if (it.kind === 'live') badges.push('<span class="live-dot">◉</span>');
    if (it.kind === 'video') badges.push('<span>▶</span>');
    if (badges.length) {
      const b = el('div', 'badges');
      b.innerHTML = badges.join('');
      tile.appendChild(b);
    }
    if (it.kind === 'video' && it.duration) {
      tile.appendChild(el('div', 'dur', fmtDuration(it.duration)));
    }
    if (it.favorite) tile.appendChild(el('div', 'fav', '♥'));

    tile.addEventListener('click', () => openViewer(globalIndex));
    frag.appendChild(tile);
  });
  grid.appendChild(frag);
}

function monthHeading(iso) {
  const d = new Date(iso);
  const wrap = el('div', 'month-head');
  wrap.dataset.month = iso.slice(0, 7);
  wrap.appendChild(el('span', 'm-name', d.toLocaleString(undefined, { month: 'long' })));
  wrap.appendChild(el('span', 'm-year', String(d.getFullYear())));
  return wrap;
}

function updateTimelineLabel() {
  // The month headings carry the date context; nothing to show up top.
}

/* -------------------------------------------------------------- viewer */

async function openViewer(index) {
  state.viewerIndex = index;
  $('#viewer').classList.remove('hidden');
  await renderViewer();
}

function closeViewer() {
  $('#viewer').classList.add('hidden');
  $('#stage').innerHTML = '';
  state.viewerIndex = -1;
}

async function renderViewer() {
  const i = state.viewerIndex;
  const it = state.items[i];
  if (!it) return closeViewer();

  $('#vPrev').disabled = i <= 0;
  $('#vNext').disabled = i >= state.items.length - 1 && state.done;

  const stage = $('#stage');
  stage.innerHTML = '';

  if (it.kind === 'video') {
    const v = document.createElement('video');
    v.src = mediaUrl("video", it.id);
    v.controls = true;
    v.autoplay = true;
    v.playsInline = true;
    stage.appendChild(v);
  } else {
    const img = document.createElement('img');
    img.src = mediaUrl('preview', it.id);
    img.alt = it.name || '';
    stage.appendChild(img);

    if (it.kind === 'live') {
      const btn = el('button', 'live-toggle', '◉ Play Live');
      btn.addEventListener('click', () => {
        stage.innerHTML = '';
        const v = document.createElement('video');
        v.src = mediaUrl("video", it.id);
        v.controls = true;
        v.autoplay = true;
        v.loop = true;
        stage.appendChild(v);
      });
      const body = $('.viewer-body');
      body.querySelectorAll('.live-toggle').forEach((n) => n.remove());
      body.appendChild(btn);
    }
  }
  $('.viewer-body').querySelectorAll('.live-toggle').forEach((n) => {
    if (it.kind !== 'live') n.remove();
  });

  try {
    const d = await api(`/api/assets/${it.id}`);
    renderInfo(d);
    Object.assign(it, d);
  } catch (_) { /* detail is optional */ }
}

function renderInfo(d) {
  const info = $('#info');
  info.innerHTML = '';
  info.appendChild(el('h3', null, d.name || d.filename || 'Untitled'));
  info.appendChild(el('div', 'when', fmtDate(d.date)));

  const rows = [];
  if (d.kind === 'video' || d.kind === 'live') {
    rows.push(['Type', d.kind === 'live' ? 'Live Photo' : 'Video']);
  } else if (d.kind === 'screenshot') {
    rows.push(['Type', 'Screenshot']);
  }
  if (d.w && d.h) rows.push(['Dimensions', `${d.w} × ${d.h}`]);
  if (d.duration) rows.push(['Duration', fmtDuration(d.duration)]);
  if (d.size) rows.push(['File size', fmtBytes(d.size)]);
  if (d.camera) rows.push(['Camera', d.camera]);
  if (d.lens) rows.push(['Lens', d.lens]);
  if (d.focal) rows.push(['Focal length', `${d.focal} mm`]);
  if (d.aperture) rows.push(['Aperture', `ƒ/${d.aperture}`]);
  if (d.shutter) rows.push(['Shutter', fmtShutter(d.shutter)]);
  if (d.iso) rows.push(['ISO', d.iso]);
  if (d.fps) rows.push(['Frame rate', `${d.fps} fps`]);
  if (d.codec) rows.push(['Codec', d.codec]);
  if (d.lat != null && d.lon != null) {
    const link = document.createElement('a');
    link.href = `https://www.openstreetmap.org/?mlat=${d.lat}&mlon=${d.lon}#map=15/${d.lat}/${d.lon}`;
    link.target = '_blank';
    link.rel = 'noreferrer';
    link.textContent = `${d.lat.toFixed(5)}, ${d.lon.toFixed(5)}`;
    const dd = el('dd');
    dd.appendChild(link);
    const dl = el('dl');
    dl.appendChild(el('dt', null, 'Location'));
    dl.appendChild(dd);
    info.appendChild(dl);
  }
  if (d.tz) rows.push(['Time zone', d.tz]);
  if (d.uti) rows.push(['Format', d.uti.replace('public.', '').toUpperCase()]);
  if (d.edited) rows.push(['Edited', 'Yes (adjustments applied)']);
  if (d.folders && d.folders.length) rows.push(['Albums', d.folders.join(', ')]);

  const dl = el('dl');
  rows.forEach(([k, v]) => {
    dl.appendChild(el('dt', null, k));
    dl.appendChild(el('dd', null, String(v)));
  });
  info.appendChild(dl);

  const actions = el('div', 'actions');
  const open = el('a', null, 'Open original');
  open.href = mediaUrl("original", d.id);
  open.target = '_blank';
  open.rel = 'noreferrer';
  actions.appendChild(open);

  const dlBtn = el('a', null, 'Download original');
  dlBtn.href = mediaUrl("original", d.id, "download=true");
  actions.appendChild(dlBtn);

  if (d.path) {
    const copy = el('button', null, 'Copy file path');
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(d.path);
        toast('Path copied');
      } catch (_) {
        toast('Could not copy', true);
      }
    });
    actions.appendChild(copy);
  }
  info.appendChild(actions);
}

/* --------------------------------------------------------- AI index box */

async function refreshAiBox() {
  const box = $('#aiIndexBox');
  if (!state.lib) { box.innerHTML = ''; return; }
  let st;
  try {
    st = await api('/api/ai/status');
  } catch (_) {
    box.innerHTML = '';
    return;
  }

  if (!st.available || !st.available.clip) {
    box.innerHTML = '<div class="ai-index-note">AI extras not installed.<br>'
      + '<code>pip install -r requirements-ai.txt</code></div>';
    return;
  }

  const idx = st.index || { count: 0, scored: 0 };
  const job = st.job;

  if (job && job.state === 'running') {
    box.innerHTML = `
      <div class="ai-index-note">Indexing ${job.done}/${job.total} · ${job.percent}%${
        job.eta ? ` · ~${Math.ceil(job.eta / 60)} min left` : ''}</div>
      <div class="ai-progress"><i style="width:${job.percent}%"></i></div>
      <button class="ai-index-btn" id="aiCancelBtn" type="button">Cancel</button>`;
    $('#aiCancelBtn').addEventListener('click', async () => {
      await api('/api/ai/cancel', { method: 'POST' }).catch(() => {});
      refreshAiBox();
    });
    clearTimeout(refreshAiBox._t);
    refreshAiBox._t = setTimeout(refreshAiBox, 1500);
    return;
  }

  if (idx.count > 0) {
    const mb = idx.sizeBytes ? (idx.sizeBytes / 1048576).toFixed(1) : '0';
    const warn = (job && job.state === 'done_with_errors' && job.failed)
      ? `<div class="ai-index-note" style="color:#ffb3bb">${job.failed} photo${job.failed === 1 ? '' : 's'} could not be read</div>`
      : '';
    box.innerHTML = `
      ${warn}
      <div class="ai-index-note">${idx.count} photos indexed · ${idx.scored} scored<br>
        ${mb} MB in app cache · model ${idx.model || '?'}</div>
      <button class="ai-index-btn" id="aiRescanBtn" type="button">Update index</button>`;
    $('#aiRescanBtn').addEventListener('click', startIndex);
    return;
  }

  box.innerHTML = `
    <div class="ai-index-note">Enable search and picks by indexing this library once.
      Nothing is written inside the library.</div>
    <button class="ai-index-btn primary" id="aiBuildBtn" type="button">Build index</button>`;
  $('#aiBuildBtn').addEventListener('click', startIndex);
}

async function startIndex() {
  try {
    const r = await api('/api/ai/index', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (r.started) toast('Indexing started — you can keep browsing.');
    refreshAiBox();
  } catch (err) {
    toast(err.message, true);
  }
}

/* --------------------------------------------------------------- events */

function wire() {
  $('#openBtn').addEventListener('click', () => openLibrary(null));
  const ob2 = $('#openBtn2');
  if (ob2) ob2.addEventListener('click', () => openLibrary(null));

  document.querySelectorAll('.nav-item[data-view]').forEach((n) => {
    if (n.closest('#albumList')) return;
    n.addEventListener('click', (e) => {
      e.preventDefault();
      setView(n.dataset.view);
    });
  });

  let searchTimer;
  $('#search').addEventListener('input', (e) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      state.query = e.target.value.trim();
      reload();
    }, 220);
  });

  // AI content search runs on submit / button, not on every keystroke: each
  // query costs a CLIP text encoding plus a full similarity pass.
  const runAiSearch = () => {
    const q = $('#aiQuery').value.trim();
    if (!q) { showEmpty('Type a description above to search the photo contents.'); return; }
    state.aiQuery = q;
    if (state.view !== 'ai-search') setView('ai-search');
    else loadAi();
  };
  $('#aiGo').addEventListener('click', runAiSearch);
  $('#aiQuery').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runAiSearch(); }
  });

  $('#sort').addEventListener('change', (e) => {
    state.sort = e.target.value;
    reload();
  });

  $('#year').addEventListener('change', (e) => {
    state.year = e.target.value;
    reload();
  });

  $('#zoom').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.size = b.dataset.size;
    $('#zoom').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
    const px = { xs: 96, s: 168, m: 240, l: 330 }[state.size] || 168;
    document.documentElement.style.setProperty('--tile', px + 'px');
  });

  // Infinite scroll.
  const io = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore(false);
  }, { root: $('#scroller'), rootMargin: '900px' });
  io.observe($('#sentinel'));

  // Viewer controls.
  $('#vClose').addEventListener('click', closeViewer);
  $('#vPrev').addEventListener('click', () => step(-1));
  $('#vNext').addEventListener('click', () => step(1));
  document.querySelector('.viewer-bg').addEventListener('click', closeViewer);

  document.addEventListener('keydown', (e) => {
    const viewerOpen = !$('#viewer').classList.contains('hidden');
    if (e.key === 'Escape' && viewerOpen) return closeViewer();
    if (viewerOpen) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
      return;
    }
    if (e.key === '/' && document.activeElement !== $('#search')) {
      e.preventDefault();
      $('#search').focus();
    }
  });

  // Drag & drop a .photoslibrary folder onto the window.
  ['dragover', 'drop'].forEach((ev) =>
    window.addEventListener(ev, (e) => e.preventDefault())
  );
  window.addEventListener('drop', (e) => {
    const item = e.dataTransfer?.items?.[0];
    if (!item) return;
    const entry = item.webkitGetAsEntry?.();
    if (entry?.isDirectory) {
      // A dropped folder has no usable path in the browser; ask the server.
      toast('Use “Open Library…” to pick the folder.');
    }
  });
}

async function step(delta) {
  const next = state.viewerIndex + delta;
  if (next < 0) return;
  if (next >= state.items.length) {
    if (state.done) return;
    await loadMore(false);
    if (next >= state.items.length) return;
  }
  state.viewerIndex = next;
  await renderViewer();
}

/* ----------------------------------------------------------------- init */

async function adoptOpenLibrary() {
  // The launcher may have opened a library before the browser connected.
  const info = await api('/api/library');
  await applyLibrary(info);
}

(async function init() {
  wire();
  let adopted = false;
  try {
    const health = await api('/api/health');
    if (health.library) {
      await adoptOpenLibrary();
      adopted = true;
    }
  } catch (_) { /* fall through to the picker */ }

  try {
    const recent = await api('/api/recent');
    const libs = recent.libraries || [];
    const wrap = $('#recentWrap');
    const list = $('#recentList');
    list.innerHTML = '';
    libs.forEach((p) => {
      const b = el('button', 'recent-item');
      const name = p.split(/[\\/]/).pop().replace('.photoslibrary', '');
      b.appendChild(document.createTextNode(name));
      b.appendChild(el('span', null, p));
      b.addEventListener('click', () => openLibrary(p));
      list.appendChild(b);
    });
    wrap.classList.toggle('hidden', libs.length === 0);
  } catch (_) { /* recents are optional */ }

  if (!adopted) $('#dropzone').classList.remove('hidden');
})();
