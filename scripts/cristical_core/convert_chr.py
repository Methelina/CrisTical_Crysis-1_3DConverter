#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
convert_chr.py — Crysis 1 .chr -> glTF 2.0 converter
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0
"""

import argparse
import json
import os
import sys

from .crychr import read_chr
from .crygltf import export_gltf


def main_cli():
    ap = argparse.ArgumentParser(description="Crysis 1 .chr -> glTF 2.0 converter")
    ap.add_argument("input", help="path to .chr or .cgf file")
    ap.add_argument("--out", "-o", help="output .gltf path (default: input.gltf)")
    args = ap.parse_args()

    if not os.path.isfile(args.input):
        print("ERROR: file not found: %s" % args.input, file=sys.stderr)
        return 2
    out = args.out or os.path.splitext(args.input)[0] + ".gltf"
    out_bin = out.replace(".gltf", ".bin")

    print("[convert] reading %s ..." % args.input)
    data = read_chr(args.input)
    bones = data["skeleton"]
    mesh = data["mesh"]
    print("[convert] bones: %d, primitives: %d" % (len(bones), len(mesh["primitives"])))

    gltf, buf = export_gltf(bones, mesh)

    gltf["buffers"][0]["uri"] = os.path.basename(out_bin)

    with open(out_bin, "wb") as f:
        f.write(bytes(buf))

    with open(out, "w", encoding="utf-8") as f:
        json.dump(gltf, f, separators=(",", ":"))

    print("[convert] written: %s + %s" % (out, out_bin))
    return 0


if __name__ == "__main__":
    sys.exit(main_cli())
