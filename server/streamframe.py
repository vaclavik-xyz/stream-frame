#!/usr/bin/env python3
"""streamframe - live view of a VR compositor over HTTP.

Pipeline
    vrcompositor --(vrcap Vulkan layer)--> /dev/shm ring --> ffmpeg --+--> HLS (portable, ~6 s)
                                                                     +--> RTSP -> MediaMTX -> WebRTC (<1 s)

Routes
    /              player page (WebRTC first, HLS for iOS/AirPlay)
    /live.m3u8     HLS playlist          /liveN.ts  HLS segments (with Range support)
    /status        JSON: capture fps, encoder state, restarts, viewers
    /healthz       200 while the daemon is alive
    /wake          starts the encoder (used by MediaMTX runOnDemand, unauthenticated)
    /log?e=<text>  client telemetry from the player page

The encoder starts on the first request and stops after --idle seconds without one, so the device
costs nothing while nobody watches. It is supervised: if ffmpeg dies it is restarted without
dropping the viewer to a dead stream, and a compositor restart (which recreates the shared-memory
ring behind a new inode) is detected and recovered from.
"""
from __future__ import annotations

import argparse
import fcntl
import hmac
import http.server
import json
import mmap
import os
import re
import shutil
import signal
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import clip as clips
import hotspot
import settings as prefs
from gaze import EyeSource, GazeRecorder

HERE = os.path.dirname(os.path.abspath(__file__))

# ring buffer written by the Vulkan layer (see docs/architecture.md)
HDR = 64
SLOTS = 3
SLOT_HDR = 16

RTSP_PATH = "vr"

# layout presets: what the compositor presents -> what a TV wants to see
BEACON_MAX_BYTES = 1 << 20        # rotate the client telemetry log at 1 MiB
BEACON_LOG = "/tmp/streamframe-client.log"
# Each eye is half of the captured frame, so the crop follows the capture width
# instead of assuming a 1440-wide (2x720) buffer. Capturing wider is then a pure
# env change on the layer side: sharpness is decided by how many pixels the warp
# gets to read, not by the output size.
EYE_FOV = "input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=75"
OUT_W, OUT_H = 1280, 720

LAYOUTS = {
    # two lens-warped eyes side by side, untouched
    "stereo": None,
    # left eye, fisheye unwrapped to a flat 16:9 picture
    "mono-flat": (
        "crop={eye}:{eye}:0:0,"
        f"v360={EYE_FOV}:w={OUT_W}:h={OUT_H},"
        "format=yuv420p"
    ),
    # left eye as presented, square
    "mono-round": "crop={eye}:{eye}:0:0",
    # already a flat, undistorted picture (SteamVR headset view, /dev/video99)
    "passthrough": f"scale=-2:{OUT_H}:flags=bicubic,format=yuv420p",
    # right eye, fisheye unwrapped
    "mono-flat-right": (
        "crop={eye}:{eye}:{eye}:0,"
        f"v360={EYE_FOV}:w={OUT_W}:h={OUT_H},"
        "format=yuv420p"
    ),
}


def mem_suffix(value: str) -> int:
    """Parse '4M' / '2500K' / '1G' into bytes.

    A bare number below 10k is rejected: it is almost always a missing suffix, and ffmpeg would
    only fail later with a cryptic error.
    """
    v = str(value).strip().upper()
    if not v:
        raise ValueError("empty size")
    suffix = v[-1]
    if suffix in "KMG":
        num, mult = v[:-1], {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[suffix]
    else:
        num, mult = v, 1
    try:
        n = float(num)
    except ValueError as exc:
        raise ValueError(f"bad size: {value!r}") from exc
    if n <= 0:
        raise ValueError(f"bad size: {value!r}")
    if mult == 1 and n < 10_000:
        raise ValueError(f"ambiguous size: {value!r} — write it as 2500K / 4M")
    return int(n * mult)


def fmt_size(n: int) -> str:
    """Bytes back into the '4M' / '2500K' form ffmpeg takes."""
    for suffix, unit in (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if n >= unit:
            value = n / unit
            # one decimal: rounding 2.5M down to 2M would silently cost a fifth of the bitrate
            return f"{value:.1f}".rstrip("0").rstrip(".") + suffix
    return str(max(1, n))


def record_files(directory: str) -> list[dict]:
    """Closed mp4 chunks, oldest first. The open chunk is included: it is the newest file."""
    try:
        names = sorted(n for n in os.listdir(directory) if n.startswith("rec-") and n.endswith(".mp4"))
    except OSError:
        return []
    out = []
    for name in names:
        try:
            st = os.stat(os.path.join(directory, name))
        except OSError:
            continue
        out.append({"name": name, "bytes": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda f: f["mtime"])
    return out


def prune_recordings(directory: str, limit: int) -> int:
    """Delete the oldest chunks once the folder exceeds the limit; never the newest one."""
    files = record_files(directory)
    total = sum(f["bytes"] for f in files)
    removed = 0
    while files and total > limit and len(files) > 1:
        oldest = files.pop(0)
        try:
            os.unlink(os.path.join(directory, oldest["name"]))
        except OSError:
            break
        total -= oldest["bytes"]
        removed += 1
    return removed


def _env(name: str, default=None):
    """STREAMFRAME_* is the name now; VRSTREAM_* is still read so units written for the old name boot."""
    for prefix in ("STREAMFRAME_", "VRSTREAM_"):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


@dataclass
class Config:
    port: int = 8101
    bind: str = "0.0.0.0"
    fps: int = 60
    bitrate: str = "4M"
    preset: str = "ultrafast"
    layout: str = "mono-flat"
    vf: str | None = None              # raw ffmpeg filter override
    source: str = "ring"               # "ring" (Vulkan layer) or "v4l2:/dev/video99"
    gaze: str = "auto"                 # off | auto (when the eye server exists) | path
    gaze_shm: str = "/dev/shm/eye-server.mmap"
    gaze_fov: str = "100x90"           # approximate, only used to place the reticle
    gaze_csv: str = ""                 # "" = no recording, "auto" = <hls_dir>/gaze.csv, or a path
    threads: int = 0                   # x264 threads, 0 = whatever x264 picks
    nice: int = 10                     # run ffmpeg at this nice level so a game keeps the CPU it needs
    adapt: bool = True                 # lower the bitrate once if the encoder cannot keep up
    record: str = ""                   # "" = off, "auto" = ~/streamframe-recordings, or a directory
    record_time: int = 300             # seconds per file; the mp4 is only playable once closed
    record_limit: str = "20G"          # oldest files are pruned past this
    shm: str = "/dev/shm/streamframe_frames"
    hls_dir: str = "/tmp/streamframe-hls"
    hls_time: int = 2
    hls_window: int = 8
    clip_window: float = 90.0          # seconds of history kept aside so a clip can reach back
    idle: float = 120.0
    audio: str = ""                    # "" = off, "auto" = default sink monitor, or a pulse source
    rtsp_url: str | None = "rtsp://127.0.0.1:8554/vr"
    mediamtx_api: str = "http://127.0.0.1:9997"
    webrtc_url: str = ""               # shown on the page, e.g. http://host:8889/vr
    token: str = ""
    settings: str = "auto"             # where verified settings are stored; "" turns persistence off
    verbose: bool = False
    page: str = field(default="")

    def v4l2_device(self) -> str | None:
        """Device path when the source is a kernel/gstreamer video node, else None."""
        src = (self.source or "ring").strip()
        if src.startswith("v4l2:"):
            return src.split(":", 1)[1] or None
        if src.startswith("/dev/video"):
            return src
        return None

    def gaze_source(self) -> str | None:
        """Path of the eye-server record to read, or None when gaze is disabled/absent."""
        mode = (self.gaze or "off").strip()
        if mode in ("", "off", "0", "no"):
            return None
        if mode in ("auto", "on", "yes", "1"):
            return self.gaze_shm if os.path.exists(self.gaze_shm) else None
        return mode

    def record_dir(self) -> str | None:
        """Directory for the mp4 recording, or None when recording is off."""
        value = (self.record or "").strip()
        if not value or value in ("0", "off", "no"):
            return None
        if value == "auto":
            return os.path.expanduser("~/streamframe-recordings")
        return os.path.expanduser(value)

    def record_limit_bytes(self) -> int:
        try:
            return mem_suffix(self.record_limit)
        except ValueError:
            return 20 * 1024 ** 3

    def gaze_csv_path(self) -> str | None:
        """Where gaze samples are appended, or None when recording is off."""
        mode = (self.gaze_csv or "").strip()
        if mode in ("", "off", "0", "no"):
            return None
        if mode in ("auto", "on", "yes", "1"):
            return os.path.join(self.hls_dir, "gaze.csv")
        if os.path.isdir(mode):
            return os.path.join(mode, "gaze.csv")
        return mode

    def gaze_fov_pair(self) -> tuple[float, float]:
        try:
            h, _, v = (self.gaze_fov or "").partition("x")
            return max(1.0, float(h)), max(1.0, float(v or h))
        except ValueError:
            return 100.0, 90.0

    def filter_chain(self, width: int = 0) -> str | None:
        if self.vf:
            return self.vf
        if self.layout not in LAYOUTS:
            raise ValueError(f"unknown layout {self.layout!r} "
                             f"(pick one of {', '.join(sorted(LAYOUTS))})")
        chain = LAYOUTS[self.layout]
        if chain is None:
            return None
        # even widths keep the two eyes the same size for ffmpeg's crop
        eye = max(1, (width or 1440) // 2) & ~1
        return chain.format(eye=eye)


# ----------------------------------------------------------------------------- ring buffer


class FrameRing:
    """Reader of the shared-memory ring written by the Vulkan layer.

    The layer shm_unlink()s and recreates the ring whenever the compositor starts, so a mapping is
    only valid for one compositor generation: `stale()` reports when the path points at a new inode.
    """

    def __init__(self, path: str):
        self.path = path
        self._fd = -1
        self._mm: mmap.mmap | None = None
        self._lock = threading.Lock()
        self.torn = 0                       # frames refused because the writer overwrote them mid-copy
        self.last_at = 0.0                  # when the last good frame was handed to the encoder
        self.last_seq = 0
        self.width = self.height = 0
        self.slot_size = 0
        self.inode = 0

    def open(self) -> bool:
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return False
        try:
            mm = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
        except (OSError, ValueError):
            os.close(fd)                     # never leak the fd when the ring is half-written
            return False
        with self._lock:
            self._release()
            self._fd, self._mm = fd, mm
            self.inode = os.fstat(fd).st_ino
            if not self._read_header():
                self._release()
                return False
            return True

    def _release(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            except (BufferError, ValueError):
                pass
            self._mm = None
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def close(self) -> None:
        with self._lock:
            self._release()

    def stale(self) -> bool:
        """True when the compositor recreated the ring behind a new inode."""
        try:
            return os.stat(self.path).st_ino != self.inode
        except OSError:
            return True

    def age(self) -> float | None:
        """Seconds since the last frame was handed to the encoder, or None if none ever was.

        This is the honest answer to "is the picture live": the compositor keeps the HLS playlist
        rolling from the last frames it saw, so a stuck capture still looks like a live stream.
        """
        return time.monotonic() - self.last_at if self.last_at else None

    def _read_header(self) -> bool:
        if self._mm is None:
            return False
        try:
            w, h, slot_size, _active = struct.unpack_from("<IIII", self._mm, 0)
        except struct.error:
            return False
        if not (w and h and slot_size) or slot_size < SLOT_HDR + w * h * 4:
            return False
        self.width, self.height, self.slot_size = w, h, slot_size
        return True

    def _seq(self, slot: int) -> int:
        assert self._mm is not None
        return struct.unpack_from("<Q", self._mm, HDR + slot * self.slot_size)[0]

    def latest(self, last_seq: int, frame_bytes: int) -> tuple[int, bytes | None]:
        """Newest frame newer than last_seq, or (last_seq, None).

        A truncated read is refused: a short buffer written into ffmpeg would desync the raw stream
        for the rest of the session.
        """
        with self._lock:
            if self._mm is None or not self.slot_size:
                return last_seq, None
            best_seq, best_slot = last_seq, -1
            for i in range(SLOTS):
                seq = self._seq(i)
                if seq > best_seq:
                    best_seq, best_slot = seq, i
            if best_slot < 0:
                return last_seq, None
            off = HDR + best_slot * self.slot_size + SLOT_HDR
            data = self._mm[off:off + frame_bytes]
            if len(data) != frame_bytes:
                return last_seq, None
            # the writer bumps seq *after* copying, so a sample is only trustworthy if the counter
            # did not move while we were reading: otherwise this is half of two different frames, and
            # feeding that to x264 corrupts every P-frame until the next IDR
            if self._seq(best_slot) != best_seq:
                self.torn += 1
                return last_seq, None
            self.last_seq, self.last_at = best_seq, time.monotonic()
            return best_seq, data

    def fps(self, sample: float = 0.35) -> float:
        with self._lock:
            if self._mm is None or not self.slot_size:
                return 0.0
            a = max(self._seq(i) for i in range(SLOTS))
        time.sleep(sample)
        with self._lock:
            if self._mm is None or not self.slot_size:
                return 0.0
            b = max(self._seq(i) for i in range(SLOTS))
        return max(0.0, (b - a) / sample)


# ----------------------------------------------------------------------------- encoder


class Encoder:
    """Owns the ffmpeg child: starts on demand, feeds frames, restarts on crash.

    Thread ownership: the supervisor thread starts a session and, inside it, runs the feeder — the
    feeder is the only code allowed to touch proc.stdin. stop()/shutdown() flip flags and signal the
    process; closing the pipe from another thread can block forever on the buffered stream's lock
    and would wedge the very watchdog that is supposed to recover us.
    """

    def __init__(self, cfg: Config, ring: FrameRing):
        self.cfg = cfg
        self.ring = ring
        self._want = threading.Event()
        self._shutdown = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self.frames = 0            # frames pushed since daemon start
        self.restarts = 0          # consecutive fast failures
        self.sessions = 0          # encoder sessions started
        self.last_exit: int | None = None
        self.last_error = ""
        self.started_at = 0.0
        self._seq_no: int | None = None
        self._readers_at = 0.0
        self._readers_n = 0

    # -- lifecycle ----------------------------------------------------------
    def ensure(self) -> None:
        """Ask the encoder to be running (cheap, called on every request)."""
        with self._lock:                          # thread creation must not race itself
            self._want.set()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._supervise, name="encoder", daemon=True)
                self._thread.start()

    def wanted(self) -> bool:
        return self._want.is_set()

    def stop(self, wait: float = 0.0) -> None:
        """Idle stop: the feeder notices the cleared flag and closes its own pipe."""
        with self._lock:
            self._want.clear()
            proc = self._proc
        if proc is None:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.05)
        if proc.poll() is None:
            proc.terminate()                  # feeder sees the broken pipe and exits

    def shutdown(self) -> None:
        """Stop ffmpeg *cleanly*: an MP4 chunk only becomes playable when its index is written.

        Closing stdin is the polite stop (the feeder does the same when the idle timer expires): ffmpeg
        sees EOF, flushes every muxer, writes the moov atom and exits 0. A SIGTERM straight away used to
        leave the last chunk unreadable, and killing it after three seconds took the index with it.
        """
        with self._lock:
            self._shutdown.set()
            self._want.set()
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=8)
                return
            except subprocess.TimeoutExpired:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()

    def alive(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def restart(self) -> None:
        """Re-run the pipeline (e.g. after MediaMTX came back and the RTSP branch is dead)."""
        self.stop(wait=4.0)
        self.ensure()

    # -- command line -------------------------------------------------------
    def build_cmd(self, width: int, height: int, start_no: int,
                  audio: str | None = None) -> list[str]:
        cfg = self.cfg
        device = cfg.v4l2_device()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
               "-progress", self._progress_path()]
        if device:
            # SteamVR's own v4l2loopback node: already flat, already composed, no layer involved
            cmd += ["-f", "v4l2", "-framerate", str(cfg.fps), "-i", device]
        else:
            cmd += ["-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{width}x{height}",
                    "-r", str(cfg.fps), "-i", "-"]
        if audio:
            # system audio (PipeWire/Pulse monitor of the sink the headset plays into)
            cmd += ["-f", "pulse", "-i", audio]
        vf = cfg.filter_chain(width)
        if vf:
            cmd += ["-vf", vf]
        if cfg.threads:
            cmd += ["-threads", str(cfg.threads)]
        cmd += ["-c:v", "libx264", "-preset", cfg.preset, "-tune", "zerolatency",
                "-maxrate", cfg.bitrate, "-bufsize", str(mem_suffix(cfg.bitrate) * 2),
                "-g", str(max(1, cfg.fps // 2)), "-b:v", cfg.bitrate, "-pix_fmt", "yuv420p"]
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        playlist = os.path.join(cfg.hls_dir, "live.m3u8")
        hls = ["-f", "hls",
               "-hls_time", str(cfg.hls_time),
               "-hls_list_size", str(cfg.hls_window),
               "-hls_delete_threshold", "4",
               "-start_number", str(start_no),
               "-hls_flags", "delete_segments+omit_endlist+independent_segments",
               playlist]
        maps = ["-map", "0:v"] + (["-map", "1:a"] if audio else [])
        rec = cfg.record_dir()
        if not cfg.rtsp_url and not rec:
            return cmd + maps + hls
        # one encode, up to three outputs; onfail=ignore keeps HLS alive when the others break
        opts = (f"hls_time={cfg.hls_time}:hls_list_size={cfg.hls_window}:hls_delete_threshold=4:"
                f"start_number={start_no}:"
                f"hls_flags=delete_segments+omit_endlist+independent_segments:"
                f"hls_segment_filename={cfg.hls_dir}/live%d.ts")
        slaves = [f"[f=hls:{opts}]{playlist}"]
        if cfg.rtsp_url:
            slaves.append(f"[f=rtsp:onfail=ignore:rtsp_transport=tcp]{cfg.rtsp_url}")
        if rec:
            # mp4 chunks, not one long file: each chunk is playable as soon as it is closed, and a
            # power cut costs the open chunk instead of the whole session. fragmented mp4 flags do
            # not survive the segment muxer (the files come out undecodable), so plain mp4 it is.
            os.makedirs(rec, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            ropts = (f"segment_time={max(10, int(cfg.record_time))}:segment_format=mp4:"
                     f"reset_timestamps=1")
            slaves.append(f"[f=segment:{ropts}:onfail=ignore]{rec}/rec-{stamp}-%03d.mp4")
        return cmd + ["-f", "tee"] + maps + ["|".join(slaves)]

    def _progress_path(self) -> str:
        return f"/tmp/streamframe-progress-{self.cfg.port}.log"

    def encoder_fps(self) -> float:
        """fps reported by ffmpeg itself (-progress), good for sources we cannot sample."""
        try:
            with open(self._progress_path(), encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-40:]
        except OSError:
            return 0.0
        fps = 0.0
        for line in lines:
            if line.startswith("fps="):
                try:
                    fps = float(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
        return fps

    def _segment_base(self) -> int:
        """Segment numbers must keep growing across restarts, or a browser replays a cached
        live0.ts that now holds different bytes."""
        if self._seq_no is None:
            highest = 0
            try:
                for name in os.listdir(self.cfg.hls_dir):
                    m = re.match(r"live(\d+)\.ts$", name)
                    if m:
                        highest = max(highest, int(m.group(1)) + 1)
            except OSError:
                pass
            self._seq_no = highest
        return self._seq_no

    # -- supervisor ---------------------------------------------------------
    def _supervise(self) -> None:
        while not self._shutdown.is_set():
            self._want.wait()
            if self._shutdown.is_set() or not self._want.is_set():
                continue
            began = time.time()
            try:
                rc = self._run_session()
            except Exception as exc:                      # never let the supervisor die
                self.last_error = f"{type(exc).__name__}: {exc}"
                log(f"encoder session failed: {self.last_error}")
                rc = -1
            self.last_exit = rc
            self._seq_no = None                          # re-seed from disk next session
            if self._shutdown.is_set() or not self._want.is_set():
                continue                                 # intentional stop
            if time.time() - began > 10:
                self.restarts = 0                        # it ran fine; this is a fresh crash
            self.restarts += 1
            time.sleep(min(0.2 * self.restarts, 3.0))

    def _run_session(self) -> int:
        cfg = self.cfg
        os.makedirs(cfg.hls_dir, exist_ok=True)
        device = cfg.v4l2_device()
        if device:
            return self._run_device(device)
        if not self.ring.open():
            self.last_error = f"ring not available: {cfg.shm}"
            time.sleep(2)
            return -1
        width, height = self.ring.width, self.ring.height
        frame_bytes = width * height * 4
        start_no = self._segment_base()
        self._seq_no = start_no + cfg.hls_window + 2     # never reuse a number, even if files vanish
        audio = resolve_audio(cfg)
        cmd = self.build_cmd(width, height, start_no, audio)
        if cfg.verbose:
            log("encoder:", " ".join(cmd))
        fferr = self._ffmpeg_log()
        try:
            proc = subprocess.Popen(self._nice(cmd, cfg.nice), stdin=subprocess.PIPE, stderr=fferr)
        except BaseException:                 # ffmpeg missing: do not leak the log file on every retry
            self._close_log(fferr)
            raise
        with self._lock:
            self._proc = proc
        self.sessions += 1
        self.started_at = time.time()
        log(f"encoder started (layout={cfg.layout}, {width}x{height}, {cfg.bitrate})")
        try:
            return self._feed(proc, frame_bytes)
        finally:
            with self._lock:
                self._proc = None
            self._close_log(fferr)

    def _run_device(self, device: str) -> int:
        """ffmpeg reads the video node itself: no ring, no feeder, no raw frames in Python."""
        cfg = self.cfg
        if not os.path.exists(device):
            self.last_error = f"source device missing: {device}"
            time.sleep(2)
            return -1
        start_no = self._segment_base()
        self._seq_no = start_no + cfg.hls_window + 2
        audio = resolve_audio(cfg)
        cmd = self.build_cmd(0, 0, start_no, audio)
        if cfg.verbose:
            log("encoder:", " ".join(cmd))
        fferr = self._ffmpeg_log()
        proc = subprocess.Popen(self._nice(cmd, cfg.nice), stdin=subprocess.DEVNULL, stderr=fferr)
        with self._lock:
            self._proc = proc
        self.sessions += 1
        self.started_at = time.time()
        log(f"encoder started (source={device}, layout={cfg.layout}, {cfg.bitrate})")
        try:
            while proc.poll() is None:
                if self._shutdown.is_set() or not self._want.is_set():
                    proc.terminate()
                    break
                time.sleep(0.2)
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -1
            log(f"encoder stopped (rc={rc})")
            return rc if rc is not None else -1
        finally:
            with self._lock:
                self._proc = None
            self._close_log(fferr)

    @staticmethod
    def _nice(cmd: list[str], level: int) -> list[str]:
        """ffmpeg must lose every CPU race against the game it is recording."""
        if level and shutil.which("nice"):
            return ["nice", "-n", str(level)] + cmd
        return cmd

    @staticmethod
    def _close_log(stream) -> None:
        # _ffmpeg_log hands back either a file or subprocess.DEVNULL, which is the int -3
        if not isinstance(stream, int):
            stream.close()

    def _ffmpeg_log(self):
        path = "/tmp/streamframe-ffmpeg.log"
        try:
            if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                os.unlink(path)                       # keep a restart loop from filling /tmp
            return open(path, "ab", buffering=0)
        except OSError:
            return subprocess.DEVNULL

    def _feed(self, proc: subprocess.Popen, frame_bytes: int) -> int:
        cfg = self.cfg
        interval = 1.0 / cfg.fps
        t_next = time.time()
        last_seq = 0
        sent = 0
        t_report = time.time()
        t_stat = time.time()
        stdin = proc.stdin
        frame = bytes(frame_bytes)
        rc = -1
        try:
            while not self._shutdown.is_set():
                if proc.poll() is not None:
                    rc = proc.returncode
                    return rc if rc is not None else -1
                if not self._want.is_set():
                    break                             # idle stop requested
                if time.time() - t_stat > 2:
                    t_stat = time.time()
                    if self.ring.stale():             # compositor restarted -> new ring inode
                        log("ring recreated by the compositor, reopening")
                        if self.ring.open():
                            frame_bytes = self.ring.width * self.ring.height * 4
                            frame = bytes(frame_bytes)
                            last_seq = 0
                last_seq, newer = self.ring.latest(last_seq, frame_bytes)
                if newer is not None:
                    frame = newer
                if stdin is None:
                    return -1
                try:
                    stdin.write(frame)
                except (BrokenPipeError, ValueError):
                    rc = proc.poll()
                    return rc if rc is not None else -1
                except OSError as exc:
                    self.last_error = f"write: {exc}"
                    return -1
                sent += 1
                self.frames += 1
                now = time.time()
                t_next += interval
                if t_next > now:
                    time.sleep(t_next - now)
                else:
                    t_next = now
                if now - t_report >= 10 and cfg.verbose:
                    log(f"pushed {sent / (now - t_report):.1f} fps")
                    sent, t_report = 0, now
        finally:
            try:
                if stdin is not None and not stdin.closed:
                    stdin.close()
            except OSError:
                pass
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -1
            log(f"encoder stopped (rc={rc})")
        return rc

    # -- reporting ----------------------------------------------------------
    def readers(self) -> int:
        """Viewers connected through MediaMTX (RTSP/WebRTC), cached for 2 s: every player polls
        /status every 3 s and must not block on a wedged MediaMTX."""
        if not self.cfg.rtsp_url:
            return 0
        now = time.time()
        with self._lock:
            if now - self._readers_at < 2.0:
                return self._readers_n
        n = 0
        try:
            with urllib.request.urlopen(f"{self.cfg.mediamtx_api}/v3/paths/list", timeout=1.5) as r:
                data = json.loads(r.read().decode())
            n = sum(len(item.get("readers") or []) for item in data.get("items") or [])
        except Exception:
            n = 0
        with self._lock:
            self._readers_at, self._readers_n = now, n
        return n

    def status(self) -> dict:
        return {
            "source": self.cfg.v4l2_device() or "ring",
            "enc_fps": round(self.encoder_fps(), 1) if self.alive() else 0.0,
            "encoder": self.alive(),
            "restarts": self.restarts,
            "sessions": self.sessions,
            "last_exit": self.last_exit,
            "last_error": self.last_error,
            "frames": self.frames,
            "layout": self.cfg.layout,
            "bitrate": self.cfg.bitrate,
            "readers": self.readers(),
        }


# ----------------------------------------------------------------------------- http


LAST_REQUEST = 0.0


class Runtime:
    """Everything a settings change may touch: the encoder, the eye reader and the saved file.

    A change only counts as done once the new pipeline has been seen running; if it does not survive,
    the previous values go back and the caller gets an error instead of a stream that quietly died.
    """

    def __init__(self, cfg: Config, enc: Encoder, ring: FrameRing):
        self.cfg, self.enc, self.ring = cfg, enc, ring
        self.lock = threading.Lock()       # two changes at once would fight over the same encoder
        self.notes: list[str] = []

    # -- gaze ---------------------------------------------------------------
    def start_gaze(self) -> bool:
        """True when samples will flow; the caller turns a False into an honest note, not a rollback."""
        self.stop_gaze()
        path = self.cfg.gaze_source()
        if not path:
            if self.cfg.gaze not in ("off", "", "0", "no"):
                log(f"gaze: {self.cfg.gaze_shm} not present, reticle stays off")
            return False
        src = EyeSource(path)
        rec = None
        csv_path = self.cfg.gaze_csv_path()
        if csv_path:
            rec = GazeRecorder(csv_path)
            if rec.start():
                src.on_sample = rec.write
                log(f"gaze: recording to {csv_path}")
            else:
                log(f"gaze: cannot record — {rec.error}")
                rec = None
        if src.start():
            log(f"gaze: reading {path} (abi {src.version})")
        else:
            log(f"gaze: unavailable — {src.error}")
            src = None
            if rec is not None:
                rec.stop()
                rec = None
        # the request handlers read these two straight off the class; one writer keeps them honest
        Handler.gaze, Handler.gaze_rec = src, rec
        return src is not None

    def stop_gaze(self) -> None:
        for obj in (Handler.gaze, Handler.gaze_rec):
            if obj is not None:
                obj.stop()
        Handler.gaze, Handler.gaze_rec = None, None

    def shutdown(self) -> None:
        self.stop_gaze()

    # -- settings -----------------------------------------------------------
    def describe(self) -> dict:
        return {"settings": prefs.current(self.cfg), "limits": prefs.LIMITS,
                "layouts": sorted(LAYOUTS), "persisted": prefs.settings_path(self.cfg),
                "restart_fields": sorted(prefs.ENCODER_FIELDS)}

    def change(self, payload: dict) -> dict:
        """Validate, apply, prove it runs, persist — in that order, with rollback in the middle."""
        values = prefs.validate(payload)              # ValueError -> 400, nothing touched
        with self.lock:
            self.notes = []
            before = prefs.snapshot(self.cfg, values)
            prefs.apply(self.cfg, values)
            restarted = False
            try:
                # validate the folder on every change, not only when recording is switched on:
                # a path that became unwritable should not be discovered at the next chunk boundary
                if self.cfg.record_dir():
                    prefs.check_record_dir(self.cfg)
                if set(values) & prefs.ENCODER_FIELDS:
                    restarted = True
                    ok, why = self._restart_verified()
                    if not ok:
                        raise RuntimeError(why)
                if "gaze" in values:
                    if values["gaze"]:
                        if not self.start_gaze():
                            # the preference is real, the source is not there yet: say so instead of
                            # claiming a reticle that nobody will see
                            self.notes.append("gaze saved, but the runtime is not publishing eye data "
                                              "right now — the reticle stays off until it does")
                    else:
                        self.stop_gaze()
            except (ValueError, RuntimeError):
                prefs.restore(self.cfg, before)
                if restarted:
                    self._restart_verified()          # put the pipeline that worked back
                if "gaze" in values:
                    self.start_gaze() if before.get("gaze") else self.stop_gaze()
                raise
            path = prefs.settings_path(self.cfg)
            if path:
                try:
                    # only what just ran: a value that failed the check above never reaches the disk
                    prefs.save(path, prefs.current(self.cfg))
                except OSError as exc:
                    self.notes.append(f"applied, but not saved: {exc}")
            return {"settings": prefs.current(self.cfg), "restarted": restarted,
                    "notes": self.notes}

    def _restart_verified(self) -> tuple[bool, str]:
        """Watch the new pipeline: one that dies at once is a bad setting, not a slow start.

        Waiting out a fixed deadline made every save feel broken — eight seconds of nothing while the
        encoder had been up for half a second. Frames are the proof that actually matters, so a pipeline
        whose frame count rises twice is done being suspicious and returns at once; the deadline is only
        the sad path, for a pipeline that stays alive without producing anything.
        """
        self.enc.stop(wait=4.0)
        self.enc.ensure()
        deadline = time.time() + 8.0
        rising, last = 0, self.enc.frames
        while time.time() < deadline:
            time.sleep(0.2)
            if not self.enc.alive():
                return False, self.enc.last_error or "the encoder stopped right after the change"
            if self.enc.frames > last:
                rising, last = rising + 1, self.enc.frames
                if rising >= 2:
                    return True, ""
            else:
                rising = 0
        return (True, "") if self.enc.alive() else (False, "the encoder did not start")


def log(*parts: object) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30                     # a half-open client must not pin a thread forever
    cfg: Config
    enc: Encoder
    ring: FrameRing
    runtime: Runtime
    gaze: EyeSource | None = None
    gaze_rec: GazeRecorder | None = None

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=self.cfg.hls_dir, **kw)

    # -- helpers ------------------------------------------------------------
    def end_headers(self):
        path = urlparse(self.path).path
        if path.endswith(".m3u8") or path.endswith(".ts") or path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def guess_type(self, path):
        name = str(path)
        if name.endswith(".m3u8"):
            return "application/vnd.apple.mpegurl"
        if name.endswith(".ts"):
            return "video/mp2t"
        return super().guess_type(name)

    def _given_token(self, query: dict) -> str:
        given = (query.get("token") or [""])[0]
        if not given:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                given = auth[7:].strip()
        if not given:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "streamframe_token":
                    given = value
        return given

    def _authorized(self, query: dict) -> bool:
        token = self.cfg.token
        if not token:
            return True
        given = self._given_token(query)
        # compare_digest on str demands pure ASCII and raises TypeError otherwise — and a TypeError
        # inside a request handler is a dropped connection, not a 401. Compare bytes instead.
        return hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))

    def _send(self, data: bytes) -> int:
        """Write the whole buffer: with an unbuffered wfile a single write() may accept only part."""
        view = memoryview(data)
        total = 0
        while view:
            sent = self.wfile.write(view)
            if not sent:
                raise BrokenPipeError("client went away")
            view = view[sent:]
            total += sent
        return total

    def _json(self, payload: dict, code: int = 200, head: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self._send(body)

    def _asset(self, path: str, ctype: str, head: bool = False, max_age: int = 3600) -> None:
        """A file from server/web/ that the page loads by itself (the QR encoder)."""
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self._json({"error": "not found"}, 404, head=head)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", f"public, max-age={max_age}")
        self.end_headers()
        if not head:
            self._send(body)

    def _beacon(self, query: dict) -> None:
        text = (query.get("e") or [""])[0][:200]
        text = text.replace("\r", " ").replace("\n", " ")      # no log-line forging
        path = BEACON_LOG
        try:
            # /tmp is tmpfs on SteamOS: an unauthenticated, unbounded append is a way to fill RAM
            if os.path.exists(path) and os.path.getsize(path) > BEACON_MAX_BYTES:
                os.replace(path, path + ".1")
            with open(path, "a") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {self.client_address[0]} {text}\n")
        except OSError:
            pass
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _picture_state(self) -> dict:
        if self.cfg.v4l2_device():
            # a device is only observed through ffmpeg; the newest closed segment is our clock, so
            # the age is an upper bound (up to one hls_time stale) — say so instead of pretending
            newest = 0.0
            try:
                for name in os.listdir(self.cfg.hls_dir):
                    if name.endswith(".ts"):
                        newest = max(newest, os.path.getmtime(os.path.join(self.cfg.hls_dir, name)))
            except OSError:
                pass
            age = max(0.0, time.time() - newest) if newest else None
            return {"source": "segment", "age": None if age is None else round(age, 1),
                    "frozen": bool(age is not None and age > 3 * max(1, self.cfg.hls_time)),
                    "seq": None, "torn": 0}
        age = self.ring.age()
        return {"source": "ring", "age": None if age is None else round(age, 1),
                "frozen": bool(age is not None and age > 2.0),
                "seq": self.ring.last_seq, "torn": self.ring.torn}

    def _record_status(self) -> dict:
        directory = self.cfg.record_dir()
        if not directory:
            return {"enabled": False}
        files = record_files(directory)
        return {"enabled": True, "dir": directory, "files": len(files),
                "bytes": sum(f["bytes"] for f in files), "limit": self.cfg.record_limit_bytes(),
                # wall clock of the running session: what the REC timer counts from
                "since": int(self.enc.started_at) if getattr(self.enc, "started_at", 0) else None}

    def _serve_file(self, path: str, head: bool, ctype: str = "video/mp2t",
                    attachment: str | None = None) -> None:
        """Segments with single-range support: AVPlayer asks for ranges even on live HLS."""
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            self.send_error(404, "file not found")
            return
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and "," not in rng:
            first, _, last = rng[6:].partition("-")
            try:
                if first:
                    start = max(0, int(first))
                    end = int(last) if last else size - 1
                elif last:                              # suffix range: the last N bytes
                    start = max(0, size - int(last))
                end = min(end, size - 1)
                if start >= size or start > end:
                    # a range past the end is unsatisfiable; pretending it is the whole segment
                    # makes AVPlayer re-request a file it thinks is already streaming fine
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        with fh:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head:
                return
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self._send(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # -- routing ------------------------------------------------------------
    def _body(self, limit: int = 8192) -> dict:
        """Read a small JSON object from the request body."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ValueError("empty body")
        if length > limit:
            raise ValueError(f"body larger than {limit} bytes")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("body is not valid JSON") from None
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def do_POST(self):
        global LAST_REQUEST
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path not in ("/api/settings", "/api/clip"):
            self._json({"error": "not found"}, 404)
            return
        if not self._authorized(query):
            self._json({"error": "unauthorized"}, 401)
            return
        try:
            payload = self._body()
        except ValueError as exc:                 # /api/clip takes an empty body: thirty seconds
            if url.path == "/api/clip":
                payload = {}
            else:
                self._json({"error": str(exc)}, 400)
                return
        if url.path == "/api/clip":
            LAST_REQUEST = time.time()
            try:
                result = clips.make_clip(self.cfg, payload.get("seconds", 30))
            except (ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 409)
                return
            self._json({"ok": True, **result})
            return
        try:
            result = self.runtime.change(payload)
        except ValueError as exc:                 # validation said no: nothing was touched
            self._json({"error": str(exc), "settings": prefs.current(self.cfg)}, 400)
            return
        except RuntimeError as exc:               # applied, did not survive, put back
            self._json({"error": str(exc), "rolled_back": True,
                        "settings": prefs.current(self.cfg)}, 409)
            return
        LAST_REQUEST = time.time()
        self._json({"ok": True, **result})

    def do_GET(self):
        self._route(False)

    def do_HEAD(self):
        self._route(head=True)

    def _route(self, head: bool) -> None:
        global LAST_REQUEST
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path

        if path == "/healthz":
            self._json({"ok": True}, head=head)
            return
        if path == "/log":
            if not self._authorized(query):
                self._json({"error": "unauthorized"}, 401, head=head)
                return
            self._beacon(query)
            return
        if path == "/qr.js":                      # a <script src> carries no token, and the file is public
            self._asset(os.path.join(HERE, "web", "qr.js"), "application/javascript", head)
            return
        if path == "/wake":                       # MediaMTX runOnDemand target, no token needed
            self.enc.ensure()
            LAST_REQUEST = time.time()
            self._json({"waking": True}, head=head)
            return
        if not self._authorized(query):
            self._json({"error": "unauthorized"}, 401, head=head)
            return

        LAST_REQUEST = time.time()
        self.enc.ensure()

        if path == "/api/settings":
            self._json(self.runtime.describe(), head=head)
            return
        if path == "/api/hotspot":
            self._json(hotspot.summary(self.cfg.port), head=head)
            return
        if path == "/status":
            # in ring mode capture fps is sampled from the ring; a device is measured by ffmpeg
            fps = self.enc.encoder_fps() if self.cfg.v4l2_device() else self.ring.fps()
            gaze: dict = {"enabled": self.gaze is not None}
            if self.gaze is not None:
                gaze.update(self.gaze.info())
            if self.gaze_rec is not None:
                gaze["csv"] = self.gaze_rec.info()
            self._json({"fps": round(fps, 1), "gaze": gaze, "record": self._record_status(),
                        "clip_window": self.cfg.clip_window, "picture": self._picture_state(),
                        **self.enc.status()}, head=head)
            return
        if path == "/gaze":
            self._gaze_json(head)
            return
        if path == "/gaze.sse":
            self._gaze_stream(head)
            return
        if path == "/gaze.csv":
            self._gaze_file(head)
            return
        if path == "/recordings":
            files = record_files(self.cfg.record_dir() or "")
            # the newest chunk is still open while the encoder lives: it has no index yet and will not
            # play, so say so instead of handing out a file that looks broken
            for f in files:
                f["open"] = False
            if files and self.enc.alive():
                files[-1]["open"] = True
            self._json({"enabled": bool(self.cfg.record_dir()), "dir": self.cfg.record_dir() or "",
                        "limit": self.cfg.record_limit_bytes(),
                        "bytes": sum(f["bytes"] for f in files),
                        "files": [{**f, "url": "/recordings/" + f["name"]} for f in files]},
                       head=head)
            return
        if path.startswith("/recordings/"):
            name = path[len("/recordings/"):]
            directory = self.cfg.record_dir()
            if (not directory or "/" in name or ".." in name
                    or not name.startswith("rec-") or not name.endswith(".mp4")):
                self._json({"error": "no such recording"}, 404, head=head)
                return
            self._serve_file(os.path.join(directory, name), head, "video/mp4", name)
            return
        if path == "/clips":
            self._json({"files": clips.clip_files(clips.clips_dir(self.cfg.hls_dir)),
                        "window": self.cfg.clip_window}, head=head)
            return
        if path.startswith("/clips/"):
            name = path[len("/clips/"):]
            if ("/" in name or ".." in name or not name.startswith(clips.CLIP_PREFIX)
                    or not name.endswith(".mp4")):
                self._json({"error": "no such clip"}, 404, head=head)
                return
            self._serve_file(os.path.join(clips.clips_dir(self.cfg.hls_dir), name), head, "video/mp4", name)
            return
        if path in ("/", "/index.html"):
            self._page(head)
            return
        if path.endswith(".m3u8"):
            target = os.path.join(self.cfg.hls_dir, "live.m3u8")
            for _ in range(100):                  # wait for a *parseable* playlist, not just a file
                try:
                    with open(target, encoding="utf-8", errors="replace") as fh:
                        if "#EXTINF" in fh.read():
                            break
                except OSError:
                    pass
                time.sleep(0.1)
            if head:
                size = os.path.getsize(target) if os.path.exists(target) else 0
                self.send_response(200 if size else 404)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                return
        if path.endswith(".ts"):
            self._serve_file(os.path.join(self.cfg.hls_dir, os.path.basename(path)), head)
            return
        if head:
            self.send_error(405, "HEAD not supported here")
            return
        super().do_GET()

    def _gaze_payload(self) -> dict:
        """Latest sample shaped for a player: degrees, plus -1..1 placement inside the flat image."""
        half_h, half_v = (v / 2.0 for v in self.cfg.gaze_fov_pair())
        info = self.gaze.info() if self.gaze is not None else {"available": False, "error": "gaze off"}
        sample = self.gaze.latest() if self.gaze is not None else None
        if sample is None:
            return {"available": False, **info, "fov": [half_h * 2, half_v * 2]}
        norm = lambda value, half: max(-1.0, min(1.0, value / half))  # noqa: E731
        return {
            **info,
            "fov": [half_h * 2, half_v * 2],
            **sample,
            "x": round(norm(sample["yaw"], half_h), 4),
            "y": round(norm(sample["pitch"], half_v), 4),
        }

    def _gaze_file(self, head: bool = False) -> None:
        """Serve the recording so far — a snapshot download, not a tail."""
        rec = self.gaze_rec
        path = rec.path if rec is not None else None
        if not path or not os.path.exists(path):
            self._json({"error": "no gaze recording (start with --gaze-csv auto)"}, code=404)
            return
        try:
            fh = open(path, "rb")
        except OSError as exc:
            self._json({"error": f"cannot read {path}: {exc.strerror or exc}"}, code=500)
            return
        with fh:
            size = os.fstat(fh.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", 'attachment; filename="gaze.csv"')
            self.end_headers()
            if head:
                return
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                try:
                    self._send(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return

    def _gaze_json(self, head: bool = False) -> None:
        self._json(self._gaze_payload(), head=head)

    def _gaze_stream(self, head: bool = False) -> None:
        """Server-sent events: no client library, automatic reconnects, no WebSocket handshake."""
        if self.gaze is None:
            self._json({"available": False, "error": "gaze off"}, 200)
            return
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        if head:                                   # HEAD must not open an hour-long stream
            return
        self._send(b"retry: 1000\n\n")
        last, start, beat = None, time.time(), time.time()
        try:
            while time.time() - start < 3600:
                payload = self._gaze_payload()
                stamp = payload.get("sample_time")
                if stamp != last:
                    last = stamp
                    body = json.dumps(payload, separators=(",", ":")).encode()
                    self._send(b"data: " + body + b"\n\n")
                    beat = time.time()
                elif time.time() - beat > 15:          # keep proxies and sleepy clients awake
                    self._send(b": ping\n\n")
                    self.wfile.flush()
                    beat = time.time()
                time.sleep(1 / 30)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _page(self, head: bool = False) -> None:
        token = self.cfg.token
        html = self.cfg.page
        try:                                  # pick up edits without restarting the daemon
            with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as fh:
                html = fh.read()
        except OSError:
            pass
        html = (html.replace("__WEBRTC__", self.cfg.webrtc_url)
                    .replace("__TOKEN__", json.dumps(token)[1:-1])   # safe inside a JS string
                    .replace("__LAYOUT__", self.cfg.layout))
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if token:
            self.send_header("Set-Cookie",
                             f"streamframe_token={token}; Path=/; SameSite=Lax; HttpOnly")
        self.end_headers()
        if not head:
            self._send(body)

    def log_message(self, format, *args):
        if self.cfg.verbose:
            log(f"{self.address_string()} {format % args}")


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def server_bind(self):
        # HTTPServer.server_bind() calls socket.getfqdn(), which can block for ~35 s on a box
        # whose own hostname does not resolve (common on headless/embedded machines).
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), port


def load_page(cfg: Config) -> str:
    for candidate in (os.path.join(HERE, "web", "index.html"),
                      os.path.join(HERE, "index.html")):
        if os.path.exists(candidate):
            with open(candidate, encoding="utf-8") as fh:
                return fh.read()
    return "<!doctype html><video src='live.m3u8' controls autoplay playsinline></video>"


def resolve_audio(cfg: Config) -> str | None:
    """Turn --audio into a PulseAudio/PipeWire source name.

    "auto" means the monitor of the current default sink — on SteamOS the compositor plays through
    PipeWire, so its sink monitor carries exactly the VR audio. Any failure degrades to video-only:
    losing the picture because the sound device moved would be the wrong trade.
    """
    want = (cfg.audio or "").strip()
    if want in ("", "0", "off", "no"):
        return None
    if want == "auto":
        try:
            out = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                 text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired) as exc:
            log(f"audio: cannot ask pactl ({exc}), continuing without sound")
            return None
        sink = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
        if not sink:
            log("audio: no default sink, continuing without sound")
            return None
        want = f"{sink}.monitor"
    return want


def mediamtx_ready(cfg: Config) -> bool | None:
    """True/False when MediaMTX answers, None when it is not there at all."""
    try:
        with urllib.request.urlopen(f"{cfg.mediamtx_api}/v3/paths/list", timeout=1.5) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return None
    for item in data.get("items") or []:
        if item.get("name") == RTSP_PATH:
            return bool(item.get("ready"))
    return False


def clear_hls(cfg: Config) -> None:
    try:
        names = os.listdir(cfg.hls_dir)
    except OSError:
        return
    for name in names:
        if name.startswith("live") and (name.endswith(".ts") or name.endswith(".m3u8")):
            try:
                os.unlink(os.path.join(cfg.hls_dir, name))
            except OSError:
                pass


def watchdog(cfg: Config, enc: Encoder, ring: FrameRing) -> None:
    """Idle stop + recovery of the RTSP branch. Never raises out of the loop."""
    lost_rtsp, rtsp_pause_until, ticks = 0, 0.0, 0
    slow, downgrades = 0, 0
    while True:
        time.sleep(5)
        ticks += 1
        if cfg.adapt and enc.alive() and ticks % 2 == 0 and not cfg.v4l2_device():
            # the compositor has to be producing full-rate frames: if the *game* runs at 40 fps,
            # the encoder will too, and that is not starvation — lowering the bitrate would only
            # make a healthy picture worse
            src = ring.fps()
            got = enc.encoder_fps()
            starving = bool(src and got and src > 0.9 * cfg.fps and got < 0.85 * cfg.fps)
            slow = slow + 1 if starving else 0
            if slow >= 3 and downgrades < 2 and enc.uptime() > 20:
                bitrate = max(1_500_000, int(mem_suffix(cfg.bitrate) * 0.7))
                cfg.bitrate = fmt_size(bitrate)
                downgrades += 1
                slow = 0
                log(f"encoder cannot keep up ({got:.0f} of {cfg.fps} fps); "
                    f"lowering to {cfg.bitrate} and restarting")
                enc.restart()
        if ticks % 6 == 0:                     # every 30 s is plenty for a size cap
            rec_dir = cfg.record_dir()
            if rec_dir:
                removed = prune_recordings(rec_dir, cfg.record_limit_bytes())
                if removed:
                    log(f"record: pruned {removed} old chunk(s) to stay under {cfg.record_limit}")
        try:
            if not enc.wanted():
                continue
            if LAST_REQUEST and time.time() - LAST_REQUEST > cfg.idle and enc.readers() == 0:
                log("idle, stopping encoder")
                enc.stop(wait=4.0)          # returns once the child is reaped
                clear_hls(cfg)              # a stale playlist pointing at deleted segments is worse
                continue
            # tee with onfail=ignore drops the RTSP branch for good when MediaMTX restarts:
            # notice it and re-run the encoder instead of pretending the second path still works
            if cfg.rtsp_url and enc.alive() and enc.uptime() > 15 and time.time() >= rtsp_pause_until:
                ready = mediamtx_ready(cfg)
                if ready is False:
                    lost_rtsp += 1
                    if lost_rtsp <= 3:
                        log(f"mediamtx has no publisher on /{RTSP_PATH} (attempt {lost_rtsp}), "
                            f"restarting encoder")
                        enc.restart()
                    else:
                        # do not give up for the life of the process: MediaMTX may take a while to
                        # come back, and a permanent switch to HLS-only is a silent loss of the fast path
                        rtsp_pause_until = time.time() + 300
                        lost_rtsp = 0
                        log("rtsp branch keeps failing; pausing retries for 5 min (HLS continues)")
                    continue
                if ready:
                    lost_rtsp = 0
        except Exception as exc:                      # keep the watchdog alive no matter what
            log(f"watchdog: {type(exc).__name__}: {exc}")


def build_config(argv=None) -> Config:
    ap = argparse.ArgumentParser(description="VR compositor live stream (HLS + RTSP/WebRTC)")
    ap.add_argument("--port", type=int, default=int(_env("PORT", 8101)))
    ap.add_argument("--bind", default=_env("BIND", "0.0.0.0"))
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", default="4M")
    ap.add_argument("--preset", default="ultrafast")
    ap.add_argument("--layout", default=None, choices=sorted(LAYOUTS),
                    help="mono-flat (default for the ring), passthrough (default for a device), "
                         "mono-round, stereo, mono-flat-right")
    ap.add_argument("--gaze", default=_env("GAZE", "auto"),
                    help='eye tracking: "auto" (when /dev/shm/eye-server.mmap exists), "off", or a path')
    ap.add_argument("--gaze-shm", default=_env("GAZE_SHM",
                                                         "/dev/shm/eye-server.mmap"))
    ap.add_argument("--gaze-csv", default=_env("GAZE_CSV", ""),
                    help='append every gaze sample to a CSV: "auto" = <hls-dir>/gaze.csv, or a path')
    ap.add_argument("--gaze-fov", default=_env("GAZE_FOV", "100x90"),
                    help="horizontal x vertical degrees the flat image covers, for reticle placement")
    ap.add_argument("--source", default=_env("SOURCE", "ring"),
                    help='"ring" = Vulkan layer shm (default), "v4l2:/dev/video99" = SteamVR '
                         'headset view, no layer needed')
    ap.add_argument("--vf", default=None, help="raw ffmpeg filter chain (overrides --layout)")
    ap.add_argument("--threads", type=int, default=int(_env("THREADS", 0)),
                    help="x264 threads (0 = auto)")
    ap.add_argument("--nice", type=int, default=int(_env("NICE", 10)),
                    help="nice level for ffmpeg so a game keeps the CPU (0 = off)")
    ap.add_argument("--adapt", action=argparse.BooleanOptionalAction, default=True,
                    help="lower the bitrate once or twice when the encoder cannot keep up")
    ap.add_argument("--record", default=_env("RECORD", ""),
                    help="mp4 recording dir: a path, 'auto' (~/streamframe-recordings) or '' for off")
    ap.add_argument("--record-time", type=int, default=300,
                    help="seconds per mp4 chunk (each chunk is playable once closed)")
    ap.add_argument("--record-limit", default="20G",
                    help="prune the oldest chunks past this total, e.g. 20G")
    ap.add_argument("--shm", default=_env("CAP_SHM", "/dev/shm/streamframe_frames"))
    ap.add_argument("--hls-dir", default=_env("HLS", "/tmp/streamframe-hls"))
    ap.add_argument("--hls-time", type=int, default=2)
    ap.add_argument("--hls-window", type=int, default=8)
    ap.add_argument("--clip-window", type=float, default=float(_env("CLIP_WINDOW", 90) or 90),
                    help="seconds of recent stream kept aside for /api/clip; 0 turns it off")
    ap.add_argument("--idle", type=float, default=float(_env("IDLE", 120)))
    ap.add_argument("--audio", default=_env("AUDIO", ""),
                    help='system audio to mux in: "auto" (default sink monitor) or a pulse source name')
    ap.add_argument("--rtsp-url", default=_env("RTSP", "rtsp://127.0.0.1:8554/vr"),
                    help="extra RTSP output for MediaMTX; empty disables it")
    ap.add_argument("--mediamtx-api", default="http://127.0.0.1:9997")
    ap.add_argument("--webrtc-url", default=_env("WEBRTC", ""))
    ap.add_argument("--token", default=_env("TOKEN", ""))
    ap.add_argument("--settings", default=_env("SETTINGS", "auto"),
                    help='where settings changed from the player are stored: "auto" '
                         '(~/.config/streamframe/settings.json), a path, or "" for memory only')
    ap.add_argument("--no-settings", action="store_true",
                    help="ignore the stored file and do not write one (escape hatch)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    if a.no_settings:
        a.settings = ""
    mem_suffix(a.bitrate)                      # fail fast on a bad bitrate
    cfg = Config(**{k: v for k, v in vars(a).items() if k in Config.__dataclass_fields__})
    if cfg.source not in ("ring", "") and not cfg.v4l2_device():
        raise SystemExit(f"--source must be 'ring' or 'v4l2:<device>', got {cfg.source!r}")
    if cfg.layout is None:
        # a device already delivers a flat, composed picture; the ring needs the fisheye unwarp
        cfg.layout = "passthrough" if cfg.v4l2_device() else "mono-flat"
    cfg.filter_chain()                         # fail fast on a bad layout/filter
    cfg.page = load_page(cfg)
    return cfg


def acquire_lock(cfg: Config):
    """One daemon per machine: two would fight over the playlist and the encoder."""
    path = os.path.join(tempfile.gettempdir(), f"streamframe-{cfg.port}.lock")
    try:
        fh = open(path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except OSError:
        return None


def main(argv=None) -> int:
    cfg = build_config(argv)
    os.makedirs(cfg.hls_dir, exist_ok=True)

    lock = acquire_lock(cfg)
    if lock is None:
        log("another Stream Frame daemon already runs on this machine (lock held)")
        return 1

    clear_hls(cfg)
    ring = FrameRing(cfg.shm)
    ring.open()
    enc = Encoder(cfg, ring)

    # values that a previous session proved to work; a broken file is ignored, not fatal
    stored = prefs.load(prefs.settings_path(cfg) or "")
    if stored:
        try:
            values = prefs.validate(stored)
        except ValueError as exc:
            log(f"settings: ignoring {prefs.settings_path(cfg)} — {exc}")
        else:
            prefs.apply(cfg, values)
            log(f"settings: {len(values)} stored value(s) applied from {prefs.settings_path(cfg)}")

    Handler.cfg, Handler.enc, Handler.ring = cfg, enc, ring
    runtime = Runtime(cfg, enc, ring)
    Handler.runtime = runtime
    runtime.start_gaze()
    srv = Server((cfg.bind, cfg.port), Handler)

    threading.Thread(target=watchdog, args=(cfg, enc, ring), daemon=True).start()

    # the last --clip-window seconds stay on disk: a clip is a concat of links, never a re-encode
    shutil.rmtree(clips.recent_dir(cfg.hls_dir), ignore_errors=True)
    keeper = clips.Keeper(cfg.hls_dir, cfg.clip_window, cfg.hls_time) if cfg.clip_window > 0 else None
    if keeper:
        keeper.start()

    stopping = threading.Event()

    def _bye(*_):
        if stopping.is_set():
            return
        stopping.set()
        if keeper:
            keeper.stop()
        enc.shutdown()
        runtime.shutdown()
        # shutdown() waits for serve_forever() to return: calling it from this thread deadlocks
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    log(f"streamframe on {cfg.bind}:{cfg.port} | layout={cfg.layout} | hls={cfg.hls_dir} | "
        f"{'rtsp=' + cfg.rtsp_url if cfg.rtsp_url else 'hls only'} | idle={cfg.idle:.0f}s")
    try:
        srv.serve_forever()
    finally:
        enc.shutdown()
        srv.server_close()
        try:
            lock.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
