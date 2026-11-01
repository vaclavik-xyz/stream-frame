#!/bin/sh
# Play the headset's stream fullscreen on the Steam Deck's own session.
#
#   deck-stream.sh start [host]   switch to the desktop session and play RTSP
#   deck-stream.sh stop           stop the player and go back to Game Mode
#   deck-stream.sh status [host]  what session, what player, how many readers
#
# Steam refuses to launch the non-Steam shortcut on this Deck ("game
# configuration is not available"), so the working route is the desktop session:
# it owns the TV output just as well, and a mode switch is one command.
set -u

LOG=/tmp/deck-stream.log
UNIT=deck-stream-mpv
HOST_DEFAULT=${STREAMFRAME_HOST:-frame.local}
# The player has to be started inside the graphical session, with the session's
# own bus and runtime dir: without them mpv opens no window at all.
RUNTIME=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}
SESSION_ENV="--setenv=DISPLAY=:0 --setenv=XDG_RUNTIME_DIR=$RUNTIME
  --setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=$RUNTIME/bus"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"
}

session_name() {
    if pgrep -x plasmashell >/dev/null 2>&1; then echo plasma; else echo gamescope; fi
}

player_running() {
    systemctl --user is-active --quiet "$UNIT" 2>/dev/null
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

cmd_start() {
    host=${1:-$HOST_DEFAULT}
    # the player lives next to this script; it owns the mpv/ffplay choice and the
    # waking, so there is one place to fix a player problem
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
    # shellcheck disable=SC2086  # SESSION_ENV is a list of options on purpose
    systemd-run --user --unit="$UNIT" --collect $SESSION_ENV \
        /bin/sh "$player" rtsp "$host" >>"$LOG" 2>&1
    log "player started for $host"
    echo "playing rtsp://$host:8554/vr"
}

cmd_stop() {
    systemctl --user stop "$UNIT" 2>/dev/null || true
    log "player stopped"
    if [ "$(session_name)" = plasma ]; then
        log "switching back to gamescope"
        steamos-session-select gamescope
    fi
    echo "stopped"
}

cmd_status() {
    host=${1:-$HOST_DEFAULT}
    echo "session: $(session_name)"
    if player_running; then echo "player: running"; else echo "player: stopped"; fi
    body=$(curl -s -m 5 "http://$host:8101/status" 2>/dev/null || true)
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
    *)      echo "usage: $0 {start [host]|stop|status [host]}" >&2; exit 1 ;;
esac
