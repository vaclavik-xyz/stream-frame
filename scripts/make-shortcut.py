#!/usr/bin/env python3
"""Write a Steam shortcuts.vdf holding non-Steam entries.

Steam's file is a small binary VDF; this writes the subset Steam reads and
nothing else, so an entry can be added without opening the desktop UI.
"""
import binascii
import struct
import sys


def _str(key, value):
    return b"\x01" + key.encode() + b"\x00" + value.encode() + b"\x00"


def _int(key, value):
    return b"\x02" + key.encode() + b"\x00" + struct.pack("<I", value)


def _end():
    return b"\x08"


def appid_for(exe, name):
    return (binascii.crc32((exe + name).encode()) & 0xFFFFFFFF) | 0x80000000


def entry(index, name, exe, start_dir, launch_options="", icon=""):
    body = b"\x00" + str(index).encode() + b"\x00"
    body += _int("appid", appid_for(exe, name))
    body += _str("AppName", name)
    body += _str("Exe", exe)
    body += _str("StartDir", start_dir)
    body += _str("icon", icon)
    body += _str("ShortcutPath", "")
    body += _str("LaunchOptions", launch_options)
    body += _int("IsHidden", 0)
    body += _int("AllowDesktopConfig", 1)
    body += _int("AllowOverlay", 1)
    body += _int("OpenVR", 0)
    body += _int("Devkit", 0)
    body += _str("DevkitGameID", "")
    body += _int("DevkitOverrideAppID", 0)
    body += _int("LastPlayTime", 0)
    body += _str("FlatpakAppID", "")
    body += b"\x00tags\x00" + _end()
    return body + _end()


def build(entries):
    out = b"\x00shortcuts\x00"
    for i, e in enumerate(entries):
        out += entry(i, *e)
    return out + _end() + _end()


if __name__ == "__main__":
    # argv: name|exe|startdir|options per entry
    items = []
    for spec in sys.argv[1:]:
        name, exe, startdir, opts = (spec.split("|") + ["", "", ""])[:4]
        items.append((name, exe, startdir, opts))
    sys.stdout.buffer.write(build(items))
