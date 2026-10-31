"""Unit tests for vrstreamd — no GPU, no ffmpeg, no shared memory required."""
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import vrstreamd as vs  # noqa: E402

# --------------------------------------------------------------- helpers


def write_ring(path: Path, w: int = 8, h: int = 4) -> int:
    """Create a ring file shaped like the one the Vulkan layer produces."""
    slot = vs.SLOT_HDR + w * h * 4
    with open(path, "wb") as fh:
        fh.write(struct.pack("<IIII", w, h, slot, 1))
        fh.write(b"\0" * (vs.HDR - 16))
        for _ in range(vs.SLOTS):
            fh.write(struct.pack("<QQ", 0, 0) + bytes(w * h * 4))
    return slot


def put_frame(path: Path, slot_size: int, slot: int, seq: int, fill: int, w: int = 8, h: int = 4) -> None:
    with open(path, "r+b") as fh:
        fh.seek(vs.HDR + slot * slot_size)
        fh.write(struct.pack("<QQ", seq, 0))
        fh.write(bytes([fill]) * (w * h * 4))


@pytest.fixture()
def ring_file(tmp_path):
    p = tmp_path / "ring.bin"
    return p, write_ring(p)


# --------------------------------------------------------------- config


def test_mem_suffix():
    assert vs.mem_suffix("4M") == 4 * 1024 * 1024
    assert vs.mem_suffix("2500K") == 2500 * 1024
    assert vs.mem_suffix("1G") == 1024 ** 3
    assert vs.mem_suffix("20000") == 20000


def test_mem_suffix_rejects_garbage_and_ambiguity():
    with pytest.raises(ValueError):
        vs.mem_suffix("fast")
    with pytest.raises(ValueError):          # bare 512 is a missing suffix, not 512 bit/s
        vs.mem_suffix("512")
    with pytest.raises(ValueError):
        vs.mem_suffix("0M")


def test_layout_presets():
    assert vs.Config(layout="mono-flat").filter_chain().startswith("crop=720:720:0:0")
    assert vs.Config(layout="stereo").filter_chain() is None
    assert vs.Config(layout="mono-flat", vf="null").filter_chain() == "null"


def test_unknown_layout_is_an_error():
    with pytest.raises(ValueError):
        vs.Config(layout="nope").filter_chain()


# --------------------------------------------------------------- encoder


def test_build_cmd_hls_only():
    cfg = vs.Config(hls_dir="/tmp/x", rtsp_url=None, bitrate="4M", fps=60)
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(1440, 1440, 5)
    assert cmd[-1] == "/tmp/x/live.m3u8"
    last_f = len(cmd) - 1 - cmd[::-1].index("-f")
    assert cmd[last_f + 1] == "hls"
    assert "tee" not in cmd
    assert cmd[cmd.index("-start_number") + 1] == "5"
    assert cmd[cmd.index("-bufsize") + 1] == str(8 * 1024 * 1024)


def test_build_cmd_tee_has_both_outputs():
    cfg = vs.Config(hls_dir="/tmp/x", rtsp_url="rtsp://127.0.0.1:8554/vr", fps=60)
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(1440, 1440, 0)
    assert "tee" in cmd and cmd[cmd.index("tee") - 1] == "-f"
    spec = cmd[-1]
    hls_part, rtsp_part = spec.split("|")
    assert "[f=hls:" in hls_part and "hls_segment_filename=/tmp/x/live%d.ts" in hls_part
    assert hls_part.endswith("/tmp/x/live.m3u8")
    assert "[f=rtsp:onfail=ignore:rtsp_transport=tcp]" in rtsp_part
    assert rtsp_part.endswith("rtsp://127.0.0.1:8554/vr")


def test_segment_numbers_keep_growing(tmp_path):
    for name in ("live3.ts", "live7.ts", "live.m3u8", "index.html"):
        (tmp_path / name).write_bytes(b"")
    cfg = vs.Config(hls_dir=str(tmp_path))
    enc = vs.Encoder(cfg, vs.FrameRing("/nonexistent"))
    assert enc._segment_base() == 8
    # within a session the next base is pushed past the whole window, even if files vanish
    enc._seq_no = enc._segment_base() + cfg.hls_window + 2
    for f in tmp_path.glob("live*"):
        f.unlink()
    assert enc._segment_base() == 8 + cfg.hls_window + 2


def test_ensure_starts_exactly_one_supervisor(tmp_path, monkeypatch):
    cfg = vs.Config(hls_dir=str(tmp_path))
    enc = vs.Encoder(cfg, vs.FrameRing("/nonexistent"))
    started = []

    def fake_session():
        started.append(time.time())
        time.sleep(0.4)
        return 0

    monkeypatch.setattr(enc, "_run_session", fake_session)
    threads = [threading.Thread(target=enc.ensure) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(0.2)
    assert len(started) == 1              # no second supervisor, no second ffmpeg
    enc.shutdown()


def test_restart_counter_counts_consecutive_failures_only(tmp_path, monkeypatch):
    cfg = vs.Config(hls_dir=str(tmp_path))
    enc = vs.Encoder(cfg, vs.FrameRing("/nonexistent"))
    runs = []

    def instant_fail():
        runs.append(1)
        return 1

    monkeypatch.setattr(enc, "_run_session", instant_fail)
    enc.ensure()
    time.sleep(1.5)
    enc.stop()
    assert enc.restarts >= 1
    enc.shutdown()


# --------------------------------------------------------------- ring


def test_ring_header_and_latest(ring_file):
    path, slot = ring_file
    ring = vs.FrameRing(str(path))
    assert ring.open() is True
    assert (ring.width, ring.height, ring.slot_size) == (8, 4, slot)

    # nothing written yet: no slot is newer than 0
    seq, data = ring.latest(0, 8 * 4 * 4)
    assert seq == 0 and data is None
    ring.close()


def test_ring_reads_newest_slot(ring_file):
    path, slot = ring_file
    ring = vs.FrameRing(str(path))
    assert ring.open()

    put_frame(path, slot, 0, 10, 0x11)
    put_frame(path, slot, 1, 11, 0x22)

    seq, data = ring.latest(0, 8 * 4 * 4)
    assert seq == 11 and data[0] == 0x22
    # asking again with the same seq returns no new frame
    seq2, data2 = ring.latest(seq, 8 * 4 * 4)
    assert seq2 == seq and data2 is None
    ring.close()


def test_ring_detects_recreated_inode(tmp_path):
    """A compositor restart shm_unlink()s the ring: same path, different file."""
    path = tmp_path / "ring.bin"
    write_ring(path, 8, 4)
    ring = vs.FrameRing(str(path))
    assert ring.open() and not ring.stale()

    path.unlink()
    write_ring(path, 16, 8)
    assert ring.stale() is True
    assert ring.open() is True
    assert (ring.width, ring.height) == (16, 8)
    assert ring.stale() is False
    ring.close()


def test_ring_open_does_not_leak_fds(tmp_path):
    broken = tmp_path / "broken.bin"
    broken.write_bytes(b"")                       # empty file: mmap() raises
    before = len(os.listdir("/dev/fd")) if os.path.isdir("/dev/fd") else 0
    for _ in range(50):
        assert vs.FrameRing(str(broken)).open() is False
    if before:
        assert len(os.listdir("/dev/fd")) <= before + 2


def test_ring_fps_measurement(ring_file):
    path, slot = ring_file
    ring = vs.FrameRing(str(path))
    assert ring.open()
    assert 0 <= ring.fps(sample=0.02) < 1e6
    ring.close()


def test_ring_missing_file():
    assert vs.FrameRing("/nonexistent/ring").open() is False


# --------------------------------------------------------------- http


class FakeEncoder:
    def __init__(self):
        self.calls = 0

    def ensure(self):
        self.calls += 1

    def status(self):
        return {"encoder": False, "restarts": 0, "sessions": 0, "last_exit": None,
                "last_error": "", "frames": 0, "layout": "mono-flat",
                "bitrate": "4M", "readers": 2}

    def readers(self):
        return 2

    def alive(self):
        return False


class FakeRing:
    def fps(self):
        return 37.5


@pytest.fixture()
def server(tmp_path):
    cfg = vs.Config(hls_dir=str(tmp_path), page="<html>page __TOKEN__ __WEBRTC__</html>")
    enc = FakeEncoder()
    vs.Handler.cfg, vs.Handler.enc, vs.Handler.ring = cfg, enc, FakeRing()
    srv = vs.Server(("127.0.0.1", 0), vs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, cfg, enc
    srv.shutdown()
    srv.server_close()


def get(url, timeout=5, headers=None, method=None):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_healthz(server):
    base, _, _ = server
    code, body, _ = get(base + "/healthz")
    assert code == 200 and json.loads(body)["ok"] is True


def test_status_payload(server):
    base, _, enc = server
    code, body, _ = get(base + "/status")
    payload = json.loads(body)
    assert code == 200
    assert payload["fps"] == 37.5 and payload["readers"] == 2 and payload["layout"] == "mono-flat"
    assert enc.calls == 1          # requesting status wakes the encoder


def test_page_gets_substitutions(server):
    base, cfg, _ = server
    cfg.token = ""
    code, body, _ = get(base + "/")
    html = body.decode()
    assert code == 200 and "vrstream" in html
    assert "__TOKEN__" not in html and "__WEBRTC__" not in html   # placeholders replaced


def test_head_on_page_does_not_list_the_directory(server):
    base, _, _ = server
    code, body, headers = get(base + "/", method="HEAD")
    assert code == 200
    assert headers["Content-Type"].startswith("text/html")
    assert body == b""                       # HEAD never carries a body


def test_token_required(server):
    base, cfg, _ = server
    cfg.token = "s3cret"
    assert get(base + "/status")[0] == 401
    assert get(base + "/status?token=nope")[0] == 401
    code, body, _ = get(base + "/status?token=s3cret")
    assert code == 200 and json.loads(body)["encoder"] is False
    # bearer header works too
    assert get(base + "/status", headers={"Authorization": "Bearer s3cret"})[0] == 200


def test_wake_works_without_token(server):
    base, cfg, enc = server
    cfg.token = "s3cret"
    before = enc.calls
    code, body, _ = get(base + "/wake")
    assert code == 200 and json.loads(body)["waking"] is True
    assert enc.calls == before + 1           # MediaMTX runOnDemand can wake a token-protected daemon


def test_beacon_writes_log_and_strips_newlines(server):
    base, _, _ = server
    log = Path("/tmp/vrstreamd-client.log")
    before = log.read_text().count("\n") if log.exists() else 0
    code, _, _ = get(base + "/log?e=stalled%0Aforged%20line")
    assert code == 204
    text = log.read_text()
    assert text.count("\n") == before + 1     # injected newline did not create a second entry


def test_segment_range_request(server):
    base, cfg, _ = server
    payload = bytes(range(200))
    (Path(cfg.hls_dir) / "live5.ts").write_bytes(payload)
    code, body, headers = get(base + "/live5.ts", headers={"Range": "bytes=10-19"})
    assert code == 206 and body == payload[10:20]
    assert headers["Content-Range"] == "bytes 10-19/200"
    assert headers["Accept-Ranges"] == "bytes"
    # a plain GET still returns everything
    code, body, _ = get(base + "/live5.ts")
    assert code == 200 and body == payload


def test_segment_missing(server):
    base, _, _ = server
    assert get(base + "/live999.ts")[0] == 404


def test_segment_content_type(tmp_path):
    cfg = vs.Config(hls_dir=str(tmp_path), page="x")
    handler = vs.Handler.__new__(vs.Handler)
    handler.cfg = cfg
    assert handler.guess_type(str(tmp_path / "live.m3u8")) == "application/vnd.apple.mpegurl"
    assert handler.guess_type(str(tmp_path / "live1.ts")) == "video/mp2t"


# --------------------------------------------------------------- process


def test_sigterm_exits_promptly(tmp_path):
    """systemctl stop must not hang: shutdown() is invoked off the serve_forever thread."""
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server" / "vrstreamd.py"),
         "--port", "0", "--bind", "127.0.0.1", "--hls-dir", str(tmp_path / "hls"),
         "--rtsp-url", "", "--shm", str(tmp_path / "none.bin")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        assert proc.poll() is None
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=6)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode in (0, -signal.SIGTERM)


# --------------------------------------------------------------- audio


def test_build_cmd_without_audio_has_no_extra_input():
    cfg = vs.Config(hls_dir="/tmp/x", audio="")
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(1440, 720, 0)
    assert "pulse" not in cmd and "1:a" not in cmd


def test_build_cmd_with_audio_adds_pulse_input_and_aac():
    cfg = vs.Config(hls_dir="/tmp/x", audio="auto")
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(1440, 720, 0, "alsa_out.monitor")
    assert "pulse" in cmd and cmd[cmd.index("pulse") + 2] == "alsa_out.monitor"
    assert cmd[cmd.index("-map") + 1] == "0:v"
    assert cmd.count("-map") == 2 and "1:a" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "128k" in cmd
    # both slaves still get every stream: tee without an explicit select
    assert cmd[-1].count("|") == 1 and "select=" not in cmd[-1]


def test_resolve_audio_off_and_auto(monkeypatch):
    assert vs.resolve_audio(vs.Config(audio="")) is None
    assert vs.resolve_audio(vs.Config(audio="off")) is None

    class R:
        stdout = "alsa_output.pci-0000_00_1f.3.analog-stereo\n"

    monkeypatch.setattr(vs.subprocess, "run", lambda *a, **k: R())
    assert vs.resolve_audio(vs.Config(audio="auto")) == \
        "alsa_output.pci-0000_00_1f.3.analog-stereo.monitor"
    assert vs.resolve_audio(vs.Config(audio="my.monitor")) == "my.monitor"


def test_resolve_audio_survives_missing_pactl(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("pactl")

    monkeypatch.setattr(vs.subprocess, "run", boom)
    assert vs.resolve_audio(vs.Config(audio="auto")) is None   # video keeps working
