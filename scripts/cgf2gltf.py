#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cgf2gltf.py — CrisTical: static Crysis .cgf -> glTF 2.0 converter
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0

Converts static compiled CGF geometry (vegetation, props, buildings —
anything without a skeleton) to glTF 2.0, preserving:

  - vertex colors  -> COLOR_0 (RGBA 0..1 floats; 8-bit source values)
  - tangents       -> TANGENT  (VEC4, unpacked from int16 packed tangents)
  - UVs, normals, indices, node hierarchy baked into world space
  - .mtl materials -> PBR glTF materials (diffuse/normal/specular + DDS->PNG)

Chunk layout determined by independent analysis of sample files:
Mesh 0xCCCC0000, Node 0xCCCC000B, MtlName 0xCCCC0014,
DataStream 0xCCCC0016, MeshSubsets 0xCCCC0017.

=== CLI ===
  python cgf2gltf.py --cgf palm.cgf --gamedir "F:\Games\Crysis_Remastered\Game"
  python cgf2gltf.py --cgf bush.cgf --gamedir "F:\Games\Crysis\Game" -o out.gltf --no-tex
  python cgf2gltf.py --cgf tree.cgf --gamedir "..." --glb

=== Interactive ===
  python cgf2gltf.py   (no args)
"""

import argparse
import json
import os
import shutil
import struct
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cristical_core import read_cgf, read_cgf_meshes, export_gltf_static, convert_materials
from cristical_core.mtl_resolve import resolve_mtl
from cristical_core.path_resolve import resolve_geometry_path

_PROJ_TEMP = os.path.join(os.path.dirname(SCRIPT_DIR), "temp")


def _clean_temp():
    if os.path.isdir(_PROJ_TEMP):
        try:
            shutil.rmtree(_PROJ_TEMP)
        except OSError:
            pass
    os.makedirs(_PROJ_TEMP, exist_ok=True)


_clean_temp()


def _mtl_submaterial_names(mtl_path):
    """Parse .mtl and return set of sub-material names (shared helper)."""
    from cristical_core.mtl_resolve import submaterial_names
    return submaterial_names(mtl_path)


def _resolve_mtl(cgf_path, game_dirs, prim_materials=None, verbose=True, log=None):
    """Find the best .mtl for a static CGF.

    Delegates to the shared resolver in cristical_core.mtl_resolve with the
    LOD/size suffix stripping enabled (CGF variants like palm_tree_large_a).
    """
    return resolve_mtl(cgf_path, game_dirs, prim_materials=prim_materials,
                       strip_suffixes=True, verbose=verbose, log=log or print)


def _write_glb(gltf, bin_bytes, out_path):
    json_str = json.dumps(gltf, separators=(",", ":"))
    json_bytes = json_str.encode("utf-8")
    while len(json_bytes) % 4:
        json_bytes += b" "
    bin_bytes = bytes(bin_bytes)
    while len(bin_bytes) % 4:
        bin_bytes += b"\x00"
    total_len = 12 + 8 + len(json_bytes) + 8 + len(bin_bytes)
    header = struct.pack("<III", 0x46546C67, 2, total_len)
    chunk_json = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    chunk_bin = struct.pack("<II", len(bin_bytes), 0x004E4942) + bin_bytes
    with open(out_path, "wb") as f:
        f.write(header + chunk_json + chunk_bin)


def run_pipeline(input_path, game_dirs, out_gltf, do_tex=True, progress_cb=None, glb=False):
    import datetime
    log = []

    def L(text):
        log.append(text)
        if progress_cb:
            progress_cb(text)
        else:
            print(text)

    L("=" * 60)
    L("CrisTical — Crysis static .cgf -> glTF Converter")
    L("  Authors: Soror L.'.L.'. aka Methelina")
    L("  Started: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L("  Input: %s" % os.path.abspath(input_path))
    L("=" * 60)

    L("[1/3] Static mesh")
    prims = read_cgf_meshes(input_path)
    if not prims:
        L("  ERROR: no mesh primitives found")
        return log
    L("  primitives=%d  total_verts=%d  total_idx=%d" % (
        len(prims), sum(len(p["positions"]) for p in prims),
        sum(len(p["indices"]) for p in prims)))
    for p in prims:
        L("    %-24s mat=%-24s verts=%d col=%s" % (
            p["node_name"], p["material"], len(p["positions"]),
            "yes" if p["colors"] else "no"))

    gltf, buf = export_gltf_static(prims)

    out_dir = os.path.dirname(out_gltf)
    os.makedirs(out_dir, exist_ok=True)

    if do_tex:
        L("[2/3] Materials + textures")
        prim_materials = [p.get("material") for p in prims]
        mtl_path = _resolve_mtl(input_path, game_dirs, prim_materials)
        L("  MTL: %s" % mtl_path)
        if os.path.isfile(mtl_path):
            mats, pngs, mat_info, tex_sources, xml_to_mat = convert_materials(mtl_path, game_dirs, out_dir)
            if mats:
                L("  Materials (%d):" % len(mat_info))
                for mi in mat_info:
                    L("    %-25s Shininess=%-6s Shader=%s" % (
                        mi["name"], mi["shininess"], mi["shader"]))
                L("  Textures (%d generated):" % len(pngs))
                for png in sorted(set(pngs)):
                    src = tex_sources.get(png, "unknown")
                    L("    %s  <-  %s" % (png, src))
            else:
                L("  no materials parsed from .mtl, skipping textures")

            if mats and pngs:
                png_files = sorted(set(os.path.basename(f) for f in pngs))
                gltf["images"] = [{"uri": f} for f in png_files]
                gltf.setdefault("samplers", [{}])
                gltf["samplers"][0] = {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}
                gltf["textures"] = [{"source": i, "sampler": 0} for i in range(len(png_files))]
                tex_idx = {f: i for i, f in enumerate(png_files)}

                gltf["materials"] = mats
                for m in gltf["materials"]:
                    for slot in ("baseColorTexture", "normalTexture", "emissiveTexture"):
                        t = m.get("pbrMetallicRoughness", {}).get(slot) or m.get(slot)
                        if t and isinstance(t, dict) and "index" in t:
                            idx = t["index"]
                            if idx < len(pngs):
                                png_basename = os.path.basename(pngs[idx])
                                t["index"] = tex_idx.get(png_basename, idx)

                prims_out = gltf["meshes"][0]["primitives"]
                for prim in prims_out:
                    mat_name = prim.pop("_mat_name", "material")
                    mat_id = prim.pop("_mat_id", -1)
                    # 1) exact name match
                    idx_m = next((i for i, m in enumerate(mats) if m.get("name") == mat_name), None)
                    # 2) normalized name match (case/space-insensitive)
                    if idx_m is None:
                        norm = lambda s: "".join(ch.lower() for ch in s if ch.isalnum())
                        tn = norm(mat_name)
                        idx_m = next((i for i, m in enumerate(mats) if norm(m.get("name", "")) == tn), None)
                    # 3) prefix match (e.g. CGF "branch" vs .mtl "branches")
                    if idx_m is None:
                        norm = lambda s: "".join(ch.lower() for ch in s if ch.isalnum())
                        tn = norm(mat_name)
                        idx_m = next(
                            (i for i, m in enumerate(mats)
                             if (norm(m.get("name", "")) or "").startswith(tn)
                             or tn.startswith(norm(m.get("name", "")))), None)
                    # 4) fall back through the Nodraw-aware mat_id mapping
                    #    (mat_id is the .mtl XML sub-material index; xml_to_mat
                    #    translates it to the non-Nodraw materials list)
                    if idx_m is None:
                        mapped = xml_to_mat.get(mat_id)
                        if mapped is not None:
                            idx_m = mapped
                    prim["material"] = idx_m if idx_m is not None else 0
                L("  %d textures, %d materials" % (len(png_files), len(mats)))
        else:
            L("  no .mtl found, skipping textures")

    L("[3/3] Write output")
    out_bin = out_gltf.replace(".gltf", ".bin")
    gltf["buffers"][0]["byteLength"] = len(buf)

    if glb:
        out_glb = out_gltf.replace(".gltf", ".glb")
        _write_glb(gltf, bytes(buf), out_glb)
        L("  Output: %s" % out_glb)
    else:
        gltf["buffers"][0]["uri"] = os.path.basename(out_bin)
        with open(out_bin, "wb") as f:
            f.write(bytes(buf))
        with open(out_gltf, "w", encoding="utf-8") as f:
            json.dump(gltf, f, separators=(",", ":"))
        L("  Output: %s + %s" % (out_gltf, out_bin))

    log_path = out_gltf.replace(".gltf", ".log")
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write("\n".join(log))
    L("  Log: %s" % log_path)

    return log


def _interactive():
    print("\n=== CrisTical: Crysis static .cgf -> glTF ===\n")

    cgf_path = ""
    game_dirs: list = []
    while not cgf_path:
        cgf_path = input("Path to .cgf file: ").strip().strip('"')
        if not cgf_path:
            continue
        if not cgf_path.lower().endswith(".cgf"):
            print("  Expected a .cgf file (static geometry)")
            cgf_path = ""
            continue
        if not os.path.isfile(cgf_path):
            # Virtual path inside a pak? Ask for game dirs and try to
            # materialize it before rejecting the input.
            gd = input("  Not on disk — game dir to resolve from "
                       "(Enter to abort): ").strip().strip('"')
            if not gd:
                cgf_path = ""
                continue
            game_dirs = [d for d in (x.strip() for x in gd.split(";")) if d]
            try:
                cgf_path = resolve_geometry_path(cgf_path, game_dirs)
                print("  Virtual path materialized.")
            except FileNotFoundError as e:
                print("  %s" % e)
                cgf_path = ""
    print()

    if not game_dirs:
        game_dirs = ["F:\\Games\\Crysis_Remastered\\Game"]
        print("Game directories (Enter to keep default, multiple separated by ;):")
        custom = input("  [%s] : " % game_dirs[0]).strip().strip('"')
        if custom:
            game_dirs = [d.strip() for d in custom.split(";") if d.strip()]
    print()

    cgf_name = os.path.splitext(os.path.basename(cgf_path))[0]
    out_path = input("Output glTF path (Enter for auto = %s.gltf): " % cgf_name).strip().strip('"')
    if not out_path:
        out_path = os.path.join(os.path.dirname(cgf_path) or ".", cgf_name + ".gltf")
    print()

    print("--- Detecting model structure ---")
    data = read_cgf(cgf_path)
    print("  Materials: %d" % len(data["materials"]))
    print("  Mesh chunks: %d" % len(data["mesh_chunks"]))
    print("  Nodes: %d" % len(data["nodes"]))
    print()

    print("  Material mode:")
    print("    0 — skip  1 — auto-PBR")
    tmode = input("    [1] : ").strip()
    do_tex = tmode != "0"
    print()

    print("-" * 50)
    run_pipeline(cgf_path, game_dirs, out_path, do_tex=do_tex)


def _cli():
    ap = argparse.ArgumentParser(description="CrisTical: Crysis static .cgf -> glTF")
    ap.add_argument("--cgf", help="path to static .cgf file")
    ap.add_argument("--gamedir", "-g", action="append", default=[], help="game root (repeatable)")
    ap.add_argument("--out", "-o", help="output .gltf path")
    ap.add_argument("--no-tex", action="store_true", help="skip textures/materials")
    ap.add_argument("--glb", action="store_true", help="output as binary .glb instead of .gltf+.bin")
    args = ap.parse_args()

    if not args.cgf:
        ap.error("--cgf is required; run without args for interactive mode")
    try:
        cgf_real = resolve_geometry_path(args.cgf, args.gamedir)
    except FileNotFoundError as e:
        ap.error(str(e))
    if cgf_real != args.cgf:
        print("[cgf2gltf] virtual path materialized: %s -> %s" % (args.cgf, cgf_real))

    cgf_name = os.path.splitext(os.path.basename(args.cgf))[0]
    out = args.out or os.path.join(os.path.dirname(args.cgf) or ".", cgf_name + ".gltf")
    run_pipeline(cgf_real, args.gamedir, out,
                 do_tex=not args.no_tex, glb=args.glb)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        _interactive()
