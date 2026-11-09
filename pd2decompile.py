#!/usr/bin/env python3
"""
pd2decompile -- PAYDAY 2 LuaJIT script decompiler.

Rebuilt toolchain:
  1. Read a PAYDAY 2 Diesel bundle (".crate", "YAOI" magic).
  2. Every script entry is a zlib stream that inflates to a real
     LuaJIT 2.1 (`\\x1bLJ\\x02`) bytecode chunk.
  3. Feed each chunk through ljd (Luajit-Decompiler, ZNix/LuaJIT fork)
     to rebuild readable Lua source.

Usage:
  python pd2decompile.py <bundle.crate> [out_dir] [options]
  python pd2decompile.py --all <assets_dir> [out_dir] [options]

Options:
  --all DIR         scan DIR/*.crate, decompile every LuaJIT chunk found
  --workers N       parallel workers (default: cpu count)
  --keep-bc         also write the raw .ljbc bytecode next to the .lua
  --index           write index.tsv  (hash, bundle, entry_index, size, output)
  --limit N         only process first N lua chunks (debug)
"""
import argparse
import os
import sys
import struct
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

# -- make local ljd importable regardless of cwd --
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pd2.bundle import Bundle          # noqa: E402

LUAJIT_MAGIC = b"\x1bLJ"


# --------------------------------------------------------------------------
# ljd decompilation
# --------------------------------------------------------------------------
def _init_ljd():
    """Import and prepare ljd modules (once per process)."""
    import ljd
    import ljd.rawdump.parser
    import ljd.rawdump.code
    import ljd.ast.builder
    import ljd.ast.unwarper
    import ljd.ast.slotworks
    import ljd.ast.validator
    import ljd.ast.locals
    import ljd.ast.mutator
    import ljd.lua.writer
    import ljd.pseudoasm.instructions
    return ljd


def _set_luajit_version(ljd, bc_version):
    if getattr(ljd, "CURRENT_VERSION", None) == bc_version:
        return
    ljd.CURRENT_VERSION = bc_version
    if bc_version == 2.0:
        from ljd.rawdump.luajit.v2_0.luajit_opcode import _OPCODES as opcodes
    elif bc_version == 2.1:
        from ljd.rawdump.luajit.v2_1.luajit_opcode import _OPCODES as opcodes
    else:
        raise ValueError("Unknown LuaJIT bytecode version %r" % bc_version)
    ljd.rawdump.code.init(opcodes)
    ljd.ast.builder.init()
    ljd.pseudoasm.instructions.init()


def decompile_chunk(bc_bytes, jsrc_path):
    """Decompile one LuaJIT bytecode chunk to Lua source text.

    Returns (ok, lua_text_or_error)."""
    ljd = _init_ljd()
    with open(jsrc_path, "wb") as f:
        f.write(bc_bytes)

    def on_hdr(preheader):
        _set_luajit_version(ljd, 2.0 if preheader.version == 1 else 2.1)

    import io
    try:
        header, prototype = ljd.rawdump.parser.parse(jsrc_path, on_hdr)
        if not prototype:
            return False, "ljd: failed to parse chunk"
        ast = ljd.ast.builder.build(header, prototype)
        if ast is None:
            return False, "ljd: AST builder returned None"
    except Exception as exc:
        return False, "ljd[parse]: %s: %s" % (type(exc).__name__, exc)

    # Tolerant pipeline: each stage is best-effort so one bad chunk still
    # yields as much source as possible (mirrors ljd's --catch_asserts).
    def safe(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            return None

    safe(ljd.ast.validator.validate, ast, warped=True)
    safe(ljd.ast.mutator.pre_pass, ast)
    safe(ljd.ast.validator.validate, ast, warped=True)
    safe(ljd.ast.locals.mark_locals, ast)
    safe(ljd.ast.slotworks.eliminate_temporary, ast, identify_slots=True)
    safe(ljd.ast.unwarper.unwarp, ast, False)
    safe(ljd.ast.locals.mark_local_definitions, ast)
    safe(ljd.ast.mutator.primary_pass, ast)
    safe(ljd.ast.locals.mark_locals, ast, alt_mode=True)
    safe(ljd.ast.locals.mark_local_definitions, ast)

    try:
        buf = io.StringIO()
        ljd.lua.writer.write(buf, ast)
        return True, buf.getvalue()
    except Exception as exc:
        return False, "ljd[write]: %s: %s" % (type(exc).__name__, exc)


# --------------------------------------------------------------------------
# per-chunk worker (runs in subprocess)
# --------------------------------------------------------------------------
def _process_one(job):
    """job = dict(bundle, out_dir, index, offset, size, keep_bc, ...).

    The worker re-opens the bundle and slices its own entry, so we never
    pickle raw bytes across the process pool."""
    bundle_path = job["bundle"]
    out_dir = job["out_dir"]
    idx = job["index"]
    keep_bc = job["keep_bc"]

    import zlib
    try:
        with open(bundle_path, "rb") as f:
            f.seek(job["offset"])
            raw = f.read(job["size"])
        try:
            bc = zlib.decompress(raw)
        except zlib.error:
            bc = raw
        if bc[:3] != LUAJIT_MAGIC:
            return dict(index=idx, ok=False, reason="not LuaJIT", hash=job["hash"])

        os.makedirs(os.path.join(out_dir, "bc"), exist_ok=True)
        os.makedirs(os.path.join(out_dir, "lua"), exist_ok=True)
        name = "%08x_%04d" % (job["hash"] & 0xFFFFFFFF, idx)
        if keep_bc:
            with open(os.path.join(out_dir, "bc", name + ".ljbc"), "wb") as f:
                f.write(bc)
        tmp = os.path.join(out_dir, "bc", ".%s_%d.tmp" % (name, os.getpid()))
        ok, result = decompile_chunk(bc, tmp)
        try:
            os.remove(tmp)
        except OSError:
            pass
        if ok:
            lua_dir = os.path.join(out_dir, "lua")
            out_path = os.path.join(lua_dir, name + ".lua")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(result)
            return dict(index=idx, ok=True, hash=job["hash"], out=out_path,
                        size=len(bc))
        else:
            return dict(index=idx, ok=False, reason=result, hash=job["hash"])
    except Exception as exc:
        return dict(index=idx, ok=False,
                    reason="%s: %s" % (type(exc).__name__, exc), hash=job["hash"])


# --------------------------------------------------------------------------
# main driver
# --------------------------------------------------------------------------
def find_jobs(bundle_path, limit=None):
    """Enumerate LuaJIT-bearing entries in one bundle (cheap: try zlib on each)."""
    import zlib
    b = Bundle(bundle_path)
    jobs = []
    with open(bundle_path, "rb") as f:
        for e in b.entries:
            f.seek(e.offset)
            raw = f.read(e.size)
            try:
                bc = zlib.decompress(raw)
            except zlib.error:
                continue
            if bc[:3] == LUAJIT_MAGIC:
                jobs.append(dict(bundle=bundle_path, index=e.index,
                                 offset=e.offset, size=e.size,
                                 hash=e.name_hash))
                if limit and len(jobs) >= limit:
                    break
    b.close()
    return jobs


def run(jobs, out_dir, workers, keep_bc, write_index):
    os.makedirs(os.path.join(out_dir, "lua"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "bc"), exist_ok=True)
    for j in jobs:
        j["out_dir"] = out_dir
        j["keep_bc"] = keep_bc

    results = []
    ok = fail = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_process_one, j): j for j in jobs}
        for done, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            if r["ok"]:
                ok += 1
            else:
                fail += 1
                if fail <= 20:
                    print("  [!] entry #%d fail: %s" % (r["index"], r["reason"]))
            if done % 100 == 0 or done == len(jobs):
                print("  progress %d/%d  (ok=%d fail=%d)" %
                      (done, len(jobs), ok, fail), flush=True)

    if write_index:
        idx_path = os.path.join(out_dir, "index.tsv")
        results.sort(key=lambda r: r["index"])
        with open(idx_path, "w", encoding="utf-8") as f:
            f.write("entry\thash\tok\tout\tsize\n")
            for r in results:
                f.write("%d\t%08x\t%d\t%s\t%s\n" % (
                    r["index"], r["hash"] & 0xFFFFFFFF, 1 if r["ok"] else 0,
                    r.get("out", ""), r.get("size", "")))
        print("index ->", idx_path)
    return ok, fail


def main():
    ap = argparse.ArgumentParser(description="PAYDAY 2 LuaJIT script decompiler")
    ap.add_argument("target", nargs="?", help="a .crate bundle, or assets dir with --all")
    ap.add_argument("out_dir", nargs="?", default=None)
    ap.add_argument("--all", metavar="DIR", default=None,
                    help="scan DIR/*.crate and decompile every LuaJIT chunk")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--keep-bc", action="store_true")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    if args.all:
        import glob
        bundles = sorted(glob.glob(os.path.join(args.all, "*.crate")))
        out_dir = args.target or os.path.join(HERE, "out")
    else:
        if not args.target:
            ap.error("provide a .crate file (or use --all DIR)")
        bundles = [args.target]
        out_dir = args.out_dir or os.path.join(HERE, "out")

    print("scaning %d bundle(s) for LuaJIT chunks..." % len(bundles))
    all_jobs = []
    for bp in bundles:
        try:
            js = find_jobs(bp, limit=args.limit)
        except Exception as e:
            print("  [skip] %s: %s" % (os.path.basename(bp), e))
            continue
        if js:
            print("  %s -> %d LuaJIT chunk(s)" % (os.path.basename(bp), len(js)))
        all_jobs.extend(js)

    if not all_jobs:
        print("No LuaJIT chunks found.")
        return 1

    print("decompiling %d chunk(s) -> %s" % (len(all_jobs), out_dir))
    ok, fail = run(all_jobs, out_dir, args.workers, args.keep_bc, args.index)
    print("\nDONE: ok=%d fail=%d  output=%s" % (ok, fail, out_dir))
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
