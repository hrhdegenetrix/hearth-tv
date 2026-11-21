"""
Hearth — the brains of a home TV box (a Raspberry Pi on the TV, a phone as the remote).

The Pi on the TV (receiver/receiver.py) is only a screen; this service owns
everything else and serves the phone remote:

  remote    static/index.html — a home-screen web app: now playing, transport,
            d-pad / touchpad / keyboard for the browser apps, the library,
            music, radio, podcasts, paste-a-link casting. Its own login
            (TV_PASSWORD, a 1-year cookie).
  library   Jellyfin's catalog (movies, shows, music). The Pi's mpv streams
            the original files straight from Jellyfin, and this service
            reports progress back so resume points and watched marks stay true;
            resumable chunked uploads drop new videos into the media share.
  music     a queue on the Pi's mpv, the A-to-Z index, radio stations that
            play on and on (radio.py), daily lists, a song menu, Jellyfin
            playlists, podcasts (podcasts.py).
  casting   YouTube / any yt-dlp link → mpv on the Pi; streaming-site links
            (Prime, Max, Tubi, Netflix...) → the Pi's kiosk browser.
  discs     optional (TV_DISCS=1): a USB CD/DVD drive, ripped into the share.
  status    one poller caches the Pi's state for the remote.

Doc: docs/server.md. Systemd unit: systemd/hearth.service.
"""

import asyncio
import hmac
import json
import logging
import os
import random
import re
import shutil
import sys
import time
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / '.env')
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from hearth.errors import if_errors  # noqa: E402
from hearth.jellyfin import Jellyfin  # noqa: E402
from hearth.music_index import MusicIndex  # noqa: E402
from hearth import music_history  # noqa: E402
from hearth.radio import BATCH as RADIO_BATCH, Radio  # noqa: E402
from hearth.podcasts import FeedError, NotFound, Podcasts  # noqa: E402
from hearth.discs import Discs  # noqa: E402
from hearth.push import AVAILABLE as PUSH_AVAILABLE, Push  # noqa: E402
from hearth import flamingo_genres  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
log = logging.getLogger('hearth')


def _on(name, default='0'):
    return os.getenv(name, default).strip().lower() not in ('0', 'false', 'no', 'off', '')


PORT = int(os.getenv('TV_PORT', '5080'))
RECEIVER = os.getenv('TV_RECEIVER_URL', 'http://hearth-tv.local:8770').rstrip('/')
RECEIVER_TOKEN = os.getenv('TV_RECEIVER_TOKEN', '')
# The media share Jellyfin reads (Movies/, Shows/<Show>/, Music/), as this host sees it.
LIBRARY = Path(os.getenv('TV_MEDIA_ROOT', '/mnt/media'))
DISCS_ON = _on('TV_DISCS')
STATIC_DIR = Path(__file__).parent / 'static'
DATA_DIR = Path(__file__).parent / 'data'
STATE_PATH = DATA_DIR / 'state.json'
UPLOAD_DIR = DATA_DIR / 'uploads'     # chunks assemble locally, then move to the share
STATION_ART_DIR = DATA_DIR / 'station_art'

COOKIE = 'tv_session'
SESSION_MAX_AGE = 60 * 60 * 24 * 365
VIDEO_EXTS = {'.mp4', '.mkv', '.m4v', '.webm', '.mov', '.avi', '.ts', '.mpg',
              '.mpeg', '.wmv', '.flv', '.ogv'}
# Links that only play inside the site (logins, DRM) open in the kiosk browser.
BROWSER_DOMAINS = ('amazon.', 'primevideo.', 'max.com', 'hbomax.', 'netflix.',
                   'disneyplus.', 'hulu.', 'peacocktv.', 'tubitv.', 'paramountplus.',
                   'tv.apple.com', 'crunchyroll.', 'pluto.tv', 'rifftrax.com',
                   'gizmoplex.', 'vhx.tv')
APPS = ('youtube', 'tubi', 'prime', 'max', 'pluto', 'android')
# yt-dlp (and the JS runtime it wants, deno) may live next to this Python (a venv
# or conda env) rather than on systemd's PATH: look there too.
_BIN = os.path.dirname(sys.executable)
_TOOL_PATH = _BIN + os.pathsep + os.environ.get('PATH', '')
YTDLP = shutil.which('yt-dlp', path=_TOOL_PATH) or 'yt-dlp'
# Errors to the phone use 424, not 502/503 — Cloudflare (a tunnel in front of
# the remote) swaps its own page in for those and the remote loses the message.
UPSTREAM_ERROR = 424
UPLOAD_NAS_ERROR = 424

_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=4.0))
JF = Jellyfin()
JF_PLAY = {'item': None, 'last_pos': 0.0, 'last_report': 0.0, 'paused': False}


@asynccontextmanager
async def _lifespan(_app):
    for key in ('TV_PASSWORD', 'TV_SECRET_KEY', 'TV_RECEIVER_TOKEN', 'JELLYFIN_API_KEY', 'JELLYFIN_USER'):
        if not os.getenv(key):
            log.warning('%s is not set in .env', key)
    _load_state()
    _load_uploads()
    poller = asyncio.create_task(_poller())
    nas_watch = asyncio.create_task(_nas_watch())
    _load_listens()
    try:
        RADIO['resume'] = json.loads(RADIO_SESSION_PATH.read_text())
    except (OSError, ValueError):
        RADIO['resume'] = None
    podcast_refresh = asyncio.create_task(_podcast_refresher())
    disc_watch = asyncio.create_task(DISCS.watch()) if DISCS_ON else None
    yield
    if disc_watch:
        await DISCS.stop()              # a rip in progress would otherwise run on orphaned
        disc_watch.cancel()
    _save_radio_session()
    podcast_refresh.cancel()
    await PODCASTS.client.aclose()
    nas_watch.cancel()
    poller.cancel()
    _save_state()


app = FastAPI(title='Hearth', version='1.0', docs_url=None, redoc_url=None, lifespan=_lifespan)



# --------------------------------------------------------------------------- #
# Auth — one household password (TV_PASSWORD in .env)
# --------------------------------------------------------------------------- #
def _serializer(salt):
    secret = os.getenv('TV_SECRET_KEY', '')
    if not secret:
        raise RuntimeError('TV_SECRET_KEY is not set in .env')
    return URLSafeTimedSerializer(secret, salt=salt)


def _password_ok(password):
    """The remote's password, compared in constant time (fail closed: no password set = no way in)."""
    expected = os.environ.get('TV_PASSWORD')
    if not expected or not password:
        return False
    return hmac.compare_digest(str(password), str(expected))


def _signed_in(request: Request):
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    try:
        _serializer('tv-session').loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


def require_session(request: Request):
    if not _signed_in(request):
        raise HTTPException(status_code=401, detail='Sign in first')


_failed_logins = {}


def _client_ip(request: Request):
    return request.headers.get('cf-connecting-ip') or (request.client.host if request.client else '?')


class LoginBody(BaseModel):
    password: str


@app.post('/api/login')
async def api_login(body: LoginBody, request: Request):
    ip = _client_ip(request)
    now = time.time()
    recent = [t for t in _failed_logins.get(ip, []) if now - t < 900]
    if len(recent) >= 5:
        raise HTTPException(status_code=429, detail='Too many tries — wait a few minutes')
    if not _password_ok(body.password):
        recent.append(now)
        _failed_logins[ip] = recent
        raise HTTPException(status_code=401, detail="That password didn't match")
    _failed_logins.pop(ip, None)
    response = JSONResponse({'ok': True})
    # Secure cookies only travel over HTTPS: set TV_COOKIE_SECURE=1 behind a tunnel
    # or `tailscale serve`, leave it 0 for a plain-http remote on the home network.
    secure = _on('TV_COOKIE_SECURE')
    response.set_cookie(COOKIE, _serializer('tv-session').dumps({'u': 'home'}),
                        httponly=True, samesite='lax', secure=secure, max_age=SESSION_MAX_AGE)
    return response


@app.post('/api/logout')
async def api_logout():
    response = JSONResponse({'ok': True})
    response.delete_cookie(COOKIE)
    return response


# --------------------------------------------------------------------------- #
# The Pi
# --------------------------------------------------------------------------- #
async def receiver(method, path, body=None, timeout=15.0):
    try:
        r = await _client.request(method, RECEIVER + path, json=body, timeout=timeout,
                                  headers={'X-TV-Token': RECEIVER_TOKEN})
    except httpx.HTTPError:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail="The TV box isn't answering")
    if r.status_code >= 400:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail='The TV box said %d' % r.status_code)
    return r


async def receiver_json(method, path, body=None, timeout=15.0):
    return (await receiver(method, path, body, timeout)).json()


# --------------------------------------------------------------------------- #
# State: what we told the TV to play + resume positions
# --------------------------------------------------------------------------- #
STATUS = {'online': False, 'data': None, 'at': 0.0, 'ok_at': 0.0}
# One slow answer isn't "offline": the Pi stalls for a few seconds while it
# opens an app, and the remote would flash "The TV box isn't answering".
OFFLINE_AFTER_SECONDS = 15
NOW = {'kind': None, 'id': None, 'title': None, 'url': None, 'source': None, 'started': None}
_state = {'positions': {}}
_state_dirty_at = 0.0


def _load_state():
    global _state
    try:
        with open(STATE_PATH) as f:
            _state = json.load(f)
    except (OSError, ValueError):
        _state = {'positions': {}}
    _state.setdefault('positions', {})


@if_errors
def _save_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(_state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def _remember(item_id, position, duration):
    global _state_dirty_at
    positions = _state['positions']
    if duration and position > duration - 90:
        positions.pop(item_id, None)  # finished — next time starts fresh
    elif position > 30:
        positions[item_id] = {'position': round(position, 1),
                              'duration': round(duration or 0, 1), 'updated': time.time()}
    else:
        return
    if time.time() - _state_dirty_at > 10:
        _state_dirty_at = time.time()
        _save_state()


@if_errors
async def _poll_once():
    try:
        data = await receiver_json('GET', '/status', timeout=6)
    except HTTPException:
        if time.time() - STATUS['ok_at'] > OFFLINE_AFTER_SECONDS:
            STATUS['online'] = False
        STATUS['at'] = time.time()
        return
    STATUS.update(online=True, data=data, at=time.time(), ok_at=time.time())
    player = data.get('player') or {}
    if (player.get('active') and NOW.get('kind') == 'library'
            and player.get('path') == NOW.get('url') and player.get('position') is not None):
        _remember(NOW['id'], player['position'], player.get('duration'))
    await _jellyfin_progress(player)
    await _music_progress(data)


@if_errors
async def _jellyfin_progress(player):
    """Keep Jellyfin's resume point and watched state in step with the TV."""
    item = JF_PLAY['item']
    if not item:
        return
    ours = player.get('active') and player.get('path') == NOW.get('url') and NOW.get('kind') == 'jellyfin'
    if ours:
        pos = player.get('position') or 0.0
        paused = bool(player.get('paused'))
        JF_PLAY['last_pos'] = pos
        if time.time() - JF_PLAY['last_report'] > 10 or paused != JF_PLAY['paused']:
            await JF.report('progress', item, pos, paused)
            JF_PLAY.update(last_report=time.time(), paused=paused)
    elif time.time() - (NOW.get('started') or 0) > 15:
        # Our item stopped (finished, stopped, or replaced) — after a grace
        # period for mpv to open the stream: close the Jellyfin session.
        await JF.report('stop', item, JF_PLAY['last_pos'])
        JF_PLAY.update(item=None, last_pos=0.0, last_report=0.0, paused=False)


async def _poller():
    while True:
        await _poll_once()
        await asyncio.sleep(2 if STATUS['online'] else 10)


def _no_key(url):
    """Jellyfin stream URLs carry the API key (mpv can't send headers) — keep it off the phone."""
    return re.sub(r'([?&]api_key=)[^&]+', r'\1hidden', url) if isinstance(url, str) else url


def _status_payload():
    data = STATUS['data'] or {}
    player = dict(data.get('player') or {})
    player['path'] = _no_key(player.get('path'))
    now = dict(NOW)
    now['url'] = _no_key(now.get('url'))
    if not player.get('active') and now.get('kind') in ('library', 'link', 'music'):
        now = {k: None for k in NOW}
    return {'online': STATUS['online'], 'age': round(time.time() - STATUS['at'], 1),
            'mode': data.get('mode', 'offline' if not STATUS['online'] else 'home'),
            'player': player, 'browser': data.get('browser') or {},
            'playing': data.get('playing'), 'music': data.get('music'),
            'audio': data.get('audio') or {}, 'now': now,
            'android': data.get('android'),   # opening / starting / restarting, while it lasts
            'radio': ({'name': RADIO['session']['name'], 'station': RADIO['session']['station']}
                      if RADIO['session'] else None),
            'loved': ((data.get('music') or {}).get('track') or {}).get('id') in set(_radio_store().get('loves') or [])}


@app.get('/api/status', dependencies=[Depends(require_session)])
async def api_status():
    if time.time() - STATUS['at'] > 1.5:
        await _poll_once()
    return _status_payload()


# --------------------------------------------------------------------------- #
# Library
# --------------------------------------------------------------------------- #
@app.get('/api/library', dependencies=[Depends(require_session)])
async def api_library():
    """Jellyfin's catalog for the remote: continue watching, movies, shows."""
    try:
        cont, movies, shows = await asyncio.gather(JF.continue_watching(), JF.movies(), JF.shows())
    except httpx.HTTPError:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail="Jellyfin isn't answering")
    return {'continue': cont, 'movies': movies, 'shows': shows}


@app.get('/api/library/show/{series_id}', dependencies=[Depends(require_session)])
async def api_library_show(series_id: str):
    try:
        return {'episodes': await JF.episodes(series_id)}
    except httpx.HTTPError:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail="Jellyfin isn't answering")


@app.get('/api/jf/image/{item_id}', dependencies=[Depends(require_session)])
async def api_jf_image(item_id: str, w: int = 360):
    if not re.fullmatch(r'[0-9a-f]{32}', item_id):
        raise HTTPException(status_code=400, detail='Bad id')
    data, ctype = await JF.image(item_id, min(max(w, 80), 900))
    if not data:
        raise HTTPException(status_code=404, detail='No image')
    return Response(data, media_type=ctype, headers={'Cache-Control': 'private, max-age=86400'})


class PlayBody(BaseModel):
    id: str
    start: float | None = None


@app.post('/api/play', dependencies=[Depends(require_session)])
async def api_play(body: PlayBody):
    """Play a Jellyfin item on the TV (the Pi's mpv streams the original file)."""
    if not re.fullmatch(r'[0-9a-f]{32}', body.id):
        raise HTTPException(status_code=400, detail='Bad id')
    try:
        item = await JF.item(body.id)
    except httpx.HTTPError:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail="Jellyfin isn't answering")
    title = item['name']
    if item.get('series'):
        title = '%s — S%sE%s %s' % (item['series'], item.get('season') or '?', item.get('episode') or '?', item['name'])
    start = item.get('resume') or 0 if body.start is None else body.start
    url = JF.stream_url(body.id)
    if JF_PLAY['item'] and JF_PLAY['item'] != body.id:
        await _jf_safe_report('stop', JF_PLAY['item'], JF_PLAY['last_pos'])
    await receiver_json('POST', '/play', {'url': url, 'title': title, 'start': start}, timeout=25)
    NOW.update(kind='jellyfin', id=body.id, title=title, url=url, source='library', started=time.time())
    JF_PLAY.update(item=body.id, last_pos=start, last_report=time.time(), paused=False)
    await _jf_safe_report('start', body.id, start)
    return {'ok': True, 'title': title}


def require_tv(request: Request):
    """Calls from the TV box's own home screen, relayed by the receiver."""
    token = request.headers.get('X-TV-Token', '')
    if not RECEIVER_TOKEN or not hmac.compare_digest(token, RECEIVER_TOKEN):
        raise HTTPException(status_code=401, detail='Bad token')


@app.get('/api/tv/library', dependencies=[Depends(require_tv)])
async def api_tv_library():
    lib = await api_library()
    try:
        index = await music_index()
        lib['albums'] = index.recent(40)     # the TV's Music shelf
        lib.update(await _radio_cards())      # the Radio shelf (§ Radio)
        lib['songs'] = len(RADIO['engine'].music)   # what Shuffle all plays
        lib['mine'] = await _my_playlists()   # the household's own playlists (§ Lists and the queue)
    except httpx.HTTPError:
        lib['albums'], lib['songs'] = [], 0
    lib.setdefault('stations', [])
    lib.setdefault('playlists', [])
    lib['podcasts'] = PODCASTS.shows()        # the Podcasts shelf (§ Podcasts)
    return lib


@app.get('/api/tv/show/{series_id}', dependencies=[Depends(require_tv)])
async def api_tv_show(series_id: str):
    return await api_library_show(series_id)


@app.get('/api/tv/jf_image/{item_id}', dependencies=[Depends(require_tv)])
async def api_tv_jf_image(item_id: str, w: int = 360):
    return await api_jf_image(item_id, w)


@app.post('/api/tv/play', dependencies=[Depends(require_tv)])
async def api_tv_play(body: PlayBody):
    return await api_play(body)


@if_errors
async def _jf_safe_report(kind, item_id, position):
    await JF.report(kind, item_id, position)


# --------------------------------------------------------------------------- #
# Music: Jellyfin's Music library, browsed on the phone (or the TV's Music
# shelf) and played by the Pi's mpv as a queue. Doc: docs/server.md § Music.
# --------------------------------------------------------------------------- #
MUSIC_PLAY = {'id': None, 'pos': 0.0}
# The whole collection, A to Z, for the TV's browsing (music_index.py). Rebuilt
# when it's 10 minutes old, or after the share watcher has Jellyfin rescan.
MUSIC_INDEX = {'index': None, 'at': 0.0}
MUSIC_INDEX_TTL = 600
_music_index_lock = asyncio.Lock()


async def music_index():
    async with _music_index_lock:
        if not MUSIC_INDEX['index'] or time.time() - MUSIC_INDEX['at'] > MUSIC_INDEX_TTL:
            MUSIC_INDEX['index'] = MusicIndex(*await JF.music_items())
            MUSIC_INDEX['at'] = time.time()
    return MUSIC_INDEX['index']


def _check_jf_id(value):
    if value is not None and not re.fullmatch(r'[0-9a-f]{32}', value):
        raise HTTPException(status_code=400, detail='Bad id')


async def _jf_call(coro):
    try:
        return await coro
    except httpx.HTTPError:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail="Jellyfin isn't answering")
    except RuntimeError as e:         # e.g. JELLYFIN_USER missing from .env
        raise HTTPException(status_code=UPSTREAM_ERROR, detail=str(e))


@app.get('/api/music', dependencies=[Depends(require_session)])
async def api_music():
    recent, artists = await _jf_call(asyncio.gather(JF.recent_albums(), JF.artists()))
    return {'recent': recent, 'artists': artists}


@app.get('/api/music/artist/{artist_id}', dependencies=[Depends(require_session)])
async def api_music_artist(artist_id: str):
    _check_jf_id(artist_id)
    return {'albums': await _jf_call(JF.artist_albums(artist_id))}


@app.get('/api/music/album/{album_id}', dependencies=[Depends(require_session)])
async def api_music_album(album_id: str):
    _check_jf_id(album_id)
    return {'tracks': await _jf_call(JF.album_tracks(album_id))}


@app.get('/api/music/search', dependencies=[Depends(require_session)])
async def api_music_search(q: str = ''):
    q = q.strip()[:80]
    if not q:
        return {'artists': [], 'albums': [], 'tracks': []}
    return await _jf_call(JF.search_music(q))


class MusicBody(BaseModel):
    album: str | None = None
    artist: str | None = None
    track: str | None = None      # this song, inside its album when it has one
    albums: list[str] = []        # a TV album card: its folders (music_index.py)
    tracks: list[str] = []        # ...and its loose songs
    everything: bool = False      # shuffle the whole collection
    shuffle: bool = False
    start: int = 0


@app.post('/api/music/play', dependencies=[Depends(require_session)])
async def api_music_play(body: MusicBody):
    if len(body.albums) + len(body.tracks) > 200:
        raise HTTPException(status_code=400, detail='Too many')
    for value in (body.album, body.artist, body.track, *body.albums, *body.tracks):
        _check_jf_id(value)
    start = max(0, body.start)
    if body.everything:           # every song, shuffled
        # Songs only: podcasts, audiobooks and comedy that live in the music library,
        # and second copies of a few songs, stay out; radio.py sorts those out.
        # Spoken word has its own station.
        dislikes = set(_radio_store().get('dislikes') or [])       # "Not for me"
        tracks = [JF._track(s.raw) for s in (await _jf_call(radio_engine())).music if s.id not in dislikes]
        random.shuffle(tracks)
    elif body.albums or body.tracks:
        index = await _jf_call(music_index())
        tracks = [JF._track(t) for t in index.songs(body.albums, body.tracks)]
    elif body.album:
        tracks = await _jf_call(JF.album_tracks(body.album))
    elif body.artist:
        tracks = await _jf_call(JF.artist_tracks(body.artist))
    elif body.track:
        song = await _jf_call(JF.track(body.track))
        tracks = await _jf_call(JF.album_tracks(song['album_id'])) if song.get('album_id') else [song]
        start = next((i for i, t in enumerate(tracks) if t['id'] == song['id']), 0)
    else:
        raise HTTPException(status_code=400, detail='What should it play?')
    tracks = _drop_copies(tracks, keep=body.track)
    if body.track:                # the song that was tapped, wherever it landed
        start = next((i for i, t in enumerate(tracks) if t['id'] == body.track), start)
    if not tracks:
        raise HTTPException(status_code=404, detail='Nothing to play there')
    if body.shuffle and not body.everything:
        random.shuffle(tracks)
        start = 0
    RADIO['session'] = None
    return await _play_queue(tracks, min(start, len(tracks) - 1))


def _drop_copies(tracks, keep=None):
    """Leave out a song's second copy (radio.Radio._mark_copies: the same song in
    the library twice), except the one that was picked."""
    engine = RADIO['engine']
    if not engine:
        return tracks
    return [t for t in tracks if t['id'] == keep or not getattr(engine.songs.get(t['id']), 'dupe', False)]


async def _play_queue(tracks, start=0, start_at=0, **extra):
    """Send the TV a queue: songs (Jellyfin's original files), a radio station's
    first songs (extra: radio, station), or a podcast episode (its own url)."""
    if JF_PLAY['item']:           # a movie was playing: close its Jellyfin session
        await _jf_safe_report('stop', JF_PLAY['item'], JF_PLAY['last_pos'])
        JF_PLAY.update(item=None, last_pos=0.0, last_report=0.0, paused=False)
    queue = [{**t, 'url': t.get('url') or JF.audio_url(t['id']), **extra} for t in tracks]
    await receiver_json('POST', '/music/play', {'queue': queue, 'start': start, 'start_at': start_at},
                        timeout=30)
    first = tracks[start]
    NOW.update(kind='music', id=first['id'], title=first['title'], url=None,
               source=first.get('kind') or ('radio' if extra.get('radio') else 'music'), started=time.time())
    return {'ok': True, 'count': len(queue), 'title': first['title']}


@app.get('/api/music/queue', dependencies=[Depends(require_session)])
async def api_music_queue():
    """The TV's whole music queue and the song playing, for the phone's Now tab."""
    return await receiver_json('GET', '/music/queue')


@app.post('/api/tv/music/play', dependencies=[Depends(require_tv)])
async def api_tv_music_play(body: MusicBody):
    return await api_music_play(body)


@app.get('/api/tv/music/index', dependencies=[Depends(require_tv)])
async def api_tv_music_index():
    index = await _jf_call(music_index())
    engine = await _jf_call(radio_engine())
    return {'albums': index.albums(), 'songs': len(engine.music),     # what Shuffle all plays
            'artists': [{k: v for k, v in a.items() if k != 'sort'} for a in index.artists]}


@app.get('/api/tv/music/artist/{artist_id}', dependencies=[Depends(require_tv)])
async def api_tv_music_artist(artist_id: str):
    _check_jf_id(artist_id)
    return {'albums': (await _jf_call(music_index())).artist_albums(artist_id)}


@if_errors
async def _music_progress(data):
    """From the status poll, for whatever the TV's queue is playing: Jellyfin's
    play counts and "recently played" for songs, the listening log and the
    radio's skips, podcast resume points, and topping up a radio station."""
    music = data.get('music') or {}
    track = music.get('track') or {}
    current = track.get('id')
    if current != MUSIC_PLAY['id']:
        prev = dict(MUSIC_PLAY)
        if prev['id'] and prev.get('kind') == 'podcast':
            PODCASTS.save_position(prev['id'], prev['pos'], prev.get('duration'))
        elif prev['id']:
            await _jf_safe_report('stop', prev['id'], prev['pos'])
            moved_on = current and music.get('index') == (prev.get('index') or 0) + 1
            skipped = bool(moved_on and prev['pos'] < min(SKIP_SECONDS, 0.33 * (prev.get('duration') or 600)))
            _note_listen(prev, skipped)
            session = RADIO['session']
            if skipped and session and prev.get('radio') == session['id'] and RADIO['engine']:
                RADIO['engine'].feedback(session, prev['id'], 'skip')
        if current and track.get('kind') != 'podcast':
            await _jf_safe_report('start', current, 0)
        MUSIC_PLAY.update(id=current, pos=0.0, index=music.get('index'), duration=track.get('duration'),
                          kind=track.get('kind'), radio=track.get('radio'), station=track.get('station'),
                          saved=time.time())
    elif current:
        MUSIC_PLAY['pos'] = (data.get('player') or {}).get('position') or MUSIC_PLAY['pos']
        if track.get('kind') == 'podcast' and time.time() - MUSIC_PLAY.get('saved', 0) > 10:
            PODCASTS.save_position(current, MUSIC_PLAY['pos'], track.get('duration'))
            MUSIC_PLAY['saved'] = time.time()
    resume = RADIO.get('resume')
    if resume and not RADIO['session'] and current and track.get('radio') == resume['id']:
        RADIO['resume'] = None
        await _resume_radio(resume)
    session = RADIO['session']
    if session:
        if current and track.get('radio') == session['id']:
            session['seen'] = time.time()
            left = (music.get('count') or 0) - (music.get('index') or 0) - 1
            if left < RADIO_TOP_UP_AT and not RADIO['topping_up']:
                RADIO['topping_up'] = True
                RADIO['task'] = asyncio.create_task(_radio_top_up(session))
        elif current or time.time() - session.get('seen', session['started']) > 60:
            RADIO['session'] = None       # something else is playing, or it stopped


# --------------------------------------------------------------------------- #
# Radio: stations that play on and on, from the household's own listening (the
# TV's listening log, an old iTunes library if there is one: music_history.py)
# and what Music Flamingo heard (flamingo_genres.py). radio.py picks the songs;
# this keeps the TV's queue topped up, notices skips, and keeps the hearts and
# "Not for this station". Doc: docs/radio.md.
# --------------------------------------------------------------------------- #
RADIO = {'engine': None, 'index': None, 'session': None, 'topping_up': False, 'store': None, 'task': None}
RADIO_STORE_PATH = DATA_DIR / 'radio.json'          # loves, Not for me, per-station bans, playlist names
# The station playing now, so a restart of this service (every update) picks it
# back up instead of letting the TV's queue run dry.
RADIO_SESSION_PATH = DATA_DIR / 'radio_session.json'
HISTORY_CACHE = DATA_DIR / 'itunes_history.json'
LISTEN_LOG = DATA_DIR / 'listen_log.jsonl'          # every song the TV played: how long, skipped?
RADIO_TOP_UP_AT = 4       # fewer songs than this left in the queue: add more
RADIO_TOP_UP = 15
SKIP_SECONDS = 30         # moving on to the next song this early is a skip
RECENT_HOURS = 12         # a station holds back what the TV played this recently
STATION_RE = re.compile(r'^(favorites|deepcuts|spoken|year:\d{4}|decade:\d{4}|genre:[a-z]+|'
                        r'artist:[0-9a-f]{32}|song:[0-9a-f]{32}|playlist:[0-9a-f]{12})$')
LISTENS = []              # [(time, song id)] for the last day
_radio_lock = asyncio.Lock()


def _radio_store():
    if RADIO['store'] is None:
        try:
            with open(RADIO_STORE_PATH) as f:
                RADIO['store'] = json.load(f)
        except (OSError, ValueError):
            RADIO['store'] = {'bans': {}, 'loves': []}
    return RADIO['store']


@if_errors
def _save_radio_store():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RADIO_STORE_PATH.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(RADIO['store'], f)
    os.replace(tmp, RADIO_STORE_PATH)


@if_errors
def _load_listens():
    try:
        with open(LISTEN_LOG) as f:
            lines = f.readlines()[-3000:]
    except OSError:
        return
    since = time.time() - 86400
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get('t', 0) > since:
            LISTENS.append((e['t'], e['id']))


@if_errors
def _note_listen(prev, skipped):
    entry = {'t': round(time.time()), 'id': prev['id'], 'secs': round(prev.get('pos') or 0),
             'of': prev.get('duration'), 'skipped': skipped, 'station': prev.get('station')}
    LISTENS.append((entry['t'], prev['id']))
    del LISTENS[:max(0, len(LISTENS) - 3000)]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LISTEN_LOG, 'a') as f:
        f.write(json.dumps(entry) + '\n')


def _recent_ids():
    since = time.time() - RECENT_HOURS * 3600
    return {i for t, i in LISTENS if t > since}


def _tv_listening():
    """{song id: (played through, skipped)} from everything the TV has played
    (listen_log.jsonl): a song heard to its last minute or past half counts as
    a play, an early move to the next one as a skip."""
    counts = {}
    try:
        with open(LISTEN_LOG) as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                played, skipped = counts.get(e.get('id'), (0, 0))
                secs, of = e.get('secs') or 0, e.get('of') or 0
                if e.get('skipped'):
                    skipped += 1
                elif of and (secs >= of / 2 or secs >= of - 60):
                    played += 1
                counts[e.get('id')] = (played, skipped)
    except OSError:
        pass
    return counts


async def radio_engine():
    index = await music_index()
    async with _radio_lock:
        heard_at = flamingo_genres.mtime()    # rebuilt when Flamingo's genres are refreshed (daily cron)
        if RADIO['engine'] is None or RADIO['index'] is not index or RADIO.get('heard_at') != heard_at:
            history = await asyncio.to_thread(music_history.load, str(HISTORY_CACHE), index.tracks)
            tv = await asyncio.to_thread(_tv_listening)
            heard = await asyncio.to_thread(flamingo_genres.load)
            RADIO.update(engine=Radio(index, history, _radio_store(), tv=tv, heard=heard), index=index,
                         heard_at=heard_at)
    return RADIO['engine']


async def _radio_cards():
    engine = await radio_engine()
    stations = engine.stations()
    for st in stations:                 # a cover, when one has been dropped into data/station_art/
        st['art'] = bool(_station_art_path(st['id']))
    return {'stations': stations, 'playlists': engine.playlist_cards()}


def _station_art_path(station):
    """A station's cover: hearth/data/station_art/<station id, lowercased, runs of
    anything but a-z0-9 replaced by _>.jpg (genre:classical is genre_classical.jpg),
    a 400 px square JPEG. Optional; a station without one shows a plain card."""
    path = STATION_ART_DIR / (re.sub(r'[^a-z0-9]+', '_', station.lower()).strip('_') + '.jpg')
    return path if path.is_file() else None


def _station_art(station):
    if not STATION_RE.match(station):
        raise HTTPException(status_code=400, detail='Unknown station')
    path = _station_art_path(station)
    if not path:
        raise HTTPException(status_code=404, detail='No cover yet')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'max-age=86400'})


@app.get('/api/radio/art', dependencies=[Depends(require_session)])
async def api_radio_art(station: str):
    return _station_art(station)


@app.get('/api/tv/radio/art', dependencies=[Depends(require_tv)])
async def api_tv_radio_art(station: str):
    return _station_art(station)


@if_errors
def _save_radio_session():
    session = RADIO['session']
    if not session:
        RADIO_SESSION_PATH.unlink(missing_ok=True)
        return
    keep = {k: session.get(k) for k in ('id', 'station', 'name', 'played', 'artist_mult', 'started')}
    tmp = RADIO_SESSION_PATH.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(keep, f)
    os.replace(tmp, RADIO_SESSION_PATH)


@if_errors
async def _resume_radio(saved):
    """The TV is still playing a station this service started before it restarted."""
    try:
        engine = await radio_engine()
        session = engine.session(saved['station'], recent=_recent_ids())
    except (KeyError, LookupError):
        return
    session.update(id=saved['id'], played=list(saved.get('played') or []),
                   artist_mult=dict(saved.get('artist_mult') or {}), lead=None,
                   started=saved.get('started') or time.time(), seen=time.time())
    RADIO['session'] = session
    log.info('[RADIO] picked %s back up after a restart (%d played)', session['name'], len(session['played']))


async def _radio_top_up(session):
    try:
        engine = await radio_engine()
        raws = engine.next_batch(session, RADIO_TOP_UP)
        queue = [{**JF._track(r), 'url': JF.audio_url(r['Id']), 'radio': session['id'], 'station': session['name']}
                 for r in raws]
        if queue and RADIO['session'] is session:
            await receiver_json('POST', '/music/append', {'queue': queue, 'radio': session['id']}, timeout=20)
            _save_radio_session()
    finally:
        RADIO['topping_up'] = False


@app.get('/api/radio', dependencies=[Depends(require_session)])
async def api_radio():
    cards = await _jf_call(_radio_cards())
    cards['mine'] = await _my_playlists()
    return cards


class RadioBody(BaseModel):
    station: str
    shuffle: bool = False     # a playlist: shuffled instead of in its own order


@app.post('/api/radio/play', dependencies=[Depends(require_session)])
async def api_radio_play(body: RadioBody):
    if not STATION_RE.match(body.station):
        raise HTTPException(status_code=400, detail='Unknown station')
    engine = await _jf_call(radio_engine())
    if body.station.startswith('playlist:'):
        try:
            playlist, raws = engine.playlist(body.station.split(':', 1)[1])
        except KeyError:
            raise HTTPException(status_code=404, detail='No such playlist')
        if body.shuffle:
            random.shuffle(raws)
        RADIO['session'] = None
        return await _play_queue([JF._track(r) for r in raws], 0, station=playlist['name'])
    try:
        session = engine.session(body.station, recent=_recent_ids())
    except KeyError:
        raise HTTPException(status_code=404, detail='No such station')
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    raws = engine.next_batch(session, RADIO_BATCH)
    session['seen'] = time.time()
    RADIO['session'] = session
    if body.station.startswith('song:'):
        continued = await _radio_after_song(body.station.split(':', 1)[1], raws, session)
        if continued:
            _save_radio_session()
            return continued
    result = await _play_queue([JF._track(r) for r in raws], 0, radio=session['id'], station=session['name'])
    _save_radio_session()
    return {**result, 'station': session['name']}


async def _radio_after_song(song_id, raws, session):
    """Radio from the song playing on the TV right now: it plays on where it
    is, and the station's songs come after it (receiver /music/continue). None when that
    song isn't the one playing, or the receiver can't (then the station starts over)."""
    if time.time() - STATUS['at'] > 1.5:
        await _poll_once()
    playing = ((STATUS['data'] or {}).get('music') or {}).get('track') or {}
    if playing.get('id') != song_id or playing.get('kind') == 'podcast':
        return None
    queue = [{**JF._track(r), 'url': JF.audio_url(r['Id']), 'radio': session['id'], 'station': session['name']}
             for r in raws if r['Id'] != song_id]
    try:
        await receiver_json('POST', '/music/continue',
                            {'queue': queue, 'radio': session['id'], 'station': session['name']}, timeout=30)
    except HTTPException as e:
        print('[RADIO] continue after the song failed (%s); starting the station over' % e.detail, flush=True)
        return None
    MUSIC_PLAY.update(radio=session['id'], station=session['name'])
    return {'ok': True, 'count': len(queue) + 1, 'title': playing.get('title'), 'station': session['name'],
            'continued': True}


class FeedbackBody(BaseModel):
    kind: str     # love | unlove | ban


@app.post('/api/radio/feedback', dependencies=[Depends(require_session)])
async def api_radio_feedback(body: FeedbackBody):
    """♥ the song playing (kept for Old favorites, and a heart in Jellyfin), or
    "Not for this station" (skips it, and the station never plays it again)."""
    if body.kind not in ('love', 'unlove', 'ban'):
        raise HTTPException(status_code=400, detail='Unknown feedback')
    if time.time() - STATUS['at'] > 1.5:
        await _poll_once()
    track = ((STATUS['data'] or {}).get('music') or {}).get('track') or {}
    if not track.get('id') or track.get('kind') == 'podcast':
        raise HTTPException(status_code=409, detail='No song is playing')
    engine = await _jf_call(radio_engine())
    session = RADIO['session'] if RADIO['session'] and track.get('radio') == RADIO['session']['id'] else None
    if body.kind == 'ban':
        if not session:
            raise HTTPException(status_code=409, detail='That only works on a radio station')
        engine.feedback(session, track['id'], 'ban')
        _save_radio_store()
        await receiver_json('POST', '/control', {'action': 'next'})
        return {'ok': True, 'title': track.get('title')}
    if body.kind == 'love':
        engine.feedback(session, track['id'], 'love')
    else:
        loves = _radio_store().setdefault('loves', [])
        if track['id'] in loves:
            loves.remove(track['id'])
        engine.loved.discard(track['id'])
    _save_radio_store()
    try:
        await JF.favorite(track['id'], body.kind == 'love')
    except httpx.HTTPError as e:
        print(f'[RADIO] Jellyfin favorite failed: {e}', flush=True)
    return {'ok': True, 'title': track.get('title'), 'loved': body.kind == 'love'}


class PlaylistNameBody(BaseModel):
    playlist: str     # playlist id (12 hex)
    name: str         # empty: back to its iTunes name


@app.post('/api/radio/playlist_name', dependencies=[Depends(require_session)])
async def api_radio_playlist_name(body: PlaylistNameBody):
    """A new name for one of the imported iTunes playlists (an old mix's name may
    not be one anybody wants on the TV any more). Kept in radio.json by playlist
    id; the songs don't change."""
    if not re.fullmatch(r'[0-9a-f]{12}', body.playlist):
        raise HTTPException(status_code=400, detail='Bad playlist')
    engine = await _jf_call(radio_engine())
    try:
        engine.playlist(body.playlist)
    except KeyError:
        raise HTTPException(status_code=404, detail='No such playlist')
    name = re.sub(r'\s+', ' ', body.name).strip()[:80]
    names = _radio_store().setdefault('names', {})
    if name:
        names[body.playlist] = name
    else:
        names.pop(body.playlist, None)
    _save_radio_store()
    return {'ok': True, 'name': engine.playlist(body.playlist)[0]['name']}


@app.get('/api/tv/radio', dependencies=[Depends(require_tv)])
async def api_tv_radio():
    return await api_radio()


@app.post('/api/tv/radio/play', dependencies=[Depends(require_tv)])
async def api_tv_radio_play(body: RadioBody):
    return await api_radio_play(body)


# --------------------------------------------------------------------------- #
# Lists, the queue, the household's own playlists: an album, playlist or
# station opens as a page (Play all, Shuffle, or start from a song); the
# stations hold still for a day; every song has a menu: Play next, Add to
# queue, Love, Not for me, Add to playlist. Doc: docs/server.md § Music.
# --------------------------------------------------------------------------- #
DAILY_PATH = DATA_DIR / 'daily.json'          # each station's list for today
LIST_KINDS = ('album', 'playlist', 'mine', 'station')
HEX32 = re.compile(r'[0-9a-f]{32}')
_daily = {'data': None}


def _daily_store():
    if _daily['data'] is None:
        try:
            with open(DAILY_PATH) as f:
                _daily['data'] = json.load(f)
        except (OSError, ValueError):
            _daily['data'] = {}
    return _daily['data']


@if_errors
def _save_daily():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_PATH.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(_daily['data'], f)
    os.replace(tmp, DAILY_PATH)


def _daily_list(engine, station):
    """Today's list for a station: made once a day, then the same until midnight
    (leaving a station and coming back shouldn't find it all reshuffled)."""
    store, today = _daily_store(), time.strftime('%Y-%m-%d')
    if (store.get(station) or {}).get('date') != today:
        store[station] = {'date': today, 'ids': engine.daily(station, today, recent=_recent_ids())}
        for old in [k for k, v in store.items() if v.get('date') != today]:
            del store[old]
        _save_daily()
    radio = _radio_store()
    gone = set(radio.get('dislikes') or []) | set((radio.get('bans') or {}).get(station) or [])
    return [i for i in store[station]['ids'] if i in engine.songs and i not in gone]


def _ids_param(text, limit=200):
    ids = [i for i in (text or '').split(',') if i]
    if len(ids) > limit or not all(HEX32.fullmatch(i) for i in ids):
        raise HTTPException(status_code=400, detail='Bad ids')
    return ids


def _check_list(kind, ident):
    ok = ((kind in ('album', 'mine') and HEX32.fullmatch(ident or ''))
          or (kind == 'playlist' and re.fullmatch(r'[0-9a-f]{12}', ident or ''))
          or (kind == 'station' and STATION_RE.match(ident or '') and not ident.startswith('playlist:')))
    if not ok:
        raise HTTPException(status_code=400, detail='Unknown list')


async def _build_list(kind, ident, ids=(), songs=()):
    """{kind, id, title, sub, image, own, items[...]} for an album, an imported
    iTunes playlist, one of the household's own playlists, or a station's list
    for today."""
    _check_list(kind, ident)
    engine = await _jf_call(radio_engine())
    index = RADIO['index']
    entries = {}
    if kind == 'album':
        group = next((g for g in index.groups if g['id'] == ident), None)
        if not group and not (ids or songs):
            raise HTTPException(status_code=404, detail='No such album')
        raws = index.songs(ids, songs) if (ids or songs) else index.songs(group['ids'], group['songs'])
        title = group['name'] if group else (raws[0].get('Album') if raws else '')
        sub = ' · '.join(str(x) for x in ((group or {}).get('artist'), (group or {}).get('year')) if x)
    elif kind == 'playlist':
        try:
            playlist, raws = engine.playlist(ident)
        except KeyError:
            raise HTTPException(status_code=404, detail='No such playlist')
        title, sub = playlist['name'], 'your old iTunes playlist'
    elif kind == 'mine':
        if ident not in _radio_store().get('mine', []):
            raise HTTPException(status_code=404, detail='No such playlist')
        pairs = await _jf_call(JF.playlist_entries(ident))
        raws = [index.by_track[i] for i, _ in pairs if i in index.by_track]
        entries = {i: e for i, e in pairs}
        info = await _jf_call(JF.playlist_info([ident]))
        title, sub = (info.get(ident) or {}).get('name', 'My playlist'), 'my playlist'
    else:
        raws = [engine.songs[i].raw for i in _daily_list(engine, ident)]
        title, sub = engine.name_of(ident), "today's list · a new one tomorrow"
    if kind in ('album', 'playlist'):     # the household's own playlists keep exactly what was put in them
        raws = [r for r in raws if not getattr(engine.songs.get(r['Id']), 'dupe', False)]
    store = _radio_store()
    loves, dislikes = set(store.get('loves') or []), set(store.get('dislikes') or [])
    items = []
    for raw in raws:
        t = JF._track(raw)
        t.update(loved=t['id'] in loves, disliked=t['id'] in dislikes)
        if kind == 'mine':
            t['entry'] = entries.get(t['id'])
        items.append(t)
    image = (ident if kind == 'album' and any(g['id'] == ident and g['has_image'] for g in index.groups)
             else next((t['image'] for t in items if t.get('image')), None))
    return {'kind': kind, 'id': ident, 'title': title, 'sub': sub, 'image': image,
            'own': kind == 'mine', 'items': items}


@app.get('/api/list', dependencies=[Depends(require_session)])
async def api_list(kind: str, id: str, ids: str = '', songs: str = ''):
    return await _build_list(kind, id, _ids_param(ids), _ids_param(songs))


class ListPlayBody(BaseModel):
    kind: str
    id: str
    ids: list[str] = []       # an album card's folders (music_index.py) ...
    songs: list[str] = []     # ... and its loose songs
    start: int = 0            # play from this song
    shuffle: bool = False


@app.post('/api/list/play', dependencies=[Depends(require_session)])
async def api_list_play(body: ListPlayBody):
    lst = await _build_list(body.kind, body.id, _ids_param(','.join(body.ids)), _ids_param(','.join(body.songs)))
    tracks = [{k: v for k, v in t.items() if k not in ('loved', 'disliked', 'entry')} for t in lst['items']]
    if not tracks:
        raise HTTPException(status_code=404, detail='Nothing to play there')
    start = max(0, min(body.start, len(tracks) - 1))
    if body.shuffle:
        random.shuffle(tracks)
        start = 0
    extra = {}
    if body.kind == 'station':      # today's list, then the station plays on from there
        engine = RADIO['engine']
        session = engine.session(body.id, recent=_recent_ids())
        session['played'] = [t['id'] for t in tracks]
        session['seen'] = time.time()
        RADIO['session'] = session
        extra = {'radio': session['id'], 'station': session['name']}
    else:
        RADIO['session'] = None
        if body.kind in ('playlist', 'mine'):
            extra = {'station': lst['title']}
    return {**await _play_queue(tracks, start, **extra), 'list': lst['title']}


class QueueBody(BaseModel):
    song: str
    mode: str = 'queue'       # queue: after what's already queued · next: right after this song


@app.post('/api/queue', dependencies=[Depends(require_session)])
async def api_queue(body: QueueBody):
    """Add to queue / Play next. Nothing playing: it just plays."""
    _check_jf_id(body.song)
    index = await _jf_call(music_index())
    raw = index.by_track.get(body.song)
    if not raw:
        raise HTTPException(status_code=404, detail='No such song')
    track = JF._track(raw)
    if time.time() - STATUS['at'] > 1.5:
        await _poll_once()
    data = STATUS['data'] or {}
    now = ((data.get('music') or {}).get('track')) if (data.get('player') or {}).get('active') else None
    if not now:
        RADIO['session'] = None
        return {**await _play_queue([track]), 'started': True}
    item = {**track, 'url': JF.audio_url(track['id']), 'queued': True}
    if now.get('radio'):             # a station keeps playing on around it
        item.update(radio=now['radio'], station=now.get('station'))
    reply = await receiver_json('POST', '/music/insert', {'item': item, 'mode': 'next' if body.mode == 'next' else 'queue'})
    return {'ok': bool(reply.get('ok')), 'title': track['title'], 'position': reply.get('at')}


class SongFeedbackBody(BaseModel):
    song: str
    kind: str     # love | unlove | dislike | undislike


@app.post('/api/song/feedback', dependencies=[Depends(require_session)])
async def api_song_feedback(body: SongFeedbackBody):
    """♥ any song (Old favorites plays it more; a heart in Jellyfin too), or "Not
    for me" (never on a station or in Shuffle all again; skipped if it's playing)."""
    if body.kind not in ('love', 'unlove', 'dislike', 'undislike'):
        raise HTTPException(status_code=400, detail='Unknown feedback')
    _check_jf_id(body.song)
    engine = await _jf_call(radio_engine())
    if body.song not in engine.songs:
        raise HTTPException(status_code=404, detail='No such song')
    engine.feedback(RADIO['session'], body.song, body.kind)
    _save_radio_store()
    if body.kind in ('love', 'unlove'):
        try:
            await JF.favorite(body.song, body.kind == 'love')
        except httpx.HTTPError as e:
            print(f'[RADIO] Jellyfin favorite failed: {e}', flush=True)
    playing = (((STATUS['data'] or {}).get('music') or {}).get('track') or {}).get('id')
    if body.kind == 'dislike' and playing == body.song:
        await receiver_json('POST', '/control', {'action': 'next'})
    store = _radio_store()
    return {'ok': True, 'loved': body.song in (store.get('loves') or []),
            'disliked': body.song in (store.get('dislikes') or [])}


async def _my_playlists():
    """The household's own playlists (Jellyfin's, made here), newest first."""
    mine = _radio_store().setdefault('mine', [])
    try:
        info = await JF.playlist_info(mine)
    except httpx.HTTPError:
        return []
    if any(i not in info for i in mine):      # deleted elsewhere (Finamp, Jellyfin)
        mine[:] = [i for i in mine if i in info]
        _save_radio_store()
    return [{'id': i, 'type': 'MyPlaylist', 'name': info[i]['name'], 'count': info[i]['count']}
            for i in reversed(mine)]


def _playlist_name(text):
    name = re.sub(r'\s+', ' ', text or '').strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail='A playlist needs a name')
    return name


def _my_playlist(pid):
    if not HEX32.fullmatch(pid) or pid not in _radio_store().get('mine', []):
        raise HTTPException(status_code=404, detail='No such playlist')


class NewPlaylistBody(BaseModel):
    name: str
    songs: list[str] = []


class PlaylistSongsBody(BaseModel):
    songs: list[str] = []      # song ids to add
    entries: list[str] = []    # playlist entry ids to remove


@app.get('/api/playlists', dependencies=[Depends(require_session)])
async def api_playlists():
    return {'playlists': await _my_playlists()}


@app.post('/api/playlists', dependencies=[Depends(require_session)])
async def api_playlists_new(body: NewPlaylistBody):
    name = _playlist_name(body.name)
    songs = _ids_param(','.join(body.songs))
    pid = await _jf_call(JF.create_playlist(name, songs))
    _radio_store().setdefault('mine', []).append(pid)
    _save_radio_store()
    return {'id': pid, 'name': name}


@app.post('/api/playlists/{pid}/add', dependencies=[Depends(require_session)])
async def api_playlists_add(pid: str, body: PlaylistSongsBody):
    _my_playlist(pid)
    await _jf_call(JF.add_to_playlist(pid, _ids_param(','.join(body.songs))))
    return {'ok': True}


@app.post('/api/playlists/{pid}/remove', dependencies=[Depends(require_session)])
async def api_playlists_remove(pid: str, body: PlaylistSongsBody):
    _my_playlist(pid)
    entries = [e for e in body.entries if re.fullmatch(r'[0-9a-f-]{8,40}', e)]
    if entries:
        await _jf_call(JF.remove_from_playlist(pid, entries))
    return {'ok': True}


class RenameBody(BaseModel):
    name: str


@app.post('/api/playlists/{pid}/rename', dependencies=[Depends(require_session)])
async def api_playlists_rename(pid: str, body: RenameBody):
    _my_playlist(pid)
    name = _playlist_name(body.name)
    await _jf_call(JF.rename_playlist(pid, name))
    return {'ok': True, 'name': name}


@app.delete('/api/playlists/{pid}', dependencies=[Depends(require_session)])
async def api_playlists_delete(pid: str):
    _my_playlist(pid)
    await _jf_call(JF.delete_playlist(pid))
    _radio_store()['mine'].remove(pid)
    _save_radio_store()
    return {'ok': True}


# The TV's own screens reach the same handlers (receiver /local/tvapi/<path>).
@app.get('/api/tv/list', dependencies=[Depends(require_tv)])
async def api_tv_list(kind: str, id: str, ids: str = '', songs: str = ''):
    return await api_list(kind, id, ids, songs)


@app.post('/api/tv/list/play', dependencies=[Depends(require_tv)])
async def api_tv_list_play(body: ListPlayBody):
    return await api_list_play(body)


@app.post('/api/tv/queue', dependencies=[Depends(require_tv)])
async def api_tv_queue(body: QueueBody):
    return await api_queue(body)


@app.post('/api/tv/song/feedback', dependencies=[Depends(require_tv)])
async def api_tv_song_feedback(body: SongFeedbackBody):
    return await api_song_feedback(body)


@app.get('/api/tv/playlists', dependencies=[Depends(require_tv)])
async def api_tv_playlists():
    return await api_playlists()


@app.post('/api/tv/playlists', dependencies=[Depends(require_tv)])
async def api_tv_playlists_new(body: NewPlaylistBody):
    return await api_playlists_new(body)


@app.post('/api/tv/playlists/{pid}/add', dependencies=[Depends(require_tv)])
async def api_tv_playlists_add(pid: str, body: PlaylistSongsBody):
    return await api_playlists_add(pid, body)


# --------------------------------------------------------------------------- #
# Podcasts: Apple's free directory to find them, the shows' own RSS feeds for
# episodes, played by the Pi's mpv straight from the publisher with resume
# points. podcasts.py is the engine. Doc: docs/server.md § Podcasts.
# --------------------------------------------------------------------------- #
PODCASTS = Podcasts(str(DATA_DIR))
PODCAST_REFRESH_SECONDS = 20 * 60


async def _podcast_refresher():
    while True:
        try:
            await PODCASTS.refresh()
        except Exception as e:           # one bad round must not end the loop
            print(f'[PODCASTS] refresh failed: {e}', flush=True)
        await asyncio.sleep(PODCAST_REFRESH_SECONDS)


async def _pod(call):
    """Run an engine call; its errors become answers the phone can show."""
    try:
        return await call if asyncio.iscoroutine(call) else call
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e) or 'Bad request')
    except NotFound as e:
        raise HTTPException(status_code=404, detail=str(e) or 'Not found')
    except FeedError as e:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail=str(e) or "That podcast isn't answering")


@app.get('/api/podcasts', dependencies=[Depends(require_session)])
async def api_podcasts():
    return {'shows': PODCASTS.shows()}


@app.get('/api/podcasts/search', dependencies=[Depends(require_session)])
async def api_podcasts_search(q: str = ''):
    q = q.strip()[:100]
    return {'results': await _pod(PODCASTS.search(q)) if q else []}


class SubscribeBody(BaseModel):
    feed_url: str


@app.post('/api/podcasts/subscribe', dependencies=[Depends(require_session)])
async def api_podcasts_subscribe(body: SubscribeBody):
    return {'show': await _pod(PODCASTS.subscribe(body.feed_url.strip()))}


@app.delete('/api/podcasts/{show_id}', dependencies=[Depends(require_session)])
async def api_podcasts_unsubscribe(show_id: str):
    if not await _pod(asyncio.to_thread(PODCASTS.unsubscribe, show_id)):
        raise HTTPException(status_code=404, detail='Not subscribed')
    return {'ok': True}


@app.get('/api/podcasts/show/{show_id}', dependencies=[Depends(require_session)])
async def api_podcasts_show(show_id: str):
    try:
        await _pod(PODCASTS.refresh(show_id))     # only fetches when the feed is due
    except HTTPException as e:
        if e.status_code != UPSTREAM_ERROR:       # a feed that's down still shows what we have
            raise
    show = next((s for s in PODCASTS.shows() if s['id'] == show_id), None)
    return {'show': show, 'episodes': await _pod(asyncio.to_thread(PODCASTS.episodes, show_id))}


class EpisodeBody(BaseModel):
    episode: str
    from_start: bool = False
    played: bool = True


def _episode_date(iso):
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).strftime('%b %-d, %Y')
    except (AttributeError, ValueError):
        return ''


@app.post('/api/podcasts/play', dependencies=[Depends(require_session)])
async def api_podcasts_play(body: EpisodeBody):
    ep = await _pod(asyncio.to_thread(PODCASTS.episode, body.episode))
    if not ep:
        raise HTTPException(status_code=404, detail='No such episode')
    start_at = 0 if body.from_start else (ep.get('resume') or 0)
    item = {'id': ep['id'], 'title': ep['title'], 'artist': ep.get('show') or '', 'album': _episode_date(ep.get('date')),
            'duration': ep.get('duration'), 'image': None, 'art': ep.get('art'), 'kind': 'podcast',
            'url': ep['url']}
    RADIO['session'] = None
    result = await _play_queue([item], 0, start_at=start_at)
    return {**result, 'resumed': start_at}


@app.post('/api/podcasts/played', dependencies=[Depends(require_session)])
async def api_podcasts_played(body: EpisodeBody):
    await _pod(asyncio.to_thread(PODCASTS.mark_played, body.episode, body.played))
    return {'ok': True}


@app.get('/api/podcasts/art/{show_id}', dependencies=[Depends(require_session)])
async def api_podcasts_art(show_id: str):
    path = await _pod(asyncio.to_thread(PODCASTS.art_path, show_id))
    if not path:
        raise HTTPException(status_code=404, detail='No artwork')
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'max-age=86400'})


@app.get('/api/tv/podcasts', dependencies=[Depends(require_tv)])
async def api_tv_podcasts():
    return await api_podcasts()


@app.get('/api/tv/podcasts/show/{show_id}', dependencies=[Depends(require_tv)])
async def api_tv_podcasts_show(show_id: str):
    return await api_podcasts_show(show_id)


@app.post('/api/tv/podcasts/play', dependencies=[Depends(require_tv)])
async def api_tv_podcasts_play(body: EpisodeBody):
    return await api_podcasts_play(body)


@app.get('/api/tv/podcasts/art/{show_id}', dependencies=[Depends(require_tv)])
async def api_tv_podcasts_art(show_id: str):
    return await api_podcasts_art(show_id)


# --------------------------------------------------------------------------- #
# Casting links + YouTube search
# --------------------------------------------------------------------------- #
class CastBody(BaseModel):
    url: str
    mode: str = 'auto'   # auto | player | browser
    title: str | None = None


def _wants_browser(url):
    host = (urlparse(url).hostname or '').lower()
    return any(d in host for d in BROWSER_DOMAINS)


@app.post('/api/cast', dependencies=[Depends(require_session)])
async def api_cast(body: CastBody):
    url = body.url.strip()
    if not re.match(r'^https?://', url):
        url = 'https://' + url
    if not urlparse(url).hostname:
        raise HTTPException(status_code=400, detail="That doesn't look like a link")
    mode = body.mode
    if mode == 'auto':
        mode = 'browser' if _wants_browser(url) else 'player'
    if mode == 'browser':
        await receiver_json('POST', '/app', {'url': url})
        NOW.update(kind='app', id=None, title=None, url=url, source='link', started=time.time())
    else:
        await receiver_json('POST', '/play', {'url': url, 'title': body.title or ''}, timeout=25)
        NOW.update(kind='link', id=None, title=body.title, url=url, source='link',
                   started=time.time())
    return {'ok': True, 'mode': mode}


@if_errors
async def youtube_search(query):
    env = dict(os.environ, PATH=_TOOL_PATH)
    proc = await asyncio.create_subprocess_exec(
        YTDLP, '--flat-playlist', '-J', '--no-warnings', 'ytsearch15:' + query,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=40)
    data = json.loads(out or b'{}')
    results = []
    for e in data.get('entries') or []:
        vid = e.get('id')
        if not vid:
            continue
        results.append({
            'id': vid,
            'title': e.get('title') or '',
            'channel': e.get('channel') or e.get('uploader') or '',
            'duration': e.get('duration'),
            'url': 'https://www.youtube.com/watch?v=' + vid,
            'thumb': 'https://i.ytimg.com/vi/%s/mqdefault.jpg' % vid,
        })
    return results


# --------------------------------------------------------------------------- #
# Discs (optional, TV_DISCS=1): a USB CD/DVD drive on this server. CDs are
# ripped into the Music library, DVDs into Movies (hearth/discs.py).
# Doc: docs/server.md § Discs.
# --------------------------------------------------------------------------- #
async def _disc_added(kind):
    await _jf_refresh()
    if kind == 'music':
        MUSIC_INDEX['at'] = 0.0             # the next music browse reads the new album


async def _disc_notify(title, body):
    await PUSH.send(title, body, url='/#discs', tag='discs')


PUSH = Push(DATA_DIR)
DISCS = Discs(DATA_DIR, on_added=_disc_added, notify=_disc_notify)
MBID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


class DiscRipBody(BaseModel):
    release: str | None = None    # a MusicBrainz release id, when the CD matched several
    name: str | None = None       # a DVD's title, as corrected on the remote
    year: int | None = None
    unknown: bool = False         # rip a CD MusicBrainz doesn't know anyway
    names: dict | None = None     # and the names typed for it (artist, album, year, tracks)


class DiscNamesBody(BaseModel):
    artist: str = ''
    album: str = ''
    year: str | int | None = None
    tracks: list[str] = []
    apply: bool = False           # an album already in Music: rename it there now


@app.get('/api/disc', dependencies=[Depends(require_session)])
async def api_disc():
    return {**DISCS.status(), 'enabled': DISCS_ON}


def _need_discs():
    if not DISCS_ON:
        raise HTTPException(status_code=409, detail='The disc drive is off (TV_DISCS=1 in .env turns it on)')


@app.post('/api/disc/rip', dependencies=[Depends(require_session)])
async def api_disc_rip(body: DiscRipBody):
    _need_discs()
    if body.release and not MBID_RE.match(body.release):
        raise HTTPException(status_code=400, detail='Bad release id')
    if body.name is not None and not 0 < len(body.name.strip()) <= 120:
        raise HTTPException(status_code=400, detail='The name should be 1 to 120 characters')
    if body.year is not None and not 1890 <= body.year <= 2100:
        raise HTTPException(status_code=400, detail='That year looks wrong')
    try:
        job = await DISCS.rip(release=body.release, name=(body.name or '').strip() or None, year=body.year,
                              unknown=body.unknown, names=body.names)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {'ok': True, 'job': job}


@app.post('/api/disc/names', dependencies=[Depends(require_session)])
async def api_disc_names(body: DiscNamesBody):
    """Names for a CD MusicBrainz doesn't know (docs/server.md § Discs)."""
    _need_discs()
    if len(body.tracks) > 99:
        raise HTTPException(status_code=400, detail='A CD has at most 99 songs')
    names = await DISCS.set_names(body.model_dump(exclude={'apply'}), apply=body.apply)
    return {'ok': True, 'names': names}


@app.post('/api/disc/stop', dependencies=[Depends(require_session)])
async def api_disc_stop():
    _need_discs()
    return {'ok': await DISCS.stop()}


@app.post('/api/disc/reread', dependencies=[Depends(require_session)])
async def api_disc_reread():
    _need_discs()
    try:
        return {'ok': DISCS.reread()}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post('/api/disc/eject', dependencies=[Depends(require_session)])
async def api_disc_eject():
    _need_discs()
    try:
        return {'ok': await DISCS.eject()}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))




# Phone notifications (hearth/push.py): the installed remote subscribes from its
# Discs tab; static/sw.js shows them. Needs HTTPS and pywebpush + py-vapid.
class PushSubBody(BaseModel):
    subscription: dict
    device: str = ''


class PushEndpointBody(BaseModel):
    endpoint: str


@app.get('/api/push/key', dependencies=[Depends(require_session)])
async def api_push_key():
    try:
        return {'key': await asyncio.to_thread(PUSH.public_key)}
    except RuntimeError as e:         # pywebpush / py-vapid not installed
        raise HTTPException(status_code=UPSTREAM_ERROR, detail=str(e))


@app.post('/api/push/subscribe', dependencies=[Depends(require_session)])
async def api_push_subscribe(body: PushSubBody):
    if not PUSH_AVAILABLE:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail='Push needs pywebpush and py-vapid on the server')
    try:
        n = await asyncio.to_thread(PUSH.subscribe, body.subscription, body.device)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await PUSH.send('🔔 Disc notifications are on', "You'll hear from the remote when a disc needs you, "
                    'when a rip is done, or if something goes wrong.', url='/#discs', tag='discs-test')
    return {'ok': True, 'phones': n}


@app.post('/api/push/unsubscribe', dependencies=[Depends(require_session)])
async def api_push_unsubscribe(body: PushEndpointBody):
    return {'ok': await asyncio.to_thread(PUSH.unsubscribe, body.endpoint)}


@app.post('/api/push/check', dependencies=[Depends(require_session)])
async def api_push_check(body: PushEndpointBody):
    return {'on': await asyncio.to_thread(PUSH.has, body.endpoint)}


@app.post('/api/push/test', dependencies=[Depends(require_session)])
async def api_push_test():
    return {'sent': await PUSH.send('🔔 A test from Hearth', 'This is how a disc will let you know.',
                                    url='/#discs', tag='discs-test')}


@app.get('/api/search', dependencies=[Depends(require_session)])
async def api_search(q: str):
    q = q.strip()
    if not q:
        return {'results': []}
    results = await youtube_search(q)
    if results is None:
        raise HTTPException(status_code=UPSTREAM_ERROR, detail="YouTube search didn't answer")
    return {'results': results}


# --------------------------------------------------------------------------- #
# Remote buttons — straight through to the Pi
# --------------------------------------------------------------------------- #
@app.post('/api/control', dependencies=[Depends(require_session)])
async def api_control(request: Request):
    body = await request.json()
    result = await receiver_json('POST', '/control', body)
    if body.get('action') == 'stop':
        NOW.update({k: None for k in NOW})
    return result


class MediaBody(BaseModel):
    action: str   # playpause | rewind | fastforward


@app.post('/api/media', dependencies=[Depends(require_session)])
async def api_media(body: MediaBody):
    """The remote's transport row: our player gets real seeks, everything else
    (Android apps, YouTube TV, browser sites) gets media keys."""
    if body.action not in ('playpause', 'rewind', 'fastforward'):
        raise HTTPException(status_code=400, detail='Unknown action')
    if time.time() - STATUS['at'] > 1.5:
        await _poll_once()
    if (STATUS['data'] or {}).get('mode') == 'player':
        control = {'playpause': {'action': 'toggle'},
                   'rewind': {'action': 'seek', 'seconds': -10},
                   'fastforward': {'action': 'seek', 'seconds': 10}}[body.action]
        return await receiver_json('POST', '/control', control)
    return await receiver_json('POST', '/input', {'type': 'key', 'key': body.action}, timeout=30)


@app.post('/api/input', dependencies=[Depends(require_session)])
async def api_input(request: Request):
    return await receiver_json('POST', '/input', await request.json(), timeout=30)


class AppBody(BaseModel):
    name: str


@app.post('/api/app', dependencies=[Depends(require_session)])
async def api_app(body: AppBody):
    if body.name not in APPS:
        raise HTTPException(status_code=400, detail='Unknown app')
    # Android may need to boot (~30-90 s) the first time it opens, and a
    # restart under an open can take ~70 s more.
    result = await receiver_json('POST', '/app', {'name': body.name}, timeout=240)
    if not result.get('cancelled'):     # Home was pressed while it opened
        NOW.update(kind='app', id=None, title=None, url=result.get('url'), source=body.name,
                   started=time.time())
    return result



@app.post('/api/android/prepare', dependencies=[Depends(require_session)])
async def api_android_prepare():
    """The phone remote just came up: Android gets ready in the background (woken,
    and restarted now if its display has crashed) before an app is picked
    (receiver Android.prepare)."""
    return await receiver_json('POST', '/android/prepare', timeout=10)


@app.post('/api/home', dependencies=[Depends(require_session)])
async def api_home():
    NOW.update({k: None for k in NOW})
    # Home waits behind an Android app that is still opening (~30 s on the
    # Pi); at 15 s the remote showed an error and the presses ran anyway.
    return await receiver_json('POST', '/home_screen', timeout=60)


@app.post('/api/browser/restart', dependencies=[Depends(require_session)])
async def api_browser_restart():
    return await receiver_json('POST', '/browser/restart', timeout=40)


@app.get('/api/snapshot', dependencies=[Depends(require_session)])
async def api_snapshot():
    r = await receiver('GET', '/snapshot', timeout=20)
    return Response(r.content, media_type=r.headers.get('content-type', 'image/jpeg'),
                    headers={'Cache-Control': 'no-store'})


@app.get('/api/audio', dependencies=[Depends(require_session)])
async def api_audio():
    return await receiver_json('GET', '/audio')


@app.post('/api/audio/default', dependencies=[Depends(require_session)])
async def api_audio_default(request: Request):
    return await receiver_json('POST', '/audio/default', await request.json())


# --------------------------------------------------------------------------- #
# Uploads — chunked, because Cloudflare refuses any request over 100 MB
# --------------------------------------------------------------------------- #
_uploads = {}
UPLOAD_CHUNK = 32 * 1024 * 1024
PROBE_BYTES = 64 * 1024      # the phone fingerprints the start of every chunk (sha256)
UPLOAD_KEEP = 3 * 86400      # unfinished uploads wait this long to be resumed
_upload_tasks = set()


class UploadStart(BaseModel):
    name: str
    size: int
    folder: str = ''
    probes: list[str] = []


def _safe_name(name):
    name = os.path.basename(name.replace('\\', '/')).strip()
    name = re.sub(r'[\x00-\x1f/]', '', name)
    if not name or name.startswith('.') or Path(name).suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(status_code=400, detail="That isn't a video file I can play")
    return name


def _safe_folder(folder):
    parts = [p for p in re.split(r'[\\/]+', folder or '') if p and p not in ('.', '..')]
    if any(p.startswith('.') for p in parts):
        raise HTTPException(status_code=400, detail='Bad folder name')
    return '/'.join(parts)


def _save_upload(upload_id):
    """Uploads outlive a reload of the phone page or a server restart: the
    pieces are on disk, and this sidecar says what they are."""
    up = _uploads[upload_id]
    meta = {k: v for k, v in up.items() if k != 'part'}
    tmp = UPLOAD_DIR / (upload_id + '.json.tmp')
    tmp.write_text(json.dumps(meta))
    tmp.replace(UPLOAD_DIR / (upload_id + '.json'))


def _drop_upload(upload_id):
    up = _uploads.pop(upload_id, None)
    (UPLOAD_DIR / (upload_id + '.json')).unlink(missing_ok=True)
    if up:
        up['part'].unlink(missing_ok=True)


def _load_uploads():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for meta_path in UPLOAD_DIR.glob('*.json'):
        upload_id = meta_path.stem
        part = UPLOAD_DIR / (upload_id + '.part')
        try:
            meta = json.loads(meta_path.read_text())
        except ValueError:
            meta = {}
        if not meta or not part.exists() or now - meta.get('updated', 0) > UPLOAD_KEEP:
            meta_path.unlink(missing_ok=True)
            part.unlink(missing_ok=True)
            continue
        if meta.get('state') != 'receiving':      # a restart mid-move: the whole file is here, move it again
            meta['state'] = 'receiving'
        meta['part'] = part
        _uploads[upload_id] = meta
    for part in UPLOAD_DIR.glob('*.part'):        # pieces nobody can claim any more
        if part.stem not in _uploads and now - part.stat().st_mtime > UPLOAD_KEEP:
            part.unlink(missing_ok=True)


def _probe_ok(part, probes, chunks):
    """The first PROBE_BYTES of each of the first `chunks` chunks match the phone's file."""
    import hashlib
    if len(probes) < chunks:
        return False
    with open(part, 'rb') as f:
        for i in range(chunks):
            f.seek(i * UPLOAD_CHUNK)
            if hashlib.sha256(f.read(PROBE_BYTES)).hexdigest() != probes[i]:
                return False
    return True


def _resume_match(name, folder, size, probes):
    """An unfinished upload of this same file: by its sidecar, or an orphaned
    .part with no sidecar (sent before uploads could resume) whose chunks
    match the phone's fingerprints."""
    for upload_id, up in _uploads.items():
        if (up['state'] == 'receiving' and up['name'] == name and up['size'] == size
                and up['folder'] == folder and _probe_ok(up['part'], probes, up['next'])):
            return upload_id
    best = None
    for part in UPLOAD_DIR.glob('*.part'):
        if part.stem in _uploads:
            continue
        whole = min(part.stat().st_size, size) // UPLOAD_CHUNK
        if whole and (best is None or whole > best[1]) and _probe_ok(part, probes, whole):
            best = (part, whole)
    if not best:
        return None
    part, whole = best
    with open(part, 'r+b') as f:                  # drop the half-written chunk at the end
        f.truncate(whole * UPLOAD_CHUNK)
    upload_id = part.stem
    _uploads[upload_id] = {'name': name, 'folder': folder, 'size': size, 'next': whole,
                           'received': whole * UPLOAD_CHUNK, 'part': part, 'state': 'receiving',
                           'updated': time.time(), 'adopted': True}
    _save_upload(upload_id)
    return upload_id


@app.post('/api/upload/start', dependencies=[Depends(require_session)])
async def api_upload_start(body: UploadStart):
    name = _safe_name(body.name)
    folder = _safe_folder(body.folder)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = await asyncio.to_thread(_resume_match, name, folder, body.size, body.probes)
    if upload_id:
        up = _uploads[upload_id]
        return {'id': upload_id, 'chunk_size': UPLOAD_CHUNK, 'next': up['next'],
                'received': up['received'], 'resumed': True}
    upload_id = uuid.uuid4().hex
    part = UPLOAD_DIR / (upload_id + '.part')
    part.touch()
    _uploads[upload_id] = {'name': name, 'folder': folder, 'size': body.size, 'next': 0, 'received': 0,
                           'part': part, 'state': 'receiving', 'updated': time.time()}
    _save_upload(upload_id)
    return {'id': upload_id, 'chunk_size': UPLOAD_CHUNK, 'next': 0, 'received': 0}


@app.post('/api/upload/chunk/{upload_id}/{index}', dependencies=[Depends(require_session)])
async def api_upload_chunk(upload_id: str, index: int, request: Request):
    up = _uploads.get(upload_id)
    if not up or up['state'] != 'receiving':
        raise HTTPException(status_code=404, detail='Upload not found — start again')
    if index != up['next']:
        return JSONResponse({'detail': 'Expected chunk %d' % up['next'], 'next': up['next']}, status_code=409)
    expected = min(UPLOAD_CHUNK, up['size'] - up['received'])
    written = 0
    with open(up['part'], 'r+b') as f:
        # A retry after a dropped connection must not stack on the half-chunk
        # the drop left behind (that used to corrupt the file silently).
        f.seek(up['received'])
        f.truncate()
        async for piece in request.stream():
            written += len(piece)
            if written > expected:
                raise HTTPException(status_code=413, detail='Chunk too large')
            f.write(piece)
    if written != expected:
        raise HTTPException(status_code=400, detail='That piece arrived short — sending it again')
    up['next'] += 1
    up['received'] += written
    up['updated'] = time.time()
    _save_upload(upload_id)
    return {'received': up['received'], 'next': up['next']}


@app.post('/api/upload/finish/{upload_id}', dependencies=[Depends(require_session)])
async def api_upload_finish(upload_id: str):
    up = _uploads.get(upload_id)
    if not up:
        raise HTTPException(status_code=404, detail='Upload not found')
    if up['state'] == 'moving':
        return {'ok': True, 'moving': True, 'id': upload_id}
    if up['received'] != up['size'] or up['part'].stat().st_size != up['size']:
        raise HTTPException(status_code=400, detail='Upload incomplete — try again')
    if not (LIBRARY / 'Movies').is_dir():
        raise HTTPException(status_code=UPLOAD_NAS_ERROR, detail="The media folder's Movies/ isn't there (is the share mounted?)")
    # The move into the share can outlast Cloudflare's 100 s request limit on a big
    # file, so it runs here and the phone polls /api/upload/status.
    up['state'] = 'moving'
    _save_upload(upload_id)
    task = asyncio.create_task(_move_upload(upload_id))
    _upload_tasks.add(task)
    task.add_done_callback(_upload_tasks.discard)
    return {'ok': True, 'moving': True, 'id': upload_id}


@if_errors
async def _move_upload(upload_id):
    import shutil
    up = _uploads[upload_id]
    try:
        folder = up['folder'] or 'Movies'
        if not (folder == 'Movies' or folder.startswith('Shows/')):
            folder = 'Movies'
        dest_dir = LIBRARY / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / up['name']
        n = 2
        while dest.exists():
            dest = dest_dir / ('%s (%d)%s' % (Path(up['name']).stem, n, Path(up['name']).suffix))
            n += 1
        await asyncio.to_thread(shutil.move, str(up['part']), str(dest))
        if dest.stat().st_size != up['size']:
            raise OSError('The copy in the media folder came out the wrong size')
    except Exception as exc:
        up.update(state='receiving', error=str(exc))   # all the pieces are still here: finish can retry
        _save_upload(upload_id)
        raise
    up.update(state='done', dest=dest.relative_to(LIBRARY).as_posix(), updated=time.time())
    (UPLOAD_DIR / (upload_id + '.json')).unlink(missing_ok=True)
    await _jf_refresh()


@app.get('/api/upload/status/{upload_id}', dependencies=[Depends(require_session)])
async def api_upload_status(upload_id: str):
    up = _uploads.get(upload_id)
    if not up:
        raise HTTPException(status_code=404, detail='Upload not found')
    if up['state'] == 'receiving' and up.get('error'):
        return {'state': 'error', 'error': up['error']}
    return {'state': up['state'], 'received': up['received'], 'size': up['size'], 'dest': up.get('dest')}


@app.get('/api/uploads', dependencies=[Depends(require_session)])
async def api_uploads():
    """Unfinished uploads, so a reloaded remote can offer to continue one."""
    return {'uploads': [{'id': k, 'name': u['name'], 'folder': u['folder'], 'size': u['size'],
                         'received': u['received'], 'state': u['state']}
                        for k, u in _uploads.items() if u['state'] in ('receiving', 'moving')]}


@app.delete('/api/upload/{upload_id}', dependencies=[Depends(require_session)])
async def api_upload_delete(upload_id: str):
    up = _uploads.get(upload_id)
    if up and up['state'] == 'moving':
        raise HTTPException(status_code=409, detail="It's already moving into the library")
    _drop_upload(upload_id)
    return {'ok': True}


@if_errors
async def _jf_refresh():
    await JF.refresh()


# Files dropped straight into the media share (a file manager, not the remote's
# upload) reach Jellyfin only at its 12-hour scan when the share is a network
# mount: its realtime monitor gets no change events over CIFS/SMB. Look every 2
# minutes; once the folders changed and then held still for a round (the copy
# finished), ask for a scan.
NAS_WATCH_SECONDS = 120


AUDIO_EXTS = {'.mp3', '.m4a', '.aac', '.flac', '.ogg', '.opus', '.wav', '.aif', '.aiff',
              '.wma', '.alac', '.ape', '.mka'}


@if_errors
def _library_snapshot():
    """Every video under the media folders → (size, mtime), and every folder that
    holds music → (its mtime, how many tracks): a big music collection is
    thousands of files, one network stat each over CIFS, and a song copies in
    seconds, so its folder settling is enough. None when the share isn't there
    or is empty (an unmounted mount point is an empty folder, and a scan of it
    would make Jellyfin drop everything)."""
    try:
        if not LIBRARY.is_dir() or not any(LIBRARY.iterdir()):
            return None
    except OSError:
        return None
    snap = {}
    for root, _dirs, files in os.walk(LIBRARY):
        tracks = 0
        for name in files:
            if name.startswith('.'):
                continue
            ext = Path(name).suffix.lower()
            if ext in AUDIO_EXTS:
                tracks += 1
            if ext not in VIDEO_EXTS:
                continue
            try:
                st = os.stat(os.path.join(root, name))
            except OSError:
                continue
            snap[os.path.join(root, name)] = (st.st_size, int(st.st_mtime))
        if tracks:
            try:
                snap[root + '/'] = (tracks, int(os.stat(root).st_mtime))
            except OSError:
                pass
    return snap


async def _nas_watch():
    seen = await asyncio.to_thread(_library_snapshot)
    changed = False
    while True:
        await asyncio.sleep(NAS_WATCH_SECONDS)
        now = await asyncio.to_thread(_library_snapshot)
        if now is None or (seen and not now):
            continue                       # share down, or it came back empty: leave Jellyfin alone
        if now != seen:
            seen, changed = now, True      # still copying, or just finished: one more round
            continue
        if changed:
            changed = False
            await _jf_refresh()
            MUSIC_INDEX['at'] = 0.0        # the next music browse reads the new library


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def _versioned_page(name):
    """Serve a page with ?v=<mtime> on every /static/ file it names — a CDN in
    front (Cloudflare's edge caches .css/.js/.png) would otherwise serve a stale
    remote.js that silently loses new buttons."""
    html = (STATIC_DIR / name).read_text()

    def version(m):
        asset = STATIC_DIR / m.group(1)
        if not asset.is_file():
            return m.group(0)
        return '/static/%s?v=%d"' % (m.group(1), int(asset.stat().st_mtime))
    html = re.sub(r'/static/([\w.-]+)"', version, html)
    return Response(html, media_type='text/html', headers={'Cache-Control': 'no-cache'})


@app.get('/')
async def index(request: Request):
    if not _signed_in(request):
        return RedirectResponse('/login', status_code=303)
    return _versioned_page('index.html')


@app.get('/login')
async def login_page():
    return _versioned_page('login.html')


@app.get('/sw.js')
async def service_worker():
    # at the root so its scope is the whole remote; no cookie (the browser fetches it on its own)
    return FileResponse(STATIC_DIR / 'sw.js', media_type='application/javascript',
                        headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})


@app.get('/manifest.webmanifest')
async def manifest():
    # Versioned icon URLs: a new icon is a new URL, which is what makes an
    # installed Android app pick it up (and Cloudflare can't serve the old one).
    data = json.loads((STATIC_DIR / 'manifest.webmanifest').read_text())
    for icon in data.get('icons', []):
        path = STATIC_DIR / icon['src'].rsplit('/', 1)[-1]
        if path.is_file():
            icon['src'] += '?v=%d' % int(path.stat().st_mtime)
    return Response(json.dumps(data), media_type='application/manifest+json',
                    headers={'Cache-Control': 'no-cache'})


@app.get('/health')
async def health():
    return {'status': 'ok', 'service': 'hearth', 'tv_online': STATUS['online']}


app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


if __name__ == '__main__':
    import uvicorn
    # timeout_graceful_shutdown: a long-lived request (a chunk upload mid-flight)
    # must not make a restart wait out systemd's 90 s stop timeout.
    uvicorn.run(app, host=os.getenv('TV_HOST', '0.0.0.0'), port=PORT,
                proxy_headers=True, forwarded_allow_ips='127.0.0.1',
                timeout_graceful_shutdown=5)