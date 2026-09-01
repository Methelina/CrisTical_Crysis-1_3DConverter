#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cga2gltf.py --- CrisTical Crysis animated .cga -> animated glTF 2.0 Orchestrator
Authors: Soror L.'.L'. aka Methelina    Project: CrisTical
Version: 1.0

Converts a .cga (animated geometry) file — Crysis binary chunk format 0x0745 — to
glTF 2.0, preserving:

  - node hierarchy       (parent/child tree, topologically ordered)
  - local transforms     (translation + rotation, axis-swapped Z-up -> Y-up)
  - mesh primitives      (positions/normals/uvs/indices, axis-swapped)
  - materials            (minimal PBR stubs or full .mtl -> PNG conversion)
  - animations           (sibling .anm files -> DBA -> glTF animation channels)

Unlike cgf2gltf (static, hierarchy baked to world space) or cdf2gltf (skeletal
skin via .chr/.dba), CGA nodes carry their own local transforms and mesh
references, so the glTF node tree is built directly from read_cga output.

=== CLI mode ===
  python cga2gltf.py --cga model.cga --gamedir "F:\\Games\\Crysis_Remastered\\Game"
  python cga2gltf.py --cga model.cga --gamedir "..." -o out.gltf --no-anim --no-tex

=== Interactive mode ===
  python cga2gltf.py   (no args)
"""

import os
import sys
import json
import struct
import zlib
import types
import argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cristical_core import (
    read_cga, read_anm, anm_to_dba,
    GltfAnimationInjector, convert_materials,
)
from cristical_core.mtl_resolve import resolve_mtl
from cristical_core.crygltf import AXIS_SWAP_POS, AXIS_SWAP_QUAT, AXIS_SWAP_NRM

_COMPONENT_FLOAT = 5126     # FLOAT
_COMPONENT_USHORT = 5123    # UNSIGNED_SHORT


# ---------------------------------------------------------------------------
# Small helpers (mirrors crygltf._is_identity_rot / _is_zero_vec)
# ---------------------------------------------------------------------------

def _is_identity_rot(q):
    """True when quaternion is the identity (0, 0, 0, 1)."""
    return (abs(q[0]) <= 1e-6 and abs(q[1]) <= 1e-6
            and abs(q[2]) <= 1e-6 and abs(q[3] - 1.0) <= 1e-6)


def _is_zero_vec(v):
    """True when all vector components are ~0."""
    return all(abs(x) <= 1e-6 for x in v)


# ---------------------------------------------------------------------------
# glTF builder
# ---------------------------------------------------------------------------

def build_gltf_from_cga(cga_data):
    """Build a minimal valid glTF 2.0 dict + binary buffer from CGA data.

    Args:
        cga_data: dict returned by :func:`cristical_core.read_cga` with keys
            ``nodes`` (list of node dicts) and ``mesh`` (list of primitive
            dicts).

    Returns:
        ``(gltf, bin_bytes)`` where *gltf* is the glTF JSON dict and
        *bin_bytes* is a ``bytearray`` holding all accessor payload.

    The node hierarchy is preserved: parents appear before children in
    ``gltf["nodes"]`` (topological order), each node carries an optional
    ``translation`` / ``rotation`` (axis-swapped), ``children`` links, and a
    ``mesh`` reference when the node owns geometry.  Each CGA primitive becomes
    its own ``gltf["meshes"]`` entry with a single ``primitive``.
    """
    nodes_in = cga_data["nodes"]
    prims = cga_data["mesh"]

    buf = bytearray()
    accessors = []
    buffer_views = []
    gltf_meshes = []
    materials = []
    mat_by_name = {}

    # --- minimal materials (one per unique primitive material string) ---
    for p in prims:
        mat_name = p.get("material") or "material"
        if mat_name not in mat_by_name:
            mat_by_name[mat_name] = len(materials)
            materials.append({
                "name": mat_name,
                "pbrMetallicRoughness": {"baseColorFactor": [0.8, 0.8, 0.8, 1.0]},
            })

    # --- buffer / accessor helpers (style matches crygltf.export_gltf_static) ---

    def append_buf(payload):
        while len(buf) % 4:
            buf.append(0)
        off = len(buf)
        buf.extend(payload)
        vi = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": len(payload)})
        return vi

    def add_accessor(view_idx, count, acc_type, comp_type=_COMPONENT_FLOAT, byte_off=0):
        acc = {
            "bufferView": view_idx,
            "byteOffset": byte_off,
            "componentType": comp_type,
            "count": count,
            "type": acc_type,
        }
        accessors.append(acc)
        return len(accessors) - 1

    # --- one glTF mesh per CGA primitive ---
    mesh_for_node = {}          # node_id -> first glTF mesh index
    for p in prims:
        pos_raw = []
        nrm_raw = []
        uv_raw = []
        idx_raw = []

        for v in p["positions"]:
            pos_raw.extend(AXIS_SWAP_POS(v))
        for n in (p["normals"] or []):
            nrm_raw.extend(AXIS_SWAP_NRM(n))
        for uv in (p["uvs"] or []):
            uv_raw.extend(uv)
        for vi in p["indices"]:
            idx_raw.append(vi)

        nv = len(p["positions"])
        ni = len(p["indices"])

        attrs = {}

        # POSITION (with min/max)
        pv = append_buf(struct.pack("<%df" % len(pos_raw), *pos_raw))
        pa = add_accessor(pv, nv, "VEC3")
        if pos_raw:
            xs = pos_raw[0::3]
            ys = pos_raw[1::3]
            zs = pos_raw[2::3]
            accessors[pa]["min"] = [min(xs), min(ys), min(zs)]
            accessors[pa]["max"] = [max(xs), max(ys), max(zs)]
        attrs["POSITION"] = pa

        # NORMAL
        if nrm_raw:
            nvb = append_buf(struct.pack("<%df" % len(nrm_raw), *nrm_raw))
            attrs["NORMAL"] = add_accessor(nvb, nv, "VEC3")

        # TEXCOORD_0
        if uv_raw:
            uvb = append_buf(struct.pack("<%df" % len(uv_raw), *uv_raw))
            attrs["TEXCOORD_0"] = add_accessor(uvb, nv, "VEC2")

        # indices — use USHORT (crygltf style), fall back to UINT if too large
        if idx_raw and max(idx_raw) > 65535:
            ivb = append_buf(struct.pack("<%dI" % len(idx_raw), *idx_raw))
            ia = add_accessor(ivb, ni, "SCALAR", comp_type=5125)
        else:
            ivb = append_buf(struct.pack("<%dH" % len(idx_raw), *idx_raw))
            ia = add_accessor(ivb, ni, "SCALAR", comp_type=_COMPONENT_USHORT)

        mat_idx = mat_by_name.get(p.get("material") or "material", 0)

        gltf_meshes.append({
            "name": p.get("node_name", "mesh_%d" % len(gltf_meshes)),
            "primitives": [{
                "attributes": attrs,
                "indices": ia,
                "material": mat_idx,
                "mode": 4,
                "_mat_name": p.get("material") or "material",
                "_mat_id": p.get("mat_id", 0),
            }],
        })

        nid = p.get("node_id")
        if nid is not None and nid not in mesh_for_node:
            mesh_for_node[nid] = len(gltf_meshes) - 1

    # --- topological sort: parents before children ---
    node_id_to_idx = {}
    for i, n in enumerate(nodes_in):
        node_id_to_idx[n["node_id"]] = i

    ordered = []
    visited = set()
    node_order = {}

    def _visit(idx):
        if idx in visited:
            return
        visited.add(idx)
        pid = nodes_in[idx]["parent_id"]
        if pid is not None and pid in node_id_to_idx:
            _visit(node_id_to_idx[pid])
        node_order[idx] = len(ordered)
        ordered.append(idx)

    for i in range(len(nodes_in)):
        _visit(i)

    # --- build glTF node list (topologically ordered) ---
    gltf_nodes = []
    root_indices = []
    for ni in ordered:
        src = nodes_in[ni]
        node = {"name": src["name"]}
        t = AXIS_SWAP_POS(src["pos"])
        if not _is_zero_vec(t):
            node["translation"] = list(t)
        q = AXIS_SWAP_QUAT(src["rot_quat"])
        if not _is_identity_rot(q):
            node["rotation"] = list(q)
        gltf_nodes.append(node)

    # --- children / roots / mesh refs ---
    for ni in ordered:
        src = nodes_in[ni]
        oi = node_order[ni]
        pid = src["parent_id"]
        if pid is not None and pid in node_id_to_idx:
            p_oi = node_order[node_id_to_idx[pid]]
            gltf_nodes[p_oi].setdefault("children", []).append(oi)
        else:
            root_indices.append(oi)

        nid = src["node_id"]
        if nid in mesh_for_node:
            gltf_nodes[oi]["mesh"] = mesh_for_node[nid]

    gltf = {
        "asset": {"version": "2.0", "generator": "CrisTical cga2gltf.py 1.0"},
        "scene": 0,
        "scenes": [{"nodes": root_indices}],
        "nodes": gltf_nodes,
        "meshes": gltf_meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(buf)}],
    }

    return gltf, buf


# ---------------------------------------------------------------------------
# DBA merge (multiple .anm files -> single DBA to avoid losing animations
# because GltfAnimationInjector.inject() resets gltf["animations"] = [])
# ---------------------------------------------------------------------------

def _merge_dba(dba_list):
    """Merge several DBA-like objects produced by :func:`anm_to_dba`.

    Track indices (``rot_t``, ``rot_kt``, ``pos_t``, ``pos_kt``) are offset
    so they index correctly into the concatenated ``key_times`` /
    ``key_pos`` / ``key_rot`` lists.
    """
    merged = types.SimpleNamespace()
    merged.key_times = []
    merged.key_pos = []
    merged.key_rot = []
    merged.animations = []

    for d in dba_list:
        kt_off = len(merged.key_times)
        kp_off = len(merged.key_pos)
        kr_off = len(merged.key_rot)

        for anim in d.animations:
            new_anim = types.SimpleNamespace()
            new_anim.name = anim.name
            new_anim.secs_per_tick = anim.secs_per_tick
            new_anim.controllers = []
            for ctrl in anim.controllers:
                nc = types.SimpleNamespace()
                nc.controller_id = ctrl.controller_id
                nc.has_rot = ctrl.has_rot
                nc.has_pos = ctrl.has_pos
                if ctrl.has_rot:
                    nc.rot_t = ctrl.rot_t + kr_off
                    nc.rot_kt = ctrl.rot_kt + kt_off
                else:
                    nc.rot_t = -1
                    nc.rot_kt = -1
                if ctrl.has_pos:
                    nc.pos_t = ctrl.pos_t + kp_off
                    nc.pos_kt = ctrl.pos_kt + kt_off
                else:
                    nc.pos_t = -1
                    nc.pos_kt = -1
                new_anim.controllers.append(nc)
            merged.animations.append(new_anim)

        merged.key_times.extend(d.key_times)
        merged.key_pos.extend(d.key_pos)
        merged.key_rot.extend(d.key_rot)

    return merged


def _inject_animations(gltf, buf, dba, out_dir):
    """Round-trip glTF+bin through GltfAnimationInjector.

    Writes a temporary ``.gltf``/``.bin`` pair, injects *dba*, reads back the
    updated documents and removes the temporaries.

    Returns ``(updated_gltf, updated_buf, num_injected)``.
    """
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


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run_pipeline(input_path, game_dirs, out_gltf, do_anim=True, do_tex=True, progress_cb=None):
    """Convert a single ``.cga`` file (with sibling ``.anm`` files) to glTF 2.0.

    Args:
        input_path:   path to the ``.cga`` file.
        game_dirs:    list of game-root directories for material/texture lookup.
        out_gltf:     output ``.gltf`` path (the ``.bin`` / ``.log`` siblings
                      are derived by replacing the extension).
        do_anim:      if True, search for sibling ``.anm`` files and inject
                      their animations.
        do_tex:       if True, resolve and convert ``.mtl`` materials.
        progress_cb:  optional callable receiving every log line.

    Returns:
        List of log strings (also written to ``<out_gltf_basename>.log``).
    """
    import datetime
    log = []

    def L(text):
        log.append(text)
        if progress_cb:
            progress_cb(text)
        else:
            print(text)

    L("=" * 60)
    L("CrisTical v1.0 — Crysis CGA -> glTF 2.0 Converter")
    L("  Authors: Soror L.'.L'. aka Methelina")
    L("  Started: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L("  Input: %s" % os.path.abspath(input_path))
    L("=" * 60)

    # ------------------------------------------------------------------
    # [1/3] Skeleton + mesh
    # ------------------------------------------------------------------
    L("[1/3] Skeleton + mesh")
    data = read_cga(input_path)
    L("  nodes=%d primitives=%d" % (data["num_nodes"], data["num_prims"]))
    for n in data["nodes"]:
        pstr = str(n["parent_id"]) if n["parent_id"] is not None else "(root)"
        L("    %-32s id=%-6d parent=%-8s type=%s" % (
            n["name"][:32], n["node_id"], pstr, n["controller_type"] or "-"))
    gltf, buf = build_gltf_from_cga(data)

    out_dir = os.path.dirname(out_gltf)
    os.makedirs(out_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # [2/3] Animations
    # ------------------------------------------------------------------
    if do_anim:
        L("[2/3] Animations")
        cga_base = os.path.splitext(os.path.basename(input_path))[0]
        cga_dir = os.path.dirname(os.path.abspath(input_path))
        anm_files = []
        if os.path.isdir(cga_dir):
            for f in os.listdir(cga_dir):
                if f.lower().endswith(".anm") and f.lower().startswith(cga_base.lower()):
                    anm_files.append(os.path.join(cga_dir, f))
        anm_files.sort()

        # Map CGA controller chunk-ids to crc32(node_name) so that the
        # controller_id produced by anm_to_dba (which is the raw TCB chunk id)
        # resolves to the matching glTF node inside GltfAnimationInjector.
        # Mirror gltf_anim._collect_joint_nodes exactly (no lowercasing, all
        # underscore/space name variants registered) so the crc always hits.
        chunk_id_to_crc = {}
        for node in data["nodes"]:
            name = node.get("name", "")
            if not name:
                continue
            crc_variants = set()
            for variant in (name, name.replace("_", " "), name.replace(" ", "_")):
                crc_variants.add(zlib.crc32(variant.encode("ascii", "replace")) & 0xFFFFFFFF)
            for fld in ("pos_cont_id", "rot_cont_id"):
                cid = node.get(fld)
                if cid is not None and cid >= 0 and cid != 0xFFFF and cid != -1:
                    for crc in crc_variants:
                        chunk_id_to_crc[cid] = crc

        if anm_files:
            dba_list = []
            for anm_path in anm_files:
                anm_stem = os.path.splitext(os.path.basename(anm_path))[0]
                try:
                    anm_data = read_anm(anm_path)
                    dba = anm_to_dba(anm_data, animation_name=anm_stem)
                    for anim in dba.animations:
                        for ctrl in anim.controllers:
                            ctrl.controller_id = chunk_id_to_crc.get(ctrl.controller_id, ctrl.controller_id)
                    dba_list.append(dba)
                    L("  ANM: %s (%d animation(s), %d pos_tracks, %d rot_tracks)" % (
                        os.path.basename(anm_path), len(dba.animations),
                        anm_data["num_pos_tracks"], anm_data["num_rot_tracks"]))
                except Exception as e:
                    L("  ANM: %s FAILED — %s" % (os.path.basename(anm_path), e))

            if dba_list:
                merged = _merge_dba(dba_list)
                try:
                    gltf, buf, n = _inject_animations(gltf, buf, merged, out_dir)
                    L("  Injected %d animation(s) total" % n)
                except Exception as e:
                    L("  Animation injection FAILED — %s" % e)
        else:
            L("  no .anm found")

    # ------------------------------------------------------------------
    # [3/3] Materials + textures
    # ------------------------------------------------------------------
    if do_tex:
        L("[3/3] Materials + textures")
        prim_materials = [p.get("material") for p in data["mesh"]]
        mtl_path = resolve_mtl(input_path, game_dirs, prim_materials,
                               strip_suffixes=True, verbose=True, log=L)
        L("  MTL: %s" % mtl_path)

        if os.path.isfile(mtl_path):
            mats, pngs, mat_info, tex_sources, xml_to_mat = convert_materials(mtl_path, game_dirs, out_dir)
            if mats:
                for mi in mat_info:
                    L("  Material: %-25s Shininess=%-6s Diffuse=%s Specular=%s Shader=%s" % (
                        mi["name"], mi["shininess"], mi["diffuse"], mi["specular"], mi["shader"]))
                L("  Textures (%d generated):" % len(pngs))
                for png in sorted(set(pngs)):
                    src = tex_sources.get(png, "unknown")
                    L("    %s  <-  %s" % (png, src))

                if pngs:
                    png_files = sorted(set(os.path.basename(f) for f in pngs))
                    gltf["images"] = [{"uri": f} for f in png_files]
                    gltf.setdefault("samplers", [{}])
                    gltf["samplers"][0] = {
                        "magFilter": 9729, "minFilter": 9987,
                        "wrapS": 10497, "wrapT": 10497,
                    }
                    gltf["textures"] = [
                        {"source": i, "sampler": 0} for i in range(len(png_files))
                    ]
                    tex_idx = {f: i for i, f in enumerate(png_files)}

                    gltf["materials"] = mats
                    for m in gltf["materials"]:
                        for slot in ("baseColorTexture", "normalTexture", "emissiveTexture"):
                            t = m.get("pbrMetallicRoughness", {}).get(slot) or m.get(slot)
                            if t and isinstance(t, dict) and "index" in t:
                                idx = t["index"]
                                if idx < len(pngs):
                                    png_base = os.path.basename(pngs[idx])
                                    t["index"] = tex_idx.get(png_base, idx)

                    # remap each primitive's material to the converted set
                    _norm = lambda s: "".join(ch.lower() for ch in s if ch.isalnum())
                    for mesh in gltf["meshes"]:
                        for prim in mesh["primitives"]:
                            mat_name = prim.pop("_mat_name", "material")
                            mat_id = prim.pop("_mat_id", 0)
                            idx_m = next(
                                (i for i, m in enumerate(mats) if m.get("name") == mat_name), None)
                            if idx_m is None:
                                tn = _norm(mat_name)
                                idx_m = next(
                                    (i for i, m in enumerate(mats)
                                     if _norm(m.get("name", "")) == tn), None)
                            if idx_m is None:
                                tn = _norm(mat_name)
                                idx_m = next(
                                    (i for i, m in enumerate(mats)
                                     if (_norm(m.get("name", "")) or "").startswith(tn)
                                     or tn.startswith(_norm(m.get("name", "")))), None)
                            if idx_m is None:
                                # translate mat_id (XML sub-material index) through
                                # the Nodraw-aware mapping instead of positional index
                                idx_m = xml_to_mat.get(mat_id)
                            prim["material"] = idx_m if idx_m is not None else 0

                    L("  %d textures, %d materials" % (len(png_files), len(mats)))
            else:
                L("  no materials parsed from .mtl, skipping textures")
        else:
            L("  no .mtl found, skipping")

    # pop internal metadata fields (_mat_name / _mat_id) from primitives
    for mesh in gltf["meshes"]:
        for prim in mesh["primitives"]:
            prim.pop("_mat_name", None)
            prim.pop("_mat_id", None)

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------
    out_bin = out_gltf.replace(".gltf", ".bin")
    gltf["buffers"][0]["byteLength"] = len(buf)
    gltf["buffers"][0]["uri"] = os.path.basename(out_bin)

    with open(out_bin, "wb") as f:
        f.write(bytes(buf))
    with open(out_gltf, "w", encoding="utf-8") as f:
        json.dump(gltf, f, separators=(",", ":"))

    L("Output: %s + %s (nodes=%d primitives=%d)" % (
        out_gltf, out_bin, data["num_nodes"], data["num_prims"]))
    L("Done: %s" % out_gltf)

    log_path = out_gltf.replace(".gltf", ".log")
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write("\n".join(log))
    L("  Log: %s" % log_path)

    return log


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def _interactive():
    print("\n=== CrisTical: Crysis animated .cga -> glTF ===\n")

    cga_path = ""
    while not cga_path or not os.path.isfile(cga_path):
        cga_path = input("Path to .cga file: ").strip().strip('"')
        if not os.path.isfile(cga_path):
            print("  File not found!")
        elif not cga_path.lower().endswith(".cga"):
            print("  Expected a .cga file (animated geometry)")
            cga_path = ""
    print()

    game_dirs = ["F:\\Games\\Crysis_Remastered\\Game"]
    print("Game directories (Enter to keep default, multiple separated by ;):")
    custom = input("  [%s] : " % game_dirs[0]).strip().strip('"')
    if custom:
        game_dirs = [d.strip() for d in custom.split(";") if d.strip()]
    print()

    cga_name = os.path.splitext(os.path.basename(cga_path))[0]
    out_path = input("Output glTF path (Enter for auto = %s.gltf): " % cga_name).strip().strip('"')
    if not out_path:
        out_path = os.path.join(os.path.dirname(cga_path) or ".", cga_name + ".gltf")
    print()

    print("--- Detecting model structure ---")
    data = read_cga(cga_path)
    print("  Nodes:      %d" % data["num_nodes"])
    print("  Primitives: %d" % data["num_prims"])
    print("  Materials:  %d" % len(data["materials"]))
    print()

    print("--- Options ---")
    print("  Animation mode:")
    print("    0 — skip  1 — auto")
    amode = input("    [1] : ").strip()
    do_anim = amode != "0"

    print("  Material mode:")
    print("    0 — skip  1 — auto-PBR")
    tmode = input("    [1] : ").strip()
    do_tex = tmode != "0"
    print()

    print("-" * 50)
    run_pipeline(cga_path, game_dirs, out_path, do_anim=do_anim, do_tex=do_tex)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli():
    ap = argparse.ArgumentParser(description="CrisTical: Crysis CGA -> animated glTF")
    ap.add_argument("--cga", help="path to .cga file")
    ap.add_argument("--gamedir", "-g", action="append", default=[], help="game root (repeatable)")
    ap.add_argument("--out", "-o", help="output .gltf path")
    ap.add_argument("--no-anim", action="store_true", help="skip animations")
    ap.add_argument("--no-tex", action="store_true", help="skip textures")
    args = ap.parse_args()

    if not args.cga:
        ap.error("--cga is required; run without args for interactive mode")
    if not os.path.isfile(args.cga):
        ap.error("file not found: %s" % args.cga)

    cga_name = os.path.splitext(os.path.basename(args.cga))[0]
    out = args.out or os.path.join(os.path.dirname(args.cga) or ".", cga_name + ".gltf")
    run_pipeline(args.cga, args.gamedir, out,
                 do_anim=not args.no_anim, do_tex=not args.no_tex)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        _interactive()
