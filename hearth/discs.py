"""Discs: a USB CD/DVD drive on the server, ripped into the Jellyfin libraries
from the remote's Discs tab. Optional: only when TV_DISCS=1.

  a CD goes in  → the MusicBrainz disc id from the kernel's TOC → the release(s)
                → ripped by itself when MusicBrainz knows the disc and the album
                  isn't in the library yet: whipper (cdparanoia + AccurateRip
                  check, FLAC, tags, cover art) into a scratch folder, then moved
                  to Music/<Artist>/<Album>/ in the media share, Jellyfin
                  rescans, eject. A CD MusicBrainz doesn't know can be named on
                  the remote's form and ripped anyway.
  a DVD goes in → lsdvd (needs libdvdcss for most commercial discs; ripping your
                  own discs for your own use is your call and your jurisdiction's
                  law) → its name guessed from the disc label, corrected on the
                  remote → HandBrake rips the main feature (every audio track and
                  subtitle) into Movies/<Name (Year)>/ when Rip is tapped

The drive is polled with the kernel's CDROM ioctls (a USB drive's media changes don't
always reach udev). One job at a time. Doc: docs/server.md § Discs.
"""
import ast
import asyncio
import base64
import configparser
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import time
from pathlib import Path

import httpx

from hearth.errors import if_errors

DEVICE = os.getenv('TV_DISC_DEVICE', '/dev/sr0')
MEDIA_ROOT = Path(os.getenv('TV_MEDIA_ROOT', '/mnt/media'))
MUSIC_DIR = Path(os.getenv('TV_MUSIC_DIR') or MEDIA_ROOT / 'Music')
MOVIES_DIR = Path(os.getenv('TV_MOVIES_DIR') or MEDIA_ROOT / 'Movies')
# rips assemble on a local disk (a network share is slow and flaky under cdparanoia), then move into the share
SCRATCH = Path(os.getenv('TV_DISC_SCRATCH', '/var/tmp/hearth-disc-rips'))
WHIPPER_CONF = Path.home() / '.config' / 'whipper' / 'whipper.conf'
POLL_SECONDS = 3
STALL_SECONDS = 600        # whipper silent this long = frozen on a read (one sat 3 h 38 min on one song)
# One song may read this long, all its tries together, or 1.5 x its own length if that's longer;
# then its read is stopped and whipper (-k) skips it and goes on (a scratched 2:47 song once read for
# over an hour at under half speed; the freeze watch never fired because cd-paranoia kept reporting
# crumbs of progress)
CRAWL_MIN_SECONDS = 600
CRAWL_FACTOR = 1.5
PICK_WAIT_SECONDS = 180   # several different albums for one CD (box sets differ): the pick, else the first match


def _mb_user_agent():
    """MusicBrainz asks every client for a User-Agent with a way to contact its owner
    (musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting): TV_CONTACT in .env."""
    contact = (os.getenv('TV_CONTACT') or '').strip() or 'set TV_CONTACT in .env'
    return 'Hearth/1.0 ( %s )' % contact

# linux/cdrom.h
CDROM_DRIVE_STATUS, CDROM_DISC_STATUS, CDSL_CURRENT = 0x5326, 0x5327, 0x7fffffff
CDROMREADTOCHDR, CDROMREADTOCENTRY, CDROM_LBA, CDROM_LEADOUT, CDROM_DATA_TRACK = 0x5305, 0x5306, 0x01, 0xAA, 0x04
TOC_ENTRY = 'BBBxiBxxx'           # struct cdrom_tocentry: track, adr|ctrl, format, (pad), lba, datamode
DRIVE = {0: 'empty', 1: 'empty', 2: 'open', 3: 'busy', 4: 'disc'}
AUDIO_DISCS = {100, 105}          # CDS_AUDIO, CDS_MIXED (an enhanced CD: audio + a data session)

TRACK_TEMPLATE = '%A/%d/%t %n'
TRACK_TEMPLATE_DISCS = '%A/%d/Disc %N/%t %n'
DISC_TEMPLATE = '%A/%d/%A - %d'


def drive_status(device=DEVICE):
    """('none' | 'empty' | 'open' | 'busy' | 'disc', disc status code or None)."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return 'none', None
    try:
        state = DRIVE.get(fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT), 'empty')
        return state, (fcntl.ioctl(fd, CDROM_DISC_STATUS, 0) if state == 'disc' else None)
    except OSError:
        return 'busy', None
    finally:
        os.close(fd)


def disc_id_from_toc(device=DEVICE):
    """(MusicBrainz disc id, audio tracks, seconds) from the kernel's TOC, in milliseconds.
    whipper gets the same id, but its cdrdao read took ~3 min on a USB drive (both
    ways gave the same id on the test disc). The id is
    SHA-1 over first/last track, lead-out and 99 track offsets (+150), base64 with
    . _ - in place of + / =; an enhanced CD's data session ends the audio 11,400
    sectors before it starts (musicbrainz.org/doc/Disc_ID_Calculation)."""
    fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    try:
        first, last = struct.unpack('BB', fcntl.ioctl(fd, CDROMREADTOCHDR, b'\0\0'))

        def entry(n):
            _t, adr_ctrl, _f, lba, _m = struct.unpack(
                TOC_ENTRY, fcntl.ioctl(fd, CDROMREADTOCENTRY, struct.pack(TOC_ENTRY, n, 0, CDROM_LBA, 0, 0)))
            return lba, bool((adr_ctrl >> 4) & CDROM_DATA_TRACK)
        tracks = {n: entry(n) for n in range(first, last + 1)}
        leadout = entry(CDROM_LEADOUT)[0]
    finally:
        os.close(fd)
    audio = [n for n, (_lba, data) in tracks.items() if not data]
    if not audio:
        raise ValueError('no audio tracks')
    last_audio = max(audio)
    if last_audio < last:
        leadout = tracks[last_audio + 1][0] - 11400
    h = hashlib.sha1(b'%02X%02X%08X' % (first, last_audio, leadout + 150))
    for i in range(1, 100):
        h.update(b'%08X' % (tracks[i][0] + 150 if first <= i <= last_audio else 0))
    disc_id = base64.b64encode(h.digest()).decode().translate(str.maketrans('+/=', '._-'))
    return disc_id, last_audio - first + 1, (leadout - tracks[first][0]) / 75


def safe_name(text, fallback='Unknown'):
    """A folder name an SMB share takes: no / \\ : * ? " < > |, no trailing dot."""
    text = re.sub(r'\s*:\s+', ' - ', str(text or ''))       # "Title: Subtitle" → "Title - Subtitle"
    text = re.sub(r'[\\/:*?"<>|]+', ' ', text).strip().rstrip('.')
    return re.sub(r'\s+', ' ', text)[:120] or fallback


def clean_names(names, n_tracks=99):
    """Names typed on the remote for a CD MusicBrainz doesn't know: artist, album, year, one
    title per song."""
    names = names or {}
    out = {k: re.sub(r'\s+', ' ', str(names.get(k) or '')).strip()[:120] for k in ('artist', 'album')}
    year = str(names.get('year') or '').strip()
    out['year'] = year if re.fullmatch(r'(18|19|20)\d\d', year) else ''
    out['tracks'] = [re.sub(r'\s+', ' ', str(t or '')).strip()[:200] for t in (names.get('tracks') or [])][:n_tracks]
    return out


def _paranoia_reads(parent_pid):
    """whipper's cd-paranoia children → [(pid, track, length in s)]. The span on its command line
    ("24[00:00:00.00]-24[00:02:47.13]", [hh:mm:ss.sectors]) gives the song and its length."""
    reads = []
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % d) as f:
                if int(f.read().rsplit(')', 1)[1].split()[1]) != parent_pid:
                    continue
            with open('/proc/%s/cmdline' % d, 'rb') as f:
                args = f.read().decode('utf-8', 'replace').split('\0')
        except (OSError, ValueError, IndexError):
            continue
        if not args or not args[0].endswith('cd-paranoia'):
            continue
        span = next((a for a in args if re.match(r'^\d+\[', a)), '')
        m = re.match(r'(\d+)\[[\d:.]+\]-\d+\[(\d+):(\d+):(\d+)', span)
        if m:
            reads.append((int(d), int(m.group(1)), int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))))
    return reads


def album_dir(names, fallback=None):
    """Where an unknown CD's album goes in Music: the typed artist and album, else Unknown Artist /
    "Unknown album <date>" (fallback) so a later naming can move it."""
    return (MUSIC_DIR / safe_name(names.get('artist') or 'Unknown Artist')
            / safe_name(names.get('album') or fallback or time.strftime('Unknown album %Y-%m-%d %H-%M')))


def _track_no(path):
    m = re.match(r'(\d+)', path.name)
    return int(m.group(1)) if m else 0


def _name_tracks(folder, names):
    """Put the typed names on an unknown CD's FLACs in `folder`: "NN Title.flac" and the TITLE /
    ARTIST / ALBUMARTIST / ALBUM / DATE / TRACKNUMBER tags (metaflac). Songs left blank keep theirs."""
    for f in sorted(folder.rglob('*.flac'), key=_track_no):
        n = _track_no(f)
        title = names['tracks'][n - 1] if 0 < n <= len(names['tracks']) else ''
        tags = {'TRACKNUMBER': str(n)}
        if title:
            tags['TITLE'] = title
        if names.get('artist'):
            tags['ARTIST'] = tags['ALBUMARTIST'] = names['artist']
        if names.get('album'):
            tags['ALBUM'] = names['album']
        if names.get('year'):
            tags['DATE'] = names['year']
        subprocess.run(['metaflac'] + ['--remove-tag=%s' % k for k in tags]
                       + ['--set-tag=%s=%s' % kv for kv in tags.items()] + [str(f)], check=True, capture_output=True)
        if title:
            new = f.with_name('%02d %s.flac' % (n, safe_name(title, 'Track %d' % n)))
            if new != f and not new.exists():
                f.rename(new)


def label_title(label):
    """A DVD volume label as a title guess: "THE_BAND_LIVE_1984" → "The Band Live 1984"."""
    words = re.sub(r'[_\s]+', ' ', str(label or '')).strip()
    words = re.sub(r'\b(DISC|DVD|D)\s*\d+$', '', words, flags=re.I).strip()
    return ' '.join(w.capitalize() if w.isupper() or w.islower() else w for w in words.split()) or 'Untitled disc'


def ensure_whipper_conf():
    """whipper's own settings: names safe for an SMB share (vfat filter)."""
    conf = configparser.ConfigParser()
    conf.read(WHIPPER_CONF)
    if not conf.has_section('main'):
        conf.add_section('main')
    if conf.get('main', 'path_filter_vfat', fallback=None) != 'True':
        conf.set('main', 'path_filter_vfat', 'True')
        WHIPPER_CONF.parent.mkdir(parents=True, exist_ok=True)
        with open(WHIPPER_CONF, 'w') as f:
            conf.write(f)


def whipper_offset_known():
    conf = configparser.ConfigParser()
    conf.read(WHIPPER_CONF)
    return any(s.startswith('drive:') and conf.has_option(s, 'read_offset') for s in conf.sections())


class Discs:
    def __init__(self, data_dir, on_added=None, notify=None):
        self.log_path = Path(data_dir) / 'discs.jsonl'
        self.tool_logs = Path(data_dir) / 'disc_logs'          # each rip's whole tool output
        self.on_added = on_added            # async callable(kind): Jellyfin rescan
        self.notify = notify                # async callable(title, body): the phone (push.py)
        self.drive = 'none'
        self.disc = None                    # what's in the drive, once read
        self.job = None                     # the rip running or just finished
        self.last = None                    # the last finished rip, shown until the next disc
        self.proc = None
        self._task = None
        self._gen = 0                       # bumps each time a disc leaves: a slow lookup can't land on the next one

    # ------------------------------------------------------------------ status
    def status(self):
        return {'drive': self.drive, 'disc': self.disc, 'job': self.job, 'last': self.last}

    def _note(self, **entry):
        entry['t'] = round(time.time())
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except OSError:
            pass

    # ------------------------------------------------------------------ watching
    async def watch(self):
        while True:
            try:
                await self._tick()
            except Exception as e:          # never let the drive loop die
                print('[DISCS] watch error: %r' % e)
            await asyncio.sleep(POLL_SECONDS)

    async def _tick(self):
        if self._running():
            return                          # the job owns the drive
        state, code = await asyncio.to_thread(drive_status)
        self.drive = state
        if state != 'disc':
            if state in ('empty', 'open', 'none'):
                if self.disc:
                    self.disc = None
                    self._gen += 1
                if self.job and self.job.get('state') in ('done', 'failed', 'stopped'):
                    self.last, self.job = self.job, None      # the next disc starts clean
            return
        if self.disc is None:
            self.disc = {'kind': 'reading', 'code': code}
            if self.job and self.job.get('state') in ('done', 'failed', 'stopped'):
                self.last, self.job = self.job, None
            self._task = asyncio.create_task(self._identify(code, self._gen))

    def _running(self):
        return bool(self.job and self.job.get('state') in ('starting', 'ripping', 'moving'))

    async def _tell(self, title, body):
        if self.notify:
            try:
                await self.notify(title, body)
            except Exception as e:
                print('[DISCS] notification failed: %r' % e)

    async def _tell_if_needed(self, disc):
        """A disc that's waiting for a choice says so on the phone."""
        kind, title = disc.get('kind'), disc.get('title') or 'The disc'
        if kind == 'dvd':
            await self._tell('🎬 A DVD is waiting for you',
                             '"%s": check its title in the Discs tab and tap Rip to Movies.' % title)
        elif kind == 'cd' and not disc.get('auto'):
            rels = disc.get('releases') or []
            if not rels:
                await self._tell('💿 A CD MusicBrainz doesn\'t know',
                                 'Open Discs to rip it anyway (as Unknown) or eject it.')
            elif disc.get('already'):
                await self._tell('💿 %s is already in your Music' % title,
                                 'Tap Rip it again for a fresh copy, or eject it.')
            elif len(rels) > 1:
                at = time.strftime('%-I:%M', time.localtime(time.time() + PICK_WAIT_SECONDS))
                await self._tell('🙋 Which %s?' % title,
                                 'MusicBrainz knows %d versions. Pick yours in Discs, or I\'ll rip the first one at %s.'
                                 % (len(rels), at))
        elif kind in ('error', 'other'):
            await self._tell('⚠️ Couldn\'t read the disc', disc.get('error') or title)

    @if_errors
    async def _read_disc(self, code):
        return await (self._identify_cd() if code in AUDIO_DISCS else self._identify_dvd())

    async def _identify(self, code, gen):
        disc = await self._read_disc(code)
        # Right after a CD goes in, a drive may not call it an audio CD yet (the status says something
        # else, so it was tried as a DVD and failed; minutes later the same CD read as audio). Ask the
        # drive again, three times 5 s apart, before giving up.
        for _ in range(3):
            if (disc and not disc.get('error')) or gen != self._gen:
                break
            await asyncio.sleep(5)
            state, again = await asyncio.to_thread(drive_status)
            if state != 'disc':
                break
            code = again
            disc = await self._read_disc(code) or disc
        disc = disc or {'kind': 'error', 'title': "Couldn't read the disc", 'code': code}
        if gen != self._gen:
            return                          # that disc is gone
        self.disc = disc
        self._note(event='inserted', kind=disc.get('kind'), title=disc.get('title'), artist=disc.get('artist'),
                   **({'code': disc.get('code')} if disc.get('kind') == 'error' else {}))
        await self._tell_if_needed(disc)
        if disc['kind'] == 'cd' and disc.get('auto'):
            await self.rip()
        elif disc['kind'] == 'cd' and len(disc.get('releases') or []) > 1 and not disc.get('already'):
            disc['auto_at'] = time.time() + PICK_WAIT_SECONDS
            await asyncio.sleep(PICK_WAIT_SECONDS)
            if gen == self._gen and self.disc is disc and not self._running() and not self.job:
                self._note(event='auto_pick', title=disc.get('title'), release=disc.get('release'))
                await self.rip()

    # ------------------------------------------------------------------ CDs
    async def _identify_cd(self):
        ensure_whipper_conf()
        try:
            disc_id, n_tracks, secs = await asyncio.to_thread(disc_id_from_toc)
        except (OSError, ValueError, KeyError) as e:
            return {'kind': 'cd', 'error': "Couldn't read the CD's table of contents (%s)." % e, 'releases': []}
        releases = await self._musicbrainz(disc_id)
        disc = {'kind': 'cd', 'disc_id': disc_id, 'releases': releases, 'tracks': n_tracks,
                'duration': '%d:%02d' % (secs // 60, secs % 60)}
        if not releases:
            disc.update(title='A CD MusicBrainz doesn\'t know', auto=False)
            return disc
        # pressings of one album ("Album", "Album (XRCD2)", "Album (remastered)")
        # are the same songs: rip by itself, under the plain title when one has it
        base = lambda r: (r['artist'].lower(), re.sub(r'\s*\(.*?\)\s*', ' ', r['title']).strip().lower())
        same = len({base(r) for r in releases}) == 1
        first = next((r for r in releases if '(' not in r['title'] and r.get('discs', 1) == 1), releases[0])
        releases.sort(key=lambda r: r is not first)
        disc.update(release=first['id'], title=first['title'], artist=first['artist'], year=first.get('year'),
                    cover=first.get('cover'), already=self._have_album(first))
        disc['auto'] = same and not disc['already']
        return disc

    async def _musicbrainz(self, disc_id):
        url = 'https://musicbrainz.org/ws/2/discid/%s' % disc_id
        data = None
        for attempt in range(4):            # MusicBrainz allows ~1 request/s and says 503 "busy" past it
            await asyncio.sleep(1.5 * attempt + 1)
            try:
                async with httpx.AsyncClient(timeout=20, headers={'User-Agent': _mb_user_agent()}) as c:
                    r = await c.get(url, params={'inc': 'artist-credits recordings', 'fmt': 'json'})
                if r.status_code == 200:
                    data = r.json()
                    break
                if r.status_code == 404:
                    return []
            except (httpx.HTTPError, ValueError):
                pass
        if not data:
            return []
        releases = []
        for rel in data.get('releases') or []:
            media = rel.get('media') or []
            medium = next((m for m in media if any(d.get('id') == disc_id for d in m.get('discs') or [])),
                          media[0] if media else {})
            credit = ''.join(a.get('name', '') + a.get('joinphrase', '') for a in rel.get('artist-credit') or [])
            releases.append({
                'id': rel.get('id'), 'title': rel.get('title') or 'Untitled', 'artist': credit or 'Unknown Artist',
                'year': (rel.get('date') or '')[:4] or None, 'country': rel.get('country'),
                'discs': len(media), 'disc_number': medium.get('position'),
                'cover': 'https://coverartarchive.org/release/%s/front-250' % rel.get('id')
                         if (rel.get('cover-art-archive') or {}).get('front') else None,
                'tracks': [{'n': t.get('position'), 'title': t.get('title'), 'length': (t.get('length') or 0) // 1000}
                           for t in medium.get('tracks') or []],
            })
        return releases

    def _have_album(self, rel):
        album = MUSIC_DIR / safe_name(rel['artist']) / safe_name(rel['title'])
        if (rel.get('discs') or 1) > 1:     # disc 2 of an album whose disc 1 is in already
            return (album / ('Disc %s' % rel.get('disc_number'))).exists()
        return album.exists()

    async def _rip_cd(self, job):
        rel = next((r for r in self.disc.get('releases') or [] if r['id'] == job.get('release')), None)
        work = SCRATCH / job['id']
        out = work / 'out'
        out.mkdir(parents=True, exist_ok=True)
        (work / 'work').mkdir(exist_ok=True)        # whipper chdirs into it, it doesn't make it
        ensure_whipper_conf()
        if not whipper_offset_known():
            job.update(step="Learning this drive's read offset (first CD only, a few minutes)…")
            rc, text = await self._run(['whipper', '-e', 'never', 'offset', 'find'], job=job, timeout=1800)
            if not whipper_offset_known():
                raise RuntimeError("Couldn't find the drive's read offset: " + text.strip()[-200:])
        cmd = ['whipper', '-e', 'never', 'cd', 'rip', '-O', str(out), '-W', str(work / 'work'),
               '--track-template', TRACK_TEMPLATE_DISCS if rel and rel.get('discs', 1) > 1 else TRACK_TEMPLATE,
               '--disc-template', DISC_TEMPLATE, '-C', 'complete', '-k',
               # 2 tries a track, not whipper's 5: over 102 track rips no retry ever saved one
               # (8 failed all 5), and a scratched track's tries took hours (one CD with 3 bad
               # tracks took 2.6 h for 46 min of music)
               '-r', '2']
        if rel:
            cmd += ['-R', rel['id']]
        else:
            cmd += ['-U']
        job.update(step='Ripping', state='ripping')
        since, checked = {}, [0.0]

        def slow_song_watch(proc):
            now = time.monotonic()
            if now - checked[0] < 5:
                return
            checked[0] = now
            for pid, track, length in _paranoia_reads(proc.pid):
                if now - since.setdefault(track, now) > max(CRAWL_MIN_SECONDS, CRAWL_FACTOR * length):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        continue
                    if track not in job.setdefault('slow', []):
                        job['slow'].append(track)
                        self._note(event='slow_song', title=job.get('title'), track=track, length=length)
        started = time.time()
        rc, text = await self._run(cmd, job=job, progress=self._whipper_progress, timeout=4 * 3600,
                                   stall=STALL_SECONDS, tick=slow_song_watch)
        for tmp in Path('/tmp').glob('tmp*.whipper.wav'):    # a stopped read leaves its temp file
            try:
                if tmp.stat().st_mtime >= started:
                    tmp.unlink()
            except OSError:
                pass
        for part in out.rglob('*.part'):             # the song it froze on
            part.unlink(missing_ok=True)
        albums = [p for p in out.glob('*/*') if p.is_dir() and any(p.rglob('*.flac'))]
        if job.get('state') == 'stopped':
            raise RuntimeError('Stopped')
        if not albums:
            raise RuntimeError('The rip stopped: ' + _last_lines(text))
        if job.get('stalled'):
            # whipper writes its .log only at the end, so what's missing comes from MusicBrainz's list
            have = {f.name[:2] for a in albums for f in a.rglob('*.flac')}
            missing = [t['title'] for t in (rel or {}).get('tracks', []) if '%02d' % int(t['n'] or 0) not in have]
            why = ("The rip ran past 4 hours, so it was stopped there." if job.get('timed_out') else
                   "The drive froze on a song (probably a scratch), so the rip stopped there.")
            job['missing'] = missing
            job['warning'] = ("%s Not in: %s. The other songs are in." % (why, ', '.join(missing)) if missing else
                              "%s The songs it read are in." % why)
        elif rc != 0:
            # a scratched track is skipped (-k) and whipper exits non-zero: the songs it did
            # read still go to the library (on one scratched CD, 19 of 20 were fine)
            missing = [m for m in re.findall(r'(?m)^\s*Filename: .*/(\d\d [^/\n]+)\.flac\s*\n(?:(?!\s*Filename:).*\n){0,40}?\s*Status: Track not ripped',
                                             _read_logs(albums))]
            if not missing:
                # Some whipper versions only say "track 9 failed to rip." in their output, and their .log
                # has nothing the pattern above finds: without this, lost songs showed only as "some songs
                # couldn't be read" on the card and left no trace in the ledger.
                titles = {int(t['n'] or 0): t['title'] for t in (rel or {}).get('tracks', [])}
                nums = sorted({int(n) for n in re.findall(r'(?i)\btrack (\d+) failed to rip', text)})
                missing = ['%02d %s' % (n, titles[n]) if n in titles else 'track %d' % n for n in nums]
            job['missing'] = missing
            job['warning'] = ("Couldn't read %s, probably a scratch. The other songs are in." % ', '.join(missing)
                              if missing else "Some songs couldn't be read cleanly; the rest are in.")
        job.update(state='moving', step='Adding it to your Music library…', percent=100)
        if job.get('unknown'):
            names = job.get('names') or clean_names(None)          # what was typed, up to this moment
            await asyncio.to_thread(_name_tracks, out, names)
            target = album_dir(names)
            for album in albums:
                await asyncio.to_thread(_move_tree, album, target)
            job.update(placed=str(target), where=[str(target.relative_to(MUSIC_DIR))],
                       title=names['album'] or target.name, artist=names['artist'] or None)
            await asyncio.to_thread(shutil.rmtree, work, True)
            return 'music'
        placed = []
        for album in albums:
            target = MUSIC_DIR / album.parent.name / album.name
            await asyncio.to_thread(_move_tree, album, target)
            placed.append(str(target.relative_to(MUSIC_DIR)))
        job['where'] = placed
        await asyncio.to_thread(shutil.rmtree, work, True)
        return 'music'

    @staticmethod
    def _whipper_progress(job, line):
        m = re.search(r'[Rr]ipping track (\d+) of (\d+)', line)
        if m:
            n, total = int(m.group(1)), int(m.group(2))
            job.update(track=n, of=total, step='Ripping track %d of %d' % (n, total))
            job['percent'] = round(100 * (n - 1) / max(total, 1))
        m = re.search(r'(\d{1,3}(?:\.\d+)?) ?%', line)
        if m and job.get('of'):
            within = min(100.0, float(m.group(1)))
            job['percent'] = round(100 * ((job['track'] - 1) + within / 100) / job['of'])
        if 'AccurateRip' in line and 'match' in line.lower():
            job['accurate'] = line.strip()[-120:]

    # ------------------------------------------------------------------ DVDs
    async def _identify_dvd(self):
        rc, out = await self._run(['lsdvd', '-Oy', DEVICE], timeout=120)
        try:
            data = ast.literal_eval(out[out.index('{'):])
        except (ValueError, SyntaxError):
            return {'kind': 'other', 'title': "A disc that isn't an audio CD or a video DVD"}
        tracks = data.get('track') or []
        main = max(tracks, key=lambda t: t.get('length') or 0) if tracks else {}
        title = label_title(data.get('title'))
        return {'kind': 'dvd', 'label': data.get('title'), 'title': title, 'year': None,
                'length': round(main.get('length') or 0), 'format': main.get('format') or data.get('format'),
                'titles': len(tracks), 'already': (MOVIES_DIR / safe_name(title)).exists()}

    async def _rip_dvd(self, job):
        name = safe_name(job.get('name') or self.disc.get('title'), 'Untitled disc')
        if job.get('year'):
            name = '%s (%s)' % (name, job['year'])
        work = SCRATCH / job['id']
        work.mkdir(parents=True, exist_ok=True)
        out = work / (name + '.mkv')
        pal = str(self.disc.get('format') or '').upper() == 'PAL'
        cmd = ['HandBrakeCLI', '-i', DEVICE, '--main-feature', '-o', str(out),
               '-Z', 'HQ 576p25 Surround' if pal else 'HQ 480p30 Surround', '-f', 'av_mkv',
               # every audio track as it is on the disc (AC-3 surround and all); the preset
               # alone made them all stereo MP3
               '--all-audio', '-E', 'copy', '--audio-copy-mask', 'ac3,eac3,dts,dtshd,mp2,aac,mp3',   # not lpcm: HandBrake can't copy it and refuses to start
               '--audio-fallback', 'av_aac', '--all-subtitles']
        job.update(state='ripping', step='Ripping the main feature')
        rc, text = await self._run(cmd, job=job, progress=self._handbrake_progress, timeout=6 * 3600)
        if rc != 0 or not out.exists() or out.stat().st_size < 10_000_000:
            raise RuntimeError('The rip stopped: ' + _last_lines(text))
        job.update(state='moving', step='Adding it to your Movies library…', percent=100)
        target = MOVIES_DIR / name
        await asyncio.to_thread(_move_tree, work, target)
        job['where'] = [str(target.relative_to(MOVIES_DIR))]
        return 'movies'

    @staticmethod
    def _handbrake_progress(job, line):
        m = re.search(r'Encoding: task (\d+) of (\d+), (\d+\.\d+) %(?:.*ETA (\d+)h(\d+)m(\d+)s)?', line)
        if m:
            job['percent'] = round(float(m.group(3)))
            if m.group(4):
                job['eta'] = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6))

    # ------------------------------------------------------------------ jobs
    async def set_names(self, names, apply=False):
        """Typed names for a CD MusicBrainz doesn't know. Kept with the disc and its rip, and put on the
        files when the rip finishes; for an album already in Music, apply=True renames it there."""
        disc = self.disc if self.disc and self.disc.get('kind') == 'cd' and not self.disc.get('releases') else None
        names = clean_names(names, (disc or {}).get('tracks') or 99)
        if disc:
            disc['names'] = names
        job = next((j for j in (self.job, self.last) if j and j.get('unknown')), None)
        if job:
            job['names'] = names
            if apply and job.get('state') == 'done' and job.get('placed'):
                await asyncio.to_thread(self._rename_placed, job, names)
                self._note(event='named', title=job.get('title'), artist=job.get('artist'), where=job.get('where'))
                if self.on_added:
                    await self.on_added('music')
        return names

    def _rename_placed(self, job, names):
        """An unknown CD already in Music: retitle, retag, and move its folder to the typed artist/album."""
        folder = Path(job['placed'])
        _name_tracks(folder, names)
        target = album_dir(names, fallback=folder.name)
        if target != folder and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(folder, target)
            try:
                folder.parent.rmdir()            # the old "Unknown Artist" folder, if it's now empty
            except OSError:
                pass
            folder = target
        job.update(placed=str(folder), where=[str(folder.relative_to(MUSIC_DIR))],
                   title=names.get('album') or job.get('title'), artist=names.get('artist') or job.get('artist'))

    async def rip(self, release=None, name=None, year=None, unknown=False, names=None):
        """Start ripping what's in the drive (one job at a time)."""
        if self._running():
            raise RuntimeError('A disc is already being ripped')
        disc = self.disc or {}
        if disc.get('kind') not in ('cd', 'dvd'):
            raise RuntimeError('There is no CD or DVD to rip')
        if disc['kind'] == 'cd' and not disc.get('releases') and not unknown:
            raise RuntimeError("MusicBrainz doesn't know this CD")
        job = {'id': time.strftime('%Y%m%d-%H%M%S'), 'kind': disc['kind'], 'state': 'starting', 'percent': 0,
               'step': 'Starting', 'started': time.time(), 'release': release or disc.get('release'),
               'name': name, 'year': year, 'title': name or disc.get('title'), 'artist': disc.get('artist')}
        if disc['kind'] == 'cd' and not disc.get('releases'):
            job['unknown'] = True
            job['n_tracks'] = disc.get('tracks') or 0
            job['names'] = clean_names(names or disc.get('names'), disc.get('tracks') or 99)
            job.update(title=job['names']['album'] or 'An unknown CD', artist=job['names']['artist'] or None)
        if disc['kind'] == 'cd' and job['release']:
            rel = next((r for r in disc['releases'] if r['id'] == job['release']), None)
            if rel:
                job.update(title=rel['title'], artist=rel['artist'])
        self.job = job
        self._task = asyncio.create_task(self._do(job))
        return job

    async def _do(self, job):
        try:
            kind = await (self._rip_cd(job) if job['kind'] == 'cd' else self._rip_dvd(job))
            job.update(state='done', step='Added to your %s library' % ('Music' if kind == 'music' else 'Movies'),
                       finished=time.time())
            self._note(event='ripped', kind=job['kind'], title=job.get('title'), artist=job.get('artist'),
                       where=job.get('where'), secs=round(job['finished'] - job['started']),
                       **({'missing': job['missing'], 'warning': job.get('warning')} if job.get('missing')
                          else {'warning': job['warning']} if job.get('warning') else {}))
            if self.on_added:
                try:
                    await self.on_added(kind)
                except Exception as e:
                    print('[DISCS] library refresh failed: %r' % e)
            await self._run(['eject', DEVICE], timeout=30)
            lib = 'Music' if kind == 'music' else 'Movies'
            who = ('%s · ' % job['artist']) if job.get('artist') else ''
            await self._tell('✅ %s is in your %s' % (job.get('title') or 'The disc', lib),
                             who + (job['warning'] + ' ' if job.get('warning') else '') + 'The tray is open for the next one.')
        except asyncio.CancelledError:
            job.update(state='stopped', step='Stopped')
            raise
        except Exception as e:
            if job.get('state') != 'stopped':
                job.update(state='failed', step=str(e)[:300])
                self._note(event='failed', kind=job['kind'], title=job.get('title'), error=str(e)[:300])
                await self._tell('⚠️ %s didn\'t rip' % (job.get('title') or 'The disc'), str(e)[:160])
        finally:
            self.proc = None

    async def stop(self):
        job = self.job
        if not self._running():
            return False
        job.update(state='stopped', step='Stopped')
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self._note(event='stopped', kind=job['kind'], title=job.get('title'))
        return True

    def reread(self):
        """Read the disc in the drive again (after a failed read)."""
        if self._running():
            raise RuntimeError('A disc is being ripped')
        self.disc = None
        self._gen += 1
        return True

    async def eject(self):
        if self._running():
            raise RuntimeError('A disc is being ripped')
        await self._run(['eject', DEVICE], timeout=30)
        self.disc = None
        return True

    async def _run(self, cmd, job=None, progress=None, timeout=600, stall=None, tick=None):
        """Run a tool in its own process group; stdout+stderr, split on \\n and \\r
        (HandBrake redraws its progress line). Returns (returncode, all text).
        stall: seconds without a word from the tool before it's stopped as frozen (job['stalled']);
        with stall set, running past `timeout` also stops it that way (job['timed_out']) instead of
        failing the job, so what it read is still filed.
        tick(proc): called after each piece of output (the slow-song watch)."""
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        if job is not None:
            self.proc = proc
        chunks, buf = [], ''
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        data = await asyncio.wait_for(proc.stdout.read(4096), stall)
                    except TimeoutError:
                        if stall is None:
                            raise
                        # frozen on a read: one rip sat on a song for over 3.5 h, to the 4 h limit,
                        # without one more byte
                        if job is not None:
                            job['stalled'] = True
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        chunks.append('\n[no output for %d s: stopped as frozen]\n' % stall)
                        break
                    if not data:
                        break
                    text = data.decode('utf-8', 'replace')
                    chunks.append(text)
                    buf += text
                    *lines, buf = re.split(r'[\r\n]', buf)
                    if progress and job is not None:
                        for line in lines:
                            progress(job, line)
                    if tick:
                        tick(proc)
                await proc.wait()
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
            self._log(job, cmd, ''.join(chunks), 'killed: took too long')
            if stall is None or job is None:
                raise RuntimeError('%s took too long and was stopped' % cmd[0])
            job['stalled'] = job['timed_out'] = True            # file the songs it read
            return proc.returncode, ''.join(chunks)[-200_000:]
        text = ''.join(chunks)
        self._log(job, cmd, text, proc.returncode)
        return proc.returncode, text[-200_000:]

    def _log(self, job, cmd, text, status):
        """The tool's whole output in data/disc_logs/<job>.log, also when it was stopped."""
        if job is None:
            return
        try:
            self.tool_logs.mkdir(parents=True, exist_ok=True)
            with open(self.tool_logs / ('%s.log' % job['id']), 'a') as f:
                f.write('$ %s\n%s\n[exit %s]\n\n' % (' '.join(cmd), text.replace('\r', '\n'), status))
        except OSError:
            pass


def _move_tree(src, dst):
    """Into the share: copy, then remove the scratch copy (a rename can't cross mounts).
    An existing folder of the same name gets the new files beside it."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    shutil.rmtree(src, ignore_errors=True)


def _read_logs(albums):
    """whipper's .log files in the ripped album folders, as one text."""
    text = ''
    for album in albums:
        for log in album.rglob('*.log'):
            try:
                text += log.read_text(errors='replace') + '\n'
            except OSError:
                pass
    return text


def _last_lines(text, n=3):
    lines = [l.strip() for l in re.split(r'[\r\n]', text or '') if l.strip()]
    return ' | '.join(lines[-n:])[-300:]
