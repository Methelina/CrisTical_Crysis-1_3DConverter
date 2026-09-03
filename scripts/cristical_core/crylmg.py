#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crylmg.py — CrisTical: Crysis .lmg (Locomotion Group) XML parser
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Parses locomotion-group files for every supported edition:

  - Crysis 1 / Remastered (legacy):  root <LocomotionGroup> with
    <BLENDTYPE> + <ExampleList> of Position/AName pairs
  - Crysis 2 (hybrid): the same <LocomotionGroup> root carrying the modern
    parametric tags — <CAPS>, <Dimensions>/<Param>, SetPara0..3 on examples,
    <ExamplePseudo>, <Blendable>, <VEGPARAMS>, <THRESHOLD> — mirroring the
    engine's GlobalAnimationHeaderLMG::LoadFromXML schema

Files are plain XML (never binary), loaded through cryxmlb.load_file so
CryXmlB/pbxml inputs still decode if they ever appear. Tag and attribute
matching is case-insensitive throughout.

  Structure (all optional tags absent in the simplest C1 files):
    <LocomotionGroup>
      <BLENDTYPE type="IROT" />            blend type 4-char code (raw string)
      <Caps code="RUN_" />                 capability code ("" when absent)
      <ExampleList>
        <Example Position=" 0, 0, 0" AName="hunter_idle_01"
                  SetPara0..3=".." PlaybackScale=".." />
      </ExampleList>
      <Dimensions>
        <Param name=".." min=".." max=".." cells=".." locked=".." scale=".." JointName=".." skey=".." ekey=".." />
      </Dimensions>
      <ExamplePseudo>
        <Pseudo p0=".." p1=".." w0=".." w1=".." />
      </ExamplePseudo>
      <Blendable>
        <Face p0=".." p1=".." ... p7=".." />
      </Blendable>
      <VEGPARAMS ... />                    all attributes kept as a string dict
      <THRESHOLD tz=".." />
      <JointList>                          only in some game versions
        <Joint name="Bip01" />             attribute casing varies — parsed case-insensitively
      </JointList>

Exposed API:
  parse_lmg(lmg_path) -> dict
    blend_type  : str    — raw 4-char blend code from <BLENDTYPE type="..."/>
    examples    : list   — [{"position": [x,y,z floats], "anim_name": str, "set_para": [float|None]*4, "playback_scale": float}, ...]
    joints      : list   — str joint names from <JointList>/<Joint> (empty if absent)
    caps        : str    — value of <CAPS code="..."/> ("" when absent)
    dimensions  : list   — [{"name": str, "min": float, "max": float, "cells": int, "locked": bool, +optional "scale"/"joint_name"/"skey"/"ekey"}, ...]
    pseudos     : list   — [{"p0": int, "p1": int, "w0": float, "w1": float}, ...]
    faces       : list   — [{"points": [ints]}, ...]
    vegparams   : dict   — all <VEGPARAMS> attributes as str ({} when absent)
    threshold   : float  — tz of <THRESHOLD> (None when absent)
  lmg_to_json(lmg_path) -> str
    Pretty JSON string of the parsed result.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_position(raw):
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part == "":
            continue
        try:
            out.append(float(part))
        except ValueError:
            pass
    return out


def _to_float(value, default=0.0):
    if value is None:
        return default
    value = value.strip()
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _to_int(value, default=0):
    if value is None:
        return default
    value = value.strip()
    if value == "":
        return default
    try:
        return int(value)
    except ValueError:
        try:
            return int(float(value))
        except ValueError:
            return default


def _empty():
    return {
        "blend_type": "",
        "examples": [],
        "joints": [],
        "caps": "",
        "dimensions": [],
        "pseudos": [],
        "faces": [],
        "vegparams": {},
        "threshold": None,
    }


def _attr(node, *names):
    """Read an XML attribute by any of the given names (case-insensitive).

    The .lmg XML files are not consistent about attribute casing: real
    .lmg files use ``<Joint name="..."/>`` (lowercase) while the docs and
    some tools use ``<Joint Name="..."/>``.  Read case-insensitively so
    either form works.
    """
    wanted = set(n.lower() for n in names)
    for key, value in node.attrib.items():
        if key.lower() in wanted:
            return value
    return None


def parse_lmg(lmg_path):
    empty = _empty()
    try:
        try:
            from .cryxmlb import load_file as _load_xml
        except ImportError:
            from cryxmlb import load_file as _load_xml
        root = _load_xml(lmg_path)
    except (ET.ParseError, OSError, ValueError):
        return empty
    if root is None or not (root.tag or "").upper() == "LOCOMOTIONGROUP":
        return empty

    result = empty
    for child in root:
        tag = (child.tag or "").upper()
        if tag == "BLENDTYPE":
            result["blend_type"] = _attr(child, "type") or ""
        elif tag == "CAPS":
            result["caps"] = _attr(child, "code", "Code") or ""
        elif tag == "DIMENSIONS":
            for param in child:
                if (param.tag or "").upper() != "PARAM":
                    continue
                dim = {
                    "name": _attr(param, "name", "Name") or "",
                    "min": _to_float(_attr(param, "min"), 0.0),
                    "max": _to_float(_attr(param, "max"), 0.0),
                    "cells": _to_int(_attr(param, "cells"), 3),
                    "locked": (_attr(param, "locked") or "").strip().lower() in ("1", "true"),
                }
                scale = _attr(param, "scale")
                if scale is not None:
                    dim["scale"] = _to_float(scale)
                joint_name = _attr(param, "JointName", "jointname")
                if joint_name is not None:
                    dim["joint_name"] = joint_name
                skey = _attr(param, "skey")
                if skey is not None:
                    dim["skey"] = _to_float(skey)
                ekey = _attr(param, "ekey")
                if ekey is not None:
                    dim["ekey"] = _to_float(ekey)
                result["dimensions"].append(dim)
        elif tag == "EXAMPLELIST":
            for ex in child.findall("Example"):
                position = _parse_position(_attr(ex, "Position") or "")
                anim_name = _attr(ex, "AName", "name", "animname") or ""
                set_para = [None, None, None, None]
                for i in range(4):
                    sp = _attr(ex, f"SetPara{i}")
                    if sp is not None:
                        set_para[i] = _to_float(sp)
                playback_scale = _to_float(_attr(ex, "PlaybackScale", "playbackscale"), 1.0)
                result["examples"].append({
                    "position": position,
                    "anim_name": anim_name,
                    "set_para": set_para,
                    "playback_scale": playback_scale,
                })
        elif tag == "JOINTLIST":
            for jn in child.findall("Joint"):
                name = _attr(jn, "Name", "name") or ""
                if name:
                    result["joints"].append(name)
        elif tag == "EXAMPLEPSEUDO":
            for pseudo in child:
                if (pseudo.tag or "").upper() != "PSEUDO":
                    continue
                result["pseudos"].append({
                    "p0": _to_int(_attr(pseudo, "p0"), -1),
                    "p1": _to_int(_attr(pseudo, "p1"), -1),
                    "w0": _to_float(_attr(pseudo, "w0"), 1.0),
                    "w1": _to_float(_attr(pseudo, "w1"), 1.0),
                })
        elif tag == "BLENDABLE":
            for face in child:
                if (face.tag or "").upper() != "FACE":
                    continue
                points = []
                for i in range(8):
                    pv = _attr(face, f"p{i}")
                    if pv is not None:
                        points.append(_to_int(pv))
                result["faces"].append({"points": points})
        elif tag == "VEGPARAMS":
            result["vegparams"] = dict(child.attrib)
        elif tag == "THRESHOLD":
            tz = _attr(child, "tz")
            if tz is not None:
                result["threshold"] = _to_float(tz)

    return result


def lmg_to_json(lmg_path):
    return json.dumps(parse_lmg(lmg_path), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: crylmg.py <file.lmg>\n")
        sys.exit(1)
    print(lmg_to_json(sys.argv[1]))


if __name__ == "__main__":
    main()
