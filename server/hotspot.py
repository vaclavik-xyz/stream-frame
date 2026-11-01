"""What the headset's own hotspot is doing, if SteamOS is running one.

SteamOS starts `softapmanager.service` at boot. It writes `/var/run/softap_hostapd.conf` for the AP
interface (`wlanap`), starts hostapd and its own dnsmasq, and leaves the AP up next to the station link —
on this hardware the station sits on 5 GHz and the AP on 6 GHz, so both live at once.

The file is world-readable, which is the whole point of this module: the player can hand a phone the
exact SSID and passphrase instead of sending anyone to go digging in /var/run. Nothing here changes the
network — it reads what is there and says what it found, or says nothing is running.
"""

from __future__ import annotations

import os
import re
import subprocess
import time

CONF = "/var/run/softap_hostapd.conf"
IFACE = "wlanap"
CACHE_SECONDS = 5.0

_cache: tuple[float, dict] | None = None


def _run(args: list[str]) -> str:
    """Run a read-only query and return its stdout, or "" when the tool is missing."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_conf(text: str) -> dict:
    """Pull the handful of fields a client needs out of a hostapd config."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()

    password = fields.get("sae_password") or fields.get("wpa_passphrase") or ""
    if fields.get("wpa_key_mgmt", "").upper().startswith("SAE") or fields.get("sae_password"):
        security = "WPA3"
    elif fields.get("wpa", "") in ("2", "3") or password:
        security = "WPA2"
    else:
        security = "open"

    channel = fields.get("channel", "")
    return {
        "ssid": fields.get("ssid", ""),
        "password": password,
        "security": security,
        "hidden": fields.get("ignore_broadcast_ssid", "0") not in ("", "0"),
        "channel": int(channel) if channel.isdigit() else None,
    }


def band_of(mhz: int) -> str:
    """Channel numbers repeat across bands (37 is both 5 GHz and 6 GHz), so ask the frequency."""
    if not mhz:
        return ""
    if mhz < 3000:
        return "2.4 GHz"
    return "6 GHz" if mhz >= 5925 else "5 GHz"


def state(conf: str = CONF, iface: str = IFACE, run=_run, fresh: bool = False) -> dict:
    """Everything a phone needs to join, plus whether anyone is on it right now."""
    global _cache
    now = time.monotonic()
    if not fresh and _cache and now - _cache[0] < CACHE_SECONDS:
        return dict(_cache[1])

    info = {
        "available": False, "interface": iface, "ssid": "", "password": "", "security": "",
        "hidden": False, "channel": None, "band": "", "freq": None, "address": "", "clients": 0,
        "signal": None,
    }
    try:
        with open(conf, encoding="utf-8") as fh:
            info.update(parse_conf(fh.read()))
    except OSError:
        pass                                        # no hotspot on this machine — a normal answer

    if info["ssid"]:
        # `ip -4 -brief addr show wlanap` -> "wlanap  UP  10.35.78.1/24 fe80::.../64"
        for line in run(["ip", "-4", "-brief", "addr", "show", iface]).splitlines():
            for token in line.split()[2:]:
                candidate = token.split("/")[0]
                if candidate.count(".") == 3:
                    info["address"] = candidate
        # `iw dev wlanap info` -> "channel 37 (6135 MHz), width: 160 MHz, center1: 6135 MHz"
        freq = re.search(r"channel\s+\d+\s+\((\d+)\s+MHz\)", run(["iw", "dev", iface, "info"]))
        if freq:
            info["freq"] = int(freq.group(1))
            info["band"] = band_of(info["freq"])
        stations = run(["iw", "dev", iface, "station", "dump"])
        info["clients"] = stations.count("Station ")
        signals = [int(m) for m in re.findall(r"signal:\s*(-?\d+)", stations)]
        if signals:
            info["signal"] = max(signals)
        info["available"] = bool(info["address"])

    if info["available"]:
        _cache = (now, dict(info))
    return info


def summary(port: int, **kwargs) -> dict:
    """The same view, plus the two things a client should type: the URL and the mDNS name."""
    info = state(**kwargs)
    info["port"] = port
    info["url"] = f"http://{info['address']}:{port}/" if info["available"] else ""
    # avahi publishes the machine's hostname; SteamOS names this machine after the device
    try:
        info["hostname"] = os.uname().nodename
        info["mdns"] = f"http://{info['hostname']}.local:{port}/"
    except OSError:                                  # pragma: no cover - uname does not fail in practice
        info["hostname"], info["mdns"] = "", ""
    info["wifi_payload"] = ""
    if info["available"] and info["password"]:
        hidden = "H:true;" if info["hidden"] else ""
        kind = "WPA" if info["security"] in ("WPA2", "WPA3") else info["security"]
        info["wifi_payload"] = f"WIFI:T:{kind};S:{info['ssid']};P:{info['password']};{hidden};"
    return info
