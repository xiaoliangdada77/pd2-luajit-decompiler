"""
pd2.bundle -- PAYDAY 2 Diesel bundle ("YAOI" crate) reader.

Format (reverse-engineered from retail .crate files):

  Header (12 bytes):
    magic   : 4s  == b"YAOI"
    version : u32 (== 1)
    count   : u32 number of entries

  Entry table: count * 48 bytes, little endian, layout:
    [0:4]   u32  reserved (0)
    [4:8]   u32  name-mask-0  (part of per-bundle name obfuscation key)
    [8:12]  u32  name-mask-1  (constant across entries; e0 differs)
    [12:16] u32  name-hash    (per-entry, changes every entry)
    [16:20] u32  reserved / unknown
    [20:24] u32  data offset  (absolute file offset)
    [24:28] u32  reserved (0)
    [28:32] u32  unknown
    [32:36] u32  reserved (0)
    [36:40] u32  compressed size (zlib stream length)
    [40:48] u64  reserved (0)

  Data payload: each entry is a standalone zlib stream at `offset`
  of length `size`.  Decompressed bytes carry the original asset.

Names are stored only as an obfuscated 32-bit hash; plaintext paths are
NOT present in the container. We therefore identify assets by index/hash
and by sniffing the decompressed content.
"""

import struct
import zlib

MAGIC = b"YAOI"
HEADER_SIZE = 12
ENTRY_SIZE = 48


class Entry:
    __slots__ = ("index", "offset", "size", "name_hash", "mask0", "mask1")

    def __init__(self, index, offset, size, name_hash, mask0, mask1):
        self.index = index
        self.offset = offset
        self.size = size
        self.name_hash = name_hash
        self.mask0 = mask0
        self.mask1 = mask1

    def __repr__(self):
        return "<Entry #%d off=%d size=%d hash=%08x>" % (
            self.index, self.offset, self.size, self.name_hash)


class Bundle:
    def __init__(self, path):
        self.path = path
        self.fd = open(path, "rb")
        head = self.fd.read(HEADER_SIZE)
        if len(head) < HEADER_SIZE or head[:4] != MAGIC:
            raise ValueError("Not a PAYDAY 2 YAOI bundle: %r" % head[:4])
        self.version, self.count = struct.unpack("<II", head[4:12])
        self._read_table()

    def _read_table(self):
        self.fd.seek(HEADER_SIZE)
        tbl = self.fd.read(self.count * ENTRY_SIZE)
        if len(tbl) < self.count * ENTRY_SIZE:
            raise ValueError("Truncated entry table")
        self.entries = []
        for i in range(self.count):
            r = tbl[i * ENTRY_SIZE:(i + 1) * ENTRY_SIZE]
            mask0 = struct.unpack("<I", r[4:8])[0]
            mask1 = struct.unpack("<I", r[8:12])[0]
            name_hash = struct.unpack("<I", r[12:16])[0]
            offset = struct.unpack("<I", r[20:24])[0]
            size = struct.unpack("<I", r[36:40])[0]
            self.entries.append(Entry(i, offset, size, name_hash, mask0, mask1))

    def read_raw(self, entry):
        self.fd.seek(entry.offset)
        return self.fd.read(entry.size)

    def read_decompressed(self, entry):
        raw = self.read_raw(entry)
        try:
            return zlib.decompress(raw)
        except zlib.error:
            # not zlib; return raw bytes as-is
            return raw

    def close(self):
        self.fd.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def iter_bundles(paths):
    for p in paths:
        yield Bundle(p)
