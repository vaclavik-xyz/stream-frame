"""End-to-end check of the capture path without a headset.

The ring is the contract between the Vulkan layer and the daemon, so a test can
be the writer: fill the ring the way the layer does and see whether a playable
segment comes out the other end. This is what catches a broken filter chain, a
crop that no longer matches the frame, or an encoder that never starts — none of
which the unit tests touch.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tests.synth_ring import SyntheticRing  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def status(port: int) -> dict:
    url = f"http://127.0.0.1:{port}/status"
    with urllib.request.urlopen(url, timeout=1.0) as resp:
        return json.loads(resp.read().decode("utf-8"))


def stop_quietly(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not in PATH")
def test_synthetic_ring_reaches_hls(tmp_path):
    port = free_port()
    hls_dir = tmp_path / "hls"
    hls_dir.mkdir()
    # the daemon and a parallel run must not share a ring
    ring = SyntheticRing(
        f"test_e2e_ring_{os.getpid()}", path=str(tmp_path / "ring")
    )
    proc = subprocess.Popen(
        [
            sys.executable, "server/streamframe.py",
            "--port", str(port),
            "--shm", ring.path,
            "--hls-dir", str(hls_dir),
            "--bitrate", "2M",
            "--layout", "mono-flat",
            "--no-settings",
        ],
        cwd=REPO_ROOT,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while True:
            try:
                status(port)
                break
            except OSError:
                if time.time() > deadline:
                    pytest.fail("daemon never answered /status")
                time.sleep(0.1)

        # keep feeding while waiting: the picture has to stay fresh, and the
        # segment only appears once ffmpeg has a few frames of its own
        interval = 1 / 30
        deadline = time.time() + 20
        segment = None
        while time.time() < deadline:
            ring.write_frame()
            time.sleep(interval)
            segments = sorted(hls_dir.glob("*.ts"))
            if segments:
                segment = segments[0]
                break
        assert segment is not None, "no HLS segment appeared within 20 s"

        body = status(port)
        assert body["encoder"] is True
        assert body["frames"] > 0
        assert body["picture"]["age"] < 2.0
        assert segment.stat().st_size > 0
    finally:
        stop_quietly(proc)
        ring.close()
