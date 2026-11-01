"""The one place that may change a running pipeline.

The daemon is deliberately read-only: everything comes from the command line, and a player that can
rewrite its own encoder is a player that can break the stream for whoever is watching. This module is
the exception, so it also holds the whole policy — which fields may change, inside which range, what
counts as a successful change, and what is allowed to reach the disk.

Rules that are not negotiable here:

* anything outside the range is refused with a readable message, never clamped silently — a bitrate of
  200 kbit/s is not "a bit low", it is a broken picture;
* the settings file is written only after the new pipeline has been seen running, so a bad value can
  never survive a reboot;
* the caller keeps the previous command line around for rollback; this module only validates, snapshots
  and persists.
"""

from __future__ import annotations

import json
import os
import tempfile


def _core():
    """Import the daemon lazily: vrstreamd imports this module, and a cycle helps nobody."""
    import vrstreamd
    return vrstreamd


def fmt_size(n: int) -> str:
    return _core().fmt_size(n)


def mem_suffix(value: str) -> int:
    return _core().mem_suffix(value)

SETTINGS_VERSION = 1
DEFAULT_PATH = "~/.config/streamframe/settings.json"

# what a change costs: encoder fields need a new ffmpeg process, the rest only touch the daemon
ENCODER_FIELDS = {"bitrate", "fps", "layout", "threads", "nice", "adapt", "record", "record_time"}
LIVE_FIELDS = {"gaze"}

BITRATE_MIN = 1 * 1024 ** 2          # binary on purpose: the limits are what the panel shows (1M..12M)
BITRATE_MAX = 12 * 1024 ** 2
FPS_MIN, FPS_MAX = 15, 120
THREADS_MAX = 8


def settings_path(cfg) -> str | None:
    """Where settings are persisted, or None when persistence is disabled."""
    value = (getattr(cfg, "settings", "") or "").strip()
    if not value or value in ("0", "off", "no", "none"):
        return None
    if value == "auto":
        return os.path.expanduser(DEFAULT_PATH)
    return os.path.expanduser(value)


def _bitrate(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("bitrate has to be a size string such as 4M or 4000k")
    try:
        n = int(value) if isinstance(value, int) else mem_suffix(str(value).strip())
    except ValueError:
        raise ValueError("bitrate has to be a size string such as 4M or 4000k") from None
    if not BITRATE_MIN <= n <= BITRATE_MAX:
        raise ValueError(f"bitrate has to stay between {fmt_size(BITRATE_MIN)} and "
                         f"{fmt_size(BITRATE_MAX)} — below that the picture stops being watchable")
    # keep it exact and readable: 4000k stays 4000K, not the rounded 3.9M ffmpeg would still accept
    if n % 1048576 == 0:
        return f"{n // 1048576}M"
    if n % 1024 == 0:
        return f"{n // 1024}K"
    return str(n)


def _int(name: str, low: int, high: int):
    def check(value) -> int:
        if isinstance(value, bool):
            raise ValueError(f"{name} has to be a whole number")
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{name} has to be a whole number") from None
        if not low <= n <= high:
            raise ValueError(f"{name} has to stay between {low} and {high}, got {n}")
        return n
    return check


def _bool(name: str):
    def check(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "on", "yes", "1"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("false", "off", "no", "0"):
            return False
        raise ValueError(f"{name} has to be true or false")
    return check


def _layout(value) -> str:
    LAYOUTS = _core().LAYOUTS
    if not isinstance(value, str) or value not in LAYOUTS:
        raise ValueError(f"layout has to be one of {', '.join(sorted(LAYOUTS))}")
    return value


VALIDATORS = {
    "bitrate": _bitrate,
    "fps": _int("fps", FPS_MIN, FPS_MAX),
    "layout": _layout,
    "threads": _int("threads", 0, THREADS_MAX),
    "nice": _int("nice", 0, 19),
    "adapt": _bool("adapt"),
    "record": _bool("record"),
    "record_time": _int("record_time", 30, 3600),
    "gaze": _bool("gaze"),
}


# published to the panel so the ranges live in one place, not duplicated in JavaScript
LIMITS = {
    "bitrate": {"kind": "size", "min": BITRATE_MIN, "max": BITRATE_MAX},
    "fps": {"kind": "int", "min": FPS_MIN, "max": FPS_MAX},
    "threads": {"kind": "int", "min": 0, "max": THREADS_MAX},
    "nice": {"kind": "int", "min": 0, "max": 19},
    "record_time": {"kind": "int", "min": 30, "max": 3600},
    "adapt": {"kind": "bool"},
    "record": {"kind": "bool"},
    "gaze": {"kind": "bool"},
}


def current(cfg) -> dict:
    """What the user sees in the panel: effective values, not the raw flags."""
    return {
        "bitrate": cfg.bitrate,
        "fps": cfg.fps,
        "layout": cfg.layout,
        "threads": cfg.threads,
        "nice": cfg.nice,
        "adapt": bool(cfg.adapt),
        "record": bool(cfg.record_dir()),
        "record_time": cfg.record_time,
        "gaze": bool(cfg.gaze_source()),
    }


def validate(payload: dict) -> dict:
    """Check a partial update; raises ValueError listing every problem at once."""
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    unknown = sorted(set(payload) - set(VALIDATORS))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    if not payload:
        raise ValueError("no settings to change")
    clean, errors = {}, []
    for name, value in payload.items():
        try:
            clean[name] = VALIDATORS[name](value)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    return clean


def apply(cfg, values: dict) -> None:
    """Write validated values onto the live Config."""
    for name, value in values.items():
        if name == "record":
            if value and not cfg.record_dir():
                cfg.record = "auto"
            elif not value:
                cfg.record = ""
        elif name == "gaze":
            cfg.gaze = "auto" if value else "off"
        else:
            setattr(cfg, name, value)


def snapshot(cfg, values: dict) -> dict:
    """Undo-friendly copy of the fields a change may touch."""
    return {name: getattr(cfg, name) for name in values}


def restore(cfg, snapshot: dict) -> None:
    for name, value in snapshot.items():
        setattr(cfg, name, value)


def check_record_dir(cfg) -> str:
    """Raise unless the recording folder can actually be written to."""
    directory = cfg.record_dir()
    if not directory:
        return ""
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".probe-", delete=True):
            pass
    except OSError as exc:
        raise ValueError(f"recording folder {directory} is not writable: {exc.strerror or exc}") from None
    return directory


def load(path: str) -> dict:
    """Stored settings, or an empty dict when there are none (or they are damaged)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("version") != SETTINGS_VERSION:
        return {}
    stored = data.get("settings")
    return stored if isinstance(stored, dict) else {}


def save(path: str, values: dict) -> None:
    """Persist the settings that are currently running — atomically, owner-only."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {"version": SETTINGS_VERSION, "settings": values,
               "about": "written by the daemon after the pipeline ran with these values"}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".settings-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
