# Setting up Hearth, start to finish

This runbook is written for an AI coding agent working on the owner's behalf (or a patient human). Do the steps in order, and **run each step's check before moving on**. Most failures here are quiet: a 200 that carried no data, a cookie that never stuck, a sink that isn't HDMI. Checks catch them early.

Ask the owner before anything that costs money, deletes files, or changes the router or DNS. Their media is theirs: never delete a library item through Jellyfin (see step 2).

## 0. What you need to know first (ask the owner)

- Which machine is the **server**. It should be always on, with Linux and Docker. Where is the media: a local disk, or a NAS share?
- The **Pi**: its hostname or IP, and a user you can ssh into with a key.
- Whether they want **HTTPS from outside the house** (a Cloudflare tunnel or `tailscale serve`), or LAN only. LAN only is simplest; start there.
- Whether there's an **NVIDIA GPU** for the Music Flamingo pass (step 7), and whether there's a USB disc drive (step 8).
- An **email or URL** they're happy to put in MusicBrainz's User-Agent and the Web Push contact (`TV_CONTACT`). It's only needed for discs and notifications.

## 1. The media share

Lay the media out like this, at one path on the server (call it `TV_MEDIA_ROOT`, e.g. `/mnt/media`):

```
Movies/<Name (Year)>/<Name (Year)>.mkv
Shows/<Show Name>/Season 01/<Show Name> - S01E01 - Title.mkv
Music/<Artist>/<Album>/<NN Title>.flac        (Jellyfin reads the tags, not the folder names)
```

A NAS share: mount it from `/etc/fstab` (CIFS with a credentials file, or NFS) so it's back after a reboot. Add `x-systemd.automount` or `_netdev` so boot doesn't hang when the NAS is slow.

**Check:** `ls "$TV_MEDIA_ROOT"/{Movies,Shows,Music} | head` lists files.

## 2. Jellyfin

```bash
docker run -d --name jellyfin --restart unless-stopped --user "$(id -u):$(id -g)" \
  -p 8096:8096 \
  -v /srv/jellyfin/config:/config -v /srv/jellyfin/cache:/cache \
  --mount type=bind,source=/mnt/media,target=/media,readonly,bind-propagation=rslave \
  jellyfin/jellyfin:latest
```

- Put config and cache on a **native Linux filesystem**. Jellyfin's SQLite dislikes NTFS and network shares.
- `bind-propagation=rslave` lets a share mounted *after* Docker started still show up inside the container.
- ⛔ **That read-only flag may not reach a network mount that arrives later.** On the original setup, Jellyfin could still write to the CIFS share despite `readonly`: it wrote playlist `.xml` files beside imported `.m3u` files. **Never delete an item through Jellyfin's web UI or API: it deletes the real file.** For a truly read-only view, mount the share before Docker starts, or try Docker 25+'s `bind-recursive=readonly` (untested here).
- In the web setup (http://server:8096), create the household's user and three libraries: **Movies** → `/media/Movies` (type Movies), **Shows** → `/media/Shows` (Shows) and **Music** → `/media/Music` (Music).
- Dashboard → API Keys → make a key named `Hearth`.
- Jellyfin's realtime monitor gets no change events over CIFS. Hearth's NAS watcher asks Jellyfin to rescan about 2–4 min after a copy finishes (docs/server.md).

**Check:** `curl -s -H 'Authorization: MediaBrowser Client="Hearth", Device="setup", DeviceId="setup", Version="1.0", Token="<KEY>"' http://127.0.0.1:8096/Items/Counts` shows movie, series and song counts. ⛔ Jellyfin 12 rejects the old `X-Emby-Authorization` header (400). Use `Authorization: MediaBrowser …` as above.

## 3. The server

```bash
sudo mkdir -p /opt/hearth-tv && sudo chown "$USER" /opt/hearth-tv
git clone <this repo> /opt/hearth-tv && cd /opt/hearth-tv
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install yt-dlp          # YouTube search + link resolving
curl -fsSL https://deno.land/install.sh | sh   # yt-dlp needs a JavaScript runtime for YouTube; put deno on PATH or in .venv/bin
sudo apt install ffmpeg
cp .env.example .env && chmod 600 .env   # fill it in: docs/server.md has every key
```

Fill in `.env`:
- `TV_PASSWORD`, `TV_SECRET_KEY` and `TV_RECEIVER_TOKEN` (make the token and key with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`).
- The Jellyfin key, user and password.
- `TV_MEDIA_ROOT`.
- `TV_RECEIVER_URL`: the Pi, e.g. `http://192.0.2.20:8770`.
- `JELLYFIN_PI_URL`: **how the Pi reaches Jellyfin**, e.g. `http://192.0.2.10:8096`. Every stream URL the Pi's mpv opens is built from it, so it must work *from the Pi*. `127.0.0.1` there is the classic mistake: the library browses fine, and nothing plays.

Then install the unit: edit `systemd/hearth.service` (User, paths), copy it to `/etc/systemd/system/`, and run `sudo systemctl enable --now hearth`.

**Checks:**
- `curl -s http://127.0.0.1:5080/health` answers.
- Open `http://server:5080` on the phone and log in. If the login "works" but bounces back to the login page, the cookie didn't stick: `TV_COOKIE_SECURE=1` over plain http does exactly that. Use 0 on the LAN.
- Library → Music lists albums.
- `/api/status` says `online: false` until the Pi is up. That's expected.

## 4. The Pi

Follow **docs/receiver.md**: Raspberry Pi OS (Trixie) desktop with labwc autologin, its packages, the uinput udev rule, screen blanking off, the venv with yt-dlp + deno, and `~/hearth/config.json` (`token` = `TV_RECEIVER_TOKEN`, `server` = the server's URL as the Pi sees it). Then deploy from the server:

```bash
PI=user@pi-host receiver/deploy.sh
```

**Checks:**
- The TV shows Hearth's home screen.
- The phone's Now tab says the box is online.
- The remote's arrows move the focus on the TV.
- Play a movie from Library. It must start within a few seconds, with sound **from the TV** (if sound comes from the Pi's headphone jack, see receiver.md § 10. Sound).
- Seek, pause, and stop. Play again and it offers to resume.
- Peek (snapshot) shows the frame.

## 5. Streaming sites and YouTube

- **YouTube:** the remote's YouTube button opens YouTube's TV app on the TV. Sign in there once, with the remote.
- **Widevine** (Prime Video's website etc.): open a DRM site once. Chromium downloads Widevine in the background; restart the browser from the remote (or wait a minute and reopen). `http://127.0.0.1:8770/home/drm.html` on the Pi says "DRM OK" when it works. Linux gets software (L3) Widevine only, so expect roughly 720p. HBO Max blocks Linux browsers.
- Android apps are optional and advanced: docs/android.md.

**Check:** cast a YouTube link from the phone (it plays through mpv, H.264 ≤1080p) and open Tubi from the remote.

## 6. Music and radio

- Library → Music → Radio lists stations once the library has enough songs (60 per station).
- Play one, skip a song early, ♥ another. The Now tab shows the station and the heart. The heart also shows in Jellyfin's own apps.
- Optional: an old iTunes `iTunes Music Library.xml` (`TV_ITUNES_XML`) gives the radio years of play counts and playlists. The server matches it to Jellyfin's songs by itself.

**Check:** `python -m pytest tests -q` passes. Then on the phone, a genre station's songs fit the genre.

## 7. Music Flamingo (genres for untagged music)

Skip this step if every song already has a sensible Genres tag. Otherwise read **docs/radio.md § Music Flamingo** first. In short:

1. The owner reads and accepts NVIDIA's Music Flamingo license (noncommercial, home use) on its Hugging Face page; if the page asks them to agree before downloading, they also make a read token (`hf auth login`).
2. On the GPU box: create a venv, install a CUDA build of torch, then `pip install -r flamingo/requirements.txt`. Download the model: `hf download nvidia/music-flamingo-hf --local-dir ~/models/music-flamingo-hf` (about 16.5 GB; older huggingface_hub releases call it `huggingface-cli download`).
3. On the server: `.venv/bin/python flamingo/prepare_audio.py --remote user@gpu-box:~/flamingo`. Leave off `--remote` if the GPU is in the server.
4. On the GPU box: `python listen.py --home ~/flamingo --model ~/models/music-flamingo-hf --limit 5`. Read the five descriptions in `~/flamingo/out/describe.jsonl`; each should start by naming a genre. Then run it without `--limit`, at night with `--deadline`, until it says `PASS COMPLETE`.
5. On the server: set `FLAMINGO_SSH=user@gpu-box:~/flamingo/out/describe.jsonl` (or `FLAMINGO_DESCRIPTIONS` for a local file). Then run `.venv/bin/python -m hearth.flamingo_genres refresh`, then `… unmatched` to see what landed nowhere.
6. Add the nightly cron lines from docs/radio.md § Keeping it fresh, so new music gets heard.

**Check:** `python -m hearth.flamingo_genres stats` shows family counts. The phone's Radio list gains stations (e.g. Classical) that were missing before.

## 8. Discs (optional)

On the server:
- `sudo apt install whipper flac lsdvd handbrake-cli`. For DVDs, also libdvdcss (Debian/Ubuntu: `libdvd-pkg`); that's the owner's call.
- Add the service user to the `cdrom` group.
- Set `TV_DISCS=1` and `TV_CONTACT` in `.env`.
- Set whipper's read offset for the drive: `whipper drive analyze`, then `whipper offset find` with a common CD.
- Rips land on a local disk first (`TV_DISC_SCRATCH`), then move into `Music/` or `Movies/`.

Details and the guards against scratched and crawling discs are in docs/server.md § Discs.

**Check:** insert a CD. The phone's Discs tab shows the album from MusicBrainz and starts ripping. When it's done, the album is in Library → Music within a few minutes.

## 9. From outside the house (optional)

Pick one, with the owner:
- **`tailscale serve`** on the server: only their own devices can reach it.
- **A Cloudflare tunnel** (`cloudflared`) to `localhost:5080`.

With either, set `TV_COOKIE_SECURE=1` and restart. Never port-forward 5080 or the Pi's 8770 on the router. Hearth's errors come back as HTTP 424 rather than 502/503, because Cloudflare replaces 502/503 bodies with its own page. Uploads go in 32 MB chunks for Cloudflare's 100 MB request limit.

## When something's wrong

- `journalctl -u hearth -f` on the server; `journalctl -u hearth-receiver -f` on the Pi.
- **Remote says the box isn't answering.** Check `curl -H "X-TV-Token: <token>" http://pi:8770/status` from the server.
- **Video stutters.** Measure on the real TV over HDMI. With no TV attached, the Pi's virtual output runs video at about half speed and misleads you (receiver.md § Known issues and notes).
- **No sound.** The Pi probed HDMI while the TV was off (receiver.md § 10. Sound).
