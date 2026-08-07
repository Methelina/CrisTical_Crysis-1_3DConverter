#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cdf2gltf.py — CrisTical Crysis CDF -> animated glTF 2.0 Orchestrator
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 2.1

Always starts from .cdf (Character Definition File). The .chr is resolved
from the CDF's <Model File="..."> entry. Attachments are merged automatically.

=== CLI mode ===
  python cdf2gltf.py --cdf model.cdf --gamedir "F:\Games\Crysis\Game"
  python cdf2gltf.py --cdf model.cdf --gamedir "F:\Games\Crysis\Game" --split-anim
  python cdf2gltf.py --cdf model.cdf --gamedir "F:\Games\Crysis\Game" -o out.gltf --no-anim

=== Interactive mode ===
  python cdf2gltf.py   (no args)
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cristical_core import (
    read_chr_or_cdf, read_cdf, export_gltf,
    read_dba, has_tcb_controllers, read_dba_version,
    GltfAnimationInjector, convert_materials,
)

import argparse
import json
import re
import shutil
import struct
import subprocess

_PROJ_TEMP = os.path.join(os.path.dirname(SCRIPT_DIR), "temp")


def _clean_temp():
    if os.path.isdir(_PROJ_TEMP):
        try:
            shutil.rmtree(_PROJ_TEMP)
        except OSError:
            pass
    os.makedirs(_PROJ_TEMP, exist_ok=True)


_clean_temp()


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
    return result


def resolve_dba(dba_rel, game_dirs):
    candidates = []
    for d in game_dirs:
        rel = dba_rel.replace("\\", "/").lstrip("/")
        loose = os.path.join(d, rel.replace("/", os.sep))
        if os.path.isfile(loose):
            candidates.append(loose)
    for path in candidates:
        try:
            dba = read_dba(path)
            if dba and dba.animations:
                return path
        except Exception:
            continue
    if game_dirs:
        pak = os.path.join(game_dirs[0], "Animations.pak")
        if os.path.isfile(pak):
            tmp = os.path.join(_PROJ_TEMP, "dba_extract")
            os.makedirs(tmp, exist_ok=True)
            rel_win = dba_rel.replace("/", "\\")
            try:
                subprocess.call(["7z", "x", pak, rel_win, "-o" + tmp, "-y"], stdout=subprocess.DEVNULL)
            except Exception:
                pass
            extracted = os.path.join(tmp, dba_rel.replace("/", os.sep))
            if os.path.isfile(extracted):
                return extracted
    return None


def _resolve_mtl(chr_path, game_dirs):
    mtl = os.path.splitext(chr_path)[0] + ".mtl"
    if os.path.isfile(mtl):
        return mtl
    chr_base = os.path.splitext(os.path.basename(chr_path))[0].lower()
    for gd in game_dirs:
        for root, dirs, files in os.walk(gd):
            for f in files:
                if f.lower().endswith(".mtl"):
                    if os.path.splitext(f)[0].lower() == chr_base:
                        return os.path.join(root, f)
    chr_dir = os.path.dirname(chr_path)
    if os.path.isdir(chr_dir):
        candidates = [f for f in os.listdir(chr_dir) if f.lower().endswith(".mtl")]
        if candidates:
            return os.path.join(chr_dir, candidates[0])
    return mtl


def _inject_all(gltf, buf, dba, out_dir):
    tmp_g = os.path.join(out_dir, "_tmp_anim.gltf")
    tmp_b = tmp_g.replace(".gltf", ".bin")
    gltf["buffers"][0]["uri"] = os.path.basename(tmp_b)
    gltf["buffers"][0]["byteLength"] = len(buf)
    with open(tmp_b, "wb") as f:
        f.write(bytes(buf))
    with open(tmp_g, "w") as f:
        json.dump(gltf, f, separators=(",", ":"))
    injector = GltfAnimationInjector(tmp_g)
    n = injector.inject(dba, progress=lambda msg: print("   " + msg))
    injector.save()
    with open(tmp_g, "r") as f:
        updated = json.load(f)
    with open(tmp_b, "rb") as f:
        updated_buf = bytearray(f.read())
    os.remove(tmp_g)
    os.remove(tmp_b)
    return updated, updated_buf, n


def _split_anims(gltf, buf, dba, out_dir, chr_name):
    anim_dir = os.path.join(out_dir, chr_name + "_anims")
    os.makedirs(anim_dir, exist_ok=True)
    total = 0

    for a in dba.animations:
        short = os.path.splitext(os.path.basename(a.name))[0]
        safe = short.replace("/", "_").replace("\\", "_").replace(":", "_")
        gname = "%s_Anim_%s" % (chr_name, safe)
        ag = os.path.join(anim_dir, gname + ".gltf")
        ab = ag.replace(".gltf", ".bin")

        tmp_g = os.path.join(out_dir, "_split_tmp.gltf")
        tmp_b = tmp_g.replace(".gltf", ".bin")
        gltf["buffers"][0]["uri"] = os.path.basename(tmp_b)
        gltf["buffers"][0]["byteLength"] = len(buf)
        with open(tmp_b, "wb") as f:
            f.write(bytes(buf))
        with open(tmp_g, "w") as f:
            json.dump(gltf, f, separators=(",", ":"))

        injector = GltfAnimationInjector(tmp_g)
        injector.inject(dba, name_filter={short.lower()}, progress=lambda _: None)
        injector.save()

        with open(tmp_g, "r") as f:
            ad = json.load(f)
        with open(tmp_b, "rb") as f:
            abuf = bytearray(f.read())
        ad["buffers"][0]["uri"] = os.path.basename(ab)
        ad["buffers"][0]["byteLength"] = len(abuf)

        with open(ab, "wb") as f:
            f.write(bytes(abuf))
        with open(ag, "w") as f:
            json.dump(ad, f, separators=(",", ":"))
        total += 1
        try:
            os.remove(tmp_g)
            os.remove(tmp_b)
        except OSError:
            pass

    if gltf.get("images"):
        for img in gltf["images"]:
            src = os.path.join(out_dir, os.path.basename(img["uri"]))
            dst = os.path.join(anim_dir, os.path.basename(img["uri"]))
            if os.path.isfile(src) and not os.path.isfile(dst):
                shutil.copy2(src, dst)

    print("   %d files -> %s" % (total, anim_dir))
    return total


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


def run_pipeline(input_path, game_dirs, out_gltf, do_anim=True, do_tex=True, split_anim=False, progress_cb=None, glb=False):
    import datetime
    log = []

    def L(text):
        log.append(text)
        if progress_cb:
            progress_cb(text)
        else:
            print(text)

    L("=" * 60)
    L("CrisTical v2.1 — Crysis CDF -> glTF Converter")
    L("  Authors: Soror L.'.L.'. aka Methelina")
    L("  Started: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L("  Input: %s" % os.path.abspath(input_path))
    L("=" * 60)

    chr_path = input_path
    if input_path.lower().endswith(".cdf"):
        cdf_info = read_cdf(input_path)
        L("  CDF model: %s" % cdf_info.get("model_path", "?"))
        for aname, apath in cdf_info.get("skin_attachments", []):
            L("  CDF attachment: %s <- %s" % (aname, apath))
        if cdf_info.get("model_path"):
            chr_path = cdf_info["model_path"]

    L("[1/3] Skeleton + mesh")
    data = read_chr_or_cdf(input_path)
    bones = data["skeleton"]
    mesh = data["mesh"]
    L("  bones=%d primitives=%d" % (len(bones), len(mesh["primitives"])))
    gltf, buf = export_gltf(bones, mesh)

    out_dir = os.path.dirname(out_gltf)
    os.makedirs(out_dir, exist_ok=True)

    if do_tex:
        L("[2/3] Materials + textures")
        mtl_path = _resolve_mtl(chr_path, game_dirs)
        L("  MTL: %s" % mtl_path)
        all_pngs = []
        all_materials = []
        loaded_mtls = set()

        if os.path.isfile(mtl_path):
            mats, pngs, mat_info, tex_sources = convert_materials(mtl_path, game_dirs, out_dir)
            loaded_mtls.add(os.path.normpath(mtl_path))
            if mats:
                for mi in mat_info:
                    L("  Material: %-25s Shininess=%-6s Diffuse=%s Specular=%s Shader=%s" % (
                        mi["name"], mi["shininess"], mi["diffuse"], mi["specular"], mi["shader"]))
                L("  Textures (%d generated):" % len(pngs))
                for png in sorted(set(pngs)):
                    src = tex_sources.get(png, "unknown")
                    L("    %s  <-  %s" % (png, src))
                all_materials.extend(mats)
                all_pngs.extend(pngs)

        for pi, prim in enumerate(mesh["primitives"]):
            att_name = prim.get("_cdf_attachment")
            if att_name:
                att_chr = prim.get("_cdf_chr_path")
                if att_chr:
                    att_mtl = os.path.normpath(_resolve_mtl(att_chr, game_dirs))
                    if os.path.isfile(att_mtl) and att_mtl not in loaded_mtls:
                        mats2, pngs2, _mi2, _ts2 = convert_materials(att_mtl, game_dirs, out_dir)
                        loaded_mtls.add(att_mtl)
                        if mats2:
                            all_materials.extend(mats2)
                            all_pngs.extend(pngs2)
                            L("  [mtl] %s -> %s (%d materials)" % (
                                att_name, os.path.basename(att_mtl), len(mats2)))

        if all_materials and all_pngs:
            png_files = sorted(set(os.path.basename(f) for f in all_pngs))
            gltf["images"] = [{"uri": f} for f in png_files]
            gltf.setdefault("samplers", [{}])
            gltf["samplers"][0] = {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}
            gltf["textures"] = [{"source": i, "sampler": 0} for i in range(len(png_files))]
            tex_idx = {f: i for i, f in enumerate(png_files)}

            gltf["materials"] = all_materials
            for m in gltf["materials"]:
                for slot in ("baseColorTexture", "normalTexture", "emissiveTexture"):
                    t = m.get("pbrMetallicRoughness", {}).get(slot) or m.get(slot)
                    if t and isinstance(t, dict) and "index" in t:
                        idx = t["index"]
                        if idx < len(all_pngs):
                            png_basename = os.path.basename(all_pngs[idx])
                            t["index"] = tex_idx.get(png_basename, idx)

            prims = gltf["meshes"][0]["primitives"]
            for pi, prim in enumerate(prims):
                mat_id = prim.pop("_mat_id", 0)
                prim["material"] = min(int(mat_id), len(all_materials) - 1) if len(all_materials) > 0 else 0
            L("  %d textures, %d materials" % (len(png_files), len(all_materials)))
        else:
            L("  no .mtl found, skipping")

    if do_anim:
        L("[3/3] Animations")
        cal_path = os.path.splitext(chr_path)[0] + ".cal"
        cal = parse_cal(cal_path)
        dba_rel = cal.get("TracksDatabase") or cal.get("#filepath")
        if dba_rel:
            if not dba_rel.lower().endswith(".dba"):
                dba_rel += ".dba" if not os.path.splitext(dba_rel)[1] else ""
            dba_path = resolve_dba(dba_rel, game_dirs)
            if dba_path:
                dba = read_dba(dba_path)
                if split_anim:
                    chr_name = os.path.splitext(os.path.basename(chr_path))[0]
                    n = _split_anims(gltf, buf, dba, out_dir, chr_name)
                else:
                    gltf, buf, n = _inject_all(gltf, buf, dba, out_dir)
                L("  DBA: %s (%d animations)" % (dba_path, n))
                L("  Animations:")
                for a in dba.animations:
                    L("    %s" % os.path.basename(a.name))
            else:
                L("  DBA not found: %s" % dba_rel)
        else:
            L("  no .cal file found, skipping animations")

    out_bin = out_gltf.replace(".gltf", ".bin")
    gltf["buffers"][0]["byteLength"] = len(buf)

    if glb:
        out_glb = out_gltf.replace(".gltf", ".glb")
        _write_glb(gltf, bytes(buf), out_glb)
        L("Output: %s (bones=%d prims=%d)" % (out_glb, len(bones), len(mesh["primitives"])))
        L("Done: %s" % out_glb)
    else:
        gltf["buffers"][0]["uri"] = os.path.basename(out_bin)
        with open(out_bin, "wb") as f:
            f.write(bytes(buf))
        with open(out_gltf, "w", encoding="utf-8") as f:
            json.dump(gltf, f, separators=(",", ":"))
        L("Output: %s + %s (%d bones, %d prims)" % (out_gltf, out_bin, len(bones), len(mesh["primitives"])))
        L("Done: %s" % out_gltf)

    log_path = out_gltf.replace(".gltf", ".log")
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write("\n".join(log))
    L("  Log: %s" % log_path)

    return log


def _interactive():
    print("\n=== CrisTical: Crysis CDF -> animated glTF ===\n")

    cdf_path = ""
    while not cdf_path or not os.path.isfile(cdf_path):
        cdf_path = input("Path to .cdf file: ").strip().strip('"')
        if not os.path.isfile(cdf_path):
            print("  File not found!")
        elif not cdf_path.lower().endswith(".cdf"):
            print("  Expected a .cdf file (Character Definition File)")
            cdf_path = ""
    print()

    game_dirs = ["F:\\Games\\Crysis_Remastered\\Game"]
    print("Game directories (Enter to keep default, multiple separated by ;):")
    custom = input("  [%s] : " % game_dirs[0]).strip().strip('"')
    if custom:
        game_dirs = [d.strip() for d in custom.split(";") if d.strip()]
    print()

    cdf_name = os.path.splitext(os.path.basename(cdf_path))[0]
    out_path = input("Output glTF path (Enter for auto = %s.gltf): " % cdf_name).strip().strip('"')
    if not out_path:
        out_path = os.path.join(os.path.dirname(cdf_path) or ".", cdf_name + ".gltf")
    print()

    print("--- Detecting model structure ---")
    data = read_chr_or_cdf(cdf_path)
    bones = data["skeleton"]
    mesh = data["mesh"]
    print("  Bones:      %d" % len(bones))
    print("  Primitives: %d" % len(mesh["primitives"]))

    mtl_count = 0
    mtl_path = os.path.splitext(cdf_path)[0] + ".mtl"
    if os.path.isfile(mtl_path):
        import xml.etree.ElementTree as ET2
        tree = ET2.parse(mtl_path)
        sub = tree.getroot().find("SubMaterials")
        mtl_count = len(sub.findall("Material")) if sub is not None else (1 if tree.getroot().tag == "Material" else 0)
        print("  Materials:  %d" % mtl_count)

    anim_count = 0
    cal_path = os.path.splitext(cdf_path)[0] + ".cal"
    dba_rel = ""
    if os.path.isfile(cal_path):
        cal = parse_cal(cal_path)
        dba_rel = cal.get("TracksDatabase", "")
        print("  Animations: detected in .cal")

    tcb = False
    if dba_rel:
        if not dba_rel.lower().endswith(".dba"):
            dba_rel += ".dba" if not os.path.splitext(dba_rel)[1] else ""
        dba_test = resolve_dba(dba_rel, game_dirs)
        if dba_test:
            tcb = has_tcb_controllers(dba_test)
            print("  TCB curves: %s" % ("YES" if tcb else "no"))

    print()
    print("--- Options ---")
    print("  Animation mode:")
    print("    0 — skip  1 — split  2 — single file")
    amode = input("    [2] : ").strip()
    do_anim = amode != "0"
    split_anim = (amode == "1")

    print("  Material mode:")
    print("    0 — skip  1 — auto-PBR")
    tmode = input("    [1] : ").strip()
    do_tex = tmode != "0"
    print()

    print("-" * 50)
    run_pipeline(cdf_path, game_dirs, out_path, do_anim, do_tex, split_anim)


def _cli():
    ap = argparse.ArgumentParser(description="CrisTical: Crysis CDF -> animated glTF")
    ap.add_argument("--cdf", help="path to .cdf file")
    ap.add_argument("--gamedir", "-g", action="append", default=[], help="game root (repeatable)")
    ap.add_argument("--out", "-o", help="output .gltf path")
    ap.add_argument("--no-anim", action="store_true", help="skip animations")
    ap.add_argument("--no-tex", action="store_true", help="skip textures")
    ap.add_argument("--split-anim", action="store_true", help="one glTF per animation")
    ap.add_argument("--glb", action="store_true", help="output as binary .glb instead of .gltf+.bin")
    args = ap.parse_args()

    if not args.cdf:
        ap.error("--cdf is required; run without args for interactive mode")
    if not os.path.isfile(args.cdf):
        ap.error("file not found: %s" % args.cdf)

    cdf_name = os.path.splitext(os.path.basename(args.cdf))[0]
    out = args.out or os.path.join(os.path.dirname(args.cdf) or ".", cdf_name + ".gltf")
    run_pipeline(args.cdf, args.gamedir, out,
                 do_anim=not args.no_anim, do_tex=not args.no_tex,
                 split_anim=args.split_anim, glb=args.glb)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        _interactive()
