#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
twofish_fast.py — CrisTical: optional accelerated Twofish-CTR backends
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

The pure-Python reference lives in :mod:`cristical_core.crypak` and is
the ground truth for every accelerated path here.
This module adds two optional backends for bulk pak decryption:

* ``numba`` — the 16-round ECB + CTR loop compiled with ``@njit`` on
  numpy arrays. Key schedules stay in pure Python (only ~16 keys per
  pak) and are memoized per key; the JIT'd loop handles every block.
* ``cupy`` — the same ECB ported to a CUDA RawKernel; one thread per
  16-byte block, one launch per buffer. Key schedules are computed on
  the host and uploaded as uint32 arrays.

Backend selection (first match wins):

* explicit choice via :func:`set_backend` (also used by the CLI
  ``--crypto`` argument of ``unpack_crysis.py``);
* env var ``CRISTICAL_CRYPTO_BACKEND`` = ``auto``|``python``|``numba``|``cupy``;
* ``auto`` (default): cupy -> numba -> python — GPU first, then JIT,
  then the reference — whichever imports and passes the known-answer
  self-test.

The effective backend is reported once through ``stderr`` (and is
queryable via :func:`get_backend`) on first use, so verbose runs show
which mode is actually working.

Every accelerated backend must reproduce the reference byte-for-byte.
The self-test (KAT) runs once per backend on first use; on any mismatch
or runtime error the backend is disabled with a single warning and the
call falls back to the reference implementation. ``python`` is always
available and never validated (it *is* the reference).

The accelerated paths only implement the 128-bit-key case (``_k == 2``),
which is all Crysis 3 paks use; other key sizes go to the reference.
"""

from __future__ import annotations

import os
import struct
import sys
import warnings
from typing import Callable

__all__ = [
    "set_backend", "get_backend", "list_backends",
    "make_schedule", "ctr_decrypt",
]

_BACKENDS = ("python", "numba", "cupy")

# Selection state. "_active" is the *effective* backend after probing;
# "_requested" is what the user asked for (may fail to import).
_requested: str | None = None
_active: str | None = None
_available: dict[str, bool] = {}

# Memoized key schedules: key bytes -> (subkeys list[int], sboxes list[list[int]])
_SCHEDULE_CACHE: dict[bytes, tuple[list[int], list[list[int]]]] = {}

# Validated-backend flags (KAT passed). None = not tried yet.
_validated: dict[str, bool] = {}


def _warn_once(msg: str) -> None:
    warnings.warn(msg, RuntimeWarning, stacklevel=3)


def _verbose(msg: str) -> None:
    """One-time status line to stderr (safe for MCP stdio transports)."""
    try:
        sys.stderr.write("[crypto] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Key schedule (pure Python, memoized — shared by every backend)
# --------------------------------------------------------------------------

def make_schedule(key: bytes):
    """Return ``(twofish_instance, subkeys, sboxes)`` for ``key``, memoized.

    ``twofish_instance`` is a :class:`cristical_core.crypak._Twofish` used
    by the python backend; ``subkeys`` (40 uint32) and ``sboxes``
    (4x256 uint32) are precomputed flat lists for the accelerated kernels.
    """
    from cristical_core.crypak import _Twofish
    cached = _SCHEDULE_CACHE.get(key)
    if cached is not None:
        tf = _Twofish(key)
        tf._subkeys, tf._sboxes = cached[0], cached[1]
        return tf, cached[0], cached[1]
    tf = _Twofish(key)
    _SCHEDULE_CACHE[key] = (tf._subkeys, tf._sboxes)
    return tf, tf._subkeys, tf._sboxes


# --------------------------------------------------------------------------
# Numba backend
# --------------------------------------------------------------------------

_NUMBA_SRC = '''
import numpy as np
from numba import njit


@njit
def _rol32(x, n):
    return ((np.uint32(x) << np.uint32(n)) | (np.uint32(x) >> np.uint32(32 - n)))


@njit
def _ror32(x, n):
    return ((np.uint32(x) >> np.uint32(n)) | (np.uint32(x) << np.uint32(32 - n)))


@njit
def _g(sboxes, x):
    x = np.uint32(x)
    return (np.uint32(sboxes[0, x & np.uint32(0xFF)])
            ^ np.uint32(sboxes[1, (x >> np.uint32(8)) & np.uint32(0xFF)])
            ^ np.uint32(sboxes[2, (x >> np.uint32(16)) & np.uint32(0xFF)])
            ^ np.uint32(sboxes[3, (x >> np.uint32(24)) & np.uint32(0xFF)]))


@njit
def _g1(sboxes, x):
    x = np.uint32(x)
    return (np.uint32(sboxes[1, x & np.uint32(0xFF)])
            ^ np.uint32(sboxes[2, (x >> np.uint32(8)) & np.uint32(0xFF)])
            ^ np.uint32(sboxes[3, (x >> np.uint32(16)) & np.uint32(0xFF)])
            ^ np.uint32(sboxes[0, (x >> np.uint32(24)) & np.uint32(0xFF)]))


@njit
def _ecb_encrypt_block(sboxes, k, a, b, c, d):
    # Twofish ECB on words a,b,c,d (uint32). Returns (c,d,a,b) post-whitening.
    idx = 8
    for _ in range(8):
        t2 = _g1(sboxes, b)
        t1 = np.uint32(_g(sboxes, a) + t2)
        c = _ror32(np.uint32(c ^ np.uint32(t1 + np.uint32(k[idx]))), np.uint32(1))
        d = np.uint32(_rol32(d, np.uint32(1)) ^ np.uint32(t2 + np.uint32(t1) + np.uint32(k[idx + 1])))
        t2 = _g1(sboxes, d)
        t1 = np.uint32(_g(sboxes, c) + t2)
        a = _ror32(np.uint32(a ^ np.uint32(t1 + np.uint32(k[idx + 2]))), np.uint32(1))
        b = np.uint32(_rol32(b, np.uint32(1)) ^ np.uint32(t2 + np.uint32(t1) + np.uint32(k[idx + 3])))
        idx += 4
    return (np.uint32(c ^ np.uint32(k[4])), np.uint32(d ^ np.uint32(k[5])),
            np.uint32(a ^ np.uint32(k[6])), np.uint32(b ^ np.uint32(k[7])))


@njit
def _ctr_numba(data, out, k, sboxes, iv):
    n = data.shape[0]
    nblocks = (n + 15) >> 4
    ctr = np.empty(16, dtype=np.uint8)
    for i in range(16):
        ctr[i] = iv[i]
    ks = np.empty(16, dtype=np.uint8)
    for blk in range(nblocks):
        # ECB-encrypt the current counter block.
        a = np.uint32(np.uint32(ctr[0]) | (np.uint32(ctr[1]) << np.uint32(8))
                      | (np.uint32(ctr[2]) << np.uint32(16)) | (np.uint32(ctr[3]) << np.uint32(24)))
        a = np.uint32(a ^ np.uint32(k[0]))
        b = np.uint32(np.uint32(ctr[4]) | (np.uint32(ctr[5]) << np.uint32(8))
                      | (np.uint32(ctr[6]) << np.uint32(16)) | (np.uint32(ctr[7]) << np.uint32(24)))
        b = np.uint32(b ^ np.uint32(k[1]))
        c = np.uint32(np.uint32(ctr[8]) | (np.uint32(ctr[9]) << np.uint32(8))
                      | (np.uint32(ctr[10]) << np.uint32(16)) | (np.uint32(ctr[11]) << np.uint32(24)))
        c = np.uint32(c ^ np.uint32(k[2]))
        d = np.uint32(np.uint32(ctr[12]) | (np.uint32(ctr[13]) << np.uint32(8))
                      | (np.uint32(ctr[14]) << np.uint32(16)) | (np.uint32(ctr[15]) << np.uint32(24)))
        d = np.uint32(d ^ np.uint32(k[3]))
        c, d, a, b = _ecb_encrypt_block(sboxes, k, a, b, c, d)
        # XOR keystream with data (handles the final partial block).
        base = blk << 4
        end = n - base
        if end > 16:
            end = 16
        ks[0] = (c & np.uint32(0xFF))
        ks[1] = ((c >> np.uint32(8)) & np.uint32(0xFF))
        ks[2] = ((c >> np.uint32(16)) & np.uint32(0xFF))
        ks[3] = ((c >> np.uint32(24)) & np.uint32(0xFF))
        ks[4] = (d & np.uint32(0xFF))
        ks[5] = ((d >> np.uint32(8)) & np.uint32(0xFF))
        ks[6] = ((d >> np.uint32(16)) & np.uint32(0xFF))
        ks[7] = ((d >> np.uint32(24)) & np.uint32(0xFF))
        ks[8] = (a & np.uint32(0xFF))
        ks[9] = ((a >> np.uint32(8)) & np.uint32(0xFF))
        ks[10] = ((a >> np.uint32(16)) & np.uint32(0xFF))
        ks[11] = ((a >> np.uint32(24)) & np.uint32(0xFF))
        ks[12] = (b & np.uint32(0xFF))
        ks[13] = ((b >> np.uint32(8)) & np.uint32(0xFF))
        ks[14] = ((b >> np.uint32(16)) & np.uint32(0xFF))
        ks[15] = ((b >> np.uint32(24)) & np.uint32(0xFF))
        for i in range(end):
            out[base + i] = data[base + i] ^ ks[i]
        # Increment the counter (little-endian over 16 bytes).
        for i in range(16):
            new_val = np.uint8(ctr[i] + np.uint8(1))
            ctr[i] = new_val
            if new_val != np.uint8(0):
                break
    return out
'''

_numba_impl: dict[str, object] = {}


def _load_numba():
    """Import numba and compile the kernel; returns kernel namespace dict."""
    if _numba_impl:
        return _numba_impl
    import numba  # noqa: F401  (probe import)
    import numpy as np
    ns: dict[str, object] = {}
    exec(compile(_NUMBA_SRC, "<twofish_fast_numba>", "exec"), ns)
    # Force compilation now (not lazily at first decrypt).
    ns["_ctr_numba"](np.zeros(16, dtype=np.uint8), np.zeros(16, dtype=np.uint8),
                     np.zeros(40, dtype=np.uint32), np.zeros((4, 256), dtype=np.uint32),
                     np.zeros(16, dtype=np.uint8))
    _numba_impl.update(ns)
    return _numba_impl


def _ctr_numba_impl(data: bytes, key: bytes, iv: bytes) -> bytes:
    import numpy as np
    ns = _load_numba()
    _tf, subkeys, sboxes = make_schedule(key)
    k = np.asarray(subkeys, dtype=np.uint32)
    sb = np.asarray(sboxes, dtype=np.uint32)
    src = np.frombuffer(data, dtype=np.uint8)
    out = np.empty_like(src)
    iv_arr = np.frombuffer(iv, dtype=np.uint8)
    ns["_ctr_numba"](src, out, k, sb, iv_arr)
    return out.tobytes()


# --------------------------------------------------------------------------
# CuPy (GPU) backend
# --------------------------------------------------------------------------

_CUPY_KERNEL_SRC = r'''
extern "C" {

__device__ __forceinline__ unsigned int rol32(unsigned int x, unsigned int n) {
    return (x << n) | (x >> (32u - n));
}

__device__ __forceinline__ unsigned int ror32(unsigned int x, unsigned int n) {
    return (x >> n) | (x << (32u - n));
}

__device__ __forceinline__ unsigned int g(const unsigned int* sboxes, unsigned int x) {
    return sboxes[(x & 0xFFu)]
         ^ sboxes[256u + ((x >> 8) & 0xFFu)]
         ^ sboxes[512u + ((x >> 16) & 0xFFu)]
         ^ sboxes[768u + ((x >> 24) & 0xFFu)];
}

__device__ __forceinline__ unsigned int g1(const unsigned int* sboxes, unsigned int x) {
    return sboxes[256u + (x & 0xFFu)]
         ^ sboxes[512u + ((x >> 8) & 0xFFu)]
         ^ sboxes[768u + ((x >> 16) & 0xFFu)]
         ^ sboxes[(x >> 24) & 0xFFu];
}

// One thread decrypts one 16-byte block (CTR). The keystream word layout
// matches crypak._Twofish.ecb_encrypt: out words are (c,d,a,b) after
// whitening; XOR order a,b,c,d -> bytes 0..15 of the keystream.
__global__ void tf_ctr_decrypt(const unsigned char* __restrict__ data,
                               unsigned char* __restrict__ out,
                               const unsigned int* __restrict__ k,
                               const unsigned int* __restrict__ sboxes,
                               const unsigned char* __restrict__ iv,
                               long long nbytes) {
    long long blk = (long long)blockIdx.x * (long long)blockDim.x + threadIdx.x;
    long long nblocks = (nbytes + 15) >> 4;
    if (blk >= nblocks) return;

    // counter = iv + blk (little-endian 128-bit add; blk fits 64-bit).
    unsigned int w0 = (unsigned int)iv[0] | ((unsigned int)iv[1] << 8)
                    | ((unsigned int)iv[2] << 16) | ((unsigned int)iv[3] << 24);
    unsigned int w1 = (unsigned int)iv[4] | ((unsigned int)iv[5] << 8)
                    | ((unsigned int)iv[6] << 16) | ((unsigned int)iv[7] << 24);
    unsigned int w2 = (unsigned int)iv[8] | ((unsigned int)iv[9] << 8)
                    | ((unsigned int)iv[10] << 16) | ((unsigned int)iv[11] << 24);
    unsigned int w3 = (unsigned int)iv[12] | ((unsigned int)iv[13] << 8)
                    | ((unsigned int)iv[14] << 16) | ((unsigned int)iv[15] << 24);
    unsigned long long lo = (unsigned long long)w0
        + (unsigned long long)((unsigned long long)blk & 0xFFFFFFFFULL);
    w0 = (unsigned int)(lo & 0xFFFFFFFFULL);
    unsigned long long hi = (unsigned long long)w1
        + (unsigned long long)((unsigned long long)blk >> 32)
        + (lo >> 32);
    w1 = (unsigned int)(hi & 0xFFFFFFFFULL);
    unsigned long long c2 = (unsigned long long)w2 + (hi >> 32);
    w2 = (unsigned int)(c2 & 0xFFFFFFFFULL);
    if (c2 >> 32) {
        w3 += 1u;  // wraps naturally
    }
    unsigned char ctr[16];
    #pragma unroll
    for (int i = 0; i < 4; ++i) {
        ctr[i] = (unsigned char)((w0 >> (8 * i)) & 0xFFu);
        ctr[4 + i] = (unsigned char)((w1 >> (8 * i)) & 0xFFu);
        ctr[8 + i] = (unsigned char)((w2 >> (8 * i)) & 0xFFu);
        ctr[12 + i] = (unsigned char)((w3 >> (8 * i)) & 0xFFu);
    }

    unsigned int a = ((unsigned int)ctr[0] | ((unsigned int)ctr[1] << 8)
                      | ((unsigned int)ctr[2] << 16) | ((unsigned int)ctr[3] << 24)) ^ k[0];
    unsigned int b = ((unsigned int)ctr[4] | ((unsigned int)ctr[5] << 8)
                      | ((unsigned int)ctr[6] << 16) | ((unsigned int)ctr[7] << 24)) ^ k[1];
    unsigned int c = ((unsigned int)ctr[8] | ((unsigned int)ctr[9] << 8)
                      | ((unsigned int)ctr[10] << 16) | ((unsigned int)ctr[11] << 24)) ^ k[2];
    unsigned int d = ((unsigned int)ctr[12] | ((unsigned int)ctr[13] << 8)
                      | ((unsigned int)ctr[14] << 16) | ((unsigned int)ctr[15] << 24)) ^ k[3];

    int idx = 8;
    #pragma unroll
    for (int r = 0; r < 8; ++r) {
        unsigned int t2 = g1(sboxes, b);
        unsigned int t1 = g(sboxes, a) + t2;
        c = ror32(c ^ (t1 + k[idx]), 1);
        d = rol32(d, 1) ^ (t2 + t1 + k[idx + 1]);
        t2 = g1(sboxes, d);
        t1 = g(sboxes, c) + t2;
        a = ror32(a ^ (t1 + k[idx + 2]), 1);
        b = rol32(b, 1) ^ (t2 + t1 + k[idx + 3]);
        idx += 4;
    }
    c ^= k[4]; d ^= k[5]; a ^= k[6]; b ^= k[7];

    unsigned int ks[4] = {c, d, a, b};
    long long base = blk << 4;
    int end = (int)(nbytes - base);
    if (end > 16) end = 16;
    for (int i = 0; i < end; ++i) {
        out[base + i] = data[base + i] ^ (unsigned char)((ks[i >> 2] >> (8 * (i & 3))) & 0xFFu);
    }
}

}  // extern "C"
'''

_cupy_impl: dict[str, object] = {}


def _load_cupy():
    """Import cupy, build the RawKernel; returns kernel namespace dict."""
    if _cupy_impl:
        return _cupy_impl
    import numpy as np
    import cupy as cp
    kernel = cp.RawKernel(_CUPY_KERNEL_SRC, "tf_ctr_decrypt")
    _cupy_impl.update({"cp": cp, "np": np, "kernel": kernel})
    return _cupy_impl


def _ctr_cupy_impl(data: bytes, key: bytes, iv: bytes) -> bytes:
    ns = _load_cupy()
    cp, np, kernel = ns["cp"], ns["np"], ns["kernel"]
    _tf, subkeys, sboxes = make_schedule(key)
    nbytes = len(data)
    if nbytes == 0:
        return b""
    d_data = cp.frombuffer(data, dtype=cp.uint8)  # type: ignore[attr-defined]
    d_out = cp.empty_like(d_data)
    d_k = cp.asarray(np.asarray(subkeys, dtype=np.uint32))
    d_sb = cp.asarray(np.asarray(sboxes, dtype=np.uint32))
    d_iv = cp.asarray(np.frombuffer(iv, dtype=np.uint8))
    nblocks = (nbytes + 15) >> 4
    threads = 256
    grid = (int((nblocks + threads - 1) // threads), 1, 1)
    kernel(grid, (threads,), (d_data, d_out, d_k, d_sb, d_iv, nbytes))
    return cp.asnumpy(d_out).tobytes()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Reference (python) backend — reuse crypak._twofish_ctr verbatim
# --------------------------------------------------------------------------

def _ctr_python_impl(data: bytes, key: bytes, iv: bytes) -> bytes:
    tf, _sk, _sb = make_schedule(key)
    out = bytearray()
    counter = bytearray(iv)
    for off in range(0, len(data), 16):
        block = tf.ecb_encrypt(bytes(counter))
        chunk = data[off:off + 16]
        out += bytes(x ^ y for x, y in zip(block, chunk))
        for i in range(16):
            counter[i] = (counter[i] + 1) & 0xFF
            if counter[i] != 0:
                break
    return bytes(out)


_IMPLS: dict[str, Callable[[bytes, bytes, bytes], bytes]] = {
    "python": _ctr_python_impl,
    "numba": _ctr_numba_impl,
    "cupy": _ctr_cupy_impl,
}


# --------------------------------------------------------------------------
# Known-answer self-test (KAT) — validates a backend against the reference
# --------------------------------------------------------------------------

def _kat_vector() -> tuple[bytes, bytes, bytes]:
    """Deterministic test vector (data, key, iv) exercising multi-block CTR,
    partial-block tail and counter carry."""
    key = bytes(range(0x10))
    iv = bytes([0xFF] * 15 + [0xF0])  # forces carry into byte 15 on block 1
    data = bytes(((i * 7 + 3) & 0xFF) for i in range(16 * 3 + 5))  # 53 bytes
    return data, key, iv


def _validate_backend(name: str) -> bool:
    """Run the KAT: backend output must equal the reference byte-for-byte."""
    if _validated.get(name) is not None:
        return _validated[name]
    data, key, iv = _kat_vector()
    expected = _ctr_python_impl(data, key, iv)
    try:
        got = _IMPLS[name](data, key, iv)
        ok = got == expected
    except Exception as e:  # pragma: no cover — depends on machine
        _warn_once("twofish backend %r failed self-test: %s; falling back" % (name, e))
        ok = False
    _validated[name] = ok
    if not ok and _validated.get(name) is False and "expected" in locals():
        _warn_once("twofish backend %r KAT mismatch; using python" % name)
    return ok


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def set_backend(name: str) -> str:
    """Select the crypto backend explicitly.

    ``name``: ``auto`` | ``python`` | ``numba`` | ``cupy``. For ``auto``
    the module probes numba -> cupy -> python at first use. Explicit
    names that fail to import or pass the KAT emit one warning and fall
    back to ``python``.
    Returns the *effective* backend.
    """
    global _requested, _active
    name = (name or "auto").strip().lower()
    if name not in ("auto",) + _BACKENDS:
        raise ValueError("backend must be one of auto|python|numba|cupy, got %r" % name)
    _requested = name
    _active = None  # force re-probe on next use
    if name == "auto":
        return name
    if _ensure_backend(name):
        _active = name
        return name
    _active = "python"
    return "python"


def get_backend() -> str:
    """The effective backend name (probes on first call when auto)."""
    global _requested, _active
    if _active is not None:
        return _active
    if _requested is None:
        env = os.environ.get("CRISTICAL_CRYPTO_BACKEND", "auto").strip().lower()
        _requested = env if env in ("auto",) + _BACKENDS else "auto"
    if _requested != "auto":
        if _ensure_backend(_requested):
            _active = _requested
        else:
            _active = "python"
        _verbose("backend requested %r -> effective %r"
                 % (_requested, _active))
        return _active
    # auto: GPU first, then JIT, then the reference implementation.
    for cand in ("cupy", "numba"):
        if _ensure_backend(cand):
            _active = cand
            _verbose("backend auto: using %r" % _active)
            return _active
    _active = "python"
    _verbose("backend auto: no accelerator available, using %r" % _active)
    return _active


def _ensure_backend(name: str) -> bool:
    """True if backend is importable and KAT-validated (python always True)."""
    if name == "python":
        return True
    if _available.get(name) is None:
        try:
            if name == "numba":
                _load_numba()
            elif name == "cupy":
                _load_cupy()
            _available[name] = True
        except Exception as e:
            _available[name] = False
            _warn_once("twofish backend %r unavailable (%s)" % (name, e))
    if not _available.get(name, False):
        return False
    return _validate_backend(name)


def list_backends() -> dict[str, bool]:
    """Probe every backend without selecting one: {name: usable}."""
    return {name: _ensure_backend(name) for name in _BACKENDS}


def ctr_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Drop-in accelerated replacement for crypak._twofish_ctr."""
    backend = get_backend()
    if backend == "python" or len(key) != 16:
        # python path reuses the memoized _Twofish instance (skips the
        # per-file key-schedule rebuild); other key sizes are reference-only.
        return _ctr_python_impl(data, key, iv)
    try:
        return _IMPLS[backend](data, key, iv)
    except Exception as e:
        _warn_once("twofish backend %r failed mid-run (%s); falling back to python"
                   % (backend, e))
        global _active
        _active = "python"
        return _ctr_python_impl(data, key, iv)


if __name__ == "__main__":  # pragma: no cover — manual smoke test
    print("backends:", list_backends())
    print("selected:", get_backend())
    data, key, iv = _kat_vector()
    out = ctr_decrypt(data, key, iv)
    ref = _ctr_python_impl(data, key, iv)
    print("KAT:", "PASS" if out == ref else "FAIL")
