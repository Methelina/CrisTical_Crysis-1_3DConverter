#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crylmg.py — Crysis 1 .lmg (Locomotion Group) XML parser
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0

=== .lmg (Locomotion Group) XML parser ===

Locomotion Group files are plain XML (root <LocomotionGroup>), never binary.
They define blend-space locomotion groups referenced by character skeletons.

Structure:
  <LocomotionGroup>
    <BLENDTYPE type="IROT" />            blend type 4-char code (raw string)
    <ExampleList>
      <Example Position=" 0, 0, 0" AName="hunter_idle_01" />
    </ExampleList>
    <Caps code="..." />
    <JointList>                          only in some game versions
      <Joint name="Bip01" />             attribute casing varies — parsed case-insensitively
    </JointList>

Exposed API:
  parse_lmg(lmg_path) -> dict
    blend_type  : str   — raw 4-char blend code from <BLENDTYPE type="..."/>
    examples    : list  — [{"position": [x,y,z floats], "anim_name": str}, ...]
    joints      : list  — str joint names from <JointList>/<Joint> (empty if absent)
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


def _empty():
    return {"blend_type": "", "examples": [], "joints": []}


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
        tree = ET.parse(lmg_path)
    except (ET.ParseError, OSError):
        return empty

    root = tree.getroot()
    if root is None or not (root.tag or "").upper() == "LOCOMOTIONGROUP":
        return empty

    result = empty
    for child in root:
        tag = (child.tag or "").upper()
        if tag == "BLENDTYPE":
            result["blend_type"] = _attr(child, "type") or ""
        elif tag == "EXAMPLELIST":
            for ex in child.findall("Example"):
                position = _parse_position(_attr(ex, "Position") or "")
                anim_name = _attr(ex, "AName", "name", "animname") or ""
                result["examples"].append({"position": position, "anim_name": anim_name})
        elif tag == "JOINTLIST":
            for jn in child.findall("Joint"):
                name = _attr(jn, "Name", "name") or ""
                if name:
                    result["joints"].append(name)

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
