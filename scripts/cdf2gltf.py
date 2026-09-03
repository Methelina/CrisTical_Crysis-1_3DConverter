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
    read_cgf_meshes,
)
from cristical_core.mtl_resolve import resolve_mtl
from cristical_core.crylmg import parse_lmg
from cristical_core.path_resolve import resolve_geometry_path
from cristical_core.game_profile import GameTitle, classify_game_dir

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


# ---------------------------------------------------------------------------
# CA_BONE attachment placement policy, keyed by game edition (reuses the
# canonical resolver game_profile.classify_game_dir — do not duplicate).
#
# CA_BONE attachment .cgf geometry is authored in a frame that varies by
# game edition, so how we lay a static piece over its anchor bone is a
# per-title decision, not a universal heuristic:
#
#   * Crysis 2 — pieces are authored around their OWN origin (gun,
#     helmet, chest plates are small and centred near 0). They must be
#     LIFTED to the anchor bone's bind pose.
#   * Crysis 3 — pieces are authored in the character's MODEL space
#     (armour already sits on the body). Lifting would double the offset and
#     push them into the air; they are placed RAW.
#   * Crysis 1 / Remastered / Warhead / Wars — no armour attachments in the
#     corpus; falls back to geometric auto-detection.
#
# The geometric auto-detect (compact-and-near-origin -> lift, else raw) is kept
# as the default for undetermined titles and as a cross-check. An explicit
# override is available via CRITICAL_CA_BONE_FRAME=lift|raw|auto.
# ---------------------------------------------------------------------------

_CA_BONE_FRAME_BY_TITLE = {
    GameTitle.CRYSIS_1: "lift",
    GameTitle.CRYSIS_WARHEAD: "lift",
    GameTitle.CRYSIS_WARS: "lift",
    GameTitle.CRYSIS_REMASTERED: "lift",
    GameTitle.CRYSIS_2: "lift",
    GameTitle.CRYSIS_3: "raw",
}


def _resolve_game_title(game_dirs):
    """Resolve the game title from the data root(s) via game_profile (the
    canonical resolver). Returns a GameTitle or None when undetermined."""
    if not game_dirs:
        return None
    for d in game_dirs:
        try:
            info = classify_game_dir(str(d))
        except Exception:
            continue
        title = info.get("title")
        if title is not None:
            return title
    return None


# Backwards-compatible alias for the CA_BONE policy block.
_ca_bone_title = _resolve_game_title


def _ca_bone_frame_mode(game_dirs):
    """CA_BONE placement policy for a data root, keyed by game title.

    Reuses game_profile.classify_game_dir (C1/Warhead/C2/C3/Remastered/Wars);
    C1-family (original/Remastered/Warhead/Wars) has no armour attachments in
    the corpus and falls back to geometric auto-detection. An explicit
    override wins via CRITICAL_CA_BONE_FRAME=lift|raw|auto."""
    env = (os.environ.get("CRITICAL_CA_BONE_FRAME") or "").strip().lower()
    if env in ("lift", "raw", "auto"):
        return env
    title = _resolve_game_title(game_dirs)
    if title is None:
        return "auto"
    return _CA_BONE_FRAME_BY_TITLE.get(title, "auto")


# ---------------------------------------------------------------------------
# Blend/locomotion strategy, keyed by game title. The lmg/bspace asset mix
# differs per edition and the collectors are gated on this table, so each
# title runs only the parsers its data actually needs:
#
#   lmg   — collect .lmg locomotion groups -> <chr>_locomotion.json
#   bspace — collect .bspace/.comb blend spaces -> <chr>_blends.json
#
# The "format" tag is written into the sidecar JSON so downstream consumers
# know which schema (legacy C1 Position-based, hybrid C2 Dimensions-based)
# the .lmg files follow. For undetermined titles both collectors run (auto).
# ---------------------------------------------------------------------------

_BLEND_LOCO_BY_TITLE = {
    GameTitle.CRYSIS_1: {"lmg": True, "bspace": False, "format": "legacy_lmg"},
    GameTitle.CRYSIS_WARHEAD: {"lmg": True, "bspace": False, "format": "legacy_lmg"},
    GameTitle.CRYSIS_REMASTERED: {"lmg": True, "bspace": False, "format": "legacy_lmg"},
    GameTitle.CRYSIS_WARS: {"lmg": True, "bspace": False, "format": "legacy_lmg"},
    GameTitle.CRYSIS_2: {"lmg": True, "bspace": True, "format": "hybrid_lmg"},
    GameTitle.CRYSIS_3: {"lmg": False, "bspace": True, "format": "bspace"},
}

_BLEND_LOCO_AUTO = {"lmg": True, "bspace": True, "format": "auto"}


def _blend_loco_strategy(game_dirs):
    """Blend/locomotion collector strategy for a data root, keyed by title.

    Returns the dict from _BLEND_LOCO_BY_TITLE or _BLEND_LOCO_AUTO when the
    title cannot be determined (both collectors run as a safe default)."""
    title = _resolve_game_title(game_dirs)
    if title is None:
        return dict(_BLEND_LOCO_AUTO)
    return dict(_BLEND_LOCO_BY_TITLE.get(title, _BLEND_LOCO_AUTO))


def _q_conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def _q_mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _q_rot(q, v):
    x, y, z, w = q
    ux, uy, uz = x, y, z
    cx = w * (uy * v[2] - uz * v[1])
    cy = w * (uz * v[0] - ux * v[2])
    cz = w * (ux * v[1] - uy * v[0])
    return (
        v[0] + 2 * (uy * cz - uz * cy),
        v[1] + 2 * (uz * cx - ux * cz),
        v[2] + 2 * (ux * cy - uy * cx),
    )


def _quat_wxyz(s):
    """'w,x,y,z' engine quaternion string -> (x,y,z,w)."""
    try:
        w, x, y, z = [float(t) for t in (s or "").split(",")]
    except (ValueError, TypeError):
        return (0.0, 0.0, 0.0, 1.0)
    return (x, y, z, w)


def _vec3(s):
    try:
        x, y, z = [float(t) for t in (s or "").split(",")]
    except (ValueError, TypeError):
        return (0.0, 0.0, 0.0)
    return (x, y, z)


def _bone_world_pose(bones, i):
    """Engine-space world (rot quat xyzw, pos) of bone i via the parent chain."""
    n = len(bones)
    pose = [None] * n

    def resolve(idx):
        if pose[idx] is not None:
            return pose[idx]
        b = bones[idx]
        lq = tuple(b["local_quat"])
        lt = tuple(b["local_trans"])
        p = b.get("parent", -1)
        if p is None or p < 0 or p >= idx:
            pose[idx] = (lq, lt)
            return pose[idx]
        pq, pp = resolve(p)
        pose[idx] = (_q_mul(pq, lq),
                     tuple(_q_rot(pq, lt)[k] + pp[k] for k in range(3)))
        return pose[idx]

    return resolve(i)


def quat_rot(q, v):
    """Rotate vector v by unit quaternion q (x,y,z,w), glTF frame."""
    x, y, z, w = q
    ux, uy, uz = x, y, z
    cx = w * (uy * v[2] - uz * v[1])
    cy = w * (uz * v[0] - ux * v[2])
    cz = w * (ux * v[1] - uy * v[0])
    return (v[0] + 2 * (uy * cz - uz * cy),
            v[1] + 2 * (uz * cx - ux * cz),
            v[2] + 2 * (ux * cy - uy * cx))


def _emit_ca_bone_child_nodes(gltf, buf, pieces):
    """Emit CA_BONE .cgf pieces as separate child mesh nodes under their joint.

    Joint-child mode (used for C2 origin-authored pieces): the .cgf
    geometry keeps its authored coordinates and is attached as an unskinned
    mesh node whose parent is the anchor joint node, so it inherits the joint's
    animation instead of baking the bind translation into the vertices. A yaw
    about the vertical (glTF Y) is applied per piece to fix authored 90-deg
    orientation mismatches (armour on Head/Spine04 vs the aligned weapon).
    """
    import math
    nodes = gltf.setdefault("nodes", [])
    gltf.setdefault("meshes", [])
    gltf.setdefault("accessors", [])
    gltf.setdefault("bufferViews", [])
    ax_p = lambda p: (-p[0], p[2], p[1])
    ax_n = lambda n: (-n[0], n[2], n[1])

    def rot_y(p, rad):
        c = math.cos(rad); s = math.sin(rad)
        return (p[0] * c + p[2] * s, p[1], -p[0] * s + p[2] * c)

    def rot_x(p, rad):
        c = math.cos(rad); s = math.sin(rad)
        return (p[0], p[1] * c - p[2] * s, p[1] * s + p[2] * c)

    def push(floats):
        while len(buf) % 4:
            buf.append(0)
        off = len(buf)
        buf.extend(struct.pack("<%df" % len(floats), *floats))
        gltf["bufferViews"].append({"buffer": 0, "byteOffset": off,
                                    "byteLength": len(floats) * 4})
        return len(gltf["bufferViews"]) - 1

    def push_u16(vals):
        while len(buf) % 4:
            buf.append(0)
        off = len(buf)
        buf.extend(struct.pack("<%dH" % len(vals), *vals))
        gltf["bufferViews"].append({"buffer": 0, "byteOffset": off,
                                    "byteLength": len(vals) * 2})
        return len(gltf["bufferViews"]) - 1

    def acc(bv, count, atype, ct=5126, mn=None, mx=None):
        a = {"bufferView": bv, "byteOffset": 0, "componentType": ct,
             "count": count, "type": atype}
        if mn is not None:
            a["min"] = mn
        if mx is not None:
            a["max"] = mx
        gltf["accessors"].append(a)
        return len(gltf["accessors"]) - 1

    n_child = 0
    for piece in pieces:
        bone_node = piece["bone_idx"]  # bone index == glTF joint node index
        # Correct child node placement (engine-faithful): the child sits at the
        # joint (inheriting its bind POSITION) but carries a LOCAL rotation equal
        # to the conjugate of the joint's bind rotation, so at rest the piece
        # keeps its authored orientation (CE35: AttRelativeDefault = bind^-1 *
        # AttAbsoluteDefault). Geometry is NOT rotated.
        parent = nodes[bone_node] if 0 <= bone_node < len(nodes) else {}
        qbind = parent.get("rotation", (0.0, 0.0, 0.0, 1.0))
        qchild = (-qbind[0], -qbind[1], -qbind[2], qbind[3])
        mesh_children = []
        name = piece["name"]
        for cp in piece["prims"]:
            pos = [ax_p(v) for v in cp["positions"]]
            nv = len(pos); ni = len(cp["indices"])
            pv = push([c for v in pos for c in v])
            pa = acc(pv, nv, "VEC3",
                     mn=[min(v[0] for v in pos), min(v[1] for v in pos), min(v[2] for v in pos)],
                     mx=[max(v[0] for v in pos), max(v[1] for v in pos), max(v[2] for v in pos)])
            attrs = {"POSITION": pa}
            if cp.get("normals"):
                nrm = [ax_n(v) for v in cp["normals"]]
                attrs["NORMAL"] = acc(push([c for n in nrm for c in n]), nv, "VEC3")
            if cp.get("uvs"):
                uv = list(cp["uvs"])
                attrs["TEXCOORD_0"] = acc(push([c for u in uv for c in u]), nv, "VEC2")
            ia = acc(push_u16([int(x) for x in cp["indices"]]), ni, "SCALAR", ct=5123)
            mi = len(gltf["meshes"])
            gltf["meshes"].append({"name": name,
                                   "primitives": [{"attributes": attrs,
                                                   "indices": ia, "mode": 4,
                                                   "_mat_id": cp.get("mat_id", 0)}]})
            nn = len(nodes)
            ident = (abs(qchild[3] - 1.0) < 1e-6 and all(abs(x) < 1e-9 for x in qchild[:3]))
            nd = {"name": name + "_mesh", "mesh": mi}
            if not ident:
                nd["rotation"] = list(qchild)
            nodes.append(nd)
            mesh_children.append(nn)
        if mesh_children and 0 <= bone_node < len(nodes):
            nodes[bone_node].setdefault("children", []).extend(mesh_children)
            n_child += len(mesh_children)
    return n_child


def _bone_attachment_prims(bones, bone_idx, cgf_real, bind_q, bind_pos, att_name,
                           mode="auto", log=None, bind_scale=1.0, vert_scale=1.0):
    """Turn a static .cgf piece bound to a bone into skinned skin primitives.

    ``mode`` is the game-version dependent placement policy:
      - "lift": origin-authored piece, translate to the anchor bone's bind pose;
      - "raw":  model-space piece, keep authored coords;
      - "auto": geometric detection — if the authored geometry is compact and
        close to its origin (max bound < ~1.2 m) treat as origin-space ("lift"),
        otherwise model-space ("raw").
    Each vertex is weighted 1.0 to the anchor bone so it follows the animation.
    """
    if log is None:
        log = print
    prims = []
    try:
        cgf_prims = read_cgf_meshes(cgf_real)
    except Exception as e:
        log("  [cdf] WARN CA_BONE %s: %s" % (att_name, e))
        return prims
    if not cgf_prims:
        return prims
    xs = [v[0] for p in cgf_prims for v in p["positions"]]
    ys = [v[1] for p in cgf_prims for v in p["positions"]]
    zs = [v[2] for p in cgf_prims for v in p["positions"]]
    bound = max(abs(min(xs)), abs(max(xs)), abs(min(ys)), abs(max(ys)),
                abs(min(zs)), abs(max(zs))) if xs else 0.0
    if mode not in ("lift", "raw"):
        mode = "lift" if bound < 1.2 else "raw"
    do_lift = (mode == "lift")
    anchor_p = (0.0, 0.0, 0.0)
    if do_lift:
        # Engine-faithful (CE35): a bone-attached object keeps its authored
        # orientation at rest; only the bone's bind TRANSLATION places it. Use
        # the authoritative CompiledBone b2w (world_transform_matrix) translation
        # — a hand recomputation of the local chain is unreliable for C2 rigs.
        # Skinning weight-1 then reproduces joint_anim * bind^-1.
        try:
            _b2w = bones[bone_idx]["b2w"]
            anchor_p = (_b2w[3] * bind_scale, _b2w[7] * bind_scale,
                        _b2w[11] * bind_scale)
        except (IndexError, KeyError, TypeError):
            _aq, anchor_p = _bone_world_pose(bones, bone_idx)
            if bind_scale != 1.0:
                anchor_p = (anchor_p[0] * bind_scale, anchor_p[1] * bind_scale,
                            anchor_p[2] * bind_scale)
        for p in cgf_prims:
            if vert_scale != 1.0:
                _pv = [(v[0] * vert_scale, v[1] * vert_scale, v[2] * vert_scale)
                    for v in p["positions"]]
            else:
                _pv = list(p["positions"])
            if do_lift:
                pos = [(v[0] + anchor_p[0], v[1] + anchor_p[1], v[2] + anchor_p[2])
                    for v in _pv]
                nrm = list(p["normals"]) if p["normals"] else []
            else:
                pos = _pv
                nrm = list(p["normals"]) if p["normals"] else []
            if log is not None and pos:
                xs = [v[0] for v in pos]
                ys = [v[1] for v in pos]
                zs = [v[2] for v in pos]
                minx, maxx = min(xs), max(xs)
                miny, maxy = min(ys), max(ys)
                minz, maxz = min(zs), max(zs)
                log("  PRIMITIVE: att=%s, mode=%s, verts=%d, bbox=[(%f,%f,%f),(%f,%f,%f)]" % (att_name, "lift" if do_lift else "raw", len(pos), minx, miny, minz, maxx, maxy, maxz))
                # Also log anchor_p and first vertex for first primitive of this attachment
                if len(pos) > 0 and hasattr(self, '_debug_logged_att') and self._debug_logged_att != att_name:
                    # Avoid spamming: log only first primitive per attachment
                    pass
            joints = [[bone_idx, 0, 0, 0]] * len(p["positions"])
            weights = [[1.0, 0.0, 0.0, 0.0]] * len(p["positions"])
            prims.append({
                "positions": pos,
                "normals": nrm,
                "uvs": p["uvs"],
                "indices": p["indices"],
                "joints": joints,
                "weights": weights,
                "mat_id": p.get("mat_id", 0),
                "material": p.get("material", ""),
                "_cdf_attachment": att_name,
                "_cdf_chr_path": cgf_real,
            })
    return prims


def _materialize_cgf(binding, game_dirs):
    """Resolve a CA_BONE .cgf binding to a real file (loose or from pak)."""
    rel = binding.replace("\\", "/").lstrip("/")
    try:
        from cristical_core.cryvfs import mount_game, index_open_bytes
    except ImportError:
        from cryvfs import mount_game, index_open_bytes
    idx = mount_game([str(d) for d in game_dirs]) if game_dirs else None
    if idx is None:
        return None
    rec = idx.get(rel)
    if rec is None:
        return None
    if rec["kind"] == "loose":
        return rec["real"]
    return _materialize_index(idx, rel, os.path.join(_PROJ_TEMP, "cdf_cgf"))


def parse_cal_text(text):
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("//"):
            continue
        m = re.match(r"^\$(\w+)\s*=\s*(.+)$", line)
        if m:
            result[m.group(1)] = m.group(2).strip()
    return result


def parse_cal(cal_path):
    result = {}
    if not os.path.isfile(cal_path):
        return result
    with open(cal_path, "r", encoding="utf-8", errors="replace") as f:
        return parse_cal_text(f.read())


def _materialize_index(idx, rel, temp_dir):
    """Materialize a VFSIndex path (pak entry) to a real file under temp_dir.
    Loose entries are the caller's concern; this writes pak bytes once."""
    import hashlib
    os.makedirs(temp_dir, exist_ok=True)
    base = rel.rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(base)
    digest = hashlib.md5(rel.encode("utf-8")).hexdigest()[:8]
    out = os.path.join(temp_dir, "%s_%s%s" % (stem, digest, ext))
    if not os.path.isfile(out):
        try:
            from cristical_core.cryvfs import index_open_bytes
        except ImportError:
            from cryvfs import index_open_bytes
        with open(out, "wb") as f:
            f.write(index_open_bytes(idx, rel))
    return out


def resolve_dba(dba_rel, game_dirs):
    """Resolve a .dba to a real on-disk path via the single shared VFSIndex
    (loose files and every .pak). ``dba_rel`` is a game-relative path read
    from a .cal (e.g. ``Animations/human/male/.../x.dba``)."""
    if not game_dirs:
        return None
    try:
        from cristical_core.cryvfs import mount_game, index_open_bytes
    except ImportError:
        from cryvfs import mount_game, index_open_bytes
    idx = mount_game([str(d) for d in game_dirs])
    rel = dba_rel.replace("\\", "/").lstrip("/")
    rec = idx.get(rel)
    if rec is None:
        return None
    real = rec["real"] if rec["kind"] == "loose" else _materialize_index(idx, rel, _PROJ_TEMP)
    try:
        dba = read_dba(real)
        if dba and dba.animations:
            return real
    except Exception:
        pass
    return None


def _rel_of(model_path, game_dirs):
    """Game-root-relative VFS path for a real on-disk model path, or None if
    the model is not inside any configured game root (e.g. a pak materialized
    to temp — callers then supply the original virtual path)."""
    mp = os.path.abspath(model_path).replace("\\", "/")
    for d in game_dirs:
        d2 = os.path.abspath(str(d)).replace("\\", "/").rstrip("/")
        if mp.lower().startswith(d2.lower() + "/"):
            return mp[len(d2) + 1:]
    return None


def _resolve_chrparams_cafs(chr_path, game_dirs, L, virtual=None):
    """Roadmap 4.3: resolve .caf clips referenced by the .chrparams next to
    a character, through the single shared VFSIndex.

    ``virtual`` is the model's game-relative VFS path (for pak inputs);
    otherwise it is derived from ``chr_path`` when that real file lies inside
    a game root. Returns a list of REAL file paths (VFS entries are
    materialized) usable by the --caf injection stage, or [].
    """
    if not game_dirs:
        return []
    try:
        from cristical_core.cryvfs import mount_game, index_open_bytes
        from cristical_core.crychrparams import clips_for_fs
    except ImportError:
        from cryvfs import mount_game, index_open_bytes
        from crychrparams import clips_for_fs
    chr_rel = virtual
    if not chr_rel:
        chr_rel = _rel_of(chr_path, game_dirs)
    if not chr_rel:
        return []
    idx = mount_game([str(d) for d in game_dirs])
    try:
        refs = clips_for_fs(chr_rel, idx)
    except Exception as e:
        L("  chrparams: failed (%s)" % e)
        return []
    if refs is None:
        return []
    if getattr(refs, "missing_includes", None):
        L("  chrparams: missing includes: %s" % ", ".join(refs.missing_includes))
    if getattr(refs, "empty_wildcards", None):
        L("  chrparams: empty wildcards: %s" % ", ".join(refs.empty_wildcards))
    out = []
    seen_keys = set()
    for clip_name, vfs_path, ext in refs.clips:
        if ext != ".caf":
            continue
        key = vfs_path.replace("\\", "/")
        rec = idx.get(vfs_path)
        if rec is None:
            continue
        real = rec["real"] if rec["kind"] == "loose" else _materialize_index(
            idx, vfs_path, os.path.join(_PROJ_TEMP, "caf_extract"))
        if real:
            seen_keys.add(key.lower())
            out.append(real)
    # The engine loads every clip under the character's animation root. The
    # chrparams wildcard "*\*.caf" only matches cafs one directory deep, so
    # clips living directly under the root (mastermind.stand_*.caf etc.) are
    # missed. Sweep the whole animation base dir recursively for loose .caf
    # clips and merge any that clips_for_fs did not already resolve.
    base = getattr(refs, "animation_base_path", None)
    if base:
        base = base.replace("\\", "/").strip("/")
        prefix = base + "/"
        for key in idx.keys():
            k = key.replace("\\", "/")
            if not k.lower().startswith(prefix.lower()):
                continue
            if not k.lower().endswith(".caf"):
                continue
            if k.lower() in seen_keys:
                continue
            rec = idx.get(key)
            if rec is None:
                continue
            real = rec["real"] if rec["kind"] == "loose" else _materialize_index(
                idx, key, os.path.join(_PROJ_TEMP, "caf_extract"))
            if real:
                seen_keys.add(k.lower())
                out.append(real)
    return out


def collect_lmg_refs(chr_path, game_dirs, virtual=None):
    """Collect locomotion-group (.lmg) references from the .chrparams next to a character.

    The .chrparams XML holds an ``<Animation name="#filepath" path="...">`` entry
    defining the animation root, plus ``<Animation name="..." path=".../*.lmg"/>``
    entries whose path is resolved relative to that root and the game directory.

    The sibling .chrparams is looked up through the shared VFS mount, so it may
    live inside a .pak (the .chr itself is usually materialized to temp during
    a run — its sibling never exists on disk then). Include chains are followed
    via crychrparams.load_chrparams_with_includes, so .lmg entries declared in
    an included chrparams (grunt: grunt_base.chrparams -> $Include ->
    animations/alien/grunt/grunt.chrparams) are picked up as well.

    For each existing .lmg the file is parsed via :func:`parse_lmg`; broken
    references are reported with ``file: None`` so callers can surface them.

    Args:
        chr_path: resolved path to the character .chr file.
        game_dirs: list of candidate game-root directories.
        virtual: the character's game-relative VFS path (for pak inputs);
            derived from ``chr_path`` when omitted and it lies in a game root.

    Returns:
        A dict ``{"source": <chrparams basename>, "filepath": <anim root>,
        "groups": [...]}`` or ``None`` when no .chrparams file exists next to
        ``chr_path``.
    """
    if not game_dirs:
        return None

    try:
        from cristical_core.cryvfs import mount_game, mount_gamedirs
        from cristical_core.crychrparams import load_chrparams_with_includes
    except ImportError:
        from cryvfs import mount_game, mount_gamedirs
        from crychrparams import load_chrparams_with_includes

    fs = mount_gamedirs([str(d) for d in game_dirs])
    idx = mount_game([str(d) for d in game_dirs])

    # Sibling .chrparams lookup: virtual path first (pak models), then the
    # real disk sibling (loose models).
    chr_rel = virtual
    if not chr_rel:
        chr_rel = _rel_of(chr_path, game_dirs)
    chrparams_rel = None
    if chr_rel:
        cand = os.path.splitext(chr_rel.replace("\\", "/"))[0] + ".chrparams"
        if fs.exists(cand):
            chrparams_rel = cand
    if chrparams_rel is None:
        sibling = os.path.splitext(chr_path)[0] + ".chrparams"
        if os.path.isfile(sibling):
            chrparams_rel = sibling.replace("\\", "/")
    if chrparams_rel is None:
        return None

    try:
        params = load_chrparams_with_includes(chrparams_rel, fs)
    except Exception:
        return None
    if params is None:
        return None

    source_name = os.path.basename(chrparams_rel)
    filepath_root = params.animation_base_path or ""

    raw_entries = []  # list of (anim_name, lmg_path, entry_base)
    for entry in params.animations:
        name = entry.name or ""
        path_attr = entry.path or ""
        if path_attr:
            m = re.search(r"\.[lL][mM][gG]", path_attr)
            if m:
                # Per-entry base dir: the #filepath root recorded when the
                # entry was parsed. Entries pulled in via $Include keep the
                # base of THEIR source file (grunt's lmg live under
                # Animations/Alien/grunt while the model chrparams has no
                # #filepath of its own), so this is not optional.
                entry_base = entry.base_path or ""
                raw_entries.append((name, path_attr[:m.end()], entry_base))

    game_root = None
    for d in game_dirs:
        if not os.path.isdir(d):
            continue
        if os.path.isdir(os.path.join(d, "Objects")) or os.path.isfile(os.path.join(d, "Animations.pak")):
            game_root = d
            break
    if game_root is None and game_dirs:
        game_root = game_dirs[0]

    groups = []
    lmg_temp = os.path.join(_PROJ_TEMP, "lmg_vfs")
    for anim_name, lmg_path, entry_base in raw_entries:
        resolved = None
        base = entry_base or filepath_root
        rel = (base + "/" + lmg_path).replace("\\", "/").lstrip("/")
        # VFS index keys are lowercase; chrparams paths often mix case
        # (Animations\Alien\grunt vs animations/alien/grunt). Try the exact
        # form first, then a fully-lowercased fallback, and materialize via
        # whichever key actually hit.
        mat_key = None
        rec = idx.get(rel)
        if rec is not None:
            mat_key = rel
        else:
            rec = idx.get(rel.lower())
            if rec is not None:
                mat_key = rel.lower()
        if rec is not None:
            resolved = (rec["real"] if rec["kind"] == "loose"
                        else _materialize_index(idx, mat_key, lmg_temp))
        if resolved is None and game_root:
            loose = os.path.join(game_root, rel.replace("/", os.sep))
            if os.path.isfile(loose):
                resolved = loose

        if resolved:
            parsed = parse_lmg(resolved)
            groups.append({
                "anim_name": anim_name,
                "file": resolved,
                "blend_type": parsed["blend_type"],
                "examples": parsed["examples"],
                "joints": parsed["joints"],
                "caps": parsed["caps"],
                "dimensions": parsed["dimensions"],
                "pseudos": parsed["pseudos"],
                "faces": parsed["faces"],
                "vegparams": parsed["vegparams"],
                "threshold": parsed["threshold"],
            })
        else:
            groups.append({
                "anim_name": anim_name,
                "file": None,
                "blend_type": "",
                "examples": [],
                "joints": [],
                "caps": "",
                "dimensions": [],
                "pseudos": [],
                "faces": [],
                "vegparams": {},
                "threshold": None,
            })

    return {
        "source": source_name,
        "filepath": filepath_root,
        "groups": groups,
    }


def collect_bspace_refs(chr_path, game_dirs, virtual=None):
    """Collect blend-space (.bspace/.comb) assets for a character.

    The engine loads every blend-space under the character's animation root
    (the chrparams wildcard ``*\\*.bspace`` only matches one directory deep),
    so this follows the same sweep pattern as the loose .caf promotion in
    _resolve_chrparams_cafs: all VFS keys with the animation-root prefix and
    a .bspace/.comb suffix are materialized and parsed via crybspace.

    .comb files additionally reference sub blend-spaces by game path in
    ``<BlendSpace AName="...">``; those references are resolved through the
    VFS and reported as ``file`` on each entry (null when unresolvable).

    Args:
        chr_path: resolved path to the character .chr file.
        game_dirs: list of candidate game-root directories.
        virtual: the character's game-relative VFS path (for pak inputs),
            derived from ``chr_path`` when omitted and it lies in a game root.

    Returns:
        A dict ``{"source": <chrparams basename>, "filepath": <anim root>,
        "blend_spaces": [...]}`` or ``None`` when no .chrparams exists (or it
        declares no animation root to sweep).
    """
    if not game_dirs:
        return None

    try:
        from cristical_core.cryvfs import mount_game, mount_gamedirs
        from cristical_core.crychrparams import load_chrparams_with_includes
        from cristical_core.crybspace import parse_bspace_data, parse_comb_data
    except ImportError:
        from cryvfs import mount_game, mount_gamedirs
        from crychrparams import load_chrparams_with_includes
        from crybspace import parse_bspace_data, parse_comb_data

    fs = mount_gamedirs([str(d) for d in game_dirs])
    idx = mount_game([str(d) for d in game_dirs])

    chr_rel = virtual
    if not chr_rel:
        chr_rel = _rel_of(chr_path, game_dirs)
    chrparams_rel = None
    if chr_rel:
        cand = os.path.splitext(chr_rel.replace("\\", "/"))[0] + ".chrparams"
        if fs.exists(cand):
            chrparams_rel = cand
    if chrparams_rel is None:
        sibling = os.path.splitext(chr_path)[0] + ".chrparams"
        if os.path.isfile(sibling):
            chrparams_rel = sibling.replace("\\", "/")
    if chrparams_rel is None:
        return None

    try:
        params = load_chrparams_with_includes(chrparams_rel, fs)
    except Exception:
        return None
    if params is None:
        return None

    filepath_root = params.animation_base_path or ""
    if not filepath_root:
        return None

    prefix = filepath_root.replace("\\", "/").strip("/") + "/"
    bspace_temp = os.path.join(_PROJ_TEMP, "bspace_vfs")

    blend_spaces = []
    for key in idx.keys():
        k = key.replace("\\", "/")
        kl = k.lower()
        if not kl.startswith(prefix.lower()):
            continue
        if not (kl.endswith(".bspace") or kl.endswith(".comb")):
            continue
        rec = idx.get(key)
        if rec is None:
            continue
        real = rec["real"] if rec["kind"] == "loose" else _materialize_index(
            idx, key if rec["kind"] != "pak" else kl, bspace_temp)
        if not real or not os.path.isfile(real):
            continue
        with open(real, "rb") as bf:
            data = bf.read()
        if kl.endswith(".bspace"):
            parsed = parse_bspace_data(data)
        else:
            parsed = parse_comb_data(data)
        if "error" in parsed:
            continue
        entry = {
            "file": key,
            "parsed": parsed,
        }
        # .comb: resolve the referenced sub blend-spaces through the VFS.
        if parsed["kind"] == "comb":
            resolved_refs = []
            for ref in parsed.get("blend_spaces", []):
                name = (ref.get("name") or "").replace("\\", "/")
                hit = None
                for cand in (name, name.lower()):
                    if idx.get(cand) is not None:
                        hit = cand
                        break
                resolved_refs.append({
                    "name": ref.get("name"),
                    "file": hit,
                })
            entry["resolved_refs"] = resolved_refs
        blend_spaces.append(entry)

    return {
        "source": os.path.basename(chrparams_rel),
        "filepath": filepath_root,
        "blend_spaces": blend_spaces,
    }


def _resolve_mtl(chr_path, game_dirs, prim_materials=None, verbose=True, log=None):
    """Find the best .mtl for a character model (.chr/.cdf).

    Delegates to the shared resolver in cristical_core.mtl_resolve, which
    scores candidates by sub-material name overlap with the primitives'
    material names.
    """
    return resolve_mtl(chr_path, game_dirs, prim_materials=prim_materials,
                       verbose=verbose, log=log or print)


def _inject_caf_batch(gltf, buf, containers, out_dir):
    """Inject many CAF DBA-containers in ONE injector pass (roadmap 4.3).

    Avoids the quadratic tmp-file rewrite of per-clip _inject_all calls:
    the gltf/bin pair is written once, all containers go through
    inject_multi, and the result is read back once.
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
    n = injector.inject_multi(containers, progress=lambda _: None)
    injector.save()
    with open(tmp_g, "r") as f:
        updated = json.load(f)
    with open(tmp_b, "rb") as f:
        updated_buf = bytearray(f.read())
    os.remove(tmp_g)
    os.remove(tmp_b)
    return updated, updated_buf, n


def _inject_all(gltf, buf, dba, out_dir, reset=True):
    tmp_g = os.path.join(out_dir, "_tmp_anim.gltf")
    tmp_b = tmp_g.replace(".gltf", ".bin")
    gltf["buffers"][0]["uri"] = os.path.basename(tmp_b)
    gltf["buffers"][0]["byteLength"] = len(buf)
    with open(tmp_b, "wb") as f:
        f.write(bytes(buf))
    with open(tmp_g, "w") as f:
        json.dump(gltf, f, separators=(",", ":"))
    injector = GltfAnimationInjector(tmp_g)
    n = injector.inject(dba, progress=lambda msg: print("   " + msg),
                        reset=reset)
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


def run_pipeline(input_path, game_dirs, out_gltf, do_anim=True, do_tex=True, split_anim=False, progress_cb=None, glb=False, caf_paths=None, keep_root_motion=True, virtual_path=None):
    """Convert a character (.cdf/.chr) to glTF.

    ``virtual_path`` is the ORIGINAL user-facing path (e.g. the in-pak
    ``Objects/characters/alien/grunt/Grunt.cdf``) when the input was
    materialized from a game .pak. Companion lookups (.cal, .chrparams,
    .mtl, .caf) must resolve from THIS path through the VFS, not from the
    temp dir where the geometry lives.
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
    L("CrisTical v2.1 — Crysis CDF -> glTF Converter")
    L("  Authors: Soror L.'.L.'. aka Methelina")
    L("  Started: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    L("  Input: %s" % os.path.abspath(input_path))
    L("=" * 60)

    chr_path = input_path
    model_ref = None
    if input_path.lower().endswith(".cdf"):
        cdf_info = read_cdf(input_path, game_dirs)
        L("  CDF model: %s" % cdf_info.get("model_path", "?"))
        for aname, apath in cdf_info.get("skin_attachments", []):
            L("  CDF attachment: %s <- %s" % (aname, apath))
        if cdf_info.get("model_path"):
            chr_path = cdf_info["model_path"]
            # Virtual (game-relative) path of the MODEL as written in the CDF
            # XML — companion lookups (.cal/.chrparams/.mtl) resolve from it
            # through the shared VFSIndex even for pak-materialized inputs.
            model_ref = cdf_info.get("model_ref")

    L("[1/3] Skeleton + mesh")
    data = read_chr_or_cdf(input_path, game_dirs)
    bones = data["skeleton"]
    mesh = data["mesh"]

    # --- CA_BONE static .cgf attachments (mask/head, chest/leg/groin plates) ---
    # These are ordinary object pieces bound to a bone at rest; the skin/CA_SKIN
    # merge above only covers .skin attachments, so without this the character
    # is missing half its visible armour/mask geometry.
    ca_bone_pieces = []
    if input_path.lower().endswith(".cdf") and game_dirs:
        try:
            _cdf_info = read_cdf(input_path, game_dirs)
        except Exception:
            _cdf_info = None
        if _cdf_info and _cdf_info.get("attachments"):
            bone_by_name = {}
            for _i, _b in enumerate(bones):
                bone_by_name.setdefault((_b.get("name") or "").lower(), _i)
            _frame = _ca_bone_frame_mode(game_dirs)
            _title = _ca_bone_title(game_dirs)
            L("  CA_BONE frame policy: %s (game %s)" % (
                _frame, (_title.value if _title else "unknown")))
            # Joint-child mode (child mesh nodes under joints, no
            # bind-lift baked into vertices). Enabled only for C2 characters
            # whose CA_BONE pieces are origin-authored, via env override.
            _env_node = (os.environ.get("CRITICAL_CA_BONE_NODES") or "").strip().lower()
            _nodes_mode = _env_node in ("1", "true", "yes") and _title == GameTitle.CRYSIS_2
            if _nodes_mode:
                L("  CA_BONE mode: child mesh nodes under joints (no bind-lift)")
            added = 0
            for att in _cdf_info["attachments"]:
                if att.get("type") != "CA_BONE":
                    continue
                binding = att.get("binding") or ""
                if not binding.lower().endswith(".cgf"):
                    continue
                bname = att.get("bone_name") or ""
                base = os.path.basename(binding)
                if "damage" in base.lower() or "Damage" in base:
                    continue  # healthy (non-damaged) variant only
                bi = bone_by_name.get(bname.lower())
                if bi is None:
                    continue
                cgf_real = _materialize_cgf(binding, game_dirs)
                if not cgf_real:
                    continue
                attname = att.get("name") or os.path.basename(base)
                if _nodes_mode:
                    try:
                        cprims = read_cgf_meshes(cgf_real)
                    except Exception as e:
                        L("  [cdf] WARN CA_BONE %s: %s" % (attname, e))
                        continue
                    if not cprims:
                        continue
                    # Orientation: bake the editor-confirmed armour rotation
                    # (helmet/chest) so no manual rotation is needed. Weapon/hand
                    # pieces stay aligned.
                    low = (attname + " " + (bname or "")).lower()
                    if "weapon" in low or "gun" in low or "hand" in low:
                        rmode = "none"
                    else:
                        rmode = "armor"
                    ca_bone_pieces.append({
                        "name": attname, "bone_idx": bi, "rot": rmode,
                        "prims": [{"positions": cp["positions"],
                                   "normals": cp.get("normals") or [],
                                   "uvs": cp.get("uvs") or [],
                                   "indices": cp["indices"],
                                   "mat_id": cp.get("mat_id", 0)} for cp in cprims],
                    })
                    added += len(cprims)
                    continue
                bind_q = _quat_wxyz(att.get("rotation"))
                bind_pos = _vec3(att.get("position"))
                # C1-family CompiledBone b2w is stored in cm (ReadCompiledBones
                # copies m_DefaultB2W unscaled) and the authored .cgf verts are in
                # cm, while the body mesh is metres — scale both to metres (×0.01)
                # for C1 so attachments land on the (metre) body.
                _c1 = _title in (GameTitle.CRYSIS_1, GameTitle.CRYSIS_WARHEAD,
                                  GameTitle.CRYSIS_WARS, GameTitle.CRYSIS_REMASTERED)
                _bscale = 0.01 if (_c1 and _frame == "lift") else 1.0
                _vscale = 0.01 if _c1 else 1.0
                newp = _bone_attachment_prims(
                    bones, bi, cgf_real, bind_q, bind_pos, attname,
                    mode=_frame, log=L, bind_scale=_bscale, vert_scale=_vscale)
                mesh["primitives"].extend(newp)
                added += len(newp)
            if added:
                L("  CA_BONE .cgf attachments: +%d primitives" % added)

    L("  bones=%d primitives=%d" % (len(bones), len(mesh["primitives"])))
    gltf, buf = export_gltf(bones, mesh)
    if ca_bone_pieces:
        buf = bytearray(buf)
        n_cc = _emit_ca_bone_child_nodes(gltf, buf, ca_bone_pieces)
        L("  CA_BONE child node meshes: %d" % n_cc)

    out_dir = os.path.dirname(out_gltf)
    os.makedirs(out_dir, exist_ok=True)

    if do_tex:
        L("[2/3] Materials + textures")
        prim_materials = [p.get("material") for p in mesh["primitives"]]
        mtl_path = _resolve_mtl(chr_path, game_dirs, prim_materials)
        L("  MTL: %s" % mtl_path)
        all_pngs = []
        all_materials = []
        loaded_mtls = set()

        if os.path.isfile(mtl_path):
            mats, pngs, mat_info, tex_sources, xml_to_mat = convert_materials(mtl_path, game_dirs, out_dir)
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
                        mats2, pngs2, _mi2, _ts2, _xm2 = convert_materials(att_mtl, game_dirs, out_dir)
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
            mat_names = [m.get("name", "") for m in all_materials]
            for pi, prim in enumerate(prims):
                mat_name = prim.pop("_mat_name", "material")
                mat_id = prim.pop("_mat_id", 0)
                # prefer exact name, then normalized, then prefix match
                idx_m = next((i for i, n in enumerate(mat_names) if n == mat_name), None)
                if idx_m is None:
                    tn = "".join(ch.lower() for ch in mat_name if ch.isalnum())
                    idx_m = next(
                        (i for i, n in enumerate(mat_names)
                         if "".join(ch.lower() for ch in n if ch.isalnum()) == tn), None)
                if idx_m is None:
                    tn = "".join(ch.lower() for ch in mat_name if ch.isalnum())
                    idx_m = next(
                        (i for i, n in enumerate(mat_names)
                         if tn and (("".join(ch.lower() for ch in n if ch.isalnum())).startswith(tn)
                                    or tn.startswith("".join(ch.lower() for ch in n if ch.isalnum())))), None)
                # last resort: translate mat_id (XML index) through the Nodraw-aware mapping
                if idx_m is None:
                    mapped = xml_to_mat.get(mat_id)
                    if mapped is not None and 0 <= mapped < len(mats):
                        idx_m = mapped
                prim["material"] = idx_m if idx_m is not None else 0
            L("  %d textures, %d materials" % (len(png_files), len(all_materials)))
        else:
            L("  no .mtl found, skipping")

    if do_anim:
        L("[3/3] Animations")
        # Companion (.cal / .chrparams) of the character is resolved through
        # the shared VFSIndex using the model's game-relative path: the CDF's
        # Model/File reference when available (pak inputs materialize to temp,
        # losing the original directory), else the original virtual path when
        # the .chr itself was the pak input, else derived from the real path.
        model_rel = model_ref or virtual_path or _rel_of(chr_path, game_dirs)
        cal = {}
        if model_rel:
            try:
                from cristical_core.cryvfs import mount_game
            except ImportError:
                from cryvfs import mount_game
            idx_cal = mount_game([str(d) for d in game_dirs])
            cal_rel = os.path.splitext(model_rel)[0] + ".cal"
            if idx_cal.get(cal_rel) is not None:
                try:
                    cal = parse_cal_text(idx_cal.read_all_bytes(cal_rel)
                                         .decode("utf-8", errors="replace"))
                except Exception:
                    cal = {}
        if not cal:
            cal = parse_cal(os.path.splitext(chr_path)[0] + ".cal")
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
            # Roadmap 4.3: no .cal — fall back to .chrparams (C2/C3+ style).
            chrparams_clips = _resolve_chrparams_cafs(chr_path, game_dirs, L,
                                                      virtual=model_rel)
            if chrparams_clips:
                caf_paths = list(caf_paths or []) + chrparams_clips
                L("  chrparams: %d loose .caf refs promoted" % len(chrparams_clips))
            else:
                L("  no .cal file found, skipping animations")

    if do_anim:
        _loco_strategy = _blend_loco_strategy(game_dirs)
        _loco_title = _resolve_game_title(game_dirs)
        L("  LMG/BSPACE strategy: %s (game %s)" % (
            _loco_strategy["format"],
            (_loco_title.value if _loco_title else "unknown")))
        # Sidecar names come from the model's VIRTUAL path (the .chr as named
        # in the game data), never from the materialized temp file which
        # carries a content-hash suffix (grunt_base_d631a274 etc.).
        _chr_stem = None
        if model_rel:
            _chr_stem = os.path.splitext(os.path.basename(
                model_rel.replace("\\", "/")))[0]
        if not _chr_stem:
            _chr_stem = os.path.splitext(os.path.basename(chr_path))[0]
        if _loco_strategy["lmg"]:
            lmg_result = collect_lmg_refs(chr_path, game_dirs, virtual=model_rel)
            if lmg_result and lmg_result.get("groups"):
                lmg_result["game_title"] = _loco_title.value if _loco_title else "unknown"
                lmg_result["format"] = _loco_strategy["format"]
                lmg_name = _chr_stem + "_locomotion.json"
                lmg_out = os.path.join(out_dir, lmg_name)
                with open(lmg_out, "w", encoding="utf-8") as lf:
                    json.dump(lmg_result, lf, indent=2, ensure_ascii=False)
                L("  LMG: %d groups -> %s" % (len(lmg_result["groups"]), lmg_out))
        if _loco_strategy["bspace"]:
            bs_result = collect_bspace_refs(chr_path, game_dirs, virtual=model_rel)
            if bs_result and bs_result.get("blend_spaces"):
                bs_result["game_title"] = _loco_title.value if _loco_title else "unknown"
                bs_name = _chr_stem + "_blends.json"
                bs_out = os.path.join(out_dir, bs_name)
                with open(bs_out, "w", encoding="utf-8") as bf:
                    json.dump(bs_result, bf, indent=2, ensure_ascii=False)
                L("  BSPACE: %d blend spaces -> %s" % (len(bs_result["blend_spaces"]), bs_out))

    if do_anim and caf_paths:
        L("[3/3] Loose .caf clips (%d)" % len(caf_paths))
        from cristical_core.crycaf import read_caf, caf_to_dba
        root_cid = None
        for b in bones:
            if b.get("parent", -1) == -1:
                root_cid = b.get("controller_id")
                break
        containers = []
        n_failed = 0
        for caf_path in caf_paths:
            try:
                caf = read_caf(caf_path)
            except Exception as e:
                L("  CAF failed: %s (%s)" % (caf_path, e))
                n_failed += 1
                continue
            caf_dba = caf_to_dba(
                caf, keep_root_motion=keep_root_motion,
                root_controller_id=root_cid,
                log=lambda msg: L("  " + msg))
            if caf_dba.animations and caf_dba.animations[0].controllers:
                containers.append((caf_path, caf, caf_dba))
            else:
                L("  CAF: %s (no tracks, skipped)" % os.path.basename(caf_path))
        if containers:
            gltf, buf, n = _inject_caf_batch(
                gltf, buf, [c[2] for c in containers], out_dir)
            L("  CAF: %d clips injected, %d failed, %d empty" % (
                n, n_failed, len(caf_paths) - n_failed - len(containers)))
        else:
            L("  CAF: nothing to inject")

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
    game_dirs: list = []
    while not cdf_path:
        cdf_path = input("Path to .cdf file: ").strip().strip('"')
        if not cdf_path:
            continue
        if not cdf_path.lower().endswith((".cdf", ".chr")):
            print("  Expected a .cdf file (Character Definition File)")
            cdf_path = ""
            continue
        if not os.path.isfile(cdf_path):
            # Virtual path inside a pak? Ask for game dirs and try to
            # materialize it before rejecting the input.
            gd = input("  Not on disk — game dir to resolve from "
                       "(Enter to abort): ").strip().strip('"')
            if not gd:
                cdf_path = ""
                continue
            game_dirs = [d for d in (x.strip() for x in gd.split(";")) if d]
            try:
                cdf_path = resolve_geometry_path(cdf_path, game_dirs)
                print("  Virtual path materialized.")
            except FileNotFoundError as e:
                print("  %s" % e)
                cdf_path = ""
    print()

    if not game_dirs:
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
    ap.add_argument("--caf", action="append", default=[], help="loose .caf clip to inject (repeatable)")
    ap.add_argument("--no-root-motion", action="store_true", help="drop the root bone position track from .caf clips")
    ap.add_argument("--glb", action="store_true", help="output as binary .glb instead of .gltf+.bin")
    args = ap.parse_args()

    if not args.cdf:
        ap.error("--cdf is required; run without args for interactive mode")
    try:
        cdf_real = resolve_geometry_path(args.cdf, args.gamedir)
    except FileNotFoundError as e:
        ap.error(str(e))
    if cdf_real != args.cdf:
        print("[cdf2gltf] virtual path materialized: %s -> %s" % (args.cdf, cdf_real))

    cdf_name = os.path.splitext(os.path.basename(args.cdf))[0]
    out = args.out or os.path.join(os.path.dirname(args.cdf) or ".", cdf_name + ".gltf")
    run_pipeline(cdf_real, args.gamedir, out,
                 do_anim=not args.no_anim, do_tex=not args.no_tex,
                 split_anim=args.split_anim, glb=args.glb,
                 caf_paths=args.caf, keep_root_motion=not args.no_root_motion,
                 virtual_path=args.cdf if cdf_real != args.cdf else None)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        _interactive()
