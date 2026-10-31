# What has been verified on the Steam Frame (aarch64, SteamOS holo)

Measured 2026-10-20, kernel 6.18, Mesa 26.3 (Turnip/zink, Adreno 750).

## There is no way out for the image

| Path | Status | Evidence |
|---|---|---|
| Wireless cast (AirPlay/Cast/Miracast) | does not exist | no listening daemon, `miracast` is a stub in the OS |
| Streaming link (dongle / PC Link) | one-directional | host → headset; Valve does not have the reverse direction |
| USB-C DisplayPort alt-mode | **none** | `/sys/class/drm` contains only `card0-DSI-1` and `card0-Writeback-1`; the USB-C partner reports `type: not_dfp` |
| SteamVR "Display Mirror" window | never appears | `showMirrorView=true` + geometry in `~/.config/openvr/config/steamvr.vrsettings` → the compositor does not create the window |

## The OpenVR mirror path is dead

`IVRCompositor::GetMirrorTextureGL(Eye_Left, ...)` returns `102 = VRCompositorError_InvalidTexture`
and `texId=0`. The compositor on the Frame is **Vulkan-only** (`Creating CHmdWindowVulkanWSI` in
`~/.local/share/Steam/logs/vrcompositor.txt`); it does not create a GL texture. This does not depend
on whether the user is wearing the headset.

## Vulkan is the only real path

The compositor creates its instance and device through the loader and presents via `VK_KHR_display` +
`VK_KHR_swapchain` (confirmed in the log). That leaves:

1. **The stock `VK_LAYER_LUNARG_screenshot`** (`/usr/lib/libVkLayer_screenshot.so`) — can write
   selected frames out as PPM. Verified on `vkcube` (see `tests/`). Risk: injection into the VR stack.
2. A custom layer from scratch — rejected as a first step: the first attempt crashed
   `vkCreateDevice` inside the gamescope WSI layer and triggered Deckard recovery.
3. DRM writeback (`card0-Writeback-1`) — requires DRM master, which the compositor holds; unusable
   as a safe read path.

## Traps

- `pkill -f`/`pgrep -f` with a pattern that also appears in the SSH command line kills your own
  session (exit 255). Build the pattern at runtime, or use `pgrep -x`.
- `steamvr-health-check` (the service's ExecStartPre) tries the OS slot fallback and resets unpacked
  Steam after a failed start. Keep it in mind during experiments and act within ~30 s.
