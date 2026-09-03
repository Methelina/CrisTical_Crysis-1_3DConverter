#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crycodecs.py — CrisTical: shared animation codec constants and decompressors
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Holds the controller-track formats shared by the .dba and .caf readers
(crydba.py / crycaf.py): SmallTree quaternion/position decoding,
key-time formats and the format constants observed in the engine's
controller chunk descriptors (CONTROLLER_CHUNK_DESC_0829/0831).
"""

import math
import struct

KEYTIME_F32 = 0
KEYTIME_UINT16 = 1
KEYTIME_BYTE = 2
KEYTIME_F32_STARTSTOP = 3
KEYTIME_UINT16_STARTSTOP = 4
KEYTIME_BYTE_STARTSTOP = 5
KEYTIME_BITSET = 6

POS_NOCOMPRESS = 0
POS_NOCOMPRESS_VEC3 = 2

ROT_NOCOMPRESS = 0
ROT_NOCOMPRESS_QUAT = 1
ROT_SHOTINT3 = 3
ROT_SMALLTREE_DWORD = 4
ROT_SMALLTREE48 = 5
ROT_SMALLTREE64 = 6
ROT_SMALLTREE64EXT = 8

_KEYTIME_SIZE = {0: 4, 1: 2, 2: 1, 3: 4, 4: 2, 5: 1, 6: 2}
_POS_SIZE = {0: 12, 2: 12}
_ROT_SIZE = {0: 16, 1: 16, 3: 6, 4: 4, 5: 6, 6: 8, 8: 8}

_SQRT1_2 = 0.70710678118654752


def _smalltree_finish(index, comps):
    out = [0.0, 0.0, 0.0, 0.0]
    stored = [i for i in range(4) if i != index]
    sq = 0.0
    for k, ci in enumerate(stored):
        out[ci] = comps[k]
        sq += comps[k] * comps[k]
    rem = 1.0 - sq
    out[index] = math.sqrt(rem) if rem > 0.0 else 0.0
    return (out[0], out[1], out[2], out[3])


def _best_rep(q, prev_q):
    d0 = abs(q[0]*prev_q[0] + q[1]*prev_q[1] + q[2]*prev_q[2] + q[3]*prev_q[3])
    d1 = abs((-q[0])*prev_q[0] + (-q[1])*prev_q[1] + (-q[2])*prev_q[2] + (-q[3])*prev_q[3])
    return q if d0 >= d1 else (-q[0], -q[1], -q[2], -q[3])


def decompress_smalltree48(m1, m2, m3):
    index = m3 >> 14
    v0 = m1 & 0x7FFF
    v1 = ((m1 >> 15) + (m2 << 1)) & 0x7FFF
    v2 = ((m2 >> 14) + (m3 << 2)) & 0x7FFF
    comps = [v / 23170.0 - _SQRT1_2 for v in (v0, v1, v2)]
    return _smalltree_finish(index, comps)


def decompress_smalltree64(m1, m2):
    index = (m2 >> 30) & 3
    v0 = m1 & 0xFFFFF
    v1 = ((m1 >> 20) + (m2 << 12)) & 0xFFFFF
    v2 = (m2 >> 8) & 0xFFFFF
    comps = [v / 741454.0 - _SQRT1_2 for v in (v0, v1, v2)]
    return _smalltree_finish(index, comps)


def decompress_smalltree64ext(m1, m2):
    index = (m2 >> 30) & 3
    v0 = m1 & 0x1FFFFF
    v1 = ((m1 >> 21) + (m2 << 11)) & 0x1FFFFF
    v2 = (m2 >> 10) & 0xFFFFF
    comps = [
        v0 / 1482909.0 - _SQRT1_2,
        v1 / 1482909.0 - _SQRT1_2,
        v2 / 741454.0 - _SQRT1_2,
    ]
    return _smalltree_finish(index, comps)


def decompress_shotint3(m1, m2, m3):
    """ShotInt3 quaternion: 3 x int16 in [-32768..32767] mapping to
    [-sqrt(2)/2 .. sqrt(2)/2]; w is reconstructed as sqrt(1 - x^2 - y^2 - z^2)
    with the sign kept positive."""
    _s = 32767.0 * _SQRT1_2
    x = m1 / _s
    y = m2 / _s
    z = m3 / _s
    w = math.sqrt(max(0.0, 1.0 - x * x - y * y - z * z))
    return (x, y, z, w)


def decompress_smalltree_dword(m1):
    """SmallTree DWORD quaternion: 3 x 10-bit fields + 2-bit dropped index."""
    index = (m1 >> 30) & 3
    v0 = m1 & 0x3FF
    v1 = (m1 >> 10) & 0x3FF
    v2 = (m1 >> 20) & 0x3FF
    comps = [v / 1023.0 - _SQRT1_2 for v in (v0, v1, v2)]
    return _smalltree_finish(index, comps)


def decode_keytimes(data, offset, count, fmt):
    """Decode a key-time track of `count` entries at `offset` in `data`.

    Returns a list of floats (ticks). Same semantics as the former
    crydba._decode_keytimes, kept verbatim.
    """
    if count == 0:
        return []
    if fmt == KEYTIME_F32:
        return list(struct.unpack_from("<%df" % count, data, offset))
    if fmt == KEYTIME_UINT16:
        return [float(v) for v in struct.unpack_from("<%dH" % count, data, offset)]
    if fmt == KEYTIME_BYTE:
        return [float(v) for v in struct.unpack_from("<%dB" % count, data, offset)]
    if fmt == KEYTIME_BITSET:
        start, end, size = struct.unpack_from("<HHH", data, offset)
        times = []
        n_words = count - 3
        words = struct.unpack_from("<%dH" % n_words, data, offset + 6)
        tick = start
        for w in words:
            for b in range(16):
                if (w >> b) & 1:
                    times.append(float(tick))
                tick += 1
        if len(times) != size:
            times = times[:size]
        return times
    if fmt == KEYTIME_F32_STARTSTOP:
        start, stop = struct.unpack_from("<ff", data, offset)
    elif fmt == KEYTIME_UINT16_STARTSTOP:
        start, stop = struct.unpack_from("<HH", data, offset)
    elif fmt == KEYTIME_BYTE_STARTSTOP:
        start, stop = struct.unpack_from("<BB", data, offset)
    else:
        raise ValueError("unknown keytime format %d" % fmt)
    n = int(round(stop - start)) + 1
    return [float(start + i) for i in range(max(n, 0))]


def decode_positions(data, offset, count, fmt):
    """Decode a position track (list of (x, y, z) tuples)."""
    if fmt in (POS_NOCOMPRESS, POS_NOCOMPRESS_VEC3):
        return [struct.unpack_from("<3f", data, offset + i * 12) for i in range(count)]
    raise ValueError("unsupported position format %d" % fmt)


def decode_rotations(data, offset, count, fmt):
    """Decode a rotation track (list of (x, y, z, w) tuples)."""
    out = []
    if fmt in (ROT_NOCOMPRESS, ROT_NOCOMPRESS_QUAT):
        for i in range(count):
            out.append(struct.unpack_from("<4f", data, offset + i * 16))
    elif fmt == ROT_SHOTINT3:
        for i in range(count):
            m1, m2, m3 = struct.unpack_from("<hhh", data, offset + i * 6)
            out.append(decompress_shotint3(m1, m2, m3))
    elif fmt == ROT_SMALLTREE_DWORD:
        for i in range(count):
            (m1,) = struct.unpack_from("<I", data, offset + i * 4)
            out.append(decompress_smalltree_dword(m1))
    elif fmt == ROT_SMALLTREE48:
        for i in range(count):
            m1, m2, m3 = struct.unpack_from("<HHH", data, offset + i * 6)
            out.append(decompress_smalltree48(m1, m2, m3))
    elif fmt == ROT_SMALLTREE64:
        for i in range(count):
            m1, m2 = struct.unpack_from("<II", data, offset + i * 8)
            out.append(decompress_smalltree64(m1, m2))
    elif fmt == ROT_SMALLTREE64EXT:
        for i in range(count):
            m1, m2 = struct.unpack_from("<II", data, offset + i * 8)
            out.append(decompress_smalltree64ext(m1, m2))
    else:
        raise ValueError("unsupported rotation format %d" % fmt)
    return out


def read_keytime(fmt, data, offset):
    """Read one key-time value in the given format; returns (value, new_offset).

    Key-time format codes: 0/3 = f32, 1/4/6 = u16, 2/5 = u8.
    """
    if fmt in (KEYTIME_F32, KEYTIME_F32_STARTSTOP):
        return struct.unpack_from("<f", data, offset)[0], offset + 4
    if fmt in (KEYTIME_UINT16, KEYTIME_UINT16_STARTSTOP, KEYTIME_BITSET):
        return float(struct.unpack_from("<H", data, offset)[0]), offset + 2
    if fmt in (KEYTIME_BYTE, KEYTIME_BYTE_STARTSTOP):
        return float(struct.unpack_from("<B", data, offset)[0]), offset + 1
    raise ValueError("unknown keytime format %d" % fmt)


def rotation_size(fmt):
    return _ROT_SIZE.get(fmt, 16)


def position_size(fmt):
    return _POS_SIZE.get(fmt, 12)


def keytime_size(fmt):
    return _KEYTIME_SIZE.get(fmt, 4)
