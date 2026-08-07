"""
inject_anim.py — DBA animation injector CLI for CrisTical pipeline
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0
"""

import argparse
import os
import re
import subprocess
import sys

from .crydba import read_dba
from .gltf_anim import GltfAnimationInjector


def parse_cal(cal_path):
    result = {}
    if not os.path.isfile(cal_path):
        return result
    with open(cal_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            m = re.match(r"^\$(\w+)\s*=\s*(.+)$", line)
            if m:
                result[m.group(1)] = m.group(2).strip()
                continue
            m = re.match(r"^(#\w+)\s*=\s*(.+)$", line)
            if m:
                result[m.group(1)] = m.group(2).strip()
    return result


def resolve_dba(dba_rel, gameroot):
    rel_norm = dba_rel.replace("\\", "/").lstrip("/")
    loose = os.path.join(gameroot, rel_norm.replace("/", os.sep))
    if os.path.isfile(loose):
        return loose

    pak = os.path.join(gameroot, "Animations.pak")
    if not os.path.isfile(pak):
        return None

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_temp", "dba_extract")
    os.makedirs(tmp, exist_ok=True)
    print("[inject] extracting %s from Animations.pak ..." % rel_norm)
    ret = subprocess.call(
        ["7z", "x", pak, rel_norm.replace("/", "\\"), "-o" + tmp, "-y"],
        stdout=subprocess.DEVNULL)
    if ret != 0:
        return None
    extracted = os.path.join(tmp, rel_norm.replace("/", os.sep))
    return extracted if os.path.isfile(extracted) else None


def main():
    ap = argparse.ArgumentParser(description="Inject Crysis 1 DBA animations into glTF")
    ap.add_argument("--gltf", required=True, help="path to .gltf from cgf-converter")
    ap.add_argument("--chr", help="path to source .chr (used to find .cal)")
    ap.add_argument("--dba", help="explicit path to .dba (skips .cal resolution)")
    ap.add_argument("--gameroot", help="game root (folder with Animations.pak)")
    ap.add_argument("--out", help="output .gltf path (default: overwrite input)")
    ap.add_argument("--anim", action="append", help="inject only this animation")

    args = ap.parse_args()

    dba_path = args.dba
    if not dba_path:
        if not args.chr:
            ap.error("either --dba or --chr is required")
        cal_path = os.path.splitext(args.chr)[0] + ".cal"
        cal = parse_cal(cal_path)
        dba_rel = cal.get("TracksDatabase") or cal.get("#filepath")
        if not dba_rel:
            print("[inject] no $TracksDatabase in %s" % cal_path)
            return 2
        if not dba_rel.lower().endswith(".dba"):
            dba_rel += ".dba" if os.path.splitext(dba_rel)[1] == "" else ""
        if not args.gameroot:
            ap.error("--gameroot is required to resolve $TracksDatabase")
        dba_path = resolve_dba(dba_rel, args.gameroot)
        if not dba_path:
            print("[inject] ERROR: cannot resolve dba '%s'" % dba_rel)
            return 2

    print("[inject] dba: %s" % dba_path)
    dba = read_dba(dba_path)
    print("[inject] parsed: %d animations, %d/%d/%d tracks" % (
        len(dba.animations), len(dba.key_times), len(dba.key_pos), len(dba.key_rot)))

    injector = GltfAnimationInjector(args.gltf)
    name_filter = None
    if args.anim:
        name_filter = set(a.lower() for a in args.anim)
    n = injector.inject(dba, name_filter=name_filter)
    out = injector.save(args.out)
    print("[inject] done: %d animations injected -> %s" % (n, out))
    return 0 if n > 0 else 3


if __name__ == "__main__":
    sys.exit(main())
