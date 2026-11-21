"""Podcasts for Hearth: shows found through Apple's free podcast directory (the
iTunes Search API, no account or key) and followed by their RSS feeds.
Episodes stream straight from the publisher to the Pi's mpv. This module is
only the engine: search, subscriptions, the episode lists and where each
episode was stopped. hearth/server.py serves it to the remote and the TV.
Doc: docs/server.md § Podcasts.

State lives in data_dir:
  podcasts.json          shows + their cached episodes (newest 300 each)
  podcast_progress.json  resume points and played marks by episode id, kept
                         apart because it's written every few seconds while an
                         episode plays, and so it survives a feed that drops
                         or reorders its items
  podcast_art/<id>.jpg   each show's artwork, ≤600 px
"""

import asyncio
import hashlib
import html
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as ET

from hearth.errors import if_errors

SEARCH_URL = 'https://itunes.apple.com/search'
USER_AGENT = 'Hearth/1.0 (home podcast player)'
TIMEOUT_S = 20
FEED_MAX_BYTES = 20 * 1024 * 1024
ART_MAX_BYTES = 10 * 1024 * 1024
ART_SIZE = 600
REFRESH_EVERY_S = 2 * 3600       # a feed is re-checked when it's this old
RETRY_AFTER_ERROR_S = 15 * 60    # ...or this long after a failed check
EPISODES_KEPT = 300
SUMMARY_CHARS = 400
RESUME_MIN_S = 30
NEW_DAYS = 30
FEEDS_AT_ONCE = 4

_ID = re.compile(r'^[0-9a-f]{16}$')
_MEDIA_EXT = re.compile(r'\.(mp3|m4a|m4b|aac|ogg|oga|opus|wav|flac|mp4|m4v|mov|webm)$', re.I)
# Podcasting 2.0 transcripts, best first (kept per episode; unused so far)
_TRANSCRIPT_PREF = ('text/vtt', 'application/x-subrip', 'application/srt', 'text/srt',
                    'application/json', 'text/plain', 'text/html')


class PodcastError(Exception):
    """Base for what the server should answer with a clear message."""


class NotFound(PodcastError):
    pass


class FeedError(PodcastError):
    """The feed couldn't be fetched or read (the server answers 424)."""


def show_id_for(feed_url):
    return hashlib.sha1(feed_url.encode('utf-8')).hexdigest()[:16]


def episode_id_for(show_id, key):
    return hashlib.sha1((show_id + key).encode('utf-8')).hexdigest()[:16]


def _check_id(value):
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError('Bad id')
    return value


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_duration(text):
    """itunes:duration: seconds, mm:ss or hh:mm:ss (fractions allowed)."""
    if not text:
        return None
    try:
        parts = [float(p) for p in str(text).strip().split(':')]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return int(round(secs)) or None


def parse_date(text):
    """RFC 822 (RSS) or ISO 8601 (Atom) → aware UTC datetime, or None."""
    if not text:
        return None
    text = str(text).strip()
    dt = None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        pass
    if dt is None:
        try:
            dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ') if dt else None


def plain_text(text, limit=SUMMARY_CHARS):
    """Show notes are HTML: keep the words."""
    if not text:
        return ''
    s = re.sub(r'(?is)<(script|style)\b.*?</\1\s*>', ' ', str(text))
    s = re.sub(r'(?i)<\s*/?\s*(br|p|div|li|ul|ol|h\d)\b[^>]*>', ' ', s)
    s = re.sub(r'<[^>]*>', '', s)
    s = html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip()
    if len(s) > limit:
        cut = s[:limit - 1].rsplit(' ', 1)[0] or s[:limit - 1]
        s = cut.rstrip(' ,;:.-') + '…'
    return s


def _local(tag):
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''


def _family(tag):
    """Which namespace an element is in, by family (feeds disagree on http/https
    and trailing slashes): '' for plain RSS."""
    if not isinstance(tag, str) or not tag.startswith('{'):
        return ''
    uri = tag[1:].split('}', 1)[0].lower()
    for name, marker in (('itunes', 'itunes.com/dtds'), ('podcast', 'podcastindex.org/namespace'),
                         ('content', 'purl.org/rss/1.0/modules/content'), ('media', 'search.yahoo.com/mrss'),
                         ('atom', 'www.w3.org/2005/atom')):
        if marker in uri:
            return name
    return uri


def _kids(el, name, family=''):
    return [c for c in el if _local(c.tag) == name and _family(c.tag) == family]


def _kid(el, name, family=''):
    found = _kids(el, name, family)
    return found[0] if found else None


def _text(el, name, family=''):
    c = _kid(el, name, family)
    return (c.text or '').strip() if c is not None and c.text else ''


def _is_media(url, mime):
    if not url or not re.match(r'^https?://', url, re.I):
        return False
    mime = (mime or '').lower().strip()
    if mime.startswith(('audio/', 'video/')):
        return True
    if mime and mime not in ('application/octet-stream', 'binary/octet-stream'):
        return False
    return bool(_MEDIA_EXT.search(urlparse(url).path))


def _transcript(item):
    best = None
    for t in _kids(item, 'transcript', 'podcast'):
        url, kind = (t.get('url') or '').strip(), (t.get('type') or '').strip().lower()
        if not re.match(r'^https?://', url, re.I):
            continue
        rank = _TRANSCRIPT_PREF.index(kind) if kind in _TRANSCRIPT_PREF else len(_TRANSCRIPT_PREF)
        if best is None or rank < best[0]:
            best = (rank, {'url': url, 'type': kind or None})
    return best[1] if best else None


def _episode(show_id, guid, title, date, duration, summary, url, mime, transcript):
    dt = parse_date(date)
    ep = {
        'id': episode_id_for(show_id, guid or url),
        'guid': guid or None,
        'title': plain_text(title, 300) or (dt.strftime('%B %-d, %Y') if dt else 'Untitled episode'),
        'date': iso(dt),
        'ts': dt.timestamp() if dt else 0.0,
        'duration': duration,
        'summary': plain_text(summary),
        'url': url,
        'mime': mime or None,
    }
    if transcript:
        ep['transcript'] = transcript
    return ep


def _parse_rss(root, show_id):
    ch = _kid(root, 'channel')
    if ch is None:
        raise FeedError('The feed has no channel')
    owner = _kid(ch, 'owner', 'itunes')
    itunes_image, rss_image = _kid(ch, 'image', 'itunes'), _kid(ch, 'image')
    show = {
        'title': plain_text(_text(ch, 'title'), 200),
        'author': plain_text(_text(ch, 'author', 'itunes') or (_text(owner, 'name', 'itunes') if owner is not None else '')
                             or _text(ch, 'managingEditor'), 200),
        'description': plain_text(_text(ch, 'summary', 'itunes') or _text(ch, 'description'), 1000),
        'image_url': ((itunes_image.get('href') or '').strip() if itunes_image is not None else '')
        or (_text(rss_image, 'url') if rss_image is not None else ''),
        'link': _text(ch, 'link'),
    }
    episodes = []
    for item in _kids(ch, 'item'):
        enc = _kid(item, 'enclosure')
        url, mime = '', ''
        if enc is not None:
            url, mime = (enc.get('url') or '').strip(), (enc.get('type') or '').strip()
        if not _is_media(url, mime):
            media = next((m for m in _kids(item, 'content', 'media')
                          if _is_media((m.get('url') or '').strip(), m.get('type'))), None)
            if media is None:
                continue
            url, mime = media.get('url').strip(), (media.get('type') or '').strip()
        summary = (_text(item, 'summary', 'itunes') or _text(item, 'description')
                   or _text(item, 'encoded', 'content') or _text(item, 'subtitle', 'itunes'))
        episodes.append(_episode(show_id, _text(item, 'guid'), _text(item, 'title'), _text(item, 'pubDate'),
                                 parse_duration(_text(item, 'duration', 'itunes')), summary, url, mime,
                                 _transcript(item)))
    return show, episodes


def _parse_atom(root, show_id):
    author = _kid(root, 'author', 'atom')
    show = {
        'title': plain_text(_text(root, 'title', 'atom'), 200),
        'author': plain_text(_text(author, 'name', 'atom') if author is not None else '', 200),
        'description': plain_text(_text(root, 'subtitle', 'atom'), 1000),
        'image_url': _text(root, 'logo', 'atom') or _text(root, 'icon', 'atom'),
        'link': next((l.get('href') for l in _kids(root, 'link', 'atom') if l.get('rel', 'alternate') == 'alternate'), ''),
    }
    episodes = []
    for entry in _kids(root, 'entry', 'atom'):
        enc = next((l for l in _kids(entry, 'link', 'atom') if l.get('rel') == 'enclosure'
                    and _is_media((l.get('href') or '').strip(), l.get('type'))), None)
        if enc is None:
            continue
        episodes.append(_episode(show_id, _text(entry, 'id', 'atom'), _text(entry, 'title', 'atom'),
                                 _text(entry, 'published', 'atom') or _text(entry, 'updated', 'atom'),
                                 parse_duration(_text(entry, 'duration', 'itunes')),
                                 _text(entry, 'summary', 'atom') or _text(entry, 'content', 'atom'),
                                 enc.get('href').strip(), (enc.get('type') or '').strip(), _transcript(entry)))
    return show, episodes


def parse_feed(data, feed_url):
    """A feed's bytes → (show fields, episodes newest first)."""
    data = (data or b'').lstrip(b'\xef\xbb\xbf \t\r\n')
    try:
        root = ET.fromstring(data)
    except Exception as e:      # ParseError and defusedxml's refusals alike
        raise FeedError("That doesn't read as a podcast feed (%s)" % type(e).__name__) from None
    show_id = show_id_for(feed_url)
    if _local(root.tag) == 'rss':
        show, episodes = _parse_rss(root, show_id)
    elif _local(root.tag) == 'feed':
        show, episodes = _parse_atom(root, show_id)
    else:
        raise FeedError("That doesn't read as a podcast feed")
    # the same episode twice in one feed (it happens) is one episode
    seen, unique = set(), []
    for ep in episodes:
        if ep['id'] not in seen:
            seen.add(ep['id'])
            unique.append(ep)
    unique.sort(key=lambda e: e['ts'], reverse=True)     # stable: undated keep feed order
    return show, unique


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def _write_json(path, obj):
    tmp = '%s.tmp' % path
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        # keep the damaged file for a look; start clean rather than refuse to run
        try:
            os.replace(path, '%s.bad-%d' % (path, int(time.time())))
        except OSError:
            pass
        return default


class Podcasts:
    def __init__(self, data_dir, client=None):
        self.dir = str(data_dir)
        self.art_dir = os.path.join(self.dir, 'podcast_art')
        os.makedirs(self.art_dir, exist_ok=True)
        self.path = os.path.join(self.dir, 'podcasts.json')
        self.progress_path = os.path.join(self.dir, 'podcast_progress.json')
        self.client = client or httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=True,
                                                  headers={'User-Agent': USER_AGENT})
        self._lock = threading.RLock()
        self.shows_data = _read_json(self.path, {}).get('shows', {})
        self.progress = _read_json(self.progress_path, {})
        self._index()

    # ------------------------------------------------------------ storage
    def _index(self):
        self._eps = {ep['id']: (sid, ep) for sid, s in self.shows_data.items() for ep in s.get('episodes', [])}

    def _save_shows(self):
        _write_json(self.path, {'shows': self.shows_data})

    def _save_progress(self):
        _write_json(self.progress_path, self.progress)

    # ------------------------------------------------------------ network
    async def _get(self, url, max_bytes, headers=None):
        """GET with a size cap. Returns the response (body read into .content
        as `body`) — a 304 comes back too, for the caller to see."""
        h = {'User-Agent': USER_AGENT, **(headers or {})}
        async with self.client.stream('GET', url, headers=h, follow_redirects=True,
                                      timeout=TIMEOUT_S) as r:
            if r.status_code == 304:
                return r, b''
            r.raise_for_status()
            if int(r.headers.get('content-length') or 0) > max_bytes:
                raise FeedError('That file is too big')
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise FeedError('That file is too big')
        return r, bytes(body)

    async def _fetch_feed(self, feed_url, etag=None, last_modified=None):
        headers = {}
        if etag:
            headers['If-None-Match'] = etag
        if last_modified:
            headers['If-Modified-Since'] = last_modified
        try:
            r, body = await self._get(feed_url, FEED_MAX_BYTES, headers)
        except httpx.HTTPStatusError as e:
            raise FeedError('The feed answered %d' % e.response.status_code) from None
        except httpx.HTTPError as e:
            raise FeedError("The feed couldn't be reached (%s)" % type(e).__name__) from None
        if r.status_code == 304:
            return None
        show, episodes = parse_feed(body, feed_url)
        show['etag'] = r.headers.get('etag')
        show['last_modified'] = r.headers.get('last-modified')
        return show, episodes

    async def _fetch_art(self, show_id, url):
        """The show's artwork as a ≤600 px JPEG. A missing or broken image only
        means a placeholder on the TV."""
        if not url or not re.match(r'^https?://', url, re.I):
            return False
        try:
            r, body = await self._get(url, ART_MAX_BYTES)
            if r.status_code == 304 or not body:
                return False
            jpeg = await asyncio.to_thread(_to_jpeg, body)
        except (httpx.HTTPError, FeedError, OSError, ValueError):
            return False
        path = os.path.join(self.art_dir, show_id + '.jpg')
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(jpeg)
        os.replace(tmp, path)
        return True

    # ------------------------------------------------------------ search
    async def search(self, term):
        term = (term or '').strip()[:100]
        if not term:
            return []
        try:
            r = await self.client.get(SEARCH_URL, params={'media': 'podcast', 'entity': 'podcast',
                                                          'term': term, 'limit': 25},
                                      headers={'User-Agent': USER_AGENT}, timeout=TIMEOUT_S)
            r.raise_for_status()
            data = json.loads(r.content or b'{}')   # Apple sends text/javascript
        except (httpx.HTTPError, ValueError):
            raise FeedError("Apple's podcast directory isn't answering") from None
        with self._lock:
            subscribed = set(self.shows_data)
        out = []
        for res in data.get('results') or []:
            feed = (res.get('feedUrl') or '').strip()
            if not re.match(r'^https?://', feed, re.I):
                continue
            out.append({
                'itunes_id': res.get('collectionId'),
                'title': res.get('collectionName') or res.get('trackName') or '',
                'author': res.get('artistName') or '',
                'feed_url': feed,
                'art_url': res.get('artworkUrl600') or res.get('artworkUrl100') or None,
                'genre': res.get('primaryGenreName') or None,
                'episodes': res.get('trackCount'),
                'subscribed': show_id_for(feed) in subscribed,
            })
        return out

    # ------------------------------------------------------------ subscriptions
    async def subscribe(self, feed_url):
        feed_url = (feed_url or '').strip()
        if not re.match(r'^https?://', feed_url, re.I) or not urlparse(feed_url).hostname:
            raise ValueError("That doesn't look like a feed link")
        sid = show_id_for(feed_url)
        with self._lock:
            if sid in self.shows_data:
                return self._show_card(sid)
        show, episodes = await self._fetch_feed(feed_url)
        now = time.time()
        with self._lock:
            if sid not in self.shows_data:          # a second tap while the first was fetching
                self.shows_data[sid] = {**show, 'feed_url': feed_url, 'added': now, 'fetched': now,
                                        'checked': now, 'error': None,
                                        'episodes': episodes[:EPISODES_KEPT]}
                self._index()
                self._save_shows()
        await self._fetch_art(sid, show.get('image_url'))
        with self._lock:
            return self._show_card(sid)

    def unsubscribe(self, show_id):
        _check_id(show_id)
        with self._lock:
            if self.shows_data.pop(show_id, None) is None:
                return False
            self.progress = {k: v for k, v in self.progress.items() if v.get('show_id') != show_id}
            self._index()
            self._save_shows()
            self._save_progress()
        try:
            os.remove(os.path.join(self.art_dir, show_id + '.jpg'))
        except FileNotFoundError:
            pass
        return True

    def art_path(self, show_id):
        _check_id(show_id)
        path = os.path.join(self.art_dir, show_id + '.jpg')
        return path if os.path.exists(path) else None

    # ------------------------------------------------------------ refreshing
    def _due(self, show, now):
        if show.get('error'):
            return now - (show.get('checked') or 0) >= RETRY_AFTER_ERROR_S
        return now - (show.get('checked') or show.get('fetched') or 0) >= REFRESH_EVERY_S

    async def refresh(self, show_id=None, force=False):
        """Re-check feeds (all, or one). Returns how many new episodes arrived.
        A feed that fails keeps its last episodes and its error is noted on the
        show; the others still refresh."""
        if show_id is not None:
            _check_id(show_id)
        now = time.time()
        with self._lock:
            if show_id is not None and show_id not in self.shows_data:
                raise NotFound('No such show')
            ids = [sid for sid, s in self.shows_data.items()
                   if (show_id is None or sid == show_id) and (force or self._due(s, now))]
        gate = asyncio.Semaphore(FEEDS_AT_ONCE)

        async def one(sid):
            async with gate:
                return await self._refresh_one(sid) or 0

        counts = await asyncio.gather(*(one(sid) for sid in ids))
        return sum(counts)

    @if_errors
    async def _refresh_one(self, sid):
        with self._lock:
            s = self.shows_data.get(sid)
            if not s:
                return 0
            feed_url, etag, modified, old_image = s['feed_url'], s.get('etag'), s.get('last_modified'), s.get('image_url')
        try:
            got = await self._fetch_feed(feed_url, etag, modified)
        except FeedError as e:
            with self._lock:
                if sid in self.shows_data:
                    self.shows_data[sid].update(checked=time.time(), error=str(e))
                    self._save_shows()
            print('[PODCASTS] %s: %s' % (s.get('title') or sid, e), flush=True)
            return 0
        now = time.time()
        with self._lock:
            s = self.shows_data.get(sid)
            if not s:                                 # unsubscribed meanwhile
                return 0
            if got is None:                           # 304: nothing new
                s.update(checked=now, error=None)
                self._save_shows()
                return 0
            show, episodes = got
            fresh = {ep['id'] for ep in episodes}
            before = {ep['id'] for ep in s.get('episodes', [])}
            # the feed's version of an episode wins (ad-inserted URLs change);
            # older ones the feed no longer lists stay playable
            kept = episodes + [ep for ep in s.get('episodes', []) if ep['id'] not in fresh]
            kept.sort(key=lambda e: e.get('ts') or 0, reverse=True)
            s.update({k: v for k, v in show.items() if v or k in ('etag', 'last_modified')})
            s.update(episodes=kept[:EPISODES_KEPT], fetched=now, checked=now, error=None)
            self._index()
            self._save_shows()
            new = len(fresh - before)
        if show.get('image_url') and (show['image_url'] != old_image or not self.art_path(sid)):
            await self._fetch_art(sid, show['image_url'])
        return new

    # ------------------------------------------------------------ cards
    def _show_card(self, sid):
        s = self.shows_data[sid]
        eps = s.get('episodes', [])
        cutoff = time.time() - NEW_DAYS * 86400
        card = {
            'id': sid, 'type': 'PodcastShow', 'title': s.get('title') or 'Untitled podcast',
            'author': s.get('author') or '',
            'has_art': os.path.exists(os.path.join(self.art_dir, sid + '.jpg')),
            'latest': eps[0].get('date') if eps else None,
            'new': sum(1 for ep in eps if (ep.get('ts') or 0) >= cutoff
                       and not (self.progress.get(ep['id']) or {}).get('played')),
            'count': len(eps),
        }
        if s.get('error'):
            card['error'] = s['error']
        return card

    def _episode_card(self, sid, ep):
        prog = self.progress.get(ep['id']) or {}
        pos = prog.get('pos') or 0
        card = {
            'id': ep['id'], 'type': 'PodcastEpisode', 'show_id': sid,
            'show': self.shows_data.get(sid, {}).get('title') or '',
            'title': ep.get('title') or '', 'date': ep.get('date'), 'duration': ep.get('duration'),
            'summary': ep.get('summary') or '', 'url': ep.get('url'),
            'resume': round(pos) if pos > RESUME_MIN_S and not prog.get('played') else None,
            'played': bool(prog.get('played')),
        }
        if ep.get('transcript'):
            card['transcript'] = ep['transcript']
        return card

    def shows(self):
        with self._lock:
            cards = [self._show_card(sid) for sid in self.shows_data]
        return sorted(cards, key=lambda c: c['latest'] or '', reverse=True)

    def episodes(self, show_id, limit=200):
        _check_id(show_id)
        with self._lock:
            s = self.shows_data.get(show_id)
            if s is None:
                raise NotFound('No such show')
            return [self._episode_card(show_id, ep) for ep in s.get('episodes', [])[:max(0, int(limit))]]

    def episode(self, episode_id):
        _check_id(episode_id)
        with self._lock:
            found = self._eps.get(episode_id)
            if not found:
                return None
            sid, ep = found
            return {**self._episode_card(sid, ep), 'art': sid}

    # ------------------------------------------------------------ where each episode was stopped
    def save_position(self, episode_id, position, duration):
        """Every few seconds while an episode plays. Near the end it counts as
        played: within 60 s of it, or 97% through."""
        _check_id(episode_id)
        position = max(0.0, float(position or 0))
        duration = float(duration) if duration else None
        if duration is None:
            with self._lock:
                found = self._eps.get(episode_id)
            duration = float(found[1].get('duration') or 0) or None if found else None
        done = bool(duration) and (duration - position <= 60 or position / duration >= 0.97)
        with self._lock:
            found = self._eps.get(episode_id)
            prog = self.progress.setdefault(episode_id, {'show_id': found[0] if found else None})
            if done:
                prog.update(played=True, pos=0, at=time.time())
            else:
                prog.update(pos=round(position, 1), at=time.time())
                # a played episode started again is in progress again only once
                # it's past the resume threshold
                if prog.get('played') and position > RESUME_MIN_S:
                    prog['played'] = False
            self._save_progress()

    def mark_played(self, episode_id, played=True):
        _check_id(episode_id)
        with self._lock:
            found = self._eps.get(episode_id)
            prog = self.progress.setdefault(episode_id, {'show_id': found[0] if found else None})
            prog.update(played=bool(played), pos=0, at=time.time())
            self._save_progress()


def _to_jpeg(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
            im = im.convert('RGBA')
            bg = Image.new('RGB', im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert('RGB')
        im.thumbnail((ART_SIZE, ART_SIZE))
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=85)
        return out.getvalue()
