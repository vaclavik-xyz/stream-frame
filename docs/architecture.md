# Architecture

Three processes, one frame path. Nothing on the VR side is modified — the compositor keeps presenting
to the headset exactly as before, and a Vulkan layer copies what it was going to present anyway.

```
 ┌──────────────┐   present()   ┌───────────────────────────────┐
 │  VR app      │──────────────▶│ vrcompositor (SteamVR)        │
 └──────────────┘               │  └─ libvrcap.so  (Vulkan layer)│
                                └───────────────┬───────────────┘
                        GPU blit → host-visible │  BGRA, 1440×1440 (per eye)
                                                ▼
                                  /dev/shm/vrstream_frames  (3-slot ring)
                                                │  newest frame, no queue, no backpressure
                                                ▼
                                 ┌──────────────────────────────┐
                                 │ vrstreamd  (python, no deps)  │
                                 │  ffmpeg: bgra → x264 → tee   │
                                 └───────┬──────────────┬───────┘
                                         │ HLS          │ RTSP (tcp, 127.0.0.1)
                                         ▼              ▼
                              /tmp/vrstream-hls      mediamtx :8554
                                    │                    │
                              HTTP :8101           WebRTC :8889 ──▶ browser (WHEP)
                                    │                    │
                              phone / AirPlay      mpv / VLC / Deck  (<1 s)
```

## The ring buffer (the only contract between C and Python)

The layer allocates `shm_open("/vrstream_frames")` sized `64 + 3 * (16 + w*h*4)`.

Header (64 B, little endian):

| offset | type | meaning |
|-------:|------|---------|
| 0  | u32 | width |
| 4  | u32 | height |
| 8  | u32 | slot size in bytes (`16 + w*h*4`) |
| 12 | u32 | active slot (informational) |

Each of the 3 slots:

| offset | type | meaning |
|-------:|------|---------|
| 0  | u64 | sequence number, written **last** with release semantics |
| 8  | u64 | reserved / timestamp |
| 16 | w*h*4 | BGRA pixels, tightly packed, top-down |

The writer memcpy's the pixels, then bumps the slot's sequence. A reader picks the slot with the
highest sequence, reads its pixels and keeps the sequence as a cursor — so it never blocks the
compositor and always gets the freshest frame instead of a backlog. If the reader stalls, frames are
simply overwritten: latency is bounded, frames are lost by design.

The layer does the copy on the GPU (`vkCmdBlitImage` into a `LINEAR` image, then a buffer copy into
host-visible memory) and paces itself with a fence, so a slow reader costs dropped frames, not stalls.
Roughly 1.5 % of compositor frame time at 1440×1440.

## Encoder

`ffmpeg` is fed raw BGRA on stdin at a fixed 60 fps: the newest ring frame is re-sent if the compositor
produced nothing new, which keeps HLS timing regular without inventing motion (VR present rate is
typically 60–90 Hz; re-sending is invisible at that rate).

Two outputs come from a **single** encode through the `tee` muxer:

* `-f hls` — 2 s segments, 8-segment window, independent segments, no endlist: a viewer can join
  mid-stream and a 16 s hiccup does not kill the playlist.
* `-f rtsp` — pushed to MediaMTX on loopback, which re-serves it as RTSP and WebRTC.

`onfail=ignore` on the RTSP branch is deliberate: MediaMTX being down or restarting must never take HLS
with it. One encoder is ~1.2 cores at 720p60; a second one would double that.

## Lifecycle

* **On demand.** Any HTTP request (page, playlist, segment, `/status`) sets a "wanted" flag; the
  supervisor starts ffmpeg. The VR box does no encoding when nobody watches.
* **Supervised.** If ffmpeg exits early, the supervisor restarts it with backoff (200 ms → 3 s) and keeps
  the same segment numbering, so a crash is a short freeze in the viewer, not a dead stream. Segment
  numbers only ever grow (a cached `live0.ts` must never come back with different bytes).
* **Idle stop.** After `--idle` seconds without HTTP traffic *and* with no RTSP/WebRTC readers
  (`/v3/paths/list`), the encoder is closed and its playlist and segments are deleted — a playlist that
  points at deleted segments is worse than a 404, and the next request wakes the encoder anyway.
* **Wake from RTSP.** MediaMTX `runOnDemand` pings `/wake` (unauthenticated, unlike `/status`) when a
  player connects to `rtsp://…/vr`, so a Deck with no browser open still gets a stream in a couple of
  seconds, token or no token.
* **Recovery hooks.** Two failures are invisible without an explicit check, so the watchdog checks them
  every 5 s: a compositor restart replaces the ring inode (reopen it), and a MediaMTX restart kills the
  tee's RTSP branch for good (`ready: false` → re-run the encoder, give up to HLS-only after 3 tries).
* **One daemon per machine.** A lock file prevents two instances from fighting over the same playlist;
  SIGTERM stops ffmpeg first and only then the HTTP server, off the `serve_forever()` thread.

## Latency budget

Measured on a Steam Frame with a clock burned into the source frame (cold start on both paths):

| stage | HLS | RTSP/WebRTC |
|---|---|---|
| capture → ring | ~16 ms | ~16 ms |
| x264 ultrafast, 720p60 | ~40 ms | ~40 ms |
| segmenting | 2 s | — |
| player startup buffer | 3 segments (~6 s) | ~0.2 s |
| network (LAN) | ~40 ms | ~40 ms |
| **total** | **~6 s** | **~0.3–0.8 s** |

The HLS delay is the price of a buffer that survives wifi hiccups and works in Safari/AirPlay without an
app. That is why both paths exist rather than replacing one with the other.
