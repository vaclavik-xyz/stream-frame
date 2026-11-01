#!/usr/bin/env bash
# End-to-end check of a running Stream Frame instance. Read-only: it starts nothing,
# it only asks the API and downloads one segment.
#   ./scripts/smoke-test.sh [host] [port]
set -uo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-8101}"
BASE="http://${HOST}:${PORT}"
fails=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fails=$((fails+1)); }
note() { printf '       %s\n' "$*"; }

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null; }

echo "Stream Frame smoke test against $BASE"

if body="$(curl -fsS -m 10 "$BASE/healthz" 2>/dev/null)"; then
  ok "daemon answers /healthz"
else
  bad "daemon does not answer /healthz — is streamframe.service running?"
  echo; echo "$fails check(s) failed"; exit 1
fi

# /status also starts the on-demand encoder, so poll until capture delivers frames
status=""
fps=""
for _ in $(seq 1 30); do
  status="$(curl -fsS -m 10 "$BASE/status" 2>/dev/null)" || { sleep 1; continue; }
  fps="$(printf '%s' "$status" | json fps)"
  [[ -n "$fps" ]] && awk -v f="$fps" 'BEGIN{exit !(f>1)}' && break
  sleep 1
done

if [[ -n "${fps:-}" ]] && awk -v f="$fps" 'BEGIN{exit !(f>1)}'; then
  ok "capture delivers frames (${fps} fps)"
else
  bad "no frames from the source — is the headset being worn / SteamVR running?"
fi

if [[ "$(printf '%s' "$status" | json encoder)" == "True" ]]; then
  ok "encoder running ($(printf '%s' "$status" | json bitrate), layout $(printf '%s' "$status" | json layout))"
else
  bad "encoder is not running"; note "$(printf '%s' "$status" | json last_error)"
fi

playlist=""
for _ in $(seq 1 25); do
  playlist="$(curl -fsS -m 10 "$BASE/live.m3u8" 2>/dev/null)" && [[ -n "$playlist" ]] && break
  sleep 1
done

if [[ "$playlist" == \#EXTM3U* ]]; then
  ok "HLS playlist is published"
else
  bad "no HLS playlist at /live.m3u8"
fi

seg="$(printf '%s' "$playlist" | grep -v '^#' | grep '\.ts' | tail -1)"
if [[ -n "$seg" ]]; then
  code="$(curl -s -o /tmp/streamframe-smoke.ts -w '%{http_code}' -m 15 "$BASE/$seg")"
  size="$(wc -c < /tmp/streamframe-smoke.ts 2>/dev/null || echo 0)"
  if [[ "$code" == "200" && "$size" -gt 1000 ]]; then
    ok "segment $seg downloads (${size} B)"
  else
    bad "segment $seg returned $code / ${size} B"
  fi
else
  bad "playlist has no segments"
fi

api="http://${HOST}:9997/v3/paths/list"
if curl -fsS -m 3 "$api" >/dev/null 2>&1; then
  ready="$(curl -fsS -m 3 "$api" |
           python3 -c "import json,sys; d=json.load(sys.stdin); print(any(i['name']=='vr' and i['ready'] for i in d['items']))" 2>/dev/null)"
  [[ "$ready" == "True" ]] && ok "RTSP path 'vr' is live in MediaMTX" || bad "MediaMTX is up but path 'vr' has no publisher"
elif command -v ffprobe >/dev/null 2>&1; then
  # API is bound to loopback by default, so from another machine probe the stream itself
  if probe="$(timeout 12 ffprobe -v error -rtsp_transport tcp -select_streams v:0 \
              -show_entries stream=width,height -of csv=p=0 "rtsp://${HOST}:8554/vr" 2>/dev/null)" && [[ -n "$probe" ]]; then
    ok "RTSP stream answers (${probe})"
  else
    note "no RTSP answer on ${HOST}:8554 — HLS-only mode (fine, RTSP is optional)"
  fi
else
  note "MediaMTX API not reachable from here — skipping the RTSP check"
fi
note "low latency: rtsp://${HOST}:8554/vr   browser: http://${HOST}:8889/vr"

echo
if [[ $fails -eq 0 ]]; then
  echo "all checks passed"
else
  echo "$fails check(s) failed"
fi
exit $((fails > 0))
