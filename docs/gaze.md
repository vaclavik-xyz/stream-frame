# Gaze: where the wearer is looking, next to the picture

The Frame tracks the eyes for foveated rendering. The runtime keeps each new estimate in a small
shared-memory record, and that record is readable from a normal process. vrstream reads it, puts it on
a clock shared with the video, and lets the player draw a reticle on top of the stream.

Nothing is written to the headset, no driver is patched, and no image of the eye leaves the device —
only a direction and two lid values, the same numbers the compositor already computes.

## Turn it on

`--gaze auto` is the default and does the obvious thing:

```bash
python3 server/vrstreamd.py --gaze auto        # use /dev/shm/eye-server.mmap when it exists
python3 server/vrstreamd.py --gaze off         # never touch the eye server
python3 server/vrstreamd.py --gaze /run/user/1000/other.mmap
```

In the player press **g** (or the `gaze` button) for the reticle; the choice is remembered in
`localStorage`. Hover the `stats` button and the HUD gains a line with the sample rate and the age of the
last sample.

| endpoint | what it returns |
| --- | --- |
| `/gaze` | one JSON sample: `yaw`, `pitch` (degrees), `open_l`, `open_r`, `x`, `y` (−1…1 inside the image), plus the reader's own health |
| `/gaze.sse` | the same payload as server-sent events, 30 Hz, one event per new estimate |

Server-sent events rather than WebSocket on purpose: the browser reconnects by itself, there is no
handshake to get wrong, and it needs no dependency beyond the standard library. `curl -N /gaze.sse`
is a working client.

## What the numbers mean

- `yaw`, `pitch` — the fused gaze direction, head-relative, degrees off centre. Negative pitch is down.
- `yaw_l`/`yaw_r`, `pitch_l`/`pitch_r` — the same per eye, before fusion. Their difference is vergence:
  the eyes converge on near things, stay parallel on far ones.
- `open_l`, `open_r` — raw lid values. A blink shows as a dip in both; a wink in exactly one.
- `x`, `y` — the angles mapped into the frame, using `--gaze-fov 100x90` by default. This is a placement
  aid, **not** a calibrated projection: the streamed picture is one eye's view of a stereo pair, the FOV
  of the flat image is not exactly the FOV of the lens, and none of that is measured yet. Treat the
  reticle as "roughly there", and set `--gaze-fov` if it sits too far in or out.
- `age` — seconds since the last new estimate. Eye tracking pauses when the headset is off the head, and
  a growing age is how you see that.

## Recording it next to the video

```bash
python3 server/vrstreamd.py --gaze-csv auto        # <hls-dir>/gaze.csv
python3 server/vrstreamd.py --gaze-csv ~/run1/     # a directory: gaze.csv inside it
python3 server/vrstreamd.py --gaze-csv ~/run1/eyes.csv
```

One row per new estimate, append-only:

```
t_wall,t_mono,yaw,pitch,yaw_l,pitch_l,yaw_r,pitch_r,open_l,open_r
# session 2026-11-01T01:16:51+0100
2026-11-01T01:16:51.541+0100,2003592.8030,16.7,-11.31,0.0,0.0,0.0,0.0,0.68,0.74
```

`t_wall` is the join key: HLS segments and any MP4 carry their own mtime, so a recording can be lined up
with the eyes afterwards without touching the video. `t_mono` is for gaps inside a session, where the
wall clock can jump (NTP). A `# session …` line marks every daemon start, so rows from different runs stay
separable; `grep -v '^#'` or `pandas.read_csv(..., comment='#')` handles them.

- Size: roughly 20–25 MB per hour at 60–120 Hz. It is off by default for that reason.
- Durability: flushed every 60 samples (~half a second) and on shutdown, so a crash costs about a second.
- Download the recording so far with `curl -O -J http://<host>:8101/gaze.csv` — it is a snapshot of the
  file, not a live tail.
- The recorder is attached to the reader in the daemon, not to the encoder: gaze rows keep coming while
  the video is stalled, and vice versa the stream survives a recorder that cannot write (the reason lands
  in `/status` under `gaze.csv.error`).

## How the read works

The file is a header and one record:

```
0x00  version u32          4 = 0x4f21a bytes, 5 = 0x4f21f
0x04  initialized u32      must be 1
0x08  pthread mutex        0x30 bytes, used by the writer
0x38  sequence u32         bumped for every record, doubles as a futex word
0x3c  metadata_requested   set to 1 to ask for a fresh estimate
```

The record lives at `0x152` (version 4) or `0x157` (version 5) and starts with `producer_state u32`
(1 = valid), then `sample_flag`, `sample_time f64`, the gaze pairs, the fixation point in metres with
−Z forward, and the lid values. `server/gaze.py` declares the layout as one packed struct and the tests
build the fixture at those absolute offsets, so a wrong field order fails in CI instead of quietly
reporting nonsense on a headset.

Reads use the sequence counter as a seqlock: the record is copied, the counter is re-read, and a sample
that moved underneath is dropped (`torn_reads` in `/status`). The one write is `metadata_requested = 1`,
which is the documented way for a consumer to say "publish the next estimate" — without it a reader sees
stale data.

The file is re-opened when its inode changes, so a SteamVR restart does not leave the reader staring at
an unlinked mapping.

## What is not available

Raw images from the eye cameras. Valve exposes the estimate, not the sensor frames, and HTC's guidance on
the same silicon is explicit that raw imagery is not part of the developer-facing API. If the goal is a
recording of what the eye looked like, this is not the door.

## Checking it on a headset

Start with the probe — it needs nothing but Python and the running runtime, so it tells you whether the
layout guess is right before the daemon is in the picture:

```bash
python3 server/gaze.py                   # prints one line per new estimate, 30 s
python3 server/gaze.py /dev/shm/eye-server.mmap 60
```

Then the daemon:

```bash
ls -l /dev/shm/eye-server.mmap          # exists while the runtime is up
systemctl --user status vrstream         # look for "gaze: reading … (abi 4)"
curl -s localhost:8101/gaze | jq         # yaw/pitch should change when you look around
```

If the file is there but `gaze.py` refuses it, the daemon logs the reason (`unsupported version`,
`not initialized yet`, `needs N bytes`) and keeps streaming video without a reticle — gaze never takes
the stream down with it.

## Honest status

The parser is written against the layout above and covered by 11 unit tests, and the whole path
(daemon → `/gaze` → SSE → reticle) was exercised locally against a synthetic record. It has **not** run
against a live eye server yet: the headset was offline while this was built. The first real check is the
`curl … /gaze` above; if the numbers move with your gaze, the ABI guess was right, and if the daemon logs
a version mismatch, the offsets in `ABI` are the one place to fix.
