#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
mtl_resolve.py — unified .mtl resolution for CrisTical converters.

Shared between the character orchestrator (cdf2gltf.py) and the static
geometry orchestrator (cgf2gltf.py).  Both call :func:`resolve_mtl` with
the model path and the sub-material names taken from the mesh primitives,
scoring candidates by how well their sub-material names match: exact
file-name match first, then a sub-material-name overlap score, then a
directory fallback.

Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
"""

import os
import xml.etree.ElementTree as ET

# Common suffix patterns used by Crysis assets for LOD / size variants.
_LOD_SUFFIXES = ("_lod1", "_lod2", "_lod3", "_lod4", "_lod5")
_SIZE_SUFFIXES = ("_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h")


def submaterial_names(mtl_path):
    """Parse a .mtl and return the set of its sub-material names.

    Handles both the <SubMaterials> list form and the single <Material>
    form.  Returns an empty set on parse failure or missing names.
    """
    try:
        tree = ET.parse(mtl_path)
    except Exception:
        return set()
    root = tree.getroot()
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


def _collect_candidates(model_path, game_dirs):
    """Gather every candidate .mtl path, de-duplicated.

    The model's own directory is checked first, then each game dir is
    walked recursively.  All paths are normalized before de-duplication.
    """
    candidates = []
    seen = set()
    model_dir = os.path.dirname(model_path)
    if os.path.isdir(model_dir):
        for f in os.listdir(model_dir):
            if f.lower().endswith(".mtl"):
                p = os.path.join(model_dir, f)
                seen.add(os.path.normpath(p))
                candidates.append(p)
    for gd in game_dirs:
        for root, _dirs, files in os.walk(gd):
            for f in files:
                if f.lower().endswith(".mtl"):
                    p = os.path.join(root, f)
                    np = os.path.normpath(p)
                    if np not in seen:
                        seen.add(np)
                        candidates.append(p)
    return candidates


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


def _score_names(names, want):
    """Score a candidate's sub-material set against the wanted set.

    Returns (matched, extra, score):
      matched — number of wanted sub-material names present in the candidate
      extra   — number of candidate sub-material names NOT wanted (penalty)
      score   — matched*2 - extra (favours full coverage + no junk)
    """
    matched = len(names & want)
    extra = len(names - want)
    return matched, extra, matched * 2 - extra


def _name_proximity(candidate_stem, model_stem):
    """0..1 similarity between a candidate stem and the model stem.

    Used as a tie-breaker: when scores are equal, prefer the .mtl whose
    file name is closest to the model's file name.
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


def _score_candidate(mtl_path, model_stem, want):
    """Score a single .mtl candidate. Returns dict or None (no sub-materials)."""
    bn = os.path.splitext(os.path.basename(mtl_path).lower())[0]
    names = submaterial_names(mtl_path)
    if not names:
        return None
    matched, extra, score = _score_names(names, want)
    return {
        "path": mtl_path,
        "name": os.path.basename(mtl_path),
        "stem": bn,
        "sub_materials": sorted(names),
        "matched": matched,
        "extra": extra,
        "score": score,
        "proximity": _name_proximity(bn, model_stem),
    }


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
            with the same top score), prompt the user to pick one by number.
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

    candidates = _collect_candidates(model_path, game_dirs)
    scored = []

    for p in candidates:
        bn = os.path.splitext(os.path.basename(p).lower())[0]
        if bn == model_stem:
            if verbose:
                log("  [mtl] exact name match: %s" % p)
            return p
        if strip_suffixes and base_stem and bn == base_stem:
            if verbose:
                log("  [mtl] exact stem match: %s" % p)
            return p
        if want:
            info = _score_candidate(p, model_stem, want)
            if info is not None:
                scored.append(info)

    if verbose and scored:
        relevant = [c for c in scored if c["matched"] > 0]
        display = relevant or sorted(scored, key=lambda c: c["score"], reverse=True)[:5]
        log("  [mtl] model stem: %s   wanted sub-materials: %s" % (
            model_stem, ", ".join(sorted(want)) if want else "(none)"))
        log("  [mtl] %d relevant candidates (matched/extra/score/proximity):" % len(display))
        for i, c in enumerate(sorted(display, key=lambda c: (c["score"], c["proximity"]), reverse=True)):
            log("    %2d. %-28s matched=%-2d extra=%-2d score=%-3d prox=%.2f  %s" % (
                i + 1, c["name"], c["matched"], c["extra"], c["score"],
                c["proximity"], ", ".join(c["sub_materials"])))

    if scored:
        top = max(scored, key=lambda c: (c["score"], c["proximity"]))
        tied = [c for c in scored
                if c["score"] == top["score"] and c["matched"] == top["matched"]]
        if len(tied) > 1:
            # prefer higher name proximity; if still tied and interactive,
            # ask the user
            tied_by_prox = sorted(tied, key=lambda c: c["proximity"], reverse=True)
            if interactive and tied_by_prox[0]["proximity"] == tied_by_prox[1]["proximity"]:
                log("  [mtl] AMBIGUOUS — several .mtl files match the same score:")
                for i, c in enumerate(tied):
                    log("    %d. %s  (%s)" % (i + 1, c["name"],
                                               ", ".join(c["sub_materials"])))
                while True:
                    try:
                        choice = input("  [mtl] pick sub-material file number (0 to skip): ").strip()
                        if choice == "0" or choice == "":
                            break
                        idx = int(choice) - 1
                        if 0 <= idx < len(tied):
                            if verbose:
                                log("  [mtl] user selected: %s" % tied[idx]["name"])
                            return tied[idx]["path"]
                    except (ValueError, EOFError):
                        pass
                    log("  [mtl] invalid choice, try again")
                # fall through on skip/empty
            top = tied_by_prox[0]
            if verbose and top["score"] > 0:
                log("  [mtl] selected: %s (score=%d, proximity=%.2f)" % (
                    top["name"], top["score"], top["proximity"]))
        if top["score"] > 0:
            return top["path"]

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
