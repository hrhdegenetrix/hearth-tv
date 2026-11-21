"""The receiver's Android (Waydroid) handling, and the receiver without Android.

Android notices when it can't make windows any more: once, Android's hardware
composer aborted and surfaceflinger died with it. init restarted surfaceflinger,
but system_server kept its dead connection, so every app launch failed and the
TV sat on "Opening Pluto TV…" until the session was restarted. The receiver
reads both processes' start times from the host's /proc: in a healthy session
surfaceflinger is the older one. See docs/android.md.

No Waydroid, mpv, Chromium or uinput is needed to run these: evdev is mocked and
the receiver is loaded from a copy beside a throwaway config.json.
"""
import importlib.util
import json
import os
import shutil
import sys
import time
from unittest import mock

import pytest

RECEIVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'receiver', 'receiver.py')


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    # The receiver reads config.json next to itself at import (it lives only on
    # the Pi), and makes its uinput devices with evdev (a Pi-only package).
    class Codes:                       # every ecodes name is a distinct int
        def __getattr__(self, name):
            return sum(ord(c) for c in name) * 1000 + len(name)
    monkeypatch.setitem(sys.modules, 'evdev', mock.MagicMock(ecodes=Codes()))
    shutil.copy(RECEIVER, tmp_path / 'receiver.py')
    (tmp_path / 'config.json').write_text(json.dumps({'token': 'test', 'port': 0,
                                                      'server': 'http://hearth-server.invalid:5080'}))
    spec = importlib.util.spec_from_file_location('receiver_under_test', tmp_path / 'receiver.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_proc(tmp_path, monkeypatch, receiver, procs):
    """procs: {pid: (comm, starttime)} laid out like the kernel's /proc."""
    root = tmp_path / 'proc'
    root.mkdir()
    (root / 'self').mkdir()
    for pid, (comm, start) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / 'comm').write_text(comm + '\n')
        # pid (comm) state, then fields 4..21, then starttime as field 22
        (d / 'stat').write_text(f'{pid} ({comm}) S ' + ' '.join(['0'] * 18) + f' {start} 0 0\n')
    real_listdir, real_open = os.listdir, open

    def listdir(path):
        return real_listdir(root) if path == '/proc' else real_listdir(path)

    def fake_open(path, *a, **kw):
        if isinstance(path, str) and path.startswith('/proc/'):
            path = str(root) + path[len('/proc'):]
        return real_open(path, *a, **kw)
    monkeypatch.setattr(receiver.os, 'listdir', listdir)
    monkeypatch.setattr('builtins.open', fake_open)


def test_healthy_session(tmp_path, monkeypatch, receiver):
    _fake_proc(tmp_path, monkeypatch, receiver,
               {80: ('surfaceflinger', 12876103), 269: ('system_server', 12877701)})
    assert receiver.ANDROID.graphics_broken() is False


def test_surfaceflinger_restarted_under_system_server(tmp_path, monkeypatch, receiver):
    _fake_proc(tmp_path, monkeypatch, receiver,
               {9001: ('surfaceflinger', 14000000), 269: ('system_server', 12877701)})
    assert receiver.ANDROID.graphics_broken() is True


def test_surfaceflinger_gone(tmp_path, monkeypatch, receiver):
    _fake_proc(tmp_path, monkeypatch, receiver, {269: ('system_server', 12877701)})
    assert receiver.ANDROID.graphics_broken() is True


def test_android_not_running_is_not_broken(tmp_path, monkeypatch, receiver):
    _fake_proc(tmp_path, monkeypatch, receiver, {1: ('systemd', 1), 80: ('surfaceflinger', 5)})
    assert receiver.ANDROID.graphics_broken() is False


def test_a_soft_restart_of_system_server_is_healthy(tmp_path, monkeypatch, receiver):
    # system_server restarting on its own reconnects; only the reverse order is broken
    _fake_proc(tmp_path, monkeypatch, receiver,
               {80: ('surfaceflinger', 100), 269: ('system_server', 200), 5000: ('system_server', 900)})
    assert receiver.ANDROID.graphics_broken() is False


class _FakeAndroid:
    """Drives Android.open() without Waydroid: which windows exist, and whether
    the graphics break while an app opens."""

    def __init__(self, receiver, monkeypatch, window_after_launch, broken_after_launch=()):
        self.a = receiver.Android()
        self.launches, self.restarts = 0, 0
        self.windows = set()
        self.broken = False
        a = self.a
        self.closes = 0
        self.calls = []
        monkeypatch.setattr(a, '_ensure_booted', lambda: True)
        self.wakes_land = True
        self.age = 3600.0               # a settled session
        monkeypatch.setattr(a, 'wake', lambda *args, **kw: self.calls.append('wake') or self.wakes_land)
        monkeypatch.setattr(a, 'session_age', lambda: self.age)

        def close_all():
            self.closes += 1
            self.windows.clear()
        monkeypatch.setattr(a, 'close_all', close_all)
        monkeypatch.setattr(a, '_front', lambda app_id: None)
        monkeypatch.setattr(a, 'graphics_broken', lambda: self.broken)

        self.on_launch = self.on_restart = None

        def launch(package):
            self.launches += 1
            self.calls.append('launch')
            if self.on_launch:
                self.on_launch()
            if self.launches in broken_after_launch:
                self.broken = True
            if self.launches in window_after_launch:
                self.windows.add('waydroid.' + package)
        monkeypatch.setattr(a, '_launch', launch)

        def restart(**kw):
            self.restarts += 1
            self.calls.append('restart')
            self.wakes_land = True
            self.broken = False
            if self.on_restart:
                self.on_restart()
            return True
        monkeypatch.setattr(a, 'restart_session', restart)
        monkeypatch.setattr(a, '_wlr', lambda *args: args[0] == 'find' and args[1][len('app_id:'):] in self.windows)
        monkeypatch.setattr(receiver.time, 'sleep', lambda s: None)


def test_app_window_found(receiver, monkeypatch):
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={1})
    f.a.open('com.wbd.stream')
    assert (f.launches, f.restarts, f.a.visible) == (1, 0, True)


def test_graphics_die_while_opening_restart_and_relaunch(receiver, monkeypatch):
    # 2026-10-12 19:20: the hardware composer aborted while Max was opening
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={2}, broken_after_launch={1})
    f.a.open('com.wbd.stream')
    assert (f.launches, f.restarts, f.a.visible) == (2, 1, True)


def test_no_window_says_so(receiver, monkeypatch):
    # Prime after 34 hours frozen: no window, graphics fine — the TV is told, and
    # Android stays "in front" so a late window still gets fullscreened
    clock = iter(range(0, 10_000, 1))
    monkeypatch.setattr(receiver.time, 'time', lambda: next(clock))
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch=set())
    with pytest.raises(RuntimeError, match="didn't open"):
        f.a.open('com.amazon.avod.thirdpartyclient')
    assert (f.launches, f.restarts, f.a.visible) == (1, 0, True)


# --------------------------------------------------------------------------- #
# Home while an app opens: Max's open once met a composer crash, Android
# restarted (72 s), and three Home presses made meanwhile waited behind the
# lock, then closed Max the moment it appeared. Home now stops the open.
# --------------------------------------------------------------------------- #
def test_home_while_the_app_opens_stops_it(receiver, monkeypatch):
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={1})
    f.on_launch = f.a.hide          # Home pressed mid-open (the open holds the lock)
    assert f.a.open('com.wbd.stream') is False
    assert (f.launches, f.a.visible, f.windows) == (1, False, set())
    assert f.a.state is None


def test_home_during_a_restart_stops_the_relaunch(receiver, monkeypatch):
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={2}, broken_after_launch={1})
    f.on_restart = f.a.hide
    assert f.a.open('com.wbd.stream') is False
    assert (f.launches, f.restarts, f.a.visible) == (1, 1, False)


def test_home_does_not_wait_for_an_open(receiver):
    a = receiver.Android()
    a.visible = True
    with a.lock:                    # an open (or restart) in progress
        t0 = receiver.time.monotonic()
        a.hide()
        assert receiver.time.monotonic() - t0 < 0.5
    assert a.cancel.is_set() and a.visible  # the open puts it away, not Home


def test_an_old_cancel_does_not_stop_the_next_open(receiver, monkeypatch):
    # Home pressed during the janitor's restart, then Max picked later
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={1})
    f.a.cancel.set()
    assert f.a.open('com.wbd.stream') is True
    assert f.a.visible


def test_state_while_restarting(receiver, monkeypatch):
    a = receiver.Android()
    seen = []
    monkeypatch.setattr(a, '_run', lambda *cmd, **kw: None)
    monkeypatch.setattr(a, '_ensure_booted', lambda: seen.append(a.public()['state']) or True)
    a._set_state('opening')
    assert a.restart_session() is True
    assert seen == ['restarting'] and a.state == 'opening'


def _at(hour, minute=0):
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hour, minute, 0, 0, 0, -1))


@pytest.mark.parametrize('when,refreshed,visible,remote_ago,busy,due', [
    ((5, 30), False, False, 3600, False, True),
    ((4, 59), False, False, 3600, False, False),     # before the window
    ((7, 0), False, False, 3600, False, False),      # after it
    ((6, 10), True, False, 3600, False, False),      # already done today
    ((5, 30), False, True, 3600, False, False),      # Android on screen
    ((5, 30), False, False, 600, False, False),      # the remote was just used
    ((5, 30), False, False, 3600, True, False),      # something's playing
])
def test_morning_refresh_due(receiver, monkeypatch, when, refreshed, visible, remote_ago, busy, due):
    now = _at(*when)
    receiver.ANDROID.refreshed_on = receiver.time.strftime('%Y-%m-%d', receiver.time.localtime(now)) if refreshed else None
    receiver.ANDROID.visible = visible
    receiver.REMOTE_USED['at'] = receiver.time.time() - remote_ago
    monkeypatch.setattr(receiver, '_tv_busy', lambda: busy)
    assert receiver._morning_refresh_due(now) is due


# --------------------------------------------------------------------------- #
# The warm-up after a background restart (2026-10-15): left alone, Android slept
# at its 60 s timeout and was frozen mid first-boot work.
# --------------------------------------------------------------------------- #
def _warm(receiver, monkeypatch, loads, visible_after=None, stuck=()):
    a = receiver.Android()
    cmds = []

    def run(*cmd, **kw):
        cmds.append(' '.join(cmd))
        if len(cmds) - 1 in stuck:
            raise receiver.subprocess.TimeoutExpired(list(cmd), kw.get('timeout'))
    monkeypatch.setattr(a, '_run', run)
    clock = {'t': 0.0}
    monkeypatch.setattr(receiver.time, 'time', lambda: clock['t'])

    def sleep(s):
        clock['t'] += s
        if visible_after is not None and clock['t'] >= visible_after:
            a.visible = True
    monkeypatch.setattr(receiver.time, 'sleep', sleep)
    load_iter = iter(loads)
    monkeypatch.setattr(receiver.os, 'getloadavg', lambda: (next(load_iter, loads[-1]), 0, 0))
    a.warm_up()
    return a, cmds, clock['t']


def test_warm_up_keeps_android_awake_until_the_load_settles(receiver, monkeypatch):
    a, cmds, took = _warm(receiver, monkeypatch, loads=[17.0] * 10 + [2.0])
    assert cmds[0].endswith('waydroid container unfreeze')
    assert all(c.endswith('input keyevent 224') for c in cmds[1:])
    assert not any('223' in c or 'settings' in c for c in cmds)     # no SLEEP from a shell
    assert 4 * 60 <= took <= 15 * 60 and not a.warming


def test_warm_up_gives_up_after_fifteen_minutes(receiver, monkeypatch):
    a, cmds, took = _warm(receiver, monkeypatch, loads=[9.0])
    assert took <= 15 * 60 + 40


def test_warm_up_steps_aside_when_an_app_opens(receiver, monkeypatch):
    a, cmds, took = _warm(receiver, monkeypatch, loads=[17.0], visible_after=80)
    assert took <= 120 and len(cmds) <= 6


def test_warm_up_outlives_a_key_that_never_lands(receiver, monkeypatch):
    # 2026-10-16 05:03: at load 17.6 a WAKEUP key timed out after 20 s and the
    # TimeoutExpired ended the warm-up 2.5 min in
    a, cmds, took = _warm(receiver, monkeypatch, loads=[17.6] * 10 + [2.0], stuck={3})
    assert took >= 4 * 60 and not a.warming
    assert cmds[4].endswith('waydroid container unfreeze')        # thawed and woken again
    assert cmds[5].endswith('input keyevent 224') and cmds[6].endswith('input keyevent 224')


def test_wake_says_so_instead_of_raising(receiver, monkeypatch):
    # an open calls wake() right after a boot, when the Pi is busiest
    a = receiver.Android()

    def run(*cmd, **kw):
        raise receiver.subprocess.TimeoutExpired(list(cmd), kw.get('timeout'))
    monkeypatch.setattr(a, '_run', run)
    assert a.wake() is False and a.nudge() is False


def test_open_wakes_android_before_launching(receiver, monkeypatch):
    # 2026-10-15 15:37: Max launched into a sleeping Android; Waydroid froze it
    # 21 s later and no window came
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={2}, broken_after_launch={1})
    f.a.open('com.wbd.stream')
    assert f.calls == ['wake', 'launch', 'restart', 'wake', 'launch']   # again after a restart


# --------------------------------------------------------------------------- #
# 2026-10-20: an open shown step by step; a wedged Android restarted at once;
# Android readied while the Streaming page is up; the box restarts nightly.
# --------------------------------------------------------------------------- #
def test_an_open_reports_its_steps(receiver, monkeypatch):
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={1})
    seen = []
    f.on_launch = lambda: seen.append(f.a.public())
    assert f.a.open('com.wbd.stream') is True
    mid = seen[0]
    assert mid['app'] == 'Max' and mid['state'] == 'opening'
    assert [(s['label'], s['status']) for s in mid['steps']] == [
        ('Waking Android', 'done'), ('Closing other apps', 'done'), ('Starting Max', 'active')]
    assert all(isinstance(s['usual'], int) for s in mid['steps'])
    assert f.a.public() is None and f.a.progress is None        # gone once it's open


def test_a_wedged_android_is_restarted_before_the_launch(receiver, monkeypatch):
    # 20:21: the wake-up key timed out, the launch went ahead, and the display
    # crashed 75 s later
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={1})
    f.wakes_land = False
    notes = []
    f.on_restart = lambda: notes.append(f.a.public()['note'])
    assert f.a.open('com.wbd.stream') is True
    assert f.calls == ['wake', 'restart', 'wake', 'launch']
    assert notes == ["Android stopped answering, so it's restarting."]


def test_a_fresh_android_gets_time_to_answer(receiver, monkeypatch):
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={1})
    f.wakes_land, f.age = False, 120.0      # first-boot work: slow, not wedged
    assert f.a.open('com.wbd.stream') is True
    assert f.calls == ['wake', 'launch']


def test_a_display_crash_mid_open_says_so(receiver, monkeypatch):
    f = _FakeAndroid(receiver, monkeypatch, window_after_launch={2}, broken_after_launch={1})
    notes = []
    f.on_restart = lambda: notes.append(f.a.public())
    assert f.a.open('com.wbd.stream') is True
    note, steps = notes[0]['note'], notes[0]['steps']
    assert note == "Android's display crashed while Max was opening, so it's restarting."
    assert steps[-1] == {**steps[-1], 'label': "Waiting for Max's picture", 'status': 'failed'}


def test_a_background_restart_shows_its_one_step(receiver, monkeypatch):
    a = receiver.Android()
    seen = []
    monkeypatch.setattr(a, '_run', lambda *cmd, **kw: None)
    monkeypatch.setattr(a, '_ensure_booted', lambda: seen.append(a.public()) or True)
    assert a.restart_session(reason='morning refresh') is True
    assert seen[0]['state'] == 'restarting' and seen[0]['app'] is None
    assert [s['label'] for s in seen[0]['steps']] == ['Restarting Android']
    assert a.public() is None and a.progress is None


def _prep(receiver, monkeypatch, running=True, broken=False, wake=True, age=3600.0):
    a = receiver.Android()
    restarts = []
    monkeypatch.setattr(a, '_run', lambda *cmd, **kw: type('R', (), {'stdout': 'Session:\t' + ('RUNNING' if running else 'STOPPED') + '\n'})())
    monkeypatch.setattr(a, 'graphics_broken', lambda: broken)
    monkeypatch.setattr(a, 'wake', lambda *args, **kw: wake)
    monkeypatch.setattr(a, 'session_age', lambda: age)
    monkeypatch.setattr(receiver, '_background_restart', lambda reason: restarts.append(reason))
    return a, restarts


@pytest.mark.parametrize('kw,restarted', [
    ({}, False),                                   # healthy: just woken
    ({'broken': True}, True),                      # the display had crashed
    ({'running': False}, True),                    # not started yet (after the box rebooted)
    ({'wake': False}, True),                       # wedged
    ({'wake': False, 'age': 120.0}, False),        # fresh and busy
])
def test_the_streaming_page_readies_android(receiver, monkeypatch, kw, restarted):
    a, restarts = _prep(receiver, monkeypatch, **kw)
    assert a.prepare() is True
    assert bool(restarts) is restarted
    assert not a.lock.locked()


def test_readying_waits_its_turn(receiver, monkeypatch):
    a, restarts = _prep(receiver, monkeypatch, broken=True)
    a.visible = True
    assert a.prepare() is False                    # on screen
    a.visible = False
    assert a.prepare() is True and a.prepare() is False   # at most once a minute
    assert len(restarts) == 1


@pytest.mark.parametrize('when,uptime,visible,remote_ago,busy,due', [
    ((4, 30), 30 * 3600, False, 3600, False, True),
    ((3, 59), 30 * 3600, False, 3600, False, False),    # before the window
    ((5, 0), 30 * 3600, False, 3600, False, False),     # the morning refresh's hour
    ((4, 30), 2 * 3600, False, 3600, False, False),     # rebooted recently (already tonight)
    ((4, 30), 30 * 3600, True, 3600, False, False),     # Android on screen
    ((4, 30), 30 * 3600, False, 600, False, False),     # the remote was just used
    ((4, 30), 30 * 3600, False, 3600, True, False),     # something's playing
])
def test_nightly_reboot_due(receiver, monkeypatch, when, uptime, visible, remote_ago, busy, due):
    monkeypatch.setitem(receiver.CONFIG, 'nightly_reboot', True)
    receiver.ANDROID.visible = visible
    receiver.REMOTE_USED['at'] = receiver.time.time() - remote_ago
    monkeypatch.setattr(receiver, '_tv_busy', lambda: busy)
    monkeypatch.setattr(receiver, '_uptime', lambda: uptime)
    assert receiver._nightly_reboot_due(_at(*when)) is due


def test_nightly_reboot_is_off_unless_asked_for(receiver, monkeypatch):
    receiver.ANDROID.visible = False
    receiver.REMOTE_USED['at'] = 0
    monkeypatch.setattr(receiver, '_tv_busy', lambda: False)
    monkeypatch.setattr(receiver, '_uptime', lambda: 30 * 3600)
    assert 'nightly_reboot' not in receiver.CONFIG
    assert receiver._nightly_reboot_due(_at(4, 30)) is False


# --------------------------------------------------------------------------- #
# A box without Waydroid: Android is optional.
# --------------------------------------------------------------------------- #
TOKEN = {'X-TV-Token': 'test'}


@pytest.fixture
def no_android(receiver, monkeypatch):
    """No Waydroid, and nothing that would touch a real screen or player."""
    monkeypatch.setattr(receiver, 'android_available', lambda: False)
    opened = []
    for name in ('prefer_hdmi', 'browser_home', 'browser_open'):
        monkeypatch.setattr(receiver, name, lambda *a, _n=name: opened.append((_n, *a)))
    monkeypatch.setattr(receiver.PLAYER, 'command', lambda *a, **kw: {'error': 'success'})
    monkeypatch.setattr(receiver.YOUTUBE, 'close', lambda: None)

    def no_open(*a, **kw):
        raise AssertionError('Android.open must not run without Waydroid')
    monkeypatch.setattr(receiver.ANDROID, 'open', no_open)
    return opened


def test_android_button_says_it_isnt_set_up(receiver, no_android):
    r = receiver.app.test_client().post('/app', json={'name': 'android'}, headers=TOKEN)
    assert r.status_code == 503
    assert "isn't set up" in r.get_json()['error']


@pytest.mark.parametrize('name,site', [('prime', 'amazon.com'), ('max', 'max.com')])
def test_prime_and_max_open_their_websites_without_android(receiver, no_android, name, site):
    r = receiver.app.test_client().post('/app', json={'name': name}, headers=TOKEN)
    assert r.status_code == 200 and site in r.get_json()['url']
    assert ('browser_open', receiver.APPS[name]) in no_android


def test_streaming_tile_falls_back_to_the_website(receiver, no_android):
    client = receiver.app.test_client()
    r = client.post('/local/open_android', json={'package': 'com.wbd.stream'},
                    environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert r.status_code == 200 and r.get_json()['website'] is True
    assert ('browser_open', receiver.APPS['max']) in no_android


def test_android_prepare_and_refresh_answer_quietly(receiver, no_android):
    client = receiver.app.test_client()
    assert client.post('/android/prepare', headers=TOKEN).get_json()['ok'] is False
    assert client.post('/android/refresh', headers=TOKEN).status_code == 409
    r = client.post('/local/android/prepare', environ_base={'REMOTE_ADDR': '127.0.0.1'})
    assert r.get_json()['ok'] is False


def test_local_routes_answer_localhost_only(receiver):
    client = receiver.app.test_client()
    assert client.get('/local/status', environ_base={'REMOTE_ADDR': '192.0.2.7'}).status_code == 403
    assert client.get('/status', environ_base={'REMOTE_ADDR': '192.0.2.7'}).status_code == 401


def test_relay_allowlist(receiver):
    client = receiver.app.test_client()
    assert client.get('/local/tvapi/not/allowed', environ_base={'REMOTE_ADDR': '127.0.0.1'}).status_code == 404
    assert receiver.TVAPI.fullmatch('playlists/' + 'a' * 32 + '/add')
    assert not receiver.TVAPI.fullmatch('playlists/' + 'a' * 32 + '/delete')


def test_config_needs_a_server(tmp_path, monkeypatch):
    class Codes:
        def __getattr__(self, name):
            return sum(ord(c) for c in name) * 1000 + len(name)
    monkeypatch.setitem(sys.modules, 'evdev', mock.MagicMock(ecodes=Codes()))
    shutil.copy(RECEIVER, tmp_path / 'receiver.py')
    (tmp_path / 'config.json').write_text(json.dumps({'token': 'test'}))
    spec = importlib.util.spec_from_file_location('receiver_no_server', tmp_path / 'receiver.py')
    with pytest.raises(SystemExit, match='server'):
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
