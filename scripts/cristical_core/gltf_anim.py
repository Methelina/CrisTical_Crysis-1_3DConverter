#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
gltf_anim.py — CrisTical: inject Crysis DBA/CAF/ANM animations into glTF
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Takes animation containers produced by the readers (crydba / crycaf /
crycga anm_to_dba — a shared SimpleNamespace shape: per-clip name,
secs_per_tick, and controller tracks of QuatT key-times) and writes them
as glTF 2.0 animations into an existing skeleton .gltf + .bin pair:

  - tracks are matched to skeleton nodes by controller id -> node name
  - key times are re-normalised to each track's first key and scaled
    by the clip's secs_per_tick
  - additivity/clip-kind is recorded per animation in
    extras.cryengine.animType for downstream consumers
"""

import json
import os
import struct
import zlib

COMPONENT_FLOAT = 5126


def _swap_pos(p):
    return (-p[0], p[2], p[1])


def _swap_quat(q):
    return (-q[0], q[2], q[1], q[3])


def _qmul(a, b):
    """glTF-frame quaternion multiply (x,y,z,w)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def _fix_hemisphere(track):
    out = []
    prev = None
    for q in track:
        if prev is not None:
            best_q = q
            best_dot = -2.0
            for rep in (q, (-q[0], -q[1], -q[2], -q[3])):
                d = abs(rep[0]*prev[0] + rep[1]*prev[1] + rep[2]*prev[2] + rep[3]*prev[3])
                if d > best_dot:
                    best_dot = d
                    best_q = rep
            q = best_q
        out.append(q)
        prev = q
    return out


class GltfAnimationInjector:
    def __init__(self, gltf_path):
        self.gltf_path = gltf_path
        with open(gltf_path, "r", encoding="utf-8") as f:
            self.gltf = json.load(f)

        buf = self.gltf["buffers"][0]
        uri = buf.get("uri")
        self.bin_path = None
        self.bin_data = bytearray()
        if uri and not uri.startswith("data:"):
            self.bin_path = os.path.join(os.path.dirname(gltf_path), uri)
            with open(self.bin_path, "rb") as f:
                self.bin_data = bytearray(f.read())
        else:
            raise ValueError("external .bin buffer required (use -gltf, not -glb)")

        self.gltf.setdefault("animations", [])
        self._joint_nodes = self._collect_joint_nodes()
        self._node_names = {}
        for idx, n in enumerate(self.gltf.get("nodes", [])):
            self._node_names[idx] = n.get("name", "")

    def _collect_joint_nodes(self):
        nodes = self.gltf.get("nodes", [])
        skins = self.gltf.get("skins", [])
        mapping = {}
        joint_indices = set()
        for skin in skins:
            joint_indices.update(skin.get("joints", []))
        if not joint_indices:
            joint_indices = set(range(len(nodes)))
        for idx in joint_indices:
            name = nodes[idx].get("name", "")
            if not name:
                continue
            for variant in (name, name.replace("_", " "), name.replace(" ", "_")):
                crc = zlib.crc32(variant.encode("ascii", "replace")) & 0xFFFFFFFF
                mapping.setdefault(crc, idx)
        return mapping

    def _append_buffer(self, payload):
        while len(self.bin_data) % 4 != 0:
            self.bin_data.append(0)
        offset = len(self.bin_data)
        self.bin_data.extend(payload)
        self.gltf.setdefault("bufferViews", []).append({
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(payload),
        })
        return len(self.gltf["bufferViews"]) - 1, offset

    def _add_accessor(self, floats, count, acc_type, with_minmax=False):
        bv, offset = self._append_buffer(struct.pack("<%df" % len(floats), *floats))
        acc = {
            "bufferView": bv,
            "byteOffset": 0,
            "componentType": COMPONENT_FLOAT,
            "count": count,
            "type": acc_type,
        }
        if with_minmax and count > 0:
            stride = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}[acc_type]
            mins = [float("inf")] * stride
            maxs = [float("-inf")] * stride
            for i in range(count):
                for c in range(stride):
                    v = floats[i * stride + c]
                    if v < mins[c]:
                        mins[c] = v
                    if v > maxs[c]:
                        maxs[c] = v
            acc["min"] = mins
            acc["max"] = maxs
        self.gltf.setdefault("accessors", []).append(acc)
        return len(self.gltf["accessors"]) - 1

    def inject(self, dba, name_filter=None, progress=print, reset=True):
        if reset:
            self.gltf["animations"] = []
        return self._inject_one(dba, name_filter, progress)

    def inject_multi(self, containers, name_filter=None, progress=print,
                     reset=True):
        """Inject several DBA-shaped containers in one pass.

        Semantics: reset clears animations once (not between containers),
        then each container's clips are appended. Only one save() is
        needed afterwards — no per-container disk round-trip.
        """
        if reset:
            self.gltf["animations"] = []
        n_total = 0
        for dba in containers:
            n_total += self._inject_one(dba, name_filter, progress)
        return n_total

    def _inject_one(self, dba, name_filter=None, progress=print):
        n_injected = 0
        for anim in dba.animations:
            short_name = os.path.splitext(os.path.basename(anim.name))[0]
            if name_filter and short_name.lower() not in name_filter:
                continue

            channels = []
            samplers = []
            time_acc_cache = {}
            rot_acc_cache = {}
            pos_acc_cache = {}
            used_targets = set()
            n_channels = 0

            # Additive/blend clips store rotation DELTAS relative to a base
            # pose. If they are written as absolute node rotations the whole
            # body folds into the base pose's local frame (delta ~ identity),
            # looking crumpled. Bake the deltas on top of the node's own bind
            # rotation so the body stays intact and only the deltas animate.
            clip_kind = getattr(anim, "clip_kind", None)
            is_additive = (clip_kind == "additive")

            def time_accessor(track_idx):
                if track_idx not in time_acc_cache:
                    times = dba.key_times[track_idx]
                    t0 = times[0] if times else 0.0
                    secs = [(t - t0) * anim.secs_per_tick for t in times]
                    time_acc_cache[track_idx] = self._add_accessor(
                        secs, len(secs), "SCALAR", with_minmax=True)
                return time_acc_cache[track_idx]

            for ctrl in anim.controllers:
                node = self._joint_nodes.get(ctrl.controller_id)
                if node is None:
                    continue

                if ctrl.has_rot and (node, "rotation") not in used_targets:
                    used_targets.add((node, "rotation"))
                    if ctrl.rot_t not in rot_acc_cache:
                        bind_q = None
                        if is_additive:
                            bind_q = self.gltf["nodes"][node].get(
                                "rotation", (0.0, 0.0, 0.0, 1.0))
                        track = _fix_hemisphere(dba.key_rot[ctrl.rot_t])
                        vals = []
                        for q in track:
                            g = _swap_quat(q)
                            if is_additive:
                                g = _qmul(bind_q, g)
                            vals.extend(g)
                        rot_acc_cache[ctrl.rot_t] = self._add_accessor(
                            vals, len(track), "VEC4")
                    samplers.append({
                        "input": time_accessor(ctrl.rot_kt),
                        "output": rot_acc_cache[ctrl.rot_t],
                        "interpolation": "LINEAR",
                    })
                    channels.append({
                        "sampler": len(samplers) - 1,
                        "target": {"node": node, "path": "rotation"},
                    })
                    n_channels += 1

                if ctrl.has_pos and (node, "translation") not in used_targets:
                    used_targets.add((node, "translation"))
                    if ctrl.pos_t not in pos_acc_cache:
                        vals = []
                        for p in dba.key_pos[ctrl.pos_t]:
                            vals.extend(_swap_pos(p))
                        pos_acc_cache[ctrl.pos_t] = self._add_accessor(
                            vals, len(dba.key_pos[ctrl.pos_t]), "VEC3")
                    samplers.append({
                        "input": time_accessor(ctrl.pos_kt),
                        "output": pos_acc_cache[ctrl.pos_t],
                        "interpolation": "LINEAR",
                    })
                    channels.append({
                        "sampler": len(samplers) - 1,
                        "target": {"node": node, "path": "translation"},
                    })
                    n_channels += 1

            if n_channels == 0:
                continue

            animation_entry = {
                "name": short_name,
                "channels": channels,
                "samplers": samplers,
            }

            # Clip type annotation (roadmap 5.5): carry the container's
            # clip_kind into extras.cryengine.animType. Only containers
            # that provide clip_kind (the .caf path) get the annotation —
            # DBA/ANM outputs stay byte-identical to the baseline.
            # partial_body is refined here from the actual animated node
            # names (the engine treats <= 3 tracks with a "weapon" bone as partial).
            clip_kind = getattr(anim, "clip_kind", None)
            if clip_kind is not None:
                if clip_kind == "full_body":
                    node_names = [
                        self._node_names.get(ch["target"]["node"], "")
                        for ch in channels
                    ]
                    bones = [n.lower() for n in node_names if n]
                    if bones and len(bones) <= 3 and any(
                            "weapon" in b for b in bones):
                        clip_kind = "partial_body"
                animation_entry["extras"] = {
                    "cryengine": {
                        "animType": clip_kind,
                    },
                }

            self.gltf["animations"].append(animation_entry)
            n_injected += 1
            if progress:
                progress("  [anim] %-55s channels=%d" % (short_name, n_channels))

        return n_injected

    def save(self, out_path=None):
        out_path = out_path or self.gltf_path
        self.gltf["buffers"][0]["byteLength"] = len(self.bin_data)

        if self.bin_path:
            out_bin = os.path.join(os.path.dirname(out_path),
                                    os.path.basename(self.bin_path))
            with open(out_bin, "wb") as f:
                f.write(bytes(self.bin_data))

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(self.gltf, f, separators=(",", ":"))
        return out_path
