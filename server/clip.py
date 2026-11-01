"""Grab the last N seconds of what the headset just rendered.

The HLS layer already cuts the stream into two-second segments, but ffmpeg deletes them as the playlist
window rolls on, so the newest dozen are all that exist at any moment — about twenty seconds. A person
watching something funny does not know it is funny until it has happened, so the keeper hardlinks every
segment aside and prunes by age. A clip is then a concat of those links with -c copy: no re-encode, no
second encoder, nothing that costs the game its CPU.

Hardlinks matter: ffmpeg unlinks its own segment file while our link keeps the inode alive, so the copy
costs nothing and cannot race the writer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

SEGMENT_PREFIX = "live"
CLIP_PREFIX = "clip-"
DEFAULT_SECONDS = 30
MIN_SECONDS = 2


def recent_dir(hls_dir: str) -> str:
    return os.path.join(hls_dir, "recent")


def clips_dir(hls_dir: str) -> str:
    return os.path.join(hls_dir, "clips")


def _links(directory: str) -> list[tuple[float, str]]:
    """Hardlinked segments, oldest first. The name carries the source mtime, so sorting is enough."""
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".ts"))
    except OSError:
        return []
    out = []
    for name in names:
        path = os.path.join(directory, name)
        try:
            out.append((os.path.getmtime(path), path))
        except OSError:
            continue
    out.sort(key=lambda item: item[0])
    return out


def sweep(hls_dir: str, window: float, segment: float) -> int:
    """Link new segments aside, forget the ones older than the window. Returns how many were linked."""
    if window <= 0:
        return 0
    source, keep = hls_dir, recent_dir(hls_dir)
    os.makedirs(keep, exist_ok=True)
    try:
        names = os.listdir(source)
    except OSError:
        return 0
    linked, newest = 0, 0.0
    for name in names:
        if not (name.startswith(SEGMENT_PREFIX) and name.endswith(".ts")):
            continue
        src = os.path.join(source, name)
        try:
            mtime = os.path.getmtime(src)
        except OSError:
            continue
        newest = max(newest, mtime)
        link = os.path.join(keep, f"{int(mtime * 1000):013d}-{name}")
        if os.path.exists(link):
            continue
        try:
            os.link(src, link)                    # same inode: ffmpeg unlinking its file costs us nothing
        except OSError:
            try:
                shutil.copy2(src, link)           # a filesystem without hardlinks still gets a clip
            except OSError:
                continue
        linked += 1

    # prune relative to the newest segment, not to the wall clock: a paused stream must not empty the
    # window the moment it resumes
    cutoff = (newest or time.time()) - window - segment * 2
    for mtime, path in _links(keep):
        if mtime < cutoff:
            try:
                os.unlink(path)
            except OSError:
                break
    return linked


class Keeper(threading.Thread):
    """Watches the HLS directory so a clip can always reach back into the last --clip-window seconds."""

    def __init__(self, hls_dir: str, window: float, segment: float):
        super().__init__(name="clip-keeper", daemon=True)
        self.hls_dir, self.window, self.segment = hls_dir, window, segment
        self._stop = threading.Event()
        self.linked = 0

    def run(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self.linked += sweep(self.hls_dir, self.window, self.segment)
            except Exception:                     # a full disk must not kill the thread
                pass

    def stop(self) -> None:
        self._stop.set()


def pick(entries: list[tuple[float, str]], seconds: float, segment: float) -> list[str]:
    """The newest segments that cover `seconds`, in play order."""
    if not entries:
        return []
    want = max(1, int(round(seconds / max(0.2, segment))))
    chosen = entries[-want:]
    return [path for _, path in chosen]


def make_clip(cfg, seconds: float) -> dict:
    """Concat what is on disk into one mp4. Raises ValueError when there is nothing to cut."""
    keep = recent_dir(cfg.hls_dir)
    entries = _links(keep)
    if not entries:
        raise ValueError("nothing to clip yet — the stream has not written a segment")
    seconds = min(max(MIN_SECONDS, float(seconds)), float(cfg.clip_window))
    paths = pick(entries, seconds, cfg.hls_time)
    if not paths:
        raise ValueError("no segments in the window")

    out_dir = clips_dir(cfg.hls_dir)
    os.makedirs(out_dir, exist_ok=True)
    name = f"{CLIP_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    target = os.path.join(out_dir, name)
    listing = os.path.join(out_dir, ".concat.txt")
    with open(listing, "w", encoding="utf-8") as fh:
        for path in paths:
            quoted = path.replace("'", "'\\''")      # the concat demuxer's own quoting rule
            fh.write(f"file '{quoted}'\n")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "concat", "-safe", "0", "-i", listing,
           "-c", "copy", "-avoid_negative_ts", "make_zero",
           "-movflags", "+faststart", target]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
    finally:
        try:
            os.unlink(listing)
        except OSError:
            pass
    if proc.returncode != 0 or not os.path.exists(target):
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ValueError("ffmpeg could not cut the clip" + (f": {detail[-1]}" if detail else ""))

    prune_clips(out_dir, keep_count=20)
    size = os.path.getsize(target)
    return {"name": name, "url": "/clips/" + name, "seconds": round(len(paths) * cfg.hls_time, 1),
            "segments": len(paths), "bytes": size}


def prune_clips(directory: str, keep_count: int) -> int:
    try:
        names = sorted((n for n in os.listdir(directory) if n.startswith(CLIP_PREFIX)), reverse=True)
    except OSError:
        return 0
    removed = 0
    for name in names[keep_count:]:
        try:
            os.unlink(os.path.join(directory, name))
            removed += 1
        except OSError:
            break
    return removed


def clip_files(directory: str) -> list[dict]:
    """Newest first, with the fields the panel needs."""
    try:
        names = [n for n in os.listdir(directory) if n.startswith(CLIP_PREFIX) and n.endswith(".mp4")]
    except OSError:
        return []
    out = []
    for name in names:
        try:
            st = os.stat(os.path.join(directory, name))
        except OSError:
            continue
        out.append({"name": name, "bytes": st.st_size, "mtime": st.st_mtime, "url": "/clips/" + name})
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out
