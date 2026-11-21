"""Reading Music Flamingo's song descriptions. Doc: docs/radio.md.

Plain Python + numpy, so the media server can read `describe.jsonl` without
torch. `listen.py` uses `loop_start` as its loop guard.

    import descriptions
    notes = descriptions.load('describe.jsonl')        # id -> record
    descriptions.readable(notes[song_id])              # text fit to read
"""
import json
import re

import numpy as np

# A loop: one short phrase over and over. About 2 % of unguarded descriptions
# went like "Cm6, Cm7♭5, Cm6, Cm7♭5," dozens of times until the 700-token cap.
LOOP_MAX_PERIOD = 40        # tokens in the repeated phrase, at most
LOOP_MIN_REPEATS = 6        # times in a row, at least
LOOP_MIN_TOKENS = 60        # tokens in the whole run, at least


def loop_start(ids):
    """Token index where a loop begins (its first time through the phrase), or
    None. A chorus line or a "la la la" quoted once is far below the bar."""
    a = np.asarray(ids)
    best = None
    for p in range(1, LOOP_MAX_PERIOD + 1):
        need = max(LOOP_MIN_REPEATS * p, LOOP_MIN_TOKENS) - p   # a[i] == a[i+p] this many times in a row
        if len(a) - p < need:
            continue
        same = (a[p:] == a[:-p]).astype(np.int32)
        c = np.concatenate(([0], np.cumsum(same)))
        hits = np.flatnonzero(c[need:] - c[:-need] == need)
        if hits.size and (best is None or hits[0] < best):
            best = int(hits[0])
    return best


def load(path):
    """id -> record. A song described again (a loop redone) has one line, but if
    one ever had two, the later wins."""
    out = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                out[r['id']] = r
    return out


_SENTENCE_END = re.compile(r'[.!?…]["”’)\]]*(?=\s|$)')
_NOT_AN_END = {'approx', 'e.g', 'i.e', 'vs', 'ca', 'etc', 'Mr', 'Mrs', 'Dr', 'St'}


def cut_off(rec, max_new=700):
    """True when the description stopped before Music Flamingo finished it: at
    the token cap (about a quarter of descriptions at 700 tokens, mid-way through
    the closing summary)
    or at a loop it never got out of."""
    if rec.get('looped'):
        return True
    return (rec.get('gen_tokens') or 0) >= rec.get('max_new', max_new)


def readable(rec, max_new=700):
    """The description, ending on its last full sentence when it was cut off."""
    text = (rec.get('text') or '').strip()
    if not cut_off(rec, max_new):
        return text
    end = None
    for m in _SENTENCE_END.finditer(text):
        word = re.search(r'([\w.]+)[.!?…]?$', text[:m.start() + 1])
        if word and word.group(1).rstrip('.') in _NOT_AN_END:
            continue
        end = m.end()
    return text[:end].rstrip() if end else text
