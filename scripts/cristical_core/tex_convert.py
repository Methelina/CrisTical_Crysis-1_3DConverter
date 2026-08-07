#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tex_convert.py — Crysis material/texture converter (MTL -> PNG + glTF materials)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0
"""

import argparse
import os
import struct
import sys
import tempfile

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
_TEMP_DIR = os.path.join(_PROJECT_ROOT, "_temp")


def _project_temp():
    os.makedirs(_TEMP_DIR, exist_ok=True)
    return _TEMP_DIR

import xml.etree.ElementTree as ET
from PIL import Image
import numpy as np


def _unsplit_dds(dds_path):
    dds_dir = os.path.dirname(dds_path)
    dds_base = os.path.splitext(os.path.basename(dds_path))[0]
    if dds_base.lower().endswith(".dds"):
        dds_base = os.path.splitext(dds_base)[0]

    f0 = os.path.join(dds_dir, dds_base + ".dds.0")
    if not os.path.isfile(f0):
        return dds_path

    header_path = dds_path if os.path.isfile(dds_path) and not dds_path.endswith(".0") else f0
    with open(header_path, "rb") as f:
        header = f.read()
    if len(header) < 128:
        return dds_path

    mip_count = max(1, struct.unpack_from("<I", header, 28)[0])
    dxt10 = (header[84:88] == b"DX10")
    total_hdr = 128 + (20 if dxt10 else 0)

    if not dds_path.endswith(".0") and len(header) > total_hdr + 1024:
        return dds_path

    highest = 0
    dds_base_lower = dds_base.lower()
    prefix = dds_base_lower + ".dds."
    for fn in os.listdir(dds_dir):
        fn_lower = fn.lower()
        if fn_lower.startswith(prefix):
            rest = fn_lower[len(prefix):]
            if rest.isdigit():
                n = int(rest)
                if n > highest:
                    highest = n
    if highest == 0:
        return dds_path

    mip_file = os.path.join(dds_dir, "%s.dds.%d" % (dds_base, highest))
    with open(mip_file, "rb") as mf:
        mip0_data = mf.read()

    fd, tmp = tempfile.mkstemp(suffix=".dds", prefix="cristical_", dir=_project_temp())
    os.close(fd)
    with open(tmp, "wb") as f:
        with open(header_path, "rb") as hf:
            f.write(hf.read(total_hdr))
        f.write(mip0_data)
    return tmp


def _is_ddna(path):
    base = os.path.basename(path).lower()
    return "_ddna" in base


def _find_texture(mtl_file_ref, game_dirs):
    ref = mtl_file_ref.replace("/", os.sep)
    if os.path.isabs(ref):
        candidates = [ref]
    else:
        candidates = [os.path.join(d, ref) for d in game_dirs]

    for path in candidates:
        png = os.path.splitext(path)[0] + ".png"
        if os.path.isfile(png):
            return png, png
        dds = os.path.splitext(path)[0] + ".dds"
        if os.path.isfile(dds):
            return dds, _unsplit_dds(dds)
        dds0 = dds + ".0"
        if os.path.isfile(dds0):
            return dds0, _unsplit_dds(dds0)

    return None, None


def _is_ddn(path):
    base = os.path.basename(path).lower()
    if "_ddn" in base or "_ddna" in base:
        return True
    if path.lower().endswith(".dds"):
        try:
            im = Image.open(path)
            arr = np.array(im)
            if arr.ndim > 2 and arr.shape[2] >= 3 and arr[:, :, 2].max() < 5:
                return True
        except Exception:
            pass
    return False


def _convert_to_png(src_path, dst_path, is_normal=False):
    try:
        im = Image.open(src_path)
    except Exception as e:
        print("  [tex] WARN: cannot open %s: %s" % (os.path.basename(src_path), e))
        return [dst_path]
    arr = np.array(im)
    result = [dst_path]

    if is_normal and arr.ndim > 2 and arr.shape[2] >= 2 and arr[:, :, 2].max() < 5:
        f = arr.astype(np.float32) / 127.5 - 1.0
        z_sq = 1.0 - f[:, :, 0]**2 - f[:, :, 1]**2
        if arr.shape[2] < 3:
            arr = np.dstack([arr, np.zeros_like(arr[:, :, 0])])
        arr[:, :, 2] = ((np.sqrt(np.maximum(z_sq, 0.0)) + 1.0) * 127.5).clip(0, 255).astype(np.uint8)

    if arr.ndim > 2 and arr.shape[2] >= 4:
        alpha = arr[:, :, 3].copy()
        if alpha.max() > alpha.min():
            if _is_ddna(src_path):
                a_name = dst_path.rsplit(".", 1)[0] + "_gloss.png"
            else:
                a_name = dst_path.rsplit(".", 1)[0] + "_emiss.png"
            Image.fromarray(alpha, "L").save(a_name, "PNG")
            result.append(a_name)
        arr = arr[:, :, :3]

    res = Image.fromarray(arr, "RGB")
    res.save(dst_path, "PNG")
    return result


def _shininess_to_roughness(s):
    s = max(1.0, min(256.0, float(s)))
    return (2.0 / (s + 2.0)) ** 0.25


def convert_materials(mtl_path, game_dirs, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    tree = ET.parse(mtl_path)
    root = tree.getroot()
    sub_mats = root.find("SubMaterials")
    if sub_mats is None:
        if root.tag == "Material":
            sub_mats = root
        else:
            print("[tex] no SubMaterials in %s" % mtl_path)
            return [], [], [], {}

    materials = []
    mat_info = []
    tex_index = {}
    images = []
    tex_sources = {}
    tex_count = 0

    def ensure_texture(file_ref, mat_name, map_type):
        nonlocal tex_count
        orig_path, src_path = _find_texture(file_ref, game_dirs)
        if not src_path:
            return None
        key = (mat_name, map_type)
        if key not in tex_index:
            prefix = mat_name.lower().replace(" ", "_") + "_" + map_type.lower()
            png_name = prefix + ".png"
            dst = os.path.join(out_dir, png_name)
            is_norm = (map_type == "normal")
            try:
                generated = _convert_to_png(src_path, dst, is_normal=is_norm)
            except Exception as e:
                print("  %s/%s: FAILED %s — %s" % (mat_name, map_type, os.path.basename(src_path), e))
                return None
            if generated is None:
                return None
            tex_index[key] = tex_count
            images.append({"name": generated[0], "index": tex_count})
            tex_sources[generated[0]] = orig_path
            tex_count += 1
            print("  %s/%s: %s -> %s" % (mat_name, map_type, os.path.basename(orig_path), os.path.basename(generated[0])))
            for extra in generated[1:]:
                extra_type = "emission" if "_emiss" in extra else "gloss" if "_gloss" in extra else "extra"
                extra_key = (mat_name, extra_type)
                tex_index[extra_key] = tex_count
                images.append({"name": os.path.basename(extra), "index": tex_count})
                tex_sources[os.path.basename(extra)] = orig_path
                tex_count += 1
                print("  %s/%s: %s -> %s" % (mat_name, extra_type, os.path.basename(orig_path), os.path.basename(extra)))
        return {"index": tex_index[key]}

    for mat_el in sub_mats.findall("Material"):
        name = mat_el.get("Name", "material")
        shader = mat_el.get("Shader", "")
        if shader == "Nodraw":
            continue

        diff_col = mat_el.get("Diffuse", "1,1,1")
        spec_col = mat_el.get("Specular", "1,1,1")
        shininess = float(mat_el.get("Shininess", "10"))
        opacity = float(mat_el.get("Opacity", "1.0"))
        r, g, b = [float(x) for x in diff_col.split(",")]
        sr, sg, sb = [float(x) for x in spec_col.split(",")]
        roughness = _shininess_to_roughness(shininess)

        gltf_mat = {
            "name": name,
            "pbrMetallicRoughness": {
                "baseColorFactor": [r, g, b, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": roughness,
            },
        }

        tex_node = mat_el.find("Textures")
        if tex_node is not None:
            for tex in tex_node.findall("Texture"):
                mtype = tex.get("Map", "")
                file_ref = tex.get("File", "")
                if mtype == "Diffuse":
                    t = ensure_texture(file_ref, name, "diffuse")
                    if t:
                        gltf_mat["pbrMetallicRoughness"]["baseColorTexture"] = t
                elif mtype in ("Bumpmap", "Normalmap"):
                    t = ensure_texture(file_ref, name, "normal")
                    if t:
                        gltf_mat["normalTexture"] = t
                elif mtype == "Specular":
                    ensure_texture(file_ref, name, "specular")

        e = tex_index.get((name, "emission"))
        if e is not None:
            gltf_mat["emissiveTexture"] = {"index": e}
            gltf_mat["emissiveFactor"] = [1.0, 1.0, 1.0]

        materials.append(gltf_mat)
        mat_info.append({
            "name": name, "shininess": shininess, "diffuse": diff_col,
            "specular": spec_col, "opacity": opacity, "shader": shader,
        })

    return materials, [img["name"] for img in sorted(images, key=lambda x: x["index"])], mat_info, tex_sources
