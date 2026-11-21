# Hearth

A homemade casting box and music radio for the living-room TV.

- A **Raspberry Pi** behind the TV plays videos, music and streaming sites.
- Your **phone is the remote**: a web app you add to the home screen.
- A Linux **media server** in the house runs the brains, with **Jellyfin** as the library.

Its best trick is the **radio**: Pandora-style stations made from your own music. They're shaped by what you play, skip and ♥, and filled in by a local AI model, NVIDIA's Music Flamingo, that has listened to every song. That way, untagged CD rips still land on the right genre station. Nothing about your music goes to the cloud.

## What it does

- **Phone remote:** a d-pad, a touchpad and a keyboard for the TV. Volume and mute. A peek at what's on screen.
- **Library:** movies and shows from Jellyfin, with resume points that Jellyfin's other apps share. Uploads from the phone go straight into the library, resumably.
- **Music:** albums and artists A to Z, with compilations put back together. There's a song menu (Play next, Add to queue, ♥, Not for me, Add to playlist), and your Jellyfin playlists.
- **Radio:** Old favorites, Deep cuts, Time capsule years, decades, about 19 genre stations, artist radio and song radio. Each station's list holds still for the day ([docs/radio.md](docs/radio.md)).
- **Podcasts:** search Apple's directory or paste an RSS link. Resume points included.
- **YouTube:** YouTube's own TV app on the TV, and casting any link from the phone (mpv + yt-dlp).
- **Streaming sites** in a kiosk browser (Tubi, Pluto TV, Prime Video's website with Widevine). Optionally, Android apps through Waydroid ([docs/android.md](docs/android.md), advanced).
- **Discs (optional):** put a CD in the server's USB drive and it rips itself into the Music library (whipper + MusicBrainz). DVDs go into Movies (HandBrake). The phone gets a notification when a disc needs you.
- **The TV's own screen:** a 10-foot home page, poster shelves, Now Playing with the queue, all driven by the remote's arrow keys.

## How it fits together

```
phone ── http(s) ──► hearth server :5080 (FastAPI, this repo's hearth/)
                       ├─ Jellyfin :8096 (Docker) ── the media share: Movies/ Shows/ Music/
                       ├─ USB CD/DVD drive (optional)
                       └─ token, LAN ──► Pi receiver :8770 (receiver/)
                                           ├─ mpv (videos, music queue, podcasts)
                                           ├─ kiosk Chromium (TV home page, streaming sites, YouTube TV)
                                           ├─ uinput keyboard + mouse (the remote's d-pad and touchpad)
                                           └─ PipeWire (volume, HDMI audio)
GPU box (can be the server) ── flamingo/: Music Flamingo listens to each song once ──► radio genres
```

## Setting it up

**[docs/setup.md](docs/setup.md)** is the runbook, written so an AI coding agent (or a patient human) can follow it from bare hardware to music playing. Details live in:

| Doc | What |
|---|---|
| [docs/setup.md](docs/setup.md) | the whole install, in order, with a check after each step |
| [docs/server.md](docs/server.md) | the server: config, API, Jellyfin notes, uploads, discs, push |
| [docs/receiver.md](docs/receiver.md) | the Pi: OS, packages, video flags, Widevine, YouTube TV, deploy |
| [docs/remote.md](docs/remote.md) | the phone remote |
| [docs/radio.md](docs/radio.md) | how the stations work, and the Music Flamingo pass |
| [docs/android.md](docs/android.md) | optional Android apps on the Pi (Waydroid) |

## Hardware

- A Linux box for the server and Jellyfin (any always-on x86 or ARM machine with a few GB of RAM). The media can live on it or on a NAS share mounted on it.
- A Raspberry Pi 4 (4 GB) or newer, an HDMI TV, and Ethernet if you can (Wi-Fi measured ~35 Mbps on a Pi 4: fine for 1080p, short for 4K remuxes).
- For the radio's Music Flamingo pass: an NVIDIA GPU. It was built on a 32 GB card; a 24 GB card should fit with smaller batches, but that's untested. It only has to run once per song, and it can be a borrowed machine.
- Optional: a USB CD/DVD drive on the server.

## Licenses to know about

- **Music Flamingo** is under NVIDIA's OneWay Noncommercial license: home and research use only. Whoever runs it accepts that license on Hugging Face.
- DVD ripping uses libdvdcss. Whether that's legal for your own discs depends on where you live; it's your call.

## Tests

```bash
python -m pytest tests -q
```
