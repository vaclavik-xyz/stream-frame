# Stream Frame

**Watch what your headset sees — on any screen, without a capture card.**

[![ci](https://github.com/vaclavik-xyz/stream-frame/actions/workflows/ci.yml/badge.svg)](https://github.com/vaclavik-xyz/stream-frame/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A Vulkan layer mirrors the frames the VR compositor is about to present into a shared-memory ring; a
dependency-free Python server turns that into an HLS stream your phone, TV or browser can open, and (in
parallel, from the same encode) an RTSP/WebRTC stream with sub-second latency for a player like mpv.

The project is **Stream Frame** and the daemon is `server/streamframe.py`. Paths, service names and the
old `vrstreamd.py` entry point keep working — the alias is there so nothing built around the first name
has to move at once.

I wrote it to get a Steam Frame's picture onto a TV through a Steam Deck over HDMI. It works with any
SteamVR-style compositor that presents through Vulkan, because it hooks `vkQueuePresentKHR` instead of
reading the display.

```
headset ──▶ vrcompositor ──▶ [libvrcap.so] ──▶ /dev/shm ring ──▶ ffmpeg ──┬─▶ HLS   (~6 s, AirPlay, any browser)
                                                                          ├─▶ RTSP ─▶ MediaMTX ─▶ WebRTC (<1 s, mpv/VLC/Deck)
                                                                          └─▶ MP4   (optional recording, no extra encode)
```

## Why two paths instead of one

| | HLS | RTSP → WebRTC |
|---|---|---|
| latency | ~6 s | ~0.3–0.8 s |
| survives wifi hiccups | yes, buffers through them | no, it drops |
| AirPlay / iPhone / Smart TV | native | needs a player |
| Steam Deck | browser tab | mpv/VLC fullscreen |

You cannot get both out of one protocol, so the encoder produces both. That is the whole design idea:
`-f tee` on a single x264 encode, so the second path costs no extra CPU (about 1.2 cores at 720p60).

## Features

- **No app, no capture card, no driver patch.** One implicit Vulkan layer + one Python file — or, if you
  do not want to install anything, `--source v4l2:/dev/video99` reads SteamVR's own headset view instead.
- **On demand.** Nothing is encoded while nobody watches; the first HTTP request starts ffmpeg,
  120 s of silence stops it. Connecting over RTSP wakes it through `runOnDemand`.
- **Supervised encoder.** ffmpeg dying is a two-second freeze, not a dead stream: automatic restart,
  segment numbering preserved so players do not see a discontinuity.
- **Flat picture for a TV.** The left eye's fish-eye image is unwarped to 16:9 (`mono-flat`), because a
  side-by-side stereo frame on a TV is useless. Stereo and square layouts are one flag away.
- **Self-healing on both ends.** The page watches its own clock, falls back from HLS to WebRTC when HLS
  cannot start, and beacons what happened to the server log; the daemon re-opens the capture ring when
  the compositor restarts and re-runs the encoder when MediaMTX loses the RTSP branch.
- **Optional sound, on every path.** `--audio auto` muxes the system audio (PipeWire/Pulse monitor of
  the sink the headset plays into) into both paths: AAC for HLS, and a second Opus encode of the same
  tap for RTSP, because WebRTC forwards Opus and nothing else. The page starts muted (browsers refuse
  to autoplay sound) and unmutes on the first tap; the speaker button or `a` toggles it.
- **Where the wearer looks.** The runtime's eye-tracking estimate is read from its shared-memory record
  and published as JSON and server-sent events; the player draws a reticle on the picture. Read-only, no
  driver, no images of the eye — only a direction and two lid values. See [docs/gaze.md](docs/gaze.md).
- **Gaze as data, not just a dot.** `--gaze-csv auto` appends every estimate to a CSV with wall-clock
  timestamps, so a recording of the video can be lined up with where the wearer was looking.
- **Recording, from the same encode.** `--record auto` writes the stream to MP4 chunks on disk, served
  back over HTTP with Range support — one more `tee` branch, no second encode, oldest files pruned past a
  size cap. See [docs/recording.md](docs/recording.md).
- **It admits when the picture is old.** `/status` reports how many seconds ago the last frame actually
  reached the encoder (`picture.age`, `frozen`), because a stuck capture still serves a live-looking HLS
  playlist from the last frames. In `v4l2:` mode the same number comes from the newest closed segment and
  is labelled as such.
- **A player that can change the pipeline, carefully.** `--settings auto` lets the page change bitrate,
  fps, layout, threads, nice, adapt, recording and gaze. Every value is validated against a range, the
  encoder is restarted and watched — until its frame counter climbs, usually under a second — and a
  setting that does not survive is rolled back before it can be remembered. The file is written only after a value has actually run. See
  [docs/settings.md](docs/settings.md).
- **Its own Wi-Fi, if you want none of the network you are on.** SteamOS already runs a hotspot beside
  its station link; the player reads it and hands a phone two codes — one to join, one to open the
  stream — so the picture goes headset → phone with no router in between. See
  [docs/hotspot.md](docs/hotspot.md).
- **Grab the last thirty seconds after the fact.** The segments are already on disk; the scissors button
  (or `k`) concats them with `-c copy` into an MP4 you can save — no re-encode, no second encoder. See
  [docs/clip.md](docs/clip.md).
- **It looks like a device.** A quiet chip row (transport, rate, how far behind the picture is, a `rec`
  timer), a vignette and a wordmark — fading out when you are watching, back when you move.
- **Leaves the CPU to the game.** ffmpeg runs at `nice 10` (`--nice`), can be capped with `--threads`, and
  if the compositor is producing full-rate frames while the encoder cannot keep up, the daemon lowers the
  bitrate once or twice instead of stuttering forever (`--adapt`, ring mode only).
- **Token auth** for the HTTP side when the port is not private (see [docs/troubleshooting.md](docs/troubleshooting.md#turning-on-token); MediaMTX's own ports are not covered).
- **Tests without hardware.** 129 unit tests for the ring parser, eye-record parser, command builder,
  segment numbering, HTTP routes, Range requests, event streaming, settings validation and rollback, and
  shutdown behaviour — plus an end-to-end test that fills the ring the way the layer does and checks that
  a playable segment comes out of ffmpeg. `pytest` needs no GPU and no headset.

## Two sources: full quality, or no installation at all

The Vulkan layer is the high-quality path (1440 px per eye, stereo or one eye unwarped). SteamVR on Linux
also publishes its own headset view into a `v4l2loopback` node — `steamvr-v4l2cam.service` writing
`/dev/video99` (1920×1080, flat, ~70 fps) — so the same daemon can run **with no layer, no compositor
restart and nothing to uninstall**:

```bash
python3 server/streamframe.py --source v4l2:/dev/video99      # layout defaults to passthrough
```

Cost of that convenience: 960×1080 per eye instead of 1440×1440, and you get exactly the composed image
SteamVR already mirrors (which is what you want on a TV anyway). Test it first with
`ffmpeg -f v4l2 -i /dev/video99 -frames:v 1 /tmp/vr.jpg`.

## Quick start

```bash
git clone https://github.com/vaclavik-xyz/stream-frame && cd stream-frame
./scripts/install.sh          # --dry-run first if you like
systemctl --user restart steamvr.service     # layer is picked up at compositor start
systemctl --user start streamframe.service mediamtx.service
./scripts/smoke-test.sh <host-or-ip> 8101
```

Put the headset on, then:

- phone / TV / desktop browser: `http://<host>:8101/`
- lowest latency (mpv, VLC): `rtsp://<host>:8554/vr` — `mpv --no-cache rtsp://<host>:8554/vr`
- browser WebRTC (WHEP): `http://<host>:8889/vr`

Details, manual install, environment variables: [docs/install.md](docs/install.md).

## Working on the page

`python3 scripts/devserver.py --upstream http://<headset>:8101` serves `server/web/index.html` from the
checkout on port 8099 while proxying `/status`, the HLS segments and the WHEP handshake from a real daemon
— so a CSS change is a reload, and the overlays sit on a real picture. `POST /api/settings` is answered
locally: poking the panel in dev cannot restart the stream someone is watching.

## Endpoints

| route | what |
|---|---|
| `/` | player page: WebRTC (WHEP) first, HLS on iOS or `?hls`, stats with `t`, fullscreen with `f` |
| `/live.m3u8`, `/liveN.ts` | HLS playlist and segments (segments honour single `Range` requests) |
| `/status` | JSON: capture fps, encoder state, restarts, bitrate, layout, viewers, gaze rate and age |
| `/healthz` | `{"ok":true}` while the daemon lives |
| `/wake` | starts the encoder; unauthenticated so MediaMTX `runOnDemand` can use it |
| `/gaze` | JSON: latest gaze sample (degrees, lid values, placement in the frame) and reader health |
| `/gaze.sse` | the same as server-sent events, one event per new estimate — `curl -N …/gaze.sse` |
| `/gaze.csv` | download the gaze recording so far (needs `--gaze-csv`) |
| `/recordings` | JSON list of the MP4 chunks with sizes and URLs (needs `--record`) |
| `/api/settings` | `GET` current values + ranges, `POST` a change (token) |
| `/api/clip` | `POST {"seconds": 30}` cuts the last N seconds out of the stream into an MP4 (token) |
| `/clips`, `/clips/<name>` | the clips on the shelf, newest first, and one of them with `Range` support |
| `/api/hotspot` | the headset's own hotspot: SSID, passphrase, live address, URL, band, clients (token) |
| `/qr.js` | the QR encoder the page draws the joining codes with (public, vendored) |
| `/recordings/<name>` | download or stream one chunk, `Range` supported; the newest one is `"open": true` until the encoder closes it |
| `/log?e=…` | player telemetry, appended to `/tmp/streamframe-client.log` |

```bash
curl -s http://<host>:8101/status | python3 -m json.tool
```

## Options

```
--layout mono-flat|mono-round|stereo|mono-flat-right   what to send to the TV (default mono-flat)
--vf "<ffmpeg filter>"                                bypass the presets entirely
--bitrate 4M          --fps 60          --preset ultrafast
--hls-time 2          --hls-window 8    --idle 120
--rtsp-url rtsp://127.0.0.1:8554/vr    ('' = HLS only)
--audio auto                                   mux system audio (AAC + Opus; default: off)
--source ring|v4l2:/dev/video99                capture from the layer or from a video node
--gaze auto|off|<path>                         read the eye server (default auto); see docs/gaze.md
--gaze-shm /dev/shm/eye-server.mmap            where that record lives
--gaze-fov 100x90                              degrees the flat picture covers, for the reticle
--gaze-csv auto|<dir>|<file>                   append gaze samples to a CSV (default: off)
--record auto|<dir>        --record-time 300   --record-limit 20G
                                               write MP4 chunks (default: off); docs/recording.md
--threads 0        --nice 10        --adapt / --no-adapt
--settings auto|<path>|''  --no-settings      where player changes are remembered
--token <secret>      --bind 0.0.0.0    --port 8101     -v
```

## Requirements and scope

- A compositor that presents through Vulkan (`vkQueuePresentKHR`), started by systemd if you want the
  packaged units. Tested on Steam Frame / SteamOS ARM64; the layer builds against stock Vulkan headers,
  so x86_64 SteamVR should work the same way — untested, reports welcome.
- `ffmpeg` with libx264, Python 3.9+, and for the low-latency path a `mediamtx` binary.
- The headset must be worn / VR must be rendering: an idle compositor presents nothing to mirror.

Measured latency, ring layout, lifecycle and the reasoning behind the HLS parameters are in
[docs/architecture.md](docs/architecture.md). Failures that actually happened, with causes:
[docs/troubleshooting.md](docs/troubleshooting.md). Who else shows, records and streams a headset view, and
what this project deliberately is not: [docs/prior-art.md](docs/prior-art.md).

## Limitations

- **Audio is a separate tap.** The layer hooks the present queue, which carries no audio; `--audio`
  records the PipeWire/Pulse sink monitor instead, so it hears whatever the machine plays, not a
  per-application stream.
- **One streamer per machine.** The ring name is fixed; a lock file stops a second daemon (two would
  fight over the playlist and the encoder).
- **No input, no control.** Read-only by design — nothing in the stream can drive the headset. Gaze is
  read-only in the same way: it is published for display, not fed back into the runtime.
- **Gaze is approximate and new.** The reticle places `yaw`/`pitch` inside the picture using a FOV you
  declare (`--gaze-fov`); it is not a calibrated projection, and the parser has only been exercised
  against a synthetic record so far — see [docs/gaze.md](docs/gaze.md#honest-status).
- **The hotspot is SteamOS's, not ours.** We read `/var/run/softap_hostapd.conf` and the live interface;
  we never start or reconfigure the AP. On this hardware it transmits on 6 GHz only, so Wi-Fi 5 devices
  (older phones, a Steam Deck LCD) cannot see it at all — see [docs/hotspot.md](docs/hotspot.md).
- **HLS is the stable path, WebRTC is the fast one.** They fail independently, on purpose.
- **Recording is chunked and blunt.** No transcoding, no trimming, no thumbnails: one `-c copy` MP4 per
  `--record-time` seconds, and a hard kill loses the chunk that was open. It also costs I/O on the same
  eMMC the game is loading from.

## Watching on the TV

On a Steam Deck, `scripts/deck-stream.sh` does the whole dance: it switches the session to the desktop,
waits for it, plays RTSP fullscreen and can put the Deck back into Game Mode afterwards.

```bash
deck-stream.sh start    # desktop session + fullscreen mpv
deck-stream.sh status   # session, player, readers
deck-stream.sh stop     # back to Game Mode
```

The Steam non-Steam shortcut (`scripts/make-shortcut.py`) is written and shows up in the library, but this
Deck's Steam refuses to start it from Game Mode with "game configuration is not available". Until that is
understood, the script is the route that works — and it survives an SSH disconnect, because the player
runs in its own systemd unit.

## Tested on

| Machine | OS | What was verified |
| --- | --- | --- |
| Steam Frame (ARM64, Adreno 750) | SteamOS 3.x | capture, encode, audio, hotspot, gaze, clips |
| Steam Deck LCD | SteamOS 3.x, desktop session | RTSP to HDMI over Wi-Fi |
| MacBook | macOS | player page, settings, clips, the whole test suite |

## The rest of the paperwork

- [CHANGELOG.md](CHANGELOG.md) — what changed and when
- [SECURITY.md](SECURITY.md) — what it listens on, and where it is safe to run
- [CONTRIBUTING.md](CONTRIBUTING.md) — running it locally, tests, style

## License

MIT. Built and tested on real hardware in evenings; expect the rough edges of a one-person project
rather than a product.
