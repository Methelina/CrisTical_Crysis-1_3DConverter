#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
tex_convert.py — CrisTical: material/texture converter (MTL -> PNG + glTF materials)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0
"""

import argparse
import hashlib
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

try:
    from .cryvfs import IPackFileSystem, mount_layers, materialize, mount_game, index_open_bytes
    from .cryxmlb import load_file as _load_xml
except ImportError:  # running as a script (python tex_convert.py)
    from cryvfs import IPackFileSystem, mount_layers, materialize, mount_game, index_open_bytes
    from cryxmlb import load_file as _load_xml


_VFS_LAYERS_CACHE = {}
_TEMP_TEX = os.path.join(os.path.dirname(os.path.dirname(_TEMP_DIR)), "temp", "tex_vfs")


def _get_layers(game_dirs):
    """Memoized per-root VFS layers (loose first, then paks). Empty list when no dirs."""
    key = tuple(str(d) for d in game_dirs)
    cached = _VFS_LAYERS_CACHE.get(key)
    if cached is not None:
        return cached
    layers = mount_layers([str(d) for d in game_dirs]) if game_dirs else []
    _VFS_LAYERS_CACHE[key] = layers
    return layers


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


def _unsplit_dds_vfs(layer: IPackFileSystem, rel_dds: str, temp_dir: str) -> str:
    """Combine a split DDS (header + .0/.1/... sidecars) from one VFS layer.

    Mirrors :func:`_unsplit_dds` but sources sibling files through ``layer``
    instead of the on-disk directory. A loose hit (``real_path`` set) is
    delegated to the original filesystem unsplitter so behavior is identical.
    """
    real = layer.real_path(rel_dds)
    if real is not None:
        return _unsplit_dds(real)

    base = os.path.basename(rel_dds)
    dds_base = os.path.splitext(base)[0]
    if dds_base.lower().endswith(".dds"):
        dds_base = os.path.splitext(dds_base)[0]

    if "/" in rel_dds:
        ddir = os.path.dirname(rel_dds)
        f0_rel = ddir + "/" + dds_base + ".dds.0"
        mip_dir = ddir + "/*"
    else:
        ddir = ""
        f0_rel = dds_base + ".dds.0"
        mip_dir = "*"

    if not layer.exists(f0_rel):
        mat = materialize(layer, rel_dds, temp_dir)
        return mat if mat is not None else rel_dds

    is_dotzero = rel_dds.endswith(".0")
    if (not is_dotzero) and layer.exists(rel_dds):
        header_rel = rel_dds
    else:
        header_rel = f0_rel

    header_mat = materialize(layer, header_rel, temp_dir)
    with open(header_mat, "rb") as f:
        header = f.read()
    if len(header) < 128:
        return header_mat

    mip_count = max(1, struct.unpack_from("<I", header, 28)[0])
    dxt10 = (header[84:88] == b"DX10")
    total_hdr = 128 + (20 if dxt10 else 0)

    if (not is_dotzero) and len(header) > total_hdr + 1024:
        return header_mat

    highest = 0
    prefix = dds_base.lower() + ".dds."
    for name in layer.glob(mip_dir):
        fn = os.path.basename(name).lower()
        if fn.startswith(prefix):
            rest = fn[len(prefix):]
            if rest.isdigit():
                n = int(rest)
                if n > highest:
                    highest = n
    if highest == 0:
        return header_mat

    mip_rel = (ddir + "/" + dds_base + ".dds.%d" % highest) if ddir else (dds_base + ".dds.%d" % highest)
    mip_mat = materialize(layer, mip_rel, temp_dir)
    with open(mip_mat, "rb") as f:
        mip0_data = f.read()

    fd, tmp = tempfile.mkstemp(suffix=".dds", prefix="cristical_", dir=_project_temp())
    os.close(fd)
    with open(tmp, "wb") as f:
        f.write(header[:total_hdr])
        f.write(mip0_data)
    return tmp


def _is_ddna(path):
    base = os.path.basename(path).lower()
    return "_ddna" in base


# ----------------------------------------------------------------------
# Texture-slot suffix conventions (ENGINE-derived, not invented):
# Crysis texture file names carry a slot suffix written by the resource
# compiler / art pipeline (TextureHelpers.cpp suffix table):
#   _diff/_dif = Diffuse, _ddn = normal map (BC5, RG only),
#   _ddna = normal map with attached smoothness alpha
#   (FT_HAS_ATTACHED_ALPHA, mirrored into EFTT_SMOOTHNESS),
#   _spec = specular color (alpha may carry gloss power when the
#   material sets %SPECULARPOW_GLOSSALPHA), _em = emittance,
#   _sss = subsurface mask, _displ = height, _detail = detail layer,
#   _trans = translucency, _cm/_env = environment cubemap.
# The converter keeps the ORIGINAL asset stem and suffix when writing
# PNGs (name_suffix.png), so outputs stay traceable to their source.
# ----------------------------------------------------------------------

_TEX_SUFFIXES = (
    "_ddna", "_ddn", "_diff", "_dif", "_spec", "_em", "_sss",
    "_displ", "_detail", "_trans", "_cm", "_env", "_mask", "_gloss",
    "_ddndif",
)


def _stem_and_suffix(path: str) -> tuple[str, str]:
    """Split a texture file name into (stem, suffix) by the known slot
    suffixes. Falls back to (full-stem, "") when nothing matches."""
    base = os.path.basename(path).lower()
    for ext in (".dds.0", ".dds", ".tif", ".png"):
        if base.endswith(ext):
            base = base[: -len(ext)]
            break
    for suf in _TEX_SUFFIXES:
        if base.endswith(suf):
            return base[: -len(suf)], suf
    return base, ""


# XML <Texture Map="..."> name -> slot tag. Taken from the ENGINE's own
# s_TexSlotSemantics table (MaterialHelpers.cpp) plus the shader
# template aliases ($Bump, $GlossNormalA, ...). Any Map name not listed
# here falls through to a misc_<name> slot and is STILL exported -
# nothing a material references is ever dropped.
_SLOT_ALIASES = {
    "Diffuse": "diffuse",
    "Bumpmap": "normal", "Normal": "normal", "Normalmap": "normal",
    "Specular": "specular", "Specular2": "specular2",
    "Environment": "environment",
    "Detail": "detail",
    "SecondSmoothness": "secondsmoothness",
    "Heightmap": "height", "Height": "height",
    "Decal": "decal",
    "SubSurface": "subsurface",
    "Custom": "custom", "CustomSecondary": "customsecondary",
    "[1] Custom": "customsecondary",
    "Opacity": "opacity",
    "Smoothness": "smoothness",
    "Emittance": "emissive", "Emissive": "emissive",
    "Occlusion": "occlusion",
}

# Display tags for cross-slot normal-map exports: user-facing names
# follow the art-pipeline vocabulary (SubSurface -> "sss").
_SLOT_DISPLAY = {
    "subsurface": "sss",
}


def _material_signals(mat_el) -> dict:
    """Engine-signalled channel usage for ONE <Material> element.

    Reads the same attributes the engine's material parser does
    (MakeMaterialFromXml + shader PublicParams):
      - StringGenMask/GenMask: shader-generation bits such as
        %SPECULARPOW_GLOSSALPHA (specular alpha = gloss power),
        %GLOSS_MAP (gloss texture slot in use), %ALPHAGLOW (diffuse
        alpha drives glow);
      - PublicParams channel selectors: SpecMapChannelR/G/B and
        GlossMapChannelB pick which specular-map channels carry the
        specular mask and the gloss (C3 body_spec: R+G = spec mask,
        B = gloss);
      - GlowAmount / Emissive: constant emittance.
    Returns a dict of booleans/values the PNG writer keys on.
    """
    out = {
        "gloss_from_spec_alpha": False,
        "gloss_channel": None,      # "R"|"G"|"B" of the specular map
        "spec_mask_channels": (),   # which channels are the spec colour
        "glow": False,
        "glow_amount": 0.0,
        "emissive": "0,0,0",
    }
    gen = (mat_el.get("StringGenMask") or "").upper()
    if "SPECULARPOW_GLOSSALPHA" in gen:
        out["gloss_from_spec_alpha"] = True
    if "GLOSS_MAP" in gen:
        out["gloss_map"] = True
    if "ALPHAGLOW" in gen:
        out["glow"] = True
    pub = mat_el.find("PublicParams")
    if pub is not None:
        b = pub.get("GlossMapChannelB")
        if b is not None and float(b) > 0.0:
            out["gloss_channel"] = "B"
        chans = []
        for name in ("R", "G", "B"):
            v = pub.get("SpecMapChannel" + name)
            if v is not None and float(v) > 0.0:
                chans.append(name)
        if chans:
            out["spec_mask_channels"] = tuple(chans)
    ga = mat_el.get("GlowAmount")
    if ga is not None:
        try:
            out["glow_amount"] = float(ga)
        except ValueError:
            pass
    em = mat_el.get("Emissive")
    if em:
        out["emissive"] = em
    return out


def _index_bytes_temp(idx, rel, temp_dir):
    """Materialize one index path (pak entry) to a temp file; loose handled
    by the caller via ``rec['real']``. Cached per rel under temp_dir."""
    import hashlib
    os.makedirs(temp_dir, exist_ok=True)
    base = os.path.basename(rel)
    stem, ext = os.path.splitext(base)
    digest = hashlib.md5(rel.encode("utf-8")).hexdigest()[:8]
    out = os.path.join(temp_dir, "%s_%s%s" % (stem, digest, ext))
    if not os.path.isfile(out):
        with open(out, "wb") as f:
            f.write(index_open_bytes(idx, rel))
    return out


def _unsplit_dds_index(idx, stem_rel, temp_dir):
    """Materialize a possibly-split DDS (header + ``.N`` sidecars) from the
    shared VFSIndex into a single combined ``.dds`` temp file. Mirrors
    :func:`_unsplit_dds_vfs` but sources sibling files from the index bytes,
    so .pak textures never need a loose mount."""
    full_rel = stem_rel + ".dds"
    f0_rel = stem_rel + ".dds.0"
    has_dot0 = idx.get(f0_rel) is not None
    has_full = idx.get(full_rel) is not None
    if not has_dot0 and not has_full:
        return None
    if has_dot0:
        header_rel = f0_rel
        header = index_open_bytes(idx, header_rel)
    else:
        header_rel = full_rel
        header = index_open_bytes(idx, full_rel)
    if len(header) < 128:
        return _index_bytes_temp(idx, header_rel, temp_dir)
    mip_count = max(1, struct.unpack_from("<I", header, 28)[0])
    dxt10 = (header[84:88] == b"DX10")
    total_hdr = 128 + (20 if dxt10 else 0)
    if has_full and not has_dot0 and len(header) > total_hdr + 1024:
        return _index_bytes_temp(idx, full_rel, temp_dir)

    highest = 0
    prefix = stem_rel + ".dds."
    for k in idx.keys():
        if k.startswith(prefix):
            rest = k[len(prefix):]
            if rest.isdigit():
                n = int(rest)
                if n > highest:
                    highest = n
    if highest == 0:
        return _index_bytes_temp(idx, header_rel, temp_dir)
    mip0 = index_open_bytes(idx, prefix + str(highest))

    fd, tmp = tempfile.mkstemp(suffix=".dds", prefix="cristical_", dir=_project_temp())
    os.close(fd)
    with open(tmp, "wb") as f:
        f.write(header[:total_hdr])
        f.write(mip0)
    return tmp


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

    # VFS fallback: textures not found loose are resolved through the single
    # shared VFSIndex (loose + every .pak, priority already decided inside the
    # index), instead of per-root mounts. Only the winner is touched.
    if not game_dirs:
        return None, None
    idx = mount_game([str(d) for d in game_dirs])
    rel = mtl_file_ref.replace("\\", "/").lstrip("/")
    if rel.lower().endswith(".dds.0"):
        stem = rel[:-len(".dds.0")]
    else:
        stem = os.path.splitext(rel)[0]
    for candidate in (stem + ".png", stem + ".dds", stem + ".dds.0"):
        rec = idx.get(candidate)
        if rec is None:
            continue
        if candidate.lower().endswith(".png"):
            src = rec["real"] if rec["kind"] == "loose" else _index_bytes_temp(idx, candidate, _TEMP_TEX)
        else:
            src = _unsplit_dds_index(idx, stem, _TEMP_TEX)
        if src is not None:
            return candidate, src

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


def _convert_to_png(src_path, dst_path, is_normal=False, profile=None,
                    slot=None, signals=None):
    """Decode one source texture to PNG + extracted-channel PNGs.

    Output naming: the ORIGINAL asset stem + original slot suffix is
    preserved (``name_suffix.png``); channel extractions append their
    role (``name_suffix_gloss.png`` etc.) so every file traces back to
    its source texture.

    Channel policies (each ENGINE-derived, commented inline):
      - Normal maps shipped as BC5/ATI2 (RG only, Z empty) get Z
        reconstructed. The engine itself does the same in-shader (it
        only stores XY); the reconstruction here is our conversion-side
        equivalent so the PNG is a complete tangent-space normal map.
      - Alphas NEVER survive into the RGB outputs: the engine packs
        non-transparency data into alpha channels (smoothness in _ddna,
        gloss power in _spec under %SPECULARPOW_GLOSSALPHA, glow under
        %ALPHAGLOW), so alpha is extracted to its own grayscale PNG and
        the RGB PNG is written fully opaque. Only real surface
        transparency would be kept in an alpha - the Crysis corpus does
        not use it in these slots.
    """
    try:
        im = Image.open(src_path)
    except Exception as e:
        print("  [tex] WARN: cannot open %s: %s" % (os.path.basename(src_path), e))
        return [dst_path]
    arr = np.array(im)
    result = [dst_path]
    base = dst_path.rsplit(".", 1)[0]

    # -- Normal Z reconstruction (engine behaviour: BC5 stores only XY;
    #    the shader reconstructs Z = sqrt(1 - x^2 - y^2). Our converter
    #    does the identical math so the exported PNG is complete.)
    if (is_normal and profile is not None
            and profile.get("normal_z") == "reconstruct"
            and arr.ndim > 2 and arr.shape[2] >= 2
            and arr[:, :, 2].max() < 5):
        f = arr.astype(np.float32) / 127.5 - 1.0
        z_sq = 1.0 - f[:, :, 0]**2 - f[:, :, 1]**2
        if arr.shape[2] < 3:
            arr = np.dstack([arr, np.zeros_like(arr[:, :, 0])])
        arr[:, :, 2] = ((np.sqrt(np.maximum(z_sq, 0.0)) + 1.0) * 127.5).clip(0, 255).astype(np.uint8)

    # -- Alpha extraction. Which role the alpha plays is decided by the
    #    slot suffix + material signals, mirroring the engine's slot
    #    table (TextureHelpers suffix conventions) and shader-gen bits:
    if arr.ndim > 2 and arr.shape[2] >= 4:
        alpha = arr[:, :, 3].copy()
        if alpha.max() > alpha.min():
            role = _alpha_role(src_path, slot, signals, profile)
            if role:
                # Smoothness/gloss in normal-map alpha (engine:
                # FT_HAS_ATTACHED_ALPHA on _ddna mirrors the SAME file
                # into EFTT_SMOOTHNESS and samples its alpha).
                # Gloss power in specular alpha (engine:
                # %SPECULARPOW_GLOSSALPHA reads spec-map alpha as
                # specular power). Glow in diffuse alpha (engine:
                # %ALPHAGLOW + GlowAmount).
                a_name = "%s_%s.png" % (base, role)
                Image.fromarray(alpha, "L").save(a_name, "PNG")
                result.append(a_name)
        # RGB output is written opaque: the extracted roles above are
        # the ONLY thing Crysis stores in these alphas.
        arr = arr[:, :, :3]

    res = Image.fromarray(arr, "RGB")
    res.save(dst_path, "PNG")
    return result


def _alpha_role(src_path, slot, signals, profile):
    """Decide what a non-constant alpha channel encodes, or None.

    Decision order mirrors the engine's own slot resolution:
      1. _ddna file suffix -> smoothness (TextureHelpers suffix table;
         engine mirrors the file into EFTT_SMOOTHNESS).
      2. specular slot + %SPECULARPOW_GLOSSALPHA -> gloss power.
      3. diffuse slot + %ALPHAGLOW -> glow mask.
      4. diffuse alpha without a signal -> opacity mask (kept as a
         separate grayscale PNG; see note in _convert_to_png).
    """
    if slot is None:
        return None
    base = os.path.basename(src_path).lower()
    if "_ddna" in base:
        return "gloss"
    if slot == "specular":
        if signals is not None and signals.get("gloss_from_spec_alpha"):
            return "gloss"
        # C1-family spec alphas observed constant-white in the corpus
        # (C1 hunter_spec ch3 varies but only where ALPHAGLOW pairs a
        # glow mask); without an engine signal we do not guess.
        return None
    if slot == "diffuse":
        if signals is not None and signals.get("glow"):
            return "glow"
        return "opacity"
    return None


def _shininess_to_roughness(s):
    s = max(1.0, min(256.0, float(s)))
    return (2.0 / (s + 2.0)) ** 0.25


def _spec_blue_gloss_png(file_ref, mat_name, game_dirs, out_dir,
                         tex_index, images, tex_sources):
    """Extract the C3 gloss (specular BLUE channel) to name_spec_gloss.png.

    ENGINE behaviour, not our invention: C3 materials pick the gloss
    channel through PublicParams GlossMapChannelB="1" (mastermind
    upper_metal: SpecMapChannelR/G="1" + GlossMapChannelB="1"); the
    shader samples smoothness from the spec map's B channel while R+G
    remain the specular colour mask. Verified in data: C3 body_spec
    ch2 mean 183 vs ch0/1 ~56 - a visibly separate gloss layer.

    CLEAN-CHANNEL POLICY: the engine's smoothness value is exported
    AS-IS, no inversion. The downstream engine (Unity) composes
    channels in its own material - glTF materials are hints only, so
    any gloss->roughness inversion belongs to the designer's engine
    setup, not to this converter.

    Returns None on failure; the gloss PNG is registered in
    tex_index/images/tex_sources exactly like a slot texture.
    """
    orig_path, src_path = _find_texture(file_ref, game_dirs)
    if not src_path:
        return None
    stem, suffix = _stem_and_suffix(orig_path or src_path)
    png_name = stem + suffix + "_gloss.png"
    dst = os.path.join(out_dir, png_name)
    try:
        im = Image.open(src_path)
        arr = np.array(im)
        if arr.ndim < 3:
            return None
        gloss = arr[:, :, 2].copy()
        Image.fromarray(gloss, "L").save(dst, "PNG")
    except Exception as e:
        print("  %s/gloss: FAILED %s — %s" % (mat_name, os.path.basename(src_path), e))
        return None
    count = images[-1]["index"] + 1 if images else 0
    tex_index[(mat_name, "gloss")] = count
    images.append({"name": png_name, "index": count})
    tex_sources[png_name] = orig_path
    print("  %s/gloss: %s -> %s" % (mat_name, os.path.basename(orig_path), png_name))
    return count + 1


def _default_profile(game_dirs):
    """Texture pipeline profile for the given game dirs, via the CANONICAL
    resolver game_profile.classify_game_dir (never a duplicate heuristic).
    Unknown titles fall back to the conservative default profile."""
    try:
        try:
            from .game_profile import classify_game_dir, GameTitle
        except ImportError:  # running as a script
            from game_profile import classify_game_dir, GameTitle
        for d in game_dirs or []:
            info = classify_game_dir(str(d))
            title = info.get("title")
            if title is not None:
                return title.texture_pipeline
        return GameTitle.CRYSIS_1.texture_pipeline  # conservative default
    except Exception:
        return {"normal_z": "reconstruct", "gloss_source": "shininess",
                "emissive_alpha": "none", "mtl_to_real_ext": None,
                "split_dds": False}


def convert_materials(mtl_path, game_dirs, out_dir, profile=None):
    """Convert one .mtl's textures to PNG + glTF material definitions.

    ``profile`` is a game_profile texture-pipeline dict
    (GameTitle.texture_pipeline); when None it is resolved from the
    game dirs, falling back to the conservative default profile.
    """
    if profile is None:
        profile = _default_profile(game_dirs)
    os.makedirs(out_dir, exist_ok=True)
    root = _load_xml(mtl_path)
    sub_mats = root.find("SubMaterials")
    if sub_mats is None:
        if root.tag == "Material":
            sub_mats = root
        else:
            print("[tex] no SubMaterials in %s" % mtl_path)
            return [], [], [], {}, {}

    materials = []
    mat_info = []
    tex_index = {}     # (mat_name, slot) -> image index
    images = []        # [{name, index}] in creation order
    tex_sources = {}   # png name -> original VFS reference
    tex_count = 0
    xml_to_mat = {}
    # One source asset -> ONE set of PNGs, however many materials/slots
    # reference it (grunt's metal + metal_noshadow share the same .dds).
    # Keyed by the normalized original path; value = {png basename ->
    # image index}. Channel extraction runs only the FIRST time.
    _by_source = {}
    # Output name -> the source path that OWNS it this run. Only used
    # to rename on a genuine cross-source clash (see ensure_texture).
    _name_owner = {}

    def ensure_texture(file_ref, mat_name, map_type, signals=None):
        nonlocal tex_count
        orig_path, src_path = _find_texture(file_ref, game_dirs)
        if orig_path is not None and src_path is None:
            return None
        if not src_path:
            return None
        key = (mat_name, map_type)
        if key not in tex_index:
            # Output naming policy:
            #  - normal slot -> stem + "_normal.png". The engine's _ddn
            #    is a BC5 storage format (XY only; the shader rebuilds
            #    Z in-shader) - unusable outside Crysis until Z is
            #    reconstructed. The _normal name GUARANTEES the file is
            #    a finished tangent-space normal map, whatever the
            #    source suffix was.
            #  - every other slot -> ORIGINAL asset stem + slot suffix
            #    (grunt_metal_dif.png), traceable to the source file.
            #  - CROSS-SLOT NORMAL CAVEAT: a _ddn may sit in a NON-normal
            #    slot (engine allows e.g. SubSurface sampling a normal
            #    map for jelly shading; mastermind jelly does exactly
            #    that via stalker/textures/jelly_ddn.tif). Such a file
            #    is still a BC5 engine normal map, so it is ALSO
            #    converted (Z rebuilt) and tagged with the slot so the
            #    designer sees where the twin comes from:
            #    jelly_sss_normal.png.
            stem, suffix = _stem_and_suffix(orig_path or src_path)
            if map_type == "normal":
                png_name = stem + "_normal.png"
            elif suffix == "_ddn" or suffix == "_ddna":
                tag = _SLOT_DISPLAY.get(map_type, map_type)
                png_name = "%s_%s_normal.png" % (stem, tag)
            else:
                png_name = (stem if not suffix else stem + suffix) + ".png"
            norm_src = (orig_path or src_path).replace("\\", "/").lower()
            cache = _by_source.get(norm_src)
            if cache is not None and png_name in cache:
                # Same source asset already converted (another material
                # references it): reuse the existing image, no rewrite.
                tex_index[key] = cache[png_name]
                return {"index": cache[png_name]}
            # NAME COLLISION GUARD: different source folders can hold
            # files with the same base name (mastermind/textures/
            # jelly_ddn.tif vs stalker/textures/jelly_ddn.tif). The
            # dedup cache is keyed by the full source path, so a REAL
            # clash (two different sources, one output name) is
            # disambiguated with a short path hash. The same source
            # referenced twice (main material + attachment material)
            # rewrites identical bytes - allowed, no rename.
            _written = _by_source.get(norm_src)
            if _written is None and os.path.isfile(os.path.join(out_dir, png_name)):
                _other = _name_owner.get(png_name)
                if _other is not None and _other != norm_src:
                    digest = hashlib.md5(norm_src.encode("utf-8")).hexdigest()[:6]
                    root, ext = os.path.splitext(png_name)
                    png_name = "%s_%s%s" % (root, digest, ext)
            _name_owner[png_name] = norm_src
            dst = os.path.join(out_dir, png_name)
            # A cross-slot _ddn is still a normal map: rebuild Z for it
            # too (the engine samples it as one in that slot).
            is_norm = (map_type == "normal" or suffix in ("_ddn", "_ddna"))
            try:
                generated = _convert_to_png(src_path, dst, is_normal=is_norm,
                                             profile=profile, slot=map_type,
                                             signals=signals)
            except Exception as e:
                print("  %s/%s: FAILED %s — %s" % (mat_name, map_type, os.path.basename(src_path), e))
                return None
            if generated is None:
                return None
            tex_index[key] = tex_count
            images.append({"name": generated[0], "index": tex_count})
            tex_sources[generated[0]] = orig_path
            _by_source.setdefault(norm_src, {})[png_name] = tex_count
            tex_count += 1
            print("  %s/%s: %s -> %s" % (mat_name, map_type, os.path.basename(orig_path), os.path.basename(generated[0])))
            for extra in generated[1:]:
                # Extracted alpha roles (_gloss/_glow/_opacity). Role keys
                # carry a "role_" prefix so they NEVER collide with the
                # EFTT slot keys: the Opacity SLOT (a texture of its own,
                # e.g. mastermind jelly_inner_dif in the SSS mask slot)
                # previously got shadowed by a diffuse-alpha "opacity"
                # entry and was silently dropped.
                extra_type = ("gloss" if extra.endswith("_gloss.png")
                              else "glow" if extra.endswith("_glow.png")
                              else "opacity" if extra.endswith("_opacity.png")
                              else "extra")
                extra_key = (mat_name, "role_" + extra_type)
                tex_index[extra_key] = tex_count
                images.append({"name": os.path.basename(extra), "index": tex_count})
                tex_sources[os.path.basename(extra)] = orig_path
                _by_source[norm_src][os.path.basename(extra)] = tex_count
                tex_count += 1
                print("  %s/%s: %s -> %s" % (mat_name, extra_type, os.path.basename(orig_path), os.path.basename(extra)))
        return {"index": tex_index[key]}

    for xml_idx, mat_el in enumerate(sub_mats.findall("Material")):
        name = mat_el.get("Name", "material")
        shader = mat_el.get("Shader", "")
        if shader == "Nodraw":
            xml_to_mat[xml_idx] = None
            continue

        # Engine channel signals for THIS sub-material (shader-gen
        # bits + PublicParams channel selectors, as parsed by
        # MakeMaterialFromXml in the engine).
        signals = _material_signals(mat_el)

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
                slot = _SLOT_ALIASES.get(mtype, "misc_" + mtype.lower())
                # EVERY referenced texture is exported, whatever the
                # slot - SSS masks, custom jelly maps, decals, cubemaps,
                # height/detail layers. The slot tag only decides the
                # channel-extraction policy, never whether the file is
                # written. Missing Map names land in misc_<name> so a
                # designer still receives every asset bound to the model.
                if not file_ref:
                    continue
                t = ensure_texture(file_ref, name, slot, signals)
                if t is None:
                    continue
                # glTF wiring is a HINT only (the target engine builds
                # its own material); wire the core PBR hints, leave the
                # rest as exported channel files.
                if slot == "diffuse":
                    gltf_mat["pbrMetallicRoughness"]["baseColorTexture"] = t
                elif slot == "normal":
                    gltf_mat["normalTexture"] = t
                elif slot == "emissive":
                    gltf_mat["emissiveTexture"] = t
                    gltf_mat["emissiveFactor"] = [1.0, 1.0, 1.0]
                elif slot == "specular":
                    # C3: gloss lives in the specular map's BLUE channel
                    # (engine: PublicParams GlossMapChannelB selects it;
                    # C3 body_spec ch2 mean 183 vs ch0/1 ~56 - visibly a
                    # distinct gloss layer while R+G stay the spec mask).
                    # Extracted as name_spec_gloss.png AS-IS (engine's
                    # smoothness value, no inversion: the downstream
                    # engine composes channels itself).
                    if (profile.get("gloss_source") == "spec_blue"
                            and signals.get("gloss_channel") == "B"
                            and (name, "gloss") not in tex_index):
                        _spec_blue_gloss_png(file_ref, name, game_dirs,
                                             out_dir, tex_index,
                                             images, tex_sources)

        e = tex_index.get((name, "role_glow"))
        if e is not None:
            gltf_mat["emissiveTexture"] = {"index": e}
            gltf_mat["emissiveFactor"] = [1.0, 1.0, 1.0]
        elif signals.get("glow_amount"):
            # Constant glow (engine: GlowAmount + Emissive colour on
            # %ALPHAGLOW materials, e.g. C2 grunt eyes) - carried as a
            # plain emissiveFactor, no texture.
            try:
                er, eg, eb = [float(x) for x in signals["emissive"].split(",")]
            except ValueError:
                er, eg, eb = 1.0, 1.0, 1.0
            if (er, eg, eb) != (0.0, 0.0, 0.0):
                gltf_mat["emissiveFactor"] = [er, eg, eb]

        # Extracted channel maps (gloss/glow/opacity/SSS/custom/...)
        # live as their own PNGs in the output set - the exported
        # package is channel-complete BY DESIGN (the target engine
        # composes channels in its own material; glTF is a hint). They
        # are deliberately NOT wired into the glTF material as
        # non-standard extensions.

        materials.append(gltf_mat)
        xml_to_mat[xml_idx] = len(materials) - 1
        mat_info.append({
            "name": name, "shininess": shininess, "diffuse": diff_col,
            "specular": spec_col, "opacity": opacity, "shader": shader,
        })

    return materials, [img["name"] for img in sorted(images, key=lambda x: x["index"])], mat_info, tex_sources, xml_to_mat
