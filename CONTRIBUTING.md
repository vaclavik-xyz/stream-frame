# Contributing

Bug reports and small patches are welcome. This is a personal tool that turned
out to be useful, so the bar for features is "it solves a problem the author
hit", not "it is on a roadmap".

## Running it locally

```sh
python3 -m venv .venv
.venv/bin/pip install pytest ruff
.venv/bin/python server/streamframe.py --help
```

The daemon itself has no third-party dependencies; `ffmpeg` has to be in `PATH`
for anything that encodes.

You do **not** need a headset for most work. Without the Vulkan layer the daemon
has nothing to read, but the player page, settings, clipping, playback of
existing segments and all the unit tests run fine — and `tests/test_pipeline_e2e.py`
fakes the ring so the whole capture path can be exercised on a laptop.

## Tests

```sh
.venv/bin/python -m pytest tests
.venv/bin/ruff check .
```

Both have to be clean. The end-to-end test starts a real daemon and a real
ffmpeg; it skips itself when `ffmpeg` is missing and takes about fifteen seconds.

## Code

- Python 3.9+, standard library only in `server/`. No new dependencies without a
  strong reason: the daemon is meant to be copied to a Steam Deck and run.
- Line length 110, `ruff` as configured in `pyproject.toml`.
- Comments explain *why*, not *what*. If a line exists because of something
  surprising the hardware does, say which hardware.
- The C layer (`layer/vrcap.c`) has to stay dependency-free apart from Vulkan
  and libc, and must never block the compositor: it holds a trylock and drops
  the frame when the consumer is slow.

## Commits and pull requests

Commit subjects are lower case, prefixed with what they touch, and describe the
change in plain words — `webrtc: name the wifi interface instead of an address
that DHCP moves`, not `fix bug`.

A pull request needs a test that fails without it, or a paragraph that explains
why a test is impossible (hardware timing, a specific GPU, the compositor's
behaviour). Screenshots or a captured frame help a lot for anything visual.

If you changed the ring format or the request paths, say so in the PR — the
Vulkan layer, the daemon and anything already installed on a headset have to
agree on both.
