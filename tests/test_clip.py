"""The clip shelf: hardlinks aside from the HLS window, then one concat with -c copy."""
import os
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import clip  # noqa: E402


def segment(directory: Path, name: str, mtime: float) -> Path:
    path = directory / name
    path.write_bytes(b"\x47" + b"\0" * 187)
    os.utime(path, (mtime, mtime))
    return path


def test_sweep_links_every_segment_aside(tmp_path):
    now = time.time()
    for i in range(4):
        segment(tmp_path, f"live{i}.ts", now - (3 - i) * 2)
    assert clip.sweep(str(tmp_path), window=60, segment=2) == 4
    links = os.listdir(clip.recent_dir(str(tmp_path)))
    assert len(links) == 4
    assert clip.sweep(str(tmp_path), window=60, segment=2) == 0      # idempotent


def test_a_link_survives_ffmpeg_deleting_its_own_segment(tmp_path):
    now = time.time()
    src = segment(tmp_path, "live0.ts", now)
    clip.sweep(str(tmp_path), window=60, segment=2)
    link = os.path.join(clip.recent_dir(str(tmp_path)), os.listdir(clip.recent_dir(str(tmp_path)))[0])
    os.unlink(src)
    assert os.path.exists(link)                                      # same inode, our name keeps it
    assert os.path.getsize(link) == 188


def test_sweep_forgets_what_fell_out_of_the_window(tmp_path):
    now = time.time()
    for i in range(10):
        segment(tmp_path, f"live{i}.ts", now - (9 - i) * 2)          # twenty seconds of history
    clip.sweep(str(tmp_path), window=6, segment=2)
    links = os.listdir(clip.recent_dir(str(tmp_path)))
    # six seconds of window plus two segments of slack: never all twenty seconds of history
    assert len(links) == 6
    oldest = min(os.path.getmtime(os.path.join(clip.recent_dir(str(tmp_path)), n)) for n in links)
    assert oldest >= now - 6 - 2 * 2


def test_sweep_is_off_when_the_window_is_zero(tmp_path):
    segment(tmp_path, "live0.ts", time.time())
    assert clip.sweep(str(tmp_path), window=0, segment=2) == 0
    assert not os.path.exists(clip.recent_dir(str(tmp_path)))


def test_pick_reaches_exactly_as_far_back_as_asked(tmp_path):
    entries = [(i, f"/s/{i}.ts") for i in range(10)]
    assert clip.pick(entries, seconds=6, segment=2) == ["/s/7.ts", "/s/8.ts", "/s/9.ts"]
    assert clip.pick(entries, seconds=999, segment=2) == [f"/s/{i}.ts" for i in range(10)]
    assert clip.pick([], seconds=10, segment=2) == []


def test_make_clip_concats_and_reports_what_it_got(tmp_path, monkeypatch):
    now = time.time()
    for i in range(8):
        segment(tmp_path, f"live{i}.ts", now - (7 - i) * 2)
    clip.sweep(str(tmp_path), window=60, segment=2)
    cfg = types.SimpleNamespace(hls_dir=str(tmp_path), hls_time=2, clip_window=60)

    seen = {}

    class Done:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"\0" * 1024)                      # ffmpeg writes the file
        return Done()

    monkeypatch.setattr(clip.subprocess, "run", fake_run)
    result = clip.make_clip(cfg, 6)
    assert result["segments"] == 3 and result["seconds"] == 6.0
    assert result["url"] == "/clips/" + result["name"]
    assert result["name"].startswith("clip-") and result["name"].endswith(".mp4")
    assert Path(tmp_path, "clips", result["name"]).exists()
    assert "-c" in seen["cmd"] and "copy" in seen["cmd"]             # never a re-encode
    assert "concat" in seen["cmd"]


def test_make_clip_refuses_when_the_shelf_is_empty(tmp_path):
    cfg = types.SimpleNamespace(hls_dir=str(tmp_path), hls_time=2, clip_window=60)
    with pytest.raises(ValueError, match="nothing to clip"):
        clip.make_clip(cfg, 30)


def test_make_clip_reports_ffmpeg_failure(tmp_path, monkeypatch):
    segment(tmp_path, "live0.ts", time.time())
    clip.sweep(str(tmp_path), window=60, segment=2)
    cfg = types.SimpleNamespace(hls_dir=str(tmp_path), hls_time=2, clip_window=60)

    class Bad:
        returncode = 1
        stderr = b"concat: bad timestamps\n"

    monkeypatch.setattr(clip.subprocess, "run", lambda cmd, **kw: Bad())
    with pytest.raises(ValueError, match="could not cut"):
        clip.make_clip(cfg, 2)


def test_clips_are_pruned_but_the_newest_stay(tmp_path):
    directory = tmp_path / "clips"
    directory.mkdir()
    for i in range(25):
        (directory / f"clip-{i:03d}.mp4").write_bytes(b"\0")
    assert clip.prune_clips(str(directory), keep_count=20) == 5
    names = sorted(os.listdir(directory))
    assert names[0] == "clip-005.mp4" and len(names) == 20


def test_clip_listing_is_newest_first(tmp_path):
    directory = tmp_path / "clips"
    directory.mkdir()
    for i, name in enumerate(["clip-a.mp4", "clip-b.mp4"]):
        path = directory / name
        path.write_bytes(b"\0")
        os.utime(path, (1000 + i, 1000 + i))
    files = clip.clip_files(str(directory))
    assert [f["name"] for f in files] == ["clip-b.mp4", "clip-a.mp4"]
    assert files[0]["url"] == "/clips/clip-b.mp4"
