# pd2-luajit-decompiler

A rebuilt, runnable **PAYDAY 2 LuaJIT script decompiler**.

It reads the game's Diesel bundles (`assets/scripts.crate`, magic `YAOI`),
inflates each entry (zlib), and decompiles the embedded **LuaJIT 2.1**
bytecode (`\x1bLJ\x02`) back into readable Lua source.

This is a fresh reconstruction of the pipeline behind repositories such as
`ontrigger/pd2-luajit-decompile` (which ship only the *output*), built on top
of ZNix's `ljd` Luajit-Decompiler.

---

## How it works (the reverse-engineering)

### 1. Diesel bundle ("YAOI" crate) format

Reverse-engineered directly from retail `.crate` files:

```
Header (12 bytes, little-endian)
  [0:4]   magic  = b"YAOI"
  [4:8]   version = 1
  [8:12]  entry_count

Entry table: entry_count * 48 bytes
  [0:4]   reserved
  [4:12]  u64  obfuscated name hash (unique per entry)
  [12:16] u32  per-entry name hash  <- used as output id
  [16:20] u32  unknown
  [20:24] u32  data offset (absolute)
  [24:28] ...
  [36:40] u32  compressed size (zlib stream length)
  [40:48] reserved

Payload: each entry is an independent zlib stream at `offset`, `size` long.
```

Names are stored **only** as a 32-bit hash (no plaintext path table inside
the container) — `pd2.bundle` therefore exposes `name_hash` and the engine
identifies entries by it.

### 2. LuaJIT scripts

Every entry of `assets/scripts.crate` inflates to a bytecode chunk beginning
`1b 4c 4a 02` (`\x1bLJ` + version byte `2` = **LuaJIT 2.1**). Those chunks are
fed straight into `ljd` (parser → AST builder → slotworks → unwarper →
mutator → Lua writer).

### 3. Names (optional)

Paths are not recoverable from the hash alone. `map_names.py` re-attaches
real asset paths by **content-matching** the decompiled scripts against a
known-good reference dump of PAYDAY 2 lua (exact hash first, then line-set
Jaccard similarity), producing a mirrored `named/<path>.lua` tree.

---

## Layout

```
ljd/                 Luajit-Decompiler (ZNix fork), unmodified
pd2/bundle.py        YAOI crate reader (+ zlib)
pd2decompile.py      main: crate -> bytecode -> lua source
map_names.py         attach real asset paths to output
```

## Usage

```bat
:: one bundle
python pd2decompile.py "X:\...\PAYDAY 2\assets\scripts.crate" out --index

:: scan every *.crate in a folder
python pd2decompile.py --all "X:\...\PAYDAY 2\assets" out --workers 8 --index

:: (optional) recover real paths using a reference lua dump
python map_names.py out "X:\...\payday-2-luajit-master\pd2-lua"
```

Output:

```
out/lua/<hash>_<idx>.lua     decompiled source
out/bc/<hash>_<idx>.ljbc     raw bytecode (with --keep-bc)
out/index.tsv                entry | hash | ok | out | size
out/named/<path>.lua         path-recovered mirror (from map_names.py)
```

## Result on retail PAYDAY 2

`assets/scripts.crate` → **1646 / 1646** LuaJIT chunks decompiled to Lua
source (verified byte-identical to reference output for the majority of
files).

## Notes / limits

- Bytecode version 2.0 (`\x1bLJ\x01`) is also handled.
- A handful of pathological chunks may decompile to partial source (ljd's
  own known limitations); the pipeline runs best-effort and never aborts a
  batch.
- Name recovery is best-effort (the container simply does not carry names).
