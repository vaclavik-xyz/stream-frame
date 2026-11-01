#!/bin/sh
# Play the headset's stream on the Steam Deck.
#
#   deck-stream.sh start [host] [--browser|--mpv]   play, picking the route that fits the session
#   deck-stream.sh stop                             stop the mpv route / tell you how to close the browser
#   deck-stream.sh status [host]                    session, route, whether the Deck is reading
#
# Two routes, same picture — one ffmpeg encodes once and tees the same H.264 stream to both:
#
#   browser  Game Mode, stays in Game Mode. Steam's own browser opens the player page over
#            HLS. No session switch, but HLS costs ~4-6 s of latency and the CEF process
#            burns about three times the CPU of mpv.
#   mpv      desktop session, RTSP. Under a second of latency and hardware decoding, at the
#            price of leaving Game Mode.
#
# So the default follows the session you are already in. Steam refuses to launch a non-Steam
# shortcut on this Deck ("game configuration is not available"), which is why the Game Mode
# route goes through the overlay browser instead of a library tile.
set -u

LOG=/tmp/deck-stream.log
UNIT=deck-stream-mpv
HOST_DEFAULT=${STREAMFRAME_HOST:-frame.local}
HTTP_PORT=${STREAMFRAME_HTTP_PORT:-8101}
RUNTIME=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
XAUTH=$(ls "$RUNTIME"/xauth_* 2>/dev/null | head -1)
# Anything that has to appear on screen must be started inside the graphical session, with
# that session's own display, bus and runtime dir: without them mpv opens no window and
# steam:// goes to a client that is not there.
SESSION_ENV="--setenv=DISPLAY=:0 --setenv=XDG_RUNTIME_DIR=$RUNTIME
  --setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=$RUNTIME/bus"
[ -n "$XAUTH" ] && SESSION_ENV="$SESSION_ENV --setenv=XAUTHORITY=$XAUTH"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"
}

session_name() {
    if pgrep -x plasmashell >/dev/null 2>&1; then echo plasma; else echo gamescope; fi
}

player_running() {
    systemctl --user is-active --quiet "$UNIT" 2>/dev/null
}

# The browser route leaves no process of ours behind, so the only honest evidence that it is
# playing is an open connection to the player page on the headset.
browser_reading() {
    host=$1
    if command -v ss >/dev/null 2>&1; then
        ss -tn 2>/dev/null | grep -q "$host:$HTTP_PORT"
    else
        return 1
    fi
}

wait_for_plasmashell() {
    i=0
    while [ "$i" -lt 40 ]; do
        if pgrep -x plasmashell >/dev/null 2>&1; then
            sleep 2      # plasma answers before it finishes drawing
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

start_browser() {
    host=$1
    url="http://$host:$HTTP_PORT/"
    # shellcheck disable=SC2086  # SESSION_ENV is a list of options on purpose
    systemd-run --user --collect --quiet $SESSION_ENV /usr/bin/steam "steam://openurl/$url" \
        >>"$LOG" 2>&1 || {
        log "ERROR: could not ask steam to open $url"
        echo "could not open the browser route, see $LOG" >&2
        return 1
    }
    log "browser opened $url"
    echo "opening $url in the Steam browser"
    echo "sound starts muted by design — tap the speaker button in the page"
}

start_mpv() {
    host=$1
    player=${STREAMFRAME_PLAYER:-$(dirname "$0")/deck-player.sh}
    if [ ! -f "$player" ]; then
        log "ERROR: player not found at $player"
        echo "deck-player.sh must sit next to $0" >&2
        return 1
    fi
    if player_running; then
        echo "already running"
        return 0
    fi
    if [ "$(session_name)" != plasma ]; then
        log "switching to plasma"
        steamos-session-select plasma
        if ! wait_for_plasmashell; then
            log "ERROR: plasmashell did not come up in 40 s"
            echo "session switch failed, see $LOG" >&2
            return 1
        fi
    fi
    # shellcheck disable=SC2086
    systemd-run --user --unit="$UNIT" --collect $SESSION_ENV \
        /bin/sh "$player" rtsp "$host" >>"$LOG" 2>&1
    log "player started for $host"
    echo "playing rtsp://$host:8554/vr"
}

cmd_start() {
    host=$HOST_DEFAULT
    route=auto
    for arg in "$@"; do
        case "$arg" in
            --browser) route=browser ;;
            --mpv)     route=mpv ;;
            -*)        echo "unknown option $arg" >&2; return 1 ;;
            *)         host=$arg ;;
        esac
    done
    if [ "$route" = auto ]; then
        if [ "$(session_name)" = plasma ]; then route=mpv; else route=browser; fi
    fi
    log "start: route=$route host=$host"
    if [ "$route" = mpv ]; then start_mpv "$host"; else start_browser "$host"; fi
}

cmd_stop() {
    systemctl --user stop "$UNIT" 2>/dev/null || true
    log "player stopped"
    if [ "$(session_name)" = plasma ]; then
        log "switching back to gamescope"
        steamos-session-select gamescope
        echo "stopped"
    else
        # The overlay browser belongs to Steam, not to us: there is no API to close it, and
        # killing steamwebhelper would take the whole UI down with it.
        echo "mpv stopped; close the Steam browser with B (or the Steam button)"
    fi
}

cmd_status() {
    host=${1:-$HOST_DEFAULT}
    echo "session: $(session_name)"
    if player_running; then
        echo "route: mpv (rtsp)"
    elif browser_reading "$host"; then
        echo "route: browser (hls)"
    else
        echo "route: idle"
    fi
    body=$(curl -s -m 5 "http://$host:$HTTP_PORT/status" 2>/dev/null || true)
    if [ -z "$body" ]; then
        echo "readers: n/a ($host unreachable)"
        return 0
    fi
    printf '%s' "$body" | sed -n \
        -e 's/.*"readers": *\([0-9]*\).*/readers: \1/p' \
        -e 's/.*"fps": *\([0-9.]*\).*/fps: \1/p' \
        -e 's/.*"frozen": *\([a-z]*\).*/frozen: \1/p'
}

case "${1:-}" in
    start)  shift; cmd_start "$@" ;;
    stop)   cmd_stop ;;
    status) shift; cmd_status "$@" ;;
    *)      echo "usage: $0 {start [host] [--browser|--mpv]|stop|status [host]}" >&2; exit 1 ;;
esac
