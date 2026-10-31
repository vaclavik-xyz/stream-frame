# Risk model and rules for experiments

## Why this is sensitive

`vrcompositor` is the only process that draws into the headset. If Vulkan device creation breaks for
it, SteamVR does not start. On SteamOS, `steamvr-health-check` (ExecStartPre of `steamvr.service`)
then, after a failed start:

- tries to switch to the previous OS slot,
- resets the "unpacked" Steam (`~/.local/share/Steam`, the runtime, not userdata),
- exits with a result that SteamOS treats as a VR crash.

This is not cosmetic — it is a modification to the system that the user never sees.

## Rules

1. **Never try it first on VR.** Every layer/env is first run against `vkcube` (`tests/vkcube-capture.sh`).
   Only once it demonstrably works there and nothing crashes may it go on `vrcompositor`.
2. **Rollback always ready.** `scripts/capture-off.sh` removes the drop-in, runs `daemon-reload`, stops
   a running health-check and brings VR back up.
3. **One change per attempt.** No combining layers, environment variables and config overrides in a
   single step.
4. **Check within 30 s.** After a VR restart, verify `vrserver` + compositor within 30 seconds; if they
   are not running, roll back immediately, before the health-check starts recovery.
5. **Never delete `~/.local/share/Steam` or `compatdata`** while "cleaning up" after an attempt.
6. **Record what was enabled.** `scripts/capture-on.sh` prints exactly what it added, so rollback is
   deterministic.

## What to do when VR does not start

```bash
scripts/capture-off.sh          # drop-in gone + restart
systemctl --user reset-failed steamvr.service
systemctl --user start steamvr.service
```

If the health-check is already running (`journalctl --user -u steamvr.service | grep health-check`),
kill it with a pattern assembled at runtime (e.g. `P="steamvr-heal""th-check"`), never as a literal in
the SSH command.
