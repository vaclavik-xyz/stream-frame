#!/usr/bin/env bash
# Kill switch: stop capturing frames from the compositor.
# Safe to run at any time — it never touches SteamVR while it is running.
set -euo pipefail

DROPIN="${HOME}/.config/systemd/user/steamvr.service.d"
mkdir -p "$DROPIN"

cat > "$DROPIN/99-vrstream-off.conf" <<'EOF'
[Service]
Environment=VRSTREAM_CAP_OFF=1
EOF

systemctl --user stop vrstream.service 2>/dev/null || true
systemctl --user stop mediamtx.service 2>/dev/null || true
systemctl --user daemon-reload

echo "capture disabled (VRSTREAM_CAP_OFF=1) and streamer stopped."
echo "the running compositor keeps its layer until SteamVR restarts;"
echo "to re-enable: rm $DROPIN/99-vrstream-off.conf && systemctl --user daemon-reload"
