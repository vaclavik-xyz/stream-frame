#!/usr/bin/env python3
"""vrstreamd - live view of a VR compositor over HTTP.

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

HERE = os.path.dirname(os.path.abspath(__file__))

# ring buffer written by the Vulkan layer (see docs/architecture.md)
HDR = 64
SLOTS = 3
SLOT_HDR = 16

RTSP_PATH = "vr"

# layout presets: what the compositor presents -> what a TV wants to see
LAYOUTS = {
    # two lens-warped eyes side by side, untouched
    "stereo": None,
    # left eye, fisheye unwrapped to a flat 16:9 picture
    "mono-flat": (
        "crop=720:720:0:0,"
        "v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=75:w=1280:h=720,"
        "format=yuv420p"
    ),
    # left eye as presented, square
    "mono-round": "crop=720:720:0:0",
    # right eye, fisheye unwrapped
    "mono-flat-right": (
        "crop=720:720:720:0,"
        "v360=input=fisheye:output=flat:ih_fov=180:iv_fov=180:h_fov=110:v_fov=75:w=1280:h=720,"
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


@dataclass
class Config:
    port: int = 8101
    bind: str = "0.0.0.0"
    fps: int = 60
    bitrate: str = "4M"
    preset: str = "ultrafast"
    layout: str = "mono-flat"
    vf: str | None = None              # raw ffmpeg filter override
    shm: str = "/dev/shm/vrstream_frames"
    hls_dir: str = "/tmp/vrstream-hls"
    hls_time: int = 2
    hls_window: int = 8
    idle: float = 120.0
    audio: str = ""                    # "" = off, "auto" = default sink monitor, or a pulse source
    rtsp_url: str | None = "rtsp://127.0.0.1:8554/vr"
    mediamtx_api: str = "http://127.0.0.1:9997"
    webrtc_url: str = ""               # shown on the page, e.g. http://host:8889/vr
    token: str = ""
    verbose: bool = False
    page: str = field(default="")

    def filter_chain(self) -> str | None:
        if self.vf:
            return self.vf
        if self.layout not in LAYOUTS:
            raise ValueError(f"unknown layout {self.layout!r} "
                             f"(pick one of {', '.join(sorted(LAYOUTS))})")
        return LAYOUTS[self.layout]


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
        with self._lock:
            self._shutdown.set()
            self._want.set()
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
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
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{width}x{height}",
               "-r", str(cfg.fps), "-i", "-"]
        if audio:
            # system audio (PipeWire/Pulse monitor of the sink the headset plays into)
            cmd += ["-f", "pulse", "-i", audio]
        vf = cfg.filter_chain()
        if vf:
            cmd += ["-vf", vf]
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
        if not cfg.rtsp_url:
            return cmd + maps + hls
        # one encode, two outputs; onfail=ignore keeps HLS alive when MediaMTX is down
        opts = (f"hls_time={cfg.hls_time}:hls_list_size={cfg.hls_window}:hls_delete_threshold=4:"
                f"start_number={start_no}:"
                f"hls_flags=delete_segments+omit_endlist+independent_segments:"
                f"hls_segment_filename={cfg.hls_dir}/live%d.ts")
        tee = f"[f=hls:{opts}]{playlist}|[f=rtsp:onfail=ignore:rtsp_transport=tcp]{cfg.rtsp_url}"
        return cmd + ["-f", "tee"] + maps + [tee]

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
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=fferr)
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
            if fferr is not subprocess.DEVNULL:
                fferr.close()

    @staticmethod
    def _ffmpeg_log():
        path = "/tmp/vrstreamd-ffmpeg.log"
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


def log(*parts: object) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


class Handler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30                     # a half-open client must not pin a thread forever
    cfg: Config
    enc: Encoder
    ring: FrameRing

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
                if name == "vrstream_token":
                    given = value
        return given

    def _authorized(self, query: dict) -> bool:
        token = self.cfg.token
        if not token:
            return True
        return hmac.compare_digest(self._given_token(query), token)

    def _json(self, payload: dict, code: int = 200, head: bool = False) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _beacon(self, query: dict) -> None:
        text = (query.get("e") or [""])[0][:200]
        text = text.replace("\r", " ").replace("\n", " ")      # no log-line forging
        try:
            with open("/tmp/vrstreamd-client.log", "a") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {self.client_address[0]} {text}\n")
        except OSError:
            pass
        self.send_response(204)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _serve_segment(self, path: str, head: bool) -> None:
        """Segments with single-range support: AVPlayer asks for ranges even on live HLS."""
        try:
            size = os.path.getsize(path)
            fh = open(path, "rb")
        except OSError:
            self.send_error(404, "segment not found")
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
                if start > end:
                    start = 0
                code = 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        with fh:
            self.send_response(code)
            self.send_header("Content-Type", "video/mp2t")
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
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    # -- routing ------------------------------------------------------------
    def do_GET(self):
        self._route(head=False)

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
            self._beacon(query)
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

        if path == "/status":
            self._json({"fps": round(self.ring.fps(), 1), **self.enc.status()}, head=head)
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
            self._serve_segment(os.path.join(self.cfg.hls_dir, os.path.basename(path)), head)
            return
        if head:
            self.send_error(405, "HEAD not supported here")
            return
        super().do_GET()

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
                             f"vrstream_token={token}; Path=/; SameSite=Lax; HttpOnly")
        self.end_headers()
        if not head:
            self.wfile.write(body)

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


def watchdog(cfg: Config, enc: Encoder) -> None:
    """Idle stop + recovery of the RTSP branch. Never raises out of the loop."""
    lost_rtsp = 0
    while True:
        time.sleep(5)
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
            if cfg.rtsp_url and enc.alive() and enc.uptime() > 15:
                ready = mediamtx_ready(cfg)
                if ready is False:
                    lost_rtsp += 1
                    if lost_rtsp <= 3:
                        log(f"mediamtx has no publisher on /{RTSP_PATH} (attempt {lost_rtsp}), "
                            f"restarting encoder")
                        enc.restart()
                    else:
                        log("rtsp branch keeps failing, continuing with HLS only")
                        cfg.rtsp_url = None
                    continue
                if ready:
                    lost_rtsp = 0
        except Exception as exc:                      # keep the watchdog alive no matter what
            log(f"watchdog: {type(exc).__name__}: {exc}")


def build_config(argv=None) -> Config:
    ap = argparse.ArgumentParser(description="VR compositor live stream (HLS + RTSP/WebRTC)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("VRSTREAM_PORT", 8101)))
    ap.add_argument("--bind", default=os.environ.get("VRSTREAM_BIND", "0.0.0.0"))
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", default="4M")
    ap.add_argument("--preset", default="ultrafast")
    ap.add_argument("--layout", default="mono-flat", choices=sorted(LAYOUTS))
    ap.add_argument("--vf", default=None, help="raw ffmpeg filter chain (overrides --layout)")
    ap.add_argument("--shm", default=os.environ.get("VRSTREAM_CAP_SHM", "/dev/shm/vrstream_frames"))
    ap.add_argument("--hls-dir", default=os.environ.get("VRSTREAM_HLS", "/tmp/vrstream-hls"))
    ap.add_argument("--hls-time", type=int, default=2)
    ap.add_argument("--hls-window", type=int, default=8)
    ap.add_argument("--idle", type=float, default=float(os.environ.get("VRSTREAM_IDLE", 120)))
    ap.add_argument("--audio", default=os.environ.get("VRSTREAM_AUDIO", ""),
                    help='system audio to mux in: "auto" (default sink monitor) or a pulse source name')
    ap.add_argument("--rtsp-url", default=os.environ.get("VRSTREAM_RTSP", "rtsp://127.0.0.1:8554/vr"),
                    help="extra RTSP output for MediaMTX; empty disables it")
    ap.add_argument("--mediamtx-api", default="http://127.0.0.1:9997")
    ap.add_argument("--webrtc-url", default=os.environ.get("VRSTREAM_WEBRTC", ""))
    ap.add_argument("--token", default=os.environ.get("VRSTREAM_TOKEN", ""))
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    mem_suffix(a.bitrate)                      # fail fast on a bad bitrate
    cfg = Config(**{k: v for k, v in vars(a).items() if k in Config.__dataclass_fields__})
    cfg.filter_chain()                         # fail fast on a bad layout/filter
    cfg.page = load_page(cfg)
    return cfg


def acquire_lock(cfg: Config):
    """One daemon per machine: two would fight over the playlist and the encoder."""
    path = os.path.join(tempfile.gettempdir(), f"vrstreamd-{cfg.port}.lock")
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
        log("another vrstreamd already runs on this machine (lock held)")
        return 1

    clear_hls(cfg)
    ring = FrameRing(cfg.shm)
    ring.open()
    enc = Encoder(cfg, ring)

    Handler.cfg, Handler.enc, Handler.ring = cfg, enc, ring
    srv = Server((cfg.bind, cfg.port), Handler)

    threading.Thread(target=watchdog, args=(cfg, enc), daemon=True).start()

    stopping = threading.Event()

    def _bye(*_):
        if stopping.is_set():
            return
        stopping.set()
        enc.shutdown()
        # shutdown() waits for serve_forever() to return: calling it from this thread deadlocks
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)

    log(f"vrstreamd on {cfg.bind}:{cfg.port} | layout={cfg.layout} | hls={cfg.hls_dir} | "
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
