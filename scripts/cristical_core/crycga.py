#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crycga.py — Animated geometry (.cga) reader for Crysis binary chunk files.
Authors: Soror L.'.L'. aka Methelina    Project: CrisTical
Version: 1.0

Reads .cga (animated geometry) files — Crysis binary chunk format version 0x0745.
A .cga is structurally a .cgf with added animation controller chunks.

Stage 2 scope: node hierarchy (node chunk v0823) + mesh geometry
(mesh chunk v0800 + DataStream + MeshSubsets).  TCB controller keyframe
data (Stage 3) is NOT parsed here, but controller chunk IDs and the
controller type (from the controller chunk v0826) are resolved
per node.

Reuses chunk/mesh/material readers from crycgf.py via private import.
"""

import os
import struct
import sys
import types
import zlib

# --- allow running as a standalone script (python crycga.py <file>) ---
_scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from cristical_core.crycgf import (
    _chunk_table,
    _read_nodes,
    _read_mesh,
    _read_material_names,
    _find_chunk,
    _find_chunks,
    _resolve_subset_material,
    CC_Node,
    CC_Mesh,
)
from cristical_core.crytcb import parse_controller_chunk_0826

# ---------------------------------------------------------------------------
# Chunk type constants
# ---------------------------------------------------------------------------
CC_Controller = 0xCCCC000D  # ChunkType_Controller

# ---------------------------------------------------------------------------
# Sentinel values
# ---------------------------------------------------------------------------
NO_CONTROLLER = 0xFFFF       # pos_cont_id/rot_cont_id/scl_cont_id sentinel
NO_PARENT = 0xFFFFFFFF       # ParentID sentinel (read as -1 via signed <i)

# ---------------------------------------------------------------------------
# CtrlTypes enum
# Stored in CONTROLLER_CHUNK_DESC_0826 at offset +16 (after CHUNK_HEADER).
# ---------------------------------------------------------------------------
CTRL_NONE    = 0
CTRL_CRYBONE = 1
CTRL_TCB3    = 9   # position track (Vec3)
CTRL_TCBQ    = 10  # rotation track (Quat)
CTRL_CONST   = 15

CTRL_TYPE_NAMES = {
    0:  "NONE",
    1:  "CRYBONE",
    2:  "LINEER1",
    3:  "LINEER3",
    4:  "LINEERQ",
    5:  "BEZIER1",
    6:  "BEZIER3",
    7:  "BEZIERQ",
    8:  "TCB1",
    9:  "TCB3",
    10: "TCBQ",
    11: "BSPLINE_2O",
    12: "BSPLINE_1O",
    13: "BSPLINE_2C",
    14: "BSPLINE_1C",
    15: "CONST",
}

# Offsets within the node chunk (after 16-byte chunk header)
NODE_POS_CONT_ID_OFFSET = 204
NODE_ROT_CONT_ID_OFFSET = 208
NODE_SCL_CONT_ID_OFFSET = 212


# ---------------------------------------------------------------------------
# Controller type resolution
# ---------------------------------------------------------------------------

def _resolve_controller_type(raw, chunks, pos_cont_id, rot_cont_id, scl_cont_id):
    """Look up a controller chunk by id and return its CtrlTypes name.

    Checks pos_cont_id first, then rot_cont_id, then scl_cont_id.
    Returns None when no controller chunk is found.
    """
    for cid in (pos_cont_id, rot_cont_id, scl_cont_id):
        if cid == NO_CONTROLLER or cid == -1 or cid < 0:
            continue
        cc = _find_chunk(chunks, CC_Controller, cid)
        if cc is not None:
            ctype = struct.unpack_from("<i", raw, cc["offset"] + 16)[0]
            return CTRL_TYPE_NAMES.get(ctype, "CTRL_%d" % ctype)
    return None


# ---------------------------------------------------------------------------
# Node reading (node chunk v0823)
# ---------------------------------------------------------------------------

def _read_cga_nodes(raw, chunks):
    """Read node chunk entries with controller IDs.

    Reuses crycgf._read_nodes for the base fields, then augments each
    node with pos_cont_id / rot_cont_id / scl_cont_id (offsets 204/208/212)
    and resolves controller_type.  ParentID sentinel (0xffffffff) is
    converted to None.
    """
    base = _read_nodes(raw, chunks)
    for node in base:
        c = _find_chunk(chunks, CC_Node, node["chunk_id"])
        if c is not None:
            o = c["offset"]
            node["pos_cont_id"] = struct.unpack_from("<i", raw, o + NODE_POS_CONT_ID_OFFSET)[0]
            node["rot_cont_id"] = struct.unpack_from("<i", raw, o + NODE_ROT_CONT_ID_OFFSET)[0]
            node["scl_cont_id"] = struct.unpack_from("<i", raw, o + NODE_SCL_CONT_ID_OFFSET)[0]
            node["controller_type"] = _resolve_controller_type(
                raw, chunks,
                node["pos_cont_id"], node["rot_cont_id"], node["scl_cont_id"])
        else:
            node["pos_cont_id"] = NO_CONTROLLER
            node["rot_cont_id"] = NO_CONTROLLER
            node["scl_cont_id"] = NO_CONTROLLER
            node["controller_type"] = None

        # Convert parent sentinel (0xffffffff / -1) to None
        if node["parent_id"] == -1 or node["parent_id"] == NO_PARENT:
            node["parent_id"] = None

        # Rename chunk_id -> node_id for the CGA output contract
        node["node_id"] = node.pop("chunk_id")
    return base


# ---------------------------------------------------------------------------
# Mesh primitive assembly
# ---------------------------------------------------------------------------

def _build_mesh_primitives(raw, chunks, nodes, material_chunks):
    """Build per-subset primitive list from node-referenced meshes.

    Positions/normals are left in local (node) space — the node hierarchy
    is reported separately via the nodes list.
    """
    mesh_chunks = {}
    for mc in _find_chunks(chunks, CC_Mesh):
        try:
            mesh_chunks[mc["id"]] = _read_mesh(raw, chunks, mc)
        except Exception:
            continue

    prims = []
    for n in nodes:
        obj_id = n.get("obj_id", 0)
        if obj_id not in mesh_chunks:
            continue
        mesh = mesh_chunks[obj_id]
        for ss in mesh["subsets"]:
            if ss["num_idx"] <= 0 or ss["num_vert"] <= 0:
                continue
            fi = ss["first_idx"]
            ni = ss["num_idx"]
            fv = ss["first_vert"]
            nv = ss["num_vert"]
            mid = ss["mat_id"]

            idx = [i - fv for i in mesh["indices"][fi:fi + ni]]
            pos = list(mesh["positions"][fv:fv + nv])
            nrm = list(mesh["normals"][fv:fv + nv]) if mesh["normals"] else []
            uv = list(mesh["uvs"][fv:fv + nv]) if mesh["uvs"] else []
            col = list(mesh["colors"][fv:fv + nv]) if mesh["colors"] else []
            tan = list(mesh["tangents"][fv:fv + nv]) if mesh["tangents"] else []

            mat_name = _resolve_subset_material(material_chunks, n, mid)

            prims.append({
                "positions": pos,
                "normals": nrm,
                "uvs": uv,
                "colors": col,
                "tangents": tan,
                "indices": idx,
                "mat_id": mid,
                "material": mat_name,
                "node_name": n["name"],
                "node_id": n["node_id"],
            })
    return prims


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_cga(path):
    """Parse a .cga file and return node hierarchy + mesh primitives.

    Returns dict with:
      nodes        — list of dicts: name, node_id, parent_id (int|None),
                     pos_cont_id, rot_cont_id, scl_cont_id, controller_type,
                     obj_id, mat_id, n_children, pos, rot_quat, scale
      mesh         — list of primitive dicts: positions, normals, uvs, colors,
                     tangents, indices, mat_id, material, node_name, node_id
      materials    — list of material name strings
      num_nodes    — int
      num_prims    — int
    """
    with open(path, "rb") as f:
        raw = f.read()

    chunks = _chunk_table(raw)

    nodes = _read_cga_nodes(raw, chunks)
    material_chunks = _read_material_names(raw, chunks)
    materials = [m["name"] for m in material_chunks.values()]
    prims = _build_mesh_primitives(raw, chunks, nodes, material_chunks)

    return {
        "nodes": nodes,
        "mesh": prims,
        "materials": materials,
        "material_chunks": material_chunks,
        "num_nodes": len(nodes),
        "num_prims": len(prims),
    }


def read_anm(path):
    """Parse a .anm file and return node hierarchy + TCB controller keyframes.

    Returns dict with:
      nodes            — list of node dicts (augmented with tcb_pos / tcb_rot)
      controller_chunks — int count of 0x0826 controller chunks found
      num_nodes        — int
      num_pos_tracks   — int total position tracks across nodes
      num_rot_tracks   — int total rotation tracks across nodes
      total_keys_pos   — int
      total_keys_rot   — int
    """
    with open(path, "rb") as f:
        raw = f.read()

    try:
        chunks = _chunk_table(raw)
    except struct.error:
        ft, fv, cto, nch = struct.unpack_from("<IIII", raw, 8)
        entry = 20 if fv == 0x0745 else 16
        nch = struct.unpack_from("<I", raw, cto)[0]
        chunks = []
        for i in range(nch):
            t, v, o, cid = struct.unpack_from("<IIII", raw, cto + 4 + i * entry)
            chunks.append({"type": t, "version": v, "offset": o, "id": cid})

    nodes = _read_cga_nodes(raw, chunks)

    tcb_chunks = [
        c for c in chunks
        if c["type"] == CC_Controller and c["version"] == 0x0826
    ]
    by_id = {}
    for c in tcb_chunks:
        by_id.setdefault(c["id"], c)

    def _chunk_end(c):
        offset = c["offset"]
        end = len(raw)
        for cc in chunks:
            if cc["offset"] > offset and cc["offset"] < end:
                end = cc["offset"]
        return end

    num_pos_tracks = 0
    num_rot_tracks = 0
    total_keys_pos = 0
    total_keys_rot = 0

    for node in nodes:
        pos_id = node.get("pos_cont_id", NO_CONTROLLER)
        if pos_id != NO_CONTROLLER and pos_id > 0:
            c = by_id.get(pos_id)
            if c is not None:
                decoded = parse_controller_chunk_0826(raw[c["offset"]:_chunk_end(c)])
                if decoded["ctrl_type"] == CTRL_TCB3:
                    node["tcb_pos"] = decoded
                    num_pos_tracks += 1
                    total_keys_pos += decoded["n_keys"]

        rot_id = node.get("rot_cont_id", NO_CONTROLLER)
        if rot_id != NO_CONTROLLER and rot_id > 0:
            c = by_id.get(rot_id)
            if c is not None:
                decoded = parse_controller_chunk_0826(raw[c["offset"]:_chunk_end(c)])
                if decoded["ctrl_type"] == CTRL_TCBQ:
                    node["tcb_rot"] = decoded
                    num_rot_tracks += 1
                    total_keys_rot += decoded["n_keys"]

    return {
        "nodes": nodes,
        "controller_chunks": len(tcb_chunks),
        "num_nodes": len(nodes),
        "num_pos_tracks": num_pos_tracks,
        "num_rot_tracks": num_rot_tracks,
        "total_keys_pos": total_keys_pos,
        "total_keys_rot": total_keys_rot,
    }


def anm_to_dba(anm_data, animation_name="anm_anim"):
    """Build a DBA-container-compatible object from read_anm() output.

    Returns a types.SimpleNamespace exposing key_times, key_pos, key_rot
    (lists of float-track/value-track lists) and animations (a single-entry
    list). Each controller is a SimpleNamespace with real boolean attributes
    has_pos/has_rot plus pos_t/pos_kt/rot_t/rot_kt track indices.
    """
    dba = types.SimpleNamespace()
    dba.key_times = []
    dba.key_pos = []
    dba.key_rot = []

    anim = types.SimpleNamespace()
    anim.name = animation_name
    anim.secs_per_tick = 1.0 / 30.0
    anim.controllers = []

    for node in anm_data["nodes"]:
        tcb_pos = node.get("tcb_pos")
        tcb_rot = node.get("tcb_rot")
        if tcb_pos is None and tcb_rot is None:
            continue

        if tcb_rot is not None:
            controller_id = tcb_rot["controller_id"]
        elif tcb_pos is not None:
            controller_id = tcb_pos["controller_id"]
        else:
            controller_id = zlib.crc32(
                node["name"].lower().encode("ascii", "replace")) & 0xFFFFFFFF

        rot_t = -1
        rot_kt = -1
        pos_t = -1
        pos_kt = -1

        if tcb_rot is not None and tcb_rot.get("ctrl_type") == CTRL_TCBQ:
            rot_kt = len(dba.key_times)
            dba.key_times.append([float(k["time"]) for k in tcb_rot["keys"]])
            rot_t = len(dba.key_rot)
            dba.key_rot.append([tuple(float(v) for v in k["value"])
                                for k in tcb_rot["keys"]])

        if tcb_pos is not None and tcb_pos.get("ctrl_type") == CTRL_TCB3:
            pos_kt = len(dba.key_times)
            dba.key_times.append([float(k["time"]) for k in tcb_pos["keys"]])
            pos_t = len(dba.key_pos)
            dba.key_pos.append([tuple(float(v) for v in k["value"])
                                for k in tcb_pos["keys"]])

        if rot_t < 0 and rot_kt < 0 and pos_t < 0 and pos_kt < 0:
            continue

        ctrl = types.SimpleNamespace()
        ctrl.controller_id = controller_id
        ctrl.has_rot = (rot_t >= 0 and rot_kt >= 0)
        ctrl.rot_t = rot_t
        ctrl.rot_kt = rot_kt
        ctrl.has_pos = (pos_t >= 0 and pos_kt >= 0)
        ctrl.pos_t = pos_t
        ctrl.pos_kt = pos_kt
        anim.controllers.append(ctrl)

    dba.animations = [anim]
    return dba


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _is_bone_like(node):
    """Heuristic: a node with animation controllers is bone/joint-like."""
    cid = node["pos_cont_id"]
    return cid != NO_CONTROLLER and cid != -1 and cid > 0


def main():
    if len(sys.argv) < 2:
        print("Usage: python crycga.py <file.cga>")
        sys.exit(1)

    path = sys.argv[1]
    data = read_cga(path)

    print("=== CGA Summary ===")
    print("File:       %s" % os.path.basename(path))
    print("Nodes:      %d" % data["num_nodes"])
    print("Primitives: %d" % data["num_prims"])
    print("Materials:  %d" % len(data["materials"]))
    for m in data["materials"]:
        print("  mat: %s" % m)

    print()
    print("Nodes (first %d of %d):" % (min(30, len(data["nodes"])), data["num_nodes"]))
    for n in data["nodes"][:30]:
        p = n["parent_id"]
        pstr = str(p) if p is not None else "(root)"
        print("  %-32s id=%-6d parent=%-8s pos_cont=%-6d rot_cont=%-6d scl_cont=%-6d type=%s" % (
            n["name"][:32], n["node_id"], pstr,
            n["pos_cont_id"], n["rot_cont_id"], n["scl_cont_id"],
            n["controller_type"] or "-"))

    bone_like = [n for n in data["nodes"] if _is_bone_like(n)]
    mesh_nodes = [n for n in data["nodes"] if n.get("obj_id", 0) > 0]
    print()
    print("Bone-like nodes (with controllers):  %d" % len(bone_like))
    print("Mesh-referencing nodes:             %d" % len(mesh_nodes))

    if data["num_prims"] > 0:
        sample = data["mesh"][0]
        print("Mesh read: SUCCESS  (sample prim: %d verts, %d indices, mat='%s')" % (
            len(sample["positions"]), len(sample["indices"]), sample["material"]))
    else:
        print("Mesh read: NO PRIMITIVES FOUND")


if __name__ == "__main__":
    main()
