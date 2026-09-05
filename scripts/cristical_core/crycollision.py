"""crycollision.py

Decode CryEngine baked collision geometry from serialized physics chunk payloads.

CryEngine cooks static-object collision into a CGF physics chunk whose body is a
CryPhysics-serialized "phys_geometry" blob. For the common concave triangle-mesh
case (geomType == GEOM_TRIMESH == 1) the blob embeds the collision mesh vertices and
indices inline (mesh_full_serialization is always set during cooking), preserving
concave openings such as doorways. This module decodes that trimesh into a plain
CollisionMesh so it can be re-emitted (e.g. as a non-convex MeshCollider in Unity).

Byte layout (little-endian, no padding, bool = 1 byte) is transcribed from the
CryEngine r338 source (phys_geometry / CTriMesh::Save). Only the static-CGF
phys_geometry trimesh is handled here; .chr/.skin CompiledPhysicalProxies raw
arrays are a separate format and out of scope.

API:
  CollisionMesh(positions, indices)
  decode_cgf_physics_chunk(payload: bytes) -> CollisionMesh | None
"""

import json
import os
import struct

# phys_geometry header sizes (bytes), from CryEngine serialization
_PHYS_GEOM_VER_OFFSET = 0
_PHYS_GEOM_META_SIZE = 64   # bytes 4..67: phys_geometry_serialize metadata (opaque here)
_GEOMTYPE_OFFSET = _PHYS_GEOM_VER_OFFSET + 4 + _PHYS_GEOM_META_SIZE  # = 68
_TRIMESH_PAYLOAD_OFFSET = _GEOMTYPE_OFFSET + 4                      # = 72

# geometry type ids (CryPhysics)
GEOM_TRIMESH = 1


class CollisionMesh:
    """Plain collision mesh: float triples for positions, flat index list (3 per tri)."""

    __slots__ = ("positions", "indices")

    def __init__(self) -> None:
        self.positions: list[tuple[float, float, float]] = []
        self.indices: list[int] = []


class _Cursor:
    """Bounded reader over a bytes payload advancing an explicit offset."""

    __slots__ = ("buf", "off")

    def __init__(self, buf: bytes, off: int) -> None:
        self.buf = buf
        self.off = off

    def read(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.off + size > len(self.buf):
            raise ValueError("truncated physics chunk")
        value = struct.unpack_from(fmt, self.buf, self.off)
        self.off += size
        return value[0] if len(value) == 1 else value


def decode_cgf_physics_chunk(payload: bytes) -> CollisionMesh | None:
    """Decode one static-CGF physics chunk payload into a CollisionMesh.

    Returns a CollisionMesh for a concave triangle mesh (geomType == 1), or None for
    unsupported content (convex primitives box/sphere/cylinder/capsule). Raises
    ValueError for a definitively malformed header (wrong phys_geom version) or a
    payload truncated mid-trimesh.
    """
    if len(payload) < _GEOMTYPE_OFFSET + 4:
        raise ValueError("truncated physics chunk")

    version = struct.unpack_from("<i", payload, _PHYS_GEOM_VER_OFFSET)[0]
    if version != 1:
        raise ValueError("unsupported phys_geom_version %d" % version)

    geom_type = struct.unpack_from("<i", payload, _GEOMTYPE_OFFSET)[0]
    if geom_type != GEOM_TRIMESH:
        # Convex primitives (box/sphere/cylinder/capsule) are not a triangle mesh;
        # caller treats them as "no collidable trimesh" and skips.
        return None

    c = _Cursor(payload, _TRIMESH_PAYLOAD_OFFSET)
    n_vertices = c.read("<i")
    n_tris = c.read("<i")
    _n_max_valency = c.read("<i")  # unused
    _flags = c.read("<i")          # unused

    if c.read("<b"):   # bVtxMap: skip nVertices x uint16
        c.read("<%dH" % n_vertices)
    if c.read("<b"):   # bForeignIdx: skip nTris x uint16
        c.read("<%dH" % n_tris)

    positions: list[tuple[float, float, float]] = []
    for _ in range(n_vertices):
        x, y, z = c.read("<3f")
        positions.append((x, y, z))

    idx_count = n_tris * 3
    indices = list(c.read("<%dH" % idx_count))

    if c.read("<b"):   # bIds: skip nTris x uint8 (id per triangle)
        c.read("<%dB" % n_tris)

    mesh = CollisionMesh()
    mesh.positions = positions
    mesh.indices = indices
    return mesh


def merge_meshes(meshes):
    """Concatenate several CollisionMesh into one, re-basing indices."""
    out = CollisionMesh()
    base = 0
    for m in meshes:
        out.positions.extend(m.positions)
        out.indices.extend(i + base for i in m.indices)
        base += len(m.positions)
    return out


def write_collision_gltf(render_gltf_path, meshes, scale=1.0):
    """Write a minimal glTF 2.0 (external .bin) carrying the collision triangles.

    The output is placed next to the render glTF and named '<stem>_collision.gltf'
    (+ '<stem>_collision.bin'), where <stem> is render_gltf_path without extension.
    Positions (float32, VEC3) and indices (uint16, SCALAR) only - enough for a
    Unity non-convex MeshCollider. 'scale' multiplies all positions (e.g. 0.01 to
    convert cm-native .cga/.chr assets to meters). Returns the written .gltf path,
    or None if meshes is empty.
    """
    if not meshes:
        return None
    mesh = merge_meshes(meshes)
    if not mesh.positions or not mesh.indices:
        return None

    stem = os.path.splitext(render_gltf_path)[0]
    gltf_path = stem + "_collision.gltf"
    bin_path = stem + "_collision.bin"

    idx_data = struct.pack("<%dH" % len(mesh.indices), *mesh.indices)
    pos_off = len(idx_data)
    # align position bufferView to 4 bytes (component size of float32)
    if pos_off % 4:
        pad = 4 - (pos_off % 4)
        idx_data += b"\x00" * pad
        pos_off = len(idx_data)
    pos_data = b"".join(struct.pack("<3f", p[0] * scale, p[1] * scale, p[2] * scale)
                        for p in mesh.positions)
    bin_bytes = idx_data + pos_data

    with open(bin_path, "wb") as f:
        f.write(bin_bytes)

    idx_count = len(mesh.indices)
    pos_count = len(mesh.positions)
    doc = {
        "asset": {"version": "2.0"},
        "buffers": [{"uri": os.path.basename(bin_path), "byteLength": len(bin_bytes)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": idx_count * 2, "target": 34963},
            {"buffer": 0, "byteOffset": pos_off, "byteLength": pos_count * 12, "target": 34962},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5123, "count": idx_count, "type": "SCALAR"},
            {"bufferView": 1, "componentType": 5126, "count": pos_count, "type": "VEC3"},
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1}, "indices": 0}]}],
        "nodes": [{"mesh": 0}],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }
    with open(gltf_path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    return gltf_path


if __name__ == "__main__":
    # --- synthetic self-test -------------------------------------------------
    def pack_i(v: int) -> bytes:
        return struct.pack("<i", v)

    def pack_vec(x: float, y: float, z: float) -> bytes:
        return struct.pack("<3f", x, y, z)

    verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
    tris = [0, 1, 2, 0, 2, 3]

    body = bytearray()
    body += pack_i(1)                                # phys_geom_version
    body += b"\x00" * 64                             # opaque metadata
    body += pack_i(1)                                # geomType = trimesh
    body += pack_i(len(verts))                       # nVertices
    body += pack_i(len(tris) // 3)                   # nTris
    body += pack_i(0)                                # nMaxVertexValency
    body += pack_i(0)                                # flags
    body += b"\x00"                                  # bVtxMap = False
    body += b"\x00"                                  # bForeignIdx = False
    for v in verts:
        body += pack_vec(*v)
    for t in tris:
        body += struct.pack("<H", t)
    body += b"\x00"                                  # bIds = False

    mesh = decode_cgf_physics_chunk(bytes(body))
    assert mesh is not None and mesh.positions == verts and mesh.indices == tris

    assert decode_cgf_physics_chunk(pack_i(1) + b"\x00" * 64 + pack_i(0) + b"x" * 32) is None

    try:
        decode_cgf_physics_chunk(bytes(body[:-4]))
        raise AssertionError("expected ValueError for truncated blob")
    except ValueError as exc:
        assert "truncated" in str(exc)

    print("crycollision self-test OK")
