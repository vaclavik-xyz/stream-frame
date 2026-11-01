# tests/synth_ring.py
import mmap
import os
import struct


class SyntheticRing:
    def __init__(self, name, width=1440, height=720, path=None):
        self.name = name
        self.width = width
        self.height = height
        self.slot_size = 16 + width * height * 4
        self.num_slots = 3
        self.file_size = 64 + self.num_slots * self.slot_size
        # /dev/shm is where the layer puts it on Linux; a temp file works the
        # same for the daemon and lets the test run on a Mac too
        self.path = path or f"/dev/shm/{name}"
        self.seq = 0

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o666)
        os.ftruncate(fd, self.file_size)
        self.mm = mmap.mmap(fd, self.file_size)
        self.fd = fd

        header = struct.pack("<IIII", width, height, self.slot_size, 0)
        header += b"\x00" * (64 - len(header))
        self.mm[0:64] = header

    def write_frame(self):
        self.seq += 1
        slot_idx = self.seq % self.num_slots
        slot_offset = 64 + slot_idx * self.slot_size

        blue_val = self.seq % 256
        pixel = bytes([0, 0, blue_val, 255])
        pixel_data = pixel * (self.width * self.height)

        self.mm[slot_offset + 16 : slot_offset + 16 + len(pixel_data)] = pixel_data

        seq_bytes = struct.pack("<Q", self.seq)
        guard_bytes = b"\x00" * 8
        self.mm[slot_offset : slot_offset + 8] = seq_bytes
        self.mm[slot_offset + 8 : slot_offset + 16] = guard_bytes

        active_slot = slot_idx
        self.mm[12:16] = struct.pack("<I", active_slot)

    def close(self):
        if hasattr(self, "mm") and self.mm:
            self.mm.close()
            self.mm = None
        if hasattr(self, "fd") and self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if hasattr(self, "path") and os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass
