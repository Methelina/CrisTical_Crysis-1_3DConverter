#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crychrparams.py — CrisTical: .chrparams animation-list loader
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

A `.chrparams` XML lives next to a `.chr` and lists named animations:
direct `path="*.caf"` wildcards, `$Include` sub-lists, `$TracksDatabase`
(.dba), `$AnimEventDatabase`. This module turns the AnimationList into a
flat list of resolved clip references (path inside the VFS + display
name), keeping diagnostics for missing includes and empty wildcards.

The path/classification rules follow the engine's own animation-list
semantics as observed while studying how characters load their clips
(the per-object animation directories, the #filepath animation root,
and the include-resolution order).
"""

import os
import re
import types
from pathlib import PurePosixPath

from .cryvfs import IPackFileSystem, mount_gamedirs

ANIMATION_CLIP_EXTENSIONS = frozenset({".caf", ".anim", ".dba"})

_NON_PLAYABLE_NAMES = {"$tracksdatabase", "$animeventdatabase", "#filepath",
                       "$include"}


def _normalize(path):
    return path.replace("\\", "/").lower()


def _clean_path(path):
    if path is None:
        return None
    cleaned = path.replace("\\", "/").strip().strip("/")
    return cleaned or None


def _join(*parts):
    return "/".join(p.replace("\\", "/").strip("/") for p in parts if p)


def parse_chrparams(root, source_file_name=None):
    """Parse a `<Params>` root element (from cryxmlb.load_*) into a
    SimpleNamespace: includes, animation_base_path, animations
    (name/path/base_path/source_file_name), missing_includes."""
    out = types.SimpleNamespace(
        source_file_name=source_file_name,
        includes=[],
        animation_base_path=None,
        animations=[],
        missing_includes=[])
    anim_list = root.find("AnimationList")
    if anim_list is None:
        return out
    for el in anim_list.findall("Animation"):
        name = el.get("name")
        path = el.get("path")
        lowered = (name or "").lower()
        if lowered == "$include":
            if path:
                out.includes.append(path)
            continue
        if lowered == "#filepath":
            out.animation_base_path = _clean_path(path)
            continue
        out.animations.append(types.SimpleNamespace(
            name=name, path=path, base_path=out.animation_base_path,
            source_file_name=source_file_name))
    return out


def _load_xml_root(fs, path, log):
    from .cryxmlb import load_stream
    with fs.open(path) as stream:
        return load_stream(stream)


def load_chrparams_with_includes(path, fs, log=None, _seen=None):
    """Load a .chrparams file and recursively merge `$Include` lists.

    Returns None when the file is absent; missing includes are collected
    into `missing_includes` (each entry keeps failing include path).
    """
    if log is None:
        log = lambda msg: None  # noqa: E731
    if not fs.exists(path):
        return None
    if _seen is None:
        _seen = set()
    norm = _normalize(path)
    if norm in _seen:
        return types.SimpleNamespace(
            source_file_name=path, includes=[], animation_base_path=None,
            animations=[], missing_includes=[])
    _seen.add(norm)

    try:
        root = _load_xml_root(fs, path, log)
    except Exception as e:
        log("[chrparams] failed to parse %s: %s" % (path, e))
        return None
    main = parse_chrparams(root, source_file_name=path)

    base_dir = str(PurePosixPath(path.replace("\\", "/")).parent)
    if base_dir == ".":
        base_dir = ""

    known = set()
    for anim in main.animations:
        known.add(((anim.name or "").lower(),
                    (anim.path or "").replace("\\", "/").lower()))
    for include in main.includes:
        sub = _try_load_include(include, base_dir, fs, _seen, log)
        if sub is None:
            main.missing_includes.append(include)
            continue
        main.missing_includes.extend(sub.missing_includes)
        for anim in sub.animations:
            key = ((anim.name or "").lower(),
                   (anim.path or "").replace("\\", "/").lower())
            if key in known:
                continue
            known.add(key)
            main.animations.append(anim)
    return main


def _try_load_include(include, base_dir, fs, seen, log):
    candidates = [include, _join("game", include)]
    if base_dir:
        candidates.append(_join(base_dir, include))
    # VFS keys are stored lowercase while chrparams often mix case
    # (e.g. "Animations\Alien\grunt\..."). Add a fully-lowercased try so the
    # include resolves regardless of casing.
    candidates.append(_normalize(include).lstrip("/"))
    for path in candidates:
        if fs.exists(path):
            return load_chrparams_with_includes(path, fs, log, _seen=seen)
    return None


# --- path classification helpers ----------------------------------------

def _normalize_animation_reference(path):
    """Strip trailing extension-like junk CryEngine leaves in refs."""
    ref = path.replace("\\", "/").strip()
    lowered = ref.lower()
    endings = []
    for ext in sorted(ANIMATION_CLIP_EXTENSIONS, key=len, reverse=True):
        idx = lowered.rfind(ext)
        if idx < 0:
            continue
        end = idx + len(ext)
        if end == len(lowered) or lowered[end].isspace() or lowered[end] == "(":
            endings.append(end)
    if endings:
        ref = ref[:min(endings)]
    return ref


def is_supported_animation_clip_path(path):
    suffix = PurePosixPath(_normalize_animation_reference(path)).suffix.lower()
    return suffix in ANIMATION_CLIP_EXTENSIONS


def is_non_playable_animation_reference(name, path):
    lowered_name = (name or "").lower()
    if lowered_name in _NON_PLAYABLE_NAMES - {"$include"}:
        return True
    return False


def animation_clip_name(name, path):
    if name and name != "*":
        return name
    return PurePosixPath(path.replace("\\", "/")).stem or "anim"


def has_glob(path):
    return any(ch in path for ch in "*?[")


def is_relative_animation_reference(path):
    cleaned = path.replace("\\", "/").lstrip("/")
    if not cleaned:
        return False
    return cleaned.split("/", 1)[0].lower() not in {"animations", "game"}


def object_animation_directories(input_path):
    """Per-object animation search dirs under animations/ for a given
    object path (engine convention: characters load clips from folders
    matching their own directory tree)."""
    parts = PurePosixPath(input_path.replace("\\", "/")).parts
    if not parts:
        return ()
    parent_parts = parts[:-1]
    candidates = []
    if parent_parts and parent_parts[0].lower() == "objects":
        collapsed = parent_parts
        if len(parent_parts) >= 2 and parent_parts[1].lower() == "objects":
            collapsed = (parent_parts[0], *parent_parts[2:])
        candidates.append(PurePosixPath("animations", *collapsed))
    candidates.append(PurePosixPath("animations", *parent_parts))
    out = []
    seen = set()
    for c in candidates:
        key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(str(c))
    return tuple(out)


def _without_animations_prefix(path):
    lowered = path.lower()
    if lowered.startswith("animations/"):
        return path[len("animations/"):]
    return path


def _with_nested_animations_prefix(path):
    """C2/C3 unpackers sometimes nest animations/ twice."""
    lowered = path.lower()
    if lowered.startswith("animations/"):
        return path
    return "animations/" + path.lstrip("/")


def resolve_anim_paths(path, base_dir, fs, object_dir=None):
    """Resolve one chrparams animation reference against the VFS.

    Expands globs (sorted), honours #filepath base dirs, tries the
    animations/ prefix variants and per-object animation dirs. Returns a
    de-duplicated list of VFS paths (first candidate group that matches
    wins, as the engine's loader does).
    """
    ref_path = _normalize_animation_reference(path)
    candidates = []
    relative_to_base = is_relative_animation_reference(ref_path)
    if base_dir and relative_to_base and not ref_path.startswith(base_dir):
        candidates.append(str(PurePosixPath(base_dir) / ref_path))
    if not (has_glob(ref_path) and base_dir and relative_to_base):
        candidates.append(ref_path)
    for candidate in list(candidates):
        stripped = _without_animations_prefix(candidate)
        if stripped != candidate:
            candidates.append(stripped)
        nested = _with_nested_animations_prefix(candidate)
        if nested != candidate:
            candidates.append(nested)
    if object_dir and not has_glob(ref_path):
        candidates.append(str(PurePosixPath(object_dir) / ref_path))

    matches = []
    seen = set()
    for cand in candidates:
        if has_glob(cand):
            found = sorted(
                p for p in fs.glob(cand.replace("\\", "/"))
                if is_supported_animation_clip_path(p))
        elif fs.exists(cand):
            found = [cand]
        else:
            found = []
        for item in found:
            norm = item.replace("\\", "/").lower()
            if norm in seen:
                continue
            seen.add(norm)
            matches.append(item)
        if matches:
            return matches
    return matches


def collect_clip_refs(chr_path, game_dirs, anim_dir=None, log=None):
    """Full stage-4 pipeline for one character.

    Args:
        chr_path: resolved path to the .chr (its sibling .chrparams is
            used; also the per-object animation dirs are derived from it).
        game_dirs: --gamedir roots (VFS is mounted over them).
        anim_dir: optional explicit animations root, overriding the
            #filepath base discovered in the .chrparams.
        log: callable(str) for diagnostics.

    Returns:
        SimpleNamespace with:
          chrparams_source: basename of the .chrparams or None
          clips: list of (clip_name, vfs_path, extension)
          missing_includes: list of failed $Include paths
          empty_wildcards: list of wildcard entries matching nothing
        or None when no .chrparams exists.
    """
    if log is None:
        log = lambda msg: None  # noqa: E731
    fs = mount_gamedirs(list(game_dirs))

    chr_posix = str(chr_path).replace("\\", "/")
    chrparams_path = os.path.splitext(chr_posix)[0] + ".chrparams"
    if not fs.exists(chrparams_path):
        return None

    params = load_chrparams_with_includes(chrparams_path, fs, log=log)
    if params is None:
        return None

    obj_dirs = object_animation_directories(chr_posix)

    result = types.SimpleNamespace(
        chrparams_source=os.path.basename(chrparams_path),
        clips=[], missing_includes=list(params.missing_includes),
        empty_wildcards=[], animation_base_path=params.animation_base_path)

    seen = set()
    for entry in params.animations:
        if not entry.path:
            continue
        if is_non_playable_animation_reference(entry.name, entry.path):
            continue
        if not is_supported_animation_clip_path(entry.path):
            continue

        entry_base_dir = anim_dir or ""
        if not entry_base_dir:
            if entry.base_path:
                entry_base_dir = entry.base_path
            elif entry.source_file_name:
                parent = str(PurePosixPath(
                    entry.source_file_name.replace("\\", "/")).parent)
                entry_base_dir = parent if parent != "." else ""

        resolved = resolve_anim_paths(
            entry.path, entry_base_dir, fs,
            object_dir=(obj_dirs[0] if obj_dirs else None))
        # Additional object-dir fallback for exact refs the base dirs miss:
        if not resolved and obj_dirs and not has_glob(entry.path):
            for od in obj_dirs:
                resolved = resolve_anim_paths(
                    entry.path, od, fs, object_dir=None)
                if resolved:
                    break

        if not resolved:
            if has_glob(entry.path):
                result.empty_wildcards.append(entry.path)
            else:
                log("[chrparams] unresolved animation %s=%s" % (
                    entry.name, entry.path))
            continue

        for path in resolved:
            norm = path.replace("\\", "/").lower()
            if norm in seen:
                continue
            seen.add(norm)
            ext = PurePosixPath(path).suffix.lower()
            result.clips.append((
                animation_clip_name(entry.name, path), path, ext))

    return result


def clips_for_fs(chr_path, fs, object_dir=None, log=None):
    """Resolve clip refs when a VFS instance is already mounted
    (used by cdf2gltf, which owns its mount)."""
    if log is None:
        log = lambda msg: None  # noqa: E731
    chr_posix = str(chr_path).replace("\\", "/")
    chrparams_path = os.path.splitext(chr_posix)[0] + ".chrparams"
    if not fs.exists(chrparams_path):
        return None
    params = load_chrparams_with_includes(chrparams_path, fs, log=log)
    if params is None:
        return None

    result = types.SimpleNamespace(
        chrparams_source=os.path.basename(chrparams_path),
        clips=[], missing_includes=list(params.missing_includes),
        empty_wildcards=[], animation_base_path=params.animation_base_path)

    seen = set()
    for entry in params.animations:
        if not entry.path:
            continue
        if is_non_playable_animation_reference(entry.name, entry.path):
            continue
        if not is_supported_animation_clip_path(entry.path):
            continue
        entry_base_dir = entry.base_path or ""
        if not entry_base_dir and entry.source_file_name:
            parent = str(PurePosixPath(
                entry.source_file_name.replace("\\", "/")).parent)
            entry_base_dir = parent if parent != "." else ""
        resolved = resolve_anim_paths(
            entry.path, entry_base_dir, fs, object_dir=object_dir)
        if not resolved:
            if has_glob(entry.path):
                result.empty_wildcards.append(entry.path)
            continue
        for path in resolved:
            norm = path.replace("\\", "/").lower()
            if norm in seen:
                continue
            seen.add(norm)
            ext = PurePosixPath(path).suffix.lower()
            result.clips.append((
                animation_clip_name(entry.name, path), path, ext))
    return result
