#!/usr/bin/env python3
"""
map_names.py -- attach real asset paths to decompiled scripts.

PAYDAY 2 crates store only a 32-bit name hash per entry; the container has
no plaintext path table. We therefore recover paths by content-matching the
decompiled output against a known-good reference tree of PAYDAY 2 lua
(e.g. the pd2-lua reference dump), using:

  1. exact normalized-content match (very reliable), then
  2. line-set Jaccard similarity to pick the closest reference file.

Outputs a mirrored tree  <out>/named/<path>.lua  plus <out>/name_map.tsv.

Usage:
  python map_names.py <decompiled_dir> <reference_lua_tree> [--threshold 0.5]
"""
import argparse
import hashlib
import os
import sys


def norm_text(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    lines = data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(l.rstrip() for l in lines)


def content_key(text):
    return hashlib.sha1(norm_text(text).encode("utf-8")).hexdigest()


def line_set(text):
    return set(l.strip() for l in norm_text(text).split("\n") if l.strip())


def walk_lua(root):
    for dp, _, files in os.walk(root):
        for fn in files:
            if fn.endswith(".lua"):
                yield os.path.join(dp, fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("decompiled")
    ap.add_argument("reference")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--no-copy", action="store_true")
    args = ap.parse_args()

    ref_dir = args.reference
    refs = []
    by_hash = {}
    for p in walk_lua(ref_dir):
        try:
            t = norm_text(open(p, "rb").read())
        except OSError:
            continue
        rel = os.path.relpath(p, ref_dir).replace("\\", "/")
        rec = dict(path=rel, key=content_key(t), lines=line_set(t), size=len(t))
        refs.append(rec)
        by_hash.setdefault(rec["key"], []).append(rec)
    print("reference files: %d" % len(refs))

    named_root = os.path.join(args.decompiled, "named")
    if not args.no_copy:
        os.makedirs(named_root, exist_ok=True)

    mine = sorted(walk_lua(os.path.join(args.decompiled, "lua")))
    exact = fuzzy = none = 0
    map_rows = []

    for mp in mine:
        mtext = norm_text(open(mp, "rb").read())
        mline = line_set(mtext)
        mkey = content_key(mtext)
        base = os.path.basename(mp)

        match = None
        method = "none"
        score = 0.0

        if mkey in by_hash:
            match = by_hash[mkey][0]
            method = "exact"
            score = 1.0
            exact += 1
        else:
            # Jaccard against all reference lines; pick best
            best = None
            best_s = 0.0
            if mline:
                for rec in refs:
                    inter = len(mline & rec["lines"])
                    if not inter:
                        continue
                    union = len(mline | rec["lines"])
                    s = inter / union if union else 0.0
                    if s > best_s:
                        best_s = s
                        best = rec
            if best is not None and best_s >= args.threshold:
                match = best
                method = "fuzzy"
                score = best_s
                fuzzy += 1
            else:
                none += 1

        if match and not args.no_copy:
            dest = os.path.join(named_root, match["path"])
            if not dest.lower().endswith(".lua"):
                dest += ".lua"
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(mtext if mtext.endswith("\n") else mtext + "\n")

        map_rows.append((base, method, "%.3f" % score,
                         match["path"] if match else ""))

    tsv = os.path.join(args.decompiled, "name_map.tsv")
    with open(tsv, "w", encoding="utf-8") as f:
        f.write("script\tmethod\tscore\tpath\n")
        for r in map_rows:
            f.write("\t".join(r) + "\n")

    print("mapped: exact=%d fuzzy=%d unmapped=%d / %d" %
          (exact, fuzzy, none, len(mine)))
    print("name map ->", tsv)
    if not args.no_copy:
        print("named tree ->", named_root)


if __name__ == "__main__":
    main()
