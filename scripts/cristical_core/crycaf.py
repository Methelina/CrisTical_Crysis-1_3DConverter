#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crycaf.py — CrisTical: .caf individual animation clip reader (0x829/0x831)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Verified against the full Crysis Remastered loose .caf corpus (397 files):
every file is a chunked binary (sig 8 bytes, FileType 0xFFFF0000, table
version 0x0745 with 20-byte entries) containing exactly one MotionParameters
chunk (0xAAFC0002 v0x925) plus N Controller chunks (0xCCCC000D v0x829).
Crysis 2/3 files may instead use Timing 0x918/0x919 + GAH 0x971 headers and
0x831 controllers — those are read too, but not yet corpus-verified.

Format knowledge from studying the engine's chunk identifiers and the
files themselves: CHUNK_MOTION_PARAMETERS (0x925),
CONTROLLER_CHUNK_DESC_0829/0831, CHUNK_GAHCAF_INFO (0x971),
TIMING_CHUNK_DESC_0918.
"""

import struct
import types

from .crycodecs import (
    KEYTIME_F32,
    POS_NOCOMPRESS,
    POS_NOCOMPRESS_VEC3,
    ROT_NOCOMPRESS,
    ROT_NOCOMPRESS_QUAT,
    decode_keytimes,
    decode_positions,
    decode_rotations,
    keytime_size,
    position_size,
    rotation_size,
)

CC_Controller = 0xCCCC000D
CC_Timing = 0xCCCC0009
CC_MotionParams = 0xAAFC0002
CC_GAHCAF = 0xAAFC0007


def _chunk_table(raw, path=""):
    """Parse the chunk table shared by .caf/.chr/.cgf chunked files.

    Returns a list of dicts: type, version, offset, id, size (size is
    None for 0x0744 tables). Raises ValueError on bad signature or a
    table that runs past the end of the file.
    """
    if raw[:8] != b"CryTek\x00\x00":
        raise ValueError("not a Crysis binary chunk file: %s" % path)
    file_type, version, table_offset, num_chunks = struct.unpack_from(
        "<IIII", raw, 8)
    entry_size = 20 if version == 0x0745 else 16
    end = table_offset + 4 + num_chunks * entry_size
    if end > len(raw):
        raise ValueError("chunk table overruns file: %s" % path)
    chunks = []
    for i in range(num_chunks):
        base = table_offset + 4 + i * entry_size
        if entry_size == 20:
            t, v, off, cid, size = struct.unpack_from("<IIIII", raw, base)
        else:
            t, v, off, cid = struct.unpack_from("<IIII", raw, base)
            size = None
        chunks.append({
            "type": t, "version": v, "offset": off, "id": cid, "size": size,
        })
    return chunks


def _read_motion_params(raw, off):
    """MotionParams905 body (132 bytes): flags/timing plus start/end
    position-orientation poses and locomotion stats."""
    (asset_flags, compression, ticks_per_frame, secs_per_tick,
     start, end, move_speed, turn_speed, asset_turn, distance, slope) = \
        struct.unpack_from("<IIifiifffff", raw, off)
    off2 = off + 44
    start_q = struct.unpack_from("<4f", raw, off2)
    start_t = struct.unpack_from("<3f", raw, off2 + 16)
    end_q = struct.unpack_from("<4f", raw, off2 + 28)
    end_t = struct.unpack_from("<3f", raw, off2 + 44)
    heels = struct.unpack_from("<8f", raw, off2 + 56)
    return types.SimpleNamespace(
        asset_flags=asset_flags,
        compression=compression,
        ticks_per_frame=ticks_per_frame,
        secs_per_tick=secs_per_tick,
        start=start,
        end=end,
        move_speed=move_speed,
        turn_speed=turn_speed,
        asset_turn=asset_turn,
        distance=distance,
        slope=slope,
        start_location_q=start_q,
        start_location_t=start_t,
        end_location_q=end_q,
        end_location_t=end_t,
        l_heel_start=heels[0], l_heel_end=heels[1],
        l_toe0_start=heels[2], l_toe0_end=heels[3],
        r_heel_start=heels[4], r_heel_end=heels[5],
        r_toe0_start=heels[6], r_toe0_end=heels[7],
    )


def _read_timing(raw, off, version):
    """Timing chunk body — secs_per_tick, ticks_per_frame, global range."""
    secs_per_tick, ticks_per_frame = struct.unpack_from("<fi", raw, off)
    range_name = raw[off + 8:off + 40].split(b"\x00")[0].decode(
        "ascii", "replace")
    range_start, range_end = struct.unpack_from("<ii", raw, off + 40)
    if version >= 0x0919:
        num_sub = struct.unpack_from("<i", raw, off + 48)[0]
    else:
        num_sub = 0
    return types.SimpleNamespace(
        secs_per_tick=secs_per_tick,
        ticks_per_frame=ticks_per_frame,
        range_name=range_name,
        range_start=range_start,
        range_end=range_end,
        num_sub_ranges=num_sub,
    )


def _read_gah_caf(raw, off):
    """GAHCAF_INFO body (0x971) — clip name/metadata for C2/C3 caf."""
    flags = struct.unpack_from("<I", raw, off)[0]
    file_path = raw[off + 4:off + 4 + 256].split(b"\x00")[0].decode(
        "ascii", "replace")
    (file_crc32, dba_crc32) = struct.unpack_from("<II", raw, off + 260)
    heels = struct.unpack_from("<8f", raw, off + 268)
    start_sec, end_sec, total_duration = struct.unpack_from(
        "<fff", raw, off + 300)
    num_controllers = struct.unpack_from("<I", raw, off + 312)[0]
    # QuatT start location: quat (16) + vec3 (12)
    start_loc_q = struct.unpack_from("<4f", raw, off + 316)
    start_loc_t = struct.unpack_from("<3f", raw, off + 332)
    last_loc_q = struct.unpack_from("<4f", raw, off + 344)
    last_loc_t = struct.unpack_from("<3f", raw, off + 360)
    velocity = struct.unpack_from("<3f", raw, off + 372)
    distance, speed, slope, turn_speed, asset_turn = struct.unpack_from(
        "<fffff", raw, off + 384)
    return types.SimpleNamespace(
        flags=flags,
        file_path=file_path,
        file_path_crc32=file_crc32,
        file_path_dba_crc32=dba_crc32,
        l_heel_start=heels[0], l_heel_end=heels[1],
        l_toe0_start=heels[2], l_toe0_end=heels[3],
        r_heel_start=heels[4], r_heel_end=heels[5],
        r_toe0_start=heels[6], r_toe0_end=heels[7],
        start_sec=start_sec,
        end_sec=end_sec,
        total_duration=total_duration,
        num_controllers=num_controllers,
        start_location_q=start_loc_q,
        start_location_t=start_loc_t,
        last_locator_q=last_loc_q,
        last_locator_t=last_loc_t,
        velocity=velocity,
        distance=distance,
        speed=speed,
        slope=slope,
        turn_speed=turn_speed,
        asset_turn=asset_turn,
    )


def _read_controller_829_831(raw, off, version):
    """Controller chunk body (0x829 / 0x831) — compressed dual track.

    Layout verified against the Remaster corpus by chunk-size oracle
    (table size == 16-byte local header + packed body; next chunk starts
    at the 4-byte aligned end):
      [local CHUNK_HEADER 16 bytes — skipped by caller]
      u32 controller_id
      [0x831 only: u32 flags]
      u16 num_rotation_keys, u16 num_position_keys
      u8 rotation_format, rotation_time_format, position_format,
         position_keys_info, position_time_format, tracks_aligned
      -> align to 4 (the 14 named bytes always pad to 16)
      [rotation values] [rotation times] [position values]
      [position times if position_keys_info != 0]

    Sections are PACKED back to back (no inter-section padding) when
    tracks_aligned == 0 — observed throughout the corpus; the 4-byte
    alignment is applied externally between chunks. When tracks_aligned
    != 0 each section is padded to a 4-byte boundary.

    Note: an earlier reconstruction of this reader applied unconditional
    per-section alignment and started data 4 bytes later; the corpus
    size-oracle disproves that for Remaster files.
    """
    pos = off
    controller_id = struct.unpack_from("<I", raw, pos)[0]
    pos += 4
    if version == 0x0831:
        flags = struct.unpack_from("<I", raw, pos)[0]
        pos += 4
    else:
        flags = 0
    (num_rot, num_pos, rot_fmt, rot_time_fmt, pos_fmt, pos_keys_info,
     pos_time_fmt, tracks_aligned) = struct.unpack_from(
        "<HH6B", raw, pos)
    pos += 10
    pos = (pos + 3) & ~3  # pad the named fields to a 4-byte boundary

    def align4(p):
        return (p + 3) & ~3 if tracks_aligned else p

    rot_values_off = pos
    pos += num_rot * rotation_size(rot_fmt)
    pos = align4(pos)
    rot_times_off = pos
    pos += num_rot * keytime_size(rot_time_fmt)
    pos = align4(pos)
    pos_values_off = pos
    pos += num_pos * position_size(pos_fmt)
    pos = align4(pos)
    pos_times_off = pos
    if pos_keys_info:
        pos += num_pos * keytime_size(pos_time_fmt)

    key_rot = decode_rotations(raw, rot_values_off, num_rot, rot_fmt)
    rot_times = decode_keytimes(raw, rot_times_off, num_rot, rot_time_fmt)
    key_pos = decode_positions(raw, pos_values_off, num_pos, pos_fmt)
    if pos_keys_info:
        pos_times = decode_keytimes(raw, pos_times_off, num_pos, pos_time_fmt)
    else:
        pos_times = list(rot_times)

    return types.SimpleNamespace(
        controller_id=controller_id,
        flags=flags,
        num_rotation_keys=num_rot,
        num_position_keys=num_pos,
        rotation_format=rot_fmt,
        rotation_time_format=rot_time_fmt,
        position_format=pos_fmt,
        position_keys_info=pos_keys_info,
        position_time_format=pos_time_fmt,
        tracks_aligned=tracks_aligned,
        key_rotations=key_rot,
        rotation_key_times=rot_times,
        key_positions=key_pos,
        position_key_times=pos_times,
    )


def classify_clip(name, source_file_name=None, track_bone_names=None):
    """Classify an animation clip by name/source path, following the
    engine's own clip taxonomy (metadata / aim / look poses, additive
    vs override, partial vs full body).

    Returns one of: "metadata", "aim_pose", "look_pose", "additive",
    "partial_body", "full_body".
    """
    text = "%s %s" % (name or "", source_file_name or "")
    text = text.replace("\\", "/").lower()
    if "$tracksdatabase" in text or "$animeventdatabase" in text:
        return "metadata"
    if "aimposes" in text or "aimpose" in text:
        return "aim_pose"
    if "lookposes" in text or "lookpose" in text:
        return "look_pose"
    tokens = text.replace("-", "_").replace("/", "_").split("_")
    if "additive" in tokens or "add" in tokens:
        return "additive"
    if track_bone_names is not None:
        bones = [b.lower() for b in track_bone_names if b]
        if bones and len(bones) <= 3 and any("weapon" in b for b in bones):
            return "partial_body"
    return "full_body"


def read_caf(path):
    """Read a .caf file into a SimpleNamespace container.

    Fields: motion_params (or None), timing (or None), gah (or None),
    controllers (list of controller namespaces), name (clip name),
    source_path (input file path, used for clip classification).
    """
    with open(path, "rb") as f:
        raw = f.read()
    chunks = _chunk_table(raw, str(path))
    source_path = str(path)

    container = types.SimpleNamespace(
        motion_params=None, timing=None, gah=None, controllers=[],
        name="", duration_secs=0.0, secs_per_tick=0.0,
        ticks_per_frame=1600, start=0, end=0, asset_flags=0,
        source_path=source_path)

    for ch in chunks:
        off = ch["offset"]
        body = off + 16  # local CHUNK_HEADER inside each chunk body
        if ch["type"] == CC_MotionParams and ch["version"] == 0x0925:
            container.motion_params = _read_motion_params(raw, body)
        elif ch["type"] == CC_Timing and ch["version"] in (0x0918, 0x0919):
            container.timing = _read_timing(raw, body, ch["version"])
        elif ch["type"] == CC_GAHCAF and ch["version"] == 0x0971:
            container.gah = _read_gah_caf(raw, body)
        elif ch["type"] == CC_Controller and ch["version"] in (
                0x0829, 0x0831):
            container.controllers.append(
                _read_controller_829_831(raw, body, ch["version"]))
        else:
            print("[crycaf] skipping chunk type 0x%08X version 0x%04X" % (
                ch["type"], ch["version"]))

    mp = container.motion_params
    tm = container.timing
    gah = container.gah
    header_found = False

    if mp is not None:
        header_found = True
        container.secs_per_tick = mp.secs_per_tick
        container.ticks_per_frame = mp.ticks_per_frame
        container.start = mp.start
        container.end = mp.end
        container.asset_flags = mp.asset_flags
    if tm is not None:
        header_found = True
        if mp is None:
            container.secs_per_tick = tm.secs_per_tick
            container.ticks_per_frame = tm.ticks_per_frame
            container.start = tm.range_start
            container.end = tm.range_end
    if gah is not None:
        header_found = True
        if gah.file_path:
            container.name = gah.file_path

    if not header_found:
        raise ValueError("no recognised header chunk in %s" % path)
    if not container.secs_per_tick:
        container.secs_per_tick = 1.0 / 30.0
    if not container.name:
        # Fallback: derive the clip name from the file name (without
        # extension) — Remaster CAFs carry no GAH path. Pak inputs are
        # materialized to <stem>_<md5>.caf; strip that digest suffix so the
        # clip name matches the original asset (XML/LMG references rely on
        # the unchanged name).
        try:
            import os
            import re
            stem = os.path.splitext(os.path.basename(str(path)))[0]
            stem = re.sub(r"_([0-9a-f]{8})$", "", stem)
            container.name = stem
        except Exception:
            container.name = "caf_anim"

    if mp is not None and mp.secs_per_tick and mp.end > mp.start:
        container.duration_secs = (mp.end - mp.start) * mp.secs_per_tick
    elif gah is not None and gah.total_duration > 0:
        container.duration_secs = gah.total_duration
    elif tm is not None and tm.secs_per_tick and tm.range_end > tm.range_start:
        container.duration_secs = (
            tm.range_end - tm.range_start) * tm.secs_per_tick
    else:
        max_t = 0.0
        for c in container.controllers:
            if c.rotation_key_times:
                max_t = max(max_t, c.rotation_key_times[-1])
            if c.position_key_times:
                max_t = max(max_t, c.position_key_times[-1])
        container.duration_secs = max_t * container.secs_per_tick

    return container


def _positions_look_valid(values):
    """Position sanity check: NaN or |v| > 10000 marks an invalid track
    (degenerate/corrupt keys observed in some shipped clips)."""
    for position in values:
        for value in position:
            if value != value or abs(float(value)) > 10000.0:
                return False
    return True


def caf_to_dba(caf, animation_name=None, keep_root_motion=True,
               root_controller_id=None, log=None, source_file_name=None):
    """Convert read_caf() output into the DBA-container shape used by
    gltf_anim.GltfAnimationInjector (pattern: crycga.anm_to_dba).

    Key times stay in TICKS: the injector re-normalises each track to
    its first key and multiplies by anim.secs_per_tick itself, exactly
    as it does for DBA and ANM containers.

    Root motion: by default the root bone's position track is KEPT
    (needed for in-place retargeting); pass keep_root_motion=False
    to drop it. root_controller_id is the
    controller id of the skeleton's root bone (from its CompiledBone
    entry, NOT a name CRC); when None no root filtering happens.

    Invalid position tracks (NaN or |v| > 10000, see
    _positions_look_valid) are dropped with a log line via `log`
    (default print).
    """
    if log is None:
        log = print
    if animation_name is None:
        animation_name = caf.name or "caf_anim"
    dba = types.SimpleNamespace()
    dba.key_times = []
    dba.key_pos = []
    dba.key_rot = []

    anim = types.SimpleNamespace()
    anim.name = animation_name
    anim.secs_per_tick = caf.secs_per_tick
    anim.controllers = []
    anim.asset_flags = getattr(caf, "asset_flags", 0)
    if source_file_name is None:
        source_file_name = getattr(caf, "source_path", None)

    for c in caf.controllers:
        rot_t = -1
        rot_kt = -1
        pos_t = -1
        pos_kt = -1

        if c.num_rotation_keys and c.key_rotations:
            rot_kt = len(dba.key_times)
            dba.key_times.append(list(c.rotation_key_times))
            rot_t = len(dba.key_rot)
            dba.key_rot.append(
                [tuple(float(v) for v in q) for q in c.key_rotations])

        if c.num_position_keys and c.key_positions:
            positions = [tuple(float(v) for v in p) for p in c.key_positions]
            if not _positions_look_valid(positions):
                log("[crycaf] dropping invalid position track for "
                    "controller 0x%08X" % c.controller_id)
                positions = []
            elif (not keep_root_motion
                  and root_controller_id is not None
                  and c.controller_id == root_controller_id):
                log("[crycaf] dropping root motion position track for "
                    "controller 0x%08X" % c.controller_id)
                positions = []
            if positions:
                pos_kt = len(dba.key_times)
                dba.key_times.append(list(c.position_key_times))
                pos_t = len(dba.key_pos)
                dba.key_pos.append(positions)

        if rot_t < 0 and pos_t < 0:
            continue

        ctrl = types.SimpleNamespace()
        ctrl.controller_id = c.controller_id
        ctrl.has_rot = (rot_t >= 0 and rot_kt >= 0)
        ctrl.rot_t = rot_t
        ctrl.rot_kt = rot_kt
        ctrl.has_pos = (pos_t >= 0 and pos_kt >= 0)
        ctrl.pos_t = pos_t
        ctrl.pos_kt = pos_kt
        anim.controllers.append(ctrl)

    # Clip type annotation (roadmap 5.5): classify by name/path; the
    # bone-name based partial_body check runs later in the injector,
    # which knows the node names.
    anim.clip_kind = classify_clip(animation_name, source_file_name)
    dba.animations = [anim]
    return dba
