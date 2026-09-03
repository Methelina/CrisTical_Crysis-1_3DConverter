#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crytcb.py — CrisTical: Crysis 1 TCB (TCB3 / TCBQ) .anm controller chunk (0x0826) reader
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Format: Crysis binary chunk file, ChunkType_Controller (0xCCCC000D) version 0x0826,
        CTRL_TCB3 / CTRL_TCBQ keyframe arrays. Layout determined by
        independent analysis of .anm sample files."""

import struct

CTRL_TCB3 = 9
CTRL_TCBQ = 10


def decode_tcb_keys(raw_bytes, ctrl_type, n_keys):
    if ctrl_type == CTRL_TCB3:
        key_fmt = "<i8f"
        key_size = 36
        comp = 3
    elif ctrl_type == CTRL_TCBQ:
        key_fmt = "<i9f"
        key_size = 40
        comp = 4
    else:
        raise ValueError("unknown ctrl_type %d" % ctrl_type)
    keys = []
    for i in range(n_keys):
        t_raw = struct.unpack_from(key_fmt, raw_bytes, i * key_size)
        keys.append({
            "time": t_raw[0],
            "value": list(t_raw[1:1 + comp]),
            "t": t_raw[1 + comp],
            "c": t_raw[2 + comp],
            "b": t_raw[3 + comp],
            "ein": t_raw[4 + comp],
            "eout": t_raw[5 + comp],
        })
    return keys


def parse_controller_chunk_0826(chunk_body):
    ctrl_type, n_keys, n_flags, n_controller_id = struct.unpack_from(
        "<iiII", chunk_body, 0x10)
    if ctrl_type == CTRL_TCB3:
        key_byte_size = 36
    elif ctrl_type == CTRL_TCBQ:
        key_byte_size = 40
    else:
        raise ValueError("unknown ctrl_type %d" % ctrl_type)
    keys = decode_tcb_keys(chunk_body[0x20:], ctrl_type, n_keys)
    return {
        "controller_id": n_controller_id,
        "ctrl_type": ctrl_type,
        "n_keys": n_keys,
        "flags": n_flags,
        "keys": keys,
        "key_byte_size": key_byte_size,
    }
