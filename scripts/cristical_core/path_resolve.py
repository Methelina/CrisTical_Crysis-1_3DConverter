#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
path_resolve.py — CrisTical: virtual (in-pak) geometry path resolution
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

The VFS (``cristical_core.cryvfs``) materializes side resources (.mtl,
.dds, .dba/.caf, scripts) for the conversion pipeline, but the geometry
itself (.cgf/.cga/.cdf/.chr) was always required to be a real file on
disk.  This module lets CLI / GUI / MCP accept a virtual path like
``Objects/3dtext/a.cgf`` and transparently materialize it through the
mounted VFS before ``read_cgf*`` is called.

Typical use (CGF CLI, GUI, MCP — same helper everywhere)::

    real_path = resolve_geometry_path(args.cgf, args.gamedir, temp_dir)

If ``input_path`` is already a real file on disk it is returned
unchanged; no VFS is mounted at all.
"""

from __future__ import annotations

import os

try:
    from .cryvfs import mount_gamedirs, materialize
except ImportError:  # standalone import (module run outside the package)
    from cryvfs import mount_gamedirs, materialize  # type: ignore

__all__ = ["resolve_geometry_path", "PROJ_TEMP_GEOM"]

# Default materialization dir: <project root>/temp/geom (same layout as
# the .mtl materialization dir in mtl_resolve._VFS_MTL_TEMP).
PROJ_TEMP_GEOM = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "temp", "geom",
)


def resolve_geometry_path(
    input_path: str,
    game_dirs: "list[str]",
    temp_dir: str = PROJ_TEMP_GEOM,
) -> str:
    """Resolve a geometry input path to a real on-disk file path.

    If ``input_path`` is a real file on disk, return it unchanged.
    Otherwise treat it as a virtual path inside the mounted VFS
    (``mount_gamedirs(game_dirs)``) and materialize it to ``temp_dir``.

    Raises:
        FileNotFoundError: the path is neither a real file nor a VFS
            member (also when ``game_dirs`` is empty or mounts nothing).
    """
    if os.path.isfile(input_path):
        return input_path

    if not game_dirs:
        raise FileNotFoundError(
            "input is a virtual path (%s) but no game directories are "
            "configured — pass --gamedir pointing at the game root" % input_path)

    vfs = mount_gamedirs([str(d) for d in game_dirs])
    real = materialize(vfs, input_path, temp_dir)
    if real is None:
        raise FileNotFoundError(
            "not found as a real file or inside the mounted game "
            "directories %s: %s" % (list(game_dirs), input_path))
    return real
