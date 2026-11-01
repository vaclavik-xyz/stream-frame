#!/bin/sh
# Does the pipeline ever emit a corrupt frame? -c copy recordings are the exact
# encoder output, so a full decode is the honest test: any slice/IDR error here
# came from capture or encode, and no player setting can cause or fix it.
for f in $(ls -t "$HOME"/recordings/rec-*.mp4 | head -10); do
    n=$(basename "$f")
    errs=$(ffmpeg -v error -i "$f" -f null - 2>&1 | grep -c . )
    printf '%s decode_error_lines=%s\n' "$n" "$errs"
done
