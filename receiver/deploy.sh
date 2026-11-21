#!/bin/bash
# Push the Hearth receiver to the TV box (the Pi) and restart it.
#
#   PI=user@tv-box receiver/deploy.sh        # e.g. PI=pi@hearth-tv.local
#
# Copies receiver.py, home/, android_app.sh, the PipeWire drop-in and the
# systemd unit (filled in for the Pi's user) into ~/hearth on the Pi, then
# restarts hearth-receiver. ~/hearth/config.json (token, port, server) lives only
# on the Pi and is never overwritten; write it before the first deploy
# (docs/receiver.md § config.json).
#
# A restart stops whatever is playing: the new receiver reaps the old mpv
# (Player._reap_orphan), and a deploy once cut a playlist off mid-song. So it
# refuses while the TV is in use (the player is active or a site/app is open);
# HEARTH_FORCE=1 overrides.
set -euo pipefail
PI=${PI:?set PI=user@host for the TV box, e.g. PI=pi@hearth-tv.local}
HERE=$(cd "$(dirname "$0")" && pwd)

if ! ssh "$PI" 'test -f ~/hearth/config.json'; then
  cat >&2 <<'MSG'
~/hearth/config.json is missing on the Pi. Write it first (chmod 600), e.g.:
  {"token": "<the server's TV_RECEIVER_TOKEN>", "port": 8770, "server": "http://<server-host>:5080"}
MSG
  exit 2
fi

if [ "${HEARTH_FORCE:-0}" != 1 ] && ssh "$PI" 'systemctl is-active --quiet hearth-receiver'; then
  busy=$(ssh "$PI" 'port=$(python3 -c "import json,os; print(json.load(open(os.path.expanduser(\"~/hearth/config.json\"))).get(\"port\", 8770))");
                    curl -s -m 5 "localhost:$port/local/status"' | python3 -c 'import json,sys
d=json.load(sys.stdin); p=d.get("player") or {}; b=d.get("browser") or {}
print("playing" if p.get("active") else ("app " + str(b.get("app")) if b.get("app") else ""))' 2>/dev/null || echo "unreachable")
  if [ -n "$busy" ]; then
    echo "The TV is in use ($busy); not deploying. Wait until it's idle, or HEARTH_FORCE=1." >&2
    exit 2
  fi
fi

# The unit runs as the Pi's desktop user (the one the screen autologs in as).
read -r PI_USER PI_GROUP PI_HOME PI_UID < <(ssh "$PI" 'echo "$(id -un) $(id -gn) $HOME $(id -u)"')
UNIT=$(mktemp)
sed -e "s|@USER@|$PI_USER|g" -e "s|@GROUP@|$PI_GROUP|g" -e "s|@HOME@|$PI_HOME|g" -e "s|@UID@|$PI_UID|g" \
  "$HERE/hearth-receiver.service" > "$UNIT"

ssh "$PI" 'mkdir -p ~/hearth/home ~/.config/pipewire/pipewire.conf.d'
scp -q "$HERE/receiver.py" "$HERE/android_app.sh" "$PI:hearth/"
scp -q -r "$HERE"/home/. "$PI:hearth/home/"
scp -q "$HERE/pipewire-hearth.conf" "$PI:.config/pipewire/pipewire.conf.d/hearth.conf"
scp -q "$UNIT" "$PI:/tmp/hearth-receiver.service"
rm -f "$UNIT"
ssh "$PI" 'chmod +x ~/hearth/android_app.sh \
  && sudo install -m 644 /tmp/hearth-receiver.service /etc/systemd/system/hearth-receiver.service \
  && sudo systemctl daemon-reload && sudo systemctl enable --quiet hearth-receiver \
  && sudo systemctl restart hearth-receiver && sleep 2 && systemctl is-active hearth-receiver'
