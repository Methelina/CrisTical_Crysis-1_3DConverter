#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crychr.py — CrisTical: Crysis 1 .chr / .cgf chunk file reader
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

=== Crysis binary chunk file + compiled character (CHR) parser ===

Parses Crysis 1 compiled character files (.chr) using the stream-based
format (Mesh v0800 + DataStream v0800).  Bone data comes from
ChunkType_CompiledBones (0xACDC0000, v0800).  Geometry comes from the
mesh chunk + stream data chunk + MeshSubsets v0800.

Chunk layouts determined by independent analysis of sample .chr files.

Exposed API:
  read_chr(path) -> dict
    Returns skeleton (list of dicts: name, parent, bind_abs, bind_rel, ...)
    and mesh (list of primitives: positions, normals, uvs, indices,
    joints, weights, material_name).
"""

import struct
import os
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Chunk type constants
# ---------------------------------------------------------------------------
CC_CompiledBones         = 0xACDC0000
CC_CompiledPhyBones      = 0xACDC0001
CC_CompiledMorphTargets  = 0xACDC0002
CC_CompiledPhyProxies    = 0xACDC0003
CC_CompiledIntFaces      = 0xACDC0004
CC_CompiledIntSkinVerts  = 0xACDC0005
CC_CompiledExt2IntMap    = 0xACDC0006
CC_Mesh                  = 0xCCCC0000
CC_MtlName               = 0xCCCC0014
CC_DataStream            = 0xCCCC0016
CC_MeshSubsets           = 0xCCCC0017
CC_Node                  = 0xCCCC000B

STREAM_POSITIONS  = 0
STREAM_NORMALS    = 1
STREAM_TEXCOORDS  = 2
STREAM_INDICES    = 5
STREAM_TANGENTS   = 6
STREAM_BONEMAP    = 9

LEN_CompiledBone = 584
LEN_MeshDesc0800 = 276
LEN_SkinVertHdr  = 48
LEN_Ext2IntMapHdr = 16


def _chunk_table(raw):
    sig = raw[:6]
    if sig != b"CryTek":
        raise ValueError("not a Crysis binary chunk file")
    ft, fv, cto, nch = struct.unpack_from("<IIII", raw, 8)
    entry_size = 20 if fv == 0x0745 else 16
    chunks = []
    for i in range(nch):
        t, v, o, cid = struct.unpack_from("<IIII", raw, cto + 4 + i * entry_size)
        chunks.append((t, v, o, cid))
    return chunks


def _read_skeleton(raw, chunks):
    target = None
    for t, v, o, cid in chunks:
        if t == CC_CompiledBones and v == 0x0800:
            target = o + 48
            break
    if target is None:
        raise ValueError("CompiledBones chunk not found")

    bones = []
    num_bones = 0
    pos = target
    while pos + LEN_CompiledBone <= len(raw):
        cid = struct.unpack_from("<I", raw, pos)[0]
        w2b = struct.unpack_from("<12f", raw, pos + 0xD8)
        b2w = struct.unpack_from("<12f", raw, pos + 0x108)
        name_raw = raw[pos + 0x138:pos + 0x138 + 256]
        name = name_raw.split(b"\x00")[0].decode("ascii", "replace")
        offset_parent = struct.unpack_from("<i", raw, pos + 0x23C)[0]
        bones.append({
            "name": name,
            "controller_id": cid,
            "w2b": list(w2b),
            "b2w": list(b2w),
            "offset_parent": offset_parent,
            "index": num_bones,
        })
        num_bones += 1
        pos += LEN_CompiledBone
        if not name:
            bones.pop()
            break
    return bones


def bone_parent_index(bones):
    for i, b in enumerate(bones):
        off = b["offset_parent"]
        b["parent"] = i + off if off != 0 else -1
    return bones


def _find_chunk(chunks, ctype, cid=None):
    for t, v, o, cid2 in chunks:
        if t == ctype and (cid is None or cid2 == cid):
            return o, v
    return None, None


def _field(raw, off, fmt, n=1):
    vals = struct.unpack_from("<" + fmt * n, raw, off)
    return vals if n > 1 else vals[0]


def _stream_data(raw, chunks, chunk_id):
    o, _ = _find_chunk(chunks, CC_DataStream, chunk_id)
    if o is None:
        return None, 0, b""
    nf, st, cnt, esz = struct.unpack_from("<iiii", raw, o + 16)
    return st, cnt, raw[o + 40:o + 40 + cnt * esz]


def _read_poses_from_b2w(bones):
    import math

    def m34_to_quat(b2w):
        m00, m01, m02, m03, m10, m11, m12, m13, m20, m21, m22, m23 = b2w
        trace = m00 + m11 + m22
        if trace > 0.0:
            s = math.sqrt(trace + 1.0) * 2.0
            w = 0.25 * s
            x = (m21 - m12) / s
            y = (m02 - m20) / s
            z = (m10 - m01) / s
        elif m00 > m11 and m00 > m22:
            s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
            w = (m21 - m12) / s
            x = 0.25 * s
            y = (m01 + m10) / s
            z = (m02 + m20) / s
        elif m11 > m22:
            s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
            w = (m02 - m20) / s
            x = (m01 + m10) / s
            y = 0.25 * s
            z = (m12 + m21) / s
        else:
            s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
            w = (m10 - m01) / s
            x = (m02 + m20) / s
            y = (m12 + m21) / s
            z = 0.25 * s
        nrm = math.sqrt(x * x + y * y + z * z + w * w)
        return (x / nrm, y / nrm, z / nrm, w / nrm), (m03, m13, m23)

    def quat_rotate(q, v):
        x, y, z, w = q
        tx = 2.0 * (y * v[2] - z * v[1])
        ty = 2.0 * (z * v[0] - x * v[2])
        tz = 2.0 * (x * v[1] - y * v[0])
        return (v[0] + w * tx + y * tz - z * ty,
                v[1] + w * ty + z * tx - x * tz,
                v[2] + w * tz + x * ty - y * tx)

    def quat_mul(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return (aw * bx + ax * bw + ay * bz - az * by,
                aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw,
                aw * bw - ax * bx - ay * by - az * bz)

    def quat_conj(q):
        return (-q[0], -q[1], -q[2], q[3])

    def quat_inv_mul(a, b):
        qp, tp = a
        qc, tc = b
        qp_c = quat_conj(qp)
        rq = quat_mul(qp_c, qc)
        delta = (tc[0] - tp[0], tc[1] - tp[1], tc[2] - tp[2])
        rr = quat_rotate(qp_c, delta)
        return rq, rr

    wq = []; wt = []; lq = []; lt = []
    for b in bones:
        q, t = m34_to_quat(b["b2w"])
        wq.append(q)
        wt.append(t)
    for i, b in enumerate(bones):
        p = b["parent"]
        if p < 0:
            lq.append(wq[i])
            lt.append(wt[i])
        else:
            rq, r = quat_inv_mul((wq[p], wt[p]), (wq[i], wt[i]))
            lq.append(rq)
            lt.append(r)
    return wq, wt, lq, lt


def _read_materials(raw, chunks):
    mtl_list = []
    for t, v, o, cid in chunks:
        if t == CC_MtlName and v == 0x0800:
            name_raw = raw[o + 24:o + 24 + 128]
            name = name_raw.split(b"\x00")[0].decode("ascii", "replace")
            nsub = struct.unpack_from("<i", raw, o + 24 + 128 + 4)[0]
            mtl_list.append({"chunk_id": cid, "name": name, "sub_count": nsub})
    mtl_list.sort(key=lambda m: m["chunk_id"])
    if len(mtl_list) > 1:
        return [m["name"] for m in mtl_list[1:]]
    return [_m["name"] for _m in mtl_list]


def _read_mesh(raw, chunks):
    mesh_o, _ = _find_chunk(chunks, CC_Mesh)
    if mesh_o is None:
        raise ValueError("Mesh chunk not found")
    body = mesh_o + 16
    nVerts     = _field(raw, body + 8, "i")
    nIndices   = _field(raw, body + 12, "i")
    nSubsets   = _field(raw, body + 16, "i")
    ss_chunk_id = _field(raw, body + 20, "i")
    stream_ids = [_field(raw, body + 28 + i * 4, "i") for i in range(16)]

    sup_off, _ = _find_chunk(chunks, CC_MeshSubsets, ss_chunk_id)
    if sup_off is None:
        raise ValueError("MeshSubsets chunk not found")
    sup_body = sup_off + 16
    ss_flags = _field(raw, sup_body, "i")
    ss_count = _field(raw, sup_body + 4, "i")

    subsets = []
    sp = sup_body + 16
    for _ in range(ss_count):
        fi, ni, fv, nv, mid = struct.unpack_from("<5i", raw, sp)
        subsets.append({"first_idx": fi, "num_idx": ni,
                         "first_vert": fv, "num_vert": nv,
                         "mat_id": mid})
        sp += 36

    mesh_bone_ids = []
    sp2 = sp
    if ss_flags & 2:
        for _ in range(ss_count):
            num = struct.unpack_from("<I", raw, sp2)[0]
            ids = struct.unpack_from("<%dH" % min(num, 128), raw, sp2 + 4)
            mesh_bone_ids.append(ids)
            sp2 += 4 + 256
    else:
        mesh_bone_ids = [tuple(range(256))] * ss_count

    streams = {}
    for stype, cid in enumerate(stream_ids):
        if cid <= 0:
            continue
        _, cnt, data = _stream_data(raw, chunks, cid)
        if data:
            streams[stype] = (cnt, data)

    pos_data = streams.get(STREAM_POSITIONS, (0, b""))[1]
    nrm_data = streams.get(STREAM_NORMALS, (0, b""))[1]
    uv_data  = streams.get(STREAM_TEXCOORDS, (0, b""))[1]
    idx_data = streams.get(STREAM_INDICES, (0, b""))[1]

    materials = _read_materials(raw, chunks)

    isv_offset, _ = _find_chunk(chunks, CC_CompiledIntSkinVerts)
    if isv_offset is None:
        raise ValueError("CompiledIntSkinVertices chunk not found")
    isv_start = isv_offset + 16 + 32
    isv_data_end = 0
    for t2, v2, o2, _ in chunks:
        if o2 > isv_offset:
            isv_data_end = o2
            break
    if isv_data_end == 0:
        isv_data_end = len(raw)
    isv_size = isv_data_end - isv_offset
    num_int_verts = (isv_size - 32) // 64
    int_bone_ids = []
    int_weights = []
    for vi in range(num_int_verts):
        pos = isv_start + vi * 64 + 36
        bids = struct.unpack_from("<4H", raw, pos)
        wts  = struct.unpack_from("<4f", raw, pos + 8)
        int_bone_ids.append(bids)
        int_weights.append(wts)

    ext_offset, _ = _find_chunk(chunks, CC_CompiledExt2IntMap)
    if ext_offset is None:
        raise ValueError("CompiledExt2IntMap chunk not found")
    ext_start = ext_offset + 16
    num_ext_verts = nVerts
    ext_to_int = struct.unpack_from("<%dH" % num_ext_verts, raw, ext_start)

    global_joints = []
    global_weights = []
    for v in range(nVerts):
        i = ext_to_int[v]
        if 0 <= i < num_int_verts:
            global_joints.append([int(b) for b in int_bone_ids[i]])
            global_weights.append(list(int_weights[i]))
        else:
            global_joints.append([0, 0, 0, 0])
            global_weights.append([1.0, 0.0, 0.0, 0.0])

    positions = []
    for i in range(nVerts):
        positions.append(struct.unpack_from("<3f", pos_data, i * 12))

    normals = []
    for i in range(nVerts):
        normals.append(struct.unpack_from("<3f", nrm_data, i * 12))

    uvs = []
    for i in range(nVerts):
        uvs.append(struct.unpack_from("<2f", uv_data, i * 8))

    primitives = []
    for si, ss in enumerate(subsets):
        fi, ni, fv, nv, mat_id = (
            ss["first_idx"], ss["num_idx"], ss["first_vert"], ss["num_vert"],
            ss["mat_id"])

        indices = []
        for vi in range(fi, fi + ni):
            idx = struct.unpack_from("<H", idx_data, vi * 2)[0]
            indices.append(idx)

        mat_name = materials[mat_id] if mat_id < len(materials) else "material_%d" % mat_id

        primitives.append({
            "positions": positions,
            "normals": normals,
            "uvs": uvs,
            "joints": global_joints,
            "weights": global_weights,
            "indices": indices,
            "material": mat_name,
            "mat_id": mat_id,
        })

    return {
        "primitives": primitives,
        "num_verts_global": nVerts,
    }


def read_chr(path):
    with open(path, "rb") as f:
        raw = f.read()
    chunks = _chunk_table(raw)

    bones = _read_skeleton(raw, chunks)
    bone_parent_index(bones)
    wq, wt, lq, lt = _read_poses_from_b2w(bones)
    for i, b in enumerate(bones):
        b["world_quat"] = wq[i]
        b["world_trans"] = wt[i]
        b["local_quat"] = lq[i]
        b["local_trans"] = lt[i]

    mesh = _read_mesh(raw, chunks)

    return {"skeleton": bones, "mesh": mesh}


# ---------------------------------------------------------------------------
# CDF (Character Definition File) — multi-part character assembly
# ---------------------------------------------------------------------------

def _find_chr_relative(base_dir, file_ref):
    ref = file_ref.replace("/", os.sep).replace("\\", os.sep)
    if os.path.isabs(ref):
        return ref if os.path.isfile(ref) else None
    path = os.path.join(base_dir, ref)
    if os.path.isfile(path):
        return path
    parts = ref.split(os.sep)
    for d in [base_dir] + [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]:
        test = os.path.join(d, ref)
        if os.path.isfile(test):
            return test
    cur = base_dir
    for _ in range(6):
        test = os.path.join(cur, ref)
        if os.path.isfile(test):
            return test
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def _vfs_materialize(ref, game_dirs):
    """Resolve a CDF-relative reference through the mounted game VFS.

    Returns a real on-disk path (materialized into the shared geometry temp
    dir) or None when the VFS has no such entry / no game dirs given.
    """
    if not game_dirs:
        return None
    try:
        from .cryvfs import mount_gamedirs, materialize
        from .path_resolve import PROJ_TEMP_GEOM
    except ImportError:
        from cryvfs import mount_gamedirs, materialize
        from path_resolve import PROJ_TEMP_GEOM
    norm = ref.replace("\\", "/")
    vfs = mount_gamedirs([str(d) for d in game_dirs])
    return materialize(vfs, norm, PROJ_TEMP_GEOM)


def read_cdf(cdf_path, game_dirs=None):
    try:
        from .cryxmlb import load_file as _load_xml
    except ImportError:
        from cryxmlb import load_file as _load_xml
    root = _load_xml(cdf_path)
    base_dir = os.path.dirname(os.path.abspath(cdf_path))

    model_el = root.find("Model")
    model_path = None
    model_ref = None
    if model_el is not None:
        ref = model_el.get("File", "")
        if ref:
            model_ref = ref.replace("\\", "/")
            model_path = _find_chr_relative(base_dir, ref)
            if model_path is None:
                # Pak-only CDF: the .chr lives inside a game .pak — resolve
                # the reference through the mounted VFS instead.
                model_path = _vfs_materialize(ref, game_dirs)

    skins = []
    bones = []
    attachments = []
    att_list = root.find("AttachmentList")
    if att_list is not None:
        for att in att_list.findall("Attachment"):
            atype = att.get("Type", "")
            aname = att.get("AName", "")
            binding = att.get("Binding", "")
            bone_name = att.get("BoneName", "")
            rec = {
                "type": atype,
                "name": aname,
                "bone_name": bone_name,
                "binding": binding.replace("\\", "/") if binding else "",
                "material": (att.get("Material", "") or "").replace("\\", "/"),
                "rotation": att.get("Rotation", ""),
                "position": att.get("Position", ""),
            }
            attachments.append(rec)
            if atype == "CA_SKIN" and binding:
                path = _find_chr_relative(base_dir, binding)
                if path is None:
                    path = _vfs_materialize(binding, game_dirs)
                if path:
                    skins.append((aname, path))
            elif atype == "CA_BONE" and bone_name:
                bones.append((aname, bone_name))

    return {
        "model_path": model_path,
        "model_ref": model_ref,
        "skin_attachments": skins,
        "bone_attachments": bones,
        "attachments": attachments,
    }


def read_chr_or_cdf(path, game_dirs=None):
    if path.lower().endswith(".cdf"):
        cdf = read_cdf(path, game_dirs)
        if not cdf["model_path"]:
            raise ValueError("CDF has no Model: %s" % path)

        main_data = read_chr(cdf["model_path"])
        main_bones = main_data["skeleton"]
        all_prims = list(main_data["mesh"]["primitives"])

        # Each CA_SKIN attachment carries its OWN bone list whose index order
        # does not necessarily match the main .chr skeleton (C2/C3 characters
        # often insert extra helper bones). Skin vertices weight by that local
        # order, so before merging we must remap every joint index onto the
        # main skeleton by bone NAME — otherwise body parts deform under the
        # wrong joints and look "static/chaotic" when animating.
        main_name_idx = {}
        for _i, _b in enumerate(main_bones):
            main_name_idx.setdefault((_b.get("name") or "").lower(), _i)
        _root_idx = 0
        for _i, _b in enumerate(main_bones):
            if _b.get("parent", -1) < 0:
                _root_idx = _i
                break

        def _remap_skin_joints(prim, att_skeleton):
            if not prim.get("joints"):
                return
            local_idx = {}
            for _i, _b in enumerate(att_skeleton):
                local_idx.setdefault((_b.get("name") or "").lower(), _i)
            new_joints = []
            n_missing = 0
            for j4 in prim["joints"]:
                out = []
                for jj in j4:
                    bname = None
                    if 0 <= jj < len(att_skeleton):
                        bname = (att_skeleton[jj].get("name") or "").lower()
                    mi = main_name_idx.get(bname) if bname else None
                    if mi is None:
                        mi = _root_idx
                        n_missing += 1
                    out.append(mi)
                new_joints.append(out)
            prim["joints"] = new_joints
            return n_missing

        if cdf["skin_attachments"]:
            print("  [cdf] model: %s (%d bones)" % (
                os.path.basename(cdf["model_path"]), len(main_bones)))
            for att_name, chr_path in cdf["skin_attachments"]:
                try:
                    att_data = read_chr(chr_path)
                    for p in att_data["mesh"]["primitives"]:
                        p.setdefault("_cdf_attachment", att_name)
                        p.setdefault("_cdf_chr_path", chr_path)
                        _remap_skin_joints(p, att_data["skeleton"])
                        all_prims.append(p)
                    att_bones = len(att_data["skeleton"])
                    att_prims = len(att_data["mesh"]["primitives"])
                    print("  [cdf]   + %-20s <- %s  (%d bones, %d prims)" % (
                        att_name, os.path.basename(chr_path), att_bones, att_prims))
                except Exception as e:
                    print("  [cdf]   WARN: %s — %s" % (att_name, e))
            print("  [cdf] total: %d primitives" % len(all_prims))

        return {"skeleton": main_bones, "mesh": {"primitives": all_prims}}

    return read_chr(path)
