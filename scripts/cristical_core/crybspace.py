#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crybspace.py — CrisTical: CryEngine .bspace / .comb (blend-space) XML parser
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0

Parses blend-space and combined blend-space assets used by the parametric
locomotion system (Crysis 2/3-era; the engine drops legacy .lmg here):

  <ParaGroup>            — a 1D/2D/3D blend-space (.bspace): dimensions,
                          example clips with per-axis SetPara values, pseudo
                          examples, blend faces, optional VGrid
  <CombinedBlendSpace>   — a combined blend-space (.comb) choosing between
                          sub blend-spaces per dimension

Schema verified against the engine sources (LoadFromXml):
  CryEngine CE35  Code/CryEngine/CryAnimation/GlobalAnimationHeaderLMG.cpp
  Lumberyard     .../CharacterTool/BlendSpace.cpp (BlendSpace::LoadFromXml,
                   CombinedBlendSpace::LoadFromXml — the fullest variant)

Files are plain-text XML, loaded through cryxmlb.load_bytes so CryXmlB/pbxml
inputs still decode. Tag and attribute matching is case-insensitive: real
assets mix casing (``AName`` where the engine reads ``aname``).

Exposed API:
  parse_bspace_data(data: bytes) -> dict   parse a <ParaGroup> from raw bytes
  parse_comb_data(data: bytes) -> dict     parse a <CombinedBlendSpace> from bytes
  parse_file(path: str) -> dict            read from disk, dispatch on root tag
  to_json(obj: dict) -> str                pretty JSON string (indent=2)
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET

try:
    from .cryxmlb import load_bytes as _load_xml
except ImportError:
    from cryxmlb import load_bytes as _load_xml

__all__ = ["parse_bspace_data", "parse_comb_data", "parse_file", "to_json"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _attr(node: ET.Element, *names: str) -> str | None:
    """Read an XML attribute by any of the given names (case-insensitive).

    Mirrors the ``_attr`` helper from crylmg.py: real blend-space assets use
    inconsistent attribute casing (``AName`` vs the engine's ``aname``), so we
    normalize the comparison to lower case.
    """
    wanted = {n.lower() for n in names}
    for key, value in node.attrib.items():
        if key.lower() in wanted:
            return value
    return None


def _to_float(raw: str | None, default: float = 0.0) -> float:
    """Tolerant float parse: leading ``+``/whitespace, empty/unset -> default."""
    if raw is None:
        return default
    s = raw.strip()
    if s == "":
        return default
    try:
        return float(s)
    except (ValueError, TypeError):
        return default


def _to_int(raw: str | None, default: int = 0) -> int:
    """Tolerant int parse: handles ``+`` prefix and float-encoded ints (``9.0``)."""
    if raw is None:
        return default
    s = raw.strip().lstrip("+")
    if s == "":
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def _to_bool(raw: str | None) -> bool:
    """Bool parse: True only for ``1``/``true`` (case-insensitive)."""
    if raw is None:
        return False
    return raw.strip().lower() in ("1", "true")


def _float_or_str(raw: str) -> float | str:
    """Float when parseable, otherwise the raw (stripped) string."""
    s = raw.strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return s


def _float_or_none(raw: str | None) -> float | None:
    """Float when present and parseable, else None (present-but-empty -> None)."""
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _children(node: ET.Element, tag: str):
    """Yield direct children whose tag matches ``tag`` case-insensitively."""
    want = tag.upper()
    for child in node:
        if (child.tag or "").upper() == want:
            yield child


def _first(node: ET.Element, tag: str) -> ET.Element | None:
    """First case-insensitive child matching ``tag`` (or None)."""
    for child in _children(node, tag):
        return child
    return None


# ---------------------------------------------------------------------------
# Shared sub-parsers
# ---------------------------------------------------------------------------

_FACE_POINTS = ("p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7")
_VGRID_INDICES = ("i0", "i1", "i2", "i3", "i4", "i5", "i6", "i7")
_VGRID_WEIGHTS = ("w0", "w1", "w2", "w3", "w4", "w5", "w6", "w7")


def _motion_combinations(node: ET.Element) -> list[dict]:
    """``<MotionCombination>`` -> list of ``{"style": str, "legacy": True}``."""
    out: list[dict] = []
    mc = _first(node, "MotionCombination")
    if mc is not None:
        for ns in _children(mc, "NewStyle"):
            out.append({"style": _attr(ns, "Style") or "", "legacy": True})
    return out


def _joints(node: ET.Element) -> list[str]:
    """``<JointList>`` -> list of joint names (case-insensitive ``Name``)."""
    out: list[str] = []
    jl = _first(node, "JointList")
    if jl is not None:
        for j in _children(jl, "Joint"):
            name = _attr(j, "Name", "name") or ""
            if name:
                out.append(name)
    return out


def _additional_extraction(node: ET.Element) -> list[dict]:
    """``<AdditionalExtraction>`` -> list of ``{"name": str}``."""
    out: list[dict] = []
    ae = _first(node, "AdditionalExtraction")
    if ae is not None:
        for p in _children(ae, "Param"):
            out.append({"name": _attr(p, "name") or ""})
    return out


# ---------------------------------------------------------------------------
# .bspace (ParaGroup) builder
# ---------------------------------------------------------------------------

def _bspace_dimension(param: ET.Element) -> dict:
    """One <Param> inside <Dimensions> of a ParaGroup."""
    d: dict = {
        "name": _attr(param, "name") or "",
        "min": _to_float(_attr(param, "min"), 0.0),
        "max": _to_float(_attr(param, "max"), 0.0),
        "cells": max(3, _to_int(_attr(param, "cells"), 0)),
        "locked": _to_bool(_attr(param, "locked")),
    }
    raw = _attr(param, "scale")
    if raw is not None:
        d["scale"] = _to_float(raw, 0.0)
    raw = _attr(param, "JointName", "jointname")
    if raw is not None:
        d["joint_name"] = raw
    for key, attr_name in (("skey", "skey"), ("ekey", "ekey")):
        raw = _attr(param, attr_name)
        if raw is not None:
            d[key] = _float_or_str(raw)
    return d


def _bspace_example(ex: ET.Element) -> dict:
    """One <Example> inside <ExampleList> of a ParaGroup."""
    return {
        "anim_name": _attr(ex, "AName") or "",
        "playback_scale": _to_float(_attr(ex, "PlaybackScale"), 1.0),
        "set_para": [_float_or_none(_attr(ex, "SetPara%d" % i)) for i in range(4)],
        "use_directly_for_delta_motion": [
            _to_bool(_attr(ex, "UseDirectlyForDeltaMotion%d" % i)) for i in range(4)
        ],
    }


def _build_bspace(root: ET.Element) -> dict:
    """Build the ParaGroup result dict from an already-parsed root element."""
    result: dict = {
        "kind": "bspace",
        "threshold": None,
        "vegparams": {},
        "dimensions": [],
        "examples": [],
        "pseudos": [],
        "faces": [],
        "vgrid": [],
        "motion_combinations": [],
        "joints": [],
        "additional_extraction": [],
    }

    th = _first(root, "THRESHOLD")
    if th is not None:
        result["threshold"] = _to_float(_attr(th, "tz"), 0.0)

    vp = _first(root, "VEGPARAMS")
    if vp is not None:
        result["vegparams"] = dict(vp.attrib)

    dim = _first(root, "Dimensions")
    if dim is not None:
        for p in _children(dim, "Param"):
            result["dimensions"].append(_bspace_dimension(p))

    el = _first(root, "ExampleList")
    if el is not None:
        for ex in _children(el, "Example"):
            result["examples"].append(_bspace_example(ex))

    ep = _first(root, "ExamplePseudo")
    if ep is not None:
        for ps in _children(ep, "Pseudo"):
            result["pseudos"].append({
                "p0": _to_int(_attr(ps, "p0"), -1),
                "p1": _to_int(_attr(ps, "p1"), -1),
                "w0": _to_float(_attr(ps, "w0"), 1.0),
                "w1": _to_float(_attr(ps, "w1"), 1.0),
            })

    bl = _first(root, "Blendable")
    if bl is not None:
        for f in _children(bl, "Face"):
            points: list[int] = []
            for n in _FACE_POINTS:
                v = _attr(f, n)
                if v is not None:
                    points.append(_to_int(v))
            result["faces"].append({"points": points})

    vg = _first(root, "VGrid")
    if vg is not None:
        for vx in _children(vg, "VExample"):
            indices: list[int] = []
            for n in _VGRID_INDICES:
                v = _attr(vx, n)
                if v is not None:
                    indices.append(_to_int(v))
            weights: list[float] = []
            for n in _VGRID_WEIGHTS:
                v = _attr(vx, n)
                if v is not None:
                    weights.append(_to_float(v))
            result["vgrid"].append({"indices": indices, "weights": weights})

    result["motion_combinations"] = _motion_combinations(root)
    result["joints"] = _joints(root)
    result["additional_extraction"] = _additional_extraction(root)
    return result


# ---------------------------------------------------------------------------
# .comb (CombinedBlendSpace) builder
# ---------------------------------------------------------------------------

def _build_comb(root: ET.Element) -> dict:
    """Build the CombinedBlendSpace result dict from an already-parsed root."""
    result: dict = {
        "kind": "comb",
        "vegparams": {},
        "dimensions": [],
        "additional_extraction": [],
        "blend_spaces": [],
        "motion_combinations": [],
        "joints": [],
    }

    vp = _first(root, "VEGPARAMS")
    if vp is not None:
        result["vegparams"] = dict(vp.attrib)

    dim = _first(root, "Dimensions")
    if dim is not None:
        for p in _children(dim, "Param"):
            result["dimensions"].append({
                "name": _attr(p, "name") or "",
                "para_scale": _to_float(_attr(p, "ParaScale"), 1.0),
                "choose_blend_space": _to_bool(_attr(p, "ChooseBlendSpace")),
                "locked": _to_bool(_attr(p, "locked")),
            })

    result["additional_extraction"] = _additional_extraction(root)

    bs = _first(root, "BlendSpaces")
    if bs is not None:
        for p in _children(bs, "BlendSpace"):
            result["blend_spaces"].append(
                {"name": _attr(p, "AName", "aname") or ""}
            )

    result["motion_combinations"] = _motion_combinations(root)
    result["joints"] = _joints(root)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_bspace_data(data: bytes) -> dict:
    """Parse a .bspace (root <ParaGroup>) from raw bytes.

    Returns the ParaGroup dict, or ``{"error": str}`` when the XML is not a
    ParaGroup / not parseable.
    """
    try:
        root = _load_xml(data)
    except Exception:
        return {"error": "unparseable XML"}
    if root is None:
        return {"error": "no XML root"}
    if (root.tag or "").upper() != "PARAGROUP":
        return {"error": "unexpected root tag: %s" % (root.tag or "")}
    return _build_bspace(root)


def parse_comb_data(data: bytes) -> dict:
    """Parse a .comb (root <CombinedBlendSpace>) from raw bytes.

    Returns the CombinedBlendSpace dict, or ``{"error": str}`` on wrong root.
    """
    try:
        root = _load_xml(data)
    except Exception:
        return {"error": "unparseable XML"}
    if root is None:
        return {"error": "no XML root"}
    if (root.tag or "").upper() != "COMBINEDBLENDSPACE":
        return {"error": "unexpected root tag: %s" % (root.tag or "")}
    return _build_comb(root)


def parse_file(path: str) -> dict:
    """Read a .bspace/.comb file from disk and dispatch on its root tag.

    Returns the bspace dict, the comb dict, or ``{"error": ...}``.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        return {"error": str(e)}
    try:
        root = _load_xml(data)
    except Exception:
        return {"error": "unparseable XML"}
    if root is None:
        return {"error": "no XML root"}
    tag = (root.tag or "").upper()
    if tag == "PARAGROUP":
        return _build_bspace(root)
    if tag == "COMBINEDBLENDSPACE":
        return _build_comb(root)
    return {"error": "unexpected root tag: %s" % (root.tag or "")}


def to_json(obj: dict) -> str:
    """Pretty JSON string (indent=2, ensure_ascii=False)."""
    return json.dumps(obj, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: crybspace.py <file.bspace|.comb>\n")
        sys.exit(1)
    print(to_json(parse_file(sys.argv[1])))


if __name__ == "__main__":
    main()
