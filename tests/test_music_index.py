"""The TV's A-to-Z music browsing (hearth/music_index.py).

A shelf of the 40 newest albums can't reach the rest of a big library, so the
TV has Albums / Artists A to Z; and Jellyfin's one-album-per-folder splits
compilations that a rip filed song by song under each artist's own folder.
"""
from hearth.music_index import MusicIndex, artist_key, letter, sort_key  # noqa: E402

_n = [0]


def _id():
    _n[0] += 1
    return '%032x' % _n[0]


def album(name, artist, artist_id=None, image=True, added='2026-10-10T20:00:00Z'):
    return {'Id': _id(), 'Name': name, 'AlbumArtist': artist, 'ProductionYear': 2005, 'DateCreated': added,
            'AlbumArtists': [{'Id': artist_id or _id(), 'Name': artist}],
            'ImageTags': {'Primary': 'x'} if image else {}}


def songs(alb, numbers, disc=None):
    return [{'Id': _id(), 'Name': f"{alb['Name']} {n}", 'AlbumId': alb['Id'], 'Album': alb['Name'],
             'AlbumArtist': alb['AlbumArtist'], 'IndexNumber': n, 'ParentIndexNumber': disc} for n in numbers]


def loose(name, artist, numbers):
    return [{'Id': _id(), 'Name': f'{name} {n}', 'Album': name, 'AlbumArtist': artist, 'IndexNumber': n}
            for n in numbers]


def by_name(index, name):
    return [g for g in index.groups if g['name'] == name]


def test_a_scattered_compilation_is_one_album_in_track_order():
    folders = [album('Soundtrack: Music From The Series', a) for a in ('Singer A', 'Group B', 'Singer C')]
    tracks = songs(folders[0], [3]) + songs(folders[1], [1, 4]) + songs(folders[2], [2])
    ix = MusicIndex(folders, tracks, [])
    comp, = by_name(ix, 'Soundtrack: Music From The Series')
    assert comp['artist'] == 'Various artists' and comp['count'] == 4 and len(comp['ids']) == 3
    assert [t['IndexNumber'] for t in ix.songs(comp['ids'])] == [1, 2, 3, 4]


def test_two_whole_albums_with_one_name_stay_apart():
    one, two, three = album('Greatest Hits', 'Band One'), album('Greatest Hits', 'Band Two'), album('Greatest Hits', 'Band Three')
    ix = MusicIndex([one, two, three], songs(one, range(1, 21)) + songs(two, range(1, 13)) + songs(three, [5]), [])
    assert sorted(g['artist'] for g in by_name(ix, 'Greatest Hits')) == ['Band One', 'Band Three', 'Band Two']


def test_one_artists_two_discs_in_two_folders_are_one_album():
    d1, d2 = album('Live At The Hall', 'The Live Band'), album('Live At The Hall', 'The Live Band')
    ix = MusicIndex([d1, d2], songs(d1, range(1, 12), disc=1) + songs(d2, range(1, 12), disc=2), [])
    g, = by_name(ix, 'Live At The Hall')
    assert g['count'] == 22 and g['artist'] == 'The Live Band'


def test_unknown_album_is_never_merged():
    a, b = album('Unknown Album', 'Artist X'), album('Unknown Album', 'Artist Y')
    ix = MusicIndex([a, b], songs(a, [1]) + songs(b, [1]), [])
    assert len(by_name(ix, 'Unknown Album')) == 2


def test_loose_songs_get_a_card_unless_they_copy_an_album():
    quiet = album('Long Quiet Album', 'Loud Band')
    tracks = songs(quiet, range(1, 9)) + loose('Long Quiet Album', 'Loud Band', range(1, 9)) \
        + loose('Demos', 'Solo Singer', [1, 2])
    ix = MusicIndex([quiet], tracks, [])
    assert len(by_name(ix, 'Long Quiet Album')) == 1
    demos, = by_name(ix, 'Demos')
    assert demos['ids'] == [] and len(demos['songs']) == 2
    assert [t['Name'] for t in ix.songs(demos['ids'], demos['songs'])] == ['Demos 1', 'Demos 2']
    assert len(ix.tracks) == 18          # Shuffle all still has every song


def test_an_artist_page_plays_only_their_songs_on_a_compilation():
    singer_id = _id()
    own = album('Own Album', 'Singer A', singer_id)
    comp = [album('Soundtrack', 'Singer A', singer_id), album('Soundtrack', 'Group B')]
    ix = MusicIndex([own] + comp, songs(own, range(1, 12)) + songs(comp[0], [7]) + songs(comp[1], [1, 2]),
                    [{'Id': singer_id, 'Name': 'Singer A'}])
    cards = ix.artist_albums(singer_id)
    assert [(c['name'], c['count']) for c in cards] == [('Own Album', 11), ('Soundtrack', 1)]
    assert cards[1]['ids'] == [comp[0]['Id']]
    assert by_name(ix, 'Soundtrack')[0]['count'] == 3     # the album itself is still whole


def test_cards_and_letters():
    a = album('The Big Film', 'Various')
    ix = MusicIndex([a], songs(a, [1]), [{'Id': _id(), 'Name': 'The Stripes'}, {'Id': _id(), 'Name': 'ABBA'}])
    card = ix.albums()[0]
    assert card['letter'] == 'B' and not any(k.startswith('_') for k in card) and 'sort' not in card
    assert [x['name'] for x in ix.artists] == ['ABBA', 'The Stripes']
    assert letter('(500) Days of Summer') == '#' and letter('The 88') == '#' and letter('Ölüm') == 'O'
    assert sort_key('Track 9') < sort_key('Track 10')
    assert artist_key('AC-DC') == artist_key('AC/DC') and artist_key('Estés') == artist_key('Estes')


def test_recently_added_counts_a_compilation_once():
    folders = [album('Series OST', a, added='2026-10-10T21:0%d:00Z' % i) for i, a in enumerate('ABC')]
    older = album('Closing Time', 'Bar Singer', added='2026-10-10T19:00:00Z')
    tracks = [t for f in folders for t in songs(f, [1])] + songs(older, [1, 2])
    ix = MusicIndex(folders + [older], tracks, [])
    assert [c['name'] for c in ix.recent(40)] == ['Series OST', 'Closing Time']
