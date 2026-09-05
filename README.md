# CrisTical Crysis3D Converter

Converts Crysis game assets to glTF 2.0: characters, static and animated objects, and whole levels. Supported editions: Crysis 1 (original), Warhead, Crysis 2, Crysis 3, Remastered and Wars — the game version is auto-detected from the data, so manual selection is usually unnecessary.

A character is built around `.cdf` (Character Definition File), the root assembly point that merges the main model, all attachments and its skeleton animations into a single output. Also converts static geometry (`.cgf` vegetation, props) and animated objects (`.cga`). Reads data from binary .chr/.cgf/.cga/.dba/.anm/.mtl files through independent analysis of the file formats. No third-party converters required.

**Author:** Soror L.'.L.'. aka Methelina&nbsp;|&nbsp; **Version:** 2.1 &nbsp;|&nbsp; **License:** Apache 2.0

![Output](docs/action_motor.gif)

---

## Features

- **CDF assembly** — auto-merge Model + CA_SKIN Attachments into one glTF
- **Skeleton** — full bone hierarchy with correct inverse-bind matrices
- **Mesh** — all primitives with POSITION/NORMAL/UV/JOINTS/WEIGHTS
- **Static CGF** — `cgf2gltf.py` reads skeleton-free geometry (Mesh/Node/DataStream/MeshSubsets chunks) with **vertex colors → COLOR_0** and **tangents → TANGENT** preserved (packed int16 tangents unpacked), node hierarchy baked into world space
- **Animations** — supports DBA v0903 (original) and v0905 (Remaster)
- **Textures** — auto-convert DDS (DXT1/DXT5/ATI2N/3DC/RGBA8/L8) → PNG
- **DDN normals** — Z-channel reconstruction, DDNA gloss extraction by suffix
- **Emission** — Diffuse alpha → emissiveTexture (emission power in Crysis)
- **DDS-Unsplit** — combines split files (.dds.0/.1/...) into single DDS (mip-0) based on [DDS-Unsplitter](https://github.com/Markemp/DDS-Unsplitter) method 
- **Materials** — PBR metallicRoughness + baseColorTexture + normalTexture from .mtl
- **Multi-material** — per-attachment .mtl loading; CGF subset mat_id resolved through node material subMaterials
- **Split animations** — export each animation as a separate glTF
- **GLB export** — single binary file output option
- **Quaternion fix** — eliminates bone-twisting artifacts
- **GUI + CLI** — graphical control panel and full command-line mode
- **Native file dialogs** — Browse/+ buttons open the OS picker (tkinter); the built-in dialog is only a fallback
- **Auto-detect game root** — GUI finds game directory by walking up from .cdf
- **MCP server** — `MCP_CrisTical_bridge.py` exposes the full pipeline as native MCP tools (Kilo Code, Claude, Cursor, ...): convert, scan, catalog, list, version, level2json, unpack
- **Animated geometry (.cga)** — node hierarchy with .anm animation → glTF
- **Object collider export** — `--extract-collision` writes the engine collider into a separate `<name>_collision.gltf` (handy for doors, openings and archways)
- **Level export** — level → readable JSON description (geometry, objects, lights; for Remastered data also full voxel-surface color)
- **Archive unpacking** — encrypted game .pak archives are unpacked into plain folders
- **Auto edition detection** — game version (Crysis 1/2/3, Warhead, Remastered, Wars) is detected automatically from the archive format
- **Universal** — works with characters, objects and levels across Crysis 1-3, Warhead, Remastered and Wars

---

## Support by game edition

| Capability | Crysis 1 | Warhead | Crysis 2 | Crysis 3 | Remastered | Wars |
|------|------|------|------|------|------|------|
| Character with skeleton and animations → glTF | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Static object → glTF | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Animated object (.cga) → glTF | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Textures + PBR materials | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Object collider export | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Level export into JSON description | — | — | — | — | ✔ | — |
| Game archive unpacking | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Full color of voxel surfaces | — | — | — | — | ✔ | — |

Legend: ✔ = available · — = not available · "in progress" = partial / work in progress.

Warhead and Wars use the same engine and data format as Crysis 1, so they are handled the same way as Crysis 1. Level export to JSON and the full color of voxel surfaces currently work with Remastered data.

---

## System Requirements

- **Windows 10/11 (64-bit)**
- **~2 GB of free disk space** (Python + packages + game data processing)
- **Internet connection during installation** — no manual Python install is needed; the installer provisions Python 3.11 automatically
- **tkinter** — comes with the provisioned Python; used by the GUI for native OS file dialogs

---

## Quick Start

### Installation

Run `Install_CrisTical.bat` once. The installer automatically downloads and sets up:

- **uv** — the package manager used to provision the environment
- **Python 3.11** — full uv-managed build (includes **tkinter** for the native OS file dialogs in the GUI)
- Python libraries from `requirements.txt` (numpy, pillow, bpy, dearpygui, pygltflib + numba and cupy-cuda12x as Twofish backends for Crysis 3 .pak + **mcp[cli]** for the MCP bridge)
- Assimp 6.0.5 (assimp.dll)
- 7-Zip (7za.exe) — for .dba extraction from Animations.pak
- **MCP bridge check** — verifies that `scripts/MCP_CrisTical_bridge.py` imports cleanly (FastMCP ready)

The installer is **idempotent** — re-running it skips already-installed parts and
only rebuilds the venv when it is missing or outdated. All download caches live
inside the project (`.cache/uv`), the venv is created in `cris_env/`, and the
Python interpreter itself is provisioned per-user by uv. Internet is required
only during installation.

### Launch GUI

```batch
Run_CrisTical.bat
```

Opens the control panel: select .cdf → auto-scan model → configure animations/textures → convert. All actions are logged.

### CLI Mode

```batch
Run_CrisTical.bat --cdf alien.cdf --gamedir "F:\Games\Crysis\Game" --out output
Run_CrisTical.bat --cdf alien.cdf --gamedir "F:\Games\Crysis\Game" --split-anim --glb

# Static geometry (vegetation, props) — vertex colors + tangents preserved
Run_CrisTical.bat --cgf palm_tree_large_a.cgf --gamedir "F:\Games\Crysis_Remastered\Game"
Run_CrisTical.bat --cgf bush.cgf --gamedir "F:\Games\Crysis\Game" --glb
```

### MCP Mode (native tools for AI clients)

`scripts/MCP_CrisTical_bridge.py` is a FastMCP stdio server that replaces
`Run_CrisTical.bat` for MCP-driven usage — the same dispatch, environment
and pipeline, but as native tools with verbose reports. Humans keep the
`.bat`; AI clients (Kilo Code, Claude, Cursor, ...) get the bridge.

| Tool | Description |
|------|-------------|
| `cristical_convert` | Convert `.cdf`/`.chr`/`.cgf`/`.cga` → glTF/GLB; returns the executed command, full pipeline log, exit code, duration and the list of files written |
| `cristical_scan` | Dry-run inspection: chunk versions, bone counts, mesh stats, materials, animations — no files written |
| `cristical_list` | List conversion output files with sizes and mtimes |
| `cristical_version` | Environment report: venv, scripts, Bin/ tools, mcp library |
| `cristical_catalog` | Browse assets inside the game archives by type and path (models/animations/textures/materials) |
| `cristical_level2json` | Export a level into a JSON description through the same pipeline as level2json.py |
| `cristorical_unpack` | Unpack .pak archives into plain folders (dry-run / rewrite / wait / status / crypto) |

Registration in Kilo Code (`kilo.json`, `mcp` section):

```json
"cristical": {
  "type": "local",
  "command": ["K:\\work\\CrisTical_Crysis3DConverter\\cris_env\\Scripts\\python.exe",
              "K:\\work\\CrisTical_Crysis3DConverter\\scripts\\MCP_CrisTical_bridge.py"],
  "enabled": true,
  "timeout": 600000
}
```

---

## Interface

![Interface](docs/001_interface.png)

The control panel shows:

- **CDF Status** — Valid (v0905) / Valid (v0903) / Invalid — animation controller version
- **Game Dir Status** — green (valid) / yellow (no markers) / red (not found)
- **Model Scan** — Bones, Primitives, Attachments, Materials, Animations
- **Auto-detection** — GUI walks up from .cdf to find game root automatically
- **Native dialogs** — file/folder pickers use the OS dialog (tkinter); the built-in dialog only appears if tkinter is unavailable
- **CLI Preview** — shows the equivalent command-line command
- **Edition & options** — auto game-version detection (Auto / Crysis 1 / Warhead / Crysis 2 / Crysis 3 / Remastered / Wars), texture mode (Auto-PBR / Keep as-is / Skip), "Output .glb" and "Extract collision mesh" checkboxes, and a "Map" tab to export a level into a JSON description

---

## Why Game Folders (`--gamedir`) Are Needed

The converter searches for three types of data in the specified folders:

1. **Textures** — `.dds`/`.png` files matching paths in `.mtl`. Searched in `--gamedir` order.
2. **Animations** — `.dba` file from `.cal` (`$TracksDatabase`). Also searched in `--gamedir`.
3. **Materials** — `.mtl` file next to `.cdf` or in game folders.

### How to Choose Folders

Recommended order: **Remaster → original → unpacked content**.

```batch
# Remaster only (PNG textures, v0905 animations)
--gamedir "F:\Games\Crysis_Remastered\Game"

# Remaster + original (for legacy .mtl/textures missing in Remaster)
--gamedir "F:\Games\Crysis_Remastered\Game" --gamedir "F:\Games\Crysis\Game"

# + unpacked content (split-DDS textures from .pak)
--gamedir "F:\Games\Crysis_Remastered\Game" --gamedir "F:\Games\Crysis\Game" --gamedir "F:\Games\Crysis_Remastered\__CONTENT\objectsch.pak_Unpacked"
```

**Rule:** put the folder with best-quality textures first (Remaster = 4K PNG). Others are fallbacks for missing files.

![Output](docs/002_output.png)

---

## CLI Flags

| Flag | Description |
|------|-------------|
| `--cdf <path>` | Path to `.cdf` or `.chr` file (animated character) |
| `--cgf <path>` | Path to static `.cgf` file (vegetation/props, no skeleton) |
| `--gamedir <dir>` | Game root folder (repeatable; order = priority) |
| `--out <path>` | Output directory (default: `output/`) |
| `--no-anim` | Skip animation injection |
| `--no-tex` | Skip texture conversion |
| `--split-anim` | Export each animation as a separate glTF |
| `--glb` | Output as binary `.glb` instead of `.gltf`+`.bin` |
| `--cga <path>` | Path to an animated `.cga` file |
| `--caf <path>` | Inject a loose `.caf` clip on top of the animation databases (repeatable) |
| `--no-root-motion` | Drop the root-bone position track from `.caf` clips |
| `--extract-collision` | Also write the engine collider into `<name>_collision.gltf` |
| `--level <path>` | Export a level into a JSON description (level2json) |
| `--help` | Show help |

For static `.cgf` conversion the vertex colors (COLOR_0) are the raw RGBA
bytes stored per vertex — the convention used by Crysis vegetation data for
detail bending (R=edge stiffness, G=leaf phase, B=branch stiffness, A=AO).

---

## Output

All files are written to the output directory:

```
output/
├── model_name.gltf        # glTF 2.0 scene (or .glb with --glb flag)
├── model_name.bin         # Binary buffer (not present with --glb)
├── model_name.log         # Conversion log
├── material_diffuse.png   # Diffuse texture
├── material_normal.png    # Normal map (Z-channel reconstructed)
├── material_emiss.png     # Emission map (extracted from Diffuse alpha)
├── material_specular.png  # Specular map
└── material_gloss.png     # Gloss map (DDNA alpha)
```

With `--split-anim`, each animation is placed in a `model_name_anims/` subdirectory.

---

## Formats

| File | Format | Versions |
|------|--------|----------|
| .cdf | Character Definition (XML) | Crysis 1-3, Remastered and others |
| .chr | Crysis binary character chunk file | v0744, v0745 |
| .cgf | Static chunk file (Mesh/Node/DataStream/MeshSubsets) | v0744, v0745 |
| .cga | Animated geometry | v0744, v0745 |
| .anm | CGA animation (TCB3 controllers) | — |
| .dba | CryAnimation Database | v0903, v0905 |
| .caf | Single animation clip | — |
| .chrparams | Character animation setup (XML) | — |
| .lmg | Locomotion groups (XML) | — |
| .bspace / .comb | Blend-space (XML) | — |
| .mtl | XML material | single/multi-material |
| .dds | DirectDraw Surface (split/combined) | DXT1, DXT5, ATI2N/3DC, RGBA8, L8 |
| .pak | Game archives (zip/XXTEA/Twofish, encrypted) | C1/Remaster, C2, C3 |
| .xmlb | Binary CryXmlB/pbxml | C2/C3 |
| .cal | Character Animation List | plain text |

---

## Project Structure

```
CrisTical_Crysis3DConverter/
├── Install_CrisTical.bat          # Environment installer
├── Run_CrisTical.bat              # Launcher (GUI / CLI)
├── requirements.txt               # Python dependencies (incl. mcp[cli])
├── README.md / README.ru.md      # Documentation
├── scripts/
│   ├── cristical_gui.py          # Control panel (DearPyGui)
│   ├── cdf2gltf.py               # Conversion orchestrator (characters .cdf/.chr)
│   ├── cgf2gltf.py               # Conversion orchestrator (static .cgf)
│   ├── cga2gltf.py               # Conversion orchestrator (animated .cga + .anm)
│   ├── level2json.py             # Level -> JSON export (incl. voxels)
│   ├── unpack_crysis.py          # Game archive .pak unpacking
│   ├── MCP_CrisTical_bridge.py   # MCP server (FastMCP): convert/scan/catalog/list/version/level2json/unpack
│   └── cristical_core/           # Converter library
│       ├── crychr.py              # .chr/.cdf parser (CompiledBones, DataStream, CDF XML)
│       ├── crycgf.py              # Static .cgf parser (Mesh/Node/MtlName/DataStream/MeshSubsets, COLOR0/COLOR1 streams)
│       ├── crycga.py              # Animated .cga parser
│       ├── crydba.py              # DBA v0903/v0905 parser (SmallTree64, Bitset, TCB)
│       ├── crycaf.py              # Single .caf clip parser
│       ├── crygltf.py             # glTF 2.0 writer (skeleton + mesh + static + COLOR_0 + TANGENT)
│       ├── gltf_anim.py           # Animation injector + quaternion hemisphere fix
│       ├── crycollision.py        # Engine collider decoder -> <name>_collision.gltf
│       ├── crychrparams.py / crylmg.py / crybspace.py / crytcb.py / crycodecs.py
│       ├── cryvfs.py / crypak.py / twofish_fast.py / pak_unpack.py / cryxmlb.py
│       ├── game_profile.py / mtl_resolve.py / path_resolve.py
│       └── ...                    # full module list lives in scripts/cristical_core/
├── resources/
│   └── ModeSevenBETAVHS.ttf      # Interface font
├── docs/                          # Screenshots
├── .cache/                        # uv download cache (installer)
├── Bin/                           # Native tools (installer)
├── cris_env/                      # Python venv (created by installer)
├── output/                        # Output folder
└── temp/                          # Temporary files
```

---

## Acknowledgements

- [Khronos glTF 2.0 Specification](https://github.com/pygfx/gltflib)
- [BCnEncoder.NET](https://github.com/Nominom/BCnEncoder.NET) — BC decoding algorithms
- [DDS-Unsplitter](https://github.com/Markemp/DDS-Unsplitter) — reference split-DDS implementation
