# The phone remote

The remote is a small web app the Hearth server serves at `/` (files in `hearth/static/`:
`index.html`, `remote.js`, `remote.css`, `login.html`, `manifest.webmanifest`, `sw.js`, and the
icons). It talks only to the server's `/api/*`; the server talks to the TV box. There is no build
step: edit the files and reload.

## Signing in

`/login` asks for one password, `TV_PASSWORD` from the server's `.env`. A good sign-in sets a
long-lived session cookie. Set `TV_COOKIE_SECURE=1` when the remote is reached over HTTPS (a
tunnel, `tailscale serve`); leave it `0` on a plain-http home network, or the browser won't keep
the cookie.

## The tabs

| Tab | What it does |
|-----|--------------|
| **Now** | What's on the TV: a snapshot ("Peek at TV", refreshed every 20 s while something plays), the title, a scrub bar and ±10/±30 s, Subtitles / Audio track / Stop. For music: Previous / Next song, ♡ Love, *Not for this station* (on a radio station), 📻 *Radio from this song*, and the queue (tap a song to jump to it, ⋯ for its menu). While an Android app opens, the steps and their times. Volume and Mute, and *Play a link* (YouTube or any video URL; streaming sites open in the TV's browser). |
| **Remote** | App tiles (YouTube's TV app, Tubi and Pluto TV websites, Prime, Max, Android), a d-pad with OK, media keys, Back / Esc / Space / Tab, a touchpad (drag = move, tap = click, two fingers = scroll, two-finger tap = right-click) with ▲/▼ scroll buttons, a keyboard box that types on the TV, and *Reload page* / *Restart browser*. |
| **Library** | Jellyfin: Continue watching, Movies, Shows (episodes, with Resume / Start over). Music: Shuffle all, search, Radio stations, your own playlists (Jellyfin playlists, so other Jellyfin apps see them too), playlists from an old iTunes library (only shown when one was found), Recently added, Artists. Albums, playlists and stations open as a page: ▶ Play all, Shuffle, or tap a song to play from there; ⋯ on any song gives Play next, Add to queue, ♡ Love, Not for me, Add to a playlist. Podcasts (search Apple's directory or paste an RSS link; resume points per episode). *Add to the library*: upload a movie or a TV episode from the phone. |
| **Discs** | The server's USB CD/DVD drive (only when `TV_DISCS=1`). A CD MusicBrainz knows rips into Music by itself; with several versions it asks which (and picks the first after a few minutes); one it doesn't know gets a form for its names (paste a whole track list into the first song box). A DVD waits for its title and year. Progress, Stop, Eject, and a 🔔 switch for phone notifications. |

The App tiles are plain CSS (a glyph and the name, `.app .tile`, with an `app-<name>` class on each
if you want to tint one). No logo images ship with the repo.

## Install it on the phone's home screen

It's a PWA. In Chrome on Android: open the remote, ⋮ → *Add to Home screen* (or *Install app*).
On an iPhone: Share → *Add to Home Screen*. It then opens full-screen with its own icon
(`icon-192.png` / `icon-512.png`, an ember flame drawn so it survives the maskable crop). The
server serves `/manifest.webmanifest` with versioned icon URLs so an installed app picks up a new
icon.

## Why API errors are HTTP 424

When the server can't reach the TV box, Jellyfin, or a disc tool, it answers **424** with
`{"detail": "…"}`, never 502/503. Cloudflare (and some other proxies) replace a 502/503 body with
their own error page, so the phone would lose the message. `remote.js`'s `api()` shows `detail` as
a red toast (it stays up 5 s, long enough to read "Android isn't set up on this TV box" and the like).

## Why `/static/` files carry `?v=<mtime>`

The server stamps every `/static/` file a page names with `?v=<file mtime>` when it serves `/` and
`/login`. CDNs cache `.css` and `.js` at the edge, so without the stamp a phone can get a fresh
`index.html` with yesterday's `remote.js`, and new buttons silently do nothing. After editing
`remote.js` or `remote.css`, a reload is enough.

## Uploads

*Add to the library* sends a video in **32 MB pieces** (`/api/upload/start` → `/chunk/{id}/{n}` →
`/finish/{id}`), because tunnels refuse very large single requests. The card above the tabs shows
progress from the first byte (XHR upload events; `fetch` can't report them), speed and time left.

- **Resumable.** Before sending, the phone takes the SHA-256 of the first 64 KB of every piece;
  the server checks the pieces it already holds against them, so a reload, a dropped connection or
  a server restart picks up where it stopped, and a different file with the same name can't be
  stitched on. A reloaded remote shows "Paused. Choose the same file to finish it."
- A dropped piece is retried 5 times; the page holds a screen wake lock while sending and warns
  before it's closed (phones pause uploads when the screen locks).
- After the last piece the server moves the file into `Movies/` or `Shows/<Show>/` in the media
  share in the background and asks Jellyfin to rescan; the phone polls `/api/upload/status/{id}`.

The speed is your home internet's *upload* speed if the remote goes through a tunnel, even at
home. For a pile of files, copying them onto the media share from a computer is much faster.

## Notifications (Web Push, for discs)

On the Discs tab, **🔔 Notify me on this phone** asks for notification permission and subscribes
with the server's VAPID key (`/api/push/key`, generated into `hearth/data/` the first time). The
service worker `sw.js` (served at `/sw.js` so its scope is the whole app) shows the notices: a DVD
waiting for its name, a CD with several versions, an unknown CD, a finished rip (with any songs
that couldn't be read), a failure. Tapping one opens the remote on the Discs tab. *Send a test*
and *Turn off* are there too. Web Push needs the installed app (Chrome on Android works; iOS needs
the home-screen app and iOS 16.4+).
