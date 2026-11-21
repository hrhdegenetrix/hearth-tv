"""Listening history from an old iTunes library, if the music came over with
one. Optional: without it the radio still works, from the TV's own listening
log. Doc: docs/radio.md.

`iTunes Music Library.xml` (iTunes' XML export; TV_ITUNES_XML, default
<TV_MEDIA_ROOT>/Music/iTunes/iTunes Music Library.xml) holds what Jellyfin
can't know: how often each song was played, skips, when it was last played, a
few star ratings, and the playlists (often an iPod's song-seeded mixes, named
"<song> (SharePod)" by SharePod exports). The radio reads it to know the old
favorites and which songs go together.

iTunes tracks are matched to Jellyfin's by album folder + file name (the rip
kept iTunes' folders), then by title + artist, title + album, and a file name
found only once. Duplicates in iTunes add up onto one song. Nothing is written
to the media share; the result is cached in hearth/data/ and rebuilt when the
XML or the library changes.
"""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import unicodedata
import urllib.parse
from collections import defaultdict

ITUNES_XML = os.getenv('TV_ITUNES_XML') or str(
    Path(os.getenv('TV_MEDIA_ROOT', '/mnt/media')) / 'Music' / 'iTunes' / 'iTunes Music Library.xml')
MIN_PLAYLIST = 5          # a playlist with fewer matched songs isn't worth a card
CACHE_VERSION = 1


def plain(s):
    s = unicodedata.normalize('NFKD', s or '')
    return re.sub(r'[^a-z0-9]', '', ''.join(ch for ch in s if not unicodedata.combining(ch)).lower())


def _tail(path, n=2):
    return '/'.join(plain(p) for p in re.split(r'[\\/]', path or '')[-n:])


def _itunes_path(location):
    return urllib.parse.unquote(urllib.parse.urlparse(location or '').path)


class _Matcher:
    """iTunes track → Jellyfin track id."""

    def __init__(self, jf_tracks):
        self.tail2, self.tail1 = defaultdict(list), defaultdict(list)
        self.artist, self.album = defaultdict(list), defaultdict(list)
        for t in jf_tracks:
            path = t.get('Path') or ''
            self.tail2[_tail(path)].append(t['Id'])
            self.tail1[_tail(path, 1)].append(t['Id'])
            artist = t.get('AlbumArtist') or (t.get('Artists') or [''])[0]
            self.artist[(plain(t.get('Name')), plain(artist))].append(t['Id'])
            self.album[(plain(t.get('Name')), plain(t.get('Album')))].append(t['Id'])

    def __call__(self, it):
        path = _itunes_path(it.get('Location'))
        name = plain(it.get('Name'))
        for ids in (self.tail2.get(_tail(path)),
                    self.artist.get((name, plain(it.get('Album Artist') or it.get('Artist')))),
                    self.artist.get((name, plain(it.get('Artist')))),
                    self.album.get((name, plain(it.get('Album')))),
                    self.tail1.get(_tail(path, 1))):
            if ids and len(ids) == 1:
                return ids[0]
        return None


def build(library, jf_tracks):
    """`library` is the parsed plist. Returns {songs: {jf_id: {...}}, playlists: [...]}."""
    match = _Matcher(jf_tracks)
    by_itunes = {}
    songs = {}
    for tid, it in (library.get('Tracks') or {}).items():
        if it.get('Podcast') or it.get('Has Video'):
            continue
        jf_id = match(it)
        if not jf_id:
            continue
        by_itunes[str(tid)] = jf_id
        s = songs.setdefault(jf_id, {'plays': 0, 'skips': 0, 'last': None, 'rating': None, 'added': None})
        s['plays'] += int(it.get('Play Count') or 0)
        s['skips'] += int(it.get('Skip Count') or 0)
        last = it.get('Play Date UTC')
        if last and (not s['last'] or last.isoformat() > s['last']):
            s['last'] = last.isoformat()
        if it.get('Rating') and not it.get('Rating Computed'):
            s['rating'] = max(s['rating'] or 0, int(it['Rating']) // 20)
        added = it.get('Date Added')
        if added and (not s['added'] or added.isoformat() < s['added']):
            s['added'] = added.isoformat()
    playlists = []
    for pl in library.get('Playlists') or []:
        if pl.get('Master') or pl.get('Distinguished Kind') or pl.get('Smart Info') or pl.get('Folder'):
            continue
        ids, seen = [], set()
        for item in pl.get('Playlist Items') or []:
            jf_id = by_itunes.get(str(item.get('Track ID')))
            if jf_id and jf_id not in seen:
                seen.add(jf_id)
                ids.append(jf_id)
        name = re.sub(r'\s*\(SharePod\)\s*$', '', pl.get('Name') or '').strip()
        if len(ids) < MIN_PLAYLIST or not name:
            continue
        playlists.append({'id': hashlib.sha1(name.encode()).hexdigest()[:12], 'name': name, 'tracks': ids})
    # two playlists can share a name once "(SharePod)" is gone: keep the bigger
    best = {}
    for p in playlists:
        if p['id'] not in best or len(p['tracks']) > len(best[p['id']]['tracks']):
            best[p['id']] = p
    playlists = sorted(best.values(), key=lambda p: p['name'].lower())
    return {'songs': songs, 'playlists': playlists}


def load(cache_path, jf_tracks, xml_path=ITUNES_XML):
    """The history for these Jellyfin tracks, from the cache when the XML and the
    library are unchanged. Missing XML → an empty history (the radio still works)."""
    try:
        mtime = os.path.getmtime(xml_path)
    except OSError:          # no XML, or the share is unreachable: the last good build, else nothing
        try:
            with open(cache_path) as f:
                return json.load(f)['history']
        except (OSError, ValueError, KeyError):
            return {'songs': {}, 'playlists': []}
    key = {'v': CACHE_VERSION, 'mtime': mtime, 'tracks': len(jf_tracks),
           'ids': hashlib.sha1(''.join(sorted(t['Id'] for t in jf_tracks)).encode()).hexdigest()}
    try:
        with open(cache_path) as f:
            cached = json.load(f)
        if cached.get('key') == key:
            return cached['history']
    except (OSError, ValueError):
        pass
    with open(xml_path, 'rb') as f:
        history = build(plistlib.load(f), jf_tracks)
    tmp = cache_path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump({'key': key, 'history': history}, f)
    os.replace(tmp, cache_path)
    return history
