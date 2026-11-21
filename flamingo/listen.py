"""Music Flamingo listens to the library: one description per song. Doc: docs/radio.md.

Runs on the GPU box, from a venv with torch + transformers (flamingo/requirements.txt).
Reads <home>/manifest.json and <home>/audio/<id>.flac (made by prepare_audio.py) and
writes, resumably:

  <home>/out/describe.jsonl     one line per song: {id, seconds_heard, prompt, text, gen_tokens,
                                temperature, max_new, tries, looped, at}
  <home>/out/emb_song/<id>.npy  the AF-Whisper encoder's mean-pooled output (1280-d, float16),
                                for "sounds like" similarity later (--no-embeddings to skip)
  <home>/out/listen.log         per-batch throughput

The description's first sentence names the genre; hearth/flamingo_genres.py folds it into
the radio's families. The rest (tempo, instruments, mood, the "suits" closing sentence)
is there for mood stations later.

Titles, artists and albums never go into the prompt: the model describes what it hears,
not what it might know about a name.

A description that falls into a loop (one short phrase over and over, ~2 % of songs
without a guard) is stopped there and asked for again with another seed, then once more
with no 12-token phrase allowed twice.

    python listen.py [--home DIR] [--model DIR_OR_HF_ID] [--deadline 06:30] [--limit N]
                     [--batch 8] [--max-windows 48] [--no-embeddings] [--cpu-smoke]
"""
import argparse
import datetime as dt
import json
import os
import random
import signal
import sys
import time

import numpy as np
import soundfile as sf
import torch
from transformers import StoppingCriteria, StoppingCriteriaList

from descriptions import LOOP_MIN_TOKENS, loop_start

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 16000
WINDOW = 30 * SR
MAX_WINDOWS = 20                  # Music Flamingo hears at most 10 minutes
SEED = 1012
SPOKEN = {'Podcast', 'Audiobook', 'Spoken Word', 'Comedy'}   # not music: skipped

DESCRIBE = ("Describe this track in full detail: its genre and style, tempo and feel, and key if you can "
            "hear it; the instruments and production; the vocals; how the song is structured and develops; "
            "the mood and how it shifts; and what the lyrics are about. Finish with one sentence on the "
            "moments or settings this music would suit.")

STOP = False
OUT = None


def _term(*_):
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, _term)
signal.signal(signal.SIGINT, _term)


def log(msg):
    line = f'{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
    print(line, flush=True)
    with open(os.path.join(OUT, 'listen.log'), 'a') as f:
        f.write(line + '\n')


def deadline_at(hhmm):
    if not hhmm:
        return None
    now = dt.datetime.now()
    h, m = map(int, hhmm.split(':'))
    d = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return d if d > now else d + dt.timedelta(days=1)


def done_ids(path):
    out = set()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    out.add(json.loads(line)['id'])
                except (ValueError, KeyError):
                    pass
    return out


def eligible_songs(home):
    with open(os.path.join(home, 'manifest.json'), encoding='utf-8') as f:
        manifest = json.load(f)
    songs = [t for t in manifest
             if not (set(t.get('genres') or []) & SPOKEN)
             and (t.get('duration') or 0) >= 5
             and os.path.exists(os.path.join(home, 'audio', t['id'] + '.flac'))]
    random.Random(SEED).shuffle(songs)        # a partial night covers a random slice of the library
    return songs


def load_audio(home, song_id):
    a, sr = sf.read(os.path.join(home, 'audio', song_id + '.flac'), dtype='float32')
    if a.ndim > 1:
        a = a.mean(axis=1)
    assert sr == SR, sr
    return a[:MAX_WINDOWS * WINDOW]


class LoopGuard(StoppingCriteria):
    """Stops each sequence that has fallen into a loop instead of letting it repeat itself to max_new (and
    hold its whole batch that long). Checks every 8 tokens."""
    EVERY = 8
    WINDOW = 320

    def __init__(self, prompt_len, pad):
        self.prompt_len, self.pad, self.calls = prompt_len, pad, 0

    def __call__(self, input_ids, scores, **kwargs):
        self.calls += 1
        done = torch.zeros(input_ids.shape[0], dtype=torch.bool, device=input_ids.device)
        if self.calls % self.EVERY:
            return done
        for r, row in enumerate(input_ids[:, self.prompt_len:].cpu().numpy()):
            row = row[row != self.pad][-self.WINDOW:]
            if len(row) >= LOOP_MIN_TOKENS and loop_start(row) is not None:
                done[r] = True
        return done


class Listener:
    def __init__(self, model_path, device, embeddings=True):
        from transformers import AudioFlamingo3ForConditionalGeneration, AutoProcessor
        self.device = device
        self.shrink = False           # set on CUDA OOM; the stage loop shrinks its batches
        self.proc = AutoProcessor.from_pretrained(model_path)
        self.model = AudioFlamingo3ForConditionalGeneration.from_pretrained(
            model_path, dtype=torch.bfloat16, low_cpu_mem_usage=True, attn_implementation='sdpa').to(device).eval()
        self._enc = None
        if embeddings:
            self.model.model.audio_tower.register_forward_hook(self._capture, with_kwargs=True)

    def _capture(self, module, args, kwargs, output):
        mask = kwargs.get('input_features_mask')
        if mask is None and len(args) > 1:
            mask = args[1]
        hs = output.last_hidden_state.float()                     # (windows, 750, 1280)
        _, post = module._get_feat_extract_output_lengths(mask.sum(-1).to(torch.long))
        valid = torch.arange(hs.shape[1], device=hs.device)[None, :] < post[:, None]
        sums = (hs * valid[..., None]).sum(1)                      # (windows, 1280)
        self._enc = (sums.cpu(), valid.sum(1).cpu())

    def safe_generate(self, audios, max_new, temperature, **extra):
        """generate(), halving the batch on CUDA out-of-memory. An item that fails alone comes back as text ''
        with gen_tokens -1 (recorded, so it isn't retried every night)."""
        try:
            return self.generate(audios, max_new, temperature, **extra)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            self.shrink = True
            if len(audios) == 1:
                log(f'OOM on a single song ({len(audios[0]) / SR:.0f} s of audio); skipped')
                return [''], [-1], [None], [False]
            h = len(audios) // 2
            log(f'OOM at batch {len(audios)}; splitting')
            t1, n1, e1, l1 = self.safe_generate(audios[:h], max_new, temperature, **extra)
            t2, n2, e2, l2 = self.safe_generate(audios[h:], max_new, temperature, **extra)
            return t1 + t2, n1 + n2, e1 + e2, l1 + l2

    def generate(self, audios, max_new, temperature, **extra):
        """One description per audio (float32 16 kHz). Returns texts, token counts, one mean-pooled encoder
        embedding per item (or None), and whether each fell into a loop (its text then ends where it began)."""
        text = self.proc.apply_chat_template(
            [{'role': 'user', 'content': [{'type': 'audio'}, {'type': 'text', 'text': DESCRIBE}]}],
            tokenize=False, add_generation_prompt=True)
        # No padding=True here: it overrides the audio default too ('max_length', 30 s), and a batch of only
        # songs under 30 s then pads to the longest and crashes the encoder. Text pads by default.
        inputs = self.proc(text=[text] * len(audios), audio=audios, return_tensors='pt').to(self.device)
        inputs['input_features'] = inputs['input_features'].to(torch.bfloat16)
        self._enc = None
        prompt_len, pad = inputs['input_ids'].shape[1], self.proc.tokenizer.pad_token_id
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new, do_sample=True,
                                      temperature=temperature, top_p=0.9,
                                      stopping_criteria=StoppingCriteriaList([LoopGuard(prompt_len, pad)]),
                                      **extra)
        texts, n_tok, looped = [], [], []
        for g in out[:, prompt_len:].cpu().numpy():
            ids = [int(t) for t in g if t != pad]
            start = loop_start(ids)
            n_tok.append(len(ids))
            looped.append(start is not None)
            texts.append(self.proc.batch_decode([ids if start is None else ids[:start]],
                                                skip_special_tokens=True)[0].strip())
        embs = [None] * len(audios)
        if self._enc is not None:
            embs = []
            sums, counts = self._enc
            per = [min(MAX_WINDOWS, max(1, -(-len(a) // WINDOW))) for a in audios]
            i = 0
            for n in per:
                embs.append((sums[i:i + n].sum(0) / counts[i:i + n].sum().clamp(min=1)).numpy().astype(np.float16))
                i += n
        return texts, n_tok, embs, looped


def batches_by_windows(songs, audio_of, max_items, max_windows):
    """Group songs into batches of similar length (less padding)."""
    songs = sorted(songs, key=lambda s: len(audio_of[s['id']]))
    batch, wins = [], 0
    for s in songs:
        w = min(MAX_WINDOWS, max(1, -(-len(audio_of[s['id']]) // WINDOW)))
        if batch and (len(batch) >= max_items or wins + w > max_windows):
            yield batch
            batch, wins = [], 0
        batch.append(s)
        wins += w
    if batch:
        yield batch


def past_deadline(deadline, est):
    return bool(deadline and est and dt.datetime.now() + dt.timedelta(seconds=est * 1.5) > deadline)


def describe_items(L, audios, a, seed):
    torch.manual_seed(seed)
    texts, ntok, embs, looped = L.safe_generate(audios, a.max_new, a.temperature)
    tries = [1] * len(audios)
    for attempt, extra in ((2, {}), (3, {'no_repeat_ngram_size': 12})):
        again = [j for j, lp in enumerate(looped) if lp]
        if not again:
            break
        torch.manual_seed(seed + 7919 * attempt)
        t2, n2, _, l2 = L.safe_generate([audios[j] for j in again], a.max_new, a.temperature, **extra)
        for k, j in enumerate(again):
            texts[j], ntok[j], looped[j], tries[j] = t2[k], n2[k], l2[k], attempt
    return texts, ntok, embs, tries, looped


def run(L, home, songs, a, deadline):
    path = os.path.join(OUT, 'describe.jsonl')
    have = done_ids(path)
    todo = [s for s in songs if s['id'] not in have]
    log(f'describe: {len(have)} done, {len(todo)} to go')
    if not a.no_embeddings:
        os.makedirs(os.path.join(OUT, 'emb_song'), exist_ok=True)
    t_start, n_done, est = time.time(), 0, None
    for i in range(0, len(todo), a.chunk):
        group = todo[i:i + a.chunk]
        audio_of = {s['id']: load_audio(home, s['id']) for s in group}
        if a.cpu_smoke:
            audio_of = {k: v[:4 * WINDOW] for k, v in audio_of.items()}
        for batch in batches_by_windows(group, audio_of, a.batch, a.max_windows):
            if STOP or past_deadline(deadline, est):
                log(f'describe: stopping ({"signal" if STOP else "deadline"}); {n_done} songs this run')
                return False
            t0 = time.time()
            texts, ntok, embs, tries, looped = describe_items(L, [audio_of[s['id']] for s in batch], a, SEED + i)
            took = time.time() - t0
            est = took if est is None else 0.7 * est + 0.3 * took
            with open(path, 'a', encoding='utf-8') as f:
                for s, t, n, e, tr, lp in zip(batch, texts, ntok, embs, tries, looped):
                    if e is not None and not a.no_embeddings:
                        np.save(os.path.join(OUT, 'emb_song', s['id'] + '.npy'), e)
                    f.write(json.dumps({'id': s['id'], 'seconds_heard': round(len(audio_of[s['id']]) / SR, 1),
                                        'prompt': DESCRIBE, 'text': t, 'gen_tokens': n,
                                        'temperature': a.temperature, 'max_new': a.max_new, 'tries': tr,
                                        'looped': lp, 'at': f'{dt.datetime.now():%Y-%m-%d %H:%M:%S}'},
                                       ensure_ascii=False) + '\n')
            n_done += len(batch)
            rate = (time.time() - t_start) / n_done
            again = sum(tr > 1 for tr in tries)
            log(f'describe: batch of {len(batch)} in {took:.1f}s ({took / len(batch):.2f} s/song); '
                f'{len(have) + n_done}/{len(songs)} total; run avg {rate:.2f} s/song'
                + (f'; {again} looped and were asked again ({sum(looped)} still looped)' if again else ''))
            if L.shrink:
                L.shrink = False
                a.batch, a.max_windows = max(1, a.batch * 2 // 3), max(MAX_WINDOWS, a.max_windows * 2 // 3)
                log(f'out of memory earlier; batches now <= {a.batch} songs / {a.max_windows} windows')
            if a.limit and n_done >= a.limit:
                return False
    return True


def main():
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument('--home', default=HERE, help='dir with manifest.json and audio/ (default: this folder)')
    ap.add_argument('--model', default=os.getenv('FLAMINGO_MODEL', 'nvidia/music-flamingo-hf'),
                    help='local dir or Hugging Face id of Music Flamingo (transformers format)')
    ap.add_argument('--deadline', default='', help='HH:MM: no batch starts that would run past it')
    ap.add_argument('--limit', type=int, default=0, help='stop after N songs (tests)')
    ap.add_argument('--ids', default='', help='comma-separated song ids to run (tests)')
    ap.add_argument('--batch', type=int, default=8, help='max songs per batch')
    ap.add_argument('--max-windows', type=int, default=48, help='max 30 s windows per batch')
    ap.add_argument('--chunk', type=int, default=64, help='songs loaded and length-sorted at a time')
    ap.add_argument('--max-new', type=int, default=700)
    ap.add_argument('--temperature', type=float, default=0.5)
    ap.add_argument('--no-embeddings', action='store_true')
    ap.add_argument('--cpu-smoke', action='store_true', help='CPU test: first 2 min of each song only')
    a = ap.parse_args()
    home = os.path.abspath(os.path.expanduser(a.home))
    OUT = os.path.join(home, 'out')
    os.makedirs(OUT, exist_ok=True)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    if a.cpu_smoke:
        assert device == 'cpu', 'the smoke test must run with CUDA_VISIBLE_DEVICES=""'
        torch.set_num_threads(os.cpu_count())
    elif device == 'cpu':
        sys.exit('No CUDA GPU found (use --cpu-smoke for a tiny CPU test).')
    deadline = deadline_at(a.deadline)
    songs = eligible_songs(home)
    if a.ids:
        want = set(a.ids.split(','))
        songs = [s for s in songs if s['id'] in want]
    log(f'start: device={device} deadline={deadline} eligible songs={len(songs)}')
    t0 = time.time()
    L = Listener(a.model, device, embeddings=not a.no_embeddings)
    log(f'model loaded in {time.time() - t0:.0f}s')
    complete = run(L, home, songs, a, deadline)
    if complete and not a.limit and not a.ids:
        log(f'PASS COMPLETE: every eligible song ({len(songs)}) has a description')
    log('exit')


if __name__ == '__main__':
    sys.exit(main())
