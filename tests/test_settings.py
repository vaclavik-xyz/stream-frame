"""Settings that change a running pipeline: validation, rollback and persistence — no ffmpeg needed."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

import settings as prefs  # noqa: E402
import vrstreamd as vs  # noqa: E402

# --------------------------------------------------------------- validation


def test_a_bitrate_is_normalised_to_the_form_ffmpeg_gets():
    assert prefs.validate({"bitrate": "4000k"})["bitrate"] == "4000K"
    assert prefs.validate({"bitrate": 6 * 1048576})["bitrate"] == "6M"


def test_a_bitrate_that_would_look_broken_is_refused():
    with pytest.raises(ValueError, match="between 1M and 12M"):
        prefs.validate({"bitrate": "200k"})


def test_every_problem_is_reported_at_once():
    with pytest.raises(ValueError) as err:
        prefs.validate({"fps": 5, "nice": 40})
    message = str(err.value)
    assert "fps" in message and "nice" in message


def test_unknown_settings_are_refused_rather_than_ignored():
    with pytest.raises(ValueError, match="unknown setting"):
        prefs.validate({"brightness": 11})


def test_empty_change_is_a_mistake_not_a_no_op():
    with pytest.raises(ValueError, match="no settings"):
        prefs.validate({})


def test_booleans_accept_the_strings_a_panel_sends():
    assert prefs.validate({"adapt": "off"})["adapt"] is False
    assert prefs.validate({"adapt": "true"})["adapt"] is True
    with pytest.raises(ValueError, match="true or false"):
        prefs.validate({"adapt": "maybe"})


def test_layout_must_be_one_the_encoder_knows():
    assert prefs.validate({"layout": "stereo"})["layout"] == "stereo"
    with pytest.raises(ValueError, match="layout has to be one of"):
        prefs.validate({"layout": "anaglyph"})


def test_current_reports_effective_values_not_raw_flags():
    cfg = vs.Config(record="auto", gaze="off", bitrate="4M")
    state = prefs.current(cfg)
    assert state["record"] is True and state["gaze"] is False and state["bitrate"] == "4M"


def test_apply_and_restore_toggle_record_and_gaze():
    cfg = vs.Config(record="", gaze="off")
    before = prefs.snapshot(cfg, {"record": True, "gaze": True})
    prefs.apply(cfg, {"record": True, "gaze": True})
    assert cfg.record_dir() and cfg.gaze == "auto"
    prefs.restore(cfg, before)
    assert cfg.record_dir() is None and cfg.gaze == "off"


# --------------------------------------------------------------- persistence


def test_saved_settings_come_back_and_are_owner_only(tmp_path):
    cfg = vs.Config(bitrate="6M", fps=40)
    path = tmp_path / "settings.json"
    prefs.save(str(path), prefs.current(cfg))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    stored = prefs.load(str(path))
    assert stored["bitrate"] == "6M" and stored["fps"] == 40


def test_a_damaged_file_is_ignored_instead_of_crashing_the_daemon(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    assert prefs.load(str(path)) == {}


def test_settings_from_another_version_are_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 99, "settings": {"fps": 30}}), encoding="utf-8")
    assert prefs.load(str(path)) == {}


def test_persistence_can_be_switched_off():
    assert prefs.settings_path(vs.Config(settings="")) is None
    assert prefs.settings_path(vs.Config(settings="auto")).endswith("streamframe/settings.json")


# --------------------------------------------------------------- the change


class SteadyEncoder:
    """Stays alive across a restart: the change was fine."""

    last_error = ""

    def __init__(self):
        self.stops = 0

    def stop(self, wait: float = 0.0):
        self.stops += 1

    def ensure(self):
        pass

    def alive(self):
        return True


class DyingEncoder(SteadyEncoder):
    """Dies as soon as it is restarted: the change took the stream down."""

    last_error = "Conversion failed!"

    def alive(self):
        return self.stops == 0


def make_runtime(tmp_path, enc):
    cfg = vs.Config(settings=str(tmp_path / "settings.json"), hls_dir=str(tmp_path))
    return vs.Runtime(cfg, enc, None), cfg


def test_a_change_that_runs_is_applied_and_saved(tmp_path):
    runtime, cfg = make_runtime(tmp_path, SteadyEncoder())
    result = runtime.change({"bitrate": "6M", "fps": 40})
    assert result["restarted"] is True
    assert cfg.bitrate == "6M" and cfg.fps == 40
    assert prefs.load(str(tmp_path / "settings.json"))["bitrate"] == "6M"


def test_a_change_that_kills_the_encoder_is_rolled_back_and_never_saved(tmp_path):
    runtime, cfg = make_runtime(tmp_path, DyingEncoder())
    with pytest.raises(RuntimeError, match="Conversion failed"):
        runtime.change({"layout": "stereo"})
    assert cfg.layout == "mono-flat"                 # put back
    assert not (tmp_path / "settings.json").exists()  # and never remembered
    assert runtime.enc.stops >= 2                     # rolled forward, then back


def test_a_bad_value_never_reaches_the_encoder(tmp_path):
    enc = SteadyEncoder()
    runtime, cfg = make_runtime(tmp_path, enc)
    with pytest.raises(ValueError):
        runtime.change({"fps": 500})
    assert enc.stops == 0 and cfg.fps == 60


def test_live_settings_do_not_restart_the_encoder(tmp_path):
    enc = SteadyEncoder()
    runtime, cfg = make_runtime(tmp_path, enc)
    runtime.stop_gaze = lambda: None
    result = runtime.change({"gaze": False})
    assert result["restarted"] is False and enc.stops == 0


def test_an_unwritable_recording_folder_is_refused_before_anything_moves(tmp_path):
    runtime, cfg = make_runtime(tmp_path, SteadyEncoder())
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the folder should be", encoding="utf-8")
    cfg.record = str(blocker)
    with pytest.raises(ValueError, match="not writable"):
        runtime.change({"record_time": 60})
    assert cfg.record_time == 300
