#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
crypak.py — CrisTical: encrypted CryEngine .pak support (Crysis 2/3)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.1

Reads the encrypted ``.pak`` archives studied while working out how the
engine stores game data:

Crysis 3 ships its game data as ZIP-format ``.pak`` files whose central
directory and per-file payloads are encrypted with Twofish-128 in CTR
mode.  The per-pak Twofish keys and IV are themselves wrapped in
RSA-OAEP blobs and recovered with the embedded Crysis 3 RSA *public*
key (the game "signed" the OAEP block with its private key, so the
public operation restores the plaintext).

Crysis 2 (encmode 2) is much simpler: both the central directory and
``METHOD_DEFLATE_AND_ENCRYPT`` (11) payloads are XXTEA ("btea") blocks
encrypted with one hardcoded key, byte-swapped per 32-bit word before
and after the cipher.  There is no extension header (EOCD comment
length is 0) and no RSA/Twofish machinery.  The engine never reads
local file headers of encrypted paks: file data starts at
``localHeaderOffset + 30 + nameLen`` (no extra field).

Layout recap (see docs/internal/recon/recon-task2-crypak-c2.md):

* EOCD is plaintext; ``nDisk`` high 2 bits carry a fallback encmode.
* The EOCD "comment" area holds the Crytek extension:
  ``SExtensionHeader`` (8B) + optional ``SSignatureHeader`` (132B) +
  optional ``SEncryptionHeader`` (2180B, Twofish only).  Crysis 2
  paks carry no comment at all.
* ``SExtensionHeader.EncryptionType``: 0=None, 1=StreamCipher,
  2=XXTEA, 3=Twofish.  This module implements 3 (Twofish),
  2 (XXTEA - Crysis 2) and 0 (plain passthrough).
* Central directory is encrypted with Twofish-CTR using ``Keys[0]``
  and the RSA-unwrapped global IV (mode 3), or with XXTEA (mode 2).
* Per-file payloads use key ``Keys[~(crc32>>2) & 0xF]`` and an IV
  derived from the entry's own ``DataDescriptor`` (compressed size,
  uncompressed size, crc32) in mode 3; in mode 2 method 11 payloads
  are XXTEA-encrypted with the same hardcoded key.
* Compression methods: 11 = XXTEA + raw DEFLATE (Crysis 2),
  13 = Twofish + stored, 14 = Twofish + raw DEFLATE (``zlib`` with
  ``wbits=-15``).

The public API is :class:`CryPakFileSystem`, an ``IPackFileSystem``
backing (mirrors :class:`cristical_core.cryvfs.ZipFileSystem`) that
decrypts on demand.
"""

from __future__ import annotations

import hashlib
import io
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

__all__ = ["CryPakFileSystem", "open_cry_pak"]


# --------------------------------------------------------------------------
# Twofish (128-bit key, 16-byte block) - standard Twofish cipher
# --------------------------------------------------------------------------

_MDS_POLY = 0x169
_RS_POLY = 0x14D

_RS = [
    [0x01, 0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E],
    [0xA4, 0x56, 0x82, 0xF3, 0x1E, 0xC6, 0x68, 0xE5],
    [0x02, 0xA1, 0xFC, 0xC1, 0x47, 0xAE, 0x3D, 0x19],
    [0xA4, 0x55, 0x87, 0x5A, 0x58, 0xDB, 0x9E, 0x03],
]

_QBOX = [
    [
        [0x8, 0x1, 0x7, 0xD, 0x6, 0xF, 0x3, 0x2, 0x0, 0xB, 0x5, 0x9, 0xE, 0xC, 0xA, 0x4],
        [0xE, 0xC, 0xB, 0x8, 0x1, 0x2, 0x3, 0x5, 0xF, 0x4, 0xA, 0x6, 0x7, 0x0, 0x9, 0xD],
        [0xB, 0xA, 0x5, 0xE, 0x6, 0xD, 0x9, 0x0, 0xC, 0x8, 0xF, 0x3, 0x2, 0x4, 0x7, 0x1],
        [0xD, 0x7, 0xF, 0x4, 0x1, 0x2, 0x6, 0xE, 0x9, 0xB, 0x3, 0x0, 0x8, 0x5, 0xC, 0xA],
    ],
    [
        [0x2, 0x8, 0xB, 0xD, 0xF, 0x7, 0x6, 0xE, 0x3, 0x1, 0x9, 0x4, 0x0, 0xA, 0xC, 0x5],
        [0x1, 0xE, 0x2, 0xB, 0x4, 0xC, 0x3, 0x7, 0x6, 0xD, 0xA, 0x5, 0xF, 0x9, 0x0, 0x8],
        [0x4, 0xC, 0x7, 0x5, 0x1, 0x6, 0x9, 0xA, 0x0, 0xE, 0xD, 0x8, 0x2, 0xB, 0x3, 0xF],
        [0xB, 0x9, 0x5, 0x1, 0xC, 0x3, 0xD, 0xE, 0x6, 0x4, 0x7, 0xF, 0x2, 0x0, 0x8, 0xA],
    ],
]


def _rol(x: int, n: int) -> int:
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _ror(x: int, n: int) -> int:
    return ((x >> n) | (x << (32 - n))) & 0xFFFFFFFF


def _gf_mult(a: int, b: int, p: int) -> int:
    result = 0
    while a:
        if a & 1:
            result ^= b
        a >>= 1
        b <<= 1
        if b & 0x100:
            b ^= p
    return result


def _sbox(i: int, x: int) -> int:
    a0 = (x >> 4) & 15
    b0 = x & 15
    a1 = a0 ^ b0
    b1 = (a0 ^ ((b0 << 3) | (b0 >> 1)) ^ (a0 << 3)) & 15
    a2 = _QBOX[i][0][a1]
    b2 = _QBOX[i][1][b1]
    a3 = a2 ^ b2
    b3 = (a2 ^ ((b2 << 3) | (b2 >> 1)) ^ (a2 << 3)) & 15
    a4 = _QBOX[i][2][a3]
    b4 = _QBOX[i][3][b3]
    return (b4 << 4) + a4


def _mds_column_mult(in_val: int, col: int) -> int:
    x01 = in_val
    x5b = _gf_mult(in_val, 0x5B, _MDS_POLY)
    xef = _gf_mult(in_val, 0xEF, _MDS_POLY)
    t = [x01, x5b, xef]
    order = {0: [0, 1, 2, 2], 1: [2, 2, 1, 0], 2: [1, 2, 0, 2], 3: [1, 0, 2, 1]}[col]
    return t[order[0]] | (t[order[1]] << 8) | (t[order[2]] << 16) | (t[order[3]] << 24)


def _rs_mult(in_bytes: bytes) -> list[int]:
    out = []
    for x in range(4):
        v = 0
        for y in range(8):
            v ^= _gf_mult(in_bytes[y], _RS[x][y], _RS_POLY)
        out.append(v)
    return out


def _h_func(in_bytes: bytes, m: bytes, k: int, offset: int) -> list[int]:
    y = list(in_bytes)
    if k >= 4:
        y[0] = _sbox(1, y[0]) ^ m[4 * (6 + offset) + 0]
        y[1] = _sbox(0, y[1]) ^ m[4 * (6 + offset) + 1]
        y[2] = _sbox(0, y[2]) ^ m[4 * (6 + offset) + 2]
        y[3] = _sbox(1, y[3]) ^ m[4 * (6 + offset) + 3]
    if k >= 3:
        y[0] = _sbox(1, y[0]) ^ m[4 * (4 + offset) + 0]
        y[1] = _sbox(1, y[1]) ^ m[4 * (4 + offset) + 1]
        y[2] = _sbox(0, y[2]) ^ m[4 * (4 + offset) + 2]
        y[3] = _sbox(0, y[3]) ^ m[4 * (4 + offset) + 3]
    if k >= 2:
        y[0] = _sbox(1, _sbox(0, _sbox(0, y[0]) ^ m[4 * (2 + offset) + 0]) ^ m[4 * (0 + offset) + 0])
        y[1] = _sbox(0, _sbox(0, _sbox(1, y[1]) ^ m[4 * (2 + offset) + 1]) ^ m[4 * (0 + offset) + 1])
        y[2] = _sbox(1, _sbox(1, _sbox(0, y[2]) ^ m[4 * (2 + offset) + 2]) ^ m[4 * (0 + offset) + 2])
        y[3] = _sbox(0, _sbox(1, _sbox(1, y[3]) ^ m[4 * (2 + offset) + 3]) ^ m[4 * (0 + offset) + 3])
    tmp = 0
    for col in range(4):
        tmp ^= _mds_column_mult(y[col], col)
    return [tmp & 0xFF, (tmp >> 8) & 0xFF, (tmp >> 16) & 0xFF, (tmp >> 24) & 0xFF]


class _Twofish:
    """Twofish block cipher (key sizes 16/24/32 bytes)."""

    def __init__(self, key: bytes) -> None:
        keylen = len(key)
        if keylen not in (16, 24, 32):
            raise ValueError("Twofish key must be 16/24/32 bytes")
        self._k = keylen // 8
        m = key
        s: list[int] = []
        for x in range(self._k):
            s.extend(_rs_mult(m[x * 8:(x + 1) * 8]))
        subkeys: list[int] = []
        for x in range(20):
            tmp = bytes([x + x]) * 4
            a = int.from_bytes(bytes(_h_func(tmp, m, self._k, 0)), "little")
            tmp = bytes([x + x + 1]) * 4
            b = _rol(int.from_bytes(bytes(_h_func(tmp, m, self._k, 1)), "little"), 8)
            subkeys.append((a + b) & 0xFFFFFFFF)
            subkeys.append(_rol((b + b + a) & 0xFFFFFFFF, 9))
        self._subkeys = subkeys
        self._sboxes = self._make_sboxes(s)

    def _make_sboxes(self, s: list[int]) -> list[list[int]]:
        sb = [[0] * 256 for _ in range(4)]
        k = self._k
        for x in range(256):
            tmpx0 = _sbox(0, x)
            tmpx1 = _sbox(1, x)
            if k == 2:
                sb[0][x] = _mds_column_mult(_sbox(1, (_sbox(0, tmpx0 ^ s[0]) ^ s[4])), 0)
                sb[1][x] = _mds_column_mult(_sbox(0, (_sbox(0, tmpx1 ^ s[1]) ^ s[5])), 1)
                sb[2][x] = _mds_column_mult(_sbox(1, (_sbox(1, tmpx0 ^ s[2]) ^ s[6])), 2)
                sb[3][x] = _mds_column_mult(_sbox(0, (_sbox(1, tmpx1 ^ s[3]) ^ s[7])), 3)
            elif k == 3:
                sb[0][x] = _mds_column_mult(_sbox(1, (_sbox(0, _sbox(0, tmpx1 ^ s[0]) ^ s[4]) ^ s[8])), 0)
                sb[1][x] = _mds_column_mult(_sbox(0, (_sbox(0, _sbox(1, tmpx1 ^ s[1]) ^ s[5]) ^ s[9])), 1)
                sb[2][x] = _mds_column_mult(_sbox(1, (_sbox(1, _sbox(0, tmpx0 ^ s[2]) ^ s[6]) ^ s[10])), 2)
                sb[3][x] = _mds_column_mult(_sbox(0, (_sbox(1, _sbox(1, tmpx0 ^ s[3]) ^ s[7]) ^ s[11])), 3)
            else:
                sb[0][x] = _mds_column_mult(_sbox(1, (_sbox(0, _sbox(0, _sbox(1, tmpx1 ^ s[0]) ^ s[4]) ^ s[8]) ^ s[12])), 0)
                sb[1][x] = _mds_column_mult(_sbox(0, (_sbox(0, _sbox(1, _sbox(1, tmpx0 ^ s[1]) ^ s[5]) ^ s[9]) ^ s[13])), 1)
                sb[2][x] = _mds_column_mult(_sbox(1, (_sbox(1, _sbox(0, _sbox(0, tmpx0 ^ s[2]) ^ s[6]) ^ s[10]) ^ s[14])), 2)
                sb[3][x] = _mds_column_mult(_sbox(0, (_sbox(1, _sbox(1, _sbox(0, tmpx1 ^ s[3]) ^ s[7]) ^ s[11]) ^ s[15])), 3)
        return sb

    def _g(self, x: int) -> int:
        s = self._sboxes
        return (s[0][x & 0xFF] ^ s[1][(x >> 8) & 0xFF] ^ s[2][(x >> 16) & 0xFF] ^ s[3][(x >> 24) & 0xFF]) & 0xFFFFFFFF

    def _g1(self, x: int) -> int:
        s = self._sboxes
        return (s[1][x & 0xFF] ^ s[2][(x >> 8) & 0xFF] ^ s[3][(x >> 16) & 0xFF] ^ s[0][(x >> 24) & 0xFF]) & 0xFFFFFFFF

    def ecb_encrypt(self, pt: bytes) -> bytes:
        k = self._subkeys
        a = int.from_bytes(pt[0:4], "little") ^ k[0]
        b = int.from_bytes(pt[4:8], "little") ^ k[1]
        c = int.from_bytes(pt[8:12], "little") ^ k[2]
        d = int.from_bytes(pt[12:16], "little") ^ k[3]
        idx = 8
        for _ in range(8):
            t2 = self._g1(b)
            t1 = (self._g(a) + t2) & 0xFFFFFFFF
            c = _ror(c ^ ((t1 + k[idx]) & 0xFFFFFFFF), 1)
            d = _rol(d, 1) ^ ((t2 + t1 + k[idx + 1]) & 0xFFFFFFFF)
            t2 = self._g1(d)
            t1 = (self._g(c) + t2) & 0xFFFFFFFF
            a = _ror(a ^ ((t1 + k[idx + 2]) & 0xFFFFFFFF), 1)
            b = _rol(b, 1) ^ ((t2 + t1 + k[idx + 3]) & 0xFFFFFFFF)
            idx += 4
        out = []
        for v in (c ^ k[4], d ^ k[5], a ^ k[6], b ^ k[7]):
            out.append(v.to_bytes(4, "little"))
        return b"".join(out)


def _twofish_ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Twofish-128 CTR decryption, little-endian 128-bit counter.

    Dispatches to the optional accelerated backends (Numba JIT / CuPy
    GPU) selected via ``CRISTICAL_CRYPTO_BACKEND`` or
    :func:`cristical_core.twofish_fast.set_backend`; falls back to this
    reference path. Accelerated backends are validated byte-for-byte
    against this implementation before first use.
    """
    from cristical_core.twofish_fast import ctr_decrypt
    return ctr_decrypt(data, key, iv)


def _twofish_ctr_reference(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Pure-Python reference (kept for testing / forced fallback)."""
    tf = _Twofish(key)
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


# --------------------------------------------------------------------------
# XXTEA (Crysis 2) - btea with per-word byte-swap, as the engine's pak
# reader applies it (decrypt path of the encmode-2 archives)
# --------------------------------------------------------------------------

_C2_XXTEA_KEY = (0xC968FB67, 0x8F9B4267, 0x85399E84, 0xF9B99DC4)
_XXTEA_DELTA = 0x9E3779B9
_MASK32 = 0xFFFFFFFF


def _swap_words(words: "list[int]") -> "list[int]":
    """Byte-swap each 32-bit word (SwapByteOrder / SwapDWORDBuffer)."""
    return [_MASK32 & (((w & 0xFF) << 24) | ((w & 0xFF00) << 8)
                       | ((w >> 8) & 0xFF00) | (w >> 24)) for w in words]


def _btea_decode(v: "list[int]", k: "tuple[int, ...]") -> "list[int]":
    """XXTEA decode branch of the reference btea (n < -1)."""
    n = len(v)
    if n < 2:
        return v
    rounds = 6 + 52 // n
    s = (rounds * _XXTEA_DELTA) & _MASK32
    y = v[0]
    while s:
        e = (s >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4))
                  ^ ((s ^ y) + (k[(p & 3) ^ e] ^ z))) & _MASK32
            y = (v[p] - mx) & _MASK32
            v[p] = y
        z = v[n - 1]
        mx = (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4))
              ^ ((s ^ y) + (k[0 ^ e] ^ z))) & _MASK32
        y = (v[0] - mx) & _MASK32
        v[0] = y
        s = (s - _XXTEA_DELTA) & _MASK32
    return v


def _xxtea_decrypt(data: bytes, key: "tuple[int, ...]" = _C2_XXTEA_KEY) -> bytes:
    """Engine pak decryption (encmode 2): only ``size >> 2`` words are
    processed; the buffer is byte-swapped per word before and after the
    btea decode (PC/console path without XENON/PS3).  Any trailing bytes
    beyond the last full word are returned unchanged."""
    n = len(data) >> 2
    if n == 0:
        return bytes(data)
    words = list(struct.unpack_from("<%dI" % n, data, 0))
    words = _swap_words(words)
    _btea_decode(words, key)
    words = _swap_words(words)
    return struct.pack("<%dI" % n, *words) + data[n * 4:]


# --------------------------------------------------------------------------
# RSA-OAEP (SHA-256) key unwrap (PKCS#1 v2.0 style)
# --------------------------------------------------------------------------

_CRYSIS3_RSA_DER = bytes.fromhex(
    "30819f300d06092a864886f70d010101050003818d0030818902818100"
    "a9d590a4bc92db8cf1fc5ad58f46055216eef3c3be86de701f4e2d18d3019246"
    "befaad66047b8cdd0d248da723ca52c8e501e0b72beb55cf0df79777dc11e87b"
    "18ccdb90072d9dc4ad807c50238546f3e92c5481117b6de257878e65e1d316c4"
    "54ed29ed51fdb1efe4950124aec06afae05b19d2e6f0223bc3e7dd171a8cf8e1"
    "0203010001"
)


def _mgf1(seed: bytes, mask_len: int) -> bytes:
    out = b""
    counter = 0
    while len(out) < mask_len:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:mask_len]


def _oaep_decode_sha256(msg: bytes, expected_len: int) -> bytes:
    """OAEP decode (SHA-256, empty label) of a 128-byte RSA block."""
    if len(msg) != 128 or msg[0] != 0x00:
        raise ValueError("bad OAEP block")
    h_len = 32
    seed = msg[1:1 + h_len]
    db = msg[1 + h_len:]
    seed = bytes(x ^ y for x, y in zip(seed, _mgf1(db, h_len)))
    db = bytes(x ^ y for x, y in zip(db, _mgf1(seed, len(db))))
    if db[:h_len] != hashlib.sha256(b"").digest():
        raise ValueError("OAEP lhash mismatch")
    i = h_len
    while i < len(db) and db[i] == 0x00:
        i += 1
    if i >= len(db) or db[i] != 0x01:
        raise ValueError("OAEP separator missing")
    out = db[i + 1:]
    if len(out) != expected_len:
        raise ValueError(f"OAEP outlen {len(out)} != {expected_len}")
    return out


def _rsa_unwrap_blob(blob: bytes, modulus: int, exponent: int, expected: int = 16) -> bytes:
    c = int.from_bytes(blob, "big")
    m = pow(c, exponent, modulus)
    return _oaep_decode_sha256(m.to_bytes(128, "big"), expected)


def _crysis3_rsa_public() -> tuple[int, int]:
    data = _CRYSIS3_RSA_DER
    # Fixed-layout SubjectPublicKeyInfo: modulus [29:157], exponent [159:162].
    modulus = int.from_bytes(data[29:157], "big")
    exponent = int.from_bytes(data[159:162], "big")
    return modulus, exponent


# --------------------------------------------------------------------------
# Crysis 3 pak parsing
# --------------------------------------------------------------------------

_CDR_FMT = "<IHHHHHHIIIHHHHHII"  # 46 bytes


@dataclass
class _CryPakEntry:
    method: int
    crc: int
    comp_size: int
    uncomp_size: int
    name: str
    local_header_offset: int
    name_len: int
    extra_len: int


class CryPakFileSystem:
    """Read-only view over an encrypted Crysis 3 ``.pak``.

    Backs the :class:`cristical_core.cryvfs.IPackFileSystem` interface so
    it can be dropped into the existing cascaded VFS in place of
    :class:`cristical_core.cryvfs.ZipFileSystem`.  Lookups are
    case-insensitive (CryEngine convention).  Payloads are decrypted and
    decompressed on demand.
    """

    def __init__(self, archive: str | Path) -> None:
        self._source = Path(archive).resolve()
        self._fh = self._source.open("rb")
        self._file_size = self._source.stat().st_size
        self._keys: list[bytes] = [b""] * 16
        self._global_iv = b""
        self._encmode = 0
        self._entries: dict[str, _CryPakEntry] = {}
        try:
            self._read_index()
        except BaseException:
            self._fh.close()
            raise

    @property
    def source(self) -> Path:
        return self._source

    def _read_at(self, offset: int, length: int) -> bytes:
        self._fh.seek(offset)
        return self._fh.read(length)

    def _read_index(self) -> None:
        size = self._file_size
        if size < 22:
            raise ValueError(f"not a pak archive: {self._source}")
        # EOCD lives in the last 64 KiB (matching the C tool's scan window).
        tail = self._read_at(max(0, size - 0x10016), min(size, 0x10016))
        base = max(0, size - 0x10016)
        eocd_pos = tail.rfind(b"PK\x05\x06")
        if eocd_pos < 0:
            raise ValueError(f"not a pak archive: {self._source}")
        (_, n_disk, _, _, num_entries, cdr_size, cdr_offset, comment_len) = struct.unpack_from(
            "<IHHHHIIH", tail, eocd_pos
        )
        eocd_off = base + eocd_pos
        if comment_len != size - eocd_off - 22:
            raise ValueError(f"bad EOCD comment length: {self._source}")

        # Earlier pak encryption techniques (Crysis 2) store the encryption
        # type in the top 2 bits of nDisk ((nDisk & 0xC000) >> 14,
        # accepted for TEA/StreamCipher values — engine pak layout).
        encmode = n_disk >> 14
        if encmode not in (0, 1, 2, 3):
            raise ValueError(f"bad EOCD disk number: {self._source}")
        n_disk &= 0x3FFF

        sig_type = 0
        ext_off = cdr_offset + cdr_size + 22
        if comment_len >= 8:
            esize, enc_type, sig_type = struct.unpack_from("<IHH", self._read_at(ext_off, 8), 0)
            if esize != 8:
                raise ValueError(f"bad extension header: {self._source}")
            encmode = enc_type
            total = 8
            sig_off = ext_off + 8
            if sig_type == 1:
                total += 132
            if enc_type == 3:
                total += 2180
            if comment_len != total:
                raise ValueError(f"comment length {comment_len} != extension {total}")

        if encmode == 3:
            enc_off = ext_off + 8 + (132 if sig_type == 1 else 0)
            modulus, exponent = _crysis3_rsa_public()
            enc = self._read_at(enc_off, 2180)
            iv_blob = enc[4:132]
            self._global_iv = _rsa_unwrap_blob(iv_blob, modulus, exponent)
            for i in range(16):
                blob = enc[132 + i * 128:132 + (i + 1) * 128]
                self._keys[i] = _rsa_unwrap_blob(blob, modulus, exponent)
            cdr = _twofish_ctr(self._read_at(cdr_offset, cdr_size), self._keys[0], self._global_iv)
        elif encmode == 2:
            # Crysis 2: XXTEA-encrypted central directory with the hardcoded
            # key (EOCD nDisk high bits; no comment/extension header).
            cdr = _xxtea_decrypt(self._read_at(cdr_offset, cdr_size))
        elif encmode == 0:
            # Plain zip - callers should have preferred a plain ZipFileSystem.
            raise ValueError(f"unsupported pak encryption mode {encmode}: {self._source}")
        else:
            raise ValueError(f"unsupported pak encryption mode {encmode}: {self._source}")
        self._encmode = encmode

        if cdr[:4] != b"PK\x01\x02":
            raise ValueError(f"central directory did not decrypt: {self._source}")

        off = 0
        while off + 46 <= len(cdr):
            (sig, _, _, _, method, _, _, crc, comp, uncomp, name_len, extra_len,
             comment_len2, _, _, _, lho) = struct.unpack_from(_CDR_FMT, cdr, off)
            if sig != 0x02014B50:
                break
            name = cdr[off + 46:off + 46 + name_len].decode("latin1")
            if not name.endswith("/"):
                norm = name.replace("\\", "/").lower()
                self._entries[norm] = _CryPakEntry(
                    method=method, crc=crc, comp_size=comp, uncomp_size=uncomp,
                    name=name, local_header_offset=lho, name_len=name_len,
                    extra_len=extra_len,
                )
            off += 46 + name_len + extra_len + comment_len2

    def _normalize(self, path: str) -> str:
        return path.replace("\\", "/").lstrip("/").lower()

    def _entry(self, path: str) -> _CryPakEntry | None:
        return self._entries.get(self._normalize(path))

    def _read_payload(self, entry: _CryPakEntry) -> bytes:
        if self._encmode == 2:
            # Crysis 2: the engine never parses local file headers of
            # encrypted paks - data starts right after the 30-byte header
            # plus the file name (no extra field is written by the packer).
            base = entry.local_header_offset + 30 + entry.name_len
        else:
            base = entry.local_header_offset + 30 + entry.name_len + entry.extra_len
        payload = self._read_at(base, entry.comp_size)
        if self._encmode == 2 and entry.method == 11:
            # METHOD_DEFLATE_AND_ENCRYPT: XXTEA with the same hardcoded
            # key, then raw DEFLATE.
            payload = _xxtea_decrypt(payload)
            payload = zlib.decompress(payload, -15)
        elif entry.method in (13, 14):
            key = self._keys[(~(entry.crc >> 2)) & 0xF]
            iv = bytearray(16)
            iv[0:4] = struct.pack("<I", ((entry.comp_size << 12) & 0xFFFFFFFF) ^ entry.uncomp_size)
            iv[4:8] = struct.pack("<I", 1 if entry.comp_size == 0 else 0)
            iv[8:12] = struct.pack("<I", ((entry.comp_size << 12) & 0xFFFFFFFF) ^ entry.crc)
            iv[12:16] = struct.pack("<I", entry.comp_size ^ (1 if entry.uncomp_size == 0 else 0))
            payload = _twofish_ctr(payload, key, bytes(iv))
            if entry.method == 14:
                payload = zlib.decompress(payload, -15)
        elif entry.method == 8:
            payload = zlib.decompress(payload, -15)
        return payload

    # -- IPackFileSystem interface -----------------------------------------

    def exists(self, path: str) -> bool:
        return self._entry(path) is not None

    def open(self, path: str) -> BinaryIO:
        entry = self._entry(path)
        if entry is None:
            raise FileNotFoundError(path)
        return io.BytesIO(self._read_payload(entry))

    def read_all_bytes(self, path: str) -> bytes:
        entry = self._entry(path)
        if entry is None:
            raise FileNotFoundError(path)
        return self._read_payload(entry)

    def iter_names(self) -> Iterable[str]:
        """All normalized (lower-cased, forward-slash) stored entry names."""
        return iter(self._entries.keys())

    def iter_entry_names(self) -> Iterable[str]:
        """All stored entry names with their original archive spelling."""
        for e in self._entries.values():
            yield e.name

    def glob(self, pattern: str) -> Iterable[str]:
        import fnmatch
        norm = pattern.replace("\\", "/").lstrip("/").lower()
        for key, entry in self._entries.items():
            if fnmatch.fnmatchcase(key, norm):
                yield entry.name

    def real_path(self, path: str) -> "str | None":
        return None

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "CryPakFileSystem":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_cry_pak(archive: str | Path) -> CryPakFileSystem:
    """Open a Crysis 3 encrypted pak; raises ValueError if unsupported."""
    return CryPakFileSystem(archive)
