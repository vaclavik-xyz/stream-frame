# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versioning is
[semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- The capture layer read its on/off flag from `VRSTREAM_CAP_` — suffix concatenation on an empty
  suffix — so a unit exporting `VRSTREAM_CAP=1` was silently `capture=off`: no ring, no picture,
  no error. The bare name is now read as the bare name.

### Added
- The player page can be driven without a mouse: the d-pad walks a selection across the toolbar,
  A presses, B steps back, up/down is volume, X clips. The 8BitDo Pro 2 on the Deck is seen twice
  (itself and Steam's virtual pad), so presses are latched per action, and the poll runs on a
  message-channel pump because a throttled overlay tab loses quick taps between timer ticks.
- `deck-stream.sh start` now follows the session it is in: in Game Mode it opens the player page in
  Steam's own overlay browser (`steam://openurl/…`, HLS, no session switch), on the desktop it plays
  RTSP in mpv. `--browser` / `--mpv` force either route, and `status` reports which one is live by
  looking for the Deck's connection to port 8101.
- Brand mark, app icon, favicon and a 1200×630 social preview (`docs/img/brand/`). The mark is one
  continuous ribbon around the visor rim — vector, so it holds from 16 px to print.
- `/favicon.ico`, `/icon.svg` and `/apple-touch-icon.png` are served without a token, like `qr.js`:
  browsers and iOS request them unprompted, and a 401 there just leaves a blank tab icon.

## [0.1.0] - 2026-11-01

First release. The tool does what it was written to do: a headset's picture and
sound on another screen, over the network, with no capture card.

### Added

- Sound in the browser. WebRTC carries Opus and drops AAC, so the audio tap is now
  encoded twice from one input: AAC for the HLS and MP4 outputs, Opus for the RTSP
  branch MediaMTX serves over WHEP. Each tee slave selects the pair it can mux.
- The player starts muted (browsers refuse to autoplay audio) and unmutes on the
  first tap anywhere; a speaker button and the `a` key toggle it, remembered across
  loads.
- A Vulkan implicit layer that hooks `vkQueuePresentKHR` and copies each presented
  frame into a three-slot shared-memory ring, downscaled to a configurable width.
- A dependency-free Python daemon that reads the ring and encodes it with ffmpeg
  into HLS (~6 s, any browser), RTSP through MediaMTX (<1 s, mpv/VLC) and MP4,
  all from a single encode.
- An optional AAC tap of the PipeWire/Pulse default sink monitor, so the headset's
  audio travels with the picture.
- Five picture layouts: `stereo`, `mono-flat` (one eye unwrapped to 16:9),
  `mono-flat-right`, `mono-round`, and `passthrough` for an already flat source.
  The crop follows the capture width, so a wider ring is a pure env change.
- A `--source` mode that runs without the layer, reading a SteamVR headset view.
- Runtime settings from the player page — bitrate, fps, layout, encoder threads,
  nice level, adaptive bitrate, recording — validated against fixed ranges and
  rolled back if the encoder does not come back up.
- Recording to MP4 next to the live stream in five-minute chunks, pruned oldest
  first once the directory passes a size limit.
- Clipping: `POST /api/clip` turns the last 30 seconds of HLS into one MP4
  without another encode. The dev server can cut clips too.
- Gaze: reads the eye-tracking sidecar, publishes it over SSE next to the
  picture, and records it as CSV keyed by wall clock. A standalone probe can be
  run on the headset before the daemon.
- A hotspot page that shows the headset's own access point as a QR code, so a
  phone can join without typing a password.
- `scripts/deck-stream.sh` for the Steam Deck: `start` switches to the desktop
  session and plays RTSP fullscreen, `stop` returns to Game Mode, `status` says
  what is running and how many readers the daemon has.
- A dev server that serves the page from a checkout and proxies the daemon, so
  the UI can be edited without reinstalling anything.
- A synthetic-ring end-to-end test: a Python writer fills the ring the way the
  layer does and the test asserts a playable HLS segment comes out the other end.
- Documentation: setup, the layer, the Deck, troubleshooting, prior art, and the
  measurements behind the tiling decision.

### Changed

- The project is called Stream Frame; the daemon is `server/streamframe.py`,
  units and temp paths follow the new name, and the old `vrstreamd.py` entry
  point still works as an alias.
- WebRTC names the Wi-Fi interface (`webrtcIPsFromInterfacesList: [wlan0]`)
  instead of an address, so a new DHCP lease no longer breaks the stream.
- The capture gate advances a deadline instead of comparing against the previous
  grab, which ran the stream at 40 fps against an 80 Hz compositor.
- Capture images stay linear after a probe showed tiling flips on 118 of 4096
  frames on this GPU.
- A pipeline that is already producing frames is trusted immediately, so a
  settings change returns in well under a second.
- `--audio` also takes a file or `lavfi:<graph>`, so the audio path can be
  exercised without a sound server (the end-to-end test uses a sine tone).

### Fixed

- ffmpeg is stopped by closing its stdin, so the last MP4 chunk is playable
  instead of truncated.
- Six bugs found in a review pass (ring teardown, seek behaviour in the clip
  builder, and settings that survived a failed restart).
- Three leaks found in a second pass (clip shelf, gaze reader, restart path).
- A layer whose `library_path` points at a missing `.so` is silently skipped by
  the Vulkan loader; troubleshooting now explains how to spot it in
  `/proc/<pid>/maps`.

[0.1.0]: https://github.com/vaclavik-xyz/stream-frame/releases/tag/v0.1.0
