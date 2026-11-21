"""The music library as the TV browses it: every album and artist, A to Z,
with compilations that a rip scattered across folders put back together.
Doc: docs/radio.md and docs/server.md.

Jellyfin makes one album per folder. iTunes-style rips often file a
compilation's songs under each song's own artist (Music/Artist A/Soundtrack…,
Music/Artist B/Soundtrack…), so one soundtrack can come out as dozens of
one-song albums. Folders with the same album name are one album again here,
except where two of them are whole albums of their own by different artists
(two artists' "Greatest Hits", or two different albums called "Live").
Nothing on disk or in Jellyfin changes.
"""
import re
import unicodedata
from collections import defaultdict

GENERIC_NAMES = {'', 'unknown album', 'unknown', 'none', 'untitled'}
WHOLE_ALBUM = 10        # a folder with this many songs is an album in its own right
_ARTICLE = re.compile(r'^(the|a|an)\s+')


def _plain(s):
    s = unicodedata.normalize('NFKD', s or '')
    return ''.join(ch for ch in s if not unicodedata.combining(ch)).lower()


def name_key(name):
    return re.sub(r'\s+', ' ', _plain(name)).strip()


def artist_key(name):
    """ "AC-DC" and "AC/DC", "Estés" and "Estes" are the same artist."""
    return re.sub(r'[^a-z0-9]', '', _plain(name))


def sort_key(name):
    """A to Z the way a record shop files them: no leading "The", numbers in order."""
    s = _ARTICLE.sub('', re.sub(r'^[^a-z0-9]+', '', name_key(name)))
    return re.sub(r'\d+', lambda m: m.group().zfill(8), s)


def letter(name):
    ch = sort_key(name)[:1]
    return ch.upper() if 'a' <= ch <= 'z' else '#'


def track_order(t):
    return (t.get('ParentIndexNumber') or 1, t.get('IndexNumber') or 10 ** 6, sort_key(t.get('Name')))


def _has_image(item):
    return bool((item.get('ImageTags') or {}).get('Primary'))


def _artist_ids(album):
    return {a.get('Id') for a in album.get('AlbumArtists') or []}


class MusicIndex:
    """Built from Jellyfin's raw items: MusicAlbum (with DateCreated), Audio, and
    the library's album artists."""

    def __init__(self, albums, tracks, artists):
        self.tracks = list(tracks)
        self.by_track = {t['Id']: t for t in self.tracks}
        self.by_album = defaultdict(list)
        loose = defaultdict(list)      # songs in no album folder (loose in Music/)
        for t in self.tracks:
            if t.get('AlbumId'):
                self.by_album[t['AlbumId']].append(t)
            else:
                loose[name_key(t.get('Album'))].append(t)
        named = defaultdict(list)
        for a in albums:
            if self.by_album.get(a['Id']):
                named[name_key(a.get('Name'))].append(a)
        self.groups = []
        for key, members in named.items():
            whole = [a for a in members if len(self.by_album[a['Id']]) >= WHOLE_ALBUM]
            if key in GENERIC_NAMES:
                sets = [[a] for a in members]
            elif len(whole) >= 2:      # same name, separate albums: one per artist
                by_artist = defaultdict(list)
                for a in members:
                    by_artist[artist_key(a.get('AlbumArtist'))].append(a)
                sets = list(by_artist.values())
            else:
                sets = [members]
            self.groups += [self._group(s) for s in sets]
        # Loose songs that copy an album already here (the same song both in its
        # album folder and loose at the top of Music/) don't make a second card;
        # they still come up in Shuffle all.
        have = {(name_key(g['name']), artist_key(g['artist'])) for g in self.groups}
        for songs in loose.values():
            card = self._loose(songs)
            if (name_key(card['name']), artist_key(card['artist'])) not in have:
                self.groups.append(card)
        self.groups.sort(key=lambda g: (g['sort'], g['artist'].lower()))
        self.artists = sorted(
            ({'id': a['Id'], 'type': 'MusicArtist', 'name': a.get('Name', ''), 'has_image': _has_image(a),
              'letter': letter(a.get('Name')), 'sort': sort_key(a.get('Name'))} for a in artists),
            key=lambda a: a['sort'])

    def _group(self, members):
        size = lambda a: len(self.by_album[a['Id']])
        members = sorted(members, key=size, reverse=True)
        top = members[0]
        lead = next((a for a in members if _has_image(a)), top)
        one_artist = len({artist_key(a.get('AlbumArtist')) for a in members}) == 1
        name = top.get('Name') or 'Unknown album'
        return {
            'id': lead['Id'], 'type': 'MusicAlbum', 'name': name,
            'artist': (top.get('AlbumArtist') or ', '.join(top.get('Artists') or [])) if one_artist else 'Various artists',
            'year': top.get('ProductionYear'), 'has_image': _has_image(lead),
            'count': sum(size(a) for a in members), 'ids': [a['Id'] for a in members], 'songs': [],
            'letter': letter(name), 'sort': sort_key(name),
            'added': max(a.get('DateCreated') or '' for a in members),
            '_members': members,
        }

    def _loose(self, songs):
        songs = sorted(songs, key=track_order)
        lead = next((t for t in songs if _has_image(t)), songs[0])
        name = songs[0].get('Album') or 'Unknown album'
        return {
            'id': lead['Id'], 'type': 'MusicAlbum', 'name': name,
            'artist': songs[0].get('AlbumArtist') or ', '.join(songs[0].get('Artists') or []),
            'year': songs[0].get('ProductionYear'), 'has_image': _has_image(lead),
            'count': len(songs), 'ids': [], 'songs': [t['Id'] for t in songs],
            'letter': letter(name), 'sort': sort_key(name),
            'added': max(t.get('DateCreated') or '' for t in songs),
            '_members': [],
        }

    @staticmethod
    def card(group):
        return {k: v for k, v in group.items() if not k.startswith('_') and k not in ('sort', 'added')}

    def albums(self):
        return [self.card(g) for g in self.groups]

    def recent(self, limit=40):
        return [self.card(g) for g in sorted(self.groups, key=lambda g: g['added'], reverse=True)[:limit]]

    def artist_albums(self, artist_id):
        """An artist's albums. On a compilation they appear in, only their own songs."""
        out = []
        for g in self.groups:
            mine = [a for a in g['_members'] if artist_id in _artist_ids(a)]
            if mine:
                out.append(self.card(g if len(mine) == len(g['_members']) else self._group(mine)))
        out.sort(key=lambda c: (c['year'] or 9999, sort_key(c['name'])))
        return out

    def songs(self, album_ids=(), track_ids=()):
        """The songs of these albums (in album order) and these loose songs, as raw items."""
        picked = [t for i in album_ids for t in self.by_album.get(i, ())]
        picked += [self.by_track[i] for i in track_ids if i in self.by_track]
        return sorted(picked, key=track_order)
