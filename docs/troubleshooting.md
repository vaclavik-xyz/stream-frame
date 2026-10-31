# Troubleshooting

Every entry below is a failure that actually happened during development, not a guess.

## The page loads, the video never starts

`/status` first: `fps` near 0 means the compositor is not presenting — the headset is off the head, or
SteamVR is not running. The layer only mirrors frames that are being presented; there is nothing to
capture on an idle compositor.

If `fps` is healthy but `encoder` is `false`, read `last_error` in the same JSON and the tail of
`/tmp/vrstreamd-ffmpeg.log`.

## Works in VLC, refuses to play in Safari / iOS

Three separate causes, all fixed in the current code — check them if you fork the player:

* `#EXT-X-TARGETDURATION:0` — the playlist was served while the first segment was still being written.
  Wait until the file actually contains `#EXTINF` (the server polls up to 10 s); waiting for the file
  to *exist* is not enough, because ffmpeg creates it before it holds a segment.
* `application/octet-stream` on `.ts` — Safari will not touch it. Serve `video/mp2t` and
  `application/vnd.apple.mpegurl`.
* HTTP/1.0 with a new connection per segment — iOS gives up. Use `protocol_version = "HTTP/1.1"` and
  correct `Content-Length` (both `SimpleHTTPRequestHandler` defaults here).

Also send `Cache-Control: no-store` for the playlist and the page: Safari happily replays a cached
playlist, which looks exactly like a frozen stream.

## It plays smoothly for a minute, then freezes until refresh

Four causes, in the order they were ruled out:

1. **The server was fine.** A 150 s loop pulling the playlist and every segment continuously showed
   140 segments with zero gaps and no encoder restart. Measure the server before blaming the network:
   if segments keep arriving, the client is at fault.
2. **Window too short.** 1 s segments with a 10-segment list still 404'd for a slow client. 2 s segments
   keep the same 16 s window with half the requests.
3. **Segment numbering reset to 0** after an encoder restart, so the player saw a discontinuity. The
   server now seeds `-start_number` from the highest `liveN.ts` on disk.
4. **Tailscale hairpin.** Two devices on the same wifi talking through the tailnet were routed
   `relay "fra"` (800–1130 ms, internet round trip). Use the LAN address when both ends are on the same
   network; the tailnet address is only for when one end is away.

The player page also self-heals: it watches `currentTime`, and if the clock stops advancing for 5 s
while playback is not paused it re-attaches with backoff, plus beacons `error/stalled/waiting/frozen`
to `/log?e=…` (`/tmp/vrstreamd-client.log` on the server). That is how the freezes above were counted
without a screen recording.

## Stream dies a couple of minutes after a player connects over RTSP

The idle watchdog must count RTSP/WebRTC viewers, otherwise "no HTTP requests" looks like "nobody is
watching". It asks MediaMTX for `/v3/paths/list` and counts `len(path["readers"])` — `readers` is a list
of session objects, not a number. Treating it as an int raises, is swallowed by the `except`, returns 0
and kills the encoder mid-view.

The other half of this bug is on the MediaMTX side: `runOnDemandCloseAfter` drops the publisher when the
last reader leaves, and a tee slave that lost its publisher is closed **permanently** (`onfail=ignore`
keeps HLS alive but never reconnects). Keep `runOnDemandCloseAfter: 0s` and let vrstreamd decide when to
stop; the watchdog additionally notices a `ready: false` path and re-runs the encoder.

## The picture freezes after SteamVR restarts, `fps: 0`, encoder still running

The compositor calls `shm_unlink()` and recreates the ring on every start, so the daemon's mapping
points at a dead inode while `/dev/shm/vrstream_frames` looks perfectly fine. The daemon stats the path
every 2 s and reopens the ring when the inode changes; if you fork this, never rely on the path alone.

## `systemctl stop` hangs for 90 s and then SIGKILLs

`BaseServer.shutdown()` waits for `serve_forever()` to return — calling it from a signal handler, which
runs *on* the `serve_forever()` thread, deadlocks. The handler now flips a flag, stops ffmpeg and calls
`shutdown()` from a helper thread. Anything that stops the encoder from another thread must also avoid
closing `proc.stdin`: `BufferedWriter.close()` blocks on the buffer lock while the feeder is inside
`write()`, which wedges the watchdog itself. Only the feeder owns the pipe; everybody else signals.

## `rtsp://…/vr` connects but there is no video

Check in this order:

```bash
systemctl --user status mediamtx.service
curl -s http://127.0.0.1:9997/v3/paths/list | python3 -m json.tool | grep -E '"name"|"ready"'
```

`ready: false` means nothing is publishing. The tee output needs `rtsp_transport=tcp` (default UDP
silently drops on busy wifi) and the HLS child needs its options **inside** the bracket:
`[f=hls:hls_segment_filename=/tmp/x/live%d.ts]/tmp/x/live.m3u8`. Move that option to the command line
and ffmpeg exits immediately — and because the branch carries `onfail=ignore`, HLS may keep working
while RTSP stays empty, which makes it look like a MediaMTX problem.

## Turning on `--token`

`--token` guards the HTTP side only (`/`, `/live.m3u8`, `.ts`, `/status`); `/healthz`, `/log` and `/wake`
stay open so the page beacon and MediaMTX's `runOnDemand` keep working. RTSP (`:8554`), WebRTC (`:8889`)
and the UDP port are MediaMTX's own listeners and remain unauthenticated — on a shared network either
bind MediaMTX to loopback and front it with a proxy, or configure `authInternalUsers` in
`config/mediamtx.yml`. Treat the token as "keeps honest people out", not as a security boundary.

## The layer never loads

* the manifest must live in a loader-searched directory
  (`~/.local/share/vulkan/implicit_layer.d/` is the per-user one) and its `library_path` must point to
  the `.so` — a relative path resolves against the manifest directory;
* the process must have `VRSTREAM_CAP=1` in its environment (implicit layers are gated by
  `enable_environment`) — set it in the `steamvr.service` drop-in, then restart SteamVR;
* the architecture must match the compositor's: an aarch64 `.so` in a 64-bit x86 process is ignored.

Confirm the file is being written at all:

```bash
ls -l /dev/shm/vrstream_frames    # size = 64 + 3*(16 + w*h*4)
```

If it is missing, the layer never ran in the process that presents — the compositor was usually started
before the manifest existed.

## Frames are black but the fps counter moves

The swapchain format is not BGRA. The layer blits whatever the compositor presents into a linear BGRA
image; if the driver hands back a different format the bytes are misinterpreted. Also try adding
`DISABLE_GAMESCOPE_WSI=1` to the compositor's drop-in when gamescope wraps presentation.

## Encoder pegs the CPU

The fish-eye unwarp (`v360`) is the expensive part: ~1.3 of 4 cores at 1280×720@60 on the Frame. Drop to
`--layout mono-round` (no unwarp), or lower `--bitrate`, or capture at `VRSTREAM_CAP_W=1024`. The stereo
layout is the cheapest and the ugliest on a TV.

## Startup hangs ~35 s on a Mac (dev only)

`HTTPServer.server_bind()` calls `socket.getfqdn()`, which blocks when the machine's own hostname does
not resolve. `Server.server_bind` overrides it; if you see this in your own fork, check `/etc/hosts`.
