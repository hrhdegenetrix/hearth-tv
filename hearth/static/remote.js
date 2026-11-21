// Hearth phone remote. Talks only to the Hearth server (/api/*); the server talks to the TV box.
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];

  // ------------------------------------------------------------------ api
  async function api(path, opts = {}) {
    const headers = {};
    if (opts.body && typeof opts.body === 'string') headers['Content-Type'] = 'application/json';
    const r = await fetch(path, { credentials: 'same-origin', ...opts, headers: { ...headers, ...(opts.headers || {}) } });
    if (r.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `Error ${r.status}`);
    return data;
  }
  const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

  let toastTimer;
  function toast(msg, bad = false) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.toggle('bad', bad);
    t.classList.add('on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('on'), bad ? 5000 : 2600);
  }
  const buzz = () => { try { navigator.vibrate && navigator.vibrate(8); } catch (e) {} };
  async function act(fn, okMsg) {
    buzz();
    try { const r = await fn(); if (okMsg) toast(okMsg); refresh(); return r; }
    catch (e) { toast(e.message, true); }
  }

  const fmt = (s) => {
    if (s == null || isNaN(s)) return '0:00';
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(sec).padStart(2, '0');
  };
  const size = (b) => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.max(1, Math.round(b / 1e6)) + ' MB';
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // ------------------------------------------------------------------ tabs
  let currentView = 'now';
  $$('nav.tabs button').forEach((b) => b.addEventListener('click', () => {
    currentView = b.dataset.view;
    $$('nav.tabs button').forEach((x) => x.classList.toggle('active', x === b));
    $$('.view').forEach((v) => v.classList.toggle('active', v.id === 'view-' + currentView));
    if (currentView === 'library') loadLibrary();
    discView(currentView === 'discs');
    window.scrollTo(0, 0);
  }));

  // ------------------------------------------------------------------ status
  let status = null, scrubbing = false;
  let lastDroid = null;     // Android's state at the last status, for the one restart toast
  async function refresh() {
    try { status = await api('/api/status'); }
    catch (e) { status = null; }
    render();
  }

  function render() {
    const online = !!(status && status.online);
    $('#dot').className = 'dot ' + (status ? (online ? 'on' : 'off') : '');
    $('#offline').classList.toggle('on', !!status && !online);
    if (!status) return;

    const mode = status.mode, p = status.player || {}, b = status.browser || {};
    const isPlayer = mode === 'player', isApp = mode === 'app';
    $('#player-controls').hidden = !isPlayer;
    $('#app-controls').hidden = !isApp;

    const music = isPlayer ? status.music : null;
    const podcast = !!(music && music.track.kind === 'podcast');
    $('#music-skip').hidden = !music || podcast;
    $('#song-feedback').hidden = !music || podcast;
    $$('[data-video-only]').forEach((el) => { el.hidden = !!music; });
    if (music) {
      const t = music.track;
      $('#np-title').textContent = t.title;
      const where = podcast ? '' : t.radio ? ` · ${t.station} radio` : (t.station ? ` · ${t.station}, song ${music.index + 1} of ${music.count}`
        : ` · song ${music.index + 1} of ${music.count}`);
      $('#np-sub').textContent = (p.paused ? 'Paused · ' : '') + [t.artist, t.album].filter(Boolean).join(' — ') + where;
      $('#love-btn').textContent = status.loved ? '♥ Loved' : '♡ Love';
      $('#love-btn').classList.toggle('loved', !!status.loved);
      $('#ban-btn').hidden = !t.radio;
      SONG.id = t.id;
    }
    $('#queue-wrap').hidden = !music || podcast || music.count < 2;
    if (music && !podcast && music.count > 1) {
      const key = music.track.id + ':' + music.index + ':' + music.count;
      if (key !== QUEUE.key) { QUEUE.key = key; loadQueue(); }
    } else QUEUE.key = '';
    if (isPlayer && !music) {
      $('#np-title').textContent = (status.now && status.now.title) || p.title || 'Playing';
      $('#np-sub').textContent = p.buffering ? 'Buffering…' : (p.paused ? 'Paused' : (status.now && status.now.source === 'library' ? 'From your library' : 'Playing'));
    }
    if (isPlayer) {
      $('#toggle use').setAttribute('href', p.paused ? '#i-play' : '#i-pause');
      if (!scrubbing) {
        const dur = p.duration || 0;
        $('#scrub').max = Math.max(1, Math.floor(dur));
        $('#scrub').value = Math.floor(p.position || 0);
        $('#t-pos').textContent = fmt(p.position);
        $('#t-dur').textContent = fmt(dur);
      }
    } else if (isApp) {
      $('#np-title').textContent = b.title || 'Browser';
      $('#np-sub').textContent = b.app ? 'Open in the TV browser' : (b.url || '');
    } else {
      $('#np-title').textContent = online ? 'Nothing playing' : 'TV box offline';
      $('#np-sub').textContent = 'Paste a link, search YouTube, or pick something from your library.';
    }
    // An Android app on its way, step by step with a running clock: which step
    // it's on, how long that's taken, and how long it usually takes. Android
    // restarting under it takes ~70 s, and Home stops the open.
    const droid = status.android;
    if (droid && !isPlayer) {
      const what = droid.app ? 'Opening ' + droid.app
        : (droid.state === 'restarting' ? 'Getting Android ready' : 'Opening the app');
      $('#np-title').textContent = what + (droid.elapsed != null ? ' · ' + fmt(droid.elapsed) : '…');
      const lines = (droid.steps || []).map((st) => {
        const mark = st.status === 'done' ? '✓' : (st.status === 'failed' ? '✗' : '…');
        let line = mark + ' ' + st.label + '  ' + fmt(st.secs);
        if (st.status === 'active') {
          line += st.secs > st.usual * 1.5 + 5 ? '  (longer than its usual ' + fmt(st.usual) + ')'
            : '  (usually ' + fmt(st.usual) + ')';
        }
        return line;
      });
      if (droid.note) lines.push(droid.note);
      lines.push('Home stops it.');
      $('#np-sub').textContent = lines.join('\n');
      $('#np-sub').style.whiteSpace = 'pre-line';
    } else {
      $('#np-sub').style.whiteSpace = '';
    }
    if (droid && droid.state === 'restarting' && lastDroid !== 'restarting') toast('Android is restarting. About a minute…');
    lastDroid = droid ? droid.state : null;
    // Play/pause icons follow what's actually playing (player, Android app, or browser sound).
    const playing = status.playing;
    $('#remote-playpause use').setAttribute('href', playing ? '#i-pause' : '#i-play');
    $('#app-playpause').textContent = playing === true ? 'Pause' : (playing === false ? 'Play' : 'Play / Pause');
    const a = status.audio || {};
    $('#vol').textContent = a.volume == null ? '–' : (a.muted ? 'Muted' : a.volume + '%');
    $('#mute').textContent = a.muted ? 'Unmute' : 'Mute';
    $('#mute-remote').textContent = a.muted ? 'Unmute' : 'Mute';
    if (!isPlayer && !isApp) { $('#snap').hidden = true; $('#screen-empty').hidden = false; }
  }

  let pollTimer;
  function schedulePoll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => { if (!document.hidden) await refresh(); schedulePoll(); }, 1500);
  }
  // The remote coming up is a good sign an app is next: Android gets ready
  // in the background (woken, restarted now if its display has crashed).
  let preparedAt = 0;
  function prepareAndroid() {
    if (Date.now() - preparedAt < 120000) return;
    preparedAt = Date.now();
    post('/api/android/prepare', {}).catch(() => {});
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); peekIfPlaying(); prepareAndroid(); } });

  // ------------------------------------------------------------------ peek at the TV
  let lastSnapUrl = null;
  async function peek(quiet = false) {
    try {
      const r = await fetch('/api/snapshot', { credentials: 'same-origin', cache: 'no-store' });
      if (!r.ok) throw new Error("Couldn't see the TV");
      const url = URL.createObjectURL(await r.blob());
      const img = $('#snap');
      img.src = url;
      img.hidden = false;
      $('#screen-empty').hidden = true;
      if (lastSnapUrl) URL.revokeObjectURL(lastSnapUrl);
      lastSnapUrl = url;
    } catch (e) { if (!quiet) toast(e.message, true); }
  }
  $('#peek').addEventListener('click', () => { buzz(); peek(); });
  function peekIfPlaying() {
    if (currentView === 'now' && status && (status.mode === 'player' || status.mode === 'app')) peek(true);
  }
  setInterval(() => { if (!document.hidden) peekIfPlaying(); }, 20000);

  // ------------------------------------------------------------------ transport
  $('#toggle').addEventListener('click', () => act(() => post('/api/control', { action: 'toggle' })));
  $$('[data-seek]').forEach((b) => b.addEventListener('click', () =>
    act(() => post('/api/control', { action: 'seek', seconds: Number(b.dataset.seek) }))));
  $$('[data-control]').forEach((b) => b.addEventListener('click', () =>
    act(() => post('/api/control', { action: b.dataset.control }))));
  const scrub = $('#scrub');
  scrub.addEventListener('input', () => { scrubbing = true; $('#t-pos').textContent = fmt(Number(scrub.value)); });
  scrub.addEventListener('change', async () => {
    await act(() => post('/api/control', { action: 'seek_to', seconds: Number(scrub.value) }));
    scrubbing = false;
  });
  $$('[data-vol]').forEach((b) => b.addEventListener('click', () =>
    act(() => post('/api/control', { action: 'volume', step: Number(b.dataset.vol) }))));
  $('#mute').addEventListener('click', () => act(() => post('/api/control', { action: 'mute' })));
  $('#mute-remote').addEventListener('click', () => act(() => post('/api/control', { action: 'mute' })));
  $('#home-btn').addEventListener('click', () => act(() => post('/api/home'), 'Home screen'));

  $('#link-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const url = $('#link').value.trim();
    if (!url) return;
    act(async () => {
      const r = await post('/api/cast', { url });
      $('#link').value = '';
      return r;
    }, 'Sending to the TV…');
  });

  // ------------------------------------------------------------------ remote keys + apps
  $$('[data-key]').forEach((b) => b.addEventListener('click', () =>
    act(() => post('/api/input', { type: 'key', key: b.dataset.key }))));
  $$('[data-media]').forEach((b) => b.addEventListener('click', () =>
    act(() => post('/api/media', { action: b.dataset.media }))));
  // Said at once: an Android app can take a minute or two to answer, and the
  // Now card follows it from the status (opening / restarting). When the box
  // can't open it (Android isn't set up, say), the server's own words come back
  // as a red toast that stays up long enough to read.
  $$('[data-app]').forEach((b) => b.addEventListener('click', () => {
    toast('Opening ' + (b.dataset.label || b.textContent.trim()) + '…');
    act(() => post('/api/app', { name: b.dataset.app }));
  }));
  $('#restart-browser').addEventListener('click', () =>
    act(() => post('/api/browser/restart'), 'Browser restarted'));
  $('#type-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const text = $('#type-text').value;
    if (!text) return;
    act(async () => { await post('/api/input', { type: 'text', text }); $('#type-text').value = ''; });
  });

  // ------------------------------------------------------------------ touchpad
  const pad = $('#pad');
  const ptrs = new Map();
  let travel = 0, downAt = 0, maxPtrs = 0, acc = { dx: 0, dy: 0 }, scrollAcc = 0, sending = false;
  pad.addEventListener('pointerdown', (e) => {
    pad.setPointerCapture(e.pointerId);
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    maxPtrs = Math.max(maxPtrs, ptrs.size);
    if (ptrs.size === 1) { travel = 0; downAt = performance.now(); }
    pad.classList.add('active');
  });
  pad.addEventListener('pointermove', (e) => {
    const p = ptrs.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    travel += Math.hypot(dx, dy);
    if (ptrs.size >= 2) { scrollAcc += dy / ptrs.size; return; }
    const speed = Math.hypot(dx, dy);
    const gain = 2.2 + Math.min(3, speed / 6);   // a little acceleration for long flicks
    acc.dx += dx * gain; acc.dy += dy * gain;
  });
  const lift = (e) => {
    ptrs.delete(e.pointerId);
    if (ptrs.size) return;
    pad.classList.remove('active');
    if (travel < 8 && performance.now() - downAt < 300) {
      buzz();
      post('/api/input', { type: 'click', button: maxPtrs >= 2 ? 'right' : 'left' }).catch((err) => toast(err.message, true));
    }
    maxPtrs = 0;
  };
  pad.addEventListener('pointerup', lift);
  pad.addEventListener('pointercancel', lift);
  setInterval(async () => {
    if (sending) return;
    const dx = Math.round(acc.dx), dy = Math.round(acc.dy);
    const ticks = Math.trunc(scrollAcc / 24);
    if (!dx && !dy && !ticks) return;
    sending = true;
    try {
      if (dx || dy) { acc.dx -= dx; acc.dy -= dy; await post('/api/input', { type: 'move', dx, dy }); }
      if (ticks) { scrollAcc -= ticks * 24; await post('/api/input', { type: 'scroll', dy: -ticks }); }
    } catch (e) { /* the next frame retries */ }
    sending = false;
  }, 40);

  // Scroll buttons beside the pad: two-finger scrolling over a phone touchpad is
  // unreliable, and Prime and Max are phone apps whose arrow keys can't reach
  // every list. One press = 3 wheel steps (about half a screen in Prime); hold to keep going.
  $$('[data-scroll]').forEach((b) => {
    let timer = null;
    const step = () => post('/api/input', { type: 'scroll', dy: -3 * Number(b.dataset.scroll) }).catch((err) => toast(err.message, true));
    const stop = () => { clearTimeout(timer); timer = null; };
    b.addEventListener('pointerdown', (e) => {
      e.preventDefault(); buzz(); step();
      const again = () => { step(); timer = setTimeout(again, 250); };
      timer = setTimeout(again, 450);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach((ev) => b.addEventListener(ev, stop));
  });

  // ------------------------------------------------------------------ the song playing: ♥, not for this station, radio
  const SONG = { id: null };
  // The whole queue under the song: a few played ones above, what's coming below; tap to jump.
  const QUEUE = { key: '', all: false, items: [] };
  async function loadQueue() {
    try {
      const q = await api('/api/music/queue');
      QUEUE.items = q.items;
      const at = q.index ?? 0, n = q.items.length;
      const from = QUEUE.all ? 0 : Math.max(0, at - 3), to = QUEUE.all ? n : Math.min(n, at + 30);
      let html = '';
      for (let i = from; i < to; i++) {
        const t = q.items[i];
        html += `<div class="item song-row${i === at ? ' now' : ''}${i < at ? ' played' : ''}" data-jump="${i}" role="button">`
          + `<span class="glyph num">${i === at ? '♪' : i + 1}</span><span class="meta"><span class="name">${esc(t.title)}</span>`
          + `<span class="sub">${[esc(t.artist), t.duration ? fmt(t.duration) : ''].filter(Boolean).join(' · ')}`
          + `${t.queued && i > at ? ' <span class="badge queued">Queued</span>' : ''}</span></span>`
          + `<button class="more" data-qmore="${i}" aria-label="More">⋯</button></div>`;
      }
      $('#queue-list').innerHTML = html;
      $('#queue-more').hidden = QUEUE.all || (from === 0 && to === n);
      $('#queue-more').textContent = `Show the whole list (${n.toLocaleString('en-US')} songs)`;
    } catch (e) { /* next status round */ }
  }
  $('#queue-list').addEventListener('click', (e) => {
    const m = e.target.closest('[data-qmore]');
    if (m) { const t = QUEUE.items[Number(m.dataset.qmore)]; if (t) openSongSheet(t, { onChange: loadQueue }); return; }
    const b = e.target.closest('[data-jump]');
    if (!b || b.classList.contains('now')) return;
    act(() => post('/api/control', { action: 'jump', index: Number(b.dataset.jump) }), 'Playing ' + b.querySelector('.name').textContent);
  });
  $('#queue-more').addEventListener('click', () => { QUEUE.all = true; loadQueue(); });
  $('#love-btn').addEventListener('click', () => act(async () => {
    const r = await post('/api/radio/feedback', { kind: status && status.loved ? 'unlove' : 'love' });
    toast(r.loved ? '♥ Loved. Old favorites will play it more.' : 'Not loved any more');
    return r;
  }));
  $('#ban-btn').addEventListener('click', () => act(() => post('/api/radio/feedback', { kind: 'ban' }), 'This station won\'t play it again'));
  $('#song-radio-btn').addEventListener('click', () => SONG.id && playRadio('song:' + SONG.id));

  // ------------------------------------------------------------------ a song's ⋯ menu
  // Any song row's ⋯: Play next, Add to queue, ♥ Love, Not for me, Add to a playlist
  // (or a new one), and Remove on one of your own playlists.
  const SHEET = { track: null, ctx: {} };
  const songSheet = $('#song-sheet'), plSheet = $('#pl-sheet');
  function openSongSheet(track, ctx = {}) {
    if (!track || !track.id) return;
    buzz();
    SHEET.track = track; SHEET.ctx = ctx;
    $('#ss-title').textContent = track.title || 'This song';
    $('#ss-sub').textContent = [track.artist, track.album].filter(Boolean).join(' — ');
    $('#ss-love').textContent = track.loved ? '♥ Loved (tap to unlove)' : '♡ Love';
    $('#ss-love').classList.toggle('loved', !!track.loved);
    $('#ss-dislike').textContent = track.disliked ? 'Undo “Not for me”' : 'Not for me';
    $('#ss-remove').hidden = !(ctx.own && track.entry);
    songSheet.classList.add('on');
  }
  const closeSheets = () => { songSheet.classList.remove('on'); plSheet.classList.remove('on'); };
  function sheetDone(changed) {
    closeSheets();
    if (changed && SHEET.ctx.onChange) SHEET.ctx.onChange();
  }
  async function queueSong(mode) {
    const t = SHEET.track;
    closeSheets();
    await act(async () => {
      const r = await post('/api/queue', { song: t.id, mode });
      toast(r.started ? 'Playing ' + t.title : (mode === 'next' ? 'Plays next: ' + t.title : 'Added to the queue'));
      return r;
    });
    if (SHEET.ctx.onQueued) SHEET.ctx.onQueued();
  }
  $('#ss-next').addEventListener('click', () => queueSong('next'));
  $('#ss-queue').addEventListener('click', () => queueSong('queue'));
  $('#ss-love').addEventListener('click', async () => {
    const t = SHEET.track;
    const r = await act(() => post('/api/song/feedback', { song: t.id, kind: t.loved ? 'unlove' : 'love' }));
    if (!r) return;
    t.loved = r.loved;
    toast(r.loved ? '♥ Loved. Old favorites will play it more.' : 'Not loved any more');
    sheetDone(true);
  });
  $('#ss-dislike').addEventListener('click', async () => {
    const t = SHEET.track;
    const r = await act(() => post('/api/song/feedback', { song: t.id, kind: t.disliked ? 'undislike' : 'dislike' }));
    if (!r) return;
    t.disliked = r.disliked;
    toast(r.disliked ? "Won't come up again on a station or in Shuffle all" : 'Back in the mix');
    sheetDone(true);
  });
  $('#ss-remove').addEventListener('click', async () => {
    const t = SHEET.track, pid = SHEET.ctx.playlist;
    const r = await act(() => post(`/api/playlists/${pid}/remove`, { entries: [t.entry] }));
    if (!r) return;
    toast('Removed ' + t.title);
    sheetDone(true);
  });
  $('#ss-cancel').addEventListener('click', closeSheets);
  songSheet.addEventListener('click', (e) => { if (e.target === songSheet) closeSheets(); });
  $('#ss-addpl').addEventListener('click', async () => {
    songSheet.classList.remove('on');
    $('#pl-song').textContent = SHEET.track.title || '';
    $('#pl-choices').innerHTML = '<div class="empty">Loading…</div>';
    plSheet.classList.add('on');
    try {
      const { playlists } = await api('/api/playlists');
      $('#pl-choices').innerHTML = playlists.map((pl) => `<button class="item" data-addto="${pl.id}" data-name="${esc(pl.name)}">`
        + `<span class="glyph">♪</span><span class="meta"><span class="name">${esc(pl.name)}</span>`
        + `<span class="sub">${pl.count === 1 ? '1 song' : pl.count + ' songs'}</span></span></button>`).join('')
        || '<div class="empty">No playlists of your own yet. Start one:</div>';
    } catch (e) { $('#pl-choices').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  });
  $('#pl-choices').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-addto]');
    if (!b) return;
    const t = SHEET.track;
    const r = await act(() => post(`/api/playlists/${b.dataset.addto}/add`, { songs: [t.id] }));
    if (!r) return;
    toast('Added to ' + b.dataset.name);
    sheetDone(SHEET.ctx.playlist === b.dataset.addto);
    loadRadio();
  });
  $('#pl-new').addEventListener('click', async () => {
    const t = SHEET.track;
    const name = (prompt('Name the new playlist:', '') || '').trim();
    if (!name) return;
    const r = await act(() => post('/api/playlists', { name, songs: [t.id] }));
    if (!r) return;
    toast(`Made “${r.name}” with ${t.title}`);
    sheetDone(false);
    loadRadio();
  });
  $('#pl-cancel').addEventListener('click', closeSheets);
  plSheet.addEventListener('click', (e) => { if (e.target === plSheet) closeSheets(); });

  // ------------------------------------------------------------------ library (Jellyfin)
  const posterHtml = (it) => {
    const img = it.has_image ? `<img src="/api/jf/image/${it.id}?w=300" alt="" loading="lazy">` : esc(it.name);
    const pct = it.resume && it.runtime ? Math.min(100, Math.round(100 * it.resume / it.runtime)) : 0;
    const bar = pct ? `<span class="bar"><i style="width:${pct}%"></i></span>` : '';
    const seen = it.played ? '<span class="seen">✓</span>' : '';
    const year = it.year && !String(it.name).includes(`(${it.year})`) ? ` (${it.year})` : '';
    const label = it.series ? `${esc(it.series)} · S${it.season ?? '?'}E${it.episode ?? '?'}` : esc(it.name) + year;
    return `<button class="poster" data-id="${it.id}" data-type="${it.type}" data-name="${esc(it.name)}" data-resume="${it.resume || ''}">
      <span class="art">${img}${bar}${seen}</span><span class="name">${label}</span></button>`;
  };

  async function loadLibrary() {
    $('#lib-show').hidden = true; $('#lib-music').hidden = true; $('#lib-podcast').hidden = true; $('#lib-main').hidden = false;
    loadMusic();
    loadRadio();
    loadPodcasts();
    try {
      const lib = await api('/api/library');
      $('#lib-continue-wrap').hidden = !lib.continue.length;
      $('#lib-continue').innerHTML = lib.continue.map(posterHtml).join('');
      $('#lib-movies').innerHTML = lib.movies.map(posterHtml).join('') || '<div class="empty" style="grid-column:1/-1">No movies yet.</div>';
      $('#lib-shows').innerHTML = lib.shows.map(posterHtml).join('') || '<div class="empty" style="grid-column:1/-1">No shows yet.</div>';
      $$('#view-library .poster:not(.album)').forEach((b) => b.addEventListener('click', () => pickPoster(b.dataset)));
    } catch (e) {
      $('#lib-movies').innerHTML = `<div class="empty" style="grid-column:1/-1">${esc(e.message)}</div>`;
    }
  }

  async function openShow(id, name) {
    $('#lib-main').hidden = true; $('#lib-show').hidden = false;
    $('#lib-show-title').textContent = name;
    const box = $('#lib-episodes');
    box.innerHTML = '<div class="empty">Loading…</div>';
    try {
      const { episodes } = await api('/api/library/show/' + id);
      box.innerHTML = episodes.map((ep) => {
        const resume = ep.resume ? `<span class="badge">Resume ${fmt(ep.resume)}</span>` : (ep.played ? '<span class="badge">Watched</span>' : '');
        return `<button class="item" data-id="${ep.id}" data-type="Episode" data-name="${esc(ep.name)}" data-resume="${ep.resume || ''}">
          <span class="glyph"><svg><use href="#i-play"/></svg></span>
          <span class="meta"><span class="name">S${ep.season ?? '?'}E${ep.episode ?? '?'} · ${esc(ep.name)}</span>
          <span class="sub">${ep.runtime ? fmt(ep.runtime) : ''} ${resume}</span></span></button>`;
      }).join('') || '<div class="empty">No episodes found.</div>';
      $$('.item', box).forEach((b) => b.addEventListener('click', () => pickPoster(b.dataset)));
    } catch (e) { box.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }
  $('#lib-back').addEventListener('click', () => loadLibrary());

  // ------------------------------------------------------------------ music (Jellyfin)
  const MUSIC = { artists: [], showAll: false, stack: [] };
  const albumHtml = (a) => {
    const img = a.has_image ? `<img src="/api/jf/image/${a.id}?w=300" alt="" loading="lazy">` : '♪';
    return `<button class="poster album" data-album="${a.id}" data-name="${esc(a.name)}" data-artist-name="${esc(a.artist)}">
      <span class="art">${img}</span><span class="name">${esc(a.name)}</span><span class="by">${esc(a.artist || '')}</span></button>`;
  };
  const artistHtml = (a) => `<button class="item" data-artist="${a.id}" data-name="${esc(a.name)}">
      <span class="glyph">♪</span><span class="meta"><span class="name">${esc(a.name)}</span></span></button>`;
  const songHtml = (t, i, showArtist) => `<div class="item song-row" data-song="${t.id}" data-i="${i}" role="button">
      <span class="glyph num">${t.number ?? '♪'}</span>
      <span class="meta"><span class="name">${esc(t.title)}</span>
      <span class="sub">${[showArtist ? esc(t.artist) : '', showArtist ? esc(t.album) : '', t.duration ? fmt(t.duration) : ''].filter(Boolean).join(' · ')}</span></span>
      <button class="more" data-smore="${i}" aria-label="More">⋯</button></div>`;
  // A row of a list page: tap plays the list from it, ⋯ opens the song's menu
  const listRowHtml = (t, i, showArtist, numbered) => `<div class="item song-row${t.disliked ? ' disliked' : ''}" data-li="${i}" role="button">
      <span class="glyph num">${numbered ? (t.number ?? i + 1) : i + 1}</span>
      <span class="meta"><span class="name">${esc(t.title)}${t.loved ? ' <span class="heart">♥</span>' : ''}</span>
      <span class="sub">${[showArtist ? esc(t.artist) : '', t.duration ? fmt(t.duration) : ''].filter(Boolean).join(' · ')}</span></span>
      <button class="more" data-more="${i}" aria-label="More">⋯</button></div>`;
  const empty = (msg) => `<div class="empty" style="grid-column:1/-1">${esc(msg)}</div>`;

  function renderArtists() {
    const q = $('#artist-filter').value.trim().toLowerCase();
    // "ac" → AC/DC before Backstreet Boys: names that start with it come first
    const list = !q ? MUSIC.artists : MUSIC.artists.filter((a) => a.name.toLowerCase().includes(q))
      .sort((a, b) => (b.name.toLowerCase().startsWith(q) - a.name.toLowerCase().startsWith(q)));
    const cap = q || MUSIC.showAll ? list.length : 40;
    $('#music-artists').innerHTML = list.slice(0, cap).map(artistHtml).join('')
      + (list.length > cap ? `<button class="chip more" id="artists-more">Show all ${list.length} artists</button>` : '')
      || empty(q ? 'No artist by that name.' : 'No music yet. Put some in the Music folder of the media share.');
  }

  async function loadMusic() {
    try {
      const m = await api('/api/music');
      $('#music-recent').innerHTML = m.recent.map(albumHtml).join('') || empty('No albums yet.');
      MUSIC.artists = m.artists;
      renderArtists();
    } catch (e) { $('#music-recent').innerHTML = empty(e.message); }
  }

  function musicView(title, sub, imageId) {
    $('#lib-main').hidden = true; $('#lib-show').hidden = true; $('#lib-podcast').hidden = true; $('#lib-music').hidden = false;
    $('#mu-radio').hidden = true; $('#mu-rename').hidden = true; $('#mu-delete').hidden = true;
    MUSIC.list = null; MUSIC.play = null;
    $('#mu-title').textContent = title;
    $('#mu-sub').textContent = sub || '';
    $('#mu-art').innerHTML = imageId ? `<img src="/api/jf/image/${imageId}?w=400" alt="" onerror="this.remove()">♪` : '♪';
    $('#mu-albums').innerHTML = ''; $('#mu-tracks').innerHTML = '<div class="empty">Loading…</div>';
    window.scrollTo(0, 0);
  }

  function openAlbum(id, name, artist, fromStack) {
    return openList({ kind: 'album', id, ids: [id], title: name, sub: artist, image: id }, fromStack);
  }

  // A list page: an album, an iTunes playlist, one of your own playlists, or a station's
  // list for today. Nothing starts by itself: Play all, Shuffle, or tap a song to play the
  // list from there.
  async function openList(spec, fromStack) {
    if (!fromStack) MUSIC.stack.push(['list', spec]);
    musicView(spec.title || '', spec.sub || '', spec.image || null);
    const qs = new URLSearchParams({ kind: spec.kind, id: spec.id, ids: (spec.ids || []).join(','), songs: (spec.songs || []).join(',') });
    try {
      const data = await api('/api/list?' + qs);
      if (MUSIC.stack[MUSIC.stack.length - 1]?.[1] !== spec) return;      // moved on meanwhile
      MUSIC.list = { spec, data };
      $('#mu-title').textContent = data.title || spec.title || '';
      const various = new Set(data.items.map((t) => t.artist)).size > 1;
      const n = data.items.length;
      $('#mu-sub').textContent = [data.sub || (various ? '' : (data.items[0] && data.items[0].artist)),
                                  n === 1 ? '1 song' : n + ' songs'].filter(Boolean).join(' · ');
      if (data.image) $('#mu-art').innerHTML = `<img src="/api/jf/image/${data.image}?w=400" alt="" onerror="this.remove()">♪`;
      $('#mu-rename').hidden = $('#mu-delete').hidden = !data.own;
      renderList();
    } catch (e) { $('#mu-tracks').innerHTML = empty(e.message); }
  }
  function renderList() {
    const { data } = MUSIC.list;
    const various = data.kind !== 'album' || new Set(data.items.map((t) => t.artist)).size > 1;
    $('#mu-tracks').innerHTML = data.items.map((t, i) => listRowHtml(t, i, various, data.kind === 'album')).join('')
      || empty(data.own ? 'No songs yet. Add some with ⋯ on any song.' : 'No songs found.');
  }
  function reopenList() {
    const top = MUSIC.stack[MUSIC.stack.length - 1];
    if (top && top[0] === 'list' && !$('#lib-music').hidden) openList(top[1], true);
  }
  function playList(start, shuffle) {
    const { spec } = MUSIC.list;
    return act(async () => {
      const r = await post('/api/list/play', { kind: spec.kind, id: spec.id, ids: spec.ids || [], songs: spec.songs || [],
                                               start: start || 0, shuffle: !!shuffle });
      $$('nav.tabs button')[0].click();
      toast('Playing ' + (shuffle ? (r.list || '') + ', shuffled' : r.title || r.list || 'on the TV'));
      return r;
    });
  }

  async function openArtist(id, name, fromStack) {
    if (!fromStack) MUSIC.stack.push(['artist', id, name]);
    musicView(name, '', id);
    MUSIC.play = { artist: id };
    $('#mu-radio').hidden = false;
    $('#mu-tracks').innerHTML = '';
    try {
      const { albums } = await api('/api/music/artist/' + id);
      $('#mu-sub').textContent = albums.length === 1 ? '1 album' : albums.length + ' albums';
      $('#mu-albums').innerHTML = albums.map(albumHtml).join('') || empty('No albums found.');
    } catch (e) { $('#mu-albums').innerHTML = empty(e.message); }
  }

  function playMusic(body) {
    return act(async () => {
      const r = await post('/api/music/play', body);
      $$('nav.tabs button')[0].click();
      return r;
    }, 'Playing on the TV');
  }

  const SEARCH = { tracks: [] };
  // after ♥ / Not for me / Remove: redraw the row, or reload the page when the list changed
  function renderListOrReopen() {
    if (SHEET.ctx.own) reopenList(); else if (MUSIC.list) renderList();
  }
  async function searchMusic(q) {
    const box = $('#music-results');
    box.hidden = !q;
    if (!q) return;
    box.innerHTML = '<div class="empty">Searching…</div>';
    try {
      const r = await api('/api/music/search?q=' + encodeURIComponent(q));
      SEARCH.tracks = r.tracks;
      let html = '';
      if (r.artists.length) html += '<h3>Artists</h3><div class="list">' + r.artists.map(artistHtml).join('') + '</div>';
      if (r.albums.length) html += '<h3>Albums</h3><div class="poster-grid">' + r.albums.map(albumHtml).join('') + '</div>';
      if (r.tracks.length) html += '<h3>Songs</h3><div class="list">' + r.tracks.map((t, i) => songHtml(t, i, true)).join('') + '</div>';
      box.innerHTML = html || empty('Nothing matched "' + q + '".');
    } catch (e) { box.innerHTML = empty(e.message); }
  }

  $('#music-shuffle-all').addEventListener('click', () => playMusic({ everything: true }));
  $('#music-search-form').addEventListener('submit', (e) => { e.preventDefault(); searchMusic($('#music-search').value.trim()); });
  $('#music-search').addEventListener('search', () => { if (!$('#music-search').value) searchMusic(''); });
  $('#artist-filter').addEventListener('input', renderArtists);
  $('#mu-play').addEventListener('click', () => MUSIC.list ? playList(0, false) : (MUSIC.play && playMusic(MUSIC.play)));
  $('#mu-shuffle').addEventListener('click', () => MUSIC.list ? playList(0, true) : (MUSIC.play && playMusic({ ...MUSIC.play, shuffle: true })));
  $('#mu-rename').addEventListener('click', () => {
    const { data } = MUSIC.list || {};
    if (!data || !data.own) return;
    const name = (prompt('A new name for this playlist:', data.title) || '').trim();
    if (!name) return;
    act(async () => { const r = await post(`/api/playlists/${data.id}/rename`, { name }); toast('Renamed ' + r.name); reopenList(); loadRadio(); return r; });
  });
  $('#mu-delete').addEventListener('click', () => {
    const { data } = MUSIC.list || {};
    if (!data || !data.own || !confirm(`Delete the playlist “${data.title}”? The songs stay in your library.`)) return;
    act(async () => {
      const r = await api('/api/playlists/' + data.id, { method: 'DELETE' });
      toast('Deleted ' + data.title);
      $('#music-back').click(); loadRadio();
      return r;
    });
  });
  $('#mu-radio').addEventListener('click', () => MUSIC.play && MUSIC.play.artist && playRadio('artist:' + MUSIC.play.artist));

  // ------------------------------------------------------------------ radio
  const RADIO = { playlists: [], showAll: false };
  // year, decade and genre stations say what they are in their names
  // a station with a cover picture (hearth/data/station_art/<station>.jpg) wears it behind its name
  const stationHtml = (st) => `<button class="station${st.art ? ' art' : ''}" data-station="${esc(st.id)}" data-name="${esc(st.name)}"`
    + (st.art ? ` style="background-image:linear-gradient(to top, rgba(23,19,15,.88), rgba(23,19,15,0) 62%), url('/api/radio/art?station=${encodeURIComponent(st.id)}')"` : '')
    + `><div class="name">${esc(st.name)}</div>`
    + (/^(year|decade|genre):/.test(st.id) ? '' : `<div class="sub">${esc(st.sub || '')}</div>`) + '</button>';
  function renderMine(mine) {
    $('#my-playlists').innerHTML = (mine || []).map((pl) => `<div class="item"><button class="meta" data-mine="${esc(pl.id)}" data-name="${esc(pl.name)}" style="text-align:left">`
      + `<span class="name">♥ ${esc(pl.name)}</span><span class="sub">${pl.count === 1 ? '1 song' : pl.count + ' songs'}</span></button></div>`).join('')
      || '<div class="note" style="margin:0 6px">None yet. ＋ New playlist, or ⋯ → Add to playlist on any song.</div>';
  }
  function renderPlaylists() {
    const cap = RADIO.showAll ? RADIO.playlists.length : 8;
    $('#radio-playlists').innerHTML = RADIO.playlists.slice(0, cap).map((pl) =>
      `<div class="item"><button class="meta" data-station="${esc(pl.id)}" data-name="${esc(pl.name)}" style="text-align:left"><span class="name">♪ ${esc(pl.name)}</span>`
      + `<span class="sub">${pl.count} songs</span></button>`
      + `<span class="ep-actions"><button class="chip" data-rename="${esc(pl.id)}" data-name="${esc(pl.name)}">Rename</button></span></div>`).join('')
      + (RADIO.playlists.length > cap ? `<button class="chip more" id="playlists-more">Show all ${RADIO.playlists.length} playlists</button>` : '')
      || empty('No playlists came over from iTunes.');
    // the section only shows when an iTunes library was found
    $('#itunes-playlists-h').hidden = $('#radio-playlists').hidden = !RADIO.playlists.length;
  }
  async function loadRadio() {
    try {
      const r = await api('/api/radio');
      $('#radio-stations').innerHTML = r.stations.map(stationHtml).join('') || empty('No stations yet.');
      RADIO.playlists = r.playlists;
      renderPlaylists();
      renderMine(r.mine);
    } catch (e) { $('#radio-stations').innerHTML = empty(e.message); }
  }
  function playRadio(station) {
    return act(async () => {
      const r = await post('/api/radio/play', { station });
      $$('nav.tabs button')[0].click();
      toast(r.continued ? `${r.station} comes next, after this song` : 'Playing ' + (r.station || r.title || 'on the TV'));
      return r;
    });
  }
  $('#new-playlist').addEventListener('click', () => {
    const name = (prompt('Name the new playlist:', '') || '').trim();
    if (!name) return;
    act(async () => {
      const r = await post('/api/playlists', { name, songs: [] });
      toast(`Made “${r.name}”. Add songs with ⋯ → Add to playlist.`);
      loadRadio();
      return r;
    });
  });
  $('#music-back').addEventListener('click', () => {
    MUSIC.stack.pop();
    const prev = MUSIC.stack[MUSIC.stack.length - 1];
    if (!prev) { $('#lib-music').hidden = true; $('#lib-main').hidden = false; MUSIC.list = null; return; }
    if (prev[0] === 'list') openList(prev[1], true);
    else openArtist(prev[1], prev[2], true);
  });
  // one listener for every album, artist and song button the music views draw
  $('#view-library').addEventListener('click', (e) => {
    // ⋯ first: it sits inside a row that would otherwise play
    const more = e.target.closest('[data-more], [data-smore]');
    if (more) {
      if (more.dataset.more != null && MUSIC.list) {
        const { data } = MUSIC.list;
        openSongSheet(data.items[Number(more.dataset.more)], { own: data.own, playlist: data.own ? data.id : null,
                                                              onChange: renderListOrReopen });
      } else if (more.dataset.smore != null) {
        openSongSheet(SEARCH.tracks[Number(more.dataset.smore)] || null, {});
      }
      return;
    }
    const row = e.target.closest('[data-li]');
    if (row && MUSIC.list) { playList(Number(row.dataset.li), false); return; }
    const b = e.target.closest('[data-album], [data-artist], [data-song], #artists-more, [data-rename], [data-station], [data-mine], #playlists-more');
    if (!b) return;
    if (b.id === 'artists-more') { MUSIC.showAll = true; renderArtists(); return; }
    if (b.id === 'playlists-more') { RADIO.showAll = true; renderPlaylists(); return; }
    if (b.dataset.rename) {        // your own name for an iTunes playlist
      const name = prompt('A new name for this playlist (leave empty for its old iTunes name):', b.dataset.name);
      if (name === null) return;
      act(async () => {
        const r = await post('/api/radio/playlist_name', { playlist: b.dataset.rename.replace(/^playlist:/, ''), name });
        toast('Renamed ' + r.name); loadRadio(); return r;
      });
      return;
    }
    if (b.dataset.station || b.dataset.mine) {      // a station, an iTunes playlist or one of your own: its page
      if ($('#lib-music').hidden) MUSIC.stack = [];
      const st = b.dataset.station || '';
      const spec = b.dataset.mine ? { kind: 'mine', id: b.dataset.mine, title: b.dataset.name }
        : st.startsWith('playlist:') ? { kind: 'playlist', id: st.slice(9), title: b.dataset.name }
        : { kind: 'station', id: st, title: b.dataset.name };
      openList(spec);
      return;
    }
    if (b.dataset.album) { if ($('#lib-music').hidden) MUSIC.stack = []; openAlbum(b.dataset.album, b.dataset.name, b.dataset.artistName); }
    else if (b.dataset.artist) { if ($('#lib-music').hidden) MUSIC.stack = []; openArtist(b.dataset.artist, b.dataset.name); }
    else if (b.dataset.song) {
      // inside an album: play the album from this song; a search hit: the song within its album
      if (!$('#lib-music').hidden && MUSIC.play && MUSIC.play.album) playMusic({ album: MUSIC.play.album, start: Number(b.dataset.i) });
      else playMusic({ track: b.dataset.song });
    }
  });

  // ------------------------------------------------------------------ podcasts
  const POD = { show: null, episodes: [] };
  const podArt = (id) => `<img src="/api/podcasts/art/${id}" alt="" loading="lazy" onerror="this.remove()">`;
  const showHtml = (sh) => `<button class="poster album" data-pod="${sh.id}">
      <span class="art">${sh.has_art ? podArt(sh.id) : '🎙'}</span><span class="name">${esc(sh.title)}</span>
      <span class="by">${sh.new ? sh.new + ' new' : esc(sh.author || '')}</span></button>`;
  async function loadPodcasts() {
    try {
      const r = await api('/api/podcasts');
      $('#pod-shows').innerHTML = r.shows.map(showHtml).join('') || empty('No podcasts yet. Find one below.');
    } catch (e) { $('#pod-shows').innerHTML = empty(e.message); }
  }
  async function searchPodcasts(q) {
    const box = $('#pod-results');
    box.hidden = !q;
    if (!q) return;
    box.innerHTML = '<div class="empty">Searching…</div>';
    try {
      const { results } = await api('/api/podcasts/search?q=' + encodeURIComponent(q));
      box.innerHTML = results.map((r) => `<div class="item">${r.art_url ? `<img src="${esc(r.art_url)}" alt="" style="width:64px;aspect-ratio:1">` : '<span class="glyph">🎙</span>'}
          <span class="meta"><span class="name">${esc(r.title)}</span><span class="sub">${esc(r.author || '')}${r.episodes ? ' · ' + r.episodes + ' episodes' : ''}</span></span>
          <span class="ep-actions">${r.subscribed ? '<span class="badge">Added</span>' : `<button class="chip accent" data-sub="${esc(r.feed_url)}">Add</button>`}</span></div>`).join('')
        || empty('Nothing matched "' + q + '".');
    } catch (e) { box.innerHTML = empty(e.message); }
  }
  async function subscribe(feedUrl) {
    const r = await act(() => post('/api/podcasts/subscribe', { feed_url: feedUrl }));
    if (r && r.show) { toast('Added ' + r.show.title); loadPodcasts(); openPodcast(r.show.id); }
  }
  const epHtml = (ep) => {
    const date = ep.date ? new Date(ep.date).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '';
    const state = ep.resume ? `<span class="badge">Resume ${fmt(ep.resume)}</span>` : (ep.played ? '<span class="badge">Played</span>' : '');
    return `<div class="item${ep.played ? ' played' : ''}">
      <button class="meta" data-ep="${ep.id}" style="text-align:left"><span class="name">${esc(ep.title)}</span>
      <span class="sub">${[date, ep.duration ? fmt(ep.duration) : ''].filter(Boolean).join(' · ')} ${state}</span></button>
      <span class="ep-actions">${ep.resume ? `<button class="chip" data-ep="${ep.id}" data-from-start="1">From start</button>` : ''}
      <button class="chip" data-played="${ep.id}" data-to="${ep.played ? '' : '1'}">${ep.played ? 'Mark unplayed' : 'Mark played'}</button></span></div>`;
  };
  async function openPodcast(id) {
    $('#lib-main').hidden = true; $('#lib-show').hidden = true; $('#lib-music').hidden = true; $('#lib-podcast').hidden = false;
    POD.show = id;
    $('#pod-episodes').innerHTML = '<div class="empty">Loading…</div>';
    window.scrollTo(0, 0);
    try {
      const r = await api('/api/podcasts/show/' + id);
      if (POD.show !== id) return;
      const sh = r.show || {};
      $('#pod-title').textContent = sh.title || '';
      $('#pod-author').textContent = sh.author || '';
      $('#pod-art').innerHTML = sh.has_art ? podArt(id) + '🎙' : '🎙';
      POD.episodes = r.episodes;
      $('#pod-episodes').innerHTML = r.episodes.map(epHtml).join('') || empty('No episodes yet.');
    } catch (e) { $('#pod-episodes').innerHTML = empty(e.message); }
  }
  $('#pod-search-form').addEventListener('submit', (e) => { e.preventDefault(); searchPodcasts($('#pod-search').value.trim()); });
  $('#pod-search').addEventListener('search', () => { if (!$('#pod-search').value) searchPodcasts(''); });
  $('#pod-rss-form').addEventListener('submit', (e) => { e.preventDefault(); const u = $('#pod-rss').value.trim(); if (u) subscribe(u); });
  $('#pod-back').addEventListener('click', () => loadLibrary());
  $('#pod-unsub').addEventListener('click', async () => {
    if (!POD.show || !confirm('Unsubscribe from ' + $('#pod-title').textContent + '?')) return;
    await act(() => api('/api/podcasts/' + POD.show, { method: 'DELETE' }), 'Unsubscribed');
    loadLibrary();
  });
  $('#view-library').addEventListener('click', (e) => {
    const b = e.target.closest('[data-pod], [data-sub], [data-ep], [data-played]');
    if (!b) return;
    if (b.dataset.pod) openPodcast(b.dataset.pod);
    else if (b.dataset.sub) subscribe(b.dataset.sub);
    else if (b.dataset.ep) {
      const ep = POD.episodes.find((x) => x.id === b.dataset.ep) || {};
      const fromStart = !!b.dataset.fromStart;
      act(async () => {
        const r = await post('/api/podcasts/play', { episode: b.dataset.ep, from_start: fromStart });
        $$('nav.tabs button')[0].click();
        toast(r.resumed ? 'Resuming from ' + fmt(r.resumed) : 'Playing ' + (ep.title || 'on the TV'));
        return r;
      });
    } else if (b.dataset.played) {
      act(async () => {
        await post('/api/podcasts/played', { episode: b.dataset.played, played: !!b.dataset.to });
        openPodcast(POD.show);
      });
    }
  });

  function playLibrary(id, start) {
    return act(async () => {
      const r = await post('/api/play', { id, start });
      $$('nav.tabs button')[0].click();
      return r;
    }, 'Playing on the TV');
  }

  function pickPoster(d) {
    if (d.type === 'Series') { openShow(d.id, d.name); return; }
    const resume = Number(d.resume || 0);
    if (!resume) { playLibrary(d.id, 0); return; }
    $('#sheet-title').textContent = d.name;
    $('#sheet-resume').textContent = 'Resume from ' + fmt(resume);
    const sheet = $('#sheet');
    sheet.classList.add('on');
    const close = () => sheet.classList.remove('on');
    $('#sheet-resume').onclick = () => { close(); playLibrary(d.id, Math.max(0, resume - 5)); };
    $('#sheet-start').onclick = () => { close(); playLibrary(d.id, 0); };
    $('#sheet-cancel').onclick = close;
    sheet.onclick = (e) => { if (e.target === sheet) close(); };
  }

  let upKind = 'movie';
  function paintKind() {
    $('#up-kind-movie').classList.toggle('on', upKind === 'movie');
    $('#up-kind-show').classList.toggle('on', upKind === 'show');
    $('#up-show').hidden = upKind !== 'show';
  }
  $$('[data-kind]').forEach((b) => b.addEventListener('click', () => { upKind = b.dataset.kind; paintKind(); }));
  paintKind();

  // ------------------------------------------------------------------ uploads
  // The card rides above the tabs on every tab: progress from the first byte
  // (XHR upload events; fetch can't report them), speed + time left, retries,
  // and resuming a half-sent file after a reload (the server keeps the pieces).
  const up = {
    card: $('#upcard'), name: $('#up-name'), dest: $('#up-dest'), pct: $('#up-pct'), fill: $('#up-fill'),
    state: $('#up-state'), rate: $('#up-rate'), hint: $('#up-hint'), actions: $('#up-actions'),
    retry: $('#up-retry'), tab: $('#tab-up'),
  };
  const UP = { busy: false, file: null, folder: '', lock: null, samples: [], paused: null, doneTimer: null };

  function upPaint(mode, { pct, state, rate = '' } = {}) {
    clearTimeout(UP.doneTimer);
    up.card.hidden = false;
    document.body.classList.add('uploading');
    ['working', 'moving', 'failed', 'done', 'paused'].forEach((m) => up.card.classList.toggle(m, m === mode));
    if (pct != null) { up.fill.style.width = pct.toFixed(1) + '%'; up.pct.textContent = Math.floor(pct) + '%'; }
    if (state != null) up.state.textContent = state;
    up.rate.textContent = rate;
    up.hint.hidden = mode !== 'working';
    up.actions.hidden = !['done', 'failed', 'paused'].includes(mode);
    up.retry.hidden = mode !== 'failed';
    $('#up-continue').hidden = mode !== 'paused';
    $('#up-discard').hidden = mode !== 'paused';
    $('#up-dismiss').hidden = mode === 'paused';
    up.tab.hidden = !['working', 'moving', 'paused'].includes(mode);
    up.tab.textContent = mode === 'moving' ? '…' : up.pct.textContent;
    if (mode === 'done') UP.doneTimer = setTimeout(upHide, 30000);
  }
  function upHide() { up.card.hidden = true; up.tab.hidden = true; document.body.classList.remove('uploading'); }
  function upLabel(file, folder) {
    up.name.textContent = file.name;
    up.dest.textContent = (folder.startsWith('Shows/') ? 'Shows › ' + folder.slice(6) : 'Movies') + ' · ' + size(file.size);
  }
  $('#up-dismiss').addEventListener('click', upHide);
  $('#up-retry').addEventListener('click', () => { if (UP.file) upload(UP.file, UP.folder); });
  $('#up-discard').addEventListener('click', async () => {
    if (!UP.paused || !confirm('Throw away the half-sent ' + UP.paused.name + '?')) return;
    try { await api('/api/upload/' + UP.paused.id, { method: 'DELETE' }); } catch (e) { /* gone already */ }
    UP.paused = null; upHide();
  });

  async function wake(on) {   // keep the phone awake while it sends
    try {
      if (on && 'wakeLock' in navigator && !UP.lock) {
        UP.lock = await navigator.wakeLock.request('screen');
        UP.lock.addEventListener('release', () => { UP.lock = null; });
      }
      if (!on && UP.lock) { await UP.lock.release(); UP.lock = null; }
    } catch (e) { /* not offered here; the hint covers it */ }
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden && UP.busy) wake(true); });
  window.addEventListener('beforeunload', (e) => { if (UP.busy) { e.preventDefault(); e.returnValue = ''; } });

  function rateText(sent, total) {
    const now = performance.now();
    UP.samples.push([now, sent]);
    while (UP.samples.length > 2 && now - UP.samples[0][0] > 20000) UP.samples.shift();
    const [t0, s0] = UP.samples[0];
    if (now - t0 < 3000) return 'measuring speed…';
    const bps = (sent - s0) / ((now - t0) / 1000);
    if (bps <= 0) return 'waiting on the connection…';
    const left = (total - sent) / bps, mins = Math.round(left / 60);
    const eta = left < 60 ? 'under a minute left' : mins < 60 ? `about ${mins} min left` : `about ${Math.floor(mins / 60)} h ${mins % 60} min left`;
    return `${(bps / 1e6).toFixed(1)} MB/s · ${eta}`;
  }

  function sendChunk(url, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open('POST', url);
      x.withCredentials = true;
      x.timeout = 15 * 60 * 1000;
      x.setRequestHeader('Content-Type', 'application/octet-stream');
      x.upload.onprogress = (e) => onProgress(e.loaded);
      x.onload = () => {
        let data = {};
        try { data = JSON.parse(x.responseText); } catch (e) { /* not JSON */ }
        if (x.status === 401) { location.href = '/login'; reject(new Error('Signed out')); return; }
        if (x.status >= 200 && x.status < 300) { resolve(data); return; }
        const err = new Error(data.detail || `Error ${x.status}`);
        err.status = x.status; err.data = data;
        reject(err);
      };
      x.onerror = () => reject(new Error('The connection dropped'));
      x.ontimeout = () => reject(new Error('The connection timed out'));
      x.send(blob);
    });
  }

  // sha256 of the first 64 KB of every 32 MB piece: lets the server prove the
  // pieces it already holds are this file before it carries on from them
  const CHUNK = 32 * 1024 * 1024, PROBE = 64 * 1024;
  async function fingerprints(file) {
    if (!(window.crypto && crypto.subtle)) return [];
    const out = [];
    for (let off = 0; off < file.size; off += CHUNK) {
      const buf = await file.slice(off, Math.min(file.size, off + PROBE)).arrayBuffer();
      const h = new Uint8Array(await crypto.subtle.digest('SHA-256', buf));
      out.push([...h].map((b) => b.toString(16).padStart(2, '0')).join(''));
    }
    return out;
  }

  async function waitMoved(id) {
    for (let n = 0; n < 900; n++) {
      await new Promise((res) => setTimeout(res, 2000));
      const st = await api('/api/upload/status/' + id);
      if (st.state === 'done') return;
      if (st.state === 'error') throw new Error(st.error || "Couldn't move it into the library");
    }
    throw new Error('Moving it into the library is taking too long');
  }

  async function upload(file, folder) {
    if (UP.busy) { toast('One upload at a time. This one is still going.', true); return; }
    UP.busy = true; UP.file = file; UP.folder = folder; UP.samples = []; UP.paused = null;
    upLabel(file, folder);
    upPaint('working', { pct: 0, state: 'Starting…', rate: '' });
    wake(true);
    try {
      upPaint('working', { state: 'Checking the file…' });
      const probes = await fingerprints(file);
      const start = await post('/api/upload/start', { name: file.name, size: file.size, folder, probes });
      const cs = start.chunk_size, chunks = Math.max(1, Math.ceil(file.size / cs));
      let i = start.next || 0, tries = 0;
      if (i) upPaint('working', { pct: 100 * Math.min(file.size, i * cs) / file.size, state: 'Picking up where it left off…' });
      while (i < chunks) {
        const base = i * cs;
        try {
          await sendChunk(`/api/upload/chunk/${start.id}/${i}`, file.slice(base, Math.min(file.size, base + cs)), (loaded) => {
            const sent = Math.min(file.size, base + loaded);
            upPaint('working', { pct: 100 * sent / file.size, state: `${size(sent)} of ${size(file.size)}`, rate: rateText(sent, file.size) });
          });
          i++; tries = 0;
        } catch (err) {
          if (err.status === 409 && tries < 3) {   // the server has a different piece count: go where it says
            const m = /(\d+)/.exec(err.message || '');
            const next = err.data && err.data.next != null ? err.data.next : (m ? Number(m[1]) : null);
            if (next != null) { i = next; tries++; continue; }
          }
          if (err.status === 404 || tries >= 5) throw err;
          tries++;
          upPaint('working', { state: `Connection hiccup, trying again (${tries} of 5)…`, rate: '' });
          await new Promise((res) => setTimeout(res, 2000 * tries));
        }
      }
      upPaint('moving', { pct: 100, state: 'Moving it into the library…', rate: '' });
      const fin = await post(`/api/upload/finish/${start.id}`);
      if (fin.moving) await waitMoved(start.id);
      upPaint('done', { pct: 100, state: '✓ In the library. Jellyfin is adding it.', rate: '' });
      toast('Upload finished');
      setTimeout(() => { if (currentView === 'library') loadLibrary(); }, 8000);
    } catch (err) {
      upPaint('failed', { state: 'Upload stopped: ' + err.message, rate: 'Try again picks up where it left off' });
    } finally {
      UP.busy = false; wake(false);
    }
  }

  $('#upload').addEventListener('change', (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    // the same file as a half-sent upload: continue it, into the folder it was going to
    const p = UP.paused;
    if (p && p.name === file.name && p.size === file.size) { upload(file, p.folder); return; }
    let folder = 'Movies';
    if (upKind === 'show') {
      const show = $('#up-show').value.trim();
      if (!show) { toast('Type the show name first', true); $('#up-show').focus(); return; }
      folder = 'Shows/' + show;
    }
    upload(file, folder);
  });

  async function checkPausedUploads() {   // a reload mid-upload: offer to continue
    if (UP.busy) return;
    try {
      const { uploads } = await api('/api/uploads');
      const p = uploads.find((u) => u.state === 'receiving' && u.received < u.size);
      if (!p) return;
      UP.paused = p;
      upLabel({ name: p.name, size: p.size }, p.folder || 'Movies');
      upPaint('paused', { pct: 100 * p.received / p.size, state: 'Paused. Choose the same file to finish it.', rate: '' });
    } catch (e) { /* older server: nothing to resume */ }
  }

  // ------------------------------------------------------------------ Discs
  // The CD/DVD drive on the server. A CD MusicBrainz knows is ripped into Music
  // by itself; a DVD waits for its name.
  let discTimer = null, discPick = null, discForm = null, discShown = '';
  function discView(on) {
    clearInterval(discTimer);
    discTimer = null;
    if (on) { loadDiscs(); discTimer = setInterval(loadDiscs, 2000); renderPush(); }
  }

  // Phone notifications: a disc that needs you, a finished rip, a failure.
  // The service worker (/sw.js) shows them; tapping one opens this tab.
  const pushOK = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
  const b64key = (s) => { const p = '='.repeat((4 - s.length % 4) % 4); const r = atob((s + p).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(r, (c) => c.charCodeAt(0)); };
  async function pushSub() {
    const reg = await navigator.serviceWorker.ready;
    return reg.pushManager.getSubscription();
  }
  async function renderPush() {
    const box = $('#disc-push');
    if (!pushOK()) { box.innerHTML = '<div class="disc-note">Notifications need the installed remote app (Chrome on Android).</div>'; return; }
    let on = false;
    try { const sub = await pushSub(); on = !!(sub && (await post('/api/push/check', { endpoint: sub.endpoint })).on); } catch (e) {}
    box.innerHTML = on
      ? `<div class="disc-step">🔔 Notifications are on for this phone</div>
         <div class="disc-note">You'll hear when a disc needs you, when a rip is done, or if something goes wrong.</div>
         <div class="row"><button class="chip" id="push-test">Send a test</button><button class="chip" id="push-off">Turn off</button></div>`
      : `<div class="disc-note">Get a notification on this phone when a disc needs you, when a rip is done (the tray pops out), or if something goes wrong.</div>
         <button class="chip accent" id="push-on">🔔 Notify me on this phone</button>`;
    const on_ = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    on_('#push-on', () => act(async () => {
      if (await Notification.requestPermission() !== 'granted') throw new Error('Notifications are blocked for this app in Android settings');
      const { key } = await api('/api/push/key');
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64key(key) });
      const r = await post('/api/push/subscribe', { subscription: sub.toJSON(), device: navigator.userAgent.slice(0, 80) });
      renderPush();
      return r;
    }, 'Notifications are on'));
    on_('#push-test', () => act(() => post('/api/push/test'), 'Sent'));
    on_('#push-off', () => act(async () => {
      const sub = await pushSub();
      if (sub) { await post('/api/push/unsubscribe', { endpoint: sub.endpoint }); await sub.unsubscribe(); }
      renderPush();
    }, 'Notifications are off'));
  }
  function openTab(view) {
    const b = $(`nav.tabs button[data-view="${view}"]`);
    if (b) b.click();
  }
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e.data && e.data.type === 'open' && String(e.data.url).includes('#discs')) openTab('discs');
    });
  }
  async function loadDiscs() {
    let d;
    try { d = await api('/api/disc'); }
    catch (e) { $('#disc-body').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    const html = renderDisc(d);
    if (html === discShown) return;           // don't redraw over someone's typing
    if (discForm && document.activeElement && discForm.contains(document.activeElement)) return;
    discShown = html;
    $('#disc-body').innerHTML = html;
    wireDisc(d);
  }
  const len = (s) => s ? fmt(s) : '';
  function cardHead(cover, title, sub, glyph) {
    return `<div class="album-head"><div class="art">${cover ? `<img src="${esc(cover)}" alt="" onerror="this.remove()">` : ''}${glyph}</div>
      <div class="meta"><div class="title">${esc(title)}</div><div class="sub">${sub}</div></div></div>`;
  }
  function jobBlock(job) {
    if (!job) return '';
    const running = ['starting', 'ripping', 'moving'].includes(job.state);
    if (running) {
      const eta = job.eta ? ` · about ${Math.max(1, Math.round(job.eta / 60))} min left` : '';
      return `<div class="disc-step">${esc(job.step || 'Ripping')}</div>
        <div class="disc-bar"><i style="width:${Math.max(2, job.percent || 0)}%"></i></div>
        <div class="disc-note">${job.percent || 0}%${eta}</div>
        <button class="chip" id="disc-stop">Stop</button>`;
    }
    if (job.state === 'done') {
      return `<div class="disc-step">✓ ${esc(job.step)}</div>
        <div class="disc-note">${esc((job.where || []).join(', '))}</div>
        ${job.warning ? `<div class="disc-note bad">${esc(job.warning)}</div>` : ''}`;
    }
    return `<div class="disc-note bad">${job.state === 'stopped' ? 'Stopped.' : esc(job.step || 'The rip failed.')}</div>`;
  }
  function renderDisc(d) {
    const disc = d.disc, job = d.job;
    const running = job && ['starting', 'ripping', 'moving'].includes(job.state);
    if (d.enabled === false || d.drive === 'disabled') {
      return `<div class="empty">Disc ripping is off. Set TV_DISCS=1 in the server's .env and restart it to use a USB CD/DVD drive.</div>`;
    }
    if (d.drive === 'none') return `<div class="empty">The disc drive isn't connected to the server.</div>`;
    if (!disc || ['empty', 'open'].includes(d.drive) && !running) {
      const last = d.last && d.last.state === 'done'
        ? `<div class="card disc-card">${cardHead(null, d.last.title || 'Last disc', esc(d.last.artist || ''), '♪')}${jobBlock(d.last)}
          ${d.last.unknown ? nameForm(d.last.names, d.last.n_tracks, 'saved') : ''}</div>` : '';
      return `<div class="empty">Put a CD or a DVD in the drive. A CD goes into your Music by itself; a DVD asks for its name first.</div>${last}`;
    }
    if (disc.kind === 'reading') return `<div class="empty">Reading the disc…</div>`;
    const eject = running ? '' : `<button class="chip" id="disc-eject">Eject</button>`;
    if (disc.kind === 'cd') {
      const rels = disc.releases || [];
      if (!rels.length) {
        if (disc.error) {
          return `<div class="card disc-card">${cardHead(null, 'A CD', `${disc.tracks || '?'} tracks`, '♪')}
            <div class="disc-note bad">${esc(disc.error)}</div><div class="row">${eject}</div></div>`;
        }
        const named = job && job.unknown ? job.names : disc.names;
        const done = job && job.state === 'done';
        const mode = done ? 'saved' : running ? 'ripping' : 'new';
        const title = (named && named.album) || 'A CD MusicBrainz doesn\'t know';
        return `<div class="card disc-card">${cardHead(null, title, `${disc.tracks || '?'} songs · ${esc(disc.duration || '')}`, '♪')}
          ${jobBlock(job)}${nameForm(named, disc.tracks, mode)}
          <div class="row">${running || done ? '' : `<button class="chip accent" id="disc-rip-unknown">Rip with these names</button>`}${eject}</div></div>`;
      }
      const pick = rels.find((r) => r.id === (discPick || disc.release)) || rels[0];
      const autoAt = disc.auto_at && !job ? new Date(disc.auto_at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
      const choices = rels.length > 1 && !running ? `<div class="disc-note">MusicBrainz knows ${rels.length} versions of this CD. Pick yours and tap Rip${autoAt ? `, or I'll rip the first one by itself at ${autoAt}` : ''}:</div>
        <div class="list">${rels.map((r) => `<button class="item${r.id === pick.id ? ' picked' : ''}" data-rel="${esc(r.id)}">
          <span class="meta"><span class="name">${esc(r.title)}</span><span class="sub">${esc(r.artist)}${r.year ? ' · ' + esc(r.year) : ''}${r.country ? ' · ' + esc(r.country) : ''}${r.discs > 1 ? ` · disc ${esc(r.disc_number)} of ${esc(r.discs)}` : ''}</span></span></button>`).join('')}</div>` : '';
      const sub = `${esc(pick.artist)}${pick.year ? ' · ' + esc(pick.year) : ''} · ${(pick.tracks || []).length} songs`;
      const tracks = `<div class="disc-tracks">${(pick.tracks || []).map((t) =>
        `<div><span>${esc(t.n)}</span><span>${esc(t.title)}</span><span>${len(t.length)}</span></div>`).join('')}</div>`;
      const already = disc.already && !job ? `<div class="disc-note">It's already in your Music. Rip it again only if something went wrong last time.</div>` : '';
      const ripBtn = running || (job && job.state === 'done') ? '' : `<button class="chip accent" id="disc-rip">${disc.already ? 'Rip it again' : 'Rip to Music'}</button>`;
      return `<div class="card disc-card">${cardHead(pick.cover, pick.title, sub, '♪')}${already}${jobBlock(job)}
        <div class="row">${ripBtn}${eject}</div>${choices}${tracks}</div>`;
    }
    if (disc.kind === 'dvd') {
      const form = running || (job && job.state === 'done') ? '' : `<form id="disc-dvd-form" class="disc-fields">
          <input class="field" id="disc-name" value="${esc(disc.title)}" placeholder="The movie's title" maxlength="120" autocomplete="off">
          <input class="field year" id="disc-year" type="number" inputmode="numeric" placeholder="Year" min="1890" max="2100">
        </form>
        <div class="disc-note">The name on the disc is "${esc(disc.label || '')}". Fix the title above if it's off; the year helps Jellyfin find its poster.</div>`;
      const ripBtn = running || (job && job.state === 'done') ? '' : `<button class="chip accent" id="disc-rip">Rip to Movies</button>`;
      return `<div class="card disc-card">${cardHead(null, disc.title, `DVD · main feature ${len(disc.length)}${disc.format ? ' · ' + esc(disc.format) : ''}`, '🎬')}
        ${disc.already && !job ? `<div class="disc-note">Something with this name is already in your Movies.</div>` : ''}
        ${form}${jobBlock(job)}<div class="row">${ripBtn}${eject}</div></div>`;
    }
    return `<div class="card disc-card">${cardHead(null, disc.title || 'A disc', esc(disc.error || ''), '💿')}
      <div class="row"><button class="chip accent" id="disc-reread">Read it again</button>${eject}</div></div>`;
  }
  // A CD MusicBrainz doesn't know: your names for it. Saved as you type (so a redraw never
  // loses them), put on the songs when the rip ends; an album already in Music is renamed
  // there with "Save to my Music".
  function nameForm(names, n, mode) {
    names = names || {};
    const t = names.tracks || [], count = Math.max(n || 0, t.length);
    const help = mode === 'saved' ? 'It\'s in your Music. Fix any names here and tap Save to my Music.'
      : mode === 'ripping' ? 'Type the names while it rips; they go on the songs when it\'s done.'
        : 'MusicBrainz doesn\'t know this CD. Type the names (you can paste a whole track list into the first song box), check them, then rip. Names left blank stay "Unknown Track".';
    return `<div class="disc-note">${help}</div>
      <form id="disc-cd-form" class="disc-names" data-mode="${mode}" autocomplete="off">
        <div class="disc-fields"><input class="field" id="disc-artist" value="${esc(names.artist || '')}" placeholder="Artist" maxlength="120">
          <input class="field year" id="disc-cd-year" inputmode="numeric" value="${esc(names.year || '')}" placeholder="Year" maxlength="4"></div>
        <input class="field" id="disc-album" value="${esc(names.album || '')}" placeholder="Album" maxlength="120">
        ${Array.from({ length: count }, (_, i) => `<div class="track-name"><span>${i + 1}</span>
          <input class="field" data-track="${i}" value="${esc(t[i] || '')}" placeholder="Song ${i + 1}" maxlength="200"></div>`).join('')}
        ${mode === 'saved' ? '<div class="row"><button type="button" class="chip accent" id="disc-names-apply">Save to my Music</button></div>' : ''}
      </form>`;
  }
  function collectNames() {
    return { artist: $('#disc-artist').value.trim(), album: $('#disc-album').value.trim(), year: $('#disc-cd-year').value.trim(),
      tracks: $$('#disc-cd-form [data-track]').map((el) => el.value.trim()) };
  }
  // a pasted track list: "1. Song (3:45)", "01 - Song", "Track 2: Song", one per line
  function trackLines(text) {
    return text.split(/\r?\n/).map((l) => l.trim()
      .replace(/^(track\s*)?\d{1,2}\s*[.):\-–—]?\s+/i, '')
      .replace(/\s*[([]?\d{1,2}:\d{2}(:\d{2})?[)\]]?\s*$/, '')
      .replace(/^["“”']+|["“”']+$/g, '').trim()).filter(Boolean);
  }
  let namesTimer = null;
  function wireNames() {
    const form = $('#disc-cd-form');
    if (!form) return;
    const save = (apply) => post('/api/disc/names', { ...collectNames(), apply: !!apply });
    form.addEventListener('input', (e) => {
      if (form.dataset.mode === 'saved') return;          // a library album is renamed only on Save
      clearTimeout(namesTimer);
      namesTimer = setTimeout(() => save(false).catch(() => {}), 700);
    });
    $$('#disc-cd-form [data-track]').forEach((el) => el.addEventListener('paste', (e) => {
      const text = (e.clipboardData || window.clipboardData).getData('text');
      if (!/\n/.test(text)) return;                       // one line: an ordinary paste
      e.preventDefault();
      const lines = trackLines(text), boxes = $$('#disc-cd-form [data-track]');
      lines.forEach((line, k) => { const box = boxes[Number(el.dataset.track) + k]; if (box) box.value = line; });
      form.dispatchEvent(new Event('input'));
      toast(`${Math.min(lines.length, boxes.length - Number(el.dataset.track))} song names filled in`);
    }));
    const apply = $('#disc-names-apply');
    if (apply) apply.addEventListener('click', () => act(() => save(true), 'Saved to your Music'));
  }
  function wireDisc(d) {
    discForm = $('#disc-dvd-form') || $('#disc-cd-form');
    wireNames();
    const on = (id, fn) => { const el = $(id); if (el) el.addEventListener('click', fn); };
    on('#disc-eject', () => act(() => post('/api/disc/eject'), 'Ejecting'));
    on('#disc-stop', () => { if (confirm('Stop ripping this disc?')) act(() => post('/api/disc/stop'), 'Stopped'); });
    on('#disc-rip-unknown', () => { clearTimeout(namesTimer); act(() => post('/api/disc/rip', { unknown: true, names: collectNames() }), 'Ripping'); });
    on('#disc-reread', () => act(() => post('/api/disc/reread'), 'Reading it again'));
    on('#disc-rip', () => {
      if (d.disc && d.disc.kind === 'dvd') {
        const year = parseInt($('#disc-year').value, 10);
        act(() => post('/api/disc/rip', { name: $('#disc-name').value.trim(), year: year || null }), 'Ripping the DVD');
      } else {
        act(() => post('/api/disc/rip', { release: discPick || (d.disc && d.disc.release) }), 'Ripping the CD');
      }
      discShown = '';
      setTimeout(loadDiscs, 600);
    });
    $$('#disc-body [data-rel]').forEach((b) => b.addEventListener('click', () => { discPick = b.dataset.rel; discShown = ''; loadDiscs(); }));
    if (discForm) discForm.addEventListener('submit', (e) => { e.preventDefault(); $('#disc-rip').click(); });
  }

  if (location.hash === '#discs') openTab('discs');
  refresh().then(peekIfPlaying);
  prepareAndroid();
  checkPausedUploads();
  schedulePoll();
})();
