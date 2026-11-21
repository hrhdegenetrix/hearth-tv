#!/usr/bin/env python3
"""Hearth receiver: the screen half of the home TV box (a Raspberry Pi on the TV).

The server (hearth/server.py on the home server) is the brains; this process only
does what it is told and reports what is on screen:

  mpv       one idle player, fullscreen over everything while it plays:
            files from the server's library, YouTube / any yt-dlp link, music queues
  chromium  kiosk browser on the TV home page; streaming websites (Tubi, Pluto TV,
            Prime, Max, ...) open as tabs through its local DevTools HTTP endpoint;
            YouTube's own TV app runs as a second kiosk Chromium
  uinput    a virtual keyboard + mouse, so the phone remote can drive the
            browser apps (arrows, OK, back, typing, touchpad)
  waydroid  optional Android 11 for the streaming apps' Android versions
            (docs/android.md); everything else works without it

Every route needs X-TV-Token (shared with the server) except the TV's own
home page and its /local/* buttons, which answer 127.0.0.1 only.

Runs as systemd unit hearth-receiver on the Pi (port from config.json, 8770 by
default). Deploy with receiver/deploy.sh. Doc: docs/receiver.md.
"""
import glob
import hmac
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import requests
from flask import Flask, abort, jsonify, request, send_file, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, 'config.json')
VENV_BIN = os.path.join(HERE, 'venv', 'bin')


def _load_config():
    """~/hearth/config.json on the Pi: {"token", "port", "server"} (+ optional
    "nightly_reboot", "remote_hint"). It is never in git."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except (OSError, ValueError) as exc:
        raise SystemExit(f'hearth receiver: cannot read {CONFIG_PATH} ({exc}). Write it first: '
                         '{"token": "<TV_RECEIVER_TOKEN>", "port": 8770, "server": "http://<server>:5080"}')
    missing = [k for k in ('token', 'server') if not config.get(k)]
    if missing:
        raise SystemExit(f'hearth receiver: {CONFIG_PATH} needs {", ".join(missing)} '
                         '("server" is how this box reaches the Hearth server, e.g. http://192.0.2.10:5080)')
    config['server'] = config['server'].rstrip('/')
    return config


CONFIG = _load_config()
PORT = int(CONFIG.get('port', 8770))

MPV_SOCK = '/tmp/hearth-mpv.sock'
MPV_PIDFILE = '/tmp/hearth-mpv.pid'
CDP = 'http://127.0.0.1:9222'
HOME_URL = f'http://127.0.0.1:{PORT}/home'
BROWSER_PROFILE = os.path.expanduser('~/.config/hearth-browser')
SNAPSHOT_PATH = '/tmp/hearth-snapshot.jpg'

# Pi 4 decodes H.264 (to 1080p) and HEVC in hardware; VP9/AV1 would land on
# the CPU, so YouTube is asked for avc1 first.
YTDL_FORMAT = ('bestvideo[height<=1080][vcodec^=avc1]+bestaudio/'
               'best[height<=1080][vcodec^=avc1]/best[height<=1080]/best')

# YouTube's own TV app (the one smart TVs run): d-pad navigation, and phones can
# cast to it once linked. youtube.com/tv only serves it to TV user agents, so it
# runs as a second kiosk Chromium with a Tizen UA and its own profile (its
# sign-in lives there). Verified 2026-10-09: welcome → sign-in screen by OK.
YOUTUBE_TV_URL = 'https://www.youtube.com/tv'
YOUTUBE_TV_UA = ('Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 '
                 '(KHTML, like Gecko) 85.0.4183.93/6.5 TV Safari/537.36')
YOUTUBE_PROFILE = os.path.expanduser('~/.config/hearth-youtube')

# The streaming apps' Android versions, when Waydroid is set up (docs/android.md).
# Without it, 'prime' and 'max' open their websites instead (APPS below).
ANDROID_APPS = {
    'prime': 'com.amazon.avod.thirdpartyclient',
    'max': 'com.wbd.stream',
}
# Tubi's Android app is portrait-only (a phone-shaped strip on the TV), so Tubi
# is the full-screen website instead.
# Pluto TV is the website too: Android under Waydroid on a Pi 4 has no hardware
# video decoding, and Pluto's app decoded live TV in software at ~170% CPU, so
# the picture froze while the sound and the clock ran on. Chromium plays the
# same streams smoothly. If the app is installed anyway, close_all still stops it.
ANDROID_INSTALLED = (*ANDROID_APPS.values(), 'tv.pluto.android')
ANDROID_LABELS = {'com.wbd.stream': 'Max', 'com.amazon.avod.thirdpartyclient': 'Prime Video',
                  'tv.pluto.android': 'Pluto TV'}
# How long each step of an open usually takes, learned from real opens
# (Android.public's "usually about N s").
TIMINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'android_timings.json')
# A session older than this that won't take the wake-up key is wedged, not busy
# with its first-boot work (2026-10-20 20:21, see Android._wake_or_restart).
SESSION_SETTLED = 10 * 60


def _load_timings():
    try:
        with open(TIMINGS_PATH) as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}

APPS = {
    'tubi': 'https://tubitv.com/',
    'pluto': 'https://pluto.tv/live-tv/',
    'prime': 'https://www.amazon.com/gp/video/storefront',
    'max': 'https://play.max.com/',
}

# The autologin desktop session (labwc) this service draws on: same user, so
# the same /run/user/<uid>. A systemd service doesn't inherit the session's
# environment, so it is spelled out here.
_RUNTIME_DIR = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
SESSION_ENV = {
    'XDG_RUNTIME_DIR': _RUNTIME_DIR,
    'WAYLAND_DISPLAY': os.environ.get('WAYLAND_DISPLAY') or 'wayland-0',
    'XDG_SESSION_TYPE': 'wayland',
    'DBUS_SESSION_BUS_ADDRESS': os.environ.get('DBUS_SESSION_BUS_ADDRESS') or f'unix:path={_RUNTIME_DIR}/bus',
}

app = Flask(__name__, static_folder=None)


def android_available():
    """Is Waydroid installed on this box? Android is optional (docs/android.md)."""
    return shutil.which('waydroid') is not None


NO_ANDROID = "Android isn't set up on this box (see docs/android.md)."


def _session_env():
    env = dict(os.environ)
    env.update(SESSION_ENV)
    env['PATH'] = VENV_BIN + ':' + env.get('PATH', '/usr/bin:/bin')
    return env


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _is_local():
    return request.remote_addr in ('127.0.0.1', '::1')


@app.before_request
def _gate():
    path = request.path
    if path == '/home' or path.startswith('/home/') or path.startswith('/local/'):
        if not _is_local():
            abort(403)
        return None
    token = request.headers.get('X-TV-Token', '')
    if not hmac.compare_digest(token, CONFIG.get('token', '')):
        abort(401)
    if request.method == 'POST':
        REMOTE_USED['at'] = time.time()
    return None


# When the phone remote last did anything on the TV (a key, Home, an app, a cast);
# the morning Android refresh and the nightly reboot wait for a quiet remote.
REMOTE_USED = {'at': 0.0}


# --------------------------------------------------------------------------- #
# mpv
# --------------------------------------------------------------------------- #
class Player:
    """One long-lived idle mpv; every command opens a fresh IPC connection."""

    def __init__(self):
        self.proc = None
        # Re-entrant: command() holds it while calling ensure(); the startup
        # thread calls ensure() directly. Unlocked, the two raced into two mpvs.
        self.lock = threading.RLock()
        self.req_id = 0

    def ensure(self):
        with self.lock:
            self._ensure()

    def _ensure(self):
        if self.proc and self.proc.poll() is None and os.path.exists(MPV_SOCK):
            return
        if self.proc is None:
            self._reap_orphan()
        if os.path.exists(MPV_SOCK):
            os.unlink(MPV_SOCK)
        cmd = [
            'mpv', '--idle=yes', '--force-window=no', '--fs',
            '--input-ipc-server=' + MPV_SOCK, '--no-terminal',
            # H.264 → the stateful v4l2m2m decoder (0 drops, ~8% CPU at 1080p,
            # verified 2026-10-09); HEVC → the stateless 'drm' path. auto-safe
            # alone picked software decode.
            '--hwdec=v4l2m2m,drm,v4l2m2m-copy,drm-copy,auto-safe',
            # Vulkan can't import the decoder's DMA-BUFs on the Pi 4 (482
            # 'mapping DRM dmabuf failed' in 8 s); OpenGL maps them cleanly.
            # profile=fast on the real TV (1080p H.264, 2026-10-09): default gpu
            # dropped 102 frames in 12 s, fast dropped 0-3. dmabuf-wayland also
            # dropped 0 but shows no picture for software-decoded video.
            '--vo=gpu', '--gpu-api=opengl', '--profile=fast', '--keep-open=no', '--cursor-autohide=always',
            '--audio-display=no', '--osd-level=1', '--osd-font-size=40',
            '--ytdl-format=' + YTDL_FORMAT,
            '--script-opts=ytdl_hook-ytdl_path=' + os.path.join(VENV_BIN, 'yt-dlp'),
            '--screenshot-format=jpg', '--screenshot-jpeg-quality=85',
        ]
        try:
            self.proc = subprocess.Popen(cmd, env=_session_env(),
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            print('player: mpv is not installed', flush=True)
            self.proc = None
            return
        with open(MPV_PIDFILE, 'w') as f:
            f.write(str(self.proc.pid))
        for _ in range(50):
            if os.path.exists(MPV_SOCK):
                break
            time.sleep(0.1)

    @staticmethod
    def _reap_orphan():
        """An mpv left by a previous receiver (KillMode=process keeps it alive)."""
        try:
            with open(MPV_PIDFILE) as f:
                pid = int(f.read().strip())
            with open('/proc/%d/comm' % pid) as f:
                if f.read().strip() == 'mpv':
                    os.kill(pid, 15)
                    time.sleep(0.5)
        except (OSError, ValueError):
            pass

    def command(self, *args, timeout=3.0):
        with self.lock:
            self.ensure()
            self.req_id += 1
            rid = self.req_id
            msg = json.dumps({'command': list(args), 'request_id': rid}) + '\n'
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                try:
                    s.connect(MPV_SOCK)
                except OSError as exc:          # mpv missing or not up yet: the next command retries
                    return {'error': 'mpv unreachable: %s' % exc}
                s.sendall(msg.encode())
                buf = b''
                deadline = time.time() + timeout
                while time.time() < deadline:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b'\n' in buf:
                        line, buf = buf.split(b'\n', 1)
                        try:
                            reply = json.loads(line)
                        except ValueError:
                            continue
                        if reply.get('request_id') == rid:
                            return reply
            finally:
                s.close()
        return {'error': 'timeout'}

    def get(self, prop, default=None):
        reply = self.command('get_property', prop)
        if reply.get('error') == 'success':
            return reply.get('data')
        return default

    def status(self):
        idle = self.get('idle-active', True)
        if idle:
            return {'active': False}
        return {
            'active': True,
            'path': self.get('path'),
            'title': self.get('media-title'),
            'position': self.get('time-pos'),
            'duration': self.get('duration'),
            'paused': self.get('pause', False),
            'buffering': self.get('paused-for-cache', False),
        }


PLAYER = Player()


# --------------------------------------------------------------------------- #
# Chromium (kiosk) over the DevTools HTTP endpoint
# --------------------------------------------------------------------------- #
def _cdp(path, method='GET'):
    req = urllib.request.Request(CDP + path, method=method)
    with urllib.request.urlopen(req, timeout=8) as r:
        body = r.read().decode() or 'null'
    try:
        return json.loads(body)
    except ValueError:
        return body


def _browser_up():
    try:
        _cdp('/json/version')
        return True
    except Exception:
        return False


_browser_lock = threading.Lock()


def ensure_browser():
    with _browser_lock:
        if _browser_up():
            return
        os.makedirs(BROWSER_PROFILE, exist_ok=True)
        cmd = [
            'chromium', '--kiosk', '--ozone-platform=wayland', '--lang=en-US',
            '--user-data-dir=' + BROWSER_PROFILE,
            '--remote-debugging-port=9222', '--remote-debugging-address=127.0.0.1',
            '--noerrdialogs', '--disable-infobars', '--no-first-run',
            '--password-store=basic', '--disable-session-crashed-bubble',
            '--hide-crash-restore-bubble', '--check-for-update-interval=31536000',
            '--autoplay-policy=no-user-gesture-required',
            '--disable-features=Translate,MediaRouter', HOME_URL,
        ]
        subprocess.Popen(cmd, env=_session_env(), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            if _browser_up():
                return
            time.sleep(0.5)


def _pages():
    return [t for t in _cdp('/json/list') if t.get('type') == 'page']


def _is_home(tab):
    url = tab.get('url', '')
    return url == HOME_URL or url.startswith(HOME_URL + '#') or url.startswith(HOME_URL + '?')


# Websites get play/pause/skip on their own <video>: media keys meant nothing to
# Tubi's site (the page just scrolled).
VIDEO_JS = """(() => {
  const vids = [...document.querySelectorAll('video')].filter((v) => v.readyState > 0 || v.currentSrc);
  if (!vids.length) return 'none';
  const v = vids.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
  const act = %s;
  if (act === 'playpause') { if (v.paused) { v.play(); return 'play'; } v.pause(); return 'pause'; }
  const end = isFinite(v.duration) ? v.duration - 1 : Infinity;
  v.currentTime = Math.max(0, Math.min(end, v.currentTime + (act === 'rewind' ? -10 : 10)));
  return 'seek';
})()"""


def _page_eval(page, expression, timeout=4):
    """Run JS in one tab over CDP (no activation, so nothing jumps on screen)."""
    from websockets.sync.client import connect
    with connect(page['webSocketDebuggerUrl'], open_timeout=3, close_timeout=1, max_size=None) as ws:
        ws.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate',
                            'params': {'expression': expression, 'returnByValue': True, 'userGesture': True}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.time())))
            if msg.get('id') == 1:
                return ((msg.get('result') or {}).get('result') or {}).get('value')
    return None


def browser_video(action):
    """Drive the open site's video through CDP; None when there's no video to drive."""
    page = next((t for t in _pages() if not _is_home(t) and t.get('webSocketDebuggerUrl')), None)
    if not page:
        return None
    value = _page_eval(page, VIDEO_JS % json.dumps(action))
    return None if value in (None, 'none') else value


def browser_home():
    ensure_browser()
    pages = _pages()
    home = next((t for t in pages if _is_home(t)), None)
    if home is None:
        home = _cdp('/json/new?' + urllib.parse.quote(HOME_URL, safe=''), method='PUT')
    for t in pages:
        if t['id'] != home['id']:
            _cdp('/json/close/' + t['id'])
    _cdp('/json/activate/' + home['id'])


def _site_open():
    return any(not _is_home(t) for t in _pages())


def browser_fresh_home():
    """Replace the home tab with a fresh load (after a deploy the old page stays cached)."""
    ensure_browser()
    pages = _pages()
    old = [t for t in pages if _is_home(t)]
    if old and (any(not _is_home(t) for t in pages) or YOUTUBE.pid()):
        # A site is on screen (Tubi mid-episode), or YouTube's own window is: a
        # new tab would jump in front of it (2026-10-10, a redeploy put the menus
        # over YouTube and left it running hidden), so reload the home tab
        # quietly where it sits.
        try:
            _page_eval(old[0], 'location.replace(%s)' % json.dumps(HOME_URL))
        except Exception as exc:
            print('fresh home (behind a site):', exc, flush=True)
        return
    fresh = _cdp('/json/new?' + urllib.parse.quote(HOME_URL, safe=''), method='PUT')
    for t in old:
        _cdp('/json/close/' + t['id'])
    if YOUTUBE.pid() is None:
        _cdp('/json/activate/' + fresh['id'])


def browser_open(url):
    ensure_browser()
    for t in _pages():
        if not _is_home(t):
            _cdp('/json/close/' + t['id'])
    _cdp('/json/new?' + urllib.parse.quote(url, safe=''), method='PUT')


def _main_browser_pid(profile):
    """The browser process (not a renderer) running with --user-data-dir=profile."""
    # Chromium rewrites its argv into one space-joined string, so match text,
    # not argv elements.
    flag = re.compile(rb'--user-data-dir=' + re.escape(profile.encode()) + rb'(\s|\x00|$)')
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % entry, 'rb') as f:
                cmdline = f.read()
        except OSError:
            continue
        if flag.search(cmdline) and b'--type=' not in cmdline:
            return int(entry)
    return None


class YouTubeTV:
    def __init__(self):
        self.lock = threading.Lock()
        self.started_at = 0.0

    def pid(self):
        return _main_browser_pid(YOUTUBE_PROFILE)

    def open(self):
        with self.lock:
            # One still running sits behind the home page (open_named_app brings
            # the home tab forward first) or on YouTube's own exit screen, and
            # returning early here opened nothing (2026-10-10: backed out of a
            # video, then YouTube "didn't respond" twice). Start it fresh.
            self._stop()
            os.makedirs(YOUTUBE_PROFILE, exist_ok=True)
            cmd = [
                'chromium', '--kiosk', '--ozone-platform=wayland', '--lang=en-US',
                '--user-data-dir=' + YOUTUBE_PROFILE, '--user-agent=' + YOUTUBE_TV_UA,
                '--noerrdialogs', '--disable-infobars', '--no-first-run',
                '--password-store=basic', '--disable-session-crashed-bubble',
                '--hide-crash-restore-bubble', '--check-for-update-interval=31536000',
                '--autoplay-policy=no-user-gesture-required',
                '--disable-features=Translate,MediaRouter', YOUTUBE_TV_URL,
            ]
            subprocess.Popen(cmd, env=_session_env(), start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.started_at = time.time()

    def close(self):
        with self.lock:
            self._stop()

    def _stop(self):
        pid = self.pid()
        if not pid:
            return
        try:
            os.kill(pid, 15)
        except OSError:
            return
        for _ in range(30):
            if not self.pid():
                return
            time.sleep(0.1)
        try:
            os.kill(pid, 9)
        except OSError:
            pass


YOUTUBE = YouTubeTV()


def _proc_starts(*names):
    """Start time (clock ticks since boot) of the newest host process with each
    name; None when there's none. Waydroid's Android processes run on the host
    kernel, so /proc sees them."""
    found = dict.fromkeys(names)
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/comm') as f:
                name = f.read().strip()
            if name not in found:
                continue
            with open(f'/proc/{pid}/stat') as f:
                start = int(f.read().rsplit(')', 1)[1].split()[19])   # field 22, starttime
        except (OSError, ValueError, IndexError):
            continue
        if found[name] is None or start > found[name]:
            found[name] = start
    return found


class Android:
    """Waydroid (Android 11 + Widevine L3) for the streaming apps' Android
    versions. Started on first use and then kept running: Home sends Android
    HOME (so a playing show pauses) and minimizes its window; opening it again
    just re-raises the window (wlrctl over labwc's foreign-toplevel protocol).
    Optional; setup notes: docs/android.md."""

    def __init__(self):
        self.lock = threading.Lock()
        self.visible = False
        self.opened_at = 0.0
        self.playing = None
        # What Android is busy with while the lock is held — 'opening',
        # 'starting' or 'restarting' — for the remote and the TV page.
        self.state = None
        self.state_since = 0.0
        # Home (or any other screen) asked for while an app opens: the open
        # stops at its next step. 2026-10-15: three Home presses made during a
        # 72 s restart waited behind the lock and closed Max the moment it opened.
        self.cancel = threading.Event()
        self.refreshed_on = None        # the morning refresh's date
        self.warming = False
        self.progress = None            # this open's steps (public)
        self.prepared_at = 0.0
        self._timings = _load_timings()

    def _set_state(self, state):
        self.state, self.state_since = state, time.time()

    def public(self):
        if not self.state:
            return None
        out = {'state': self.state, 'for': round(time.time() - self.state_since)}
        p = self.progress
        if p is not None:
            now = time.time()
            app = p['app'] or 'the app'
            out.update(app=p['app'], elapsed=round(now - p['started']), note=p['note'],
                       steps=[{'label': self.STEP_LABELS[s['key']].format(app=app),
                               'secs': round((s['t1'] or now) - s['t0']),
                               'usual': self._usual(s['key']),
                               'status': 'active' if s['t1'] is None else ('done' if s['ok'] else 'failed')}
                              for s in p['steps']])
        return out

    # ── An open, step by step, so a hang shows where it is. The TV page and the
    # phone remote draw these steps with a running clock against how long each
    # one usually takes.
    STEP_LABELS = {'boot': 'Starting Android', 'restart': 'Restarting Android',
                   'wake': 'Waking Android', 'close': 'Closing other apps',
                   'launch': 'Starting {app}', 'window': "Waiting for {app}'s picture"}
    STEP_USUAL = {'boot': 50, 'restart': 70, 'wake': 4, 'close': 3, 'launch': 4, 'window': 10}

    def _progress_begin(self, app):
        self.progress = {'app': app, 'started': time.time(), 'steps': [], 'note': None}

    def _step(self, key):
        """A new step begins (the one before it is done)."""
        p = self.progress
        if p is None:
            return
        self._step_done(True)
        p['steps'].append({'key': key, 't0': time.time(), 't1': None, 'ok': None})

    def _step_done(self, ok=True, note=None):
        p = self.progress
        if p is None:
            return
        if note:
            p['note'] = note
        if p['steps'] and p['steps'][-1]['t1'] is None:
            step = p['steps'][-1]
            step['t1'], step['ok'] = time.time(), ok
            if ok:
                self._learn(step['key'], step['t1'] - step['t0'])

    def _current_step(self):
        p = self.progress
        return p['steps'][-1]['key'] if p and p['steps'] and p['steps'][-1]['t1'] is None else None

    def _usual(self, key):
        return round(self._timings.get(key, self.STEP_USUAL.get(key, 10)))

    def _learn(self, key, secs):
        """A running average of real opens, kept across restarts."""
        old = self._timings.get(key, self.STEP_USUAL.get(key, secs))
        self._timings[key] = round(0.7 * old + 0.3 * secs, 1)
        try:
            with open(TIMINGS_PATH, 'w') as f:
                json.dump(self._timings, f)
        except OSError:
            pass

    def session_age(self):
        """Seconds since Android's system_server started; None when it isn't running."""
        start = _proc_starts('system_server')['system_server']
        if start is None:
            return None
        try:
            with open('/proc/uptime') as f:
                up = float(f.read().split()[0])
        except (OSError, ValueError):
            return None
        return up - start / os.sysconf('SC_CLK_TCK')

    def _run(self, *cmd, timeout=30):
        return subprocess.run(list(cmd), env=_session_env(), capture_output=True,
                              text=True, timeout=timeout)

    def _wlr(self, *args):
        try:
            return self._run('wlrctl', 'toplevel', *args, timeout=10).returncode == 0
        except FileNotFoundError:          # wlrctl not installed: nothing can be found or moved
            return False

    def windows(self):
        """Android's windows: the full-UI 'Waydroid' one and/or per-app
        'waydroid.<package>' ones (apps launched by package get their own)."""
        try:
            out = self._run('wlrctl', 'toplevel', 'list', timeout=10).stdout
        except FileNotFoundError:
            return []
        return [l.split(':', 1)[0].strip() for l in out.splitlines()
                if l.split(':', 1)[0].strip().lower().startswith('waydroid')]

    def detect(self):
        """After a receiver restart: is Android in front? Asked by focus — an
        app can own several windows (Prime: browse + player), some minimized,
        and 'any window minimized' misread a playing episode as hidden (a
        redeploy once raised the home screen over a playing show)."""
        self.visible = any(self._wlr('find', 'app_id:' + w, 'state:active')
                           for w in set(self.windows()))
        return self.visible

    def _front(self, app_id):
        self._wlr('focus', 'app_id:' + app_id)
        self._wlr('fullscreen', 'app_id:' + app_id, 'state:active')

    def minimize_all(self):
        for w in self.windows():
            self._wlr('minimize', 'app_id:' + w)

    def close_all(self):
        """Stop the streaming apps and close every Android window: leftover
        windows of an app's earlier run took focus over the new one (a Max
        relaunch showed Android's home screen), and closing guarantees no sound
        keeps playing behind the TV's home page. The services remember where you
        were in a show. One shell round-trip for all of them: each costs 1-4 s
        on the Pi, and Home waits behind this."""
        self._run('sudo', '-n', 'waydroid', 'shell', '--', 'sh', '-c',
                  '; '.join('am force-stop ' + pkg for pkg in ANDROID_INSTALLED), timeout=20)
        for w in set(self.windows()):
            for _ in range(4):
                if not self._wlr('find', 'app_id:' + w):
                    break
                self._wlr('close', 'app_id:' + w)
                time.sleep(0.3)

    def booted(self):
        try:
            return self._run('waydroid', 'prop', 'get', 'sys.boot_completed', timeout=10).stdout.strip() == '1'
        except Exception:
            return False

    def _network_ready(self):
        out = self._run('sudo', '-n', 'waydroid', 'shell', '--', 'sh', '-c',
                        'dumpsys connectivity | grep -m1 -c "NetworkAgentInfo.*VALIDATED"', timeout=15).stdout
        return out.strip() not in ('', '0')

    def _ensure_booted(self):
        status = self._run('waydroid', 'status', timeout=15).stdout
        started = 'RUNNING' not in status.split('Session:')[-1].split('\n')[0]
        if started:
            if self.state == 'opening':
                self._set_state('starting')     # a cold boot under an open: about a minute
            if self._current_step() != 'restart':
                self._step('boot')
            subprocess.Popen(['waydroid', 'session', 'start'], env=_session_env(),
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(60):
                if self.booted():
                    break
                time.sleep(1.5)
            else:
                return False
            if started:
                # Android's network comes up a few seconds after boot; Pluto opened
                # in that gap said "An internet connection is required" (2026-10-11).
                for _ in range(20):
                    if self._network_ready():
                        break
                    time.sleep(1)
            return True
        finally:
            if self.state == 'starting':
                self._set_state('opening')

    def graphics_broken(self):
        """Android can no longer make windows. 2026-10-11 07:07: the hardware
        composer aborted, surfaceflinger died with it and init restarted it, but
        system_server kept its dead connection, so every launch failed
        (createSurface: Broken pipe) and the TV sat on "Opening Pluto TV…". In a
        healthy session surfaceflinger starts before system_server."""
        starts = _proc_starts('surfaceflinger', 'system_server')
        if starts['system_server'] is None:
            return False            # not running, or still booting
        return starts['surfaceflinger'] is None or starts['surfaceflinger'] > starts['system_server']

    def restart_session(self, reason='graphics broken (surfaceflinger restarted under system_server)'):
        """Stop and start Android (~20 s to boot, then its network; ~70 s in
        all on 2026-10-15). Caller holds the lock."""
        print(f'android: {reason}; restarting the session', flush=True)
        before = self.state
        own = self.progress is None         # a background restart: its own one-step progress
        if own:
            self._progress_begin(None)
        self._step('restart')
        self._set_state('restarting')
        self.visible = False
        ok = False
        try:
            self._run('waydroid', 'session', 'stop', timeout=60)
            ok = self._ensure_booted()
        finally:
            self._step_done(ok, None if ok else "Android didn't come back after the restart.")
            self._set_state(before)
            if own:
                self.progress = None
        print('android: session restart', 'done' if ok else 'FAILED to boot', flush=True)
        return ok

    def nudge(self, timeout=20):
        """A WAKEUP key: counts as user activity, so Android's 60 s screen
        timeout starts over (it does nothing else when Android is awake).
        Under first-boot load the key can be slow to land: 2026-10-16 05:03 one
        hadn't after 20 s, Android slept meanwhile and froze the command mid-way.
        Says False then instead of raising, so neither the warm-up nor an open
        dies of it (`wake` thaws and wakes it again)."""
        try:
            self._run('sudo', '-n', 'waydroid', 'shell', '--', 'input', 'keyevent', '224', timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            print('android: WAKEUP key timed out (busy, or frozen mid-command)', flush=True)
            return False

    def wake(self, timeout=20):
        """Thaw Android and turn its screen on before opening anything. Parked
        Android is asleep, and a launch alone doesn't wake it: 2026-10-15 15:37
        Max was launched into a sleeping Android, Waydroid froze it again 21 s
        later ("Received transaction: 3"), and no window ever came. Thawed
        first, the `waydroid shell` calls of an open don't freeze it again
        between commands either (each thaws a frozen container only for itself)."""
        try:
            self._run('sudo', '-n', 'waydroid', 'container', 'unfreeze', timeout=20)
        except subprocess.TimeoutExpired:
            print('android: unfreeze timed out', flush=True)
        return self.nudge(timeout)

    def _wake_or_restart(self):
        """Thaw and wake Android before opening. A session that has been up a
        while and won't take the wake-up key is wedged: 2026-10-20 20:21 the key
        timed out, the open went ahead, and the display crashed 75 s later, so
        the restart only began then. A fresh session, busy with its first-boot
        work, can be slow to answer (2026-10-16 05:03), so it gets the time."""
        self._step('wake')
        age = self.session_age()
        settled = age is not None and age > SESSION_SETTLED
        if self.wake(12 if settled else 20) or not settled:
            return
        self._step_done(False, "Android stopped answering, so it's restarting.")
        if self.restart_session(reason="Android stopped answering (the wake-up key didn't land)"):
            self._step('wake')
            self.wake()

    def prepare(self):
        """The Streaming page is up: get Android ready while an app is chosen. Start
        it if it isn't running, wake it, and restart it now (not after an app is
        picked) if its display has crashed or it won't answer. At most once a
        minute; never while it's on screen, warming up, or busy."""
        if self.visible or self.warming or time.time() - self.prepared_at < 60:
            return False
        if not self.lock.acquire(blocking=False):
            return False
        self.prepared_at = time.time()
        try:
            status = self._run('waydroid', 'status', timeout=15).stdout
            if 'RUNNING' not in status.split('Session:')[-1].split('\n')[0]:
                _background_restart("the Streaming page is up and Android isn't running")
            elif self.graphics_broken():
                _background_restart('graphics broken (found while the Streaming page was up)')
            elif not self.wake(12) and (self.session_age() or 0) > SESSION_SETTLED:
                _background_restart("Android didn't answer (found while the Streaming page was up)")
            return True
        finally:
            self.lock.release()

    def warm_up(self, longest=15 * 60, shortest=4 * 60, settled_load=3.0):
        """Keep a fresh session awake until its first-boot work is done. Left
        alone, Android sleeps at its 60 s screen timeout and Waydroid freezes it
        mid-way (2026-10-15 15:04, 64 s after boot: Play services and dex2oat
        still going), so the work waits for the next app opened. Kept awake it
        ran ~10 minutes (Pi load up to ~18) and settled, with nothing on the TV.
        A WAKEUP key every 25 s counts as activity; when they stop, Android
        sleeps 60 s later and Waydroid freezes it. (Sending SLEEP from a shell
        froze that shell's own command mid-way, so it hung until the next thaw.)
        Every 40 s left too little room: at load ~18 a key took over 20 s to
        land, Android slept, and the warm-up died 2.5 min in (2026-10-16 05:03).
        After a key that didn't land, the next round thaws and wakes it again.
        Runs without the lock, and steps aside if an app is opened meanwhile."""
        if self.warming:
            return
        self.warming = True
        start = time.time()

        try:
            awake = self.wake()
            while time.time() - start < longest:
                time.sleep(25)
                if self.visible or self.lock.locked():
                    break                   # someone's using Android: it's awake anyway
                if time.time() - start >= shortest and os.getloadavg()[0] < settled_load:
                    break
                awake = self.nudge() if awake else self.wake()
        finally:
            self.warming = False
            print(f'android: warmed up for {round((time.time() - start) / 60, 1)} min '
                  f'(load {os.getloadavg()[0]:.1f}); it sleeps in a minute', flush=True)

    def _launch(self, package):
        # Waydroid's own launch makes a fullscreen task; a plain `am start`
        # (even with --windowingMode 1) lands in a freeform window with a
        # caption bar on this build (2026-10-09, Max looked like a browser).
        try:
            self._run('waydroid', 'app', 'launch', package, timeout=20)
        except subprocess.TimeoutExpired:
            pass                    # a slow Android may still open it: the window wait decides

    def _wait_window(self, package, seconds=40):
        """Wait for the app's own 'waydroid.<package>' window. False when it
        doesn't come, or as soon as Android's graphics die (checked every 5 s).
        2026-10-12 19:20: Android had sat frozen for 34 hours, and Prime's
        window didn't come within the old 15 s wait, so the TV just went back to
        the Streaming page; Max, opened next, met a hardware composer abort."""
        deadline = time.time() + seconds
        next_check = time.time() + 5
        while time.time() < deadline and not self.cancel.is_set():
            if self._wlr('find', 'app_id:waydroid.' + package):
                return True
            if time.time() >= next_check:
                if self.graphics_broken():
                    return False
                next_check = time.time() + 5
            time.sleep(0.5)
        return False

    def open(self, package=None):
        """True when the app (or Android's own screen) is in front; False when
        Home or another screen was asked for on the way (`cancel`)."""
        with self.lock:
            self.cancel.clear()
            self._set_state('opening')
            self._progress_begin(ANDROID_LABELS.get(package, 'Android') if package else 'Android')
            try:
                return self._open(package) and not self._cancelled()
            finally:
                self._set_state(None)
                self.progress = None

    def _cancelled(self):
        """Home came while this open ran: put Android away instead of finishing."""
        if not self.cancel.is_set():
            return False
        print('android: open cancelled (Home or another screen)', flush=True)
        self.close_all()
        self.visible = False
        return True

    def _open(self, package):
        app = ANDROID_LABELS.get(package, 'the app')
        if not self._ensure_booted():
            self._step_done(False, "Android didn't finish starting.")
            raise RuntimeError('Android did not finish booting')
        if self._cancelled():
            return False
        if self.graphics_broken():
            self._step_done(True, "Android's display had crashed, so it's restarting.")
            if not self.restart_session():
                raise RuntimeError('Android did not come back after a restart')
        if self._cancelled():
            return False
        self._wake_or_restart()
        target = 'Waydroid'
        opened = True
        if not package and not self._wlr('find', 'app_id:Waydroid'):
            subprocess.Popen(['waydroid', 'show-full-ui'], env=_session_env(),
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(40):
                if self._wlr('find', 'app_id:Waydroid') or self.cancel.is_set():
                    break
                time.sleep(0.5)
        if package:
            # A clean slate: one streaming app at a time (a Pi 4 with two or
            # three running ANR'd), and no leftover windows to steal focus.
            self._step('close')
            self.close_all()
            self._step('launch')
            self._launch(package)
            self._step('window')
            opened = self._wait_window(package)
            if self._cancelled():
                return False
            if not opened and self.graphics_broken():
                self._step_done(False, f"Android's display crashed while {app} was opening, so it's restarting.")
                if self.restart_session():
                    # once more, on a fresh session
                    if self._cancelled():
                        return False
                    self._wake_or_restart()
                    self._step('launch')
                    self._launch(package)
                    self._step('window')
                    opened = self._wait_window(package)
            self._step_done(opened, None if opened else f"{app} didn't show its picture.")
            # Waydroid maps the app to its own 'waydroid.<package>' window;
            # focus THAT one so the fullscreen lands on it.
            if opened:
                target = 'waydroid.' + package
            else:
                print(f'android: {package} opened no window', flush=True)
        if self._cancelled():
            return False
        self._front(target)
        # Still "in front" when the window is late: the janitor fullscreens it
        # if it comes, and tucks Android away if Android's home shows instead.
        self.visible = True
        self.opened_at = time.time()
        if not opened:
            raise RuntimeError("The app didn't open. Try it again.")
        return True

    def poll(self):
        """One shell round-trip while Android is in front: is its home screen
        showing (an app was exited), and is the active media session playing?
        Sets self.playing to True/False/None (no session)."""
        out = self._run('sudo', '-n', 'waydroid', 'shell', '--', 'sh', '-c',
                        'dumpsys activity activities | grep -m1 mResumedActivity; '
                        'dumpsys media_session | grep -E "active=|state=PlaybackState"',
                        timeout=15).stdout
        lines = out.splitlines()
        resumed = lines[0] if lines else ''
        playing, active = None, False
        for line in lines[1:]:
            line = line.strip()
            if line.startswith('active='):
                active = line == 'active=true'
            elif active and 'state=PlaybackState' in line:
                m = re.search(r'state=PlaybackState \{state=(\d+)', line)
                if m:
                    playing = m.group(1) == '3'   # STATE_PLAYING
                    break
        self.playing = playing
        # LineageOS's home screen — not just any activity with "Launcher" in
        # its name (Prime Video's entry screen is LauncherActivity).
        return 'com.android.launcher3/' in resumed

    def key(self, keycode):
        self._run('sudo', '-n', 'waydroid', 'shell', '--', 'input', 'keyevent', str(keycode), timeout=15)

    def hide(self):
        """Put Android away. While an app is still opening, or Android is
        restarting, this doesn't wait: it tells the open to stop, so Home answers
        at once instead of closing the app a minute later."""
        if not self.lock.acquire(blocking=False):
            self.cancel.set()
            return
        try:
            if not self.visible:
                return
            self.close_all()
            self.visible = False
        finally:
            self.lock.release()


ANDROID = Android()

# Android gets a fresh session each morning while nobody uses the TV. The
# hardware composer crash (2026-10-11, -12, -15) came after Android had sat
# frozen 34 h and 65 h (a 19 h stretch was fine), and once after ~13 h running;
# a session never more than a day old, with its slow first minutes over before
# anyone sits down, keeps that out of the evening.
MORNING_REFRESH_HOURS = (5, 7)      # 05:00-07:00; skipped that day if the TV is busy throughout
MORNING_REFRESH_QUIET = 30 * 60     # the remote untouched this long


def _background_restart(reason):
    """Restart Android while nothing of it is on screen, then let it finish its
    first-boot work awake. Caller holds the lock; the warm-up runs after it's released."""
    if ANDROID.restart_session(reason=reason):
        threading.Timer(1, ANDROID.warm_up).start()


# Optional (config.json "nightly_reboot": true): the whole box restarts once a
# night while nobody uses the TV. On a Pi 4 running Android, after 11 days up
# memory was so tight (swap ~1 GB, 340 MB free) that the kernel couldn't hand
# the graphics their buffers fast enough, and Android's display crashed four
# times in ten minutes. The morning refresh then starts a fresh Android on a
# fresh box. Without Android the box has never needed it.
NIGHTLY_REBOOT_HOURS = (4, 5)       # 04:00-05:00, before the 05:00 morning refresh
NIGHTLY_REBOOT_MIN_UPTIME = 20 * 3600


def _uptime():
    try:
        with open('/proc/uptime') as f:
            return float(f.read().split()[0])
    except (OSError, ValueError):
        return 0.0


def _nightly_reboot_due(now=None):
    if not CONFIG.get('nightly_reboot'):
        return False
    now_t = time.localtime(now)
    if not NIGHTLY_REBOOT_HOURS[0] <= now_t.tm_hour < NIGHTLY_REBOOT_HOURS[1]:
        return False
    if _uptime() < NIGHTLY_REBOOT_MIN_UPTIME or ANDROID.visible or ANDROID.lock.locked():
        return False
    if time.time() - REMOTE_USED['at'] < MORNING_REFRESH_QUIET:
        return False
    return not _tv_busy()


def _morning_refresh_due(now=None):
    now = time.localtime(now)
    if not MORNING_REFRESH_HOURS[0] <= now.tm_hour < MORNING_REFRESH_HOURS[1]:
        return False
    if ANDROID.refreshed_on == time.strftime('%Y-%m-%d', now) or ANDROID.visible:
        return False
    if time.time() - REMOTE_USED['at'] < MORNING_REFRESH_QUIET:
        return False
    return not _tv_busy()


def _android_janitor():
    """Android's launcher is never the TV's home: when an app is exited and Android
    lands on its home screen, tuck Android away so the Streaming page shows.
    Every 30 s it also checks that Android can still make windows, and restarts
    a broken session while nothing of Android's is on screen, and gives Android
    a fresh start each morning (`_morning_refresh_due`). Without Waydroid it
    only keeps the (opt-in) nightly reboot."""
    ticks = 0
    while True:
        time.sleep(2)
        ticks += 1
        try:
            if ticks % 30 == 0 and _nightly_reboot_due():
                print('janitor: the nightly restart of the box (memory never builds up for days)', flush=True)
                subprocess.Popen(['sudo', '-n', 'systemctl', 'reboot'])
            if not android_available():
                continue
            if ticks % 15 == 0 and not ANDROID.visible and ANDROID.graphics_broken() \
                    and ANDROID.lock.acquire(blocking=False):
                try:
                    _background_restart('graphics broken (surfaceflinger restarted under system_server)')
                finally:
                    ANDROID.lock.release()
            if ticks % 15 == 0 and _morning_refresh_due() and ANDROID.lock.acquire(blocking=False):
                try:
                    ANDROID.refreshed_on = time.strftime('%Y-%m-%d')   # once a day, even if it fails
                    _background_restart('morning refresh')
                finally:
                    ANDROID.lock.release()
            if ANDROID.visible and ticks % 12 == 0:
                # On screen, Android never sleeps: at its 60 s timeout with no
                # input (Max's menus, a paused show) Waydroid would freeze it
                # under the viewer. Off screen, the nudges stop and it sleeps a minute later.
                # Every ~25 s, so a slow key still lands in time (see warm_up).
                ANDROID.nudge() or ANDROID.wake()
            if ANDROID.visible:
                # Apps open new windows as they go (Prime's player is its own
                # task/window); an un-fullscreened one sat 36 px down, showing
                # the previous screen as a strip across the top.
                # Match the FOCUSED window: an app can own several (Prime: four),
                # and "is any of them fullscreen?" was true while the new player
                # window sat below a strip of the previous screen.
                for w in set(ANDROID.windows()):
                    if (ANDROID._wlr('find', 'app_id:' + w, 'state:active')
                            and not ANDROID._wlr('find', 'app_id:' + w, 'state:active', 'state:fullscreen')):
                        ANDROID._wlr('fullscreen', 'app_id:' + w, 'state:active')
            if ANDROID.visible and ANDROID.poll() and time.time() - ANDROID.opened_at > 10:
                # Closed, not just minimized (2026-10-20): a hidden Max held ~200 MB
                # of the Pi's 512 MB graphics memory, and the display crashed for want of it.
                with ANDROID.lock:
                    ANDROID.close_all()
                    ANDROID.visible = False
                INPUT.park()
        except Exception as exc:
            print('android janitor:', exc, flush=True)


def _youtube_janitor():
    """A YouTube window left running behind the TV's own menus (its Back-out
    leaves the process alive) keeps using the Pi, and the status said YouTube
    was on while the Streaming page was in front. Close it once the menus have
    been in front for ~30 s; a YouTube that's still starting up gets a grace
    period. The menus' window is found by its title: the home page's <title>
    ("Hearth") + " - Chromium"."""
    strikes = 0
    while True:
        time.sleep(10)
        try:
            if (YOUTUBE.pid() and time.time() - YOUTUBE.started_at > 30
                    and ANDROID._wlr('find', 'title:Hearth - Chromium', 'state:active')):
                strikes += 1
                if strikes >= 3:
                    print('youtube janitor: closing YouTube hidden behind the menus', flush=True)
                    YOUTUBE.close()
                    strikes = 0
            else:
                strikes = 0
        except Exception as exc:
            print('youtube janitor:', exc, flush=True)


def open_named_app(name):
    """Shared by the remote (/app) and the TV home tiles (/local/open)."""
    if name == 'android' and not android_available():
        raise RuntimeError(NO_ANDROID)
    PLAYER.command('stop')
    prefer_hdmi()
    if name == 'android' or (name in ANDROID_APPS and android_available()):
        YOUTUBE.close()
        try:
            browser_home()
        except Exception as exc:
            print('browser_home (android open):', exc, flush=True)
        if not ANDROID.open(ANDROID_APPS.get(name)):
            return None             # Home came first: the open stopped
        INPUT.unpark()
        return 'android:' + ANDROID_APPS.get(name, '')
    ANDROID.hide()
    if name == 'youtube':
        browser_home()
        YOUTUBE.open()
        INPUT.park()
        return YOUTUBE_TV_URL
    YOUTUBE.close()
    browser_open(APPS[name])
    INPUT.unpark()
    return APPS[name]


def _pointer_free_screen():
    """Home, YouTube TV and the player are driven by keys; browser sites need a pointer."""
    if ANDROID.visible:
        return False
    if YOUTUBE.pid() or PLAYER.status().get('active'):
        return True
    try:
        return not any(not _is_home(t) for t in _pages())
    except Exception:
        return False


def _pointer_janitor():
    """Re-park the pointer 20 s after the touchpad last moved it, on key-driven screens."""
    while True:
        time.sleep(5)
        try:
            if (not INPUT.parked and time.time() - INPUT.last_user_move > 20
                    and _pointer_free_screen()):
                INPUT.park()
        except Exception as exc:
            print('pointer janitor:', exc, flush=True)


def browser_status():
    if ANDROID.visible:
        return {'running': True, 'app': 'android', 'url': None, 'title': 'Android'}
    if YOUTUBE.pid():
        return {'running': True, 'app': 'youtube', 'url': YOUTUBE_TV_URL, 'title': 'YouTube'}
    if not _browser_up():
        return {'running': False}
    pages = [t for t in _pages() if not _is_home(t)]
    if not pages:
        return {'running': True, 'app': None}
    t = pages[0]
    name = next((k for k, v in APPS.items()
                 if urllib.parse.urlparse(v).netloc.split('.')[-2]
                 in urllib.parse.urlparse(t.get('url', '')).netloc), None)
    return {'running': True, 'app': name, 'url': t.get('url'), 'title': t.get('title')}


# --------------------------------------------------------------------------- #
# Virtual keyboard + mouse
# --------------------------------------------------------------------------- #
from evdev import UInput, ecodes as E  # noqa: E402

_SHIFTED = {
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7',
    '*': '8', '(': '9', ')': '0', '_': '-', '+': '=', '{': '[', '}': ']',
    '|': '\\', ':': ';', '"': "'", '<': ',', '>': '.', '?': '/', '~': '`',
}
_CHAR_KEYS = {
    '-': E.KEY_MINUS, '=': E.KEY_EQUAL, '[': E.KEY_LEFTBRACE, ']': E.KEY_RIGHTBRACE,
    '\\': E.KEY_BACKSLASH, ';': E.KEY_SEMICOLON, "'": E.KEY_APOSTROPHE,
    ',': E.KEY_COMMA, '.': E.KEY_DOT, '/': E.KEY_SLASH, '`': E.KEY_GRAVE,
    ' ': E.KEY_SPACE, '\n': E.KEY_ENTER, '\t': E.KEY_TAB,
}
NAMED_KEYS = {
    'up': [E.KEY_UP], 'down': [E.KEY_DOWN], 'left': [E.KEY_LEFT], 'right': [E.KEY_RIGHT],
    'enter': [E.KEY_ENTER], 'esc': [E.KEY_ESC], 'space': [E.KEY_SPACE],
    'tab': [E.KEY_TAB], 'shift_tab': [E.KEY_LEFTSHIFT, E.KEY_TAB],
    'backspace': [E.KEY_BACKSPACE], 'f': [E.KEY_F], 'f11': [E.KEY_F11],
    'back': [E.KEY_LEFTALT, E.KEY_LEFT], 'pageup': [E.KEY_PAGEUP],
    'pagedown': [E.KEY_PAGEDOWN], 'reload': [E.KEY_F5],
    'playpause': [E.KEY_PLAYPAUSE], 'rewind': [E.KEY_REWIND], 'fastforward': [E.KEY_FASTFORWARD],
}
# The same three as Android keycodes (sent straight into Waydroid, which is
# more reliable than relaying them through the compositor).
ANDROID_MEDIA_KEYS = {'playpause': 85, 'rewind': 89, 'fastforward': 90}


def _char_stroke(ch):
    if ch.isalpha() and ch.isascii():
        code = getattr(E, 'KEY_' + ch.upper())
        return code, ch.isupper()
    if ch.isdigit():
        return getattr(E, 'KEY_' + ch), False
    if ch in _CHAR_KEYS:
        return _CHAR_KEYS[ch], False
    if ch in _SHIFTED:
        return _char_stroke(_SHIFTED[ch])[0], True
    return None, False


class Input:
    def __init__(self):
        keys = set()
        for combo in NAMED_KEYS.values():
            keys.update(combo)
        keys.update(_CHAR_KEYS.values())
        keys.update(getattr(E, 'KEY_' + c) for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789')
        keys.update([E.KEY_LEFTSHIFT, E.KEY_LEFTCTRL, E.KEY_LEFTALT])
        self.kbd = UInput({E.EV_KEY: sorted(keys)}, name='hearth-keyboard')
        self.mouse = UInput({
            E.EV_KEY: [E.BTN_LEFT, E.BTN_RIGHT, E.BTN_MIDDLE],
            E.EV_REL: [E.REL_X, E.REL_Y, E.REL_WHEEL, E.REL_HWHEEL],
        }, name='hearth-mouse')
        self.lock = threading.Lock()
        self.last_user_move = 0.0
        self.parked = False

    def combo(self, codes):
        with self.lock:
            for c in codes:
                self.kbd.write(E.EV_KEY, c, 1)
            self.kbd.syn()
            time.sleep(0.02)
            for c in reversed(codes):
                self.kbd.write(E.EV_KEY, c, 0)
            self.kbd.syn()
            time.sleep(0.02)

    def type_text(self, text):
        for ch in text[:500]:
            code, shift = _char_stroke(ch)
            if code is None:
                continue
            self.combo([E.KEY_LEFTSHIFT, code] if shift else [code])

    def move(self, dx, dy, user=True):
        with self.lock:
            self.mouse.write(E.EV_REL, E.REL_X, int(dx))
            self.mouse.write(E.EV_REL, E.REL_Y, int(dy))
            self.mouse.syn()
        if user:
            self.last_user_move = time.time()
            self.parked = False

    def park(self):
        """Banish the pointer to the bottom-right corner (labwc has no hide-
        cursor option; the compositor clamps it to the screen edge, leaving at
        most a pixel or two of arrow)."""
        for _ in range(4):
            self.move(2000, 2000, user=False)
        self.parked = True

    def unpark(self):
        """Back to mid-screen from the bottom-right corner — never via the top
        edge, where a sweeping pointer pulls Android's notification shade."""
        if not self.parked:
            return
        for _ in range(4):
            self.move(-240, -135, user=False)
        self.parked = False

    def click(self, button='left'):
        btn = {'left': E.BTN_LEFT, 'right': E.BTN_RIGHT, 'middle': E.BTN_MIDDLE}[button]
        with self.lock:
            self.mouse.write(E.EV_KEY, btn, 1)
            self.mouse.syn()
            time.sleep(0.03)
            self.mouse.write(E.EV_KEY, btn, 0)
            self.mouse.syn()

    def scroll(self, dy):
        with self.lock:
            self.mouse.write(E.EV_REL, E.REL_WHEEL, int(dy))
            self.mouse.syn()


INPUT = Input()


# --------------------------------------------------------------------------- #
# Audio (PipeWire via wpctl) — HDMI preferred whenever it's present
# --------------------------------------------------------------------------- #
def _wpctl(*args):
    try:
        return subprocess.run(['wpctl', *args], env=_session_env(), capture_output=True,
                              text=True, timeout=5).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print('wpctl %s: %s' % (args[0] if args else '', exc), flush=True)
        return ''


def audio_sinks():
    out = _wpctl('status')
    sinks, in_audio, in_sinks = [], False, False
    for line in out.splitlines():
        if line.startswith('Audio'):
            in_audio = True
        elif line.startswith('Video'):
            break
        if in_audio and 'Sinks:' in line:
            in_sinks = True
            continue
        if in_sinks:
            if 'Sources:' in line or 'Filters:' in line or 'Streams:' in line:
                break
            m = re.search(r'(\*)?\s+(\d+)\.\s+(.+?)\s+\[vol:\s*([\d.]+)', line)
            if m:
                sinks.append({'id': int(m.group(2)), 'name': m.group(3),
                              'default': bool(m.group(1)), 'volume': float(m.group(4))})
    return sinks


_audio_restarted_at = 0.0


def _hdmi_connected():
    for path in sorted(glob.glob('/sys/class/drm/card*-HDMI-A-*/status')):
        try:
            with open(path) as f:
                if f.read().strip() == 'connected':
                    return True
        except OSError:
            continue
    return False


def prefer_hdmi():
    """Route sound to the TV. PipeWire only offers the HDMI sink if the TV was
    connected when it probed the card (booted with the TV off or unplugged,
    only the headphone jack existed), so re-probe once when the TV is there
    but its sink isn't."""
    global _audio_restarted_at
    sinks = audio_sinks()
    hdmi = next((s for s in sinks if 'hdmi' in s['name'].lower()), None)
    if hdmi is None and _hdmi_connected() and time.time() - _audio_restarted_at > 120:
        _audio_restarted_at = time.time()
        subprocess.run(['systemctl', '--user', 'restart', 'wireplumber', 'pipewire', 'pipewire-pulse'],
                       env=_session_env(), timeout=20)
        time.sleep(4)
        sinks = audio_sinks()
        hdmi = next((s for s in sinks if 'hdmi' in s['name'].lower()), None)
    if hdmi and not hdmi['default']:
        _wpctl('set-default', str(hdmi['id']))


def volume_status():
    out = _wpctl('get-volume', '@DEFAULT_AUDIO_SINK@')
    m = re.search(r'Volume:\s*([\d.]+)', out)
    return {'volume': round(float(m.group(1)) * 100) if m else None,
            'muted': '[MUTED]' in out}


def _tv_busy():
    """Is anything on screen that could be making sound?"""
    if ANDROID.visible or YOUTUBE.pid() or PLAYER.status().get('active'):
        return True
    try:
        return any(not _is_home(t) for t in _pages())
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Routes — server-facing (token)
# --------------------------------------------------------------------------- #
def _browser_sound_flowing():
    """Is the browser (YouTube TV, a site) actually putting out sound right now?"""
    try:
        out = subprocess.run(['pw-dump'], env=_session_env(), capture_output=True, text=True, timeout=5).stdout
        for obj in json.loads(out or '[]'):
            info = obj.get('info') or {}
            props = info.get('props') or {}
            if (obj.get('type') == 'PipeWire:Interface:Node'
                    and props.get('media.class') == 'Stream/Output/Audio'
                    and 'chrom' in str(props.get('application.name', '')).lower()
                    and info.get('state') == 'running'):
                return True
    except Exception:
        return None
    return False


def _now_playing_flag(mode, player):
    """True/False/None — drives the remote's play/pause icon."""
    if mode == 'player':
        return not player.get('paused', False)
    if ANDROID.visible:
        return ANDROID.playing
    if mode == 'app':
        return _browser_sound_flowing()
    return None


@app.get('/status')
def status():
    player = PLAYER.status()
    browser = browser_status()
    mode = 'player' if player.get('active') else ('app' if browser.get('app') or browser.get('url') else 'home')
    return jsonify({'mode': mode, 'player': player, 'browser': browser,
                    'music': music_status(player),
                    'playing': _now_playing_flag(mode, player),
                    'audio': volume_status(),
                    'android': ANDROID.public(), 'android_available': android_available(),
                    'time': time.time()})


@app.post('/android/refresh')
def android_refresh():
    """The morning refresh, by hand: a fresh Android session (~70 s), then its
    warm-up (~10 min), in the background. Refused while Android is on screen or busy."""
    if not android_available():
        return jsonify({'ok': False, 'error': NO_ANDROID}), 409
    if ANDROID.visible or not ANDROID.lock.acquire(blocking=False):
        return jsonify({'ok': False, 'error': 'Android is in use'}), 409

    def run():
        try:
            _background_restart('refresh asked for')
        finally:
            ANDROID.lock.release()
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@app.post('/play')
def play():
    body = request.get_json(force=True)
    url = body.get('url', '')
    if not re.match(r'^https?://', url):
        abort(400)
    ANDROID.hide()
    YOUTUBE.close()
    try:
        browser_home()
    except Exception:
        pass
    prefer_hdmi()
    INPUT.park()
    PLAYER.command('set_property', 'force-media-title', body.get('title') or '')
    start = body.get('start')
    if start:
        reply = PLAYER.command('loadfile', url, 'replace', -1, 'start=%d' % int(start))
    else:
        reply = PLAYER.command('loadfile', url, 'replace')
    PLAYER.command('set_property', 'pause', False)
    return jsonify({'ok': reply.get('error') == 'success', 'mpv': reply})


# Music: the server sends a queue of tracks (Jellyfin's original files); mpv plays
# it as a playlist with no window (--force-window=no, --audio-display=no), so the
# TV's own page shows Now Playing from MUSIC + mpv's playlist-pos.
MUSIC = {'queue': []}
MUSIC_LIST = '/tmp/hearth-music.m3u'


@app.post('/music/play')
def music_play():
    body = request.get_json(force=True)
    queue = [t for t in (body.get('queue') or []) if re.match(r'^https?://', str(t.get('url', '')))]
    if not queue:
        abort(400)
    start = max(0, min(int(body.get('start') or 0), len(queue) - 1))
    start_at = max(0, int(body.get('start_at') or 0))     # a podcast episode's resume point
    ANDROID.hide()
    YOUTUBE.close()
    try:
        browser_home()
    except Exception:
        pass
    prefer_hdmi()
    INPUT.park()
    MUSIC['queue'] = queue
    with open(MUSIC_LIST, 'w') as f:
        f.write('#EXTM3U\n' + ''.join(t['url'] + '\n' for t in queue))
    PLAYER.command('set_property', 'force-media-title', '')
    if start_at and len(queue) == 1:
        reply = PLAYER.command('loadfile', queue[0]['url'], 'replace', -1, 'start=%d' % start_at)
    else:
        reply = PLAYER.command('loadlist', MUSIC_LIST, 'replace')
        if start:
            PLAYER.command('playlist-play-index', start)
    PLAYER.command('set_property', 'pause', False)
    return jsonify({'ok': reply.get('error') == 'success', 'count': len(queue)})


@app.post('/music/append')
def music_append():
    """More songs for the radio station that's playing. Only onto that
    station's own queue: a different queue started meanwhile wins."""
    body = request.get_json(force=True)
    radio = body.get('radio')
    queue = [t for t in (body.get('queue') or []) if re.match(r'^https?://', str(t.get('url', '')))]
    if not queue or not radio or not MUSIC['queue'] or MUSIC['queue'][0].get('radio') != radio:
        return jsonify({'ok': False, 'count': len(MUSIC['queue'])})
    for t in queue:
        PLAYER.command('loadfile', t['url'], 'append')
    MUSIC['queue'] = MUSIC['queue'] + queue
    return jsonify({'ok': True, 'count': len(MUSIC['queue'])})


@app.post('/music/continue')
def music_continue():
    """Radio from the song that's playing: the song plays on untouched where it
    is, everything queued after it goes (mpv playlist-clear keeps the playing
    file), and the station's songs follow."""
    body = request.get_json(force=True)
    queue = [t for t in (body.get('queue') or []) if re.match(r'^https?://', str(t.get('url', '')))]
    pos = PLAYER.get('playlist-pos')
    if not queue or not MUSIC['queue'] or not isinstance(pos, int) or not 0 <= pos < len(MUSIC['queue']):
        abort(409)
    current = dict(MUSIC['queue'][pos])
    current.update(radio=body.get('radio'), station=body.get('station'))
    current.pop('queued', None)
    PLAYER.command('playlist-clear')
    for t in queue:
        PLAYER.command('loadfile', t['url'], 'append')
    MUSIC['queue'] = [current] + queue
    return jsonify({'ok': True, 'count': len(MUSIC['queue'])})


def music_status(player):
    """The track playing now, when mpv is playing our music queue."""
    queue = MUSIC['queue']
    if not queue or not player.get('active'):
        return None
    pos = PLAYER.get('playlist-pos')
    if not isinstance(pos, int) or not 0 <= pos < len(queue) or queue[pos].get('url') != player.get('path'):
        return None
    strip = lambda t: {k: v for k, v in t.items() if k != 'url'}
    return {'index': pos, 'count': len(queue), 'track': strip(queue[pos]),
            'next': strip(queue[pos + 1]) if pos + 1 < len(queue) else None}


def music_control(action, index=None):
    if action == 'next':
        return PLAYER.command('playlist-next', 'force')
    if action == 'previous':
        # like any music player: a few seconds in, "previous" restarts the song
        if (PLAYER.get('time-pos') or 0) > 4 or (PLAYER.get('playlist-pos') or 0) == 0:
            return PLAYER.command('seek', 0, 'absolute')
        return PLAYER.command('playlist-prev')
    if action == 'toggle':
        return PLAYER.command('cycle', 'pause')
    if action == 'stop':
        return PLAYER.command('stop')
    if action in ('rewind', 'forward'):      # a podcast: ◀ back 15 s, ▶ ahead 30 s
        return PLAYER.command('seek', -15 if action == 'rewind' else 30, 'relative')
    if action == 'jump' and isinstance(index, int) and 0 <= index < len(MUSIC['queue']):
        return PLAYER.command('playlist-play-index', index)      # a song picked from the queue
    return None


def music_queue():
    """The whole queue (no urls) and where we are in it, for Now Playing's list
    (the songs above and below the one playing)."""
    queue = MUSIC['queue']
    pos = PLAYER.get('playlist-pos') if queue else None
    keep = ('id', 'title', 'artist', 'album', 'duration', 'kind', 'station', 'queued', 'image')
    return {'index': pos if isinstance(pos, int) else None,
            'items': [{k: t.get(k) for k in keep} for t in queue]}


@app.get('/music/queue')
def music_queue_route():
    return jsonify(music_queue())


@app.post('/music/insert')
def music_insert():
    """Play next / Add to queue. 'next' goes right after the song
    playing; 'queue' after the songs already queued (Spotify's order), before
    the rest of the album, playlist or station."""
    body = request.get_json(force=True)
    item = body.get('item') or {}
    if not re.match(r'^https?://', str(item.get('url', ''))) or not MUSIC['queue']:
        abort(400)
    queue = MUSIC['queue']
    pos = PLAYER.get('playlist-pos')
    if not isinstance(pos, int) or not 0 <= pos < len(queue):
        abort(409)
    at = pos + 1
    if body.get('mode') != 'next':
        while at < len(queue) and queue[at].get('queued'):
            at += 1
    reply = PLAYER.command('loadfile', item['url'], 'append')
    if at < len(queue):
        PLAYER.command('playlist-move', len(queue), at)
    queue.insert(at, item)
    return jsonify({'ok': reply.get('error') == 'success', 'at': at, 'count': len(queue)})


@app.post('/control')
def control():
    body = request.get_json(force=True)
    action = body.get('action')
    if action == 'toggle':
        reply = PLAYER.command('cycle', 'pause')
    elif action in ('pause', 'resume'):
        reply = PLAYER.command('set_property', 'pause', action == 'pause')
    elif action == 'seek':
        reply = PLAYER.command('seek', float(body.get('seconds', 0)), 'relative')
    elif action == 'seek_to':
        reply = PLAYER.command('seek', float(body.get('seconds', 0)), 'absolute')
    elif action == 'stop':
        reply = PLAYER.command('stop')
    elif action in ('next', 'previous', 'jump'):
        reply = music_control(action, body.get('index'))
    elif action == 'subtitles':
        reply = PLAYER.command('cycle', 'sub')
    elif action == 'audio_track':
        reply = PLAYER.command('cycle', 'audio')
    elif action == 'volume':
        step = int(body.get('step', 5))
        _wpctl('set-volume', '-l', '1.0', '@DEFAULT_AUDIO_SINK@',
               '%d%%%s' % (abs(step), '+' if step > 0 else '-'))
        return jsonify({'ok': True, **volume_status()})
    elif action == 'volume_set':
        level = max(0, min(100, int(body.get('level', 50))))
        _wpctl('set-volume', '@DEFAULT_AUDIO_SINK@', '%d%%' % level)
        return jsonify({'ok': True, **volume_status()})
    elif action == 'mute':
        _wpctl('set-mute', '@DEFAULT_AUDIO_SINK@', 'toggle')
        return jsonify({'ok': True, **volume_status()})
    else:
        abort(400)
    return jsonify({'ok': reply.get('error') == 'success', 'mpv': reply})


@app.post('/app')
def open_app():
    body = request.get_json(force=True)
    name = body.get('name')
    if name in ('youtube', 'android') or name in APPS or name in ANDROID_APPS:
        try:
            url = open_named_app(name)
            return jsonify({'ok': True, 'url': url, 'cancelled': url is None})
        except RuntimeError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 503
    url = body.get('url', '')
    if not re.match(r'^https?://', url or ''):
        abort(400)
    PLAYER.command('stop')
    ANDROID.hide()
    YOUTUBE.close()
    prefer_hdmi()
    browser_open(url)
    INPUT.unpark()
    return jsonify({'ok': True, 'url': url})


@app.post('/home_screen')
def home_screen():
    PLAYER.command('stop')
    ANDROID.hide()
    YOUTUBE.close()
    browser_home()
    INPUT.park()
    # Home means the main home screen, not whichever menu was open behind the
    # player (stopping music started from the Jellyfin shelf used to leave the
    # TV on the Jellyfin screen).
    try:
        home = next((t for t in _pages() if _is_home(t) and t.get('webSocketDebuggerUrl')), None)
        if home:
            _page_eval(home, "typeof goHome === 'function' && goHome()")
    except Exception as exc:
        print('home_screen (menus to home):', exc, flush=True)
    return jsonify({'ok': True})


@app.post('/browser/restart')
def browser_restart():
    subprocess.run(['pkill', '-f', 'user-data-dir=' + BROWSER_PROFILE], timeout=5)
    time.sleep(1.5)
    ensure_browser()
    return jsonify({'ok': True, 'up': _browser_up()})


@app.post('/input')
def input_event():
    body = request.get_json(force=True)
    kind = body.get('type')
    if kind == 'key':
        key = body.get('key')
        if key == 'back' and ANDROID.visible:
            ANDROID.key(4)  # KEYCODE_BACK
            return jsonify({'ok': True})
        if key in ANDROID_MEDIA_KEYS and ANDROID.visible:
            ANDROID.key(ANDROID_MEDIA_KEYS[key])
            return jsonify({'ok': True})
        if key in ANDROID_MEDIA_KEYS and not YOUTUBE.pid():
            try:
                done = browser_video(key)
            except Exception as e:  # no CDP / page busy: fall back to the media key
                print('browser video %s failed: %s' % (key, e), flush=True)
                done = None
            if done:
                return jsonify({'ok': True, 'video': done})
        if key == 'back' and YOUTUBE.pid():
            key = 'esc'
        codes = NAMED_KEYS.get(key)
        if not codes:
            abort(400)
        INPUT.combo(codes)
    elif kind == 'text':
        INPUT.type_text(str(body.get('text', '')))
    elif kind == 'move':
        INPUT.move(body.get('dx', 0), body.get('dy', 0))
    elif kind == 'click':
        INPUT.click(body.get('button', 'left'))
    elif kind == 'scroll':
        INPUT.scroll(body.get('dy', 0))
    else:
        abort(400)
    return jsonify({'ok': True})


@app.get('/audio')
def audio():
    return jsonify({'sinks': audio_sinks(), **volume_status()})


@app.post('/audio/default')
def audio_default():
    sink = int(request.get_json(force=True).get('id'))
    _wpctl('set-default', str(sink))
    return jsonify({'ok': True, 'sinks': audio_sinks()})


@app.get('/snapshot')
def snapshot():
    """What the TV shows right now (mpv frame while playing, else the screen)."""
    # Trixie's grim has no JPEG support, so the screen comes back as PNG.
    # A song has no video frame: the screen (Now Playing) is the picture.
    player = PLAYER.status()
    if player.get('active') and not music_status(player):
        path, mime = SNAPSHOT_PATH, 'image/jpeg'
    else:
        path, mime = SNAPSHOT_PATH[:-4] + '.png', 'image/png'
    if os.path.exists(path):
        os.unlink(path)
    if mime == 'image/jpeg':
        PLAYER.command('screenshot-to-file', path, 'video')
    else:
        subprocess.run(['grim', '-t', 'png', '-l', '1', path], env=_session_env(), timeout=10)
    if not os.path.exists(path):
        abort(503)
    return send_file(path, mimetype=mime, max_age=0)


# --------------------------------------------------------------------------- #
# Routes — the TV's own home page (127.0.0.1 only)
# --------------------------------------------------------------------------- #
@app.get('/home')
def home_page():
    return send_from_directory(os.path.join(HERE, 'home'), 'index.html', max_age=0)


@app.get('/home/<path:name>')
def home_asset(name):
    return send_from_directory(os.path.join(HERE, 'home'), name, max_age=0)


@app.get('/local/status')
def local_status():
    player = PLAYER.status()
    return jsonify({'player': player, 'browser': browser_status(), 'music': music_status(player),
                    'remote_used_at': REMOTE_USED['at'],
                    'android': ANDROID.public(), 'android_available': android_available(),
                    'remote': CONFIG.get('remote_hint', '')})


@app.post('/local/open')
def local_open():
    name = request.get_json(force=True).get('name')
    if name != 'youtube' and name not in APPS and name not in ANDROID_APPS:
        abort(400)
    try:
        open_named_app(name)
    except RuntimeError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503
    return jsonify({'ok': True})


@app.post('/local/open_android')
def local_open_android():
    """A Streaming-page tile: launch that Android app straight into view."""
    package = request.get_json(force=True).get('package', '')
    if package not in ANDROID_APPS.values():
        abort(400)
    if not android_available():
        # No Android on this box: the same tile opens the app's website.
        name = next(k for k, v in ANDROID_APPS.items() if v == package)
        open_named_app(name)
        return jsonify({'ok': True, 'website': True})
    PLAYER.command('stop')
    YOUTUBE.close()
    prefer_hdmi()
    try:
        if not ANDROID.open(package):
            return jsonify({'ok': True, 'cancelled': True})
    except RuntimeError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 503
    INPUT.unpark()
    return jsonify({'ok': True})


@app.post('/local/android/prepare')
def local_android_prepare():
    """The TV's Streaming page is up: Android gets ready in the background."""
    if not android_available():
        return jsonify({'ok': False, 'error': NO_ANDROID})
    threading.Thread(target=ANDROID.prepare, daemon=True).start()
    return jsonify({'ok': True})


@app.post('/android/prepare')
def android_prepare():
    """The same from the phone remote (the server), when its page comes up."""
    if not android_available():
        return jsonify({'ok': False, 'error': NO_ANDROID})
    threading.Thread(target=ANDROID.prepare, daemon=True).start()
    return jsonify({'ok': True})


def _server(method, path, **kw):
    """Relay to the server with the receiver token (the TV's Jellyfin screens)."""
    url = CONFIG['server'] + path
    headers = {'X-TV-Token': CONFIG.get('token', '')}
    return requests.request(method, url, headers=headers, timeout=kw.pop('timeout', 30), **kw)


@app.errorhandler(requests.RequestException)
def _server_down(exc):
    """A /local/* relay found the server unreachable: say so, rather than a 500."""
    print('relay to the server failed:', exc, flush=True)
    return jsonify({'ok': False, 'error': "The Hearth server isn't answering"}), 502


@app.get('/local/library')
def local_library():
    r = _server('GET', '/api/tv/library')
    return (r.content, r.status_code, {'Content-Type': 'application/json'})


JF_ID = re.compile(r'[0-9a-f]{32}')


@app.get('/local/show/<series_id>')
def local_show(series_id):
    if not JF_ID.fullmatch(series_id):
        abort(400)
    r = _server('GET', '/api/tv/show/' + series_id)
    return (r.content, r.status_code, {'Content-Type': 'application/json'})


@app.get('/local/jf_image/<item_id>')
def local_jf_image(item_id):
    if not JF_ID.fullmatch(item_id):
        abort(400)
    r = _server('GET', '/api/tv/jf_image/' + item_id, params={'w': request.args.get('w', '360')})
    if r.status_code != 200:
        abort(404)
    return (r.content, 200, {'Content-Type': r.headers.get('content-type', 'image/jpeg'),
                             'Cache-Control': 'max-age=86400'})


@app.get('/local/station_art')
def local_station_art():
    """A radio station's cover, when the server has one (hearth/data/station_art/)."""
    station = request.args.get('station', '')
    if not STATION.fullmatch(station):
        abort(400)
    r = _server('GET', '/api/tv/radio/art', params={'station': station})
    if r.status_code != 200:
        abort(404)
    return (r.content, 200, {'Content-Type': 'image/jpeg', 'Cache-Control': 'max-age=86400'})


@app.post('/local/play_library')
def local_play_library():
    body = request.get_json(force=True)
    r = _server('POST', '/api/tv/play', json={'id': body.get('id'), 'start': body.get('start')}, timeout=40)
    return (r.content, r.status_code, {'Content-Type': 'application/json'})


@app.post('/local/play_music')
def local_play_music():
    r = _server('POST', '/api/tv/music/play', json=request.get_json(force=True), timeout=40)
    return (r.content, r.status_code, {'Content-Type': 'application/json'})


@app.get('/local/music_index')
def local_music_index():
    r = _server('GET', '/api/tv/music/index', timeout=40)
    return (r.content, r.status_code, {'Content-Type': 'application/json'})


@app.get('/local/music_artist/<artist_id>')
def local_music_artist(artist_id):
    if not JF_ID.fullmatch(artist_id):
        abort(400)
    r = _server('GET', '/api/tv/music/artist/' + artist_id, timeout=40)
    return (r.content, r.status_code, {'Content-Type': 'application/json'})


def _relay(r):
    return (r.content, r.status_code, {'Content-Type': 'application/json'})


# Radio + podcasts for the TV's own screens
STATION = re.compile(r'[a-z]+(:[0-9a-f]{12,32}|:[0-9]{4}|:[a-z]+)?')
POD_ID = re.compile(r'[0-9a-f]{16}')


# Lists, the queue, songs, playlists: one relay, an allowlist
TVAPI = re.compile(r'list|list/play|queue|song/feedback|playlists|playlists/[0-9a-f]{32}/add')


@app.route('/local/tvapi/<path:rest>', methods=['GET', 'POST'])
def local_tvapi(rest):
    if not TVAPI.fullmatch(rest):
        abort(404)
    if request.method == 'GET':
        r = _server('GET', '/api/tv/' + rest, params=request.args.to_dict(), timeout=40)
    else:
        r = _server('POST', '/api/tv/' + rest, json=request.get_json(force=True), timeout=40)
    return _relay(r)


@app.get('/local/radio')
def local_radio():
    return _relay(_server('GET', '/api/tv/radio', timeout=40))


@app.post('/local/play_radio')
def local_play_radio():
    body = request.get_json(force=True)
    if not STATION.fullmatch(str(body.get('station', ''))):
        abort(400)
    return _relay(_server('POST', '/api/tv/radio/play', json={'station': body['station'],
                                                              'shuffle': bool(body.get('shuffle'))}, timeout=40))


@app.get('/local/podcasts')
def local_podcasts():
    return _relay(_server('GET', '/api/tv/podcasts'))


@app.get('/local/podcast/<show_id>')
def local_podcast(show_id):
    if not POD_ID.fullmatch(show_id):
        abort(400)
    return _relay(_server('GET', '/api/tv/podcasts/show/' + show_id, timeout=40))


@app.post('/local/play_podcast')
def local_play_podcast():
    body = request.get_json(force=True)
    if not POD_ID.fullmatch(str(body.get('episode', ''))):
        abort(400)
    return _relay(_server('POST', '/api/tv/podcasts/play', json={'episode': body['episode'],
                                                                 'from_start': bool(body.get('from_start'))},
                          timeout=40))


@app.get('/local/podcast_art/<show_id>')
def local_podcast_art(show_id):
    if not POD_ID.fullmatch(show_id):
        abort(400)
    r = _server('GET', '/api/tv/podcasts/art/' + show_id)
    if r.status_code != 200:
        abort(404)
    return (r.content, 200, {'Content-Type': 'image/jpeg', 'Cache-Control': 'max-age=86400'})


@app.get('/local/music_queue')
def local_music_queue():
    return jsonify(music_queue())


@app.post('/local/music_control')
def local_music_control():
    body = request.get_json(force=True)
    action = body.get('action')
    if action not in ('next', 'previous', 'toggle', 'stop', 'rewind', 'forward', 'jump'):
        abort(400)
    reply = music_control(action, body.get('index')) or {}
    return jsonify({'ok': reply.get('error') == 'success'})


def _startup():
    threading.Thread(target=_pointer_janitor, daemon=True).start()
    threading.Thread(target=_android_janitor, daemon=True).start()
    threading.Thread(target=_youtube_janitor, daemon=True).start()
    try:
        prefer_hdmi()
        PLAYER.ensure()
        ensure_browser()
        # A redeploy mustn't yank the screen out from under whoever is using it.
        if not (android_available() and ANDROID.detect()):
            site = _site_open()
            browser_fresh_home()
            if not YOUTUBE.pid() and not PLAYER.status().get('active') and not site:
                INPUT.park()
    except Exception as exc:  # the routes retry on demand
        print('startup:', exc, flush=True)


if __name__ == '__main__':
    threading.Thread(target=_startup, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, threaded=True)
