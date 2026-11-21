# The receiver (the TV box)

The receiver is the screen half of Hearth: a Raspberry Pi plugged into the TV.
It only shows things. The Hearth server (`hearth/server.py`) decides what to
play and sends the receiver commands; the receiver reports back what is on the
screen.

On the Pi it runs:
- one idle **mpv** that goes fullscreen while it plays: library files, YouTube
  and other yt-dlp links, and music queues;
- a **kiosk Chromium** showing the TV's own home page (`receiver/home/index.html`,
  driven with the arrow keys) and the streaming websites;
- a second kiosk Chromium for **YouTube's TV app** (`youtube.com/tv`);
- a virtual **keyboard and mouse** (uinput), so the phone remote can drive
  websites: d-pad, OK, Back, typing, touchpad and scrolling;
- **PipeWire** for volume, with sound sent to the TV's HDMI;
- optionally, **Android under Waydroid** for the streaming apps
  ([android.md](android.md)). Everything else works without it.

Code: `receiver/receiver.py` (Flask, one file). It's deployed with
`receiver/deploy.sh` and runs as the systemd unit `hearth-receiver`.

> **What was verified, and where.** Everything here ran for weeks on one
> Raspberry Pi 4 Model B (4 GB) with Raspberry Pi OS (Trixie) and labwc, on a
> 1080p60 TV. Fresh-install steps are written from that box's setup notes, but
> nobody has yet run them start to finish on a new Pi. Steps marked
> *(untested)* were never run anywhere. Check each one as you go.

## Hardware

- **Raspberry Pi 4 (4 GB or more) or Pi 5.** The Pi 4 decodes H.264 up to
  1080p and HEVC in hardware; VP9 and AV1 run on the CPU, which is why YouTube
  is asked for H.264 first. The Pi 5 has no H.264 hardware decoder (HEVC only),
  so on a Pi 5 the `--hwdec` and YouTube format choices in `Player._ensure` and
  `YTDL_FORMAT` are worth revisiting *(untested)*.
- An HDMI TV. Sound goes out over HDMI.
- **Ethernet is recommended.** The original Pi measured about 35 Mbps over
  Wi-Fi, which is plenty for 1080p but not for a 4K remux.
- The Pi and the server must reach each other: the same LAN, or both on
  Tailscale. The receiver's port (8770) uses a token, but never forward it to
  the internet.

## 1. Operating system

1. Flash **Raspberry Pi OS (64-bit, with desktop)**, Trixie or later, with
   Raspberry Pi Imager. Set the hostname (for example `hearth-tv`), a user, SSH
   with a key, and Wi-Fi if you need it.
2. Set it to boot to the desktop with **autologin**, on **labwc** (Wayland):
   ```bash
   sudo raspi-config     # System Options → Boot / Auto Login → Desktop Autologin
                         # Advanced Options → Wayland → labwc (the default on recent images)
   ```
   The receiver draws on that session's Wayland socket
   (`/run/user/<uid>/wayland-0`). The systemd unit waits up to 90 s for it.
3. Turn **screen blanking off**: `sudo raspi-config nonint do_blanking 1`.
4. The desktop user needs **passwordless sudo** for `waydroid` (Android only)
   and for `systemctl reboot` (the optional nightly reboot). The default
   Raspberry Pi OS user already has it.

## 2. Packages

```bash
sudo apt update
sudo apt install -y mpv chromium grim wlrctl wireplumber pipewire-bin curl \
    python3-flask python3-requests python3-evdev python3-websockets python3-venv \
    fonts-dejavu-core
```

What each is for:

| Package | Used by |
|---|---|
| `mpv` | the player (`Player`) |
| `chromium` | the kiosk browser and YouTube's TV app. Raspberry Pi OS's own build has Widevine support (see § Widevine) |
| `grim` | `/snapshot` when nothing is playing in mpv (the remote's Peek). Trixie's grim has no JPEG support, so it writes PNG |
| `wlrctl` | finding, focusing and fullscreening windows (YouTube janitor, Android). If your release doesn't package it, build it from source (`wlrctl` on sr.ht) |
| `wireplumber` (`wpctl`), `pipewire-bin` (`pw-dump`) | volume, mute, HDMI sink choice, "is the browser making sound" |
| `python3-flask`, `python3-requests` | the receiver itself, run by the system `python3` |
| `python3-evdev` | the uinput keyboard and mouse |
| `python3-websockets` | DevTools `Runtime.evaluate` (`_page_eval`): play/pause a website's own `<video>`, send the home page back to its main screen. Needs websockets ≥ 11 (`websockets.sync`) |
| `curl` | `deploy.sh`'s "is the TV in use?" check |
| `fonts-dejavu-core` | the tile glyphs and the fallback text font. The home page also loads Fredoka from Google Fonts when there's internet |

For Android only, see [android.md](android.md); `android_app.sh` also needs
`python3-pil`.

## 3. The virtual keyboard and mouse (uinput)

The receiver creates two uinput devices (`hearth-keyboard`, `hearth-mouse`).
`/dev/uinput` must be writable by the `input` group (the unit runs with
`SupplementaryGroups=input`):

```bash
sudo cp receiver/99-hearth-uinput.rules /etc/udev/rules.d/    # copy it to the Pi first
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=misc
ls -l /dev/uinput      # crw-rw---- root input
```

If `/dev/uinput` doesn't exist, run `sudo modprobe uinput` and add `uinput` to
`/etc/modules-load.d/uinput.conf` *(untested: on the original Pi the node
existed already)*.

labwc has no option to hide the pointer, so on key-driven screens (home,
YouTube's TV app, the player) the receiver "parks" it in the bottom-right
corner, and brings it back to mid-screen when a website needs the touchpad.

## 4. yt-dlp and deno (a venv in ~/hearth)

mpv plays YouTube and other links through yt-dlp. YouTube now needs a
JavaScript runtime to solve its player challenges, and yt-dlp uses **deno**.
Both live in a venv next to the receiver. mpv is pointed at that yt-dlp
(`--script-opts=ytdl_hook-ytdl_path=…`), and the receiver puts `venv/bin` first
on the PATH it hands mpv and Chromium, so yt-dlp finds deno there.

```bash
mkdir -p ~/hearth
python3 -m venv ~/hearth/venv
~/hearth/venv/bin/pip install -U "yt-dlp[default]" deno
~/hearth/venv/bin/yt-dlp --version && ~/hearth/venv/bin/deno --version
```

The `deno` package on PyPI ships the official deno binary. If it has no build
for your Pi, put deno's own aarch64 release binary at `~/hearth/venv/bin/deno`.
Update yt-dlp now and then (`pip install -U "yt-dlp[default]"`): YouTube breaks
old versions.

## 5. config.json

`~/hearth/config.json` on the Pi. It is never in git and `deploy.sh` never
overwrites it:

```json
{
  "token": "<the server's TV_RECEIVER_TOKEN>",
  "port": 8770,
  "server": "http://<server-host>:5080"
}
```

- `token`: the shared secret. Every route except the home page and its
  `/local/*` buttons needs it as `X-TV-Token`. `chmod 600` the file.
- `server`: how the Pi reaches the Hearth server. **Required.** The TV's own
  Jellyfin, music, radio and podcast screens reach the server through the
  receiver's `/local/*` relays.
- `port`: default 8770. The server's `TV_RECEIVER_URL` must match.
- Optional: `"nightly_reboot": true` restarts the box between 04:00 and 05:00
  when nobody is using it (only useful with Android, see
  [android.md](android.md)). `"remote_hint"`: a free-text string returned in
  `/local/status` as `remote` (for example the remote's address), unused by the
  page today.

The receiver refuses to start without `token` and `server`, and says why in
`journalctl -u hearth-receiver`.

## 6. Deploy

From the repo, on any machine that can SSH to the Pi:

```bash
PI=pi@hearth-tv.local receiver/deploy.sh
```

This copies `receiver.py`, `home/` and `android_app.sh` into `~/hearth/`, and the
PipeWire drop-in into `~/.config/pipewire/pipewire.conf.d/hearth.conf`. It fills
in `hearth-receiver.service` for the Pi's user (`@USER@`, `@HOME@`, `@UID@`),
installs it in `/etc/systemd/system/`, enables it and restarts it.

- ⛔ **A restart stops whatever is playing.** The new receiver kills the old
  mpv by its pidfile (`Player._reap_orphan`). So `deploy.sh` refuses while the
  player is active or a website or app is open. `HEARTH_FORCE=1` overrides.
- The unit has `KillMode=process`, so a restart leaves Chromium running. A
  site that's open (a show in the middle) stays on screen, and the home tab is
  reloaded quietly behind it (`browser_fresh_home`) rather than opened in a new
  tab, which would jump in front.
- Logs: `ssh $PI 'journalctl -u hearth-receiver -f'`.

Check it from the Pi:

```bash
curl -s -H "X-TV-Token: <token>" localhost:8770/status | python3 -m json.tool
```

`mode` should be `home`, `browser.running` true, and the TV showing the Hearth
home page (YouTube · Streaming · Jellyfin).

## 7. Widevine (Prime Video, Max and other DRM sites in the browser)

Raspberry Pi OS's Chromium can play Widevine, but on the original box it only
offered Widevine to the kiosk profile **after** Chromium's component updater
had downloaded the CDM into `~/.config/hearth-browser/WidevineCdm/` and the
browser had been restarted. The `libwidevinecdm0` package was installed as
well.

1. Leave the kiosk running and online for a few minutes (the updater runs on
   its own).
2. Restart the browser: the remote's Restart browser button, or on the Pi
   `curl -s -X POST -H "X-TV-Token: <token>" localhost:8770/browser/restart`.
3. Open the check page in the kiosk:
   ```bash
   curl -s -X POST localhost:8770/app -H "X-TV-Token: <token>" -H 'Content-Type: application/json' \
        -d '{"url": "http://127.0.0.1:8770/home/drm.html"}'
   ```
   It shows "Widevine: available" (the tab's title becomes `DRM OK`).

Linux browsers only get **software (L3) Widevine**, so Prime plays at around
720p at best. HBO Max has blocked Linux browsers since 2020; Max in the browser
is expected to fail (that's what Android is for, see [android.md](android.md)).
Tubi and Pluto TV play as full-screen websites.

## 8. YouTube's TV app

The YouTube button opens **YouTube's own TV app** (`youtube.com/tv`, what smart
TVs run), not the website. YouTube only serves it to TV user agents, so it runs
as a second kiosk Chromium with a Tizen smart-TV user agent and its own profile
(`~/.config/hearth-youtube`, where its sign-in is kept).

- The d-pad drives it. The remote's Back sends Esc while it's in front
  (Alt+Left would leave `/tv`).
- To sign in: pick Sign in on the TV and enter the code at `youtube.com/activate`
  on a phone. Once linked, a phone's YouTube app can also cast to it.
- Home, Play or any other app closes it. Opening it again always starts it
  fresh: a YouTube left on its exit screen behind the menus once "didn't
  respond". `_youtube_janitor` closes a YouTube left hidden behind the menus
  for 30 s. It finds the menus' window by its title, `Hearth - Chromium`, so
  keep the home page's `<title>` as "Hearth" or change both.
- Casting a YouTube link from the remote plays it in mpv instead (no ads, and
  the server can see what's playing).

## 9. The player (mpv) and why its flags are what they are

Measured on the Pi 4 with a 1080p60 TV:

- `--hwdec=v4l2m2m,drm,…`: H.264 goes to the stateful v4l2m2m decoder (0
  dropped frames, ~8 % CPU at 1080p) and HEVC to the stateless `drm` path.
  `--hwdec=auto-safe` alone silently picked **software** decoding.
- `--vo=gpu --gpu-api=opengl`: with Vulkan, the Pi 4 couldn't import the
  decoder's DMA-BUFs (482 "mapping DRM dmabuf failed" in 8 s).
- `--profile=fast`: the default GPU renderer dropped 102 frames in 12 s of 1080p
  H.264 (visibly stuttery); `fast` dropped 0–3. `--vo=dmabuf-wayland` also
  dropped none, but shows no picture at all for software-decoded video.
- YouTube is asked for `avc1` ≤ 1080p first (`YTDL_FORMAT`) so the hardware
  decoder gets it. A 1080p YouTube cast played with 0 drops and A/V sync ≈ 0.
- **Measure on the real TV, not headless.** With no screen attached, labwc
  draws to a `NOOP-1` fallback output where mpv ran at ~0.56× real time. That's
  the virtual output, not the Pi.
- `--force-window=no --audio-display=no`: music makes no mpv window; the home
  page draws Now Playing itself from `/local/status` → `music`.

## 10. Sound

- **HDMI needs the TV present when PipeWire probes the card.** Booted with the
  TV off or unplugged, the HDMI card only offers `off`/`pro-audio` and sound
  goes to the headphone jack. `prefer_hdmi()` (run before anything plays) sees
  an HDMI connector reporting `connected` with no HDMI sink, and restarts
  `wireplumber pipewire pipewire-pulse` (at most every 2 min). The HDMI sink then
  appears and becomes the default.
- **Quantum.** `pipewire-hearth.conf` sets `default.clock.min-quantum = 1024`
  (~21 ms). On the original box Android's audio dropped out every few seconds
  at the default 256 samples (484 xruns on the HDMI sink); after the change,
  none. It's harmless without Android. Apply live with
  `pw-metadata -n settings 0 clock.min-quantum 1024`.
- Volume and mute are the PipeWire default sink's. The TV's own remote or a
  soundbar's mute never reaches the Pi.

## Routes

Token-gated (`X-TV-Token`), called by the server:

| Route | What |
|---|---|
| `GET /status` | `{mode: home\|player\|app, player, browser, music, playing, audio, android, android_available, time}` |
| `POST /play {url, title?, start?}` | mpv plays a URL (library file, Jellyfin stream, yt-dlp link) |
| `POST /music/play {queue, start?, start_at?}` · `/music/append {radio, queue}` · `/music/continue {queue, radio, station}` · `/music/insert {item, mode: next\|queue}` · `GET /music/queue` | music queues (a radio station tops itself up with `append`) |
| `POST /control {action}` | `toggle pause resume seek seek_to stop next previous jump subtitles audio_track volume volume_set mute` |
| `POST /app {name}` or `{url}` | `youtube`, `tubi`, `pluto`, `prime`, `max`, `android`, or any URL in the kiosk |
| `POST /home_screen` · `POST /browser/restart` | |
| `POST /input` | `{type: key\|text\|move\|click\|scroll, …}` |
| `GET /audio` · `POST /audio/default {id}` | sinks |
| `GET /snapshot` | an mpv frame (JPEG) while video plays, else the screen (PNG) |
| `POST /android/prepare` · `POST /android/refresh` | Android only (409 / `ok: false` without it) |

Localhost only (the TV's own page): `/home`, `/home/<file>`, `/local/status`,
`/local/open`, `/local/open_android`, `/local/android/prepare`, and relays to the
server's `/api/tv/*`: `/local/library`, `/local/show/<id>`,
`/local/jf_image/<id>`, `/local/station_art`, `/local/play_library`,
`/local/play_music`, `/local/music_index`, `/local/music_artist/<id>`,
`/local/radio`, `/local/play_radio`, `/local/podcasts`, `/local/podcast/<id>`,
`/local/play_podcast`, `/local/podcast_art/<id>`, `/local/tvapi/<path>` (an
allowlist: lists, the queue, song feedback, playlists), plus `/local/music_queue`
and `/local/music_control`. A relay whose server is down answers 502.

Without Waydroid, `/app {name: android}` answers 503 "Android isn't set up on
this box", and Prime and Max (from the remote or the Streaming page's tiles)
open their websites instead.

## The TV home page

`receiver/home/index.html` is the 10-foot UI: plain HTML and JS, arrow keys and
OK (from the remote's d-pad, through uinput) or clicks.

- **Home**: YouTube · Streaming · Jellyfin. **Streaming**: Prime Video, Tubi,
  Max, Pluto TV.
- **Jellyfin**: shelves of posters (Continue watching, Movies, Shows, Music,
  Radio, playlists, Podcasts); a show's page, one shelf per season; a
  Resume / Start over chooser; Albums and Artists A to Z (a letter typed on the
  remote's keyboard jumps); artist pages with Shuffle and Radio; list pages
  (Play all, Shuffle, start from a song; ▶ opens a song's menu: Play next, Add
  to queue, ♥, Not for me, Add to a playlist).
- **Now Playing**: the cover, a progress bar that runs between polls, and the
  queue (▲ ▼ browse, OK plays the one picked). Back leaves the music playing; a
  NOW PLAYING card in every header (◀ from the first card) brings it back.
- An Android app on its way shows each step with its own clock against how
  long it usually takes.
- The look is all CSS (palette variables at the top of the `<style>`), with no
  image files. Tile glyphs come from DejaVu Sans.

## Known issues and notes

- `dmesg` shows occasional `vc4-drm gpu: swiotlb buffer is full` on a Pi 4. No
  playback effect was seen.
- No server-side transcoding: the Pi plays original files (H.264 and HEVC in
  hardware). A file in a codec the Pi can't decode in hardware plays in
  software, if at all.
- Some sites (Prime, Max) expect a mouse; the remote's touchpad and ▲/▼ scroll
  buttons are the fallback. Prime's arrow keys can't leave the Play button on a
  show page to reach the episode list; the scroll buttons can.
- "Two mpvs" (fixed): the startup thread used to call `ensure()` without the
  player lock and raced a request. `Player.lock` is now an RLock held by both.
- Missing tools don't crash the receiver: without mpv, commands answer
  `{"error": "mpv unreachable …"}`; without `wpctl` the volume is `null`;
  without `wlrctl` nothing can be focused or fullscreened.

Tests: `python -m pytest tests/test_receiver_android.py -q` (no Pi needed: evdev
is mocked and the receiver is loaded beside a throwaway `config.json`).
