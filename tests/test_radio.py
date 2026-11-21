"""Radio stations (hearth/radio.py): stations from the household's own
listening history, the iTunes matcher, and what Music Flamingo heard.
Doc: docs/radio.md.
"""
import datetime
import random

from hearth import music_history  # noqa: E402
from hearth.music_index import MusicIndex  # noqa: E402
from hearth.radio import Radio, families  # noqa: E402

_n = [0]


def _id():
    _n[0] += 1
    return '%032x' % _n[0]


def lib(spec):
    """spec: [(artist, album, year, genre, [song names])] → (albums, tracks, artists)."""
    albums, tracks, artists = [], [], {}
    for artist, album, year, genre, names in spec:
        aid = artists.setdefault(artist, _id())
        a = {'Id': _id(), 'Name': album, 'AlbumArtist': artist, 'ProductionYear': year,
             'AlbumArtists': [{'Id': aid, 'Name': artist}], 'ImageTags': {}}
        albums.append(a)
        for n, name in enumerate(names, 1):
            tracks.append({'Id': _id(), 'Name': name, 'AlbumId': a['Id'], 'Album': album, 'AlbumArtist': artist,
                           'Artists': [artist], 'IndexNumber': n, 'ProductionYear': year, 'Genres': [genre],
                           'RunTimeTicks': 200 * 10_000_000,
                           'Path': f'/media/Music/{artist}/{album}/{n:02d} - {name}.mp3'})
    return albums, tracks, [{'Id': i, 'Name': n} for n, i in artists.items()]


def songs_of(tracks, artist):
    return [t for t in tracks if t['AlbumArtist'] == artist]


def big_library():
    spec = []
    for i in range(12):
        spec.append((f'Goth Band {i}', f'Dark Album {i}', 1990 + i, 'Gothic Rock', [f'goth {i}-{k}' for k in range(8)]))
        spec.append((f'Jazz Trio {i}', f'Smoky Album {i}', 1960 + i, 'Jazz', [f'jazz {i}-{k}' for k in range(8)]))
    spec.append(('A Poet', 'Readings', 1980, 'Spoken Word', [f'poem {k}' for k in range(25)]))
    spec.append(('Old Show', 'Episodes', 2010, 'Podcast', [f'ep {k}' for k in range(30)]))
    return lib(spec)


def test_genre_tags_fold_into_families():
    assert families(['Gothic Rock']) == {'goth'}
    assert families(['Rock']) == {'rock'}                     # plain rock only when nothing finer
    assert families(['Progessive Rock']) == {'prog'}          # misspelled tags seen in real libraries
    assert families(['Alternative & Punk']) == {'alt', 'punk'}
    assert families(['Bluesrock']) == {'classic'}


def test_history_matches_itunes_to_jellyfin_and_adds_up_duplicates():
    albums, tracks, artists = lib([('The Band', 'First Album', 1995, 'Rock', ['Morning Song', 'Evening Song'])])
    when = datetime.datetime(2015, 2, 28, 17, 21)
    itunes = {'Tracks': {
        '1': {'Name': 'Morning Song', 'Artist': 'The Band', 'Album': 'First Album', 'Play Count': 180,
              'Play Date UTC': when, 'Location': 'file://localhost/C:/Users/me/Music/The%20Band/First%20Album/01%20-%20Morning%20Song.mp3'},
        '2': {'Name': 'Morning Song', 'Artist': 'The Band', 'Album': 'Elsewhere', 'Play Count': 5, 'Skip Count': 1,
              'Location': 'file://localhost/C:/old/copy.mp3'},              # a duplicate, matched by title + artist
        '3': {'Name': 'Not In Jellyfin', 'Artist': 'Nobody', 'Play Count': 9, 'Location': 'file://x/y.m4p'},
        '4': {'Name': 'Ep 1', 'Podcast': True, 'Play Count': 3, 'Location': 'file://x/ep.mp3'},
    }, 'Playlists': [
        {'Name': 'Library', 'Master': True, 'Playlist Items': [{'Track ID': 1}]},
        {'Name': 'Morning (SharePod)', 'Playlist Items': [{'Track ID': 1}, {'Track ID': 2}] * 3},
    ]}
    h = music_history.build(itunes, tracks)
    song = h['songs'][tracks[0]['Id']]
    assert song['plays'] == 185 and song['skips'] == 1 and song['last'].startswith('2015-02-28')
    assert len(h['songs']) == 1
    assert h['playlists'] == []            # one song, deduped: under the 5-song minimum


def test_old_favorites_are_the_most_played_songs():
    albums, tracks, artists = big_library()
    hist = {'songs': {t['Id']: {'plays': 50 + i, 'last': '2013-05-01T00:00:00'} for i, t in enumerate(tracks[:70])},
            'playlists': []}
    radio = Radio(MusicIndex(albums, tracks, artists), hist, rng=random.Random(1))
    ses = radio.session('favorites')
    assert set(ses['pool']) == {t['Id'] for t in tracks[:70]}
    assert {c['id'] for c in radio.stations()} >= {'favorites', 'year:2013', 'genre:goth', 'genre:jazz'}


def test_rules_no_repeats_artist_gap_and_never_podcasts():
    albums, tracks, artists = big_library()
    radio = Radio(MusicIndex(albums, tracks, artists), None, rng=random.Random(2))
    ses = radio.session('genre:goth')
    batch = radio.next_batch(ses, 40)
    ids = [t['Id'] for t in batch]
    assert len(ids) == len(set(ids)) == 40
    names = [t['AlbumArtist'] for t in batch]
    assert all(names[i] not in names[max(0, i - 4):i] for i in range(len(names)))
    every = {t['Id'] for t in tracks if t['AlbumArtist'] == 'Old Show'}
    for sid in [c['id'] for c in radio.stations()]:
        assert not every & set(radio.pool(sid))
    assert set(radio.pool('spoken')) == {t['Id'] for t in songs_of(tracks, 'A Poet')}


def test_a_station_plays_on_after_its_pool_runs_out():
    albums, tracks, artists = big_library()
    radio = Radio(MusicIndex(albums, tracks, artists), None, rng=random.Random(3))
    ses = radio.session('genre:jazz')               # 96 songs
    got = [t['Id'] for _ in range(6) for t in radio.next_batch(ses, 25)]
    assert len(got) == 150 and len(set(got[:96])) == 96


def test_artist_radio_leans_on_the_playlists_and_is_a_third_the_artist():
    albums, tracks, artists = big_library()
    goth0, goth5 = songs_of(tracks, 'Goth Band 0'), songs_of(tracks, 'Goth Band 5')
    playlist = {'id': 'p1', 'name': 'Night', 'tracks': [t['Id'] for t in goth0[:4] + goth5[:4]]}
    radio = Radio(MusicIndex(albums, tracks, artists), {'songs': {}, 'playlists': [playlist]}, rng=random.Random(4))
    aid = next(a['Id'] for a in artists if a['Name'] == 'Goth Band 0')
    ses = radio.session('artist:' + aid)
    assert not any(t['Id'] in ses['pool'] for t in songs_of(tracks, 'Jazz Trio 3'))    # different world
    pool = ses['pool']
    w = lambda artist: sum(pool.get(t['Id'], 0) for t in songs_of(tracks, artist))
    assert w('Goth Band 5') > w('Goth Band 6')                 # a playlist put them together
    own = w('Goth Band 0') / sum(pool.values())
    assert 0.25 < own < 0.5


def test_song_radio_opens_with_its_song():
    albums, tracks, artists = big_library()
    radio = Radio(MusicIndex(albums, tracks, artists), None, rng=random.Random(5))
    seed = songs_of(tracks, 'Goth Band 2')[3]
    ses = radio.session('song:' + seed['Id'])
    batch = radio.next_batch(ses, 5)
    assert batch[0]['Id'] == seed['Id'] and seed['Id'] not in [t['Id'] for t in batch[1:]]


def test_skip_love_and_ban():
    albums, tracks, artists = big_library()
    store = {'bans': {}, 'loves': []}
    radio = Radio(MusicIndex(albums, tracks, artists), None, store, rng=random.Random(6))
    ses = radio.session('genre:goth')
    song = songs_of(tracks, 'Goth Band 1')[0]
    radio.feedback(ses, song['Id'], 'skip')
    assert ses['artist_mult'][radio.songs[song['Id']].artist] == 0.5
    radio.feedback(ses, song['Id'], 'ban')
    assert song['Id'] not in ses['pool'] and store['bans']['genre:goth'] == [song['Id']]
    assert song['Id'] not in radio.session('genre:goth')['pool']          # kept for next time
    radio.feedback(ses, songs_of(tracks, 'Goth Band 2')[0]['Id'], 'love')
    assert store['loves'] and songs_of(tracks, 'Goth Band 2')[0]['Id'] in radio.pool('favorites')


def test_recently_played_waits_unless_the_station_would_run_dry():
    albums, tracks, artists = big_library()
    radio = Radio(MusicIndex(albums, tracks, artists), None, rng=random.Random(7))
    jazz = [t['Id'] for t in tracks if t['Genres'] == ['Jazz']]
    ses = radio.session('genre:jazz', recent=jazz[:90])
    first = [t['Id'] for t in radio.next_batch(ses, 6)]
    assert set(first) <= set(jazz[90:])
    more = radio.next_batch(ses, 10)
    assert len(more) == 10                                    # then it falls back to the rest


def test_a_second_copy_of_a_song_never_comes_up():
    albums, tracks, artists = lib([('Duo', 'Second Album', 2001, 'Alternative & Punk',
                                    ['Hotel Song', 'Hotel Song', 'Another Song']),
                                   ('Quartet', 'Live Album', 1969, 'Rock', ['Opening Number']),
                                   ('Quartet', 'Studio Album', 1967, 'Rock', ['Opening Number']),
                                   ('Noise Band', 'Live Set', 1992, 'Rock', ['(-)', '(-)'])])
    copies = [t['Id'] for t in tracks if t['Name'] == 'Hotel Song']
    hist = {'songs': {copies[1]: {'plays': 12}}, 'playlists': []}
    radio = Radio(MusicIndex(albums, tracks, artists), hist)
    music = {s.id for s in radio.music}
    assert copies[1] in music and copies[0] not in music      # the copy that was played stays
    assert sum(1 for t in tracks if t['Name'] == 'Opening Number' and t['Id'] in music) == 2   # live and studio
    assert sum(1 for t in tracks if t['Name'] == '(-)' and t['Id'] in music) == 2               # nameless: not judged


def test_skipped_songs_come_up_less_and_tv_plays_count():
    albums, tracks, artists = big_library()
    goth = [t['Id'] for t in tracks if t['Genres'] == ['Gothic Rock']]
    hist = {'songs': {goth[0]: {'plays': 10}, goth[1]: {'plays': 2, 'skips': 6}, goth[2]: {'plays': 0, 'skips': 3}},
            'playlists': []}
    radio = Radio(MusicIndex(albums, tracks, artists), hist, tv={goth[3]: (4, 0)})
    pool = radio.pool('genre:goth')
    assert pool[goth[1]] < pool[goth[0]] and pool[goth[1]] < pool[goth[4]]      # skipped: less
    assert radio.songs[goth[3]].plays == 4 and goth[3] in radio.pool('favorites')   # TV plays count
    assert goth[2] not in radio.pool('deepcuts')                                  # never played, often skipped


def test_a_station_holds_still_for_a_day():
    albums, tracks, artists = big_library()
    radio = Radio(MusicIndex(albums, tracks, artists), None)
    today = radio.daily('genre:goth', '2026-10-12')
    assert len(today) == len(set(today)) == 96               # all 96 goth songs, none twice
    assert radio.daily('genre:goth', '2026-10-12') == today  # the same list all day
    assert radio.daily('genre:goth', '2026-10-13') != today  # a new one tomorrow


def test_not_for_me_keeps_a_song_off_every_station():
    albums, tracks, artists = big_library()
    store = {'bans': {}, 'loves': []}
    radio = Radio(MusicIndex(albums, tracks, artists), None, store)
    song = [t['Id'] for t in tracks if t['Genres'] == ['Gothic Rock']][0]
    radio.feedback(None, song, 'dislike')
    assert store['dislikes'] == [song]
    assert song not in radio.session('genre:goth')['pool'] and song not in radio.daily('genre:goth', '2026-10-12')
    radio.feedback(None, song, 'undislike')
    assert store['dislikes'] == [] and song in radio.session('genre:goth')['pool']
    radio.feedback(None, song, 'love'); radio.feedback(None, song, 'unlove')
    assert store['loves'] == [] and song not in radio.loved


# --- What Music Flamingo heard: untagged CD rips join genre stations ----------------------------------------


def test_tags_know_classical_now_without_stealing_chamber_pop():
    assert families(['Classical']) == {'classical'}
    assert families(['Opera']) == {'opera'}
    assert 'classical' not in families(['Chamber Pop'])      # 'chamber music' is classical, chamber pop isn't
    assert families(['Gothic Rock']) == {'goth'}                # unchanged


def test_untagged_songs_join_stations_from_what_flamingo_heard():
    spec = [(f'Composer {i}', f'Rip {i}', 1960, '', [f'aria {i}-{k}' for k in range(8)]) for i in range(10)]
    spec += [(f'Goth Band {i}', f'Dark {i}', 1990, 'Gothic Rock', [f'goth {i}-{k}' for k in range(8)]) for i in range(10)]
    albums, tracks, artists = lib(spec)
    for t in tracks:
        if t['Genres'] == ['']:
            t['Genres'] = []                                    # CD rips: no tag at all
    rips = [t['Id'] for t in tracks if not t['Genres']]
    goths = [t['Id'] for t in tracks if t['Genres']]
    plain = Radio(MusicIndex(albums, tracks, artists), None)
    assert 'genre:opera' not in {c['id'] for c in plain.stations()}
    heard = {i: ['opera', 'classical'] for i in rips}
    heard[goths[0]] = ['opera']                                 # a tagged song keeps its own tag's family
    radio = Radio(MusicIndex(albums, tracks, artists), None, heard=heard)
    ids = {c['id'] for c in radio.stations()}
    assert {'genre:opera', 'genre:classical', 'genre:goth'} <= ids
    assert set(radio.pool('genre:opera')) == set(rips)
    assert radio.songs[goths[0]].families == {'goth'}
