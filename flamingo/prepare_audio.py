"""Decode the Jellyfin music library to 16 kHz mono FLAC for Music Flamingo. Doc: docs/radio.md.

Lists every Audio item in Jellyfin's Music library (with its file path), maps the
container's path to this machine's (TV_JELLYFIN_MEDIA_ROOT -> TV_MEDIA_ROOT), and decodes
the first 600 s of each song (Music Flamingo hears at most 10 minutes) to
<audio dir>/<jellyfin item id>.flac. Low priority (nice 19, idle I/O), a few at a time.
Resumable: songs already decoded are skipped. Also writes manifest.json beside the audio
(id, name, artist, album, year, genres, duration) for bookkeeping; names never go into a
Music Flamingo prompt.

When the GPU is on another machine, --remote rsyncs each batch there and deletes the local
copy, so the media server never holds the whole decoded library.

    python3 flamingo/prepare_audio.py                                    # into flamingo/audio/
    python3 flamingo/prepare_audio.py --remote user@gpu-box:~/flamingo   # straight to the GPU box

Reads .env at the repo root: JELLYFIN_URL, JELLYFIN_API_KEY, JELLYFIN_USER,
TV_MEDIA_ROOT, TV_JELLYFIN_MEDIA_ROOT.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MAX_SECONDS = 600


def env(name, default=''):
    if os.getenv(name):
        return os.getenv(name)
    try:
        with open(os.path.join(REPO, '.env')) as f:
            for line in f:
                if line.startswith(name + '='):
                    value = line.split('=', 1)[1].split(' #', 1)[0].strip()
                    return value.strip('"').strip("'") or default
    except OSError:
        pass
    return default


def jellyfin_tracks():
    url = env('JELLYFIN_URL', 'http://127.0.0.1:8096').rstrip('/')
    headers = {'Authorization': 'MediaBrowser Client="Hearth", Device="flamingo", '
                                f'DeviceId="hearth-flamingo", Version="1.0", Token="{env("JELLYFIN_API_KEY")}"'}

    def get(path, **params):
        r = requests.get(url + path, headers=headers, params=params, timeout=120)
        r.raise_for_status()
        return r.json()

    users = get('/Users')
    want = env('JELLYFIN_USER').lower()
    uid = next((u['Id'] for u in users if u.get('Name', '').lower() == want), users[0]['Id'])
    views = get(f'/Users/{uid}/Views').get('Items', [])
    music = [v['Id'] for v in views if v.get('CollectionType') == 'music']
    if not music:
        sys.exit('No Music library in Jellyfin (a library with content type "Music").')
    container_root = env('TV_JELLYFIN_MEDIA_ROOT', '/media').rstrip('/')
    host_root = env('TV_MEDIA_ROOT', '/mnt/media').rstrip('/')
    out = []
    for lib in music:
        data = get(f'/Users/{uid}/Items', ParentId=lib, IncludeItemTypes='Audio', Recursive='true',
                   EnableUserData='false',
                   Fields='Path,Genres,ProductionYear,RunTimeTicks,DateCreated,ParentIndexNumber')
        for t in data.get('Items', []):
            path = t.get('Path') or ''
            if path.startswith(container_root + '/'):
                path = host_root + path[len(container_root):]
            out.append({'id': t['Id'], 'name': t.get('Name'),
                        'artist': t.get('AlbumArtist') or ', '.join(t.get('Artists') or []),
                        'album': t.get('Album'), 'album_id': t.get('AlbumId'), 'year': t.get('ProductionYear'),
                        'genres': t.get('Genres') or [], 'track': t.get('IndexNumber'),
                        'disc': t.get('ParentIndexNumber'), 'added': t.get('DateCreated'),
                        'duration': round((t.get('RunTimeTicks') or 0) / 1e7, 1), 'path': path})
    return out


def decode(track, out_dir):
    out = os.path.join(out_dir, track['id'] + '.flac')
    cmd = ['nice', '-n', '19', 'ionice', '-c3', 'ffmpeg', '-nostdin', '-v', 'error', '-y',
           '-i', track['path'], '-t', str(MAX_SECONDS), '-vn', '-ac', '1', '-ar', '16000',
           '-sample_fmt', 's16', '-c:a', 'flac', out]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 10_000:
        if os.path.exists(out):
            os.remove(out)
        return track['id'], ((r.stderr if r else 'timed out') or 'empty output').strip().splitlines()[-1:]
    return track['id'], None


def _ssh(host, cmd):
    return subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host, cmd],
                          capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--audio-dir', default=os.path.join(HERE, 'audio'),
                    help='where the FLACs go (local), default flamingo/audio')
    ap.add_argument('--remote', default='', help='user@host:~/dir on the GPU box; audio goes to <dir>/audio/')
    ap.add_argument('--jobs', type=int, default=3)
    ap.add_argument('--batch', type=int, default=120)
    ap.add_argument('--limit', type=int, default=0, help='decode at most N songs (tests)')
    a = ap.parse_args()

    tracks = jellyfin_tracks()
    print(f'{time.strftime("%H:%M:%S")} jellyfin: {len(tracks)} songs', flush=True)
    manifest = [{k: v for k, v in t.items() if k != 'path'} for t in tracks]

    if a.remote:
        host, _, rdir = a.remote.partition(':')
        rdir = rdir.rstrip('/') or '~/flamingo'
        if _ssh(host, f'mkdir -p {rdir}/audio').returncode != 0:
            sys.exit(f'cannot reach {host} over ssh (key auth, BatchMode)')
        stage = tempfile.mkdtemp(prefix='hearth-flamingo-')
        with open(os.path.join(stage, 'manifest.json'), 'w') as f:
            json.dump(manifest, f, ensure_ascii=False)
        subprocess.run(['rsync', '-a', '--remove-source-files', os.path.join(stage, 'manifest.json'),
                        f'{host}:{rdir}/manifest.json'], check=True)
        have = set(_ssh(host, f'ls {rdir}/audio').stdout.split())
        out_dir = stage
    else:
        os.makedirs(a.audio_dir, exist_ok=True)
        with open(os.path.join(os.path.dirname(os.path.abspath(a.audio_dir)), 'manifest.json'), 'w') as f:
            json.dump(manifest, f, ensure_ascii=False)
        have = set(os.listdir(a.audio_dir))
        out_dir = a.audio_dir

    todo = [t for t in tracks if t['id'] + '.flac' not in have and t['path']]
    if a.limit:
        todo = todo[:a.limit]
    print(f'{time.strftime("%H:%M:%S")} already decoded: {len(have)}; to decode: {len(todo)}', flush=True)

    failed, done, t0 = {}, 0, time.time()
    for i in range(0, len(todo), a.batch):
        chunk = todo[i:i + a.batch]
        with ThreadPoolExecutor(a.jobs) as pool:
            for tid, err in pool.map(lambda t: decode(t, out_dir), chunk):
                if err:
                    failed[tid] = err
        if a.remote:
            files = [os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith('.flac')]
            if files:
                r = subprocess.run(['rsync', '-a', '--remove-source-files'] + files + [f'{host}:{rdir}/audio/'],
                                   capture_output=True, text=True)
                if r.returncode != 0:
                    sys.exit('rsync failed: ' + r.stderr[-300:])
        done += len(chunk)
        rate = done / max(time.time() - t0, 1)
        print(f'{time.strftime("%H:%M:%S")} {done}/{len(todo)} decoded, {len(failed)} failed, {rate:.1f} songs/s',
              flush=True)

    if failed:
        print('failed (first 10):', json.dumps(dict(list(failed.items())[:10]), indent=1))
    print(f'{time.strftime("%H:%M:%S")} DONE: {done - len(failed)} ok, {len(failed)} failed', flush=True)


if __name__ == '__main__':
    main()
