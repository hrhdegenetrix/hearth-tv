"""Radio for the music library: stations that play on and on, the Pandora way.
Doc: docs/radio.md.

What a station knows, without hearing a note yet:
- **History** (music_history.py, an old iTunes library if there is one): how
  often each song was played, what was played which year, the playlists.
- **What goes together**: songs that share a playlist (iPod-era song-seeded
  mixes are ideal), artists on the same compilation, genre families (a hundred
  or so tag spellings folded into ~19), and era.
- **This session**: a skipped song's artist comes up less, a loved one more;
  "Not for this station" keeps a song off that station for good.
- **What gets skipped**: iTunes skip counts, and every song the TV plays (the
  listening log: played through, or skipped early), so a song that keeps
  getting skipped comes up less on every station, and one played through on
  the TV counts toward Old favorites.

Rules every station keeps: no song twice in a session, an artist doesn't come
back within 4 songs, no two songs in a row from one album, and nothing the TV
played in the last 12 hours unless the station would run dry. Podcasts and
audiobooks that live in the music library never play on the radio; spoken word
and comedy have a station of their own.

What Music Flamingo heard (flamingo_genres.py): a song with no genre tag (CD
rips usually have none) gets its families from the first sentence of
Flamingo's description of it, so it joins the genre stations. Ideas for later:
stations by how songs sound ("rainy Sunday"), and "more like this" by ear.
"""
import math
import random
import re
import time
import uuid
from collections import Counter, defaultdict

from hearth.music_index import artist_key

TICKS = 10_000_000

# (key, name, pattern on the lowercased genre tag). Every family a tag matches
# counts; plain "rock" only when nothing more specific did.
FAMILIES = [
    # Classical, opera and sacred music often arrive as CD rips with no genre tag at all; their
    # families then come from Music Flamingo (flamingo_genres.py) when a song has no tag
    ('classical', 'Classical', r'classical|baroque|orchestral(?! pop)|symphon|chamber music|concerto|sonata|'
                               r'string quartet|romantic era|neoclassical'),
    ('opera', 'Opera & art song', r'opera|\baria\b|\blied\b|art song|operetta|vocal music'),
    ('sacred', 'Sacred & choral', r'sacred|choral|chant|liturg|requiem|\bmass\b|hymn|gregorian|gospel|devotional'),
    ('goth', 'Goth & industrial', r'goth|industrial|darkwave|deathrock|ebm'),
    ('punk', 'Punk & no wave', r'punk|no wave|grunge|noise rock|hardcore'),
    ('experimental', 'Experimental & noise', r'avant|experimental|noise|drone|minimalism|post-rock|math rock|'
                                             r'unclassifiable|turntablism'),
    ('prog', 'Prog & psych', r'prog|psych|art rock'),
    ('alt', 'Alternative & indie', r'alternative|indie|lo-fi|new wave|noise pop'),
    ('classic', 'Classic rock', r'classic rock|classic pop/rock|rock & roll|hard rock|blues ?rock|metal|folk/rock'),
    ('jazz', 'Jazz & blues', r'jazz|^blues$|swing|big band|lounge|^vocal$|easy listening|chill'),
    ('oldies', 'Oldies & soul', r'oldies|soul|r&b|motown|doo.?wop|50.s|rock & roll'),
    ('electronic', 'Electronic', r'electronic|trip.?hop|dance|house|drum|ambient|synth'),
    ('hiphop', 'Hip-hop', r'hip.?hop|rap|gangsta'),
    ('folk', 'Folk & acoustic', r'folk|acoustic|singer|country'),
    ('pop', 'Pop', r'^pop$|power pop'),
    ('soundtrack', 'Soundtracks & musicals', r'soundt|soundrack|film|cabaret|musical|score'),
    ('reggae', 'Reggae & world', r'reggae|\bska\b|\bdub\b|afrobeat|afro.?funk|world|latin|bossa|samba|calypso|'
                                 r'flamenco|celtic|african|caribbean'),
    ('holiday', 'Holiday', r'holiday|christmas'),
    ('rock', 'Rock', r'rock'),
]
FAMILY_NAMES = {k: n for k, n, _ in FAMILIES}
_FAMILY_RE = [(k, re.compile(p)) for k, _, p in FAMILIES]
SPOKEN = re.compile(r'^comedy$|spoken|books|^otr$')
NOT_MUSIC = re.compile(r'podcast|audio ?books?')
LONGEST = 30 * 60            # longer than this is a reading or a podcast, not a song
MIN_STATION = 60             # a station needs this many songs
RECENT_HOURS = 12
ARTIST_GAP = 4
BATCH = 25
DAILY = 100               # a station's list for the day: it holds still until midnight


def families(genres):
    tags = [g.lower().strip() for g in genres or []]
    out = {k for k, rx in _FAMILY_RE if k != 'rock' and any(rx.search(t) for t in tags)}
    if not out and any('rock' in t for t in tags):
        out.add('rock')
    return out


def _artist(t):
    return artist_key(t.get('AlbumArtist') or (t.get('Artists') or [''])[0])


def _artist_name(t):
    return t.get('AlbumArtist') or (t.get('Artists') or [''])[0]


class Song:
    __slots__ = ('id', 'raw', 'artist', 'album', 'album_name', 'year', 'families', 'kind', 'plays', 'skips',
                 'last_year', 'seconds', 'dupe')

    def __init__(self, raw, hist, tv=(0, 0), heard=None):
        self.id = raw['Id']
        self.raw = raw
        self.artist = _artist(raw)
        self.album = raw.get('AlbumId') or ('loose:' + (raw.get('Album') or ''))
        self.album_name = re.sub(r'[^a-z0-9]', '', (raw.get('Album') or '').lower())
        self.year = raw.get('ProductionYear')
        tags = [g.lower() for g in raw.get('Genres') or []]
        path = raw.get('Path') or ''
        self.seconds = (raw.get('RunTimeTicks') or 0) / TICKS
        if '/Podcasts/' in path or any(NOT_MUSIC.search(t) for t in tags):
            self.kind = 'other'
        elif any(SPOKEN.search(t) for t in tags):
            self.kind = 'spoken'
        elif self.seconds > LONGEST:
            self.kind = 'other'
        else:
            self.kind = 'music'
        self.families = families(raw.get('Genres')) if self.kind == 'music' else set()
        if self.kind == 'music' and not self.families and heard:
            self.families = set(heard)        # what Music Flamingo heard (flamingo_genres.py), when no tag says
        self.plays = ((hist or {}).get('plays') or 0) + tv[0]
        self.skips = ((hist or {}).get('skips') or 0) + tv[1]
        last = (hist or {}).get('last')
        self.last_year = int(last[:4]) if last else None
        self.dupe = False


class Radio:
    """Stations over one MusicIndex + history. Cheap to build (~0.1 s); rebuilt
    with the index."""

    def __init__(self, index, history, store=None, rng=None, tv=None, heard=None):
        self.index = index
        self.history = history or {'songs': {}, 'playlists': []}
        self.store = store if store is not None else {'bans': {}, 'loves': []}
        self.rng = rng or random.Random()
        hs = self.history.get('songs') or {}
        tv = tv or {}                  # {song id: (played through on the TV, skipped on the TV)}
        heard = heard or {}            # {song id: [families]} from Music Flamingo, for songs with no genre tag
        self.songs = {t['Id']: Song(t, hs.get(t['Id']), tv.get(t['Id'], (0, 0)), heard.get(t['Id']))
                      for t in index.tracks}
        self._mark_copies()
        self.music = [s for s in self.songs.values() if s.kind == 'music' and not s.dupe]
        self.by_artist = defaultdict(list)
        for s in self.songs.values():
            self.by_artist[s.artist].append(s)
        self.artist_plays = Counter()
        for s in self.music:
            self.artist_plays[s.artist] += s.plays
        # a 'playlist' of 400+ songs is a whole-library dump, not a mix: it says nothing about what goes together
        self.playlists = [p for p in self.history.get('playlists') or [] if len(p['tracks']) <= 400]
        self.loved = set(self.store.get('loves') or [])
        self._build_neighbors()

    def _mark_copies(self):
        """The same song in the library twice (a re-rip, or an iTunes duplicate):
        same title, artist and album, lengths within 3 s. The most-played copy
        stays; the rest never come up on a station or in Shuffle all. Live and
        studio versions differ in album, and nameless tracks like "(-)" are never
        judged."""
        groups = defaultdict(list)
        for s in self.songs.values():
            name = (s.raw.get('Name') or '').strip().lower()
            if re.search(r'[a-z0-9]', name):
                groups[(name, s.artist, s.album_name)].append(s)
        for copies in groups.values():
            if len(copies) < 2:
                continue
            copies.sort(key=lambda s: (-s.plays, s.seconds))
            kept = [copies[0]]
            for s in copies[1:]:
                if any(abs(s.seconds - k.seconds) <= 3 for k in kept):
                    s.dupe = True
                else:
                    kept.append(s)

    # -------------------------------------------------------------- what goes together
    def _build_neighbors(self):
        """Artist ↔ artist from the playlists and the compilations, song ↔ song
        from the playlists."""
        self.artist_links = defaultdict(Counter)
        self.song_links = defaultdict(Counter)
        for p in self.playlists:
            ids = [i for i in p['tracks'] if i in self.songs]
            if len(ids) > 100:
                continue
            artists = {self.songs[i].artist for i in ids}
            for a in artists:
                for b in artists:
                    if a != b:
                        self.artist_links[a][b] += 1.0 / len(artists)
            for i in ids:
                for j in ids:
                    if i != j:
                        self.song_links[i][j] += 1.0
        for g in self.index.groups:
            members = g.get('_members') or []
            artists = {artist_key(a.get('AlbumArtist')) for a in members}
            if len(artists) > 1:
                for a in artists:
                    for b in artists:
                        if a != b:
                            self.artist_links[a][b] += 0.5 / len(artists)
        self.artist_families = {}
        self.artist_year = {}
        for a, songs in self.by_artist.items():
            fam = Counter(f for s in songs for f in s.families)
            total = sum(fam.values()) or 1
            self.artist_families[a] = {f: n / total for f, n in fam.items()}
            years = sorted(s.year for s in songs if s.year)
            self.artist_year[a] = years[len(years) // 2] if years else None

    # -------------------------------------------------------------- stations
    @staticmethod
    def _skipped(s):
        """0.4..1: a song that mostly gets skipped comes up less (plays 2, skips 4 → 0.47)."""
        return 1.0 / (1.0 + 2.0 * s.skips / (s.plays + s.skips + 1)) if s.skips else 1.0

    def _familiar(self, s):
        return (1.0 + 0.35 * math.log1p(s.plays + (5 if s.id in self.loved else 0))) * self._skipped(s)

    def pool(self, station):
        """{song id: weight} for a station id."""
        kind, _, arg = station.partition(':')
        loved = self.loved
        if kind == 'favorites':
            return {s.id: (s.plays + (8 if s.id in loved else 0)) ** 0.7 * self._skipped(s) for s in self.music
                    if s.plays >= 3 or s.id in loved}
        if kind == 'deepcuts':
            return {s.id: math.log1p(self.artist_plays[s.artist]) for s in self.music
                    if s.plays == 0 and s.skips < 2 and self.artist_plays[s.artist] >= 15}
        if kind == 'year':
            year = int(arg)
            return {s.id: (1 + math.log1p(s.plays)) * self._skipped(s) for s in self.music if s.last_year == year and s.plays}
        if kind == 'decade':
            start = int(arg)
            return {s.id: self._familiar(s) for s in self.music if s.year and start <= s.year < start + 10}
        if kind == 'genre':
            return {s.id: self._familiar(s) for s in self.music if arg in s.families}
        if kind == 'spoken':
            return {s.id: 1.0 for s in self.songs.values() if s.kind == 'spoken' and not s.dupe}
        if kind == 'artist':
            return self._artist_pool(arg)
        if kind == 'song':
            return self._song_pool(arg)
        raise KeyError(station)

    def _similar(self, a, s):
        """How well song s goes with artist a (0..~3)."""
        links = self.artist_links.get(a) or {}
        top = max(links.values()) if links else 0
        link = links.get(s.artist, 0) / top if top else 0
        fam_a = self.artist_families.get(a) or {}
        fam = sum(fam_a.get(f, 0) for f in s.families)
        year_a = self.artist_year.get(a)
        era = math.exp(-abs(s.year - year_a) / 10) if (s.year and year_a) else 0.3
        return 2.0 * link + 0.8 * fam + 0.3 * era

    def _artist_pool(self, artist_id):
        name = next((x['name'] for x in self.index.artists if x['id'] == artist_id), None)
        if name is None:
            raise KeyError(artist_id)
        a = artist_key(name)
        own = [s for s in self.by_artist.get(a, []) if s.kind != 'other']
        spoken = own and sum(s.kind == 'spoken' for s in own) > len(own) / 2
        pool = {}
        for s in (self.songs.values() if spoken else self.music):
            if s.artist == a or (spoken and s.kind != 'spoken'):
                continue
            score = self._similar(a, s)
            if score >= 0.9:
                pool[s.id] = score ** 2 * self._familiar(s)
        others = sum(pool.values())
        if own:   # about a third of the station is the artist themself
            share = max(others / 2, len(own)) / len(own)
            for s in own:
                pool[s.id] = share * self._familiar(s)
        return pool

    def _song_pool(self, song_id):
        seed = self.songs.get(song_id)
        if not seed:
            raise KeyError(song_id)
        artist_id = next((x['id'] for x in self.index.artists if artist_key(x['name']) == seed.artist), None)
        pool = self._artist_pool(artist_id) if artist_id else {}
        top = max(pool.values()) if pool else 1.0
        for j, n in (self.song_links.get(song_id) or {}).items():
            s = self.songs.get(j)
            if s and s.kind == seed.kind:
                pool[j] = pool.get(j, 0) + top * min(3, n)     # the playlists that hold this song
        pool.pop(song_id, None)
        return pool

    @staticmethod
    def label(station):
        """(name, sub) of a catalog station."""
        kind, _, arg = station.partition(':')
        if kind == 'favorites':
            return 'Old favorites', 'the songs you played most'
        if kind == 'deepcuts':
            return 'Deep cuts', "songs you've never played, by artists you love"
        if kind == 'year':
            return f'Time capsule {arg}', f'what you were playing in {arg}'
        if kind == 'decade':
            return (f'The {arg[2:]}s' if int(arg) < 2000 else f'The {arg}s'), 'by release year'
        if kind == 'genre':
            return FAMILY_NAMES.get(arg, arg), 'radio'
        if kind == 'spoken':
            return 'Spoken word & comedy', 'readings and comedy'
        return station, ''

    def stations(self, now=None):
        """The station cards, in the order the TV and the phone show them."""
        now = now or time.localtime()
        holiday = (now.tm_mon == 11 and now.tm_mday >= 15) or now.tm_mon == 12 or (now.tm_mon == 1 and now.tm_mday <= 6)
        ids = ['favorites', 'deepcuts']
        ids += [f'year:{y}' for y in sorted({s.last_year for s in self.music if s.last_year})]
        ids += [f'decade:{d}' for d in (1950, 1960, 1970, 1980, 1990, 2000, 2010)]
        ids += [f'genre:{k}' for k, _, _ in FAMILIES if k != 'holiday' or holiday]
        ids.append('spoken')
        out = []
        for sid in ids:
            n = len(self.pool(sid))
            if n >= MIN_STATION or (sid == 'spoken' and n >= 20):
                name, sub = self.label(sid)
                out.append({'id': sid, 'type': 'Station', 'name': name, 'sub': sub, 'count': n})
        return out

    def playlist_name(self, p):
        """The household's own name for an imported playlist, if it was renamed
        (store['names'], by playlist id). The songs don't change."""
        return (self.store.get('names') or {}).get(p['id']) or p['name']

    def playlist_cards(self):
        cards = [{'id': 'playlist:' + p['id'], 'type': 'Playlist', 'name': self.playlist_name(p),
                  'count': len(p['tracks'])} for p in self.playlists]
        return sorted(cards, key=lambda c: c['name'].lower())

    def playlist(self, playlist_id):
        p = next((p for p in self.playlists if p['id'] == playlist_id), None)
        if not p:
            raise KeyError(playlist_id)
        return {**p, 'name': self.playlist_name(p)}, [self.songs[i].raw for i in p['tracks'] if i in self.songs]

    def name_of(self, station):
        kind, _, arg = station.partition(':')
        if kind == 'artist':
            name = next((x['name'] for x in self.index.artists if x['id'] == arg), '')
            return f'{name} radio'
        if kind == 'song':
            s = self.songs.get(arg)
            return f"Radio from “{s.raw.get('Name', '')}”" if s else 'Song radio'
        return self.label(station)[0]

    # -------------------------------------------------------------- sessions
    def session(self, station, recent=(), rng=None):
        pool = self.pool(station)
        banned = set((self.store.get('bans') or {}).get(station) or []) | set(self.store.get('dislikes') or [])
        for i in banned:
            pool.pop(i, None)
        if not pool:
            raise LookupError('Nothing to play on that station')
        kind, _, arg = station.partition(':')
        return {'id': uuid.uuid4().hex[:12], 'station': station, 'name': self.name_of(station),
                'pool': pool, 'played': [], 'artist_mult': {}, 'recent': set(recent),
                'lead': arg if kind == 'song' else None, 'started': time.time(), 'rng': rng or self.rng}

    def next_batch(self, session, n=BATCH):
        """The next n songs (raw Jellyfin items). Endless: once every song in the
        pool has played, it starts over, minus the last 50."""
        picks = []
        if session.get('lead') in self.songs:    # song radio opens with its song
            lead = self.songs[session.pop('lead')]
            session['played'].append(lead.id)
            picks.append(lead.raw)
        while len(picks) < n:
            s = self._pick(session)
            if not s:
                break
            session['played'].append(s.id)
            picks.append(s.raw)
        return picks

    def _pick(self, session):
        pool, played = session['pool'], session['played']
        used = set(played)
        if len(used) >= len(pool):                 # went all the way round
            used = set(played[-50:])
        tail = [self.songs[i] for i in played[-ARTIST_GAP:]]
        last_album = tail[-1].album if tail else None
        recent_artists = {t.artist for t in tail}
        mult = session['artist_mult']
        # relax the spacing before a song played in the last hours comes back
        for spaced, fresh in ((True, True), (False, True), (True, False), (False, False)):
            ids, weights = [], []
            for i, w in pool.items():
                if i in used or (fresh and i in session['recent']):
                    continue
                s = self.songs[i]
                if spaced and (s.artist in recent_artists or s.album == last_album):
                    continue
                ids.append(i)
                weights.append(w * mult.get(s.artist, 1.0))
            if ids:
                return self.songs[session.get('rng', self.rng).choices(ids, weights)[0]]
        return None

    def daily(self, station, date, recent=()):
        """A station's list for one day: DAILY song ids, the same all day (leaving
        a station and coming back shouldn't reshuffle everything). Seeded by
        station and date; the server keeps the list it made so a restart or the
        day's new listening doesn't reshuffle it."""
        session = self.session(station, recent=recent, rng=random.Random(f'{station}|{date}'))
        return [t['Id'] for t in self.next_batch(session, min(DAILY, len(session['pool'])))]

    def feedback(self, session, song_id, kind):
        """skip | love | ban — for the rest of this session; love and ban are kept."""
        s = self.songs.get(song_id)
        if not s:
            return
        if not session:          # ♥ or "Not for me" on an album or a shuffle: kept all the same
            session = {'artist_mult': {}, 'station': None, 'pool': {}}
            if kind not in ('love', 'unlove', 'dislike', 'undislike'):
                return
        if kind == 'unlove':
            loves = self.store.setdefault('loves', [])
            if song_id in loves:
                loves.remove(song_id)
            self.loved.discard(song_id)
            return
        mult = session['artist_mult']
        if kind == 'skip':
            mult[s.artist] = mult.get(s.artist, 1.0) * 0.5
        elif kind == 'love':
            mult[s.artist] = min(3.0, mult.get(s.artist, 1.0) * 1.5)
            loves = self.store.setdefault('loves', [])
            if song_id not in loves:
                loves.append(song_id)
            self.loved.add(song_id)
        elif kind in ('dislike', 'undislike'):      # "Not for me": off every station and Shuffle all
            dislikes = self.store.setdefault('dislikes', [])
            if kind == 'dislike' and song_id not in dislikes:
                dislikes.append(song_id)
            elif kind == 'undislike' and song_id in dislikes:
                dislikes.remove(song_id)
            session['pool'].pop(song_id, None)
        elif kind == 'ban':
            bans = self.store.setdefault('bans', {}).setdefault(session['station'], [])
            if song_id not in bans:
                bans.append(song_id)
            session['pool'].pop(song_id, None)
