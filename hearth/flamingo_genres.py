"""Genres from what Music Flamingo heard, for songs with no genre tag. Doc: docs/radio.md.

The radio's genre stations are keyed on Jellyfin's Genres tags (radio.FAMILIES). Rips
and downloads often come with no tag at all, so those songs can only reach the decade
stations and artist/song radio. Music Flamingo (flamingo/listen.py) writes a description
of every song, and its first sentence names the genre: "This track is a dramatic
Classical Opera aria, rooted in…". This module reads that first sentence, folds its head
phrase (the part before "that / which / rooted / blending / with…") into the radio's
families, and keeps the result in hearth/data/flamingo_genres.json:

    {jellyfin item id: {"phrase": "dramatic classical opera aria", "families": ["opera", "classical"]}}

radio.py uses these families only for a song whose own tags give none. The music files'
tags are never touched.

Where the descriptions come from (.env):
  FLAMINGO_DESCRIPTIONS   local path to describe.jsonl (default flamingo/out/describe.jsonl)
  FLAMINGO_SSH            user@gpu-host:~/path/to/describe.jsonl (read over ssh instead;
                          only each description's first sentence comes over)

  python -m hearth.flamingo_genres refresh      # rebuild the sidecar (run daily from cron)
  python -m hearth.flamingo_genres stats        # family counts in the saved sidecar
  python -m hearth.flamingo_genres unmatched    # head phrases that landed in no family
"""
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PATH = os.path.join(HERE, 'data', 'flamingo_genres.json')
DEFAULT_DESCRIPTIONS = os.path.join(REPO, 'flamingo', 'out', 'describe.jsonl')

# (family key, pattern on the lowercased head phrase), most specific first; keys match radio.FAMILIES
SOUND_FAMILIES = [
    ('sacred', r'sacred|choral|chant|liturg|requiem|\bmass\b|hymn|gregorian|motet|oratorio|cantata|gospel'
               r'|worship|christian|devotional'),
    ('opera', r'opera|\baria\b|\blied\b|art song|operetta|bel canto|recitative'),
    ('classical', r'classical|baroque|orchestral|symphon|chamber|concerto|sonata|string quartet|harpsichord'
                  r'|romantic.era|neoclassical|solo piano|piano solo|classical guitar|minimalist composition'),
    ('holiday', r'christmas|holiday'),
    ('goth', r'goth|industrial|darkwave|deathrock|\bebm\b|coldwave'),
    ('punk', r'punk|no wave|grunge|noise rock|hardcore'),
    ('experimental', r'avant|experimental|noise|drone|minimalism|post-rock|math rock|musique concr'),
    ('prog', r'prog|psych|art rock|krautrock'),
    ('alt', r'alternative|indie|lo-fi|new wave|noise pop|dream pop|shoegaze|britpop|jangle'),
    ('classic', r'classic rock|classic-rock|rock (and|&|\'n\'?) roll|hard rock|blues[- ]?rock|metal|heartland rock'
                r'|southern rock|arena rock|garage rock|rockabilly'),
    ('jazz', r'jazz|(?<!rhythm and )(?<!rhythm & )\bblues\b(?![- ]?rock)|swing|big band|lounge|easy listening|bebop'),
    ('oldies', r'oldies|\bsoul\b|r&b|rhythm (and|&) blues|motown|doo.?wop|girl group|northern soul'),
    ('electronic', r'electronic|trip.?hop|dance|house|techno|drum|ambient|synth|electro|downtempo|idm'),
    ('hiphop', r'hip.?hop|\brap\b|gangsta|boom.?bap'),
    ('folk', r'folk|acoustic|singer.songwriter|country|bluegrass|americana|western swing'),
    ('reggae', r'reggae|\bska\b|\bdub\b|afrobeat|afro.funk|highlife|calypso|cumbia|salsa|samba|flamenco|celtic'
               r'|world music|mbalax|zouk'),
    ('pop', r'\bpop\b|power pop|bubblegum|adult.contemporary|soft rock'),
    ('soundtrack', r'soundtrack|film score|film-score|cinematic|musical theat|show tune|cabaret|\bscore\b'),
]
_SOUND_RE = [(k, re.compile(p)) for k, p in SOUND_FAMILIES]
_HEAD_CUT = re.compile(r'\b(that|which|rooted|blending|blends|with|featuring|characteri[sz]ed|leaning|leans|'
                       r'built|driven|set|in the vein|reminiscent)\b')
_OPEN = re.compile(r'^\s*this (track|piece|song|recording|clip|selection|composition|work|performance) is (an? |the )?',
                   re.I)


def head_phrase(text):
    """'This track is a dramatic Classical Opera aria, rooted in…' -> ('dramatic classical opera aria', first sentence)."""
    t = (text or '').replace('‑', '-').replace('‐', '-').replace(' ', ' ').replace(' ', ' ')
    first = re.split(r'(?<=[.!?])\s', t.strip(), maxsplit=1)[0]
    first = _OPEN.sub('', first).lower()
    return _HEAD_CUT.split(first, maxsplit=1)[0].strip(' ,;:-')[:120], first[:240]


def sentence_families(text):
    """The families of a description: those named in its head phrase; if none, the earliest one in the first
    sentence; plain 'rock' only when nothing finer was named. At most two."""
    head, first = head_phrase(text)
    out = [k for k, rx in _SOUND_RE if rx.search(head)]
    if not out:
        hits = sorted((m.start(), k) for k, rx in _SOUND_RE for m in [rx.search(first)] if m)
        out = [hits[0][1]] if hits else []
    if not out and re.search(r'\brock\b', first):
        out = ['rock']
    return out[:2]


def _first_sentence(text):
    return re.split(r'(?<=[.!?])\s', (text or '').strip(), maxsplit=1)[0][:400]


# Runs on the GPU box over ssh: prints {id, first} per description, so only first sentences travel.
_REMOTE_READER = r'''
import json, os, re, sys
for line in open(os.path.expanduser(sys.argv[1]), encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    r = json.loads(line)
    t = r.get("text") or ""
    if r.get("id") and t:
        print(json.dumps({"id": r["id"], "first": re.split(r"(?<=[.!?])\s", t.strip(), maxsplit=1)[0][:400]}))
'''


def _read_local(path):
    out = []
    with open(os.path.expanduser(path), encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get('id') and r.get('text'):
                out.append({'id': r['id'], 'first': _first_sentence(r['text'])})
    return out


def _read_ssh(spec, timeout):
    host, _, path = spec.partition(':')
    if not host or not path:
        raise ValueError('FLAMINGO_SSH must look like user@host:~/path/describe.jsonl')
    res = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host,
                          'python3 - ' + shlex.quote(path)],
                         input=_REMOTE_READER, capture_output=True, text=True, timeout=timeout, check=True)
    return [json.loads(line) for line in res.stdout.splitlines() if line.strip()]


def _previous_count():
    try:
        with open(PATH) as f:
            return len(json.load(f))
    except (OSError, ValueError):
        return 0


def refresh(timeout=300):
    """Rebuild data/flamingo_genres.json from the descriptions. Refuses to replace the saved file with one
    less than half its size (a truncated read), so a bad night never empties the genre stations."""
    spec = os.getenv('FLAMINGO_SSH', '').strip()
    rows = _read_ssh(spec, timeout) if spec else _read_local(os.getenv('FLAMINGO_DESCRIPTIONS') or DEFAULT_DESCRIPTIONS)
    out = {r['id']: {'phrase': head_phrase(r['first'])[0], 'families': sentence_families(r['first'])} for r in rows}
    before = _previous_count()
    if not out or len(out) < before / 2:
        raise RuntimeError(f'only {len(out)} descriptions came back (the saved file has {before}); keeping it')
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH + '.tmp', 'w') as f:
        json.dump(out, f)
    os.replace(PATH + '.tmp', PATH)
    return out


_CACHE = {'mtime': None, 'data': {}}


def load():
    """{song id: [families]} from the saved file ({} if there is none), re-read when it changes."""
    try:
        mtime = os.path.getmtime(PATH)
    except OSError:
        return {}
    if _CACHE['mtime'] != mtime:
        with open(PATH) as f:
            raw = json.load(f)
        _CACHE.update(mtime=mtime, data={k: v.get('families') or [] for k, v in raw.items()})
    return _CACHE['data']


def mtime():
    try:
        return os.path.getmtime(PATH)
    except OSError:
        return None


def _main(argv):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, '.env'))
    except ImportError:
        pass
    cmd = argv[1] if len(argv) > 1 else 'stats'
    if cmd == 'refresh':
        data = refresh()
        c = Counter(f for v in data.values() for f in v['families'])
        none = sum(1 for v in data.values() if not v['families'])
        print(f'{len(data)} songs, {none} without a family; {c.most_common()}')
    elif cmd == 'unmatched':
        with open(PATH) as f:
            raw = json.load(f)
        c = Counter(v['phrase'] for v in raw.values() if not v.get('families'))
        for phrase, n in c.most_common(60):
            print(f'{n:5d}  {phrase}')
    else:
        data = load()
        c = Counter(f for v in data.values() for f in v)
        print(f'{len(data)} songs; {c.most_common()}')


if __name__ == '__main__':
    _main(sys.argv)
