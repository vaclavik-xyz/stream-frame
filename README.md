# vrstream

**Watch what your headset sees — on any screen, without a capture card.**

A Vulkan layer mirrors the frames the VR compositor is about to present into a shared-memory ring; a
dependency-free Python server turns that into an HLS stream your phone, TV or browser can open, and (in
parallel, from the same encode) an RTSP/WebRTC stream with sub-second latency for a player like mpv.

I wrote it to get a Steam Frame's picture onto a TV through a Steam Deck over HDMI. It works with any
SteamVR-style compositor that presents through Vulkan, because it hooks `vkQueuePresentKHR` instead of
reading the display.

```
headset ──▶ vrcompositor ──▶ [libvrcap.so] ──▶ /dev/shm ring ──▶ ffmpeg ──┬─▶ HLS   (~6 s, AirPlay, any browser)
                                                                          └─▶ RTSP ─▶ MediaMTX ─▶ WebRTC (<1 s, mpv/VLC/Deck)
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
- **Optional sound.** `--audio auto` muxes the system audio (PipeWire/Pulse monitor of the sink the
  headset plays into) into both paths as AAC — HLS and RTSP/mpv/VLC get audio; the browser WebRTC path
  stays video-only, because browsers do not play AAC over WHEP.
- **Token auth** for the HTTP side when the port is not private (see [docs/troubleshooting.md](docs/troubleshooting.md#turning-on-token); MediaMTX's own ports are not covered).
- **Tests without hardware.** 26 unit tests for the ring parser, command builder, segment numbering,
  HTTP routes, Range requests and shutdown behaviour — `pytest` runs with no GPU, no ffmpeg and no shm.
- Audio: not yet. See [Limitations](#limitations).

## Two sources: full quality, or no installation at all

The Vulkan layer is the high-quality path (1440 px per eye, stereo or one eye unwarped). SteamVR on Linux
also publishes its own headset view into a `v4l2loopback` node — `steamvr-v4l2cam.service` writing
`/dev/video99` (1920×1080, flat, ~70 fps) — so the same daemon can run **with no layer, no compositor
restart and nothing to uninstall**:

```bash
python3 server/vrstreamd.py --source v4l2:/dev/video99      # layout defaults to passthrough
```

Cost of that convenience: 960×1080 per eye instead of 1440×1440, and you get exactly the composed image
SteamVR already mirrors (which is what you want on a TV anyway). Test it first with
`ffmpeg -f v4l2 -i /dev/video99 -frames:v 1 /tmp/vr.jpg`.

## Quick start

```bash
git clone https://github.com/<you>/vrstream && cd vrstream
./scripts/install.sh          # --dry-run first if you like
systemctl --user restart steamvr.service     # layer is picked up at compositor start
systemctl --user start vrstream.service mediamtx.service
./scripts/smoke-test.sh <host-or-ip> 8101
```

Put the headset on, then:

- phone / TV / desktop browser: `http://<host>:8101/`
- lowest latency (mpv, VLC): `rtsp://<host>:8554/vr` — `mpv --no-cache rtsp://<host>:8554/vr`
- browser WebRTC (WHEP): `http://<host>:8889/vr`

Details, manual install, environment variables: [docs/install.md](docs/install.md).

## Endpoints

| route | what |
|---|---|
| `/` | player page: WebRTC (WHEP) first, HLS on iOS or `?hls`, stats with `t`, fullscreen with `f` |
| `/live.m3u8`, `/liveN.ts` | HLS playlist and segments (segments honour single `Range` requests) |
| `/status` | JSON: capture fps, encoder state, restarts, bitrate, layout, RTSP/WebRTC viewers |
| `/healthz` | `{"ok":true}` while the daemon lives |
| `/wake` | starts the encoder; unauthenticated so MediaMTX `runOnDemand` can use it |
| `/log?e=…` | player telemetry, appended to `/tmp/vrstreamd-client.log` |

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
--audio auto                                   mux system audio as AAC (default: off)
--source ring|v4l2:/dev/video99                capture from the layer or from a video node
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
  per-application stream, and it is silent on the browser WebRTC path (AAC is not a WebRTC codec).
- **One streamer per machine.** The ring name is fixed; a lock file stops a second daemon (two would
  fight over the playlist and the encoder).
- **No input, no control.** Read-only by design — nothing in the stream can drive the headset.
- **HLS is the stable path, WebRTC is the fast one.** They fail independently, on purpose.

## License

MIT. Built and tested on real hardware in evenings; expect the rough edges of a one-person project
rather than a product.
