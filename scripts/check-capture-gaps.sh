#!/bin/sh
# Look for capture stalls: a gap between consecutive video PTS means the encoder
# received nothing for that long. -c copy keeps the original timestamps, so the
# recordings are an honest log of the pipeline itself.
for f in $(ls -t "$HOME"/recordings/rec-*.mp4 | head -12); do
    ffprobe -v error -select_streams v -show_entries packet=pts_time -of csv=p=0 "$f" 2>/dev/null |
    awk -F, -v n="$(basename "$f")" '
        NR > 1 { d = $1 - p; if (d > 0.05) g++; if (d > 0.20) big++; if (d > worst) worst = d }
        { p = $1 }
        END { printf "%s frames=%d gap50=%d gap200=%d worst=%.2fs\n", n, NR, g+0, big+0, worst+0 }'
done
