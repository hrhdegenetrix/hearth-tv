"""Files dropped straight into the media share reach Jellyfin.

Copying a pile of old movies onto the share beats uploading them one at a time
from the phone, but Jellyfin's realtime monitor gets no change events over a
CIFS mount, so the server asks for a scan once the folders changed and then
held still for a round (the copy finished).
"""
import asyncio

import pytest

from hearth import server

M = '/mnt/media/Movies/'


def _run(monkeypatch, snapshots):
    """Feed _nas_watch one snapshot per round; return the rounds that scanned."""
    feed = iter(snapshots)
    rounds, scans = {'n': 0}, []

    def snap():
        try:
            return next(feed)
        except StopIteration:
            raise asyncio.CancelledError
    real_sleep = asyncio.sleep

    async def sleep(_seconds):
        rounds['n'] += 1
        await real_sleep(0)

    async def refresh():
        scans.append(rounds['n'])
    monkeypatch.setattr(server, '_library_snapshot', snap)
    monkeypatch.setattr(server, '_jf_refresh', refresh)
    monkeypatch.setattr(server.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(server._nas_watch())
    return scans


def test_a_copy_is_scanned_once_it_stops_growing(monkeypatch):
    a = {M + 'Old.mkv': (10, 1)}
    growing = [a,
               {**a, M + 'New (1999).mkv': (100, 2)},
               {**a, M + 'New (1999).mkv': (900, 3)},
               {**a, M + 'New (1999).mkv': (1000, 4)},
               {**a, M + 'New (1999).mkv': (1000, 4)},
               {**a, M + 'New (1999).mkv': (1000, 4)}]
    assert _run(monkeypatch, growing) == [4]


def test_nothing_new_no_scan(monkeypatch):
    a = {M + 'Old.mkv': (10, 1)}
    assert _run(monkeypatch, [a, a, a, a]) == []


def test_a_share_that_drops_never_empties_the_library(monkeypatch):
    a = {M + 'Old.mkv': (10, 1)}
    assert _run(monkeypatch, [a, None, {}, {}, a, a]) == []


def test_music_folders_are_watched_by_folder(tmp_path, monkeypatch):
    album = tmp_path / 'Music' / 'F00'
    album.mkdir(parents=True)
    (album / 'ABCD.m4a').write_bytes(b'x')
    (album / 'EFGH.mp3').write_bytes(b'x')
    (album / 'cover.jpg').write_bytes(b'x')
    (tmp_path / 'Movies').mkdir()
    (tmp_path / 'Movies' / 'New (1999).mkv').write_bytes(b'xyz')
    monkeypatch.setattr(server, 'LIBRARY', tmp_path)
    snap = server._library_snapshot()
    assert snap[str(album) + '/'][0] == 2                     # two tracks, the cover doesn't count
    assert snap[str(tmp_path / 'Movies' / 'New (1999).mkv')][0] == 3
    assert not any(k.endswith('.m4a') or k.endswith('.mp3') for k in snap)   # no per-track stats
