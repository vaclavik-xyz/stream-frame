#!/bin/sh
# Play the headset's stream fullscreen on this machine (built for the Steam Deck).
#
#   deck-player.sh [rtsp|hls|browser] [host]
#
# rtsp is the default: mpv talks to MediaMTX directly, so the picture is under a
# second old and the sound comes with it. hls is the same player against the
# playlist — six seconds late, but it shrugs off wifi that comes and goes.
# browser opens the player page instead (WebRTC, no sound).
set -eu
mode=${1:-rtsp}
host=${2:-${STREAMFRAME_HOST:-frame.local}}

# Wake the encoder before the player lands: the daemon starts it on the first
# request, and ten seconds of spinner on a television looks broken.
curl -s -o /dev/null -m 6 "http://$host:8101/" || true
curl -s -o /dev/null -m 6 "http://$host:8101/live.m3u8" || true

case "$mode" in
  browser)
    # --ozone-platform=x11 is the difference between a window and a silent no-op
    # when Steam launches this inside the gamescope session: without it Chrome
    # starts, creates no window and makes no request at all.
    exec flatpak run com.google.Chrome \
      --kiosk --app="http://$host:8101/" --start-fullscreen --window-position=0,0 \
      --ozone-platform=x11 --no-sandbox --disable-dev-shm-usage \
      --autoplay-policy=no-user-gesture-required \
      --no-first-run --no-default-browser-check
    ;;
  hls)
    src="http://$host:8101/live.m3u8"
    ;;
  *)
    src="rtsp://$host:8554/vr"
    ;;
esac

# mpv when it is installed: proper clock sync and hardware decode. ffplay ships
# with the Deck's own ffmpeg, so the stream works even on a machine that was
# never set up for it.
if flatpak info io.mpv.Mpv >/dev/null 2>&1; then
  set -- --fullscreen --no-border --no-osc --really-quiet --no-terminal \
         --profile=low-latency --keep-open=no --force-window=yes
  [ "$mode" != "hls" ] && set -- "$@" --rtsp-transport=tcp
  exec flatpak run io.mpv.Mpv "$@" "$src"
fi

# ffplay has no -nostdin (that is an ffmpeg option); passing it made the player
# exit instantly with "Option not found".
set -- -fs -loglevel error -fflags nobuffer -flags low_delay \
       -framedrop -probesize 500000 -analyzeduration 0 -autoexit
[ "$mode" != "hls" ] && set -- "$@" -rtsp_transport tcp
exec ffplay "$@" "$src"
