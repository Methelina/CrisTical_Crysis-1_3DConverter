#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
unpack_crysis.py — CrisTical: standalone CryEngine .pak unpacker (CLI)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Builds the list of .pak archives first, then unpacks them one by one with a
tqdm progress bar and verbose stderr logging (tqdm bars appear when run in
a real console). Safe to re-run: a pak is skipped only when
:func:`cristical_core.pak_unpack.verify_unpacked` proves its folder is fully
and correctly unpacked (completion manifest + presence/size of every entry
+ crc32 of the newest file). Partial/corrupt folders are unpacked again.
``-rewrite`` forces a full re-extract regardless.

Usage::

    K:\\work\\CrisTical_Crysis3DConverter\\cris_env\\Scripts\\python.exe ^
        K:\\work\\CrisTical_Crysis3DConverter\\scripts\\unpack_crysis.py ^
        -i F:\\Games\\Crysis_3 -o F:\\Games\\Crysis_3\\C3\\__CONTENT -rewrite

    # single pak:
    python unpack_crysis.py -i F:\\Games\\Crysis_3\\C3\\Objects.pak

    # preview only (extract nothing):
    python unpack_crysis.py -i F:\\Games\\Crysis_3 --dry-run
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

# Allow running from anywhere: make sure the scripts/ dir (parent of the
# cristical_core package) is importable.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from cristical_core.pak_unpack import (  # noqa: E402
    plan_game_unpack, unpack_pak, verify_unpacked,
)


def _mb(size_bytes: int) -> str:
    return "%.1f MB" % (size_bytes / (1024.0 * 1024.0))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unpack_crysis.py",
        description="Standalone CryEngine .pak unpacker: plan first, "
                    "then extract the paks one by one (resumable).")
    parser.add_argument(
        "-i", "--input", required=True,
        help="game install dir, data root (folder with *.pak), "
             "or a single .pak file")
    parser.add_argument(
        "-o", "--out", default=None,
        help="output dir that will hold the <name>.pak_Unpacked folders "
             "(default: <parent of data root>/__CONTENT)")
    parser.add_argument(
        "-rewrite", dest="rewrite", action="store_true",
        help="re-extract paks already unpacked: delete the existing "
             "<name>_Unpacked folder first")
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="list the paks and the plan, extract nothing")
    parser.add_argument(
        "--crypto", dest="crypto", default=None,
        choices=["auto", "python", "numba", "cupy"],
        help="Twofish-CTR backend for encrypted (Crysis 3) paks: "
             "auto-detect (default), pure python, Numba JIT, or CuPy GPU. "
             "Equivalent to env CRISTICAL_CRYPTO_BACKEND.")
    args = parser.parse_args(argv)

    if args.crypto:
        from cristical_core.twofish_fast import set_backend
        effective = set_backend(args.crypto)
        print("[crypto] backend requested %r -> effective %r"
              % (args.crypto, effective))

    path = os.path.abspath(args.input)

    # ---- single .pak mode ------------------------------------------------
    if path.lower().endswith(".pak"):
        if not os.path.isfile(path):
            print("pak not found: %s" % path, file=sys.stderr)
            return 2
        out_root = os.path.abspath(args.out) if args.out else os.path.dirname(path)
        if args.dry_run:
            print("would unpack: %s -> %s" % (path, out_root))
            return 0
        base = os.path.splitext(os.path.basename(path))[0]
        dest = os.path.join(out_root, base + "_Unpacked")
        if args.rewrite and os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        _dest, written, skipped = unpack_pak(path, out_root)
        print("unpacked %s -> %s (%d files, %d skipped)"
              % (path, _dest, written, skipped))
        return 0

    # ---- folder mode: plan first -----------------------------------------
    if not os.path.isdir(path):
        print("not a .pak or a folder: %s" % path, file=sys.stderr)
        return 2
    try:
        info = plan_game_unpack(
            path, os.path.abspath(args.out) if args.out else None)
    except Exception as e:
        print("cannot resolve data root for %s: %s" % (path, e),
              file=sys.stderr)
        return 2

    out_root = info["out_root"]
    print("=" * 70)
    print("CrisTical — standalone unpack")
    print("  root:   %s" % info["root"])
    print("  out:    %s" % out_root)
    print("  paks:   %d   total: %s"
          % (info["pak_count"], _mb(info["total_bytes"])))
    print("-" * 70)
    for name, size in info["paks"]:
        print("  %-28s %10s" % (name, _mb(size)))
    print("-" * 70)

    todo = []
    skipped_done = []
    for name, size in info["paks"]:
        dest = os.path.join(out_root, os.path.splitext(name)[0] + "_Unpacked")
        pak_path = os.path.join(info["root"], name)
        if args.rewrite:
            print("  [plan] %-24s rewrite (delete %s)" % (name, dest))
            todo.append((name, size, dest))
            continue
        if os.path.isdir(dest):
            ok, reason = verify_unpacked(pak_path, dest)
            if ok:
                print("  [plan] %-24s verified — skip" % name)
                skipped_done.append(name)
                continue
            print("  [plan] %-24s NOT unpacked (%s) — will unpack" % (name, reason))
            todo.append((name, size, dest))
        else:
            todo.append((name, size, dest))
    print("  to unpack: %d of %d   already verified: %d"
          % (len(todo), info["pak_count"], len(skipped_done)))
    print("=" * 70)

    if args.dry_run:
        print("dry run — nothing written.")
        return 0

    os.makedirs(out_root, exist_ok=True)

    total_written = 0
    total_skipped = 0
    errors = []
    for idx, (name, size, dest) in enumerate(todo, 1):
        pak_path = os.path.join(info["root"], name)
        print("[%d/%d] %s (%s)" % (idx, len(todo), name, _mb(size)))
        if os.path.isdir(dest):  # -rewrite: remove the old extraction
            shutil.rmtree(dest, ignore_errors=True)
        try:
            _dest, written, skipped = unpack_pak(pak_path, out_root)
        except Exception as e:
            print("  ERROR %s: %s" % (name, e))
            errors.append((name, str(e)))
            continue
        total_written += written
        total_skipped += skipped

    print("=" * 70)
    print("CrisTical — unpack summary")
    print("  out:   %s" % out_root)
    print("  paks:  %d   written: %d   skipped entries: %d   errors: %d"
          % (len(todo), total_written, total_skipped, len(errors)))
    for name, err in errors:
        print("  %-28s ERROR: %s" % (name, err))
    print("Done.")
    return 1 if errors else 0


if __name__ == "__main__":
    rc = main()
    # Machine-readable completion marker (the MCP bridge polls the log for it).
    print("UNPACK_DONE rc=%d" % rc)
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(rc)