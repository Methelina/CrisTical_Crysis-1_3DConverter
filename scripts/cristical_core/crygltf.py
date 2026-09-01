#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crygltf.py — Crysis CHR/mesh -> glTF 2.0 exporter
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0

=== glTF skeleton + skin + mesh writer with unified axis conversion ===

Converts the source right-handed Z-up coords to glTF right-handed Y-up
using a single similarity transform C: (x,y,z) -> (-x, z, y).

API:
  export_gltf(skeleton, mesh) -> (gltf_json, bin_bytes)
"""

import json
import struct

AXIS_SWAP_POS  = lambda p: (-p[0], p[2], p[1])
AXIS_SWAP_QUAT = lambda q: (-q[0], q[2], q[1], q[3])
AXIS_SWAP_NRM  = lambda n: (-n[0], n[2], n[1])


def _quat_to_matrix(q):
    x, y, z, w = q
    return [
        1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w),
        2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
        2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y),
    ]


def _identity_quat():
    return (0.0, 0.0, 0.0, 1.0)


def _quat_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]


def _float_equal(a, b, eps=1e-6):
    return abs(a - b) <= eps


def _is_identity_rot(q):
    return _float_equal(_quat_dot(q, _identity_quat()), 1.0)


def _is_zero_vec(v):
    return all(_float_equal(x, 0.0) for x in v)


def export_gltf(skeleton, mesh):
    buf = bytearray()
    nodes = []
    scenes = [{"nodes": []}]
    accessors = []
    buffer_views = []

    def append_buf(payload):
        while len(buf) % 4:
            buf.append(0)
        off = len(buf)
        buf.extend(payload)
        view_idx = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": len(payload)})
        return view_idx

    def add_accessor(view_idx, count, acc_type, comp_type=5126, byte_off=0):
        acc = {
            "bufferView": view_idx,
            "byteOffset": byte_off,
            "componentType": comp_type,
            "count": count,
            "type": acc_type,
        }
        accessors.append(acc)
        return len(accessors) - 1

    num_bones = len(skeleton)
    node_idx_map = [-1] * num_bones

    for i, b in enumerate(skeleton):
        local_t = b["local_trans"]
        local_q = b["local_quat"]
        t_gltf = AXIS_SWAP_POS(local_t)
        q_gltf = AXIS_SWAP_QUAT(local_q)

        node = {"name": b["name"]}
        if not _is_zero_vec(t_gltf):
            node["translation"] = list(t_gltf)
        if not _is_identity_rot(q_gltf):
            node["rotation"] = list(q_gltf)
        node_idx_map[i] = len(nodes)
        nodes.append(node)

    for i, b in enumerate(skeleton):
        p = b["parent"]
        if p >= 0:
            nodes[node_idx_map[p]].setdefault("children", []).append(node_idx_map[i])
    root_indices = [i for i, b in enumerate(skeleton) if b["parent"] < 0]
    scenes[0]["nodes"] = root_indices

    ibm_list = []
    for b in skeleton:
        b2w = b["b2w"]
        m00, m01, m02, m03, m10, m11, m12, m13, m20, m21, m22, m23 = b2w

        r00i = m00; r01i = m10; r02i = m20
        r10i = m01; r11i = m11; r12i = m21
        r20i = m02; r21i = m12; r22i = m22
        def dot3(r0, r1, r2, v):
            return -(r0 * v[0] + r1 * v[1] + r2 * v[2])
        ti0 = dot3(r00i, r01i, r02i, (m03, m13, m23))
        ti1 = dot3(r10i, r11i, r12i, (m03, m13, m23))
        ti2 = dot3(r20i, r21i, r22i, (m03, m13, m23))

        nt = (-ti0, ti2, ti1)
        cr00 =  r00i; cr01 = -r02i; cr02 = -r01i
        cr10 = -r20i; cr11 =  r22i; cr12 =  r21i
        cr20 = -r10i; cr21 =  r12i; cr22 =  r11i

        col_major = [
            cr00, cr10, cr20, 0.0,
            cr01, cr11, cr21, 0.0,
            cr02, cr12, cr22, 0.0,
            nt[0], nt[1], nt[2], 1.0,
        ]
        ibm_list.extend(col_major)

    ibm_bytes = struct.pack("<%df" % len(ibm_list), *ibm_list)
    ibm_view = append_buf(ibm_bytes)
    ibm_acc = add_accessor(ibm_view, num_bones, "MAT4")

    skin = {
        "name": "skin",
        "inverseBindMatrices": ibm_acc,
        "joints": [node_idx_map[i] for i in range(num_bones)],
        "skeleton": node_idx_map[0],
    }

    gltf_mesh = {"name": "model", "primitives": []}
    for prim in mesh["primitives"]:
        pos_raw = []
        nrm_raw = []
        uv_raw = []
        j_raw = []
        w_raw = []
        idx_raw = []

        for p in prim["positions"]:
            g = AXIS_SWAP_POS(p)
            pos_raw.extend(g)
        for n in prim["normals"]:
            g = AXIS_SWAP_NRM(n)
            nrm_raw.extend(g)
        for uv in prim["uvs"]:
            uv_raw.extend(uv)
        for j4 in prim["joints"]:
            j_raw.extend(j4)
        for w4 in prim["weights"]:
            w_raw.extend(w4)
        for vi in prim["indices"]:
            idx_raw.append(vi)

        nv = len(prim["positions"])
        ni = len(prim["indices"])

        pv = append_buf(struct.pack("<%df" % len(pos_raw), *pos_raw))
        pa = add_accessor(pv, nv, "VEC3")
        xs = pos_raw[0::3]; ys = pos_raw[1::3]; zs = pos_raw[2::3]
        accessors[pa]["min"] = [min(xs), min(ys), min(zs)]
        accessors[pa]["max"] = [max(xs), max(ys), max(zs)]
        nv2 = append_buf(struct.pack("<%df" % len(nrm_raw), *nrm_raw))
        na = add_accessor(nv2, nv, "VEC3")
        uv_v = append_buf(struct.pack("<%df" % len(uv_raw), *uv_raw))
        uva = add_accessor(uv_v, nv, "VEC2")
        jv = append_buf(struct.pack("<%dH" % len(j_raw), *j_raw))
        ja = add_accessor(jv, nv, "VEC4", comp_type=5123)
        wv = append_buf(struct.pack("<%df" % len(w_raw), *w_raw))
        wa = add_accessor(wv, nv, "VEC4")
        iv = append_buf(struct.pack("<%dH" % ni, *idx_raw))
        ia = add_accessor(iv, ni, "SCALAR", comp_type=5123)

        gltf_mesh["primitives"].append({
            "attributes": {
                "POSITION": pa,
                "NORMAL": na,
                "TEXCOORD_0": uva,
                "JOINTS_0": ja,
                "WEIGHTS_0": wa,
            },
            "indices": ia,
            "_mat_id": prim.get("mat_id", 0),
        })

    mesh_node_idx = len(nodes)
    nodes.append({
        "name": "model",
        "mesh": 0,
        "skin": 0,
    })
    scenes[0]["nodes"].append(mesh_node_idx)

    gltf = {
        "asset": {"version": "2.0", "generator": "CrisTical crygltf.py 1.0"},
        "scenes": scenes,
        "nodes": nodes,
        "skins": [skin],
        "meshes": [gltf_mesh],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(buf)}],
    }

    return gltf, buf


# ---------------------------------------------------------------------------
# Static geometry export (no skeleton) — for .cgf vegetation/props
# ---------------------------------------------------------------------------

def export_gltf_static(meshes):
    """Export a list of primitive dicts (from crycgf.read_cgf_meshes) to glTF.

    All primitives share ONE merged vertex buffer (positions/normals/uvs/
    colors/tangents concatenated); each primitive becomes a sub-mesh with its
    own indices accessor offset by the running vertex count. This is what
    glTF importers that merge primitives into a single Unity mesh expect
    (indices must reference the merged buffer).
    """
    buf = bytearray()
    nodes = []
    accessors = []
    buffer_views = []

    def append_buf(payload):
        while len(buf) % 4:
            buf.append(0)
        off = len(buf)
        buf.extend(payload)
        view_idx = len(buffer_views)
        buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": len(payload)})
        return view_idx

    def add_accessor(view_idx, count, acc_type, comp_type=5126, byte_off=0):
        acc = {
            "bufferView": view_idx,
            "byteOffset": byte_off,
            "componentType": comp_type,
            "count": count,
            "type": acc_type,
        }
        accessors.append(acc)
        return len(accessors) - 1

    total_v = sum(len(p["positions"]) for p in meshes)
    total_i = sum(len(p["indices"]) for p in meshes)

    pos_raw = []
    nrm_raw = []
    uv_raw = []
    col_raw = []
    tan_raw = []
    all_nrm_has = bool(meshes) and all(len(p.get("normals", [])) == len(p["positions"]) for p in meshes)
    all_uv_has = bool(meshes) and all(len(p.get("uvs", [])) == len(p["positions"]) for p in meshes)
    all_col_has = bool(meshes) and all(len(p.get("colors", [])) == len(p["positions"]) for p in meshes)
    all_tan_has = bool(meshes) and all(len(p.get("tangents", [])) == len(p["positions"]) for p in meshes)

    cur = 0
    prim_meta = []
    for prim in meshes:
        nv = len(prim["positions"])
        for p in prim["positions"]:
            g = AXIS_SWAP_POS(p)
            pos_raw.extend(g)
        if all_nrm_has:
            for n in prim["normals"]:
                g = AXIS_SWAP_NRM(n)
                nrm_raw.extend(g)
        if all_uv_has:
            for uv in prim["uvs"]:
                uv_raw.extend(uv)
        if all_col_has:
            for c in prim["colors"]:
                col_raw.extend(c)
        if all_tan_has:
            for t in prim["tangents"]:
                tan_raw.extend(t["tangent"])
        prim_meta.append({
            "mat_name": prim.get("material") or "material",
            "mat_id": prim.get("mat_id", -1),
            "indices": [vi + cur for vi in prim["indices"]],
            "n_verts": nv,
        })
        cur += nv

    # merged buffer accessors
    pv = append_buf(struct.pack("<%df" % len(pos_raw), *pos_raw))
    pa = add_accessor(pv, total_v, "VEC3")
    xs = pos_raw[0::3]; ys = pos_raw[1::3]; zs = pos_raw[2::3]
    if xs:
        accessors[pa]["min"] = [min(xs), min(ys), min(zs)]
        accessors[pa]["max"] = [max(xs), max(ys), max(zs)]

    attributes = {"POSITION": pa}

    if all_nrm_has and nrm_raw:
        nv2 = append_buf(struct.pack("<%df" % len(nrm_raw), *nrm_raw))
        attributes["NORMAL"] = add_accessor(nv2, total_v, "VEC3")

    if all_uv_has and uv_raw:
        uv_v = append_buf(struct.pack("<%df" % len(uv_raw), *uv_raw))
        attributes["TEXCOORD_0"] = add_accessor(uv_v, total_v, "VEC2")

    if all_col_has and col_raw:
        col_v = append_buf(struct.pack("<%df" % len(col_raw), *col_raw))
        attributes["COLOR_0"] = add_accessor(col_v, total_v, "VEC4")

    if all_tan_has and tan_raw:
        tan_v = append_buf(struct.pack("<%df" % len(tan_raw), *tan_raw))
        attributes["TANGENT"] = add_accessor(tan_v, total_v, "VEC4")

    gltf_mesh = {"name": "model", "primitives": []}
    for pm in prim_meta:
        idx_raw = pm["indices"]
        iv = append_buf(struct.pack("<%dH" % len(idx_raw), *idx_raw))
        ia = add_accessor(iv, len(idx_raw), "SCALAR", comp_type=5123)
        gltf_mesh["primitives"].append({
            "attributes": dict(attributes),
            "indices": ia,
            "_mat_name": pm["mat_name"],
            "_mat_id": pm["mat_id"],
        })

    nodes.append({"name": "model", "mesh": 0})

    gltf = {
        "asset": {"version": "2.0", "generator": "CrisTical crygltf.py 1.2 (static)"},
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": [gltf_mesh],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(buf)}],
    }

    return gltf, buf
