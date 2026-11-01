"""The audio tap, end to end, without a headset or a sound server.

Two things have broken here before and neither is visible in the unit tests: HLS
losing its audio because a second encoder was added next to it, and the RTSP
stream arriving at MediaMTX with AAC, which WebRTC silently drops. A sine tone
stands in for the monitor tap, so both can be checked on any machine.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.synth_ring import SyntheticRing  # noqa: E402
from tests.test_pipeline_e2e import free_port, stop_quietly  # noqa: E402

FFMPEG = shutil.which("ffmpeg") or ""
TONE = "lavfi:sine=frequency=440:sample_rate=48000"


def has_ffmpeg() -> bool:
    return bool(FFMPEG)


def has_libopus() -> bool:
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=30).stdout
    except OSError:
        return False
    return " libopus " in out


def mediamtx() -> str | None:
    return os.environ.get("MEDIAMTX") or shutil.which("mediamtx")


def streams_of(path: str) -> list[str]:
    """Audio/video codecs inside a segment or stream, in one line each."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                         capture_output=True, text=True, timeout=30).stderr
    return [ln.strip() for ln in out.splitlines() if "Stream #" in ln]


def start_daemon(tmp_path, ring, port, hls_dir, rtsp_url=None, extra=()):
    args = [
        sys.executable, "server/streamframe.py",
        "--port", str(port),
        "--shm", ring.path,
        "--hls-dir", str(hls_dir),
        "--bitrate", "1M",
        "--fps", "15",
        "--layout", "mono-flat",
        "--no-settings",
        "--audio", TONE,
        *extra,
    ]
    if rtsp_url:
        args += ["--rtsp-url", rtsp_url]
    return subprocess.Popen(args, cwd=REPO_ROOT, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_segment(ring, hls_dir, timeout=25):
    interval = 1 / 30
    deadline = time.time() + timeout
    while time.time() < deadline:
        ring.write_frame()
        time.sleep(interval)
        segments = sorted(hls_dir.glob("*.ts"))
        if segments:
            return segments[0]
    return None


def wait_for_api(port, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=1.0):
                return
        except OSError:
            time.sleep(0.1)
    pytest.fail("daemon never answered /status")


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not available")
def test_hls_keeps_aac_when_a_second_audio_encoder_is_configured(tmp_path):
    """The Opus track is for the RTSP slave only: adding it must not disturb HLS."""
    port = free_port()
    hls_dir = tmp_path / "hls"
    hls_dir.mkdir()
    ring = SyntheticRing(f"test_audio_hls_{os.getpid()}", path=str(tmp_path / "ring"))
    # a closed port: the RTSP slave is supposed to fail without taking HLS with it
    dead = free_port()
    proc = start_daemon(tmp_path, ring, port, hls_dir, f"rtsp://127.0.0.1:{dead}/vr")
    try:
        wait_for_api(port)
        segment = wait_for_segment(ring, hls_dir)
        assert segment is not None, "no HLS segment appeared"
        codecs = " ".join(streams_of(str(segment)))
        assert "Video: h264" in codecs
        assert "Audio: aac" in codecs, codecs
        assert "opus" not in codecs.lower(), "Opus must not reach the HLS muxer"
    finally:
        stop_quietly(proc)
        ring.close()


@pytest.mark.skipif(not has_ffmpeg() or not has_libopus(), reason="ffmpeg without libopus")
@pytest.mark.skipif(mediamtx() is None, reason="mediamtx not installed (set MEDIAMTX=path)")
def test_mediamtx_gets_an_opus_track_for_webrtc(tmp_path):
    """What the browser can play is decided by the tracks MediaMTX reports."""
    api_port, rtsp_port = free_port(), free_port()
    rtp_port, rtcp_port = free_port(), free_port()
    conf = tmp_path / "mediamtx.yml"
    conf.write_text(
        "logLevel: info\n"
        "api: yes\n"
        f"apiAddress: 127.0.0.1:{api_port}\n"
        f"rtspAddress: 127.0.0.1:{rtsp_port}\n"
        f"rtpAddress: 127.0.0.1:{rtp_port}\n"
        f"rtcpAddress: 127.0.0.1:{rtcp_port}\n"
        "rtspTransports: [tcp]\n"          # no UDP ports to fight over in a test run
        "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
        "paths:\n  all_others:\n"
    )
    server = subprocess.Popen([mediamtx(), str(conf)], start_new_session=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port = free_port()
    hls_dir = tmp_path / "hls"
    hls_dir.mkdir()
    ring = SyntheticRing(f"test_audio_mtx_{os.getpid()}", path=str(tmp_path / "ring"))
    proc = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{api_port}/v3/config/global/get", timeout=1.0)
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.skip("mediamtx did not start")

        proc = start_daemon(tmp_path, ring, port, hls_dir, f"rtsp://127.0.0.1:{rtsp_port}/vr")
        wait_for_api(port)
        assert wait_for_segment(ring, hls_dir) is not None

        tracks: list[str] = []
        deadline = time.time() + 20
        while time.time() < deadline:
            ring.write_frame()
            try:
                body = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{api_port}/v3/paths/list", timeout=1.0).read())
            except OSError:
                body = {"items": []}
            for item in body.get("items", []):
                if item.get("name") == "vr" and item.get("ready"):
                    tracks = item.get("tracks") or []
            if any(t.startswith("Opus") for t in tracks):
                break
            time.sleep(0.2)
        assert any(t.startswith("Opus") for t in tracks), f"webrtc would be silent: {tracks}"
        assert any(t.startswith("H264") for t in tracks), tracks
    finally:
        if proc is not None:
            stop_quietly(proc)
        stop_quietly(server)
        ring.close()
