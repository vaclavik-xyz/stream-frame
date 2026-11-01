# Settings from the player

The daemon is otherwise read-only on purpose. This is the one place that changes a running pipeline,
and it is built so that a mistake costs a second of picture, never a dead stream.

## What can change

| setting | range | reaches |
|---|---|---|
| `bitrate` | 1M – 12M | encoder |
| `fps` | 15 – 120 | encoder |
| `layout` | `mono-flat`, `mono-flat-right`, `mono-round`, `passthrough`, `stereo` | encoder |
| `threads` | 0 – 8 (0 = whatever x264 picks) | encoder |
| `nice` | 0 – 19 | encoder |
| `adapt` | on / off | encoder |
| `record` | on / off | encoder |
| `record_time` | 30 – 3600 s | encoder |
| `gaze` | on / off | the eye reader only |

Everything except `gaze` lives in the ffmpeg command line, so it costs a restart: the picture blinks
for a second, WebRTC has to reconnect, the open MP4 chunk is closed properly and a new one starts.
`gaze` only starts or stops the reader, so the picture never notices.

## What a change does, in order

1. **Validate.** Ranges above, plus types. A value outside them is refused with a sentence that says
   which one and why — never clamped silently.
2. **Apply** the values to the live config.
3. **Prove it.** If the change touches the encoder, it is restarted and watched: a command line that dies
   immediately is a bad setting, not a slow start. The watch ends as soon as the frame counter climbs
   twice — usually about half a second — so a good save feels like a save; the eight-second deadline is
   only for a pipeline that stays up without producing anything.
4. **Roll back** if it did not survive: the old values go back, the old pipeline is restarted, and the
   answer is `409` with the reason.
5. **Persist** — but only what just ran. The file is written after step 3, so a value that failed can
   never come back after a reboot.

## Where it is stored

`~/.config/streamframe/settings.json`, mode `0600`, written atomically. On start the daemon reads it,
validates it again, and applies what passes; a damaged or outdated file is ignored with a log line, not
a crash. The command line stays the baseline.

```bash
curl -s http://<host>:8101/api/settings                     # current values + ranges
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"bitrate":"6M"}' http://<host>:8101/api/settings   # 200, restarted or not
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"fps":500}' http://<host>:8101/api/settings        # 400, nothing touched
```

With `--token` on, both need `?token=…` (or the cookie the page sets). Anyone holding the token can
change the pipeline; that is the point of the token.

**Escape hatches.** `--no-settings` ignores the file and writes nothing. Deleting
`~/.config/streamframe/settings.json` has the same effect on the next start. `--settings <path>` puts it
somewhere else.

## The player

- One capsule sits bottom-left: transport, resolution and capture rate, bitrate, how far the picture is
  behind (green when in sync, amber once it is seconds off), and a `rec` timer. It fades to a quarter of
  its opacity after a few idle seconds and comes back on any movement.
- A vignette and the wordmark keep the picture from looking like a bare `<video>` element.
- **Hiding everything** is a state, not a keystroke: the eye button (or `c`) drops the bar, capsule,
  wordmark and vignette and leaves only the picture; a small eye in the corner brings them back. The
  choice survives a reload, and it is the only way to get the overlays back on a phone, where there is
  no keyboard.
- The gear (or `s`) opens the panel; `Esc` closes it. While a change is applied the panel says so and
  the encoder's restart is announced before you press apply, not after.

The panel reads its ranges from `/api/settings`, so the numbers live in the server, not in two places.
