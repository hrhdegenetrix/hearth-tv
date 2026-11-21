# The server (`hearth/`)

The server is the brains of the box. The Raspberry Pi on the TV (`receiver/`) only shows things: it runs mpv, a kiosk Chromium and a virtual keyboard and mouse. Everything else lives here, in one FastAPI app on port 5080:

- the phone remote (`hearth/static/`), with its own login;
- the library, through Jellyfin: movies, shows and music, with resume points and watched marks kept in Jellyfin;
- music: a queue on the Pi's mpv, an A-to-Z index, radio stations, daily lists, a song menu and Jellyfin playlists;
- podcasts, from Apple's free directory and the shows' own RSS feeds;
- casting links (YouTube and anything yt-dlp plays go to mpv; streaming sites go to the kiosk browser);
- resumable uploads into the media share, and a watcher that tells Jellyfin when files are copied in by hand;
- optionally, a USB CD/DVD drive that rips discs into the libraries, with phone notifications.

Radio stations and Music Flamingo have their own doc: [radio.md](radio.md).

```
phone ── http(s)://<server>:5080 ──► hearth/server.py :5080 ──X-TV-Token──► receiver :8770 (Pi)
                                          │                                  ├─ mpv ◄── Jellyfin stream URLs (JELLYFIN_PI_URL)
                                          ├── Jellyfin :8096 (catalog, user session reports)
                                          └── hearth/data/ (radio, lists, podcasts, uploads…)
```

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # then fill it in (below)
.venv/bin/python -m hearth.server            # or the systemd unit: systemd/hearth.service
.venv/bin/python -m pytest tests -q
```

The server reads `.env` from the repo root. It logs to stdout (`journalctl -u hearth -f` under systemd). Background work (the status poll, feed refreshes, Jellyfin reports) is wrapped in `hearth/errors.py`'s `@if_errors`: a failure is logged with its traceback and the loop goes on.

## Configuration (`.env` at the repo root)

| Key | Meaning | Default |
|---|---|---|
| `TV_PASSWORD` | The phone remote's login password. No password set means nobody can sign in. | required |
| `TV_SECRET_KEY` | Signs the session cookie. Any long random string (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`). | required |
| `TV_RECEIVER_TOKEN` | Shared secret with the Pi's `~/hearth/config.json` (`token`). | required |
| `TV_RECEIVER_URL` | Where the Pi's receiver is. | `http://hearth-tv.local:8770` |
| `TV_PORT` / `TV_HOST` | Where this server listens. | `5080` / `0.0.0.0` |
| `TV_COOKIE_SECURE` | `1` when the remote is reached over HTTPS (a Cloudflare tunnel, `tailscale serve`). Leave `0` for a plain-http remote on the home network, or the browser will drop the cookie and the login will loop. | `0` |
| `TV_MEDIA_ROOT` | The media share Jellyfin reads, as this host sees it: `Movies/`, `Shows/<Show>/`, `Music/`. Uploads land here, the watcher watches it, and disc rips go into it. | `/mnt/media` |
| `JELLYFIN_URL` | How this server reaches Jellyfin. | `http://127.0.0.1:8096` |
| `JELLYFIN_PI_URL` | How the **Pi** reaches Jellyfin. Every stream URL mpv opens is built from it, so it must be the server's LAN (or Tailscale) address, never `127.0.0.1`. | `JELLYFIN_URL` |
| `JELLYFIN_API_KEY` | Jellyfin dashboard → API Keys. Used for catalog reads and stream URLs. | required |
| `JELLYFIN_USER` / `JELLYFIN_PASSWORD` | The household's Jellyfin user. Playback reports, hearts and playlists go through this user's own session (see *Jellyfin* below). | required |
| `TV_ITUNES_XML` | An old `iTunes Music Library.xml`, for play history (optional; [radio.md](radio.md)). | `<TV_MEDIA_ROOT>/Music/iTunes/iTunes Music Library.xml` |
| `TV_DISCS` | `1` watches the USB CD/DVD drive. | `0` |
| `TV_DISC_DEVICE` | The drive. | `/dev/sr0` |
| `TV_DISC_SCRATCH` | Rips assemble here, on a local disk, then move into the share. | `/var/tmp/hearth-disc-rips` |
| `TV_MUSIC_DIR` / `TV_MOVIES_DIR` | Where rips land. | `<TV_MEDIA_ROOT>/Music`, `<TV_MEDIA_ROOT>/Movies` |
| `TV_CONTACT` | An email address or URL for the server's owner. MusicBrainz asks every client to put one in its User-Agent, and Web Push sends it to the push services as the VAPID contact. Optional; without it a placeholder is sent. | unset |
| `FLAMINGO_DESCRIPTIONS` / `FLAMINGO_SSH` | Where Music Flamingo's descriptions are read from. See [radio.md](radio.md). | |

`TV_JELLYFIN_MEDIA_ROOT` (the share as Jellyfin's container sees it, default `/media`) is not read by the server; the Music Flamingo audio prep uses it to find the files ([radio.md](radio.md)).

Runtime data lives in `hearth/data/` (gitignored, created on first use):

| File | What |
|---|---|
| `radio.json` | ♥ loves, "Not for me", per-station bans, renamed iTunes playlists, ids of the playlists made here (`mine`) |
| `radio_session.json` | the station playing now, so a restart picks it back up |
| `daily.json` | each station's list for today |
| `listen_log.jsonl` | every song the TV played: how long, skipped or not, which station |
| `itunes_history.json` | the cached iTunes match |
| `podcasts.json`, `podcast_progress.json`, `podcast_art/` | subscriptions, resume points, artwork |
| `uploads/` | upload chunks and their sidecars |
| `discs.jsonl`, `disc_logs/` | the disc ledger and each rip's whole tool output |
| `push_subs.json`, `vapid_private.pem` | phone subscriptions and the Web Push key (generated on first use, mode 600) |
| `station_art/` | optional station covers ([radio.md](radio.md)) |
| `state.json` | the server's own small state |

## How playback works

1. **A movie or episode.** `POST /api/play {id}` asks Jellyfin for the item and its resume point, then sends the Pi `JELLYFIN_PI_URL/Videos/<id>/stream?static=true&api_key=…`. That is the original file with Range support and no transcode, so the Pi's hardware decoder does the work. The status poll (every 2 s while the Pi answers) reports progress to Jellyfin every 10 s and on pause/unpause, and closes the Jellyfin session 15 s after our item stops.
2. **Music.** `POST /api/music/play` (an album, an artist, a song within its album, a TV album card, or "shuffle everything") builds a queue of `/Audio/<id>/stream?static=true` URLs and sends it to the receiver's `/music/play`. The poll reports each song's start and stop to Jellyfin (play counts), writes the listening log, notices skips, and tops up a radio station's queue.
3. **A link.** `POST /api/cast {url}`: hosts in `BROWSER_DOMAINS` (Prime, Max, Netflix, Tubi, Pluto…) open in the kiosk browser, because they need logins and DRM. Everything else goes to mpv, which resolves it with yt-dlp.
4. **YouTube search.** `GET /api/search?q=` runs `yt-dlp --flat-playlist -J ytsearch15:<q>`. The server looks for `yt-dlp` next to its own Python first (a venv), then on `PATH`.
5. **Remote buttons** go straight through to the receiver: `/api/control`, `/api/media`, `/api/input`, `/api/app`, `/api/home`, `/api/snapshot`, `/api/audio`.

The Pi is marked offline only after 15 s without a good answer (`OFFLINE_AFTER_SECONDS`): it stalls for a few seconds while it opens an app, and a single slow answer used to flash "The TV box isn't answering".

Every upstream failure (the Pi, Jellyfin, a feed, YouTube) comes back to the phone as **HTTP 424** with a plain `detail`. Not 502/503: a Cloudflare tunnel in front of the remote replaces 502/503 bodies with its own page, and the remote would lose the message.

`/api/status` strips `api_key=` out of every URL it returns (`_no_key`). The key rides the stream URLs because mpv can't send headers, so it travels between the server, Jellyfin and the Pi, never to the phone.

## Jellyfin

Jellyfin owns the catalog. Run it in Docker on the server, with its config and cache on a local Linux filesystem (its SQLite database does badly on NTFS or network shares):

```bash
docker run -d --name jellyfin --restart unless-stopped --user 1000:1000 -p 8096:8096 \
  -v /srv/jellyfin/config:/config -v /srv/jellyfin/cache:/cache \
  --mount type=bind,source=/mnt/media,target=/media,readonly,bind-propagation=rslave \
  jellyfin/jellyfin:latest
```

- **Library names matter.** The server finds the libraries by name: create them as **Movies** (`/media/Movies`, type Movies), **Shows** (`/media/Shows`, type Shows) and **Music** (`/media/Music`, type Music).
- **Authorization header.** Jellyfin 12 rejects the old `X-Emby-Authorization` header with a 400. Every call here sends `Authorization: MediaBrowser Client="Hearth", Device="hearth-server", DeviceId="…", Version="1.0", Token="…"`.
- **Reports must come from a user session.** A session signed with the API key belongs to nobody, and Jellyfin silently drops its playback reports: no resume point and no watched mark. `Jellyfin.user_token()` signs in as `JELLYFIN_USER` (`/Users/AuthenticateByName`, DeviceId `hearth-playback`, so signing in again replaces the old token instead of piling up devices), keeps the token in memory, and signs in again on a 401. Catalog reads and stream URLs use the API key.
- Jellyfin keeps no resume point for items under 5 minutes (`MinResumeDuration`); a short item counts as watched instead.
- **⚠ Never delete a library item through Jellyfin's API or web UI: it deletes the real file.** The read-only bind above doesn't always hold. When the share is a CIFS mount that arrives after Docker starts, it reaches the container through `bind-propagation=rslave` without the read-only flag, and Jellyfin can write to it (it once wrote a `<name>.xml` beside every `.m3u` playlist it imported). If that matters, mount the share read-only on the host for Jellyfin, or recreate the container with a recursive read-only bind. An empty `.ignore` file in a folder makes Jellyfin skip it.
- Jellyfin also imports `.m3u` files it finds in the Music folder as playlists. The server leaves those alone: only playlists made from the remote (their ids are in `radio.json` `mine`) are shown as "my playlists".
- Jellyfin's apps (Finamp on a phone, the web UI) see the same hearts, playlists and play counts, because they all go through the household user.

### The share watcher

Files copied straight into the share (a file manager on another computer, not the remote's upload) reach Jellyfin only at its 12-hour scheduled scan when the share is a network mount: its realtime monitor gets no change events over CIFS/SMB. `_nas_watch()` walks `TV_MEDIA_ROOT` every 2 minutes. Videos are tracked per file (size, mtime). Music is tracked per folder (track count, folder mtime), because each file is a network stat over CIFS and a song copies in seconds. When anything changes and then holds still for one round (the copy finished), it calls `/Library/Refresh`, so a file appears 2–4 minutes after its copy ends. It never scans while the folder is missing or empty: an unmounted mount point is an empty folder, and a scan of it would make Jellyfin drop the whole library.

## Music

- `music_index.py` builds the TV's A-to-Z index from three Jellyfin calls (~1 s for a library of several thousand songs) and caches it 10 minutes; the share watcher and disc rips expire it. Jellyfin makes one album per folder, and iTunes-style rips often file a compilation's songs under each artist's own folder, so one soundtrack becomes dozens of one-song albums. The index puts folders with the same album name back together, except where two of them are whole albums by different artists (10+ songs each) and except "Unknown Album". On an artist's page, a compilation plays only that artist's songs. Nothing on disk or in Jellyfin changes; the phone's Library still shows Jellyfin's own albums.
- **Lists.** An album, an imported iTunes playlist, one of the household's own playlists, or a station's list for today opens as a page: `GET /api/list?kind=album|playlist|mine|station&id=…`, `POST /api/list/play {kind, id, start, shuffle}`.
- **The queue.** `POST /api/queue {song, mode: next|queue}` puts a song right after the one playing, or after the songs already queued (the receiver's `/music/insert`). Inside a station, a queued song carries the station's session id, so the station keeps going around it. With nothing playing, the song just plays.
- **A song's menu.** `POST /api/song/feedback {song, kind: love|unlove|dislike|undislike}`. ♥ goes into `radio.json` and Jellyfin's favorite. "Not for me" keeps the song off every station, today's lists and Shuffle all, and skips it if it's playing; an album or playlist still plays it when picked.
- **The household's own playlists** are Jellyfin playlists made with the user token: `GET/POST /api/playlists`, `POST /api/playlists/{id}/add {songs}`, `…/remove {entries}` (Jellyfin playlist entry ids), `…/rename {name}`, `DELETE /api/playlists/{id}`.
- A song in the library twice (same title, artist and album, lengths within 3 s) plays only as its most-played copy everywhere except where it was picked by hand.

## Podcasts

`podcasts.py` is the engine. Shows are found through Apple's free directory (`itunes.apple.com/search?media=podcast`, no key) or added by RSS link. Feeds are RSS 2.0 with iTunes tags (Atom works too), parsed with defusedxml with a 20 MB cap and a 20 s timeout, and fetched with conditional GET. Each show keeps up to 300 episodes, and episodes the feed drops stay playable. Feeds are re-checked every 2 h by a 20-minute loop; a failed one is retried after 15 min and keeps its old episodes. Artwork is shrunk to a ≤600 px JPEG.

`POST /api/podcasts/play {episode, from_start}` sends the publisher's own enclosure URL to the Pi as a one-item queue, starting at the resume point. The poll saves the position every 10 s; within 60 s of the end, or at 97 %, the episode counts as played.

## Uploads

A chunked, resumable upload from the phone into `Movies/` or `Shows/<show>/`: 32 MB chunks, because Cloudflare refuses any single request over 100 MB.

- `POST /api/upload/start {name, size, folder, probes}` → `POST /api/upload/chunk/{id}/{index}` (raw body, sequential) → `POST /api/upload/finish/{id}` → poll `GET /api/upload/status/{id}` (`moving` → `done`, or `error` with the chunks kept for a retry).
- **Resumable.** Each upload has a sidecar `<id>.json`, so it survives a reload of the phone page or a server restart; unfinished ones are kept 3 days. `probes` is the sha256 of the first 64 KB of every chunk, computed on the phone; a restart only continues an upload whose chunks match, so a different file with the same name can't be stitched on. An orphaned `.part` with no sidecar is adopted the same way, minus its half-written last chunk.
- **Retries can't corrupt.** Each chunk write seeks to the bytes received so far and truncates first. A chunk that arrives short is a 400 and isn't counted; a wrong index is a 409 with `{next}`.
- **The move into the share runs in the background**, size-checked, because a big file's move can outlast Cloudflare's 100 s request limit. Then Jellyfin rescans.
- `GET /api/uploads` lists unfinished uploads; `DELETE /api/upload/{id}` discards one.
- Through a tunnel the speed is the home internet's *upload* speed, even with the phone at home. For a pile of files, copying straight into the share from a computer on the LAN is much faster (the watcher picks them up).

## Discs (optional, `TV_DISCS=1`)

A USB CD/DVD drive on the server, ripped from the remote's Discs tab. The server's user must be in the `cdrom` group. Tools: `whipper` (CDs), `flac` (for `metaflac`), `lsdvd` and `HandBrakeCLI` (DVDs), `eject`. Most commercial DVDs also need libdvdcss (Debian/Ubuntu: `libdvd-pkg`). Ripping discs you own for your own use is your call and your local law's.

- ⚠ Some slim USB drives come with a Y-cable whose second plug carries data: with only one plugged in, the drive lights up and spins, but the kernel never sees it.
- **The drive** is polled every 3 s with the kernel's CDROM ioctls, because a USB drive's media changes don't reliably reach udev. One job at a time. The ledger is `hearth/data/discs.jsonl`, and each rip's whole tool output is in `hearth/data/disc_logs/<job>.log`.
- **A CD.** The MusicBrainz disc id comes from the kernel's TOC in ~0.02 s (whipper's own cdrdao read can take ~3 min), and `musicbrainz.org/ws/2/discid/<id>` gives the releases, their tracks and Cover Art Archive covers. MusicBrainz allows about one request a second and answers 503 past it, so lookups retry. The CD **rips by itself** when every release is one album and `Music/<Artist>/<Album>` isn't there yet. Otherwise the tab shows the choices (or "already in your Music") and waits; with several different albums (box sets differ), it rips the first one after 3 minutes.
- **whipper** runs `whipper -e never cd rip -R <release> -C complete -k -r 2` with the track template `%A/%d/%t %n` (`%A/%d/Disc %N/%t %n` for a set): cdparanoia with AccurateRip verification, FLAC with tags and cover art. `~/.config/whipper/whipper.conf` gets `path_filter_vfat = True` so names are safe on an SMB share.
  - `-r 2`: two tries a track, not whipper's five. Across 102 track rips, no retry ever saved a track, and a scratched track's retries took hours.
  - `-k`: a song that can't be read is skipped and the rest go in. The card names the missing songs ("Couldn't read 09 …, probably a scratch"), read from whipper's log or, for versions that only print "track 9 failed to rip.", from its output.
  - **A crawling song is skipped after 10 minutes**, or 1.5× its own length if longer, all its tries together (its length comes from cd-paranoia's command line). A scratched song once read for over an hour while cd-paranoia kept reporting crumbs of progress.
  - **A frozen read stops after 10 silent minutes** (`STALL_SECONDS`), and the songs read so far are filed. The whole rip is capped at 4 hours, and past that what it read is still filed.
  - The first CD finds the drive's read offset (`whipper offset find`), which takes a few minutes. If that fails, set it by hand in `whipper.conf` (`[drive:…]` `read_offset`; +6 is the usual value for LG/HL-DT-ST drives). The offset only affects the bit-exact AccurateRip check, not the sound.
  - Right after a disc goes in, a drive may not report an audio CD yet; the server asks again three times, 5 s apart, before calling it unreadable.
- **A CD MusicBrainz doesn't know** can be named on the tab's form (artist, year, album, one box per song; a pasted track list fills the boxes). Names save as they're typed (`POST /api/disc/names`). When the rip ends, each FLAC is renamed "NN Title.flac" and tagged with metaflac; the album goes to `Music/<artist>/<album>` (or `Unknown Artist/Unknown album <date>`). After the rip, **Save to my Music** (`apply: true`) retitles, retags and moves the folder in place.
- **A DVD.** `lsdvd -Oy` reads the label and the longest title, and the label becomes a title guess ("SOME_MOVIE" → "Some Movie"). It waits for the title to be corrected and **Rip to Movies**. HandBrakeCLI: `--main-feature -Z "HQ 480p30 Surround"` (`HQ 576p25 Surround` for PAL) `-f av_mkv --all-audio -E copy --audio-copy-mask ac3,eac3,dts,dtshd,mp2,aac,mp3 --audio-fallback av_aac --all-subtitles` → `Movies/<Name (Year)>/<Name (Year)>.mkv`. Every audio track is kept as it is on the disc: the preset alone turns them all into stereo. `lpcm` is not in the mask because HandBrake can't copy it and refuses to start.
- Rips assemble in `TV_DISC_SCRATCH`, are copied into the share, Jellyfin rescans, and the tray opens.
- Routes: `GET /api/disc` (`enabled` says whether `TV_DISCS` is on), `POST /api/disc/rip {release?, name?, year?, unknown?, names?}`, `/api/disc/names`, `/api/disc/stop`, `/api/disc/reread`, `/api/disc/eject`. With `TV_DISCS=0` the actions answer 409.

## Push (phone notifications)

A rip that silently waits on a question nobody saw can lose a day, so the Discs tab has **Notify me on this phone**: Web Push to the installed remote (`hearth/push.py`, shown by `static/sw.js`). Notices: a DVD waiting for its name, which version of a CD, already in your Music / unknown CD, done with the tray open (plus a scratch warning), failed. A tap opens `/#discs`.

- Needs `pywebpush` and `py-vapid` (in `requirements.txt`; without them push answers 424 and everything else works), and the remote on **HTTPS**: browsers only allow push on a secure origin.
- The VAPID key is generated into `hearth/data/vapid_private.pem` on first use. The VAPID contact is `TV_CONTACT` (or a placeholder).
- Routes: `GET /api/push/key`, `POST /api/push/subscribe {subscription, device}`, `/unsubscribe {endpoint}`, `/check {endpoint}`, `/test`. `/sw.js` is served at the root with no cookie so its scope is the whole remote.

## API reference

Cookie-authed (the remote's session) unless noted.

- **Auth:** `POST /api/login {password}` (5 wrong tries from one address in 15 min → 429; the address is `cf-connecting-ip` behind Cloudflare), `POST /api/logout`.
- **Status:** `GET /api/status` → `{online, age, mode: home|player|app|offline, player, browser, playing, music, audio, now, android, radio, loved}`. `android` is `{state, for, …}` while an Android app is on its way. `GET /health` (no auth).
- **Library:** `GET /api/library`, `GET /api/library/show/{series_id}`, `GET /api/jf/image/{id}?w=`, `POST /api/play {id, start?}`.
- **Music:** `GET /api/music`, `/api/music/artist/{id}`, `/api/music/album/{id}`, `/api/music/search?q=`, `POST /api/music/play {album|artist|track|albums+tracks|everything, shuffle, start}`, `GET /api/music/queue`.
- **Radio:** `GET /api/radio`, `POST /api/radio/play {station, shuffle?}`, `POST /api/radio/feedback {kind: love|unlove|ban}`, `POST /api/radio/playlist_name {playlist, name}`, `GET /api/radio/art?station=`.
- **Lists, queue, playlists:** `GET /api/list`, `POST /api/list/play`, `POST /api/queue`, `POST /api/song/feedback`, `GET/POST /api/playlists`, `POST /api/playlists/{id}/add|remove|rename`, `DELETE /api/playlists/{id}`.
- **Podcasts:** `GET /api/podcasts`, `GET /api/podcasts/search?q=`, `POST /api/podcasts/subscribe {feed_url}`, `DELETE /api/podcasts/{show_id}`, `GET /api/podcasts/show/{show_id}`, `POST /api/podcasts/play {episode, from_start}`, `POST /api/podcasts/played {episode, played}`, `GET /api/podcasts/art/{show_id}`.
- **Casting:** `POST /api/cast {url, mode: auto|player|browser, title?}`, `GET /api/search?q=`.
- **Remote:** `POST /api/control {action}` (toggle, pause, resume, seek {seconds}, seek_to {seconds}, stop, subtitles, audio_track, volume {step}, volume_set {level}, mute, next, previous, jump {index}…), `POST /api/media {action: playpause|rewind|fastforward}` (mpv gets real ±10 s seeks; apps and sites get media keys), `POST /api/input` (keys, text, mouse moves, clicks, scrolls), `POST /api/app {name: youtube|tubi|prime|max|pluto|android}` (up to 240 s: Android may have to boot), `POST /api/android/prepare`, `POST /api/home` (up to 60 s: it waits behind an app that is still opening), `POST /api/browser/restart`, `GET /api/snapshot`, `GET /api/audio`, `POST /api/audio/default`.
- **Uploads:** `POST /api/upload/start`, `/api/upload/chunk/{id}/{index}`, `/api/upload/finish/{id}`, `GET /api/upload/status/{id}`, `GET /api/uploads`, `DELETE /api/upload/{id}`.
- **Discs and push:** above.
- **The TV's own screens** (the receiver relays them from the Pi's home page): `X-TV-Token` instead of the cookie. `GET /api/tv/library`, `/api/tv/show/{id}`, `/api/tv/jf_image/{id}`, `POST /api/tv/play`, `POST /api/tv/music/play`, `GET /api/tv/music/index`, `/api/tv/music/artist/{id}`, `GET /api/tv/radio`, `POST /api/tv/radio/play`, `GET /api/tv/radio/art`, `GET /api/tv/list`, `POST /api/tv/list/play`, `/api/tv/queue`, `/api/tv/song/feedback`, `GET/POST /api/tv/playlists`, `POST /api/tv/playlists/{id}/add`, `GET /api/tv/podcasts`, `/api/tv/podcasts/show/{id}`, `POST /api/tv/podcasts/play`, `GET /api/tv/podcasts/art/{id}`.
- **Pages:** `/` (redirects to `/login` when signed out), `/login`, `/sw.js`, `/manifest.webmanifest`, `/static/…`. Pages are served with `?v=<mtime>` on every `/static/` file they name, because a CDN in front (Cloudflare caches `.css`/`.js`) would otherwise serve a stale `remote.js` that silently loses new buttons. The manifest versions its icon URLs the same way, which is what makes an installed Android app pick up a new icon.

## Tests

`python -m pytest tests -q` from the repo root. `test_radio.py` (stations, the iTunes matcher, daily lists, Not for me, Flamingo's families), `test_music_index.py`, `test_podcasts.py` (no network: httpx's MockTransport), `test_nas_watch.py`. None of them needs Jellyfin, the Pi or the network.
