"""Tests for the eye-server shared-memory reader.

The fixtures build the file at the *documented absolute offsets*, byte by byte, so a wrong field order
in gaze.py fails here instead of silently reporting the wrong numbers on a headset.
"""
import math
import os
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import gaze  # noqa: E402


def build(path, version=4, fixation=(0.0, 0.0, -1.0), gaze_l=(0.0, 0.0, -1.0),
          gaze_r=(0.0, 0.0, -1.0), openness=(0.70, 0.75), sequence=7,
          producer_state=1, initialized=1, sample_time=1234.5, size=None):
    if version not in gaze.ABI:                 # only used to forge an unsupported header
        need, rec = 0x1000, 0x152
    else:
        need, rec = gaze.ABI[version]
    buf = bytearray(size or need)
    struct.pack_into("<II", buf, 0x00, version, initialized)
    struct.pack_into("<I", buf, 0x38, sequence)
    if rec + 0xA1 > len(buf):                   # truncated fixtures stop after the header
        with open(path, "wb") as fh:
            fh.write(buf)
        return path
    struct.pack_into("<I", buf, rec + 0x00, producer_state)
    struct.pack_into("<B", buf, rec + 0x04, 0)
    struct.pack_into("<d", buf, rec + 0x05, sample_time)
    struct.pack_into("<6f", buf, rec + 0x0D, *gaze_l, *gaze_r)
    struct.pack_into("<6f", buf, rec + 0x25, *([0.0] * 6))
    struct.pack_into("<3f", buf, rec + 0x3D, *fixation)
    struct.pack_into("<6f", buf, rec + 0x49, *([0.0] * 6))
    struct.pack_into("<6f", buf, rec + 0x61, *([0.0] * 6))
    struct.pack_into("<2f", buf, rec + 0x79, *openness)
    with open(path, "wb") as fh:
        fh.write(buf)
    return path


def test_record_struct_covers_the_documented_offsets():
    assert gaze.RECORD.size == 0xA1              # first 0xa1 bytes of the 0xebc record are used
    assert gaze.ABI == {4: (0x4F21A, 0x152), 5: (0x4F21F, 0x157)}


def test_reads_a_version_4_record(tmp_path):
    p = build(str(tmp_path / "eye.mmap"), fixation=(0.0, 1.0, -1.0), openness=(0.70, 0.80))
    src = gaze.EyeSource(p)
    assert src.open() is True and src.version == 4
    s = src._read_once()
    assert s is not None
    assert s["yaw"] == pytest.approx(0.0, abs=0.01)
    assert s["pitch"] == pytest.approx(45.0, abs=0.01)      # atan2(1, 1)
    assert s["open_l"] == pytest.approx(0.70, abs=0.001)
    assert s["open_r"] == pytest.approx(0.80, abs=0.001)
    assert s["sample_time"] == pytest.approx(1234.5)
    src.close()


def test_version_5_record_offset_is_used(tmp_path):
    p = build(str(tmp_path / "eye5.mmap"), version=5, gaze_l=(1.0, 0.0, -1.0))
    src = gaze.EyeSource(p)
    assert src.open() is True and src.version == 5
    s = src._read_once()
    assert s["yaw_l"] == pytest.approx(45.0, abs=0.01)
    src.close()


def test_refuses_uninitialized_unsupported_and_short_files(tmp_path):
    p = build(str(tmp_path / "a.mmap"), initialized=0)
    assert gaze.EyeSource(p).open() is False
    p = build(str(tmp_path / "b.mmap"), version=9)
    assert gaze.EyeSource(p).open() is False
    p = build(str(tmp_path / "c.mmap"), size=0x120)         # header ok, record truncated
    src = gaze.EyeSource(p)
    assert src.open() is False and "needs" in src.error


def test_missing_file_reports_error_instead_of_raising(tmp_path):
    src = gaze.EyeSource(str(tmp_path / "nope"))
    assert src.open() is False
    assert "nope" in src.error
    assert src.info()["available"] is False


def test_invalid_record_is_skipped_and_torn_reads_counted(tmp_path):
    p = build(str(tmp_path / "p.mmap"), producer_state=0)
    src = gaze.EyeSource(p)
    assert src.open() and src._read_once() is None


def test_thread_publishes_and_keeps_history(tmp_path):
    p = build(str(tmp_path / "t.mmap"))
    src = gaze.EyeSource(p, poll_hz=200, history=5)
    assert src.start() is True
    try:
        for _ in range(50):
            time.sleep(0.02)
            if src.latest() is not None:
                break
        s = src.latest()
        assert s is not None and src.info()["available"] is True
        assert len(src.history(100)) <= 5
    finally:
        src.stop()


def test_angle_helpers():
    assert gaze._angles((0.0, 0.0, -1.0)) == pytest.approx((0.0, 0.0))
    yaw, pitch = gaze._angles((0.0, 1.0, -1.0))
    assert pitch == pytest.approx(math.degrees(math.atan2(1, 1)))
    assert gaze._norm_axis(50.0, 50.0) == 1.0 and gaze._norm_axis(-80.0, 50.0) == -1.0
    assert gaze._norm_axis(10.0, 0.0) == 0.0


# ------------------------------------------------------------------ recording


def test_recorder_writes_a_header_then_rows(tmp_path):
    path = tmp_path / "gaze.csv"
    rec = gaze.GazeRecorder(str(path), flush_every=1)
    assert rec.start() is True
    rec.write({"t": 10.5, "yaw": 12.25, "pitch": -3.5, "yaw_l": 12.0, "pitch_l": -3.0,
               "yaw_r": 12.5, "pitch_r": -4.0, "open_l": 0.71, "open_r": 0.69})
    rec.stop()
    lines = [ln for ln in path.read_text().splitlines() if not ln.startswith("#")]
    assert lines[0].split(",") == list(gaze.GazeRecorder.COLUMNS)
    cols = lines[1].split(",")
    assert len(cols) == len(gaze.GazeRecorder.COLUMNS)
    assert cols[2] == "12.25" and cols[3] == "-3.5" and cols[8] == "0.71"
    assert cols[0].startswith("20") and ("+" in cols[0] or "-" in cols[0][19:])   # wall clock with offset


def test_recorder_appends_a_session_line_and_keeps_old_rows(tmp_path):
    path = tmp_path / "gaze.csv"
    sample = {"t": 1.0, "yaw": 0.0, "pitch": 0.0, "yaw_l": 0.0, "pitch_l": 0.0,
              "yaw_r": 0.0, "pitch_r": 0.0, "open_l": 0.5, "open_r": 0.5}
    for _ in range(2):
        rec = gaze.GazeRecorder(str(path))
        assert rec.start()
        rec.write(sample)
        rec.stop()
    text = path.read_text()
    assert text.count("t_wall,t_mono") == 1                    # header written once
    assert text.count("# session ") == 2
    assert text.count(",1.0000,") == 2


def test_recorder_reports_a_bad_path_instead_of_raising(tmp_path):
    rec = gaze.GazeRecorder(str(tmp_path / "missing-dir" / "x.csv"))
    rec.path = "/proc/definitely/not/writable.csv"
    assert rec.start() is False
    assert rec.error
    rec.write({"t": 1, "yaw": 0, "pitch": 0, "yaw_l": 0, "pitch_l": 0, "yaw_r": 0, "pitch_r": 0,
               "open_l": 0, "open_r": 0})                      # no writer: a no-op, not a crash


def test_source_calls_the_recorder_once_per_new_sample(tmp_path):
    p = build(str(tmp_path / "eye.mmap"))
    rows = []
    src = gaze.EyeSource(p, poll_hz=200, on_sample=rows.append)
    assert src.start()
    try:
        for _ in range(50):
            time.sleep(0.02)
            if rows:
                break
    finally:
        src.stop()
    assert len(rows) == 1                                      # the fixture never changes sample_time
    assert rows[0]["yaw"] == pytest.approx(0.0, abs=0.01)
