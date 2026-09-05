#!/usr/bin/env python
"""
Level to JSON exporter for CryEngine r338 levels.

Exports level data from an unpacked level directory to a JSON file containing:
- Terrain parameters
- Vegetation definitions
- Entities (from mission XML)
- Light sources (extracted from entities)
- Brush and vegetation instances (from terrain.dat)

terrain.dat parsing supports the Crysis 1 Remastered format only
(TERRAIN_CHUNK_VERSION 25, OCTREENODE_CHUNK_VERSION 4, r338 chunk structs).
Original Crysis 1 retail levels ship no terrain.dat in level.pak and are
skipped with a warning.

Usage:
    python level2json.py <level_directory> [output.json] [--visual-only]
                         [--npc-classes "Class1,Class2"] [--game-dir DIR]

Example:
    python level2json.py F:/Games/Crysis_Remastered/Game/Levels/core/level_Unpacked core.json
"""

import xml.etree.ElementTree as ET
import json
import os
import sys
import argparse
import struct
import math
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple

# terrain.dat format constants (CryEngine r338 / Crysis 1 Remastered).
# Source: L:\\port_HDD\\CGI\\CryEngine_r338-master\\Code\\CryEngine\\
#   Cry3DEngine\\terrain_compile.cpp, ObjectsTree.cpp, ObjectsTree_Serialize.cpp,
#   terrain_node_compile.cpp; CryCommon\\I3DEngine.h, IEntityRenderState.h.

TERRAIN_CHUNK_VERSION_C1R = 25
TERRAIN_HEADER_SIZE = 32            # STerrainChunkHeader: 8B prefix + STerrainInfo 24B
TERRAIN_NODE_CHUNK_VERSION = 6      # STerrainNodeChunk (Remastered keeps 6)
TERRAIN_NODE_CHUNK_SIZE = 44        # int16 ver, int16 holes, AABB 24B, 2 floats, 2 ints
OCTREE_CHUNK_VERSION_C1R = 4        # SOcTreeNodeChunk
OCTREE_NODE_CHUNK_SIZE = 32         # int16 ver, int16 mask, AABB 24B, int32 objectsSize
VEG_GROUP_CHUNK_SIZE = 336          # StatInstGroupChunk (256B name + 20 fields)
NAME_CHUNK_SIZE = 256               # SNameChunk (statobj / material tables)
HM_LEAF_SIZE_METERS = 128.0         # heightmap leaf node extent (core); island uses 64
HM_LEAF_SIZE_CANDIDATES = (64.0, 128.0, 256.0, 32.0, 512.0)  # exact leaf is solved per level
HM_GEOM_ERROR_FLOATS = 5            # sector 64m / unit 2m bit shift

# EERType tags of object chunks inside octree nodes (IEntityRenderState.h).
ER_BRUSH = 1
ER_VEGETATION = 2
ER_VOXEL_OBJECT = 5
ER_DECAL = 7
ER_WATER_VOLUME = 9
ER_WATER_WAVE = 10
ER_ROAD = 11
ER_DISTANCE_CLOUD = 12
ER_AUTO_CUBE_MAP = 14
ER_LPV = 18
ER_LIGHT_SHAPE = 22
ER_DECAL2 = 23

# IVOXELOBJECT_FLAG_* (IEntityRenderState.h).
IVOX_FLAG_COMPILED = 32

SUPPORTED_ER_TYPES = {
    ER_BRUSH, ER_VEGETATION, ER_DECAL, ER_DECAL2, ER_WATER_VOLUME,
    ER_WATER_WAVE, ER_ROAD, ER_DISTANCE_CLOUD, ER_AUTO_CUBE_MAP,
    ER_LPV, ER_LIGHT_SHAPE,
}

# Octree record tag -> class name used by the --skip-classes filter.
TAG_CLASS_NAMES = {
    ER_BRUSH: 'Brush',
    ER_VEGETATION: 'Vegetation',
    ER_VOXEL_OBJECT: 'VoxelObject',
    ER_DECAL: 'Decal',
    ER_DECAL2: 'Decal2',
    ER_WATER_VOLUME: 'WaterVolume',
    ER_WATER_WAVE: 'WaterWave',
    ER_ROAD: 'Road',
    ER_DISTANCE_CLOUD: 'DistanceCloud',
    ER_AUTO_CUBE_MAP: 'AutoCubeMap',
    ER_LPV: 'LPV',
    ER_LIGHT_SHAPE: 'LightShape',
}


# ---------------------------------------------------------------------------
# mission / level XML parsing
# ---------------------------------------------------------------------------

def parse_level_info(level_info_path: str) -> Dict[str, Any]:
    """Parse levelinfo.xml"""
    tree = ET.parse(level_info_path)
    root = tree.getroot()
    return dict(root.attrib)


def parse_level_data(level_data_path: str) -> Dict[str, Any]:
    """Parse leveldata.xml"""
    tree = ET.parse(level_data_path)
    root = tree.getroot()

    data: Dict[str, Any] = {}

    level_info_elem = root.find('LevelInfo')
    if level_info_elem is not None:
        data['level_info'] = dict(level_info_elem.attrib)

    surface_types: List[Dict[str, Any]] = []
    surface_types_elem = root.find('SurfaceTypes')
    if surface_types_elem is not None:
        for st in surface_types_elem.findall('SurfaceType'):
            surface_type = dict(st.attrib)
            vgs = [dict(vg.attrib) for vg in st.findall('VegetationGroup')]
            if vgs:
                surface_type['vegetation_groups'] = vgs
            surface_types.append(surface_type)
    data['surface_types'] = surface_types

    vegetation_defs: List[Dict[str, Any]] = []
    vegetation_elem = root.find('Vegetation')
    if vegetation_elem is not None:
        for obj in vegetation_elem.findall('Object'):
            veg_obj = dict(obj.attrib)
            tl_elem = obj.find('TerrainLayers')
            if tl_elem is not None:
                layers = [layer.get('Name') for layer in tl_elem.findall('Layer') if layer.get('Name')]
                if layers:
                    veg_obj['terrain_layers'] = layers
            vegetation_defs.append(veg_obj)
    data['vegetation_definitions'] = vegetation_defs

    return data


def _detect_light_type(props: Dict[str, Any]) -> str:
    """Unity URP light type from CryEngine light entity properties."""
    try:
        radius = float(props.get('Radius', '0'))
    except ValueError:
        radius = 0.0
    try:
        fov = float(props.get('fProjectorFov', '360'))
    except ValueError:
        fov = 360.0
    try:
        proj_all_dirs = int(props.get('bProjectInAllDirs', '0')) != 0
    except ValueError:
        proj_all_dirs = False
    if radius == 0.0:
        return 'Directional'
    if fov < 360.0 and not proj_all_dirs:
        return 'Spot'
    return 'Point'


def parse_mission_xml(mission_xml_path: str, visual_only: bool = False,
                      npc_classes: Optional[Set[str]] = None) -> Dict[str, Any]:
    """Parse mission_mission0.xml with optional filtering."""
    tree = ET.parse(mission_xml_path)
    root = tree.getroot()

    objects_node = root.find('Objects')
    if objects_node is None:
        return {"entities": [], "lights": []}

    entities: List[Dict[str, Any]] = []
    lights: List[Dict[str, Any]] = []

    visual_classes = {'Light', 'EnvironmentLight', 'SimpleLight', 'FogVolume', 'ParticleEffect'}

    for elem in objects_node:
        if elem.tag != 'Entity':
            continue
        entity_class = elem.get('EntityClass')
        if entity_class is None:
            continue

        if npc_classes is not None:
            if entity_class not in npc_classes:
                continue
        elif visual_only:
            if entity_class not in visual_classes:
                continue

        entity_data: Dict[str, Any] = {
            'name': elem.get('Name'),
            'class': entity_class,
            'entity_id': elem.get('EntityId'),
            'guid': elem.get('EntityGuid'),
            'layer': elem.get('Layer'),
            'position': elem.get('Pos'),
            'rotation': elem.get('Rotate'),
            'scale': elem.get('Scale'),
            'archetype': elem.get('Archetype'),
            'material': elem.get('Material'),
            'properties': {}
        }

        props_elem = elem.find('Properties')
        if props_elem is not None:
            for attr_name, attr_value in props_elem.attrib.items():
                entity_data['properties'][attr_name] = attr_value
            for child in props_elem:
                if child.tag not in entity_data['properties']:
                    entity_data['properties'][child.tag] = {}
                for attr_name, attr_value in child.attrib.items():
                    entity_data['properties'][child.tag][attr_name] = attr_value

        entities.append(entity_data)

        if entity_class in ('Light', 'EnvironmentLight', 'SimpleLight', 'LightSource'):
            lights.append({
                'entity_name': entity_data['name'],
                'entity_class': entity_class,
                'entity_id': entity_data['entity_id'],
                'guid': entity_data['guid'],
                'layer': entity_data['layer'],
                'position': entity_data['position'],
                'rotation': entity_data['rotation'],
                'scale': entity_data['scale'],
                'light_type': _detect_light_type(entity_data['properties']),
                'properties': entity_data['properties'],
            })

    return {'entities': entities, 'lights': lights}


# ---------------------------------------------------------------------------
# terrain.dat parsing (Crysis 1 Remastered, r338 structs)
# ---------------------------------------------------------------------------

class TerrainDatError(Exception):
    """Raised when terrain.dat cannot be parsed with the C1R layout."""


def _read_name(data: bytes, offset: int) -> str:
    """Read a zero-terminated string from a 256-byte SNameChunk slot."""
    end = data.find(b'\x00', offset, offset + NAME_CHUNK_SIZE)
    if end < 0:
        end = offset + NAME_CHUNK_SIZE
    return data[offset:end].decode('utf-8', errors='replace').rstrip('\x00')


def _matrix34_to_trs(m: Tuple[float, ...]) -> Tuple[Tuple[float, float, float],
                                                     Tuple[float, float, float, float],
                                                     Tuple[float, float, float]]:
    """CryEngine Matrix34 (12 row-major floats m00..m23) to position/quaternion/scale.

    Column-vector convention: translation is (m03, m13, m23); basis axes are
    the matrix columns. Returns (pos, quat xyzw, scale).
    """
    pos = (m[3], m[7], m[11])
    # basis columns
    bx = (m[0], m[4], m[8])
    by = (m[1], m[5], m[9])
    bz = (m[2], m[6], m[10])
    sx = math.sqrt(sum(c * c for c in bx))
    sy = math.sqrt(sum(c * c for c in by))
    sz = math.sqrt(sum(c * c for c in bz))
    if sx < 1e-12 or sy < 1e-12 or sz < 1e-12:
        raise TerrainDatError('degenerate instance matrix')
    ux = tuple(c / sx for c in bx)
    uy = tuple(c / sy for c in by)
    uz = tuple(c / sz for c in bz)

    # rotation matrix rows built from orthonormal columns
    m00, m10, m20 = ux
    m01, m11, m21 = uy
    m02, m12, m22 = uz
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * s
        qx = (m21 - m12) / s
        qy = (m02 - m20) / s
        qz = (m10 - m01) / s
    elif m00 > m11 and m00 > m22:
        s = math.sqrt(1.0 + m00 - m11 - m22) * 2.0
        qw = (m21 - m12) / s
        qx = 0.25 * s
        qy = (m01 + m10) / s
        qz = (m02 + m20) / s
    elif m11 > m22:
        s = math.sqrt(1.0 + m11 - m00 - m22) * 2.0
        qw = (m12 - m20) / s
        qx = (m01 + m10) / s
        qy = 0.25 * s
        qz = (m12 + m21) / s
    else:
        s = math.sqrt(1.0 + m22 - m00 - m11) * 2.0
        qw = (m10 - m01) / s
        qx = (m02 + m20) / s
        qy = (m12 + m21) / s
        qz = 0.25 * s
    norm = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    if norm < 1e-12:
        raise TerrainDatError('degenerate instance rotation')
    quat = (qx / norm, qy / norm, qz / norm, qw / norm)
    return pos, quat, (sx, sy, sz)


def _walk_heightmap_tree(data: bytes, offset: int, end_limit: int,
                         leaf_size: float = HM_LEAF_SIZE_METERS,
                         _depth: int = 0) -> int:
    """Recursively skip the CTerrainNode heightmap tree; return offset after it.

    Recursion stops when a node's box extent drops to ``leaf_size`` (the
    terrain sector/leaf node level; 128 m on core, 64 m on island). A wrong
    leaf-size candidate makes the recursion run deeper than the real tree,
    so the walk is depth-capped and fails cleanly.
    """
    if _depth > 32:
        raise TerrainDatError('heightmap tree depth exceeded (wrong leaf size?)')
    if offset + TERRAIN_NODE_CHUNK_SIZE > end_limit:
        raise TerrainDatError('heightmap chunk out of file bounds')
    version, _holes = struct.unpack_from('<2h', data, offset)
    if version not in (5, TERRAIN_NODE_CHUNK_VERSION):
        raise TerrainDatError(f'heightmap chunk version {version} at {offset}')
    box_min = struct.unpack_from('<3f', data, offset + 4)
    box_max = struct.unpack_from('<3f', data, offset + 16)
    _f_offset, _f_range, n_size, n_surface_types = struct.unpack_from('<4i', data, offset + 28)
    if not (0 <= n_size <= 4096) or not (0 <= n_surface_types <= 512):
        raise TerrainDatError(f'implausible heightmap chunk at {offset}')
    p = offset + TERRAIN_NODE_CHUNK_SIZE
    if n_size:
        p += n_size * n_size * 2
        p = (p + 3) & ~3
    p += HM_GEOM_ERROR_FLOATS * 4
    if n_surface_types:
        p += n_surface_types
        p = (p + 3) & ~3
    if p > end_limit:
        raise TerrainDatError('heightmap node overruns file')
    box_extent = box_max[0] - box_min[0]
    if box_extent > leaf_size + 1e-6:
        for _ in range(4):
            p = _walk_heightmap_tree(data, p, end_limit, leaf_size, _depth + 1)
    return p


def _walk_octree(data: bytes, offset: int, end_limit: int,
                 obj_blocks: List[Tuple[int, int]]) -> int:
    """Recursively register octree nodes; collect (block_offset, block_size)."""
    if offset + OCTREE_NODE_CHUNK_SIZE > end_limit:
        raise TerrainDatError('octree chunk out of file bounds')
    version, childs_mask = struct.unpack_from('<2h', data, offset)
    if version != OCTREE_CHUNK_VERSION_C1R:
        raise TerrainDatError(f'octree chunk version {version} at {offset}')
    node_min = struct.unpack_from('<3f', data, offset + 4)
    node_max = struct.unpack_from('<3f', data, offset + 16)
    if any(node_min[k] > node_max[k] for k in range(3)):
        raise TerrainDatError(f'invalid octree node box at {offset}')
    objects_size = struct.unpack_from('<i', data, offset + 28)[0]
    if objects_size < 0 or offset + OCTREE_NODE_CHUNK_SIZE + objects_size > end_limit:
        raise TerrainDatError(f'octree objects block out of bounds at {offset}')
    p = offset + OCTREE_NODE_CHUNK_SIZE
    if objects_size:
        obj_blocks.append((p, objects_size))
        p += objects_size
    for child_id in range(8):
        if childs_mask & (1 << child_id):
            p = _walk_octree(data, p, end_limit, obj_blocks)
    return p


def _parse_object_block(data: bytes, start: int, end: int,
                        brush_table: List[str], material_table: List[str],
                        veg_table: List[str], brush_out: List[Dict[str, Any]],
                        veg_out: List[Dict[str, Any]],
                        warnings: Optional[List[str]] = None,
                        skip_classes: Optional[Set[str]] = None,
                        voxel_out: Optional[List[Dict[str, Any]]] = None) -> None:
    """Parse one octree object block (COctreeNode::LoadObjects layout).

    Records whose class name (TAG_CLASS_NAMES) is in ``skip_classes`` are
    advanced past without decoding their fields. A corrupt instance (bad
    matrix, out-of-table index) is skipped with a warning instead of
    aborting the whole block; a structural failure (unknown tag / overrun)
    aborts only this block, also with a warning.
    """
    p = start
    try:
        while p < end:
            (tag,) = struct.unpack_from('<I', data, p)
            p += 4
            if tag == ER_BRUSH:
                if skip_classes and 'Brush' in skip_classes:
                    p += 96
                else:
                    base = struct.unpack_from('<6f', data, p)
                    layer_id, _dummy, rnd_flags = struct.unpack_from('<3I', data, p + 24)
                    obj_type_id, _vdr, _lod = struct.unpack_from('<HBB', data, p + 32)
                    m = struct.unpack_from('<12f', data, p + 36)
                    _merge_group, material_id, _material_layers = struct.unpack_from('<3i', data, p + 84)
                    p += 96
                    if not (0 <= obj_type_id < len(brush_table)):
                        raise TerrainDatError(
                            f'brush statobj index {obj_type_id} out of table at {p - 100}')
                    pos, quat, scale = _matrix34_to_trs(m)
                    brush_out.append({
                        'name': brush_table[obj_type_id],
                        'class': 'Brush',
                        'entity_id': None,
                        'guid': None,
                        'layer': layer_id,
                        'position': f'{pos[0]:.4f},{pos[1]:.4f},{pos[2]:.4f}',
                        'rotation': f'{quat[0]:.6f},{quat[1]:.6f},{quat[2]:.6f},{quat[3]:.6f}',
                        'scale': f'{scale[0]:.5f},{scale[1]:.5f},{scale[2]:.5f}',
                        'material': material_table[material_id] if 0 <= material_id < len(material_table) else None,
                        'bbox': f'{base[0]:.3f},{base[1]:.3f},{base[2]:.3f}|{base[3]:.3f},{base[4]:.3f},{base[5]:.3f}',
                        'rnd_flags': rnd_flags,
                    })
            elif tag == ER_VEGETATION:
                # C1R always writes SVegetationChunkEx (60 bytes): the r338
                # SVegetationChunk fields plus normalX/normalY in the base
                # padding (@54-55) and m_HMAIndex (@56-59).
                if skip_classes and 'Vegetation' in skip_classes:
                    p += 60
                else:
                    _bbox = struct.unpack_from('<6f', data, p)
                    _layer, _dummy, _rnd = struct.unpack_from('<3I', data, p + 24)
                    group_id, _vdr, _lod = struct.unpack_from('<HBB', data, p + 32)
                    vpos = struct.unpack_from('<3f', data, p + 36)
                    vscale = struct.unpack_from('<f', data, p + 48)[0]
                    bright, angle = struct.unpack_from('<2B', data, p + 52)
                    p += 60
                    if not (0 <= group_id < len(veg_table)):
                        raise TerrainDatError(
                            f'vegetation group index {group_id} out of table at {p - 60}')
                    rotation_z = angle * (2.0 * math.pi / 255.0)
                    veg_out.append({
                        'name': veg_table[group_id],
                        'class': 'Vegetation',
                        'entity_id': None,
                        'guid': None,
                        'layer': None,
                        'position': f'{vpos[0]:.4f},{vpos[1]:.4f},{vpos[2]:.4f}',
                        'rotation': f'0.0,0.0,{math.sin(rotation_z / 2.0):.6f},{math.cos(rotation_z / 2.0):.6f}',
                        'scale': f'{vscale:.5f},{vscale:.5f},{vscale:.5f}',
                        'material': None,
                        'group_id': group_id,
                        'brightness': bright,
                    })
            elif tag == ER_ROAD:
                # SRoadChunk: m_nVertsNum @chunk+36 (after SRenderNodeChunk); total 64.
                verts_num = struct.unpack_from('<i', data, p + 36)[0]
                p += 64 + verts_num * 12
            elif tag in (ER_DECAL, ER_DECAL2):
                p += 112 if tag == ER_DECAL else 120
            elif tag == ER_WATER_VOLUME:
                # SWaterVolumeChunk: m_numVertices @chunk+104,
                # m_numVerticesPhysAreaContour @chunk+112; total 116.
                nverts, nphys = struct.unpack_from('<2I', data, p + 104)
                p += 116 + (nverts + nphys) * 12
            elif tag == ER_WATER_WAVE:
                # SWaterWaveChunk: m_nVertexCount @chunk+88; total 148.
                nverts = struct.unpack_from('<I', data, p + 88)[0]
                p += 148 + nverts * 12
            elif tag == ER_DISTANCE_CLOUD:
                p += 64
            elif tag == ER_AUTO_CUBE_MAP:
                p += 92
            elif tag == ER_LPV:
                p += 92
            elif tag == ER_LIGHT_SHAPE:
                p += 88
            elif tag == ER_VOXEL_OBJECT:
                # SVoxelObjectChunkVer4 = 36B SRenderNodeChunk + 1056B SVoxelChunkVer4
                #   + 48B Matrix34, then optional compiled-mesh payload:
                #   uint32 count + count * (uint32 size + size bytes + align4).
                record_start = p  # body start (after the 4-byte tag)
                vox_flags = struct.unpack_from('<I', data, p + 64)[0]
                skip_voxel = skip_classes and 'VoxelObject' in skip_classes
                if not skip_voxel:
                    vox_version = struct.unpack_from('<i', data, p + 36)[0]
                    if vox_version != 4:
                        raise TerrainDatError(
                            f'unsupported voxel chunk version {vox_version} at {p}')
                p += 1140
                blob_spans = []
                if vox_flags & IVOX_FLAG_COMPILED:
                    (mesh_count,) = struct.unpack_from('<I', data, p)
                    p += 4
                    for _ in range(mesh_count):
                        (mesh_size,) = struct.unpack_from('<I', data, p)
                        blob_spans.append((p + 4, mesh_size))
                        p += 4 + mesh_size
                        p = (p + 3) & ~3
                if voxel_out is not None and not skip_voxel:
                    surfaces = []
                    for i in range(16):
                        nm = data[record_start + 68 + i * 64:
                                  record_start + 68 + (i + 1) * 64].split(b'\x00')[0]
                        if nm:
                            surfaces.append(nm.decode('utf-8', errors='replace'))
                    m = struct.unpack_from('<12f', data, record_start + 1092)
                    pos, quat, scale = _matrix34_to_trs(m)
                    voxel_out.append({
                        'class': 'VoxelObject',
                        'name': surfaces[0] if surfaces else 'VoxelObject',
                        'surfaces': surfaces,
                        'flags': vox_flags,
                        'position': f'{pos[0]:.4f},{pos[1]:.4f},{pos[2]:.4f}',
                        'rotation': f'{quat[0]:.6f},{quat[1]:.6f},{quat[2]:.6f},{quat[3]:.6f}',
                        'scale': f'{scale[0]:.5f},{scale[1]:.5f},{scale[2]:.5f}',
                        'blob_count': len(blob_spans),
                        'blob_spans': [(o, s) for o, s in blob_spans],
                    })
            else:
                raise TerrainDatError(f'unknown object tag {tag} at {p - 4}')
            p = (p + 3) & ~3
        if p != end:
            raise TerrainDatError('object block trailing bytes')
    except TerrainDatError as exc:
        if warnings is not None:
            warnings.append(f'object block at {start}: skipped ({exc})')


def _extended_region_starts(data: bytes) -> List[int]:
    """Candidate veg-table offsets after the C1R extended per-node region.

    The Remastered format inserts a per-heightmap-node int32 region between
    the 32-byte header and the tables. Its size is (4^k - 1)/3 * 4 bytes —
    a depth-k quadtree node count — for k = 2..12, giving deterministic
    candidates instead of a blind scan.
    """
    starts = []
    for k in range(2, 13):
        offset = TERRAIN_HEADER_SIZE + ((4 ** k - 1) // 3) * 4
        starts.append(offset)
    return starts


def _solve_sections(data: bytes,
                    warnings: Optional[List[str]] = None
                    ) -> Tuple[int, List[str], List[str], List[str], int, int]:
    """Constructively locate veg/brush/material tables and the heightmap tree.

    The C1R format inserts a variable-size per-node region between the 32-byte
    header and the tables, so instead of fixed offsets every candidate start
    is tried: tables -> heightmap tree (per leaf-size candidate) -> octree must
    consume the file exactly. Non-solvable candidates are silently skipped;
    only the total failure is fatal (TerrainDatError).
    Returns (veg_offset, veg_names, brush_names, material_names, heightmap_offset,
    octree_root).
    """
    n = len(data)
    # octree roots, ordered by offset ascending: a valid root is the earliest
    # one whose full recursive walk consumes the file exactly.
    octree_roots = []
    for o in range(TERRAIN_HEADER_SIZE, n - OCTREE_NODE_CHUNK_SIZE, 4):
        version, mask = struct.unpack_from('<2h', data, o)
        if version != OCTREE_CHUNK_VERSION_C1R or mask > 255:
            continue
        nmin = struct.unpack_from('<3f', data, o + 4)
        nmax = struct.unpack_from('<3f', data, o + 16)
        if any(nmin[k] > nmax[k] for k in range(3)) or any(abs(v) > 1e5 for v in nmin + nmax):
            continue
        blocks: List[Tuple[int, int]] = []
        try:
            if _walk_octree(data, o, n, blocks) == n:
                octree_roots.append(o)
        except TerrainDatError:
            continue

    first_str = min(
        (m.start() for m in __import__('re').finditer(rb'[ -~]{8,}', data[TERRAIN_HEADER_SIZE:])),
        default=-1,
    )
    first_str += TERRAIN_HEADER_SIZE if first_str >= 0 else 0
    # the first table string lives at veg_start + 4 (count int32 first),
    # so the veg table may start exactly at first_str - 4 — inclusive bound
    veg_limit = (first_str - 4) if first_str > 0 else (n - 4)
    candidates: List[int] = [
        o for o in _extended_region_starts(data)
        if TERRAIN_HEADER_SIZE <= o <= veg_limit
    ]
    fallback_bound = candidates[0] if candidates else (veg_limit + 1)
    candidates.extend(range(TERRAIN_HEADER_SIZE, fallback_bound, 4))
    seen = set()
    ordered_candidates = [o for o in candidates if not (o in seen or seen.add(o))]

    for octree_start in octree_roots:
        for veg_start in ordered_candidates:
            if veg_start >= octree_start:
                continue
            nveg = struct.unpack_from('<i', data, veg_start)[0]
            if nveg < 0 or veg_start + 4 + nveg * VEG_GROUP_CHUNK_SIZE > octree_start:
                continue
            p = veg_start + 4 + nveg * VEG_GROUP_CHUNK_SIZE
            nbrush = struct.unpack_from('<i', data, p)[0]
            if nbrush < 0 or p + 4 + nbrush * NAME_CHUNK_SIZE > octree_start:
                continue
            p += 4 + nbrush * NAME_CHUNK_SIZE
            nmat = struct.unpack_from('<i', data, p)[0]
            if nmat < 0 or p + 4 + nmat * NAME_CHUNK_SIZE > octree_start:
                continue
            hm_start = p + 4 + nmat * NAME_CHUNK_SIZE
            solved_leaf = None
            for leaf in HM_LEAF_SIZE_CANDIDATES:
                try:
                    if _walk_heightmap_tree(data, hm_start, octree_start, leaf) == octree_start:
                        solved_leaf = leaf
                        break
                except TerrainDatError:
                    continue
            if solved_leaf is None:
                continue
            veg_names = [
                _read_name(data, veg_start + 4 + i * VEG_GROUP_CHUNK_SIZE)
                for i in range(nveg)
            ]
            brush_off = veg_start + 4 + nveg * VEG_GROUP_CHUNK_SIZE + 4
            brush_names = [
                _read_name(data, brush_off + i * NAME_CHUNK_SIZE)
                for i in range(nbrush)
            ]
            mat_off = brush_off + nbrush * NAME_CHUNK_SIZE + 4
            mat_names = [
                _read_name(data, mat_off + i * NAME_CHUNK_SIZE)
                for i in range(nmat)
            ]
            return veg_start, veg_names, brush_names, mat_names, hm_start, octree_start
    raise TerrainDatError(
        f'failed to locate terrain.dat sections '
        f'(octree roots probed: {len(octree_roots)})')


# StatInstGroupChunk material id offset: 256B name + 17 x 4B fields + nFlags.
_VEG_MATERIAL_ID_OFFSET = 256 + 17 * 4 + 4


def parse_terrain_dat(terrain_dat_path: str, skip_classes: Optional[Set[str]] = None,
                      warnings: Optional[List[str]] = None) -> Dict[str, Any]:
    """Parse terrain.dat (Crysis 1 Remastered layout).

    ``skip_classes`` names octree record classes (Brush, Vegetation,
    VoxelObject, Road, Decal, Decal2, WaterVolume, WaterWave, DistanceCloud,
    AutoCubeMap, LPV, LightShape) whose instances must be advanced past
    without decoding; vegetation group definitions are still exported so a
    forest spawner can be placed in the target engine instead.
    Raises TerrainDatError on fatal format mismatches; per-block problems
    are appended to ``warnings`` and the block is skipped.
    """
    if warnings is None:
        warnings = []
    if skip_classes is None:
        skip_classes = set()
    try:
        with open(terrain_dat_path, 'rb') as f:
            data = f.read()
    except OSError as exc:
        raise TerrainDatError(f'cannot read terrain.dat: {exc}') from exc
    if len(data) < TERRAIN_HEADER_SIZE + 4:
        raise TerrainDatError('file too small')
    version = data[0]
    if version != TERRAIN_CHUNK_VERSION_C1R:
        raise TerrainDatError(
            f'unsupported terrain.dat version {version} '
            f'(C1R expects {TERRAIN_CHUNK_VERSION_C1R})')
    (chunk_size,) = struct.unpack_from('<i', data, 4)
    if chunk_size != len(data):
        warnings.append(
            f'terrain.dat header chunk size {chunk_size} != file size {len(data)}; '
            'continuing anyway')

    veg_start, veg_names, brush_names, mat_names, _hm_start, octree_start = _solve_sections(data)

    collects_any = bool({'Brush', 'Vegetation'} - skip_classes)
    brushes: List[Dict[str, Any]] = []
    vegetation: List[Dict[str, Any]] = []
    voxels: List[Dict[str, Any]] = []
    obj_blocks: List[Tuple[int, int]] = []
    _walk_octree(data, octree_start, len(data), obj_blocks)
    skipped_blocks = 0
    for block_start, block_size in obj_blocks:
        before = len(brushes) + len(vegetation)
        _parse_object_block(data, block_start, block_start + block_size,
                            brush_names, mat_names, veg_names, brushes, vegetation,
                            warnings, skip_classes, voxels)
        if len(brushes) + len(vegetation) == before and block_size > 0 and collects_any:
            skipped_blocks += 1
    if skipped_blocks:
        warnings.append(
            f'{skipped_blocks} octree object block(s) produced no instances')

    veg_groups: List[Dict[str, Any]] = []
    p = veg_start + 4
    for i in range(len(veg_names)):
        (material_id,) = struct.unpack_from('<i', data, p + _VEG_MATERIAL_ID_OFFSET)
        veg_groups.append({
            'name': veg_names[i],
            'material': mat_names[material_id] if 0 <= material_id < len(mat_names) else None,
        })
        p += VEG_GROUP_CHUNK_SIZE

    return {
        'vegetation_groups': veg_groups,
        'brush_models': brush_names,
        'materials': mat_names,
        'brushes': brushes,
        'vegetation': vegetation,
        'voxels': voxels,
        '_terrain_data': data,
    }


# ---------------------------------------------------------------------------
# game-profile gating
# ---------------------------------------------------------------------------

def _import_game_profile():
    """Import game_profile from the cristical_core package next to this script."""
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from cristical_core.game_profile import GameTitle  # noqa: E402
    return GameTitle


def _brush_gating_allowed(level_dir: Path, game_dir: Optional[str]) -> bool:
    """True when the level belongs to Crysis 1 Remastered (the only supported
    terrain.dat layout). Uses --game-dir when given, else infers the game root
    from the level path (Game\\Levels\\<level>\\... -> Game)."""
    if game_dir:
        candidates = [game_dir]
    else:
        # level dir: <game_root>\\Levels\\<level>\\[level_Unpacked]
        candidates = []
        parts = level_dir.resolve().parts
        for i in range(len(parts) - 1, 1, -1):
            if parts[i].lower() == 'levels':
                candidates.append(str(Path(*parts[:i])))
                break
    if not candidates:
        return False
    GameTitle = _import_game_profile()
    from cristical_core.game_profile import classify_game_dir
    for cand in candidates:
        info = classify_game_dir(cand)
        if info.get('title') is GameTitle.CRYSIS_REMASTERED:
            return True
    return False


# ---------------------------------------------------------------------------
# voxel extraction (specialized, level-only; does not touch cgf2gltf)
# ---------------------------------------------------------------------------

def _surface_type_material_map(level_data: Dict[str, Any]) -> Dict[str, str]:
    """SurfaceType name -> DetailMaterial path from leveldata.xml.

    Voxel surface names (SVoxelObjectChunkVer4 m_arrSurfaceNames) resolve at
    engine load time to terrain surface types (VoxMan.cpp SetSurfacesInfo);
    each surface type carries a DetailMaterial .mtl with the real textures.
    """
    mapping: Dict[str, str] = {}
    for st in level_data.get('surface_types', []):
        name = st.get('Name')
        detail = st.get('DetailMaterial')
        if name and detail:
            mapping[name] = detail
    return mapping


def _extract_voxels(voxels: List[Dict[str, Any]],
                    terrain_data: bytes,
                    level_data: Dict[str, Any],
                    game_dir: Optional[str],
                    output_json: str,
                    warnings: List[str]) -> List[Dict[str, Any]]:
    """Extract compiled voxel meshes to CGF, pair with the surface-type .mtl
    sidecar, convert to glTF via the standard cgf2gltf pipeline, and return
    voxel entity records referencing the produced files.

    Specialized for the level exporter: the standard converter's material
    resolution (sidecar <stem>.mtl) is used as is — we only WRITE the right
    sidecar (voxel surface -> SurfaceType DetailMaterial from leveldata.xml,
    resolved through the game VFS).
    """
    import zlib
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    from cgf2gltf import run_pipeline as run_cgf_pipeline
    from cristical_core.cryvfs import VFSIndex

    surf_map = _surface_type_material_map(level_data)
    out_dir = Path(output_json).resolve().parent / 'voxel'
    out_dir.mkdir(parents=True, exist_ok=True)
    game_dirs = [game_dir] if game_dir else []
    vfs = VFSIndex(game_dirs) if game_dirs else None

    extracted: List[Dict[str, Any]] = []
    for idx, vox in enumerate(voxels):
        vox_name = f'voxel_{idx}'
        mesh_written = False
        for blob_idx, (blob_off, blob_size) in enumerate(vox.get('blob_spans', [])):
            blob = terrain_data[blob_off:blob_off + blob_size]
            try:
                cgf_bytes = zlib.decompress(blob[4:])  # CMemoryBlock header: uint32 usize, then zlib stream
            except zlib.error as exc:
                warnings.append(f'{vox_name} blob {blob_idx}: zlib failed ({exc}); skipped')
                continue
            if not cgf_bytes.startswith(b'CryTek\x00'):
                warnings.append(f'{vox_name} blob {blob_idx}: not a CGF (magic {cgf_bytes[:7]!r}); skipped')
                continue
            cgf_path = out_dir / f'{vox_name}_{blob_idx}.cgf'
            with open(cgf_path, 'wb') as f:
                f.write(cgf_bytes)
            mesh_written = True

            # sidecar .mtl: voxel surfaces -> SurfaceType DetailMaterial
            surf_materials = []
            for surf in vox.get('surfaces', []):
                mtl_path = surf_map.get(surf)
                if mtl_path and vfs is not None:
                    vfs_path = mtl_path if mtl_path.lower().endswith('.mtl') \
                        else mtl_path + '.mtl'
                    try:
                        # VFS names are lowercase; leveldata.xml paths are not
                        mtl_bytes = vfs.read_all_bytes(vfs_path.lower())
                        sidecar = cgf_path.with_suffix('.mtl')
                        with open(sidecar, 'wb') as f:
                            f.write(mtl_bytes)
                        surf_materials.append(surf)
                    except Exception:
                        warnings.append(f'{vox_name}: surface "{surf}" material '
                                       f'"{surf_map.get(surf)}" not found in game data')
                elif mtl_path and vfs is None:
                    warnings.append(f'{vox_name}: no --game-dir; cannot resolve '
                                   f'surface "{surf}" material')
            if not vox.get('surfaces'):
                warnings.append(f'{vox_name}: no surface names in chunk')

            # convert through the standard pipeline (sidecar resolves)
            try:
                gltf_path = cgf_path.with_suffix('.gltf')
                run_cgf_pipeline(str(cgf_path), game_dirs, str(gltf_path),
                                 do_tex=True, progress_cb=None, read_color1=True)
                vox[f'gltf_{blob_idx}'] = str(gltf_path)
            except Exception as exc:
                warnings.append(f'{vox_name} blob {blob_idx}: glTF conversion failed ({exc})')
        if mesh_written:
            vox['gltf_dir'] = str(out_dir)
            extracted.append(vox)
    return extracted


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------

def build_json(level_dir: str, visual_only: bool = False,
               npc_classes: Optional[Set[str]] = None,
               game_dir: Optional[str] = None,
               skip_classes: Optional[Set[str]] = None,
               voxel_extract: bool = False,
               output_file: Optional[str] = None) -> Dict[str, Any]:
    """Build the JSON structure from an unpacked level directory.

    ``skip_classes`` drops both binary octree record classes (Brush,
    Vegetation, VoxelObject, Road, Decal, Decal2, WaterVolume, WaterWave,
    DistanceCloud, AutoCubeMap, LPV, LightShape) and mission entity classes
    with the same names. ``voxel_extract`` additionally unpacks compiled
    voxel meshes to CGF + surface-material sidecar and converts them to
    glTF next to the output JSON. Auxiliary XML files (levelinfo/leveldata/
    mission) degrade gracefully: a missing or unparsable one contributes a
    warning and empty sections. terrain.dat failures (format mismatch,
    corrupt octree) are fatal for the terrain section but the export still
    completes, with a warning.
    """
    level_dir = Path(level_dir)
    warnings: List[str] = []
    if skip_classes is None:
        skip_classes = set()

    try:
        level_info = parse_level_info(str(level_dir / 'levelinfo.xml'))
    except (OSError, ET.ParseError) as exc:
        warnings.append(f'levelinfo.xml unreadable: {exc}')
        level_info = {}
    try:
        level_data = parse_level_data(str(level_dir / 'leveldata.xml'))
    except (OSError, ET.ParseError) as exc:
        warnings.append(f'leveldata.xml unreadable: {exc}')
        level_data = {}
    try:
        mission_data = parse_mission_xml(str(level_dir / 'mission_mission0.xml'),
                                         visual_only, npc_classes)
    except (OSError, ET.ParseError) as exc:
        warnings.append(f'mission_mission0.xml unreadable: {exc}')
        mission_data = {'entities': [], 'lights': []}

    if skip_classes:
        mission_data['entities'] = [
            e for e in mission_data['entities']
            if e['class'] not in skip_classes
        ]

    brushes: List[Dict[str, Any]] = []
    vegetation: List[Dict[str, Any]] = []
    voxels: List[Dict[str, Any]] = []
    terrain_dat_path = level_dir / 'terrain' / 'terrain.dat'
    if terrain_dat_path.is_file():
        if _brush_gating_allowed(level_dir, game_dir):
            try:
                terrain = parse_terrain_dat(str(terrain_dat_path), skip_classes,
                                           warnings)
                brushes = terrain['brushes']
                vegetation = terrain['vegetation']
                voxels = terrain['voxels']
                if voxel_extract and voxels and 'VoxelObject' not in skip_classes:
                    voxels = _extract_voxels(voxels, terrain['_terrain_data'],
                                             level_data, game_dir,
                                             output_file or str(level_dir / 'level.json'),
                                             warnings)
            except TerrainDatError as exc:
                warnings.append(f'terrain.dat not parsed: {exc}')
        else:
            warnings.append(
                'terrain.dat present but game profile is not Crysis Remastered; '
                'brush/vegetation parsing skipped')
    else:
        warnings.append('terrain.dat not found; brush/vegetation instances omitted')

    result: Dict[str, Any] = {
        'metadata': {
            'level_name': level_info.get('Name', 'unknown'),
            'sandbox_version': level_info.get('SandboxVersion'),
            'heightmap_size': level_info.get('HeightmapSize'),
            'heightmap_unit_size': level_data.get('level_info', {}).get('HeightmapUnitSize'),
            'heightmap_max_height': level_data.get('level_info', {}).get('HeightmapMaxHeight'),
            'water_level': level_data.get('level_info', {}).get('WaterLevel'),
            'terrain_sector_size': level_data.get('level_info', {}).get('TerrainSectorSizeInMeters'),
            'skip_classes': sorted(skip_classes),
            'warnings': warnings,
        },
        'terrain_parameters': dict(level_data.get('level_info', {})),
        'vegetation_definitions': level_data.get('vegetation_definitions', []),
        'surface_types': level_data.get('surface_types', []),
        'entities': mission_data['entities'] + brushes + vegetation + voxels,
        'lights': mission_data['lights'],
    }
    return result


def main():
    parser = argparse.ArgumentParser(description='Export CryEngine r338 level data to JSON.')
    parser.add_argument('level_directory', nargs='?',
                        help='Path to unpacked level directory (containing levelinfo.xml, etc.)')
    parser.add_argument('--level',
                        help='Same as the positional level_directory (flag form for .bat symmetry)')
    parser.add_argument('output_file', nargs='?',
                        help='Output JSON file (default: <level_name>.json)')
    parser.add_argument('--out', '-o',
                        help='Same as the positional output_file (flag form)')
    parser.add_argument('--visual-only', action='store_true',
                        help='Export only visual entities (lights, fog volumes, particle effects)')
    parser.add_argument('--npc-classes', type=str,
                        help='Comma-separated list of NPC classes to export (e.g., "Alien,Scout")')
    parser.add_argument('--game-dir',
                        help='Game data root for profile detection (e.g. F:/Games/Crysis_Remastered/Game); '
                             'inferred from the level path when omitted')
    parser.add_argument('--skip-classes', type=str,
                        help='Comma-separated classes to drop: binary octree records '
                             '(Brush, Vegetation, VoxelObject, Road, Decal, Decal2, WaterVolume, '
                             'WaterWave, DistanceCloud, AutoCubeMap, LPV, LightShape) and '
                             'mission entity classes with the same names')
    parser.add_argument('--no-vegetation', action='store_true',
                        help='Shorthand for --skip-classes Vegetation: skip vegetation instances; '
                             'group definitions are still exported for spawner-based placement')
    parser.add_argument('--voxel-extract', action='store_true',
                        help='Extract compiled voxel meshes to CGF, pair with the surface-type '
                             '.mtl (leveldata.xml DetailMaterial, resolved via --game-dir) and '
                             'convert each to glTF into <output_dir>/voxel/')
    args = parser.parse_args()

    level_directory = args.level or args.level_directory
    if not level_directory:
        parser.error('level directory is required (positional or --level)')
    if not os.path.isdir(level_directory):
        print(f"Error: {level_directory} is not a directory", file=sys.stderr)
        sys.exit(1)

    output_file = (args.out or args.output_file
                   or f'{os.path.basename(os.path.normpath(level_directory))}.json')

    npc_classes_set: Optional[Set[str]] = None
    if args.npc_classes:
        npc_classes_set = {cls.strip() for cls in args.npc_classes.split(',') if cls.strip()}

    skip_classes: Set[str] = set()
    if args.skip_classes:
        skip_classes.update(cls.strip() for cls in args.skip_classes.split(',') if cls.strip())
    if args.no_vegetation:
        skip_classes.add('Vegetation')

    print(f"Processing level in: {level_directory}")
    print(f"Output will be written to: {output_file}")
    if args.visual_only:
        print("Filter: visual-only entities")
    if npc_classes_set:
        print(f"Filter: NPC classes {npc_classes_set}")
    if skip_classes:
        print(f"Filter: skipping classes {sorted(skip_classes)}")

    try:
        data = build_json(level_directory, args.visual_only, npc_classes_set,
                          args.game_dir, skip_classes,
                          args.voxel_extract, output_file)
    except Exception as exc:
        print(f"Error processing level: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    for warning in data['metadata']['warnings']:
        print(f"Warning: {warning}", file=sys.stderr)

    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"Error writing {output_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    n_entities = len(data['entities'])
    n_brushes = sum(1 for e in data['entities'] if e.get('class') == 'Brush')
    n_veg = sum(1 for e in data['entities'] if e.get('class') == 'Vegetation')
    n_vox = sum(1 for e in data['entities'] if e.get('class') == 'VoxelObject')
    print(f"Successfully exported level data to {output_file} "
          f"({n_entities} entities: {n_brushes} brushes, {n_veg} vegetation, "
          f"{n_vox} voxels, {len(data['lights'])} lights, "
          f"{len(data['metadata']['warnings'])} warnings)")


if __name__ == '__main__':
    main()
