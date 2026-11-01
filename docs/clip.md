# Clipping the last thirty seconds

Something funny happens, and it happened twelve seconds ago. Recording is the wrong tool for that — you
cannot start it retroactively — so the daemon keeps the recent stream on disk and cuts a clip out of it
afterwards.

## How it works

ffmpeg already writes the stream as two-second HLS segments (`live0.ts`, `live1.ts`, …) and deletes them as
the playlist window rolls on, so only about twenty seconds exist for the player. A small keeper thread
hardlinks every segment aside into `<hls_dir>/recent/` before ffmpeg can unlink it, and prunes by age
against `--clip-window` (default 90 s). Hardlinks keep the inode alive without copying a byte, and they
cannot race the writer.

`POST /api/clip` then concatenates the newest segments that cover the asked-for length:

```
ffmpeg -f concat -safe 0 -i list.txt -c copy -movflags +faststart clip-YYYYmmdd-HHMMSS.mp4
```

`-c copy` means no second encode and no CPU stolen from the game. The result lands in `<hls_dir>/clips/`,
is kept for twenty clips, and is served at `/clips/<name>` with `Range` support, so a phone can scrub it in
place or save it.

## Using it

In the player: the scissors button, or `k`. The toast reports the real length and size and offers a **save**
link — a popup opened after an `await` is blocked by browsers, a link is not.

From a shell:

```bash
curl -X POST 'http://<host>:8101/api/clip?token=…' -d '{"seconds": 45}'
{"ok": true, "name": "clip-20260101-120000.mp4", "url": "/clips/clip-20260101-120000.mp4",
 "seconds": 46.0, "segments": 23, "bytes": 15400000}
```

You get what exists: ask for 30 s twenty seconds after the encoder started and the answer says `20.0`, not
an error. `GET /clips` lists what is on the shelf; `--clip-window 0` turns the whole thing off (the button
disappears with it).

## Limits

- The window lives in `/tmp` next to the segments: a reboot forgets it. Clips you saved are yours; the
  shelf is not.
- The clip starts on a segment boundary, so it can be up to one `--hls_time` longer than asked.
- The last segment may be the one ffmpeg is still writing. It is not corrupted (the keeper links it after
  it appears), but it can be a fraction short of two seconds.
- No audio in the clip unless the stream has audio — see `--audio` in [settings](settings.md).
