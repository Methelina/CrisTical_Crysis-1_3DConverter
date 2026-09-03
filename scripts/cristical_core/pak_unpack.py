#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
pak_unpack.py — CrisTical: unpack CryEngine .pak archives to a loose tree
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Produces the same layout as the bundled content folders seen next to an
install (e.g. ``<install>/__CONTENT``): every ``<name>.pak`` in a data root
is extracted into a sibling ``<name>.pak_Unpacked`` folder, preserving each
entry's original archive-relative path and case.

Works for plain ZIP-format paks (Crysis 1 / Warhead / Remastered / Wars) and
for the encrypted CryPak archives (Crysis 2 XXTEA, Crysis 3 Twofish), since
it goes through :func:`cristical_core.cryvfs.open_pak`.

Each fully unpacked pak writes a completion manifest
(``unpack_manifest.json``) into its ``<name>_Unpacked`` folder. Resume/verify
logic (``verify_unpacked``) is objective: presence + exact size of every
expected entry (from the pak central directory) plus a CRC32 of the single
newest file — the corruption candidate after an interrupted run. No full
re-hash of the tree is needed.

Typical use (library)::

    from cristical_core.pak_unpack import unpack_gamedir
    unpack_gamedir(r"F:\\Games\\Crysis_Remastered\\Game")

will write ``F:\\Games\\Crysis_Remastered\\__CONTENT\\*.pak_Unpacked``.
"""

from __future__ import annotations

import json
import os
import sys
import zlib

try:
    from .cryvfs import open_pak
except ImportError:  # running as a script
    from cryvfs import open_pak

try:
    from tqdm import tqdm
except ImportError:  # tqdm is optional — passthrough fallback
    def tqdm(iterable=None, **kwargs):
        if iterable is None:
            return ()
        return iterable


def _stderr_log(msg: str) -> None:
    """Verbose log sink: stderr + flush. Safe for MCP stdio transports."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _safe_rel_parts(entry_name: str) -> list[str]:
    """Split an archive-relative name into safe path components.

    Normalises ``/`` and ``\\``, drops empty/``.`` segments and refuses to
    ascend with ``..`` (returned paths never leave the output root). Drive
    letters / absolute prefixes are stripped by rejecting leading empty or
    ``..`` components.
    """
    parts: list[str] = []
    for seg in entry_name.replace("\\", "/").split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            # Cannot ascend above the extraction root — drop the segment.
            continue
        parts.append(seg)
    return parts


MANIFEST_NAME = "unpack_manifest.json"


def _manifest_path(dest_dir: str) -> str:
    """Path of the completion manifest inside an unpacked folder."""
    return os.path.join(dest_dir, MANIFEST_NAME)


def _write_manifest(dest_dir: str, data: dict) -> None:
    """Atomically write the manifest (tmp + replace) inside ``dest_dir``."""
    path = _manifest_path(dest_dir)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read_manifest(dest_dir: str) -> dict | None:
    """Return the completion manifest dict, or None if absent/invalid."""
    path = _manifest_path(dest_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _entry_infos(pak_path: str) -> list[tuple[str, int | None, int | None]]:
    """[(name, uncompressed_size, crc32)] straight from the central directory.

    Cheap: reads only archive metadata (central directory), never the payload
    — no decryption / decompression. Supports plain ZIP paks and the CryPak
    (Crysis 2/3) format by duck-typing the filesystem object.
    """
    fs = open_pak(pak_path)
    try:
        z = getattr(fs, "_zip", None)          # ZipFileSystem
        if z is not None:
            out = []
            for zi in z.infolist():
                if zi.is_dir():
                    continue
                out.append((zi.filename, zi.file_size, zi.CRC))
            return out
        entries = getattr(fs, "_entries", None)  # CryPakFileSystem
        if entries is not None:
            return [(e.name, e.uncomp_size, e.crc)
                    for e in entries.values()]
        return [(n, None, None) for n in fs.iter_entry_names()]
    finally:
        close = getattr(fs, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass


def _newest_file_path(dest_dir: str) -> str | None:
    """Absolute path of the most recently modified real file in the tree."""
    best = None
    best_time = -1.0
    for root, _dirs, files in os.walk(dest_dir):
        for fn in files:
            if fn == MANIFEST_NAME:
                continue
            p = os.path.join(root, fn)
            try:
                t = os.path.getmtime(p)
            except OSError:
                continue
            if t > best_time:
                best_time = t
                best = p
    return best


def _crc32_file(path: str) -> int:
    """CRC32 of a file on disk (streamed, low memory)."""
    crc = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def unpack_pak(pak_path: str, out_root: str, log=_stderr_log) -> tuple[str, int, int]:
    """Extract one .pak into ``out_root/<basename>_Unpacked/``.

    Handles plain ZIP-format paks and the encrypted CryPak archives
    (Crysis 2 XXTEA, Crysis 3 Twofish) via :func:`cryvfs.open_pak`. Entries
    that fail to read (e.g. an unsupported payload compression) are skipped
    individually and counted, so one bad entry never aborts the whole pak.

    A completion manifest (``unpack_manifest.json``) is written into the
    destination folder only AFTER every entry has been written and closed.
    Its presence is the objective "this pak is fully unpacked" signal used by
    :func:`verify_unpacked`; an interrupted/killed run leaves no manifest.

    Shows a tqdm progress bar (stderr) over the archive entries and prints
    verbose start/done lines through ``log``.

    Args:
        pak_path: path to the .pak archive.
        out_root: directory that will contain the ``<name>_Unpacked`` folder.
        log: callable(str) for verbose messages (default: stderr).

    Returns:
        (dest_dir, files_written, entries_skipped).
    """
    fs = open_pak(pak_path)
    base = os.path.splitext(os.path.basename(pak_path))[0]
    dest = os.path.join(out_root, base + "_Unpacked")
    os.makedirs(dest, exist_ok=True)
    written = 0
    skipped = 0
    skipped_names: list[str] = []
    try:
        names = list(fs.iter_entry_names())
        if log is not None:
            log("[unpack] %s: %d entries -> %s"
                % (os.path.basename(pak_path), len(names), dest))
        bar = tqdm(names, desc=base, unit="file",
                   mininterval=2.0, file=sys.stderr)
        for orig in bar:
            parts = _safe_rel_parts(orig)
            if not parts:
                skipped += 1
                continue
            target = os.path.join(dest, *parts)
            try:
                data = fs.read_all_bytes(orig)
            except Exception as e:
                skipped += 1
                skipped_names.append(orig)
                if log is not None:
                    log("  [unpack] skip %s (%s)" % (orig, e))
                continue
            parent = os.path.dirname(target)
            try:
                os.makedirs(parent, exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data)
                written += 1
            except OSError as e:
                skipped += 1
                skipped_names.append(orig)
                if log is not None:
                    log("  [unpack] skip write %s (%s)" % (orig, e))
    finally:
        close = getattr(fs, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                pass
    _write_manifest(dest, {
        "format": 1,
        "pak": os.path.basename(pak_path),
        "pak_size": os.path.getsize(pak_path),
        "entries_total": len(names),
        "files_written": written,
        "skipped": skipped_names,
    })
    if log is not None:
        log("[unpack] %s done: %d written, %d skipped (manifest written)"
            % (os.path.basename(pak_path), written, skipped))
    return dest, written, skipped


def verify_unpacked(pak_path: str, dest_dir: str) -> tuple[bool, str]:
    """Objective, current unpacked / not-unpacked check for one pak.

    A folder is "unpacked" only when every expected file is present with the
    exact expected size (from the pak central directory — the source of
    truth) AND the single newest (most recently written) file matches the
    archive's CRC32. The newest file is the corruption candidate when a run
    died or hung mid-write, so only that file needs re-hashing.

    A completion manifest, when present, supplies the list of entries that
    were legitimately skipped (unsupported payloads) and the pak size at
    unpack time; a pak that changed since is re-unpacked. When no manifest
    exists (e.g. a folder written by an older version) every central-directory
    entry is required — if the folder then verifies, a manifest is written so
    later runs are faster (``skipped=[]``).

    Cost: manifest read + central-directory metadata read (no payload
    decryption) + one ``stat`` per expected file + one CRC32 of the newest
    file. No full re-extract and no re-hashing of the whole tree.

    Args:
        pak_path: path to the .pak archive.
        dest_dir: the ``<name>_Unpacked`` folder to verify.

    Returns:
        (ok, reason). ``ok`` True means fully and correctly unpacked.
    """
    if not os.path.isdir(dest_dir):
        return False, "no unpacked folder"
    manifest = _read_manifest(dest_dir)
    skipped = set(manifest.get("skipped") or []) if manifest else set()
    if manifest is not None and manifest.get("pak_size") != os.path.getsize(pak_path):
        return False, "pak file changed since it was unpacked"
    try:
        infos = _entry_infos(pak_path)
    except Exception as e:
        return False, "cannot read pak index: %s" % e
    rel_to_info = {}
    for name, size, crc in infos:
        rel = "/".join(_safe_rel_parts(name))
        rel_to_info[rel.lower()] = (size, crc)
        if name in skipped:
            continue
        target = os.path.join(dest_dir, *_safe_rel_parts(name))
        if not os.path.isfile(target):
            return False, "missing file: %s" % name
        if size is not None:
            try:
                actual = os.path.getsize(target)
            except OSError:
                return False, "unreadable file: %s" % name
            if actual != size:
                return False, ("size mismatch for %s: disk %d, pak %d"
                               % (name, actual, size))
    newest = _newest_file_path(dest_dir)
    if newest is not None:
        rel = os.path.relpath(newest, dest_dir).replace(os.sep, "/")
        info = rel_to_info.get(rel.lower())
        if info is not None and info[1]:
            try:
                crc = _crc32_file(newest)
            except Exception as e:
                return False, "cannot read newest file %s: %s" % (newest, e)
            if crc != info[1]:
                return False, "crc32 mismatch on newest file: %s" % newest
    if manifest is None:
        # Adopt a legacy folder written without a manifest: it verified
        # against the central directory, so record the completion.
        _write_manifest(dest_dir, {
            "format": 1,
            "pak": os.path.basename(pak_path),
            "pak_size": os.path.getsize(pak_path),
            "entries_total": len(infos),
            "files_written": len(infos),
            "skipped": [],
        })
    return True, "verified: %d entries, sizes ok, newest-file crc ok" % len(infos)


def unpack_gamedir(game_root: str, out_root: str | None = None,
                   log=_stderr_log) -> dict:
    """Unpack every ``.pak`` under a data root into a ``__CONTENT`` folder.

    Mirrors the bundled content layout: if ``game_root`` is a data root
    (folder containing the .pak files, e.g. ``...\\Game``), the output goes
    to ``<parent>/__CONTENT`` by default (so ``Game`` and ``__CONTENT`` sit
    as siblings, exactly like ``F:\\Games\\Crysis_Remastered``).

    Verbose: prints a per-pak header before each archive (stderr) so long
    multi-GB runs can be followed in a redirected log.

    Args:
        game_root: data root directory whose ``*.pak`` files to unpack.
        out_root: override the directory that will hold the
            ``*.pak_Unpacked`` folders.
            Defaults to ``<parent of game_root>/__CONTENT``.
        log: callable(str) for verbose messages (default: stderr).

    Returns:
        Summary dict: {out_root, pak_count, files_written, entries_skipped,
        unpacked: [{pak, dir, files}]}.
    """
    if not os.path.isdir(game_root):
        raise FileNotFoundError("game root not found: %s" % game_root)
    paks = sorted(
        (n for n in os.listdir(game_root)
         if n.lower().endswith(".pak")
         and os.path.isfile(os.path.join(game_root, n))),
        key=str.lower)
    if out_root is None:
        out_root = os.path.join(os.path.dirname(game_root), "__CONTENT")
    os.makedirs(out_root, exist_ok=True)
    summary = {"out_root": out_root, "pak_count": len(paks),
               "files_written": 0, "entries_skipped": 0, "unpacked": []}
    for idx, pak_name in enumerate(paks, 1):
        pak_path = os.path.join(game_root, pak_name)
        if log is not None:
            log("[unpack] === pak %d/%d: %s ===" % (idx, len(paks), pak_name))
        try:
            dest, written, skipped = unpack_pak(pak_path, out_root, log=log)
        except Exception as e:
            summary["unpacked"].append(
                {"pak": pak_name, "dir": None, "files": 0,
                 "error": str(e)})
            if log is not None:
                log("[unpack] ERROR %s: %s" % (pak_name, e))
            continue
        summary["files_written"] += written
        summary["entries_skipped"] += skipped
        summary["unpacked"].append(
            {"pak": pak_name, "dir": dest, "files": written})
    if log is not None:
        log("[unpack] all paks done: %d written, %d skipped -> %s"
            % (summary["files_written"], summary["entries_skipped"],
               out_root))
    return summary


def plan_game_unpack(path: str, out_root: str | None = None) -> dict:
    """Resolve a folder (install dir or data root) to its data root and
    report what a full unpack would write — WITHOUT extracting anything.

    Returns: {root, out_root, pak_count, paks: [(name, bytes)], total_bytes}.
    """
    from .game_profile import find_data_root
    root = find_data_root(str(path))
    if not os.path.isdir(root):
        raise FileNotFoundError("game data root not found: %s" % root)
    if out_root is None:
        out_root = os.path.join(os.path.dirname(root), "__CONTENT")
    paks = sorted(
        (n for n in os.listdir(root)
         if n.lower().endswith(".pak")
         and os.path.isfile(os.path.join(root, n))),
        key=str.lower)
    sized = [(n, os.path.getsize(os.path.join(root, n))) for n in paks]
    return {"root": root, "out_root": out_root, "pak_count": len(sized),
            "paks": sized, "total_bytes": sum(s for _, s in sized)}


def unpack_game(path: str, out_root: str | None = None) -> dict:
    """Unpack a whole game (detect its data root first) into ``__CONTENT``.

    Accepts either a data root (folder with the ``*.pak`` files) or an
    install directory (the data root is found via ``GameData.pak``). Writes
    ``<parent of root>/__CONTENT/<name>.pak_Unpacked`` by default.
    """
    info = plan_game_unpack(path, out_root)
    return unpack_gamedir(info["root"], info["out_root"])


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        print(__doc__)
        print("Usage: python -m cristical_core.pak_unpack <game_root|install_dir|one.pak> [out_dir]")
        sys.exit(1)
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if path.lower().endswith(".pak"):
        dest, w, s = unpack_pak(path, out or os.path.dirname(path))
        print("unpacked %s -> %s (%d files, %d skipped)" % (path, dest, w, s))
    else:
        summary = unpack_game(path, out)
        print("=" * 60)
        print("out:     %s" % summary["out_root"])
        print("paks:    %d   files written: %d   skipped: %d"
              % (summary["pak_count"], summary["files_written"],
                 summary["entries_skipped"]))
        for u in summary["unpacked"]:
            if u.get("error"):
                print("  %-28s ERROR: %s" % (u["pak"], u["error"]))
            else:
                print("  %-28s -> %s (%d files)"
                      % (u["pak"], u["dir"], u["files"]))
        print("Done.")
