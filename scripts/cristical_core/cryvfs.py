#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cryvfs.py — CrisTical: virtual pack file system for game data roots
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

A small pack-file-system abstraction built while studying how CryEngine
resolves game assets across loose files and stacked .pak search paths
(cascaded mounts, per-mod layers). The interface can be backed by the real
filesystem, by cascaded search paths, or by zip / encrypted-pak archives
(the latter via crypak).

Key pieces:
  - IPackFileSystem / RealFileSystem / CascadedPackFileSystem: the
    search-path semantics observed in the engine's data-root resolution
  - ZipFileSystem: plain ZIP-format .pak (Crysis 1 / Warhead / Wars /
    Remastered)
  - VFSIndex / mount_game: one flat index over every mounted root —
    loose entries plus every .pak (zip, xxtea, twofish), used by the
    orchestrators for companion-asset lookups (.mtl/.cal/.chrparams/
    .lmg/.bspace) and materialization of pak entries to real files
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import struct
import zipfile
import zlib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Iterable

__all__ = [
    "IPackFileSystem", "RealFileSystem", "CascadedPackFileSystem",
    "InMemoryFileSystem", "ZipFileSystem", "open_pak",
    "mount_gamedirs", "mount_layers", "materialize",
    "VFSIndex", "mount_game", "index_open_bytes",
]


def _normalize(path: str) -> str:
    """Normalize a CryEngine path: forward slashes, lowercased,
    no leading slash. CryEngine paths are case-insensitive on disk."""
    p = path.replace("\\", "/").lstrip("/")
    return p


class IPackFileSystem(ABC):
    """Unified read access over a data root: loose files or .pak archives."""

    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def open(self, path: str) -> BinaryIO: ...

    @abstractmethod
    def read_all_bytes(self, path: str) -> bytes: ...

    @abstractmethod
    def glob(self, pattern: str) -> Iterable[str]: ...

    def real_path(self, path: str) -> "str | None":
        """Existing on-disk path for ``path`` when this layer is backed by
        the real filesystem; ``None`` otherwise (zip/pak, in-memory)."""
        return None


class RealFileSystem(IPackFileSystem):
    """Backed by an on-disk directory (a data root's loose tree)."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _resolve(self, path: str) -> Path | None:
        # Try the given path verbatim; if not found, do a case-insensitive
        # walk component-by-component (CryEngine PAKs are case-insensitive).
        rel = _normalize(path)
        candidate = self._root / rel
        if candidate.is_file():
            return candidate
        # Case-insensitive resolve.
        cur = self._root
        for part in rel.split("/"):
            if not part:
                continue
            try:
                entries = list(cur.iterdir())
            except (FileNotFoundError, NotADirectoryError):
                return None
            match = next((e for e in entries if e.name.lower() == part.lower()), None)
            if match is None:
                return None
            cur = match
        return cur if cur.is_file() else None

    def _resolve_dir(self, path: str) -> Path | None:
        rel = _normalize(path)
        if not rel:
            return self._root
        cur = self._root
        for part in rel.split("/"):
            if not part:
                continue
            try:
                entries = list(cur.iterdir())
            except (FileNotFoundError, NotADirectoryError):
                return None
            match = next((e for e in entries if e.name.lower() == part.lower()), None)
            if match is None or not match.is_dir():
                return None
            cur = match
        return cur

    def real_path(self, path: str) -> "str | None":
        resolved = self._resolve(path)
        return str(resolved) if resolved is not None else None

    def exists(self, path: str) -> bool:
        return self._resolve(path) is not None

    def open(self, path: str) -> BinaryIO:
        resolved = self._resolve(path)
        if resolved is None:
            raise FileNotFoundError(path)
        return resolved.open("rb")

    def read_all_bytes(self, path: str) -> bytes:
        resolved = self._resolve(path)
        if resolved is None:
            raise FileNotFoundError(path)
        return resolved.read_bytes()

    def glob(self, pattern: str) -> Iterable[str]:
        norm_pattern = _normalize(pattern)
        parts = norm_pattern.split("/")
        prefix_parts: list[str] = []
        for part in parts:
            if any(ch in part for ch in "*?["):
                break
            prefix_parts.append(part)
        base_dir = self._resolve_dir("/".join(prefix_parts))
        if base_dir is None:
            return

        remaining = parts[len(prefix_parts):]
        if len(remaining) == 1:
            candidates = base_dir.iterdir()
        else:
            candidates = base_dir.rglob("*")
        norm_pattern_lower = norm_pattern.lower()
        for p in candidates:
            if not p.is_file():
                continue
            rel = str(p.relative_to(self._root)).replace(os.sep, "/")
            if fnmatch.fnmatchcase(rel.lower(), norm_pattern_lower):
                yield rel


class CascadedPackFileSystem(IPackFileSystem):
    """Stack of file systems searched in LIFO order (mod-over-game layering)."""

    def __init__(self, layers: Iterable[IPackFileSystem] = ()) -> None:
        self._layers: list[IPackFileSystem] = list(layers)

    def push(self, fs: IPackFileSystem) -> None:
        self._layers.append(fs)

    def exists(self, path: str) -> bool:
        return any(fs.exists(path) for fs in reversed(self._layers))

    def open(self, path: str) -> BinaryIO:
        for fs in reversed(self._layers):
            if fs.exists(path):
                return fs.open(path)
        raise FileNotFoundError(path)

    def read_all_bytes(self, path: str) -> bytes:
        for fs in reversed(self._layers):
            if fs.exists(path):
                return fs.read_all_bytes(path)
        raise FileNotFoundError(path)

    def glob(self, pattern: str) -> Iterable[str]:
        seen: set[str] = set()
        for fs in reversed(self._layers):
            for p in fs.glob(pattern):
                if p not in seen:
                    seen.add(p)
                    yield p

    def real_path(self, path: str) -> "str | None":
        for fs in reversed(self._layers):
            real = fs.real_path(path)
            if real is not None:
                return real
        return None


class InMemoryFileSystem(IPackFileSystem):
    """Test helper. Holds a dict of path -> bytes."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files: dict[str, bytes] = {
            _normalize(k): v for k, v in (files or {}).items()
        }

    def add(self, path: str, data: bytes) -> None:
        self._files[_normalize(path)] = data

    def exists(self, path: str) -> bool:
        return _normalize(path) in self._files

    def open(self, path: str) -> BinaryIO:
        key = _normalize(path)
        if key not in self._files:
            raise FileNotFoundError(path)
        return io.BytesIO(self._files[key])

    def read_all_bytes(self, path: str) -> bytes:
        key = _normalize(path)
        if key not in self._files:
            raise FileNotFoundError(path)
        return self._files[key]

    def glob(self, pattern: str) -> Iterable[str]:
        norm_pattern = _normalize(pattern).lower()
        for key in self._files:
            if fnmatch.fnmatchcase(key.lower(), norm_pattern):
                yield key


# Custom zip compression method used by the Crysis 2/3 Remastered
# resource compiler for ``*_chk0.pak`` archives: LZ4 block streams.
# Not a stdlib zip method; the paks also carry a ``.lz4compression``
# marker entry (method 0) advertising the scheme.
_ZIP_METHOD_LZ4 = 12


def _lz4_decompress_block(data: bytes, uncompressed_size: int) -> bytes:
    """Decompress one LZ4-block zip payload (method 12).

    Engine behaviour: the Remastered resource compiler stores raw LZ4
    block streams whose exact output size is the zip entry's
    ``file_size`` (the standard LZ4 block API needs the size passed in).
    ``lz4`` is imported lazily so environments without it still load
    this module; method-12 entries then raise a clear error naming the
    missing dependency.

    Packer-bug tolerance (engine-mirroring): one entry per ~4200 in the
    shipped C2R paks (e.g. ``plate_up_ddn.dds.8``) declares an
    ``uncompressed_size`` SHORTER than what its LZ4 block actually
    produces (the RC's last-block flush included 50 stale buffer
    bytes). The game engine tolerates this - it trusts the declared
    size and slices; the zip CRC does not match the payload either
    (method-12 CRCs are never validated). Strict decompression fails
    on such entries, so: retry with slack and slice back to the
    declared size, exactly like the engine's reader.
    """
    try:
        import lz4.block  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "pak uses LZ4 compression (method 12) but the 'lz4' package "
            "is not installed (pip install lz4)") from e
    try:
        return lz4.block.decompress(data, uncompressed_size=uncompressed_size)
    except Exception:
        # Oversized block (see note): decompress with slack and slice.
        slack = min(uncompressed_size, 64 * 1024)
        out = lz4.block.decompress(data, uncompressed_size=uncompressed_size + slack)
        if len(out) < uncompressed_size:
            raise
        return out[:uncompressed_size]


class ZipFileSystem(IPackFileSystem):
    """Backed by a ZIP archive (vanilla CryEngine ``.pak`` / ``.zip``).

    Crysis-family titles ship their game data as ZIP-format ``.pak``
    files; ``zipfile`` from the stdlib covers them, no extra deps.

    Lookups are case-insensitive (CryEngine convention). Internally we
    build a single lower-case -> real-name map at construction time, so
    ``exists`` / ``open`` / ``read_all_bytes`` are O(1).
    """

    def __init__(self, archive: str | os.PathLike[str] | zipfile.ZipFile) -> None:
        if isinstance(archive, zipfile.ZipFile):
            self._zip = archive
            self._owns_zip = False
            self._source: Path | None = None
        else:
            self._source = Path(archive).resolve()
            self._zip = zipfile.ZipFile(self._source, "r")
            self._owns_zip = True

        # Build a lower-case lookup. Skip directory entries (trailing '/').
        self._index: dict[str, str] = {}
        for name in self._zip.namelist():
            if name.endswith("/"):
                continue
            self._index[_normalize(name).lower()] = name

    @property
    def source(self) -> Path | None:
        return self._source

    def close(self) -> None:
        if self._owns_zip:
            self._zip.close()

    def __enter__(self) -> "ZipFileSystem":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _resolve(self, path: str) -> str | None:
        return self._index.get(_normalize(path).lower())

    def _raw_read(self, real_name: str) -> bytes:
        """Read entry data bypassing the local-header filename check.

        Crysis Remastered paks store ``/``-separated names in the central
        directory but ``\\``-separated names in the local file headers;
        ``zipfile`` refuses such entries ("File name in directory ... and
        header ... differ").  The data itself is fine, so read the
        payload straight from the file with the CD metadata only.

        Compression methods (per the zip central directory):
          0  = stored;
          8  = raw DEFLATE (zlib with -15 window);
          12 = LZ4 block (Crysis 2/3 Remastered ``*_chk0.pak`` ship a
               ``.lz4compression`` marker entry and LZ4-compressed
               payloads; the engine's resource compiler writes this
               custom method, python's ``zipfile`` knows neither 12 nor
               the marker). Engine-derived behaviour: the payload is a
               raw LZ4 block stream whose uncompressed size is the zip
               entry's ``file_size``.
        """
        try:
            return self._zip.read(real_name)
        except zipfile.BadZipFile as e:
            # The / vs \ filename mismatch above - fall through to the
            # manual local-header parse. Keep the original error for the
            # re-raise path below so nothing is silently swallowed.
            cd_error = e
        info = self._zip.getinfo(real_name)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                     _ZIP_METHOD_LZ4):
            raise RuntimeError(
                "unsupported pak compression method %d for %s"
                % (info.compress_type, real_name)) from cd_error
        # Parse the local header ourselves: PK\x03\x04 + 26 fixed bytes,
        # then name + extra; the data starts right after them.
        self._zip.fp.seek(info.header_offset)
        fixed = self._zip.fp.read(30)
        if fixed[:4] != b"PK\x03\x04":
            raise zipfile.BadZipFile("bad local header for %s" % real_name)
        name_len, extra_len = struct.unpack("<HH", fixed[26:30])
        data_start = info.header_offset + 30 + name_len + extra_len
        self._zip.fp.seek(data_start)
        raw = self._zip.fp.read(info.compress_size)
        if info.compress_type == zipfile.ZIP_DEFLATED:
            return zlib.decompress(raw, -15)
        if info.compress_type == _ZIP_METHOD_LZ4:
            # Engine behaviour (Crysis 2/3 Remastered resource compiler):
            # method 12 payloads are LZ4 block streams sized to the zip
            # entry's uncompressed size; verified against real paks.
            return _lz4_decompress_block(raw, info.file_size)
        return raw

    def exists(self, path: str) -> bool:
        return self._resolve(path) is not None

    def open(self, path: str) -> BinaryIO:
        real = self._resolve(path)
        if real is None:
            raise FileNotFoundError(path)
        # ``ZipFile.open`` returns a non-seekable stream; chunk readers
        # in ``io.binary_reader`` rely on ``seek``/``tell``, so wrap in
        # an in-memory buffer.
        return io.BytesIO(self._raw_read(real))

    def read_all_bytes(self, path: str) -> bytes:
        real = self._resolve(path)
        if real is None:
            raise FileNotFoundError(path)
        return self._raw_read(real)

    def iter_names(self) -> Iterable[str]:
        """All normalized (lower-cased, forward-slash) stored file names."""
        return iter(self._index.keys())

    def iter_entry_names(self) -> Iterable[str]:
        """All stored file names with their original archive spelling."""
        return iter(self._index.values())

    def glob(self, pattern: str) -> Iterable[str]:
        # Match against the *original* (zip-stored) names but apply the
        # pattern in a case-insensitive, forward-slash form.
        norm_pattern = _normalize(pattern).lower()
        for key, real in self._index.items():
            if fnmatch.fnmatchcase(key, norm_pattern):
                yield real


def open_pak(archive: str | os.PathLike[str]) -> "ZipFileSystem | CryPakFileSystem":
    """Open a CryEngine ``.pak`` / ``.zip`` archive.

    Plain ZIP-format paks (Crysis 1, Remastered, most mods) are served by
    :class:`ZipFileSystem`.  Encrypted Crysis 3 paks (Twofish-CTR with
    RSA-OAEP-wrapped keys) fall back to :class:`CryPakFileSystem`.  The
    returned object implements the :class:`IPackFileSystem` interface.
    """
    try:
        return ZipFileSystem(archive)
    except zipfile.BadZipFile:
        try:
            from .crypak import CryPakFileSystem
        except ImportError:
            from crypak import CryPakFileSystem
        return CryPakFileSystem(archive)


_MOUNT_CACHE: "dict[tuple[str, ...], CascadedPackFileSystem]" = {}


def _root_layers(root: str) -> "list[IPackFileSystem]":
    """Desired search order inside ONE game root: loose files first,
    then every *.pak directly inside the root (case-insensitive name
    order). Unreadable paks are skipped with a warning."""
    layers: list[IPackFileSystem] = []
    if os.path.isdir(root):
        layers.append(RealFileSystem(root))
        paks = [
            entry for entry in os.listdir(root)
            if entry.lower().endswith(".pak")
            and os.path.isfile(os.path.join(root, entry))
        ]
        paks.sort(key=lambda name: name.lower())
        for pak_name in paks:
            pak_path = os.path.join(root, pak_name)
            try:
                layers.append(open_pak(pak_path))
            except Exception as e:
                print(f"[cryvfs] skipping unreadable pak: {pak_path} ({e})")
                continue
    return layers


def mount_gamedirs(game_dirs: list[str]) -> "CascadedPackFileSystem":
    """Build (memoized) a cascaded VFS over the given game roots.

    Search priority matches the existing ``--gamedir`` semantics: the
    FIRST directory in the list wins. Inside one root, loose files win
    over .pak contents; paks are searched in alphabetical order.
    ``CascadedPackFileSystem`` searches layers LIFO, so layers are
    pushed in the exact reverse of the desired search order.
    """
    cache_key = tuple(str(d) for d in game_dirs)
    if cache_key in _MOUNT_CACHE:
        return _MOUNT_CACHE[cache_key]

    desired_order: list[IPackFileSystem] = []
    for d in game_dirs:
        desired_order.extend(_root_layers(d))

    fs = CascadedPackFileSystem()
    for layer in reversed(desired_order):
        fs.push(layer)
    _MOUNT_CACHE[cache_key] = fs
    return fs


_LAYERS_CACHE: "dict[tuple[str, ...], list[CascadedPackFileSystem]]" = {}


def mount_layers(game_dirs: list[str]) -> "list[CascadedPackFileSystem]":
    """Per-root cascades in user order. Entry *i* covers game_dirs[i]:
    loose files plus that root's paks (loose wins inside the root). Used
    by consumers that must preserve per-root priority (texture extension
    probing)."""
    key = tuple(str(d) for d in game_dirs)
    if key in _LAYERS_CACHE:
        return _LAYERS_CACHE[key]

    result: list[CascadedPackFileSystem] = []
    for root in game_dirs:
        layers = _root_layers(root)
        if not layers:
            continue
        cascade = CascadedPackFileSystem()
        for layer in reversed(layers):
            cascade.push(layer)
        result.append(cascade)
    _LAYERS_CACHE[key] = result
    return result


_MATERIALIZE_CACHE: "dict[tuple[int, str, str], str]" = {}


def materialize(vfs: IPackFileSystem, path: str, temp_dir: str) -> "str | None":
    """Real on-disk path for a VFS path.

    Loose files resolve to their existing location; zip/pak entries are
    extracted once into ``temp_dir`` and the extracted path is
    returned. Returns ``None`` when ``path`` does not exist in ``vfs``.
    """
    if not vfs.exists(path):
        return None
    real = vfs.real_path(path)
    if real is not None:
        return real

    key = (id(vfs), str(temp_dir), _normalize(path).lower())
    cached = _MATERIALIZE_CACHE.get(key)
    if cached is not None and os.path.isfile(cached):
        return cached

    os.makedirs(temp_dir, exist_ok=True)
    norm = _normalize(path)
    base = norm.rsplit("/", 1)[-1]
    stem, ext = os.path.splitext(base)
    digest = hashlib.md5(norm.lower().encode("utf-8")).hexdigest()[:8]
    fname = "%s_%s%s" % (stem, digest, ext)
    tmp = os.path.join(temp_dir, fname)

    data = vfs.read_all_bytes(path)
    with open(tmp, "wb") as f:
        f.write(data)
    _MATERIALIZE_CACHE[key] = tmp
    return tmp


def _index_root(root, put):
    """Feed every reachable normalized rel-path under ``root`` to ``put``,
    loose files first, then each .pak archive (alphabetical). ``put`` keeps
    the FIRST (highest-priority) writer per key."""
    root_str = str(root)
    for dirpath, _dirnames, filenames in os.walk(root_str):
        for f in filenames:
            full = os.path.join(dirpath, f)
            rel = os.path.relpath(full, root_str).replace(os.sep, "/")
            put(rel.lower(), {"kind": "loose", "root": root_str,
                              "rel": rel.lower(), "real": full})
    paks = sorted(
        (n for n in os.listdir(root_str)
         if n.lower().endswith(".pak")
         and os.path.isfile(os.path.join(root_str, n))),
        key=str.lower)
    for pak_name in paks:
        pak_path = os.path.join(root_str, pak_name)
        try:
            fs = open_pak(pak_path)
        except Exception as e:
            print("[cryvfs] skipping unreadable pak: %s (%s)" % (pak_path, e))
            continue
        try:
            for rel_lower in fs.iter_names():
                put(rel_lower, {"kind": "pak", "root": root_str,
                                "pak": pak_name, "rel": rel_lower, "real": None})
        finally:
            close = getattr(fs, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass


class VFSIndex:
    """One, once-built, priority-ordered virtual index over several game
    roots and their .pak archives.

    Collapses every reachable file into a single lower-cased, forward-slash
    key space. A key maps to exactly one winner: the FIRST game root wins;
    inside a root, loose files win over .pak contents; paks are searched in
    case-insensitive alphabetical order. Lookups/iteration are O(1) dict
    reads — no repeated os.walk or per-call pak glob.
    """

    __slots__ = ("_index", "_basenames", "_game_dirs")

    def __init__(self, game_dirs):
        self._game_dirs = [str(d) for d in game_dirs]
        index = {}

        def put(key, record):
            if key not in index:
                index[key] = record

        for d in self._game_dirs:
            if os.path.isdir(d):
                _index_root(d, put)
        self._index = index

        basenames = {}
        for key in index:
            basenames.setdefault(key.rsplit("/", 1)[-1], []).append(key)
        self._basenames = basenames

    @property
    def game_dirs(self):
        return tuple(self._game_dirs)

    def __len__(self):
        return len(self._index)

    def exists(self, path):
        return _normalize(path) in self._index

    def get(self, path):
        return self._index.get(_normalize(path))

    def keys(self):
        return self._index.keys()

    def records(self):
        return self._index.values()

    def by_basename(self, basename):
        return self._basenames.get(_normalize(basename).rsplit("/", 1)[-1], [])

    def names_in(self, virtual_dir):
        d = _normalize(virtual_dir).strip("/")
        prefix = (d + "/") if d else ""
        return [k for k in self._index if k.startswith(prefix)]

    def glob(self, pattern):
        pat = _normalize(pattern).lower()
        return [k for k in self._index if fnmatch.fnmatchcase(k, pat)]

    def iter_names(self):
        return iter(self._index.keys())

    # --- IPackFileSystem-compatible surface (so VFSIndex can stand in for a
    # mounted layer in consumers that expect exists/glob/open/read_all_bytes) ---

    def open(self, path):
        rec = self._index.get(_normalize(path))
        if rec is None:
            raise FileNotFoundError(path)
        if rec["kind"] == "loose":
            return open(rec["real"], "rb")
        return io.BytesIO(_fs_for(rec).read_all_bytes(rec["rel"]))

    def read_all_bytes(self, path):
        return index_open_bytes(self, path)

    def real_path(self, path):
        rec = self._index.get(_normalize(path))
        if rec is None:
            return None
        return rec.get("real")


_PAK_OPEN_CACHE = {}


def _fs_for(record):
    if record["kind"] == "loose":
        return None
    pak_path = os.path.join(record["root"], record["pak"])
    fs = _PAK_OPEN_CACHE.get(pak_path)
    if fs is None:
        fs = open_pak(pak_path)
        _PAK_OPEN_CACHE[pak_path] = fs
    return fs


def index_open_bytes(index, path):
    """Return file bytes for a VFS path from an already-built VFSIndex."""
    rec = index.get(path)
    if rec is None:
        raise FileNotFoundError(path)
    if rec["kind"] == "loose":
        with open(rec["real"], "rb") as f:
            return f.read()
    return _fs_for(rec).read_all_bytes(rec["rel"])


_INDEX_MOUNT_CACHE = {}


def mount_game(game_dirs):
    """Memoized full VFS index over the given game roots.

    This is the single mount entry point consumers should use so the whole
    conversion shares one index instead of each module re-walking the disk
    / re-opening archives. Loose files and every .pak are indexed once.
    """
    key = tuple(str(d) for d in game_dirs)
    idx = _INDEX_MOUNT_CACHE.get(key)
    if idx is None:
        idx = VFSIndex(game_dirs)
        _INDEX_MOUNT_CACHE[key] = idx
    return idx
