"""Music Flamingo pieces that run without the model: the genre fold (hearth/flamingo_genres.py) and the
listening loop (flamingo/listen.py) with a stand-in listener."""
import json
import os
import sys
import types

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from hearth import flamingo_genres as fg  # noqa: E402


def test_head_phrase_and_families():
    head, _ = fg.head_phrase('This track is a dramatic Classical Opera aria, rooted in the bel canto tradition.')
    assert head == 'dramatic classical opera aria'
    assert fg.sentence_families('This track is a dramatic Classical Opera aria, rooted in…') == ['opera', 'classical']
    assert fg.sentence_families('This piece is a Gregorian chant sung by a male choir.') == ['sacred']
    assert fg.sentence_families('This track is an upbeat roots reggae song with a skanking guitar.') == ['reggae']
    # nothing finer named: plain rock
    assert fg.sentence_families('This song is a loud rock number built on two guitars.') == ['rock']


def _write(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r) + '\n')


def test_refresh_from_a_local_file_and_the_half_size_guard(tmp_path, monkeypatch):
    desc = tmp_path / 'describe.jsonl'
    rows = [{'id': f'{i:032x}', 'text': 'This track is a mellow jazz ballad with brushed drums. It suits rain.'}
            for i in range(10)]
    _write(desc, rows)
    monkeypatch.setattr(fg, 'PATH', str(tmp_path / 'data' / 'flamingo_genres.json'))
    monkeypatch.setenv('FLAMINGO_DESCRIPTIONS', str(desc))
    monkeypatch.delenv('FLAMINGO_SSH', raising=False)
    out = fg.refresh()
    assert len(out) == 10 and out[rows[0]['id']]['families'] == ['jazz']
    assert fg.load()[rows[0]['id']] == ['jazz']
    _write(desc, rows[:3])                      # a truncated read never replaces a good file
    with pytest.raises(RuntimeError):
        fg.refresh()
    assert len(json.load(open(fg.PATH))) == 10


def test_listen_loop_with_a_stand_in_model(tmp_path, monkeypatch):
    np = pytest.importorskip('numpy')
    sf = pytest.importorskip('soundfile')
    pytest.importorskip('torch')
    pytest.importorskip('transformers')
    sys.path.insert(0, os.path.join(REPO, 'flamingo'))
    import listen

    home = tmp_path
    (home / 'audio').mkdir()
    manifest = []
    for i, secs in enumerate([3, 12, 40, 75]):
        sid = f'{i:032x}'
        sf.write(home / 'audio' / f'{sid}.flac', np.zeros(int(secs * listen.SR), dtype='float32'), listen.SR)
        manifest.append({'id': sid, 'duration': secs, 'genres': []})
    manifest.append({'id': 'f' * 32, 'duration': 60, 'genres': ['Audiobook']})     # spoken: skipped
    (home / 'manifest.json').write_text(json.dumps(manifest))

    class FakeListener:
        shrink = False

        def safe_generate(self, audios, max_new, temperature, **extra):
            return (['This track is a quiet ambient piece.'] * len(audios), [12] * len(audios),
                    [np.ones(1280, dtype='float16')] * len(audios), [False] * len(audios))

    monkeypatch.setattr(listen, 'OUT', str(home / 'out'))
    os.makedirs(listen.OUT)
    args = types.SimpleNamespace(chunk=64, batch=8, max_windows=48, max_new=700, temperature=0.5,
                                 no_embeddings=False, cpu_smoke=False, limit=0)
    songs = listen.eligible_songs(str(home))
    assert len(songs) == 3                      # the 3 s song (< 5 s) and the audiobook are left out
    assert listen.run(FakeListener(), str(home), songs, args, None) is True
    recs = [json.loads(line) for line in open(home / 'out' / 'describe.jsonl')]
    assert sorted(r['id'] for r in recs) == sorted(s['id'] for s in songs)
    assert all(os.path.exists(home / 'out' / 'emb_song' / f"{r['id']}.npy") for r in recs)
    assert listen.run(FakeListener(), str(home), songs, args, None) is True      # resumable: nothing new
    assert len(open(home / 'out' / 'describe.jsonl').readlines()) == 3
