#!/bin/bash
# Hearth — install / inspect Android apps on the TV box's Waydroid (Android 11).
# Optional: only for a box set up per docs/android.md. Runs ON the Pi
# (deploy.sh copies it to ~/hearth/android_app.sh). Needs grim and python3-pil.
#
#   android_app.sh list                 third-party apps + versions
#   android_app.sh install <package>    Aurora details page → Install → wait until installed
#   android_app.sh open-page <package>  just open Aurora's page for <package>
#   android_app.sh launch <package>     start the app
#   android_app.sh shot [out.png]       screenshot of the TV (1920x1080)
#
# The Play Store in this image (20.4.33) crashes on browse/search, and Aurora's
# own "For you" feed can crash on duplicate entries, so apps are reached by
# deep link: market://details?id=<pkg> sent to Aurora. Aurora asks "Continue
# to Aurora Store?" for links from the shell; taps don't land on that sheet,
# so we Tab until its Open button shows the focus tint, then Enter.
set -u
export XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-/run/user/$(id -u)} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-wayland-0}
W="timeout 20 sudo waydroid shell --"
TOP=18   # screen y where Android's 1920x1044 display starts (window letterbox; measured on one Pi 4 + 1080p TV)

shot() { grim -t png -l 1 "${1:-/tmp/android_shot.png}"; }

open_focused() {   # 1 when the sheet's Open button (around 1282,1030) has the focus tint
  grim -t png -l 1 -g "1240,1005 80x50" /tmp/aa_open.png
  python3 -c "from PIL import Image; r,g,b=Image.open('/tmp/aa_open.png').convert('RGB').getpixel((10,25)); print(1 if (r<240 and b>245) else 0)"
}

action_pill() {   # Android-space center of Aurora's purple Install/Open button, or nothing
  shot /tmp/aa_full.png
  python3 - "$TOP" <<'PY'
import sys
from PIL import Image
top = int(sys.argv[1])
im = Image.open('/tmp/aa_full.png').convert('RGB')
xs, ys = [], []
for y in range(200, 340, 2):
    for x in range(220, 520, 2):
        r, g, b = im.getpixel((x, y))
        if b > 200 and 95 <= r <= 165 and 80 <= g <= 150:
            xs.append(x); ys.append(y)
if len(xs) > 150:
    print((min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2 - top)
PY
}

installed() { timeout 15 sudo waydroid shell -- pm list packages 2>/dev/null | grep -qx "package:$1"; }

open_page() {
  $W input keyevent 3; sleep 1
  $W am start -a android.intent.action.VIEW -d "market://details?id=$1" -p com.aurora.store >/dev/null
  sleep 5
  for n in 1 2 3 4 5 6 7 8 9; do
    $W input keyevent 61; sleep 0.5
    if [ "$(open_focused)" = 1 ]; then $W input keyevent 66; sleep 7; return 0; fi
  done
  echo "could not focus Aurora's Open button — look at: $0 shot" >&2; return 1
}

case "${1:-}" in
  list)
    timeout 20 sudo waydroid shell -- sh -c 'for p in $(pm list packages -3 | cut -d: -f2); do echo "$p $(dumpsys package $p | grep -m1 versionName | tr -d " ")"; done' 2>/dev/null ;;
  open-page) open_page "$2" ;;
  install)
    pkg="$2"
    if installed "$pkg"; then echo "$pkg already installed"; exit 0; fi
    timeout 15 sudo waydroid shell -- appops set com.aurora.store REQUEST_INSTALL_PACKAGES allow
    open_page "$pkg" || exit 1
    xy=$(action_pill)
    [ -z "$xy" ] && { echo "no Install button found — look at: $0 shot" >&2; exit 1; }
    $W input tap $xy
    for i in $(seq 1 75); do installed "$pkg" && { echo "$pkg installed (~$((i*4))s)"; exit 0; }; sleep 4; done
    echo "$pkg not installed after 5 min — look at: $0 shot" >&2; exit 1 ;;
  launch) $W monkey -p "$2" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 ;;
  shot) shot "${2:-/tmp/android_shot.png}"; echo "${2:-/tmp/android_shot.png}" ;;
  *) sed -n 2,12p "$0"; exit 2 ;;
esac
