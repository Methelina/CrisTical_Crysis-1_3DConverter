#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crycgf.py — CrisTical: static Crysis binary chunk file (.cgf) reader for static geometry
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0

Reads compiled static geometry CGF (Crysis 1 / Remastered) — chunk
layout determined by independent analysis of sample files:

  - chunk table entry width 16 (0x0744) or 20 (0x0745, aligned)
  - ChunkType_Mesh           0xCCCC0000  (mesh desc, streams referenced by id)
  - ChunkType_Node           0xCCCC000B  (node desc, parent/child, localTM)
  - ChunkType_MtlName        0xCCCC0014
  - ChunkType_DataStream     0xCCCC0016
  - ChunkType_MeshSubsets    0xCCCC0017
  - ChunkType_ExportFlags    0xCCCC0013

Streams (ECgfStreamType): POSITIONS=0, NORMALS=1, TEXCOORDS=2,
  COLORS=3 (4x u8 RGBA), COLORS2=4, INDICES=5 (u16), TANGENTS=6.

Unlike read_chr (skinned .chr), this parser does NOT require skeleton chunks:
static CGF has none. Vertex colors (stream 3) are returned as float RGBA
per vertex for direct glTF COLOR_0 export.

Exposed API:
  read_cgf(path) -> dict
    Returns: {nodes:[{name,parent,pos,rot_quat(quat),scale,localTM,meshes:[...]}],
              materials:[names], ...}
  read_cgf_meshes(path) -> list of primitive dicts (positions, normals, uvs,
    colors, indices, mat_id, node_transform)
"""

import math
import os
import struct

# ---------------------------------------------------------------------------
# Chunk type constants
# ---------------------------------------------------------------------------
CC_ExportFlags    = 0xCCCC0013
CC_MtlName        = 0xCCCC0014
CC_DataStream     = 0xCCCC0016
CC_MeshSubsets    = 0xCCCC0017
CC_Mesh           = 0xCCCC0000
CC_Node           = 0xCCCC000B

# ECgfStreamType
STREAM_POSITIONS = 0
STREAM_NORMALS   = 1
STREAM_TEXCOORDS = 2
STREAM_COLORS    = 3
STREAM_COLORS2   = 4
STREAM_INDICES   = 5
STREAM_TANGENTS  = 6

# ---------------------------------------------------------------------------
# Chunk table
# ---------------------------------------------------------------------------

def _chunk_table(raw):
    sig = raw[:6]
    if sig != b"CryTek":
        raise ValueError("not a Crysis binary chunk file")
    ft, fv, cto, nch = struct.unpack_from("<IIII", raw, 8)
    entry = 20 if fv == 0x0745 else 16
    chunks = []
    for i in range(nch):
        t, v, o, cid = struct.unpack_from("<IIII", raw, cto + 4 + i * entry)
        chunks.append({"type": t, "version": v, "offset": o, "id": cid})
    return chunks


def _find_chunk(chunks, ctype, cid=None):
    for c in chunks:
        if c["type"] == ctype and (cid is None or c["id"] == cid):
            return c
    return None


def _find_chunks(chunks, ctype):
    return [c for c in chunks if c["type"] == ctype]


def _stream_data(raw, chunk):
    """Return (stream_type, count, element_size, bytes) for a DataStream chunk."""
    if chunk is None:
        return None
    nf, st, cnt, esz = struct.unpack_from("<iiii", raw, chunk["offset"] + 16)
    data = raw[chunk["offset"] + 40: chunk["offset"] + 40 + cnt * esz]
    return st, cnt, esz, data


# ---------------------------------------------------------------------------
# Mesh subsets
# ---------------------------------------------------------------------------

def _read_subsets(raw, chunks, subsets_chunk_id):
    """MeshSubset: first_idx, num_idx, first_vtx, num_vtx, mat_id, radius, center."""
    chunk = _find_chunk(chunks, CC_MeshSubsets, subsets_chunk_id)
    if chunk is None:
        return []
    flags = struct.unpack_from("<i", raw, chunk["offset"] + 16)[0]
    count = struct.unpack_from("<i", raw, chunk["offset"] + 20)[0]
    subsets = []
    sp = chunk["offset"] + 32
    for _ in range(count):
        fi, ni, fv, nv, mid, radius = struct.unpack_from("<6i", raw, sp)
        cx, cy, cz = struct.unpack_from("<3f", raw, sp + 24)
        subsets.append({
            "first_idx": fi, "num_idx": ni,
            "first_vert": fv, "num_vert": nv,
            "mat_id": mid, "radius": radius, "center": (cx, cy, cz),
        })
        sp += 36
    return subsets


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def _read_material_names(raw, chunks):
    """Return material records from MtlName chunks (incl. sub-material chunk ids).

    Root multi-material records (FLAG_MULTI_MATERIAL=0x1)
    list their sub-materials in nSubMatChunkId[32] at +160. Mesh subset mat_id
    indexes into the NODE's material subMaterials, so the whole graph is kept.
    """
    names = {}
    for c in _find_chunks(chunks, CC_MtlName):
        name_raw = raw[c["offset"] + 24: c["offset"] + 24 + 128]
        name = name_raw.split(b"\x00")[0].decode("ascii", "replace")
        nflags = struct.unpack_from("<i", raw, c["offset"] + 16)[0]
        nsub = struct.unpack_from("<i", raw, c["offset"] + 24 + 128 + 4)[0]
        sub_ids = []
        if nsub > 0:
            sub_ids = [x for x in struct.unpack_from(
                "<%di" % 32, raw, c["offset"] + 160) if x > 0]
        names[c["id"]] = {
            "chunk_id": c["id"], "name": name,
            "flags": nflags, "sub_count": nsub, "sub_ids": sub_ids,
        }
    return names


def _resolve_subset_material(materials, node, subset_mat_id):
    """Map subset.mat_id -> material name via the node's material subMaterials.

    subset.nMatID indexes into the node material's subMaterials list.
    Falls back to the flat name when the node's material has no sub-materials.
    """
    node_mat = materials.get(node.get("mat_id"))
    if node_mat is None:
        return "material_%d" % subset_mat_id
    if node_mat["sub_ids"]:
        if 0 <= subset_mat_id < len(node_mat["sub_ids"]):
            sub = materials.get(node_mat["sub_ids"][subset_mat_id])
            if sub:
                return sub["name"]
        return "material_%d" % subset_mat_id
    return node_mat["name"]


# ---------------------------------------------------------------------------
# Mesh stream loading (mirrors CLoaderCGF::LoadCompiledMeshChunk)
# ---------------------------------------------------------------------------

def _read_mesh(raw, chunks, mesh_chunk):
    """Read a compiled mesh chunk + its referenced streams."""
    o = mesh_chunk["offset"]
    nFlags  = struct.unpack_from("<i", raw, o + 16)[0]
    nFlags2 = struct.unpack_from("<i", raw, o + 20)[0]
    nVerts   = struct.unpack_from("<i", raw, o + 24)[0]
    nIndices = struct.unpack_from("<i", raw, o + 28)[0]
    nSubsets = struct.unpack_from("<i", raw, o + 32)[0]
    ss_id    = struct.unpack_from("<i", raw, o + 36)[0]
    stream_ids = [struct.unpack_from("<i", raw, o + 44 + i * 4)[0] for i in range(16)]

    subsets = _read_subsets(raw, chunks, ss_id)

    streams = {}
    for stype, cid in enumerate(stream_ids):
        if cid <= 0:
            continue
        sc = _find_chunk(chunks, CC_DataStream, cid)
        info = _stream_data(raw, sc)
        if info and info[2] > 0:
            streams[stype] = info

    positions = []
    if STREAM_POSITIONS in streams:
        _, cnt, esz, data = streams[STREAM_POSITIONS]
        if esz == 12:
            positions = [struct.unpack_from("<3f", data, i * esz) for i in range(cnt)]
        elif esz == 8:
            # Vec3f16: 4x half-float (x, y, z, w=1.0 padding) per vertex
            positions = [struct.unpack_from("<4e", data, i * esz)[:3] for i in range(cnt)]

    normals = []
    if STREAM_NORMALS in streams:
        _, cnt, esz, data = streams[STREAM_NORMALS]
        normals = [struct.unpack_from("<3f", data, i * esz) for i in range(cnt)]

    uvs = []
    if STREAM_TEXCOORDS in streams:
        _, cnt, esz, data = streams[STREAM_TEXCOORDS]
        uvs = [struct.unpack_from("<2f", data, i * esz) for i in range(cnt)]

    colors = []
    if STREAM_COLORS in streams:
        _, cnt, esz, data = streams[STREAM_COLORS]
        for i in range(cnt):
            r, g, b, a = struct.unpack_from("<4B", data, i * esz)
            colors.append((r / 255.0, g / 255.0, b / 255.0, a / 255.0))

    indices = []
    if STREAM_INDICES in streams:
        _, cnt, esz, data = streams[STREAM_INDICES]
        fmt = "<%dH" % cnt
        indices = list(struct.unpack_from(fmt, data, 0))

    tangents = []
    if STREAM_TANGENTS in streams:
        _, cnt, esz, data = streams[STREAM_TANGENTS]
        for i in range(cnt):
            # packed tangent + binormal: 2x (4 int16 shorts, value/32767)
            t0, t1, t2, t3, b0, b1, b2, b3 = struct.unpack_from("<8h", data, i * esz)
            tangents.append(({
                "tangent": (t0 / 32767.0, t1 / 32767.0, t2 / 32767.0, t3 / 32767.0),
                "binormal": (b0 / 32767.0, b1 / 32767.0, b2 / 32767.0, b3 / 32767.0),
            }))

    return {
        "nVerts": nVerts, "nIndices": nIndices,
        "positions": positions, "normals": normals, "uvs": uvs,
        "colors": colors, "indices": indices, "tangents": tangents,
        "subsets": subsets,
    }


# ---------------------------------------------------------------------------
# Node transforms (node chunk v0823)
# ---------------------------------------------------------------------------

def _read_nodes(raw, chunks):
    """Parse Node chunks; returns list with parent ids and local pos/rot/scale."""
    nodes = []
    for c in _find_chunks(chunks, CC_Node):
        o = c["offset"]
        name = raw[o + 16: o + 16 + 64].split(b"\x00")[0].decode("ascii", "replace")
        obj_id   = struct.unpack_from("<i", raw, o + 80)[0]
        parent   = struct.unpack_from("<i", raw, o + 84)[0]
        nchild   = struct.unpack_from("<i", raw, o + 88)[0]
        mat_id   = struct.unpack_from("<i", raw, o + 92)[0]
        is_head  = raw[o + 96]
        is_member = raw[o + 97]
        # tm[4][4] floats at +100; but engine builds localTM from pos/rot/scl
        pos = struct.unpack_from("<3f", raw, o + 164)
        rot = struct.unpack_from("<4f", raw, o + 176)
        scl = struct.unpack_from("<3f", raw, o + 192)
        nodes.append({
            "chunk_id": c["id"], "name": name,
            "obj_id": obj_id, "parent_id": parent,
            "n_children": nchild, "mat_id": mat_id,
            "pos": pos, "rot_quat": rot, "scale": scl,
        })
    return nodes


def _quat_rotate(q, v):
    x, y, z, w = q
    tx = 2.0 * (y * v[2] - z * v[1])
    ty = 2.0 * (z * v[0] - x * v[2])
    tz = 2.0 * (x * v[1] - y * v[0])
    return (v[0] + w * tx + y * tz - z * ty,
            v[1] + w * ty + z * tx - x * tz,
            v[2] + w * tz + x * ty - y * tx)


def _apply_node_transform(p, node, parent_world=None):
    """Transform local point by node local pos/rot/scale (+ parent chain)."""
    q = node["rot_quat"]
    s = node["scale"]
    t = node["pos"]
    p = (p[0] * s[0], p[1] * s[1], p[2] * s[2])
    p = _quat_rotate(q, p)
    p = (p[0] + t[0], p[1] + t[1], p[2] + t[2])
    if parent_world is not None:
        # parent transform is (q_p, t_p, s_p): apply in order
        qp, tp, sp = parent_world
        p = (p[0] * sp[0], p[1] * sp[1], p[2] * sp[2])
        p = _quat_rotate(qp, p)
        p = (p[0] + tp[0], p[1] + tp[1], p[2] + tp[2])
    return p


def _world_transform(nodes, node_idx):
    """Return accumulated (quat, pos, scale) from node to root."""
    chain = []
    i = node_idx
    seen = set()
    while i is not None and i not in seen and 0 <= i < len(nodes):
        seen.add(i)
        chain.append(nodes[i])
        p = nodes[i]["parent_id"]
        i = next((k for k, n in enumerate(nodes) if n["chunk_id"] == p), None)
    q = (0.0, 0.0, 0.0, 1.0)
    t = (0.0, 0.0, 0.0)
    s = (1.0, 1.0, 1.0)
    for n in reversed(chain):
        qn, tn, sn = n["rot_quat"], n["pos"], n["scale"]
        # compose: p' = R_n * S_n * p + T_n  (apply after existing)
        p = s  # existing scale applies to new rotation already done inside _apply
        # compose rotations: q = q * qn
        q = _quat_mul(q, qn)
        t = (t[0] + qn[0] * 0, t[1] + qn[1] * 0, t[2] + qn[2] * 0)  # placeholder replaced below
        # full compose handled via explicit matrices for clarity
    return q, t, s


def _quat_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def _compose_world_matrix(nodes, node_idx):
    """Build 4x4 column-major world matrix for node (rotation from quat, TRS)."""
    chain = []
    i = node_idx
    seen = set()
    while i is not None and i not in seen and 0 <= i < len(nodes):
        seen.add(i)
        chain.append(nodes[i])
        pid = nodes[i]["parent_id"]
        i = next((k for k, n in enumerate(nodes) if n["chunk_id"] == pid), None)

    m = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    for n in reversed(chain):
        m = _mat4_mul(_trs_matrix(n), m)
    return m


def _trs_matrix(node):
    q = node["rot_quat"]
    x, y, z, w = q
    t = node["pos"]
    s = node["scale"]
    r00 = 1 - 2 * (y * y + z * z)
    r01 = 2 * (x * y - z * w)
    r02 = 2 * (x * z + y * w)
    r10 = 2 * (x * y + z * w)
    r11 = 1 - 2 * (x * x + z * z)
    r12 = 2 * (y * z - x * w)
    r20 = 2 * (x * z - y * w)
    r21 = 2 * (y * z + x * w)
    r22 = 1 - 2 * (x * x + y * y)
    # column-major 4x4 with scale applied to rotation columns
    return [
        r00 * s[0], r01 * s[0], r02 * s[0], 0.0,
        r10 * s[1], r11 * s[1], r12 * s[1], 0.0,
        r20 * s[2], r21 * s[2], r22 * s[2], 0.0,
        t[0], t[1], t[2], 1.0,
    ]


def _mat4_mul(a, b):
    # a, b are column-major 4x4; result = a * b
    r = [0.0] * 16
    for col in range(4):
        for row in range(4):
            r[col * 4 + row] = (a[0 * 4 + row] * b[col * 4 + 0] +
                                a[1 * 4 + row] * b[col * 4 + 1] +
                                a[2 * 4 + row] * b[col * 4 + 2] +
                                a[3 * 4 + row] * b[col * 4 + 3])
    return r


def _mat4_apply(m, p):
    x, y, z = p
    # m column-major: m[col*4+row]
    return (
        m[0] * x + m[4] * y + m[8] * z + m[12],
        m[1] * x + m[5] * y + m[9] * z + m[13],
        m[2] * x + m[6] * y + m[10] * z + m[14],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_cgf(path):
    """Parse a static CGF and return full structure (nodes, meshes, materials)."""
    with open(path, "rb") as f:
        raw = f.read()
    chunks = _chunk_table(raw)

    material_chunks = _read_material_names(raw, chunks)
    materials = [m["name"] for m in material_chunks.values()]

    nodes = _read_nodes(raw, chunks)

    mesh_chunks = _find_chunks(chunks, CC_Mesh)
    loaded = {}
    for mc in mesh_chunks:
        try:
            loaded[mc["id"]] = _read_mesh(raw, chunks, mc)
        except Exception:
            continue

    return {
        "nodes": nodes,
        "materials": materials,
        "material_chunks": material_chunks,
        "mesh_chunks": loaded,
        "chunk_count": len(chunks),
    }


def read_cgf_meshes(path):
    """Return list of primitive dicts for all meshes with node transforms baked in.

    Each primitive has: positions, normals, uvs, colors (RGBA floats), tangents,
    indices, mat_id, node_name. Positions/normals are in the node's world space
    (parent chain applied), matching how the object is positioned in-game.
    """
    data = read_cgf(path)
    nodes = data["nodes"]
    mesh_chunks = data["mesh_chunks"]
    materials = data["material_chunks"]

    prims = []
    for n in nodes:
        obj_id = n["obj_id"]
        if obj_id not in mesh_chunks:
            continue
        mesh = mesh_chunks[obj_id]
        nidx = nodes.index(n)
        world = _compose_world_matrix(nodes, nidx)

        for ss in mesh["subsets"]:
            if ss["num_idx"] <= 0 or ss["num_vert"] <= 0:
                continue
            fi = ss["first_idx"]
            ni = ss["num_idx"]
            fv = ss["first_vert"]
            nv = ss["num_vert"]
            mid = ss["mat_id"]

            idx = [i - fv for i in mesh["indices"][fi: fi + ni]]
            pos = mesh["positions"][fv: fv + nv]
            nrm = mesh["normals"][fv: fv + nv] if mesh["normals"] else []
            uv = mesh["uvs"][fv: fv + nv] if mesh["uvs"] else []
            col = mesh["colors"][fv: fv + nv] if mesh["colors"] else []
            tan = mesh["tangents"][fv: fv + nv] if mesh["tangents"] else []

            pos_w = [_mat4_apply(world, p) for p in pos]
            # normals: rotate only (no translation) via upper 3x3
            nrm_w = []
            for v in nrm:
                nrm_w.append(_mat4_apply([world[0], world[1], world[2], 0,
                                          world[4], world[5], world[6], 0,
                                          world[8], world[9], world[10], 0,
                                          0, 0, 0, 1], v))

            mat_name = _resolve_subset_material(materials, n, mid)
            prims.append({
                "positions": pos_w,
                "normals": nrm_w,
                "uvs": uv,
                "colors": col,
                "tangents": tan,
                "indices": idx,
                "mat_id": mid,
                "material": mat_name,
                "node_name": n["name"],
            })
    return prims
