"""Unit tests for vrstreamd — no GPU, no ffmpeg, no shared memory required."""
import json
import os
import signal
import socket
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

    def stop(self, wait: float = 0.0):
        pass

    last_error = ""


class FakeRing:
    last_seq = 314
    torn = 2

    def fps(self):
        return 37.5

    def age(self):
        return 0.4


@pytest.fixture()
def server(tmp_path):
    cfg = vs.Config(hls_dir=str(tmp_path), page="<html>page __TOKEN__ __WEBRTC__</html>")
    enc = FakeEncoder()
    vs.Handler.cfg, vs.Handler.enc, vs.Handler.ring = cfg, enc, FakeRing()
    vs.Handler.runtime = vs.Runtime(cfg, enc, FakeRing())
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


# --------------------------------------------------------------- device source


def test_source_config_and_layout_defaults():
    cfg = vs.build_config(["--source", "v4l2:/dev/video99", "--rtsp-url", ""])
    assert cfg.v4l2_device() == "/dev/video99"
    assert cfg.layout == "passthrough"        # a device is already flat; no fisheye unwarp
    cfg = vs.build_config(["--rtsp-url", ""])
    assert cfg.v4l2_device() is None and cfg.layout == "mono-flat"


def test_source_rejects_nonsense():
    with pytest.raises(SystemExit):
        vs.build_config(["--source", "hdmi0", "--rtsp-url", ""])


def test_build_cmd_for_device_has_no_rawvideo():
    cfg = vs.Config(hls_dir="/tmp/x", source="v4l2:/dev/video99", layout="passthrough",
                    rtsp_url="")
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(0, 0, 3)
    assert "v4l2" in cmd and "/dev/video99" in cmd
    assert "rawvideo" not in cmd and "bgra" not in cmd
    assert cmd[cmd.index("-start_number") + 1] == "3"
    assert "scale=-2:720" in cmd[cmd.index("-vf") + 1]

    cfg.rtsp_url = "rtsp://127.0.0.1:8554/vr"
    tee_cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(0, 0, 3)
    assert "tee" in tee_cmd and "start_number=3" in tee_cmd[-1]
    assert "rawvideo" not in tee_cmd


def test_device_session_starts_and_stops(tmp_path, monkeypatch):
    dev = tmp_path / "video99"
    dev.write_bytes(b"")
    cfg = vs.Config(hls_dir=str(tmp_path / "hls"), source=f"v4l2:{dev}", layout="passthrough")
    enc = vs.Encoder(cfg, vs.FrameRing("/nonexistent"))
    monkeypatch.setattr(enc, "build_cmd",
                        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(60)"])
    enc.ensure()
    for _ in range(50):
        time.sleep(0.1)
        if enc.alive():
            break
    assert enc.alive() and enc.sessions == 1
    t0 = time.time()
    enc.stop(wait=5)
    assert time.time() - t0 < 6 and not enc.alive()
    enc.shutdown()


def test_device_session_reports_missing_node(tmp_path, monkeypatch):
    cfg = vs.Config(hls_dir=str(tmp_path), source="v4l2:" + str(tmp_path / "nope"),
                    layout="passthrough")
    enc = vs.Encoder(cfg, vs.FrameRing("/nonexistent"))
    enc.ensure()
    for _ in range(40):
        time.sleep(0.1)
        if enc.last_error:
            break
    assert "source device missing" in enc.last_error
    enc.shutdown()


# --------------------------------------------------------------------- gaze


class FakeGaze:
    def info(self):
        return {"available": True, "path": "/dev/shm/eye-server.mmap", "version": 4, "rate": 120.0,
                "samples": 10, "torn_reads": 0, "age": 0.01, "error": ""}

    def latest(self):
        return {"t": 1.0, "sample_time": 5.0, "yaw": 25.0, "pitch": -45.0,
                "yaw_l": 0.0, "pitch_l": 0.0, "yaw_r": 0.0, "pitch_r": 0.0,
                "open_l": 0.7, "open_r": 0.75, "fixation": [0.0, 0.0, -1.0]}


def test_gaze_route_is_honest_when_disabled(server, monkeypatch):
    base, _, _ = server
    monkeypatch.setattr(vs.Handler, "gaze", None)
    code, body, _ = get(base + "/gaze")
    payload = json.loads(body)
    assert code == 200 and payload["available"] is False and payload["error"] == "gaze off"


def test_gaze_route_maps_angles_into_the_flat_image(server, monkeypatch):
    base, cfg, _ = server
    cfg.gaze_fov = "100x90"
    monkeypatch.setattr(vs.Handler, "gaze", FakeGaze())
    code, body, _ = get(base + "/gaze")
    payload = json.loads(body)
    assert code == 200 and payload["available"] is True
    assert payload["fov"] == [100.0, 90.0]
    assert payload["x"] == pytest.approx(0.5)      # 25 deg of a 50 deg half-FOV
    assert payload["y"] == pytest.approx(-1.0)     # clamped at the bottom edge
    code, body, _ = get(base + "/status")
    assert json.loads(body)["gaze"]["available"] is True


def test_gaze_sse_emits_one_event(server, monkeypatch):
    """Read the event stream off a raw socket: no proxy, no urllib buffering in the way."""
    base, _, _ = server
    monkeypatch.setattr(vs.Handler, "gaze", FakeGaze())
    host, port = base.replace("http://", "").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as sock:
        sock.sendall(b"GET /gaze.sse HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        buf, deadline = b"", time.time() + 5
        while time.time() < deadline and b'"yaw":25.0' not in buf:
            try:
                part = sock.recv(512)
            except TimeoutError:
                break
            if not part:
                break
            buf += part
    text = buf.decode(errors="replace")
    assert "HTTP/1.1 200" in text
    assert "text/event-stream" in text
    assert "retry: 1000" in text and '"yaw":25.0' in text


# --------------------------------------------------------------- regressions
# each of these pins a bug found in review; the docstring says what went wrong before


def test_non_ascii_token_is_a_401_not_a_dropped_connection(server):
    """hmac.compare_digest on str raises TypeError for non-ASCII: the request died with no response."""
    base, cfg, _ = server
    cfg.token = "s3cret"
    try:
        assert get(base + "/status?token=%C5%BE")[0] == 401
        assert get(base + "/status?token=s3cret")[0] == 200
        assert get(base + "/status", headers={"Authorization": "Bearer s3cret"})[0] == 200
    finally:
        cfg.token = ""


def test_log_beacon_needs_the_token(server, monkeypatch):
    base, cfg, _ = server
    cfg.token = "s3cret"
    monkeypatch.setattr(vs, "BEACON_LOG", "/tmp/vrstreamd-test-beacon.log", raising=False)
    try:
        assert get(base + "/log?e=hello")[0] == 401
        assert get(base + "/log?token=s3cret&e=hello")[0] == 204
    finally:
        cfg.token = ""


def test_log_beacon_rotates_instead_of_filling_tmpfs(server, monkeypatch, tmp_path):
    base, _, _ = server
    big = tmp_path / "client.log"
    big.write_bytes(b"x" * 4096)
    monkeypatch.setattr(vs, "BEACON_MAX_BYTES", 1024)
    monkeypatch.setattr(vs, "BEACON_LOG", str(big), raising=False)
    assert get(base + "/log?e=" + "a" * 50)[0] == 204
    assert (tmp_path / "client.log.1").exists()
    assert Path(str(big)).stat().st_size < 4096


def test_range_past_the_end_is_416(server):
    """bytes=999999- used to be answered with the whole segment, still labelled 206."""
    base, cfg, _ = server
    (Path(cfg.hls_dir) / "live7.ts").write_bytes(bytes(range(50)))
    code, _, headers = get(base + "/live7.ts", headers={"Range": "bytes=999999-"})
    assert code == 416
    assert headers["Content-Range"] == "bytes */50"


def test_head_on_the_event_stream_returns_no_stream(server, monkeypatch):
    base, _, _ = server
    monkeypatch.setattr(vs.Handler, "gaze", FakeGaze())
    code, body, headers = get(base + "/gaze.sse", method="HEAD")
    assert code == 200 and body == b""
    assert headers["Content-Type"].startswith("text/event-stream")


def test_ring_refuses_a_frame_overwritten_during_the_copy(ring_file, monkeypatch):
    """A writer that bumps seq after memcpy means a frame can be half old, half new."""
    path, slot = ring_file
    put_frame(path, slot, 0, 5, fill=7)
    ring = vs.FrameRing(str(path))
    assert ring.open()
    calls = {"n": 0}

    def moving_sequence(_slot):
        calls["n"] += 1
        return 5 if calls["n"] <= vs.SLOTS else 6        # the counter moves on the re-check

    monkeypatch.setattr(ring, "_seq", moving_sequence)
    seq, data = ring.latest(0, 8 * 4 * 4)
    assert (seq, data) == (0, None)
    assert ring.torn == 1
    ring.close()


# --------------------------------------------------------------- recording


def test_build_cmd_records_mp4_as_an_extra_tee_slave(tmp_path):
    rec = tmp_path / "rec"
    cfg = vs.Config(rtsp_url="", record=str(rec))
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(1440, 720, 0)
    assert "-f" in [cmd[i] for i in range(len(cmd) - 1) if cmd[i + 1] == "tee"]
    tee = cmd[-1]
    assert "[f=hls:" in tee and "[f=segment:" in tee
    assert "segment_format=mp4" in tee
    assert str(rec) + "/rec-" in tee and "%03d.mp4" in tee
    assert rec.is_dir()                       # created before ffmpeg is spawned


def test_build_cmd_without_record_has_no_mp4_slave():
    cfg = vs.Config(rtsp_url="")
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(1440, 720, 0)
    assert "tee" not in cmd and "%03d.mp4" not in " ".join(cmd)


def test_record_dir_understands_off_auto_and_paths(tmp_path):
    assert vs.Config(record="").record_dir() is None
    assert vs.Config(record="off").record_dir() is None
    assert vs.Config(record="auto").record_dir().endswith("vrstream-recordings")
    assert vs.Config(record=str(tmp_path)).record_dir() == str(tmp_path)


def test_prune_recordings_deletes_the_oldest_but_keeps_the_open_one(tmp_path):
    for i in range(4):
        f = tmp_path / f"rec-20260101-0000{i}-000.mp4"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (1000 + i, 1000 + i))
    (tmp_path / "notes.txt").write_text("not ours")

    removed = vs.prune_recordings(str(tmp_path), 1500)     # room for one chunk only
    assert removed == 3
    left = [f.name for f in tmp_path.glob("rec-*.mp4")]
    assert left == ["rec-20260101-00003-000.mp4"]
    assert (tmp_path / "notes.txt").exists()               # other files are not touched
    assert vs.prune_recordings(str(tmp_path), 0) == 0      # never deletes the last chunk


def test_recordings_route_lists_and_serves_files(server, tmp_path):
    base, cfg, _ = server
    cfg.record = str(tmp_path)
    (tmp_path / "rec-20260101-1200-000.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    (tmp_path / "rec-20260101-1200-001.mp4").write_bytes(b"second")

    code, body, _ = get(base + "/recordings")
    assert code == 200
    listing = json.loads(body)
    assert listing["enabled"] is True and len(listing["files"]) == 2
    assert listing["files"][0]["url"] == "/recordings/rec-20260101-1200-000.mp4"
    assert listing["bytes"] == len(b"\x00\x00\x00\x18ftypmp42") + len(b"second")

    code, body, headers = get(base + "/recordings/rec-20260101-1200-001.mp4")
    assert code == 200 and body == b"second"
    assert headers["Content-Type"] == "video/mp4"
    assert "rec-20260101-1200-001.mp4" in headers["Content-Disposition"]


def test_recordings_route_refuses_paths_outside_the_folder(server, tmp_path):
    base, cfg, _ = server
    cfg.record = str(tmp_path)
    for bad in ("/recordings/..%2Fetc%2Fpasswd", "/recordings/etc-passwd.mp4",
                "/recordings/rec-x-000.txt"):
        assert get(base + bad)[0] == 404


def test_status_reports_the_recording_folder(server, tmp_path):
    base, cfg, _ = server
    cfg.record = str(tmp_path)
    (tmp_path / "rec-1-000.mp4").write_bytes(b"x" * 10)
    status = json.loads(get(base + "/status")[1])
    assert status["record"]["enabled"] is True
    assert status["record"]["files"] == 1 and status["record"]["bytes"] == 10
    assert status["record"]["limit"] == 20 * 1024 ** 3


# --------------------------------------------------------------- picture truth


def test_status_says_how_old_the_picture_is(server):
    base, _, _enc = server
    ring = vs.Handler.ring
    ring.age = lambda: 0.4
    picture = json.loads(get(base + "/status")[1])["picture"]
    assert picture == {"source": "ring", "age": 0.4, "frozen": False, "seq": 314, "torn": 2}

    ring.age = lambda: 7.5
    assert json.loads(get(base + "/status")[1])["picture"]["frozen"] is True


def test_ring_age_is_none_before_the_first_frame(ring_file):
    path, _slot = ring_file
    ring = vs.FrameRing(str(path))
    assert ring.open() and ring.age() is None
    ring.close()


# --------------------------------------------------------------- cpu budget


def test_build_cmd_honours_the_thread_limit():
    cfg = vs.Config(rtsp_url="", threads=3)
    cmd = vs.Encoder(cfg, vs.FrameRing("/nonexistent")).build_cmd(1440, 720, 0)
    assert cmd[cmd.index("-threads") + 1] == "3"


def test_ffmpeg_is_niced_so_the_game_wins():
    nice = vs.Encoder._nice(["ffmpeg", "-x"], 10)
    assert nice == ["nice", "-n", "10", "ffmpeg", "-x"]
    assert vs.Encoder._nice(["ffmpeg"], 0) == ["ffmpeg"]


def test_bitrate_downgrade_stays_close_to_the_original():
    for value in ("4M", "2500K", "1G", "20M"):
        original = vs.mem_suffix(value)
        shrunk = vs.mem_suffix(vs.fmt_size(int(original * 0.7)))
        assert 0.65 * original <= shrunk <= 0.75 * original


def test_shutdown_closes_stdin_and_waits_before_terminating():
    """A SIGTERM straight away left the last MP4 chunk without its index."""
    class FakeProc:
        def __init__(self):
            self.stdin = open(os.devnull, "rb")
            self.stdin_closed = False
            self.terminated = False
            self.waited = []

        def poll(self):
            return None

        def wait(self, timeout=None):
            self.waited.append(timeout)
            return 0

        def terminate(self):
            self.terminated = True

    enc = vs.Encoder(vs.Config(rtsp_url=""), vs.FrameRing("/nonexistent"))
    proc = FakeProc()
    enc._proc = proc
    closed = []
    proc.stdin = type("S", (), {"close": lambda self: closed.append(True)})()
    enc.shutdown()
    assert closed == [True]              # EOF is the clean stop
    assert proc.waited                  # and we wait for ffmpeg to write its index
    assert proc.terminated is False


def test_recordings_marks_the_chunk_that_is_still_written(server, tmp_path):
    base, cfg, enc = server
    cfg.record = str(tmp_path)
    (tmp_path / "rec-1-000.mp4").write_bytes(b"x" * 10)
    (tmp_path / "rec-1-001.mp4").write_bytes(b"y" * 10)
    enc.alive = lambda: True
    files = json.loads(get(base + "/recordings")[1])["files"]
    assert [f["open"] for f in files] == [False, True]
    enc.alive = lambda: False
    files = json.loads(get(base + "/recordings")[1])["files"]
    assert [f["open"] for f in files] == [False, False]


# --------------------------------------------------------------- settings endpoint


def post(url, payload, timeout=5):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_settings_get_publishes_ranges_and_current_values(server):
    base, cfg, _ = server
    code, body, _ = get(base + "/api/settings")
    payload = json.loads(body)
    assert code == 200
    assert payload["settings"]["bitrate"] == cfg.bitrate
    assert payload["limits"]["bitrate"]["min"] == 1024 ** 2
    assert "stereo" in payload["layouts"]


def test_settings_need_the_token_when_one_is_set(server):
    base, cfg, _ = server
    cfg.token = "s3cret"
    assert get(base + "/api/settings")[0] == 401
    assert post(base + "/api/settings", {"gaze": False})[0] == 401
    assert get(base + "/api/settings?token=s3cret")[0] == 200


def test_settings_reject_a_value_that_would_break_the_picture(server):
    base, cfg, _ = server
    code, body = post(base + "/api/settings", {"bitrate": "150k"})
    assert code == 400 and "bitrate" in body["error"]
    assert cfg.bitrate == "4M"


def test_settings_reject_garbage_json(server):
    base, _, _ = server
    code, body = post(base + "/api/settings", b"{not json")
    assert code == 400 and "JSON" in body["error"]


def test_a_live_setting_is_applied_without_restarting_the_encoder(server):
    base, cfg, _ = server
    code, body = post(base + "/api/settings", {"gaze": False})
    assert code == 200 and body["ok"] is True
    assert body["restarted"] is False
    assert cfg.gaze == "off"
