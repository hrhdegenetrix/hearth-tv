# Android on the TV box (optional)

Some streaming services work badly or not at all in a Linux browser: Max blocks
Linux browsers, and Prime is capped by software Widevine. Hearth can run
Android's own apps for them under **Waydroid** on the Pi. This is the most
fragile part of the system. Skip it unless you need it: without Waydroid, Prime
and Max open their websites, and everything else is unaffected
([receiver.md](receiver.md)).

> **What was verified, and where.** All of this was done on **one Raspberry Pi 4
> (4 GB), Raspberry Pi OS Trixie, labwc, Waydroid 1.6.2, Android 11** and a
> 1080p TV, and lived with for a few weeks. Prime Video episodes played; Max
> opened to its home screen (playback quality inside Android was never
> measured). Nothing here was tried on a Pi 5 or on any other Android image.
> Expect to adapt.

## What it took

### Waydroid + the image

- Waydroid 1.6.2 running **Android 11**: the LineageOS 18.1 GAPPS system image
  plus the MAINLINE vendor image, placed in `/etc/waydroid-extra/images`. Android
  13 (lineage-20) also booted, but its DRM had only ClearKey, no Widevine.
- Host changes (the same ones the Pi-Apps Waydroid installer makes, plus the
  memory cgroup):
  - `nftables` installed and `LXC_USE_NFT="true"` in
    `/usr/lib/waydroid/data/scripts/waydroid-net.sh` (the Pi kernel has no legacy
    `ip_tables`). The Waydroid apt source was then removed so an update can't
    undo that edit (Pi-Apps does the same).
  - `/etc/dnsmasq.d/waydroid.conf` keeps the host's dnsmasq off `waydroid0`.
  - `/boot/firmware/cmdline.txt` += `cgroup_enable=memory cgroup_memory=1 psi=1`
    (keep a backup). Without them Android's `lmkd` dies and Android reboots in a
    loop: the Pi firmware sets `cgroup_disable=memory`, and PSI is off by default.
- Props: `persist.waydroid.width/height` = 1920×1080, `persist.waydroid.multi_windows`
  = false; in `waydroid_base.prop`, `ro.sf.lcd_density=240` and
  `qemu.hw.mainkeys=1` (no navigation bar). The status bar is hidden by the
  `hidestatusbar` overlay.

### Widevine (L3) on arm64

Installed with waydroid_script (`-a 11 install widevine`; files land in
`/var/lib/waydroid/overlay/vendor`). The prebuilt is an Android 9-era **32-bit**
HAL and needed two fixes:

1. It opens `/dev/host_hwbinder` (a Halium name). An overlay init file,
   `vendor/etc/init/<name>-hwbinder.rc`, symlinks it to `/dev/hwbinder` at
   early-init.
2. It brings its own `libhwbinder.so` and `libhidltransport.so` (merged into
   `libhidlbase` since Android 10), which crashed `ProcessState` (SIGBUS in
   `RefBase::incStrong`). They were replaced in `overlay/vendor/lib{,64}` with
   empty shims whose only content is `DT_NEEDED libhidlbase.so`, the same trick
   AOSP uses. They were built with `clang --target=armv7a-linux-androideabi` /
   `aarch64-linux-android -shared -nostdlib -fuse-ld=lld`, then
   `patchelf --add-needed libhidlbase.so`. Keep the originals somewhere.

Check: `lshal` inside Android lists `android.hardware.drm@1.1::IDrmFactory/widevine`
and `ICryptoFactory/widevine`. It's a lazy HAL that starts when an app asks.

### Getting apps

- The image's Play Store (20.4.33) crashed on browse and search before it could
  update itself.
- Apps came through **Aurora Store** (the F-Droid build, installed with
  `pm install`; `waydroid app install` failed silently), deep-linked with
  `market://details?id=<package>` because Aurora's own feed could crash on
  duplicate entries. `receiver/android_app.sh install <package>` automates this
  (it reads Aurora's buttons off screenshots, so it's tied to that Aurora version
  and a 1080p screen). `list`, `launch` and `shot` help too.
- Google Play needs the device registered as uncertified: get its `android_id`
  (`sudo waydroid shell -- sh -c "sqlite3 /data/data/*/*/gservices.db 'select * from main where name = \"android_id\";'"`)
  and register it at google.com/android/uncertified, then sign in.
- Android's media volume started at 5/15. Raise it once with volume-up
  keyevents (Android remembers it); the remote's volume (PipeWire) does the rest.
- Turned off as never needed on a TV, to cut wake-up work:
  `pm disable-user` on `com.google.android.syncadapters.calendar`,
  `…syncadapters.contacts`, `com.google.android.apps.restore`.
- Audio dropouts at PipeWire's default 256-sample quantum: fixed by
  `pipewire-hearth.conf` (1024; see [receiver.md](receiver.md) § Sound).

### Which apps

`ANDROID_APPS` in `receiver.py` holds Prime Video
(`com.amazon.avod.thirdpartyclient`) and Max (`com.wbd.stream`). Both run
landscape and fill the screen.
- **Tubi's** phone app is portrait-only (a strip in the middle of the TV), so
  Tubi is the full-screen website.
- **Pluto TV's** app decoded live TV in software (Android here has no hardware
  video decoding) at ~170 % CPU, and the picture froze while the sound ran on.
  The website plays smoothly in Chromium, so Pluto is the website too.
- Only one streaming app at a time: a Pi 4 with two or three running ANR'd.

**Android TV mode was tried and reverted.** Declaring `android.software.leanback`
+ `android.hardware.type.television` flips Android into TV mode, but LineageOS's
phone SystemUI crash-looped in it, and the installed apps are phone builds with
no TV launcher activity anyway.

## How the receiver drives it (`class Android`)

**Android's own home screen is never shown.** The TV's Streaming page launches
apps by package (`/local/open_android`), and the remote's Prime/Max buttons do
the same (`/app`).

- `open(package)`:
  1. boots the session if needed (`waydroid session start`, ~30–50 s cold, then
     waits for Android's network to validate: an app opened in that gap said
     "An internet connection is required");
  2. **wakes** Android (`waydroid container unfreeze` + `KEYCODE_WAKEUP`). A
     launch alone doesn't wake a sleeping Android, and Waydroid froze it again
     21 s later with no window;
  3. `close_all()`: force-stops every streaming app in one `waydroid shell`
     round-trip and closes every Android window. Leftover windows of an app's
     earlier run stole focus;
  4. `waydroid app launch <package>`. This makes a fullscreen task in its own
     `waydroid.<package>` window. A plain `am start` landed in a freeform window
     with a caption bar;
  5. waits up to 40 s for that window, checking every 5 s that Android's
     graphics haven't died, then focuses and fullscreens it with `wlrctl`.
- **Home closes the app** (`close_all`), so no sound plays on behind the home
  page and its graphics memory is freed. A hidden app once held ~200 MB of the
  Pi 4's 512 MB graphics memory. Android itself stays parked: Waydroid freezes
  it about a minute after it sleeps (no CPU), so the next open takes seconds,
  not a cold boot.
- **Home never waits.** While an open (or a restart) holds the lock, `hide()`
  sets `cancel` and returns at once. The open checks `cancel` between steps and
  puts Android away. Before this, Home presses made during a 72 s restart
  queued behind the lock and closed the app the moment it appeared.
- **Progress.** Each open records its steps (Starting / Restarting / Waking
  Android, Closing other apps, Starting {app}, Waiting for {app}'s picture),
  with a learned "usually" per step (`android_timings.json` on the Pi). `/status`
  carries them as `android`; the TV page and the remote draw them with running
  clocks.
- **The janitor** (`_android_janitor`, every 2 s while Android is in front):
  fullscreens the *focused* Android window (apps open new windows as they go,
  like Prime's player); sends a WAKEUP key every ~25 s so Android never sleeps
  under a paused show; reads `dumpsys media_session` for the remote's play/pause
  icon; and puts Android away when its launcher (`com.android.launcher3/`) comes
  to the front, meaning the app was exited. Match the launcher's package:
  Prime's own entry activity is called `LauncherActivity`.
- Back sends `KEYCODE_BACK`; the remote's transport row sends
  `KEYCODE_MEDIA_PLAY_PAUSE` / `REWIND` / `FAST_FORWARD` into Android.
- The pointer comes back to mid-screen from the bottom-right corner, never
  across the top edge, where it pulled Android's notification shade.

## Android's display crash, and the defenses

The recurring failure: Android's **hardware composer aborts**
(`wl_abort` in `hwc_set` → `wl_proxy_marshal_array_constructor_versioned`),
surfaceflinger dies with it, and `system_server` keeps its dead connection. From
then on every launch fails (`createSurface: Broken pipe`) until the session is
restarted. On the original box it happened after Android had sat frozen for
34 h and 65 h (19 h was fine), once after ~13 h running, and four times in ten
minutes when the box had been up 11 days with almost no free memory. **The
mechanism is unproven.**

What the receiver does about it:
- `graphics_broken()`: Waydroid's processes run on the host kernel, so the
  receiver reads surfaceflinger's and system_server's start times from the
  host's `/proc`. In a healthy session surfaceflinger is the older one; a newer
  one, or none, means broken. Checked before every open, while waiting for a
  window, and every 30 s while Android is off screen; a broken session is
  restarted in the background (`waydroid session stop` + start, ~70 s).
- A session older than 10 min that won't take the wake-up key is wedged, and is
  restarted before the launch. A fresh one busy with first-boot work gets more
  time.
- **Readied ahead**: when the Streaming page opens or the remote's page comes
  up, `prepare()` boots, restarts or wakes Android (at most once a minute).
- **A fresh Android each morning** (05:00–07:00, only when nothing is playing
  and the remote has been quiet 30 min), followed by `warm_up()`: WAKEUP keys
  every 25 s keep the new session awake through its first-boot work (Play
  services, dex2oat; Pi load up to ~18 for ~10 min), so that work doesn't wait
  for the next app opened.
- ⛔ Don't put Android to sleep from a shell (`input keyevent 223`). Waydroid
  freezes the container while that same `waydroid shell` command is still
  running, and it hangs until the next thaw. For the same reason, a signal sent
  to a frozen Android lands only at the next thaw: run
  `sudo waydroid container unfreeze` before poking at it by hand.
- **Optional nightly reboot** (`"nightly_reboot": true` in `config.json`):
  between 04:00 and 05:00, if the box has been up 20 h and nobody is using it.
  Memory was the suspect in the four-crashes evening: swap ~1 GB, `kcompactd` at
  82 % CPU, ~490 MB of the 512 MB CMA pool in graphics buffers. Watch
  `/sys/kernel/debug/dri/0/bo_stats` and `cma_alloc_fail` in `/proc/vmstat`;
  `CmaFree` near 0 is *not* proof of exhaustion (CMA also hosts movable pages).

## Not done / open questions

- Android at 720p (`persist.waydroid.width/height`): every buffer would be under
  half the size, and L3 Widevine streams SD anyway. Untested.
- A larger CMA pool (needs a test boot). Untested.
- Whether video inside Android stays smooth over a whole film was never
  measured.

Tests for all of the above (no Pi needed): `tests/test_receiver_android.py`.
