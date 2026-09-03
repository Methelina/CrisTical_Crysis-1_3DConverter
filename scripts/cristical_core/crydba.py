#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crydba.py — CrisTical: Crysis 1 DBA animation database reader (v0903/v0905 controller)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Format: Crysis binary chunk file, single ChunkType_Controller chunk.
Layout determined by independent analysis of .dba sample files.
"""

import struct
import zlib

from .crycodecs import (
    _KEYTIME_SIZE,
    _POS_SIZE,
    _ROT_SIZE,
    decode_keytimes as _decode_keytimes,
    decode_positions as _decode_positions,
    decode_rotations as _decode_rotations,
)


class ControllerInfo:
    __slots__ = ("controller_id", "pos_kt", "pos_t", "rot_kt", "rot_t")

    def __init__(self, cid, pos_kt, pos_t, rot_kt, rot_t):
        self.controller_id = cid
        self.pos_kt = pos_kt
        self.pos_t = pos_t
        self.rot_kt = rot_kt
        self.rot_t = rot_t

    @property
    def has_pos(self):
        return self.pos_kt >= 0 and self.pos_t >= 0

    @property
    def has_rot(self):
        return self.rot_kt >= 0 and self.rot_t >= 0


class DBAAnimation:
    __slots__ = ("name", "ticks_per_frame", "secs_per_tick", "start", "end",
                 "asset_flags", "controllers")

    def __init__(self):
        self.name = ""
        self.ticks_per_frame = 1
        self.secs_per_tick = 1.0 / 30.0
        self.start = 0
        self.end = 0
        self.asset_flags = 0
        self.controllers = []


class DBAFile:
    def __init__(self):
        self.animations = []
        self.key_times = []
        self.key_pos = []
        self.key_rot = []


def _find_format(index, cumulative):
    for j in range(len(cumulative) - 1):
        if index < cumulative[j + 1]:
            return j
    return -1


def _cumulative(formats, size):
    out = [0] * size
    for i in range(1, size):
        out[i] = out[i - 1] + formats[i - 1]
    return out


def _parse_controller_903(dba, data, base, size):
    num_key_pos, num_key_rot, num_key_time, num_anims = struct.unpack_from(
        "<IIII", data, base)
    pos = base + 16

    kt_sizes = struct.unpack_from("<%dH" % num_key_time, data, pos)
    pos += num_key_time * 2
    kt_formats = struct.unpack_from("<7I", data, pos)
    pos += 7 * 4

    kp_sizes = struct.unpack_from("<%dH" % num_key_pos, data, pos)
    pos += num_key_pos * 2
    kp_formats = struct.unpack_from("<9I", data, pos)
    pos += 9 * 4

    kr_sizes = struct.unpack_from("<%dH" % num_key_rot, data, pos)
    pos += num_key_rot * 2
    kr_formats = struct.unpack_from("<9I", data, pos)
    pos += 9 * 4

    kt_cum = _cumulative(kt_formats, 8)
    kp_cum = _cumulative(kp_formats, 10)
    kr_cum = _cumulative(kr_formats, 10)

    kt_track_fmt = [_find_format(i, kt_cum) for i in range(num_key_time)]
    kp_track_fmt = [_find_format(i, kp_cum) for i in range(num_key_pos)]
    kr_track_fmt = [_find_format(i, kr_cum) for i in range(num_key_rot)]

    kt_offsets = []
    total = 0
    for i in range(num_key_time):
        kt_offsets.append(total)
        total += kt_sizes[i] * _KEYTIME_SIZE[kt_track_fmt[i]]
    kp_offsets = []
    for i in range(num_key_pos):
        kp_offsets.append(total)
        total += kp_sizes[i] * _POS_SIZE[kp_track_fmt[i]]
    kr_offsets = []
    for i in range(num_key_rot):
        kr_offsets.append(total)
        total += kr_sizes[i] * _ROT_SIZE[kr_track_fmt[i]]

    storage_base = pos
    pos += total
    if pos > base + size:
        raise ValueError("storage overruns chunk (corrupt parse)")

    storage = data[storage_base:storage_base + total]

    for i in range(num_key_time):
        dba.key_times.append(
            _decode_keytimes(storage, kt_offsets[i], kt_sizes[i], kt_track_fmt[i]))
    for i in range(num_key_pos):
        dba.key_pos.append(
            _decode_positions(storage, kp_offsets[i], kp_sizes[i], kp_track_fmt[i]))
    for i in range(num_key_rot):
        dba.key_rot.append(
            _decode_rotations(storage, kr_offsets[i], kr_sizes[i], kr_track_fmt[i]))

    _read_anim_records_v903(dba, data, pos, num_anims)


def _parse_controller_905(dba, data, base, size):
    num_key_pos, num_key_rot, num_key_time, num_anims = struct.unpack_from(
        "<IIII", data, base)
    pos = base + 16

    kt_sizes = struct.unpack_from("<%dH" % num_key_time, data, pos)
    pos += num_key_time * 2
    kt_formats = struct.unpack_from("<7I", data, pos)
    pos += 7 * 4

    kp_sizes = struct.unpack_from("<%dH" % num_key_pos, data, pos)
    pos += num_key_pos * 2
    kp_formats = struct.unpack_from("<9I", data, pos)
    pos += 9 * 4

    kr_sizes = struct.unpack_from("<%dH" % num_key_rot, data, pos)
    pos += num_key_rot * 2
    kr_formats = struct.unpack_from("<9I", data, pos)
    pos += 9 * 4

    kt_cum = _cumulative(kt_formats, 8)
    kp_cum = _cumulative(kp_formats, 10)
    kr_cum = _cumulative(kr_formats, 10)

    kt_track_fmt = [_find_format(i, kt_cum) for i in range(num_key_time)]
    kp_track_fmt = [_find_format(i, kp_cum) for i in range(num_key_pos)]
    kr_track_fmt = [_find_format(i, kr_cum) for i in range(num_key_rot)]

    kt_offsets = list(struct.unpack_from("<%dI" % num_key_time, data, pos))
    pos += num_key_time * 4
    kp_offsets = list(struct.unpack_from("<%dI" % num_key_pos, data, pos))
    pos += num_key_pos * 4
    r_offsets_raw = struct.unpack_from("<%dI" % (num_key_rot + 1), data, pos)
    kr_offsets = list(r_offsets_raw[:-1])
    track_length = r_offsets_raw[-1]
    pos += (num_key_rot + 1) * 4

    if pos % 4 != 0:
        pos = (pos & ~3) + 4

    storage_base = pos
    pos += track_length
    if pos > base + size:
        raise ValueError("storage overruns chunk (corrupt parse)")

    storage = data[storage_base:storage_base + track_length]

    for i in range(num_key_time):
        dba.key_times.append(
            _decode_keytimes(storage, kt_offsets[i], kt_sizes[i], kt_track_fmt[i]))
    for i in range(num_key_pos):
        dba.key_pos.append(
            _decode_positions(storage, kp_offsets[i], kp_sizes[i], kp_track_fmt[i]))
    for i in range(num_key_rot):
        dba.key_rot.append(
            _decode_rotations(storage, kr_offsets[i], kr_sizes[i], kr_track_fmt[i]))

    _read_anim_records_v905(dba, data, pos, num_anims)


def _read_anim_records_v903(dba, data, pos, num_anims):
    for _ in range(num_anims):
        anim = DBAAnimation()
        (name_len,) = struct.unpack_from("<H", data, pos)
        pos += 2
        anim.name = data[pos:pos + name_len].decode("ascii", "replace")
        pos += name_len

        (anim.ticks_per_frame, anim.secs_per_tick, anim.start, anim.end,
         _speed, _distance, _slope, anim.asset_flags) = struct.unpack_from(
            "<ifiiiifi", data, pos)
        pos += 76

        (foot_bytes,) = struct.unpack_from("<H", data, pos)
        pos += 2 + foot_bytes

        (num_ctrl,) = struct.unpack_from("<H", data, pos)
        pos += 2
        for _ in range(num_ctrl):
            cid, pkt, pt, rkt, rt = struct.unpack_from("<Iiiii", data, pos)
            pos += 20
            anim.controllers.append(ControllerInfo(cid, pkt, pt, rkt, rt))
        dba.animations.append(anim)


def _read_anim_records_v905(dba, data, pos, num_anims):
    for _ in range(num_anims):
        anim = DBAAnimation()
        (name_len,) = struct.unpack_from("<H", data, pos)
        pos += 2
        anim.name = data[pos:pos + name_len].decode("ascii", "replace")
        pos += name_len

        (anim.asset_flags, _compression, anim.ticks_per_frame, anim.secs_per_tick,
         anim.start, anim.end) = struct.unpack_from("<IIifii", data, pos)
        pos += 132

        (foot_bytes,) = struct.unpack_from("<H", data, pos)
        pos += 2 + foot_bytes

        (num_ctrl,) = struct.unpack_from("<H", data, pos)
        pos += 2
        for _ in range(num_ctrl):
            cid, pkt, pt, rkt, rt = struct.unpack_from("<Iiiii", data, pos)
            pos += 20
            anim.controllers.append(ControllerInfo(cid, pkt, pt, rkt, rt))
        dba.animations.append(anim)


def read_dba(path):
    data = open(path, "rb").read()
    if data[:6] != b"CryTek":
        raise ValueError("not a Crysis binary chunk file: %s" % path)

    file_type, version, chunk_table_offset = struct.unpack_from("<III", data, 8)
    (num_chunks,) = struct.unpack_from("<I", data, 20)

    dba = DBAFile()
    table_pos = chunk_table_offset + 4
    for i in range(num_chunks):
        ctype, cver, coffset, cid = struct.unpack_from("<IIII", data, table_pos + i * 16)
        if ctype != 0xCCCC000D:
            continue
        chunk_end = len(data)
        if i + 1 < num_chunks:
            next_off = struct.unpack_from("<I", data, table_pos + (i + 1) * 16 + 8)[0]
            chunk_end = next_off
        if cver == 0x0903:
            _parse_controller_903(dba, data, coffset + 16, chunk_end - coffset - 16)
        elif cver == 0x0905:
            _parse_controller_905(dba, data, coffset + 16, chunk_end - coffset - 16)
        else:
            print("[crydba] skipping controller chunk version 0x%04X" % cver)
    return dba


def crc32_lower(name):
    return zlib.crc32(name.lower().encode("ascii", "replace")) & 0xFFFFFFFF


def read_dba_version(dba_path):
    data = open(dba_path, "rb").read()
    if data[:6] != b"CryTek":
        return None
    fv = struct.unpack_from("<I", data, 12)[0]
    cto = struct.unpack_from("<I", data, 16)[0]
    nch = struct.unpack_from("<I", data, 20)[0]
    entry_size = 20 if fv == 0x0745 else 16
    for i in range(nch):
        t, v, o = struct.unpack_from("<III", data, cto + 4 + i * entry_size)
        if t == 0xCCCC000D:
            return "v%04X" % v
    return None


def has_tcb_controllers(dba_path):
    data = open(dba_path, "rb").read()
    if data[:6] != b"CryTek":
        return False
    fv = struct.unpack_from("<I", data, 12)[0]
    cto = struct.unpack_from("<I", data, 16)[0]
    nch = struct.unpack_from("<I", data, 20)[0]
    entry_size = 20 if fv == 0x0745 else 16
    for i in range(nch):
        t, v, o, _cid = struct.unpack_from("<IIII", data, cto + 4 + i * entry_size)
        if t == 0xCCCC000D and v in (0x0903, 0x0905):
            nkt = struct.unpack_from("<I", data, o + 16 + 8)[0]
            fmt_off = o + 16 + 16 + nkt * 2
            fmt0 = struct.unpack_from("<I", data, fmt_off)[0] if fmt_off + 4 < len(data) else 0
            return fmt0 > 0
    return False
