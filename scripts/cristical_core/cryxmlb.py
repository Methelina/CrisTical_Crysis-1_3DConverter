#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cryxmlb.py — CrisTical: CryXmlB / pbxml binary XML decoder
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Decodes the binary-XML encodings found while studying Crysis 2/3 asset
formats, and transparently falls back to plain-text XML parsing:

- ``CryXmlB\0`` -> header + node/reference/content tables
- ``pbxml\0``   -> recursive cry-int + cstring pairs
- anything else  -> plain XML, parsed with xml.etree

Returns an ``xml.etree.ElementTree.Element`` (the root), so every
CrisTical XML consumer (read_cdf, mtl_resolve, tex_convert, chrparams
loader) can switch to this loader transparently. Binary XML never
ships with Crysis 1 / Remastered; it appears in Crysis 2/3 assets.

Exposed API:
  load_bytes(data)   -> ET.Element   (auto-detects the encoding)
  load_stream(stream)-> ET.Element
  load_file(path)    -> ET.Element
"""

from __future__ import annotations

import io
import struct
from typing import BinaryIO
from xml.etree import ElementTree as ET

__all__ = ["load_bytes", "load_stream", "load_file"]

PBXML_MAGIC = b"pbxml\x00"
CRYXMLB_MAGIC = b"CryXmlB\x00"


# ------------------------------------------------------------- cry-int I/O


def _read_cry_int(stream: BinaryIO) -> int:
    """Variable-length int used by pbxml (7 bits per byte, high bit = more)."""
    current = stream.read(1)
    if not current:
        raise EOFError("Unexpected EOF in CryInt")
    cur = current[0]
    result = cur & 0x7F
    while (cur & 0x80) != 0:
        current = stream.read(1)
        if not current:
            raise EOFError("Unexpected EOF in CryInt")
        cur = current[0]
        result = (result << 7) | (cur & 0x7F)
    return result


def _read_cstring(stream: BinaryIO) -> str:
    """Read a NUL-terminated ASCII string."""
    out = bytearray()
    while True:
        b = stream.read(1)
        if not b or b == b"\x00":
            break
        out.extend(b)
    return out.decode("ascii", errors="replace")


# ------------------------------------------------------------------ public


def load_file(path: str) -> ET.Element:
    """Load any XML-ish file: binary pbxml/CryXmlB or plain XML."""
    with open(path, "rb") as fh:
        return load_stream(fh)


def load_bytes(data: bytes) -> ET.Element:
    """Load XML from a byte buffer (auto-detects the encoding)."""
    return load_stream(io.BytesIO(data))


def load_stream(stream: BinaryIO) -> ET.Element:
    """Load XML from a stream (auto-detects the encoding)."""
    if not stream.seekable():
        stream = io.BytesIO(stream.read())

    pos = stream.tell()
    peek = stream.read(max(len(PBXML_MAGIC), len(CRYXMLB_MAGIC)))
    stream.seek(pos)

    if peek.startswith(PBXML_MAGIC):
        stream.read(len(PBXML_MAGIC))
        return _pbxml_element(stream)
    if peek.startswith(CRYXMLB_MAGIC):
        return _load_cryxmlb(stream)

    return ET.parse(stream).getroot()


# ------------------------------------------------------------------- pbxml


def _pbxml_element(stream: BinaryIO) -> ET.Element:
    n_children = _read_cry_int(stream)
    n_attrs = _read_cry_int(stream)
    name = _read_cstring(stream)

    el = ET.Element(name)
    for _ in range(n_attrs):
        key = _read_cstring(stream)
        value = _read_cstring(stream)
        el.set(key, value)

    text = _read_cstring(stream)
    if text:
        el.text = text

    for i in range(n_children):
        expected_length = _read_cry_int(stream)
        expected_position = stream.tell() + expected_length
        child = _pbxml_element(stream)
        el.append(child)
        # Last child has expected_length == 0 in the C# implementation.
        if i + 1 == n_children:
            if expected_length != 0:
                raise ValueError("pbxml: last child must have expected_length 0")
        else:
            if stream.tell() != expected_position:
                raise ValueError(
                    "pbxml: expected position %d, got %d" % (expected_position, stream.tell())
                )
    return el


# ----------------------------------------------------------------- CryXmlB


def _load_cryxmlb(stream: BinaryIO) -> ET.Element:
    """CryXmlB format: header + node/reference/content tables.

    Layout (as determined by studying Crysis 2/3 binary XML assets):

        char[8] magic "CryXmlB\\0"
        i32 fileLength
        i32 nodeTableOffset, i32 nodeTableCount   (28 bytes / entry)
        i32 referenceTableOffset, i32 referenceTableCount   (8 bytes / entry)
        i32 orderTableOffset, i32 orderTableCount   (4 bytes / entry)
        i32 contentOffset, i32 contentLength

    Each node entry (28 bytes):
        i32 NameOffset, i32 ItemType, i16 AttributeCount, i16 ChildCount,
        i32 ParentNodeId (-1 = root), i32 FirstAttributeIndex,
        i32 FirstChildIndex, i32 Reserved

    Strings live in the content area as NUL-terminated blobs whose
    offsets are relative to ``contentOffset``. Children are stitched via
    ParentNodeId (the order table is informational only).
    """
    magic = stream.read(len(CRYXMLB_MAGIC))
    if magic != CRYXMLB_MAGIC:
        raise ValueError("Not a CryXmlB stream")

    _file_length = struct.unpack("<i", stream.read(4))[0]
    node_table_offset = struct.unpack("<i", stream.read(4))[0]
    node_table_count = struct.unpack("<i", stream.read(4))[0]
    ref_table_offset = struct.unpack("<i", stream.read(4))[0]
    ref_table_count = struct.unpack("<i", stream.read(4))[0]
    _order_table_offset = struct.unpack("<i", stream.read(4))[0]
    _order_table_count = struct.unpack("<i", stream.read(4))[0]
    content_offset = struct.unpack("<i", stream.read(4))[0]
    content_length = struct.unpack("<i", stream.read(4))[0]

    # --- strings dictionary ---
    stream.seek(content_offset)
    blob = stream.read(content_length) if content_length > 0 else b""
    strings: dict[int, str] = {}
    cursor = 0
    while cursor < len(blob):
        end = blob.find(b"\x00", cursor)
        if end < 0:
            end = len(blob)
        strings[cursor] = blob[cursor:end].decode("utf-8", errors="replace")
        cursor = end + 1

    def s(off: int, default: str = "") -> str:
        return strings.get(off, default)

    # --- node table ---
    stream.seek(node_table_offset)
    nodes: list[dict] = []
    for _ in range(node_table_count):
        name_off, item_type = struct.unpack("<ii", stream.read(8))
        attr_count, child_count = struct.unpack("<hh", stream.read(4))
        parent_id, first_attr, first_child, _reserved = struct.unpack("<iiii", stream.read(16))
        nodes.append({
            "name_off": name_off,
            "item_type": item_type,
            "attr_count": attr_count,
            "child_count": child_count,
            "parent_id": parent_id,
            "first_attr": first_attr,
            "first_child": first_child,
        })

    # --- attribute reference table (key_off, val_off) ---
    stream.seek(ref_table_offset)
    attrs: list[tuple[int, int]] = [
        struct.unpack("<ii", stream.read(8)) for _ in range(ref_table_count)
    ]

    # --- build elements + attach attrs + stitch tree ---
    elements: dict[int, ET.Element] = {}
    attr_idx = 0
    root: ET.Element | None = None
    for node_id, nd in enumerate(nodes):
        el = ET.Element(s(nd["name_off"]))
        for _ in range(nd["attr_count"]):
            if attr_idx >= len(attrs):
                break  # malformed file guard
            k_off, v_off = attrs[attr_idx]
            attr_idx += 1
            el.set(s(k_off), s(v_off, "BUGGED"))
        elements[node_id] = el
        parent = elements.get(nd["parent_id"])
        if parent is not None:
            parent.append(el)
        elif root is None:
            root = el

    if root is None and elements:
        root = elements[0]
    if root is None:
        raise ValueError("CryXmlB: no nodes")
    return root
