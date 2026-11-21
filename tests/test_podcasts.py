"""Podcasts: the engine in hearth/podcasts.py — Apple's directory search, RSS
feeds, episodes and where each one was stopped. No network: every feed and
image here is served by httpx.MockTransport."""
import asyncio
import io
import json
import time

import httpx
import pytest

from hearth.podcasts import (FeedError, NotFound, Podcasts, parse_date, parse_duration,  # noqa: E402
                                 parse_feed, plain_text, show_id_for)

FEED = 'https://feeds.example.com/history.xml'
ART = 'https://cdn.example.com/art.png'


def rss(items, image=ART, title='Long History'):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
     xmlns:podcast="https://podcastindex.org/namespace/1.0">
<channel>
  <title>{title}</title>
  <itunes:author>A Historian</itunes:author>
  <description>&lt;p&gt;History, &lt;b&gt;hardcore&lt;/b&gt;.&lt;/p&gt;</description>
  <itunes:image href="{image}"/>
  {''.join(items)}
</channel></rss>""".encode()


def item(guid, title, date, duration='1:02:03', url=None, mime='audio/mpeg', extra=''):
    url = url or f'https://media.example.com/{guid}.mp3'
    enc = f'<enclosure url="{url}" type="{mime}" length="1"/>' if url != 'none' else ''
    return f"""<item><guid>{guid}</guid><title>{title}</title><pubDate>{date}</pubDate>
      <itunes:duration>{duration}</itunes:duration>
      <description><![CDATA[<p>Show&nbsp;notes for <a href="x">{title}</a></p><script>bad()</script>]]></description>
      {enc}{extra}</item>"""


def png_bytes(size=(900, 700)):
    from PIL import Image
    out = io.BytesIO()
    Image.new('RGBA', size, (200, 50, 120, 128)).save(out, 'PNG')
    return out.getvalue()


class Web:
    """A tiny fake internet: url → (status, body, headers); records requests."""
    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def __call__(self, request):
        self.seen.append(request)
        key = str(request.url).split('?')[0]
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404)
        if callable(route):
            return route(request)
        status, body, headers = route
        return httpx.Response(status, content=body, headers=headers or {})


def engine(tmp_path, routes):
    web = Web(routes)
    return Podcasts(str(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(web))), web


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- parsing
def test_durations():
    assert parse_duration('3600') == 3600
    assert parse_duration('62:03') == 3723
    assert parse_duration('1:02:03') == 3723
    assert parse_duration('01:02:03.6') == 3724
    assert parse_duration('') is None and parse_duration('abc') is None and parse_duration('0') is None
    assert parse_duration('1:2:3:4') is None


def test_dates():
    assert parse_date('Mon, 12 Oct 2026 10:00:00 -0700').isoformat() == '2026-10-12T17:00:00+00:00'
    assert parse_date('Mon, 12 Oct 2026 10:00:00 PDT').hour == 17
    assert parse_date('2026-10-12T17:00:00Z').hour == 17
    assert parse_date('2026-10-12').day == 12
    assert parse_date('someday') is None and parse_date('') is None


def test_summary_is_plain_text():
    s = plain_text('<p>Show&nbsp;notes&amp;more</p><script>x()</script><br/>line two')
    assert s == 'Show notes&more line two'      # &nbsp; is a plain space
    long = plain_text('word ' * 200)
    assert len(long) <= 400 and long.endswith('…')


def test_feed_parsing_skips_items_without_audio_and_keeps_transcripts():
    data = rss([
        item('ep1', 'Old one', 'Mon, 05 Oct 2026 10:00:00 +0000'),
        item('ep2', 'No audio', 'Tue, 06 Oct 2026 10:00:00 +0000', url='none'),
        item('ep3', 'A web page', 'Wed, 07 Oct 2026 10:00:00 +0000', url='https://example.com/page.html', mime='text/html'),
        item('ep4', 'New one', 'Thu, 08 Oct 2026 10:00:00 +0000', mime='', url='https://media.example.com/x.m4a?ad=1',
             extra='<podcast:transcript url="https://t.example.com/a.html" type="text/html"/>'
                   '<podcast:transcript url="https://t.example.com/a.vtt" type="text/vtt"/>'),
    ])
    show, eps = parse_feed(data, FEED)
    assert show['title'] == 'Long History' and show['author'] == 'A Historian'
    assert show['description'] == 'History, hardcore.'
    assert show['image_url'] == ART
    assert [e['title'] for e in eps] == ['New one', 'Old one']      # newest first, no audio skipped
    assert eps[0]['transcript'] == {'url': 'https://t.example.com/a.vtt', 'type': 'text/vtt'}
    assert 'transcript' not in eps[1]
    assert eps[1]['duration'] == 3723 and eps[1]['summary'] == 'Show notes for Old one'


def test_ids_are_stable():
    data = rss([item('ep1', 'One', 'Mon, 05 Oct 2026 10:00:00 +0000')])
    a = parse_feed(data, FEED)[1][0]['id']
    b = parse_feed(rss([item('ep0', 'Zero', 'Sun, 04 Oct 2026 10:00:00 +0000'),
                        item('ep1', 'One, retitled', 'Mon, 05 Oct 2026 10:00:00 +0000')]), FEED)[1][0]['id']
    assert a == b and len(a) == 16
    assert parse_feed(data, 'https://other.example.com/feed')[1][0]['id'] != a


def test_not_a_feed():
    with pytest.raises(FeedError):
        parse_feed(b'<html><body>hi</body></html>', FEED)
    with pytest.raises(FeedError):
        parse_feed(b'not xml at all', FEED)


def test_atom_feed():
    data = b"""<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Cast</title>
      <author><name>Someone</name></author>
      <entry><id>urn:1</id><title>First</title><published>2026-10-01T10:00:00Z</published>
        <link rel="enclosure" href="https://m.example.com/1.mp3" type="audio/mpeg"/></entry>
      <entry><id>urn:2</id><title>No audio</title><link href="https://example.com/2"/></entry></feed>"""
    show, eps = parse_feed(data, FEED)
    assert show['title'] == 'Atom Cast' and show['author'] == 'Someone'
    assert [e['title'] for e in eps] == ['First'] and eps[0]['date'] == '2026-10-01T10:00:00Z'


# --------------------------------------------------------------------------- the engine
def two_episode_feed():
    now = time.time()
    fmt = lambda t: time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(t))
    return rss([item('ep1', 'Older', fmt(now - 40 * 86400), duration='600'),
                item('ep2', 'Newer', fmt(now - 2 * 86400), duration='600')])


def test_subscribe_is_idempotent_and_saves_art(tmp_path):
    p, web = engine(tmp_path, {FEED: (200, two_episode_feed(), None), ART: (200, png_bytes(), None)})
    card = run(p.subscribe(FEED))
    assert card['id'] == show_id_for(FEED) and card['type'] == 'PodcastShow'
    assert card['count'] == 2 and card['new'] == 1 and card['has_art']
    from PIL import Image
    with Image.open(p.art_path(card['id'])) as im:
        assert im.format == 'JPEG' and max(im.size) == 600
    fetches = len(web.seen)
    assert run(p.subscribe(FEED))['id'] == card['id'] and len(web.seen) == fetches   # no refetch
    assert len(p.shows()) == 1
    # a fresh engine on the same folder sees the same state
    again = Podcasts(str(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(web)))
    assert [e['title'] for e in again.episodes(card['id'])] == ['Newer', 'Older']


def test_unsubscribe_leaves_nothing_behind(tmp_path):
    p, _ = engine(tmp_path, {FEED: (200, two_episode_feed(), None), ART: (200, png_bytes(), None)})
    sid = run(p.subscribe(FEED))['id']
    ep = p.episodes(sid)[0]['id']
    p.save_position(ep, 120, 600)
    assert p.unsubscribe(sid) is True
    assert p.art_path(sid) is None and p.shows() == [] and p.episode(ep) is None
    assert json.load(open(tmp_path / 'podcast_progress.json')) == {}
    assert p.unsubscribe(sid) is False
    with pytest.raises(NotFound):
        p.episodes(sid)


def test_bad_ids_are_refused(tmp_path):
    p, _ = engine(tmp_path, {})
    for bad in ('../etc', 'ABCDEF0123456789', '123', None):
        with pytest.raises(ValueError):
            p.episode(bad)
    with pytest.raises(ValueError):
        run(p.subscribe('file:///etc/passwd'))


def test_played_rule_and_resume(tmp_path):
    p, _ = engine(tmp_path, {FEED: (200, two_episode_feed(), None), ART: (404, b'', None)})
    sid = run(p.subscribe(FEED))['id']
    ep = p.episodes(sid)[0]['id']                       # 600 s long
    p.save_position(ep, 20, 600)
    assert p.episode(ep)['resume'] is None              # under 30 s: start over
    p.save_position(ep, 300, 600)
    assert p.episode(ep)['resume'] == 300 and not p.episode(ep)['played']
    p.save_position(ep, 545, 600)                        # within 60 s of the end
    assert p.episode(ep)['played'] and p.episode(ep)['resume'] is None
    assert p.shows()[0]['new'] == 0
    p.mark_played(ep, False)
    assert not p.episode(ep)['played']
    long_ep = 'f' * 16
    p.save_position(long_ep, 3500, 3600)                 # 97% of an hour is still played
    assert p.progress[long_ep]['played']


def test_positions_survive_a_feed_that_reorders_and_drops(tmp_path):
    feeds = {'body': two_episode_feed()}
    p, _ = engine(tmp_path, {FEED: lambda r: httpx.Response(200, content=feeds['body']),
                             ART: (404, b'', None)})
    sid = run(p.subscribe(FEED))['id']
    newer = next(e for e in p.episodes(sid) if e['title'] == 'Newer')
    older = next(e for e in p.episodes(sid) if e['title'] == 'Older')
    p.save_position(newer['id'], 200, 600)
    p.save_position(older['id'], 590, 600)
    now = time.time()
    fmt = lambda t: time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(t))
    # the publisher drops "Older", adds a brand-new one, and moves "Newer" down the list
    feeds['body'] = rss([item('ep3', 'Brand new', fmt(now - 3600)), item('ep2', 'Newer', fmt(now - 2 * 86400), duration='600')])
    assert run(p.refresh(force=True)) == 1
    titles = [e['title'] for e in p.episodes(sid)]
    assert titles == ['Brand new', 'Newer', 'Older']     # Older stays playable from the cache
    assert p.episode(newer['id'])['resume'] == 200
    assert p.episode(older['id'])['played']


def test_conditional_get_304(tmp_path):
    calls = []

    def feed(request):
        calls.append(dict(request.headers))
        if request.headers.get('if-none-match') == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=two_episode_feed(), headers={'ETag': '"v1"'})

    p, _ = engine(tmp_path, {FEED: feed, ART: (404, b'', None)})
    sid = run(p.subscribe(FEED))['id']
    assert run(p.refresh()) == 0 and len(calls) == 1     # not due yet: no request at all
    assert run(p.refresh(sid, force=True)) == 0
    assert calls[-1].get('if-none-match') == '"v1"' and 'hearth' in calls[-1]['user-agent'].lower()
    assert p.shows()[0]['count'] == 2


def test_search_maps_apples_directory(tmp_path):
    apple = {'resultCount': 3, 'results': [
        {'collectionId': 173001861, 'collectionName': "A Long History Podcast", 'artistName': 'A Historian',
         'feedUrl': FEED, 'artworkUrl600': 'https://is1.example.com/600.jpg', 'primaryGenreName': 'History',
         'trackCount': 74},
        {'collectionId': 2, 'collectionName': 'No feed', 'artistName': 'X'},
        {'collectionId': 3, 'collectionName': 'Other', 'artistName': 'Y', 'feedUrl': 'https://o.example.com/rss',
         'artworkUrl100': 'https://is1.example.com/100.jpg'},
    ]}
    p, web = engine(tmp_path, {'https://itunes.apple.com/search':
                               (200, json.dumps(apple).encode(), {'content-type': 'text/javascript'}),
                               FEED: (200, two_episode_feed(), None), ART: (404, b'', None)})
    run(p.subscribe(FEED))
    res = run(p.search('  long history '))
    assert [r['title'] for r in res] == ["A Long History Podcast", 'Other']
    assert res[0] == {'itunes_id': 173001861, 'title': "A Long History Podcast", 'author': 'A Historian',
                      'feed_url': FEED, 'art_url': 'https://is1.example.com/600.jpg', 'genre': 'History',
                      'episodes': 74, 'subscribed': True}
    assert res[1]['subscribed'] is False and res[1]['art_url'].endswith('100.jpg')
    q = web.seen[-1].url.params
    assert q['media'] == 'podcast' and q['entity'] == 'podcast' and q['term'] == 'long history'
    assert run(p.search('   ')) == []


def test_one_bad_feed_does_not_stop_the_others(tmp_path):
    good, bad = 'https://good.example.com/rss', 'https://bad.example.com/rss'
    state = {'bad': two_episode_feed()}
    p, _ = engine(tmp_path, {good: (200, two_episode_feed(), None), ART: (404, b'', None),
                             bad: lambda r: httpx.Response(200, content=state['bad'])})
    run(p.subscribe(good))
    bad_id = run(p.subscribe(bad))['id']
    state['bad'] = b'<html>gone</html>'
    run(p.refresh(force=True))
    cards = {c['id']: c for c in p.shows()}
    assert 'error' in cards[bad_id] and cards[bad_id]['count'] == 2      # keeps what it had
    assert 'error' not in cards[show_id_for(good)]


def test_subscribe_to_a_dead_feed_says_so(tmp_path):
    p, _ = engine(tmp_path, {})
    with pytest.raises(FeedError):
        run(p.subscribe('https://nowhere.example.com/rss'))
    assert p.shows() == []


def test_refresh_unknown_show(tmp_path):
    p, _ = engine(tmp_path, {})
    with pytest.raises(NotFound):
        run(p.refresh('0' * 16))
