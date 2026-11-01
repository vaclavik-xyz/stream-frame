# Recording

The same encode that feeds the live stream can also write MP4 files. It is one more branch of the
existing `tee`, so recording costs no extra CPU — only disk.

```bash
python3 server/vrstreamd.py --record auto                 # ~/vrstream-recordings
python3 server/vrstreamd.py --record /mnt/nvme/vr         # somewhere with room
python3 server/vrstreamd.py --record /mnt/nvme/vr --record-time 600 --record-limit 50G
```

Files are named `rec-<session-start>-<chunk>.mp4`, e.g. `rec-20261101-204512-003.mp4`. The prefix is the
moment that encoder session started, so chunks sort chronologically and a restart never overwrites an
older run.

## Chunks, not one long file

A chunk is closed every `--record-time` seconds (default 300) and is playable right away. One continuous
MP4 would keep its index at the end of the file, so a crash or a power cut would lose the whole session,
not the last five minutes. Fragmented MP4 would in principle survive that, but the `movflags` do not
survive the `segment` muxer — the files come out undecodable — so this is the honest trade: **at most one
chunk is lost on a hard kill**, and a normal shutdown (`systemctl --user stop vrstream`) closes the open
chunk properly.

## Watching it back

```bash
curl -s http://<host>:8101/recordings | python3 -m json.tool     # list with sizes and URLs
curl -O -J http://<host>:8101/recordings/rec-20261101-204512-000.mp4
```

`/recordings/<name>` serves the file with `Range` support, so a phone can stream it without downloading
everything first. Names are checked: only `rec-*.mp4` inside the recording folder is reachable.

## Disk budget

`--record-limit` (default `20G`) is a ceiling, not a reservation: every 30 s the daemon deletes the oldest
chunks once the folder is over it, and it never deletes the chunk that is still being written. 720p60 at
4 Mbit/s is roughly **1.8 GB per hour**, so 20 GB is about eleven hours of footage.

Put the folder on real storage, not `/tmp` — on the headset `/tmp` is tmpfs, i.e. RAM.

## Lining video up with gaze

If you also run `--gaze-csv auto`, the CSV carries wall-clock timestamps. A chunk's start time is in its
name, so `ffmpeg -ss <offset>` into the chunk plus `t_wall` in the CSV is enough to answer "where was he
looking at second 12". Both files live on the headset's clock; nothing else has to be synchronised.

## When recording does not start

- `/status` → `record.enabled` is false: `--record` was not passed, or it was `off`.
- `record.enabled` is true and `record.files` stays at 0: ffmpeg is not running (`encoder: false`) — open
  the page or `curl /wake` first, since nothing is encoded while nobody watches.
- The `segment` branch is `onfail=ignore` inside the tee: if the folder is not writable, the live stream
  keeps working and only the recording is missing. The reason is in `/tmp/vrstreamd-ffmpeg.log`.
