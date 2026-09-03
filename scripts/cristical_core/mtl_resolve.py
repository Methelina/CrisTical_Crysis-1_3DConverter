#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mtl_resolve.py — CrisTical: unified .mtl resolution for the converters
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Shared between the character orchestrator (cdf2gltf.py) and the static
geometry orchestrator (cgf2gltf.py).  Both call :func:`resolve_mtl` with
the model path and the sub-material names taken from the mesh primitives.

Resolution is served from a single, once-built virtual index
(cryvfs.mount_game): every reachable ``.mtl`` (loose files and .pak
entries) is parsed once and cached as ``{vfs_path -> sub-material names}``.
Subsequent calls for the same game directories reuse that registry, so the
expensive full-tree scan + .pak probing happens exactly once per process
instead of on every model.  Only the winning .mtl is materialized to disk
(when it lives inside a .pak); everything else is scored from memory.

Scoring order (fixed vs legacy): candidates are ranked by
(matched, name-proximity, net score) — how many wanted sub-material names
they cover first, then how close the file name is to the model, with net
score (matched*2 - extra) only as a final tie-break.

"""

import hashlib
import os

try:
    from .cryvfs import mount_game, index_open_bytes
    from .cryxmlb import load_file, load_bytes
except ImportError:  # running as a script
    from cryvfs import mount_game, index_open_bytes
    from cryxmlb import load_file, load_bytes

# Common suffix patterns used by Crysis assets for LOD / size variants.
_LOD_SUFFIXES = ("_lod1", "_lod2", "_lod3", "_lod4", "_lod5")
_SIZE_SUFFIXES = ("_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h")

# Temp dir where the (single) winning .pak .mtl is materialized on demand.
_VFS_MTL_TEMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "temp", "mtl_vfs")


def submaterial_names(mtl_path):
    """Parse a .mtl (plain or binary XML) on disk and return the set of its
    sub-material names.

    Handles both the <SubMaterials> list form and the single <Material>
    form.  Returns an empty set on parse failure or missing names.
    """
    try:
        root = load_file(mtl_path)
    except Exception:
        return set()
    names = set()
    sub = root.find("SubMaterials")
    if sub is not None:
        for m in sub.findall("Material"):
            n = m.get("Name")
            if n:
                names.add(n)
    elif root.tag == "Material":
        n = root.get("Name")
        if n:
            names.add(n)
    return names


# ---------------------------------------------------------------------------
# Game-wide .mtl registry (built once per game-directories set)
# ---------------------------------------------------------------------------

_MTL_REGISTRY_CACHE = {}


def _subs_from_root(root):
    names = set()
    sub = root.find("SubMaterials")
    if sub is not None:
        for m in sub.findall("Material"):
            n = m.get("Name")
            if n:
                names.add(n)
    elif root.tag == "Material":
        n = root.get("Name")
        if n:
            names.add(n)
    return names


def _mtl_registry(game_dirs):
    """(index, names, recs) for the given game dirs, built once.

    ``names`` maps normalized VFS .mtl path -> frozenset of sub-material
    names (parsed from bytes — no disk writes). ``recs`` maps the same path
    to its cryvfs record (loose/pak + provider).``index`` is the shared
    VFSIndex. Built lazily and cached by the game-dirs tuple.
    """
    key = tuple(str(d) for d in game_dirs)
    cached = _MTL_REGISTRY_CACHE.get(key)
    if cached is not None:
        return cached
    idx = mount_game(list(game_dirs))
    names = {}
    recs = {}
    for k in idx.keys():
        if not k.endswith(".mtl"):
            continue
        try:
            root = load_bytes(index_open_bytes(idx, k))
        except Exception:
            continue
        subs = _subs_from_root(root)
        recs[k] = idx.get(k)
        if subs:
            names[k] = frozenset(subs)
    reg = (idx, names, recs)
    _MTL_REGISTRY_CACHE[key] = reg
    return reg


def _materialize_mtl(idx, key, rec):
    """Real on-disk path for a candidate. Loose records return their path;
    .pak records are extracted (once) to temp and cached."""
    if rec["kind"] == "loose":
        return rec["real"]
    os.makedirs(_VFS_MTL_TEMP, exist_ok=True)
    base = os.path.basename(key)
    stem, ext = os.path.splitext(base)
    digest = hashlib.md5(key.encode("utf-8")).hexdigest()[:8]
    tmp = os.path.join(_VFS_MTL_TEMP, "%s_%s%s" % (stem, digest, ext))
    if not os.path.isfile(tmp):
        data = index_open_bytes(idx, key)
        with open(tmp, "wb") as f:
            f.write(data)
    return tmp


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _base_stem(path):
    """Lowercased file stem of a path, with optional variant suffixes removed."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    for suf in _LOD_SUFFIXES:
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
    if len(stem) >= 2 and stem[-2:] in _SIZE_SUFFIXES:
        stem = stem[:-2]
    return stem


def _name_proximity(candidate_stem, model_stem):
    """0..1 similarity between a candidate stem and the model stem.

    Used to rank candidates that cover the same wanted names: prefer the
    .mtl whose file name is closest to the model's file name.
    """
    a, b = candidate_stem.lower(), model_stem.lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # longest common prefix ratio
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    prefix = n / max(len(a), len(b))
    # containment bonus: one name fully inside the other
    contain = 1.0 if (a in b or b in a) else 0.0
    return max(prefix, contain * 0.9)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_mtl(model_path, game_dirs, prim_materials=None, strip_suffixes=False,
                verbose=False, interactive=False, log=print):
    """Find the best .mtl for a model (.chr/.cdf/.cgf).

    Args:
        model_path: path to the model whose material we need.
        game_dirs: list of game root directories (priority order).
        prim_materials: sub-material names bound to the mesh primitives
            (from the MtlName chunk).  Used for overlap scoring; when empty
            or None, scoring is skipped and only name/fallback rules apply.
        strip_suffixes: when True, also accept a .mtl whose stem matches the
            model stem with LOD/size suffixes stripped (CGF-style variants).
        verbose: when True, print every candidate with its sub-materials,
            score and proximity before choosing (helps spot wrong resolution).
        interactive: when True and scoring is ambiguous (multiple candidates
            with the same top rank), prompt the user to pick one by number.
        log: callable used for verbose output (default print).

    Returns:
        Path to the best .mtl candidate.  Never raises; a missing adjacent
        file name is returned as a last resort.
    """
    mtl = os.path.splitext(model_path)[0] + ".mtl"
    if os.path.isfile(mtl):
        return mtl

    model_stem = os.path.splitext(os.path.basename(model_path))[0].lower()
    base_stem = _base_stem(model_path) if strip_suffixes else None
    want = set(prim_materials or [])

    scored = []
    exact = None
    idx = None
    recs = None
    if game_dirs:
        idx, names, recs = _mtl_registry(game_dirs)
        for key, subs in names.items():
            bn = os.path.splitext(os.path.basename(key))[0]
            if bn == model_stem:
                exact = key
                if verbose:
                    log("  [mtl] exact name match: %s" % key)
                break
            if strip_suffixes and base_stem and bn == base_stem:
                exact = key
                if verbose:
                    log("  [mtl] exact stem match: %s" % key)
                break
            if want:
                matched = len(subs & want)
                extra = len(subs - want)
                scored.append({
                    "key": key,
                    "name": os.path.basename(key),
                    "sub_materials": sorted(subs),
                    "matched": matched,
                    "extra": extra,
                    "score": matched * 2 - extra,
                    "proximity": _name_proximity(bn, model_stem),
                })

    if exact is not None:
        return _materialize_mtl(idx, exact, recs[exact])

    if verbose and scored:
        relevant = [c for c in scored if c["matched"] > 0]
        display = relevant or sorted(scored, key=lambda c: c["score"], reverse=True)[:5]
        log("  [mtl] model stem: %s   wanted sub-materials: %s" % (
            model_stem, ", ".join(sorted(want)) if want else "(none)"))
        log("  [mtl] %d relevant candidates (matched/extra/score/proximity):" % len(display))
        for i, c in enumerate(sorted(display, key=lambda c: (c["matched"], c["proximity"], c["score"]), reverse=True)):
            log("    %2d. %-28s matched=%-2d extra=%-2d score=%-3d prox=%.2f  %s" % (
                i + 1, c["name"], c["matched"], c["extra"], c["score"],
                c["proximity"], ", ".join(c["sub_materials"])))

    if scored:
        # Rank by (matched, proximity, score). When several candidates match
        # the same wanted set, the closest file name wins (beach_bush.mtl
        # over forest_ground.mtl for Beach_Bush_big_b when both only contain
        # 'leaf'). Net score is a final tie-break only.
        def _rank(c):
            return (c["matched"], c["proximity"], c["score"])

        top = max(scored, key=_rank)
        tied = [c for c in scored if _rank(c) == _rank(top)]
        if len(tied) > 1 and interactive:
            log("  [mtl] AMBIGUOUS — several .mtl files match identically:")
            for i, c in enumerate(tied):
                log("    %d. %s  (%s)" % (i + 1, c["name"],
                                           ", ".join(c["sub_materials"])))
            while True:
                try:
                    choice = input("  [mtl] pick sub-material file number (0 to skip): ").strip()
                    if choice == "0" or choice == "":
                        break
                    idx_n = int(choice) - 1
                    if 0 <= idx_n < len(tied):
                        if verbose:
                            log("  [mtl] user selected: %s" % tied[idx_n]["name"])
                        return _materialize_mtl(idx, tied[idx_n]["key"], recs[tied[idx_n]["key"]])
                except (ValueError, EOFError):
                    pass
                log("  [mtl] invalid choice, try again")
        if verbose and top["matched"] > 0:
            log("  [mtl] selected: %s (matched=%d, proximity=%.2f, score=%d)" % (
                top["name"], top["matched"], top["proximity"], top["score"]))
        if top["matched"] > 0:
            return _materialize_mtl(idx, top["key"], recs[top["key"]])

    # fallback: first .mtl next to the model
    model_dir = os.path.dirname(model_path)
    if os.path.isdir(model_dir):
        candidates = [f for f in os.listdir(model_dir) if f.lower().endswith(".mtl")]
        if candidates:
            fallback = os.path.join(model_dir, candidates[0])
            if verbose:
                log("  [mtl] WARN: no score>0 candidate, falling back to %s" % fallback)
            return fallback
    return mtl
