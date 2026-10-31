#!/usr/bin/env bash
# Install vrstream on a Linux box running SteamVR (Steam Frame / any PC VR).
#   ./scripts/install.sh                 # full install into ~/vrstream
#   ./scripts/install.sh --dry-run       # show what would happen
#   ./scripts/install.sh --no-mediamtx   # HLS only, no RTSP/WebRTC
set -euo pipefail

PREFIX="${HOME}/vrstream"
PORT=8101
WITH_MEDIAMTX=1
DRY=0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --port)   PORT="$2"; shift 2 ;;
    --no-mediamtx) WITH_MEDIAMTX=0; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

run() { if [[ $DRY -eq 1 ]]; then echo "  + $*"; else "$@"; fi; }
say() { echo "==> $*"; }
# sed replacement has to survive '&', '|' and backslashes in a user-supplied --prefix
esc() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }
PYTHON_BIN="$(command -v python3 || true)"

say "checking prerequisites"
missing=0
for bin in python3 ffmpeg cc curl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "  missing: $bin" >&2; missing=1
  fi
done
[[ $missing -eq 0 ]] || { echo "install the packages above and re-run" >&2; exit 1; }

if [[ -f "${HOME}/.local/share/vulkan/implicit_layer.d/vrcap3.json" ]] ||
   [[ -f "${HOME}/.local/share/vulkan/implicit_layer.d/vrcap2.json" ]]; then
  echo "  ! an older vrcap registration exists — it writes to the same shm and would fight this one."
  echo "    disable it first: mv ~/.local/share/vulkan/implicit_layer.d/vrcap3.json{,.disabled}"
fi

say "building the Vulkan layer"
run make -C "$REPO" layer

say "installing to $PREFIX"
run install -d "$PREFIX/bin" "$PREFIX/layer" "$PREFIX/config" "$PREFIX/server/web"
run install -m 755 "$REPO/build/libvrcap.so"      "$PREFIX/layer/libvrcap.so"
run install -m 755 "$REPO/server/vrstreamd.py"    "$PREFIX/server/vrstreamd.py"
run install -m 644 "$REPO/server/web/index.html"  "$PREFIX/server/web/index.html"
run install -m 644 "$REPO/config/mediamtx.yml"    "$PREFIX/config/mediamtx.yml"
run install -d "$PREFIX/scripts"
run install -m 755 "$REPO/scripts/smoke-test.sh"  "$PREFIX/scripts/smoke-test.sh"

say "registering the layer with the Vulkan loader"
MANIFEST_DIR="${HOME}/.local/share/vulkan/implicit_layer.d"
run install -d "$MANIFEST_DIR"
if [[ $DRY -eq 1 ]]; then
  echo "  + sed library_path -> $PREFIX/layer/libvrcap.so into $MANIFEST_DIR/vrstream.json"
else
  sed "s|\"\./libvrcap\.so\"|\"${PREFIX}/layer/libvrcap.so\"|" \
      "$REPO/layer/vrcap_layer.json" > "$MANIFEST_DIR/vrstream.json"
fi

say "enabling capture inside SteamVR"
DROPIN="${HOME}/.config/systemd/user/steamvr.service.d"
run install -d "$DROPIN"
if [[ $DRY -eq 1 ]]; then
  echo "  + write 50-vrstream.conf (VRSTREAM_CAP=1, VRSTREAM_CAP_W=1440, VRSTREAM_CAP_FPS=60)"
else
  cat > "$DROPIN/50-vrstream.conf" <<EOF
[Service]
Environment=VRSTREAM_CAP=1
Environment=VRSTREAM_CAP_W=1440
Environment=VRSTREAM_CAP_FPS=60
Environment=VRSTREAM_CAP_SHM=/vrstream_frames
EOF
fi

if [[ $WITH_MEDIAMTX -eq 1 ]]; then
  say "fetching MediaMTX (RTSP/WebRTC fan-out)"
  case "$(uname -m)" in
    aarch64|arm64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=amd64 ;;
    *) echo "  unsupported arch $(uname -m)" >&2; ARCH="" ;;
  esac
  # a rate-limited or offline API used to abort the whole install under `set -e`
  VER=""
  if [[ $DRY -eq 0 ]]; then
    VER="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest 2>/dev/null \
           | grep -o '"tag_name": *"v[^"]*' | head -1 | cut -d'"' -f4 || true)"
  fi
  if [[ -n "${ARCH:-}" && -n "$VER" ]]; then
    TMP="$(mktemp -d)"
    URL="https://github.com/bluenviron/mediamtx/releases/download/${VER}/mediamtx_${VER}_linux_${ARCH}.tar.gz"
    run curl -fsSL "$URL" -o "$TMP/mediamtx.tgz"
    if [[ $DRY -eq 0 ]]; then
      tar xzf "$TMP/mediamtx.tgz" -C "$TMP" mediamtx
      install -m 755 "$TMP/mediamtx" "$PREFIX/bin/mediamtx"
      rm -rf "$TMP"
    fi
  else
    echo "  ! could not resolve MediaMTX release; skipping (HLS will still work)" >&2
    WITH_MEDIAMTX=0
  fi
fi

say "installing systemd user units"
UNITS="${HOME}/.config/systemd/user"
run install -d "$UNITS"
if [[ $DRY -eq 0 ]]; then
  [[ -n "$PYTHON_BIN" ]] || { echo "python3 disappeared" >&2; exit 1; }
  sed -e "s|@PYTHON@|$(esc "$PYTHON_BIN")|" -e "s|%h/vrstream|$(esc "$PREFIX")|g" \
      "$REPO/systemd/vrstream.service" > "$UNITS/vrstream.service"
  sed -e "s|%h/vrstream|$(esc "$PREFIX")|g" \
      "$REPO/systemd/mediamtx.service" > "$UNITS/mediamtx.service"
  # the port is an override, so the unit copy stays readable and re-installs cleanly
  install -d "$UNITS/vrstream.service.d"
  printf '[Service]\nEnvironment=VRSTREAM_PORT=%s\n' "$PORT" \
      > "$UNITS/vrstream.service.d/10-port.conf"
fi
# user units die with the login session unless lingering is enabled — fatal for a headless box
if [[ $DRY -eq 0 ]]; then
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || echo "  ! could not enable-linger; run: sudo loginctl enable-linger $(id -un)" >&2
fi
run systemctl --user daemon-reload
run systemctl --user enable vrstream.service
[[ $WITH_MEDIAMTX -eq 1 ]] && run systemctl --user enable mediamtx.service

cat <<EOF

Done. Next steps:
  1. restart SteamVR so the compositor picks up the layer:
       systemctl --user restart steamvr.service    (or just relaunch SteamVR)
  2. start the streamer:
       systemctl --user start vrstream.service$([[ $WITH_MEDIAMTX -eq 1 ]] && echo " mediamtx.service")
  3. put the headset on and open:
       http://$(hostname -I 2>/dev/null | awk '{print $1}'):${PORT}/
  4. check health:
       $PREFIX/scripts/smoke-test.sh   (or ./scripts/smoke-test.sh in the repo)
EOF
