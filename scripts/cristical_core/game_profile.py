#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
game_profile.py — CrisTical: game-title / data-root detection
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Crysis shipped in several editions that store assets differently:
- Crysis 1, Warhead, Remastered, Wars: ZIP-format ``.pak`` plus optional
  loose unpacked tree under a data root usually named ``Game``;
- Crysis 2: XXTEA-encrypted ``.pak`` archives under ``GameSDK``;
- Crysis 3: Twofish-CTR (RSA-OAEP) encrypted ``.pak`` under ``GameCrysis3``.

The low-level readers (``cryvfs``/``crypak``) already switch on the real
archive encryption when reading. This module adds the title/edition concept
on top: which game a directory belongs to, where its canonical data root
is, and which pak family to expect. It is used by the GUI ("Game" section)
and to normalise user-supplied ``--gamedir`` values to a real data root.
"""

from __future__ import annotations

import os
from enum import Enum


class GameTitle(Enum):
    """Supported Crysis editions exposed to users/CLI/GUI."""

    CRYSIS_1 = "Crysis 1"
    CRYSIS_WARHEAD = "Crysis Warhead"
    CRYSIS_2 = "Crysis 2"
    CRYSIS_3 = "Crysis 3"
    CRYSIS_REMASTERED = "Crysis Remastered"
    CRYSIS_WARS = "Crysis Wars"

    @property
    def root_names(self) -> list[str]:
        """Candidate data-root subfolder names under an install directory."""
        roots = {
            GameTitle.CRYSIS_1: ["Game"],
            GameTitle.CRYSIS_WARHEAD: ["Game"],
            GameTitle.CRYSIS_2: ["gamecrysis2", "GameSDK"],
            GameTitle.CRYSIS_3: ["C3", "GameCrysis3"],
            GameTitle.CRYSIS_REMASTERED: ["Game"],
            GameTitle.CRYSIS_WARS: ["Game"],
        }
        return roots[self]

    @property
    def pak_family(self) -> str:
        """Expected main-geometry pak family: zip | xxtea | twofish."""
        fam = {
            GameTitle.CRYSIS_1: "zip",
            GameTitle.CRYSIS_WARHEAD: "zip",
            GameTitle.CRYSIS_2: "xxtea",
            GameTitle.CRYSIS_3: "twofish",
            GameTitle.CRYSIS_REMASTERED: "zip",
            GameTitle.CRYSIS_WARS: "zip",
        }
        return fam[self]

    @property
    def texture_pipeline(self) -> dict:
        """Per-title texture reading/writing policy (see _TEXTURE_PIPELINE).

        Returns a dict with keys: normal_z, gloss_source, emissive_alpha,
        mtl_to_real_ext, split_dds. Unknown titles get the safe default
        (Z reconstruction, scalar shininess, no emissive alpha, split
        DDS tolerated)."""
        return dict(_TEXTURE_PIPELINE.get(self, _TEXTURE_PIPELINE_DEFAULT))


# ----------------------------------------------------------------------
# Texture pipeline profile per title (module-level so the table can
# reference GameTitle members; exposed via GameTitle.texture_pipeline).
# Every field reflects an ENGINE-observed behaviour (verified against
# game data / engine sources), not a converter invention:
#
# normal_z
#   "reconstruct" - shipped _ddn textures are BC5/ATI2 (two-channel
#      RG; the Blue channel is empty, e.g. C2 grunt_metal_ddn.dds and
#      C3 body_ddn.dds probe as mode=RGB with ch2 max==0). The engine
#      samples only XY and reconstructs Z in-shader, so the converter
#      rebuilds Z = sqrt(1-x^2-y^2) when writing a PNG.
#   "present" - the texture keeps a real Blue channel (Crysis 1
#      Remastered hunter_ddn.dds.0 decodes RGBA with ch2 mean 163 -
#      Z is already authored; nothing to reconstruct).
#
# gloss_source
#   Where the smoothness/gloss signal lives for this title:
#   "spec_alpha"  - specular map alpha (engine slot EFTT_SPECULAR,
#      GenMask %SPECULARPOW_GLOSSALPHA; C2 grunt plate_up etc.).
#   "spec_blue"   - specular map Blue channel, selected by the
#      material's PublicParams GlossMapChannelB="1" (C3 body_spec
#      ch2 mean 183 vs ch0/1 ~56 - visibly a distinct gloss layer).
#   "ddna_alpha"  - normal map alpha on _ddna textures (engine
#      FT_HAS_ATTACHED_ALPHA mirroring the file into EFTT_SMOOTHNESS;
#      C1 Remastered terrain detail set: beach_white_sand_ddna etc.).
#   "shininess"   - material scalar only (no gloss texture at all);
#      fallback for titles/slots without a map.
#
# emissive_alpha
#   "glow"   - diffuse alpha feeds glow/emittance when the material
#      has %ALPHAGLOW + GlowAmount (C1 family, C2 grunt eyes).
#   "none"   - alpha never carries emissive data (C3 uses explicit
#      _em / Emittance slot textures instead).
#
# mtl_to_real_ext
#   Extension rewriting between the .mtl <Texture File=...> reference
#   and the shipped asset: C1/C1R reference .dds/.tif matching the
#   real file; C2/C3 materials reference .tif source art while the
#   shipped data root only contains compiled .dds (engine resource
#   compiler replaces extensions when resolving, per
#   EF_LoadTexture/CTexture::ForName).
#
# split_dds
#   True - textures ship as split DDS: a .dds.0 header stub plus
#      .dds.N sidecars holding the mip payloads (C3, both
#      Remastereds). The converter concatenates header + highest .N.
#   False - one monolithic .dds file (C1, C2).
# ----------------------------------------------------------------------

_TEXTURE_PIPELINE = {
        GameTitle.CRYSIS_1: {
            "normal_z": "reconstruct", "gloss_source": "spec_alpha",
            "emissive_alpha": "glow", "mtl_to_real_ext": None,
            "split_dds": False,
        },
        GameTitle.CRYSIS_WARHEAD: {
            "normal_z": "reconstruct", "gloss_source": "spec_alpha",
            "emissive_alpha": "glow", "mtl_to_real_ext": None,
            "split_dds": False,
        },
        GameTitle.CRYSIS_WARS: {
            "normal_z": "reconstruct", "gloss_source": "spec_alpha",
            "emissive_alpha": "glow", "mtl_to_real_ext": None,
            "split_dds": False,
        },
        GameTitle.CRYSIS_REMASTERED: {
            "normal_z": "present", "gloss_source": "ddna_alpha",
            "emissive_alpha": "glow", "mtl_to_real_ext": None,
            "split_dds": True,
        },
        GameTitle.CRYSIS_2: {
            "normal_z": "reconstruct", "gloss_source": "spec_alpha",
            "emissive_alpha": "glow", "mtl_to_real_ext": (".tif", ".dds"),
            "split_dds": False,
        },
        GameTitle.CRYSIS_3: {
            "normal_z": "reconstruct", "gloss_source": "spec_blue",
            "emissive_alpha": "none", "mtl_to_real_ext": (".tif", ".dds"),
            "split_dds": True,
        },
    }

_TEXTURE_PIPELINE_DEFAULT = {
    "normal_z": "reconstruct", "gloss_source": "shininess",
    "emissive_alpha": "none", "mtl_to_real_ext": None,
    "split_dds": True,
}


# Small markers used to disambiguate the plain-ZIP titles (C1/Warhead/Remastered/Wars).
_ZIP_TITLE_MARKERS = {
    GameTitle.CRYSIS_REMASTERED: ("remastered",),
    GameTitle.CRYSIS_WARHEAD: ("warhead",),
    GameTitle.CRYSIS_WARS: ("wars",),
}

_DATA_MARKER_NAMES = ("objects.pak", "animations.pak", "script.pak", "scripts.pak")
_GEOM_ENTRY_HINT = ("objects.pak", "objects")

# Crysis 2 Remastered paks are plain ZIPs whose payloads use the custom
# LZ4 compression method (zip method 12) and carry a ``.lz4compression``
# marker entry. No other edition does - that makes the marker/method a
# reliable C2R discriminator inside the plain-ZIP family.
_LZ4_CACHE = {}


def _has_lz4_paks(root: str) -> bool:
    """True when a data root's paks use the custom LZ4 zip method.

    Opens one marker pak's central directory and looks for a method-12
    entry (or the ``.lz4compression`` marker). Cheap: metadata only,
    no payload decompression. Cached per root.
    """
    cached = _LZ4_CACHE.get(root)
    if cached is not None:
        return cached
    found = False
    try:
        import zipfile
        for n in sorted(os.listdir(root)):
            if not n.lower().endswith(".pak"):
                continue
            p = os.path.join(root, n)
            if not os.path.isfile(p):
                continue
            try:
                with zipfile.ZipFile(p, "r") as z:
                    if ".lz4compression" in z.namelist():
                        found = True
                        break
                    for i in z.infolist():
                        if i.compress_type == 12:
                            found = True
                            break
            except Exception:
                continue
            if found:
                break
    except OSError:
        pass
    _LZ4_CACHE[root] = found
    return found

# Remastered ships ``_ddna`` normal-map textures (often split ``.dds.N``
# sidecars); the original Crysis 1 / Warhead / Wars tree has only ``_ddn``.
# Verified on real installs: original -> 0 ``_ddna``, Remastered -> many.
_DDNA_LOOSE_CACHE = {}
_CLASSIFY_CACHE = {}


def _has_ddna_loose(root: str, cap: int = 250000):
    """True/False/None: does the loose tree contain a ``_ddna`` texture?

    Scans recursively and stops at the first hit. Returns None (unknown)
    if more than ``cap`` entries are inspected without a hit, so an
    unusually large tree does not freeze callers. Cached per root.
    """
    cached = _DDNA_LOOSE_CACHE.get(root)
    if cached is not None:
        return cached
    found = False
    count = 0
    try:
        for _dirpath, _dirnames, filenames in os.walk(root):
            for n in filenames:
                count += 1
                if count > cap:
                    return _DDNA_LOOSE_CACHE.setdefault(root, None)
                if "_ddna" in n.lower():
                    found = True
                    break
            if found:
                break
    except OSError:
        pass
    _DDNA_LOOSE_CACHE[root] = found
    return found


def _main_pak(root: str) -> str | None:
    """Path to the main ``GameData.pak`` (any case) in a data root, if any.

    Every supported title ships one primary ``GameData.pak`` at its data
    root — it is the canonical root marker and is small enough to sniff the
    archive family from quickly (vs. multi-GB Objects/Music paks).
    """
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for n in names:
        if n.lower() == "gamedata.pak" and os.path.isfile(os.path.join(root, n)):
            return os.path.join(root, n)
    return None


def _is_data_root(path: str) -> bool:
    """Heuristic: a data root has loose Objects/, a ``GameData.pak`` main
    pak, or a geometry ``objects`` pak."""
    if os.path.isdir(os.path.join(path, "Objects")):
        return True
    try:
        names = os.listdir(path)
    except OSError:
        return False
    if _main_pak(path) is not None:
        return True
    return any(n.lower().startswith(_GEOM_ENTRY_HINT) for n in names)


def find_data_root(path: str) -> str:
    """Return the canonical data-root directory for ``path``.

    If ``path`` already looks like a data root it is returned unchanged.
    Otherwise the first existing child among the known title root names that
    itself looks like a data root is returned. Falls back to ``path``.
    """
    if not os.path.isdir(path):
        return path
    if _is_data_root(path):
        return path
    for name in ("Game", "gamecrysis2", "GameSDK", "C3", "GameCrysis3",
                 "GameData", "Data"):
        child = os.path.join(path, name)
        if os.path.isdir(child) and _is_data_root(child):
            return child
    return path


def _sniff_pak_family(pak_path: str) -> str | None:
    """zip | xxtea | twofish | None, read from the actual pak header.

    ZIP-format archives (Crysis 1/Warhead/Remastered/Wars) start with
    ``PK\\x03\\x04``. Encrypted archives fall through to CryPak, whose real
    ``_encmode`` (set after decrypting the index) distinguishes Crysis 2
    (2) from Crysis 3 (3).
    """
    try:
        with open(pak_path, "rb") as f:
            if f.read(4) == b"PK\x03\x04":
                return "zip"
    except OSError:
        return None
    try:
        from .crypak import CryPakFileSystem  # type: ignore
    except ImportError:
        from crypak import CryPakFileSystem  # type: ignore
    try:
        fs = CryPakFileSystem(pak_path)
        mode = getattr(fs, "_encmode", None)
        try:
            fs.close()
        except Exception:
            pass
        return {2: "xxtea", 3: "twofish"}.get(mode)
    except Exception:
        return None


def _pick_geometry_pak(root: str) -> str | None:
    try:
        names = os.listdir(root)
    except OSError:
        return None
    for n in sorted(names):
        low = n.lower()
        if low.endswith(".pak") and (low.startswith("objects")
                                     or low.startswith("animations")):
            return os.path.join(root, n)
    return None


def _pick_marker_pak(root: str) -> str | None:
    """The pak to sniff the family from: prefer the main ``GameData.pak``
    (canonical root marker, small), fall back to a geometry pak."""
    main = _main_pak(root)
    if main is not None:
        return main
    return _pick_geometry_pak(root)


def classify_game_dir(path: str) -> dict:
    """Best-effort title/root/family detection for a user-supplied folder.

    Results are cached per absolute path so repeated GUI/status refreshes do
    not re-read or re-decrypt the pak.

    Returns a dict: {title, root, family, confidence, notes}.
    ``title`` is a GameTitle or None; ``confidence`` in 0..1; ``notes``
    explains the decision (used by the GUI tooltip/log).
    """
    key = os.path.normcase(os.path.abspath(path))
    hit = _CLASSIFY_CACHE.get(key)
    if hit is not None:
        return hit
    result = _classify_detected(path)
    _CLASSIFY_CACHE[key] = result
    return result


def _classify_detected(path: str) -> dict:
    root = find_data_root(path)
    notes = []
    if not os.path.isdir(root):
        return {"title": None, "root": root, "family": None,
                "confidence": 0.0, "notes": ["directory not found"]}

    # Fall back to the exact title markers embedded in file/dir names.
    try:
        all_lower = " ".join(n.lower() for n in os.listdir(root))
    except OSError:
        all_lower = ""
    for title, sigs in _ZIP_TITLE_MARKERS.items():
        if any(s in all_lower for s in sigs):
            return {"title": title, "root": root,
                    "family": title.pak_family, "confidence": 0.9,
                    "notes": ["matched title marker in data-root names"]}

    pak = _pick_marker_pak(root)
    family = _sniff_pak_family(pak) if pak else None
    if family == "xxtea":
        return {"title": GameTitle.CRYSIS_2, "root": root, "family": family,
                "confidence": 0.95, "notes": ["XXTEA-encrypted pak -> Crysis 2"]}
    if family == "twofish":
        return {"title": GameTitle.CRYSIS_3, "root": root, "family": family,
                "confidence": 0.95, "notes": ["Twofish-encrypted pak -> Crysis 3"]}
    if family == "zip":
        # Crysis 2 Remastered paks are plain ZIPs using the custom LZ4
        # zip method (12) with a ``.lz4compression`` marker entry - no
        # other edition ships that, so it identifies C2R by content, not
        # by install folder naming.
        if _has_lz4_paks(root):
            return {"title": GameTitle.CRYSIS_2, "root": root,
                    "family": family, "confidence": 0.95,
                    "notes": ["LZ4-compressed paks -> Crysis 2 Remastered"]}
        # Crysis 3 Remastered also ships plain-ZIP paks (deflate only)
        # under its base game's data-root name. A zip sniff already
        # excludes encrypted C2/C3, so a plain zip in a C3 root is C3R.
        base = os.path.basename(os.path.normpath(root)).lower()
        if base in ("c3", "gamecrysis3"):
            return {"title": GameTitle.CRYSIS_3, "root": root,
                    "family": family, "confidence": 0.85,
                    "notes": ["plain-ZIP paks in C3 root -> "
                              "Crysis 3 Remastered"]}
        # Within the plain-ZIP family, Remastered is the only edition that
        # ships ``_ddna`` normal-map textures — use that to disambiguate.
        rem = _has_ddna_loose(root)
        if rem:
            return {"title": GameTitle.CRYSIS_REMASTERED, "root": root,
                    "family": family, "confidence": 0.85,
                    "notes": ["_ddna textures present -> Crysis Remastered"]}
        if rem is None:
            note = ("plain-ZIP paks; loose scan too large to confirm _ddna - "
                    "select the exact title in the GUI")
            conf = 0.4
        else:
            note = ("plain-ZIP paks, no _ddna -> Crysis 1 / Warhead / Wars "
                    "(not Remastered); select the exact title in the GUI")
            conf = 0.6
        return {"title": None, "root": root, "family": family,
                "confidence": conf, "notes": [note]}
    return {"title": None, "root": root, "family": None,
            "confidence": 0.2,
            "notes": ["could not identify pak family / edition"]}
