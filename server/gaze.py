#!/usr/bin/env python3
"""Read Steam Frame eye tracking from the runtime's private shared-memory record.

Valve does not publish an API for this. The eye server (the same process that foveates the picture)
keeps one record per new estimate in ``/dev/shm/eye-server.mmap`` behind a small header; this module
reads it the way ``frameeyeosc`` does, but in-process so the frames and the gaze share a clock.

Layout (little-endian, confirmed against frameeyeosc, which asserts every offset at compile time):

    header       0x00 version u32 | 0x04 initialized u32 | 0x08 pthread mutex (0x30 bytes)
                 0x38 sequence u32 (bumped per record, used as a futex word)
                 0x3c metadata_requested u32 (set to 1 to ask for a fresh record)
    record       abi 4 -> 0x152, abi 5 -> 0x157
                 +0x00 producer_state u32   (1 = valid)
                 +0x04 sample_flag u8
                 +0x05 sample_time f64      (runtime clock)
                 +0x0d gaze_direction      [[f32; 3]; 2]  left, right, after stereo fusion
                 +0x25 gaze_covariance      [[f32; 3]; 2]
                 +0x3d fixation_point      [f32; 3]        head-relative, -Z forward, metres
                 +0x49 pre_fusion_gaze     [[f32; 3]; 2]
                 +0x61 pre_fusion_cov      [[f32; 3]; 2]
                 +0x79 openness            [f32; 2]        raw lid values, left, right
                 total 0xebc

Reads use the sequence counter as a seqlock: if it moved while the record was being copied, the sample
is thrown away rather than reported half-written. Nothing here writes to the record; the one write is
the "please publish" flag, which is exactly what any consumer of this file has to do.
"""
from __future__ import annotations

import math
import mmap
import os
import struct
import threading
import time

HEADER_MIN = 0x40
SEQ_OFF = 0x38
REQUESTED_OFF = 0x3C
ABI = {4: (0x4F21A, 0x152), 5: (0x4F21F, 0x157)}

# packed, so no implicit padding: sample_time sits at +5 and everything after shifts with it
RECORD = struct.Struct("<IBd6f6f3f6f6f2f8f")
assert RECORD.size == 0xA1

U32 = struct.Struct("<I")
F32 = struct.Struct("<f")


def _angles(vec):
    """(yaw, pitch) in degrees for a head-relative direction with -Z forward."""
    x, y, z = vec
    if -z <= 0.0 and abs(z) < 1e-9:
        return 0.0, 0.0
    return math.degrees(math.atan2(x, -z)), math.degrees(math.atan2(y, -z))


def _norm_axis(value: float, half_fov: float) -> float:
    """Map an angle to -1..1 where the edge of a flat image sits at the half-FOV."""
    if half_fov <= 0:
        return 0.0
    return max(-1.0, min(1.0, value / half_fov))


class EyeSource:
    """Latest gaze sample from the eye server, refreshed by a small polling thread."""

    def __init__(self, path: str = "/dev/shm/eye-server.mmap", poll_hz: float = 120.0,
                 history: int = 600):
        self.path = path
        self.period = 1.0 / max(1.0, poll_hz)
        self.history_len = max(1, history)
        self.error = ""
        self.version = 0
        self._fh = None
        self._map = None
        self._record_off = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: dict | None = None
        self._samples: list[dict] = []
        self._count = 0
        self._started = time.time()
        self._torn = 0
        self._last_stat: tuple[int, int] | None = None

    # ---------------------------------------------------------------- lifecycle

    def open(self) -> bool:
        """Map the file. Returns False and sets .error instead of raising."""
        self.close()
        try:
            fh = open(self.path, "r+b")
        except OSError as exc:
            self.error = f"{self.path}: {exc.strerror or exc}"
            return False
        try:
            size = os.fstat(fh.fileno()).st_size
            if size < HEADER_MIN:
                raise ValueError(f"file is {size} bytes, header needs {HEADER_MIN}")
            mm = mmap.mmap(fh.fileno(), 0)
            version = U32.unpack_from(mm, 0)[0]
            initialized = U32.unpack_from(mm, 4)[0]
            if version not in ABI:
                raise ValueError(f"unsupported eye shared-memory version {version}")
            need, record_off = ABI[version]
            if size < need:
                raise ValueError(f"file is {size} bytes, version {version} needs {need}")
            if initialized != 1:
                raise ValueError("eye shared memory is not initialized yet")
            self._fh, self._map, self._record_off = fh, mm, record_off
            self.version = version
            self._last_stat = self._stat()
            self.error = ""
            return True
        except (ValueError, OSError) as exc:
            fh.close()
            self.error = f"{self.path}: {exc}"
            return False

    def close(self) -> None:
        mm, fh = self._map, self._fh
        self._map = self._fh = None
        if mm is not None:
            try:
                mm.close()
            except (BufferError, ValueError):
                pass
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def start(self) -> bool:
        if not self.open():
            return False
        self._thread = threading.Thread(target=self._run, name="gaze", daemon=True)
        self._thread.start()
        return True

    def stop(self, wait: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=wait)
            self._thread = None
        self.close()

    # ------------------------------------------------------------------- reading

    def _stat(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return st.st_ino, st.st_size

    def _read_once(self) -> dict | None:
        """One seqlock read. Returns None when the record moved or is not valid."""
        mm = self._map
        if mm is None:
            return None
        seq = U32.unpack_from(mm, SEQ_OFF)[0]
        if not seq:
            return None
        try:
            fields = RECORD.unpack_from(mm, self._record_off)
        except (struct.error, ValueError):
            return None
        if U32.unpack_from(mm, SEQ_OFF)[0] != seq:
            self._torn += 1
            return None
        if fields[0] != 1:                       # producer_state
            return None
        sample_time = fields[2]
        if not math.isfinite(sample_time):
            return None
        gaze = [(fields[3], fields[4], fields[5]), (fields[6], fields[7], fields[8])]
        fixation = (fields[15], fields[16], fields[17])
        openness = (fields[30], fields[31])
        if not all(math.isfinite(v) for v in (*gaze[0], *gaze[1], *fixation, *openness)):
            return None
        yaw, pitch = _angles(fixation)
        left = _angles(gaze[0])
        right = _angles(gaze[1])
        return {
            "t": round(time.monotonic(), 4),
            "sample_time": round(sample_time, 6),
            "yaw": round(yaw, 2),
            "pitch": round(pitch, 2),
            "yaw_l": round(left[0], 2), "pitch_l": round(left[1], 2),
            "yaw_r": round(right[0], 2), "pitch_r": round(right[1], 2),
            "open_l": round(openness[0], 3), "open_r": round(openness[1], 3),
            "fixation": [round(v, 4) for v in fixation],
        }

    def _poke(self) -> None:
        """Ask the eye server to publish the next estimate (the protocol's only write)."""
        mm = self._map
        if mm is not None:
            try:
                U32.pack_into(mm, REQUESTED_OFF, 1)
            except (struct.error, ValueError):
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            stat = self._stat()
            if stat is not None and stat != self._last_stat:   # SteamVR restarted the eye server
                self.open()
            sample = self._read_once()
            if sample is not None:
                with self._lock:
                    if self._latest is None or sample["sample_time"] != self._latest["sample_time"]:
                        self._latest = sample
                        self._count += 1
                        self._samples.append(sample)
                        if len(self._samples) > self.history_len:
                            del self._samples[: len(self._samples) - self.history_len]
            self._poke()
            self._stop.wait(self.period)

    # ------------------------------------------------------------------ queries

    def latest(self) -> dict | None:
        with self._lock:
            return dict(self._latest) if self._latest else None

    def history(self, n: int = 100) -> list[dict]:
        with self._lock:
            return list(self._samples[-max(1, n):])

    def info(self) -> dict:
        with self._lock:
            latest = dict(self._latest) if self._latest else None
            count, torn = self._count, self._torn
        elapsed = max(1e-6, time.time() - self._started)
        age = None if latest is None else round(time.monotonic() - latest["t"], 3)
        return {
            "available": latest is not None,
            "path": self.path,
            "version": self.version,
            "rate": round(count / elapsed, 1),
            "samples": count,
            "torn_reads": torn,
            "age": age,
            "error": self.error,
        }


def _main(argv: list[str]) -> int:
    """`python3 gaze.py [path] [seconds]` — print every new estimate; the first thing to run on a headset."""
    path = argv[1] if len(argv) > 1 else "/dev/shm/eye-server.mmap"
    seconds = float(argv[2]) if len(argv) > 2 else 30.0
    src = EyeSource(path)
    if not src.start():
        print(f"cannot read {path}: {src.error}")
        return 1
    print(f"reading {path} (abi {src.version}); move your eyes, Ctrl-C to stop")
    deadline, seen, last = time.time() + seconds, 0, None
    try:
        while time.time() < deadline:
            sample = src.latest()
            if sample is not None and sample["sample_time"] != last:
                last = sample["sample_time"]
                seen += 1
                print(f"{sample['yaw']:7.2f} {sample['pitch']:7.2f}  "
                      f"L {sample['yaw_l']:7.2f} {sample['pitch_l']:7.2f}  "
                      f"R {sample['yaw_r']:7.2f} {sample['pitch_r']:7.2f}  "
                      f"lids {sample['open_l']:.2f} {sample['open_r']:.2f}")
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        src.stop()
    info = src.info()
    print(f"{seen} new estimates, {info['rate']} Hz, {info['torn_reads']} torn reads")
    return 0 if seen else 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
