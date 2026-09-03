#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MCP_CrisTical_bridge.py — CrisTical MCP server (FastMCP / stdio)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.0

Native MCP bridge for the CrisTical Crysis->glTF converter. It fully
replaces Run_CrisTical.bat for MCP-driven usage: the .bat's orchestration
logic is reimplemented inside this bridge (same script dispatch, same
PATH/temp/portability environment, same argument semantics), so MCP clients
(Kilo Code, Claude, Cursor, ...) get a first-class tool instead of a
batch-file wrapper.

Division of responsibility:
    - Human user  -> Run_CrisTical.bat (GUI / CLI mode)
    - MCP clients -> this bridge (identical pipeline, native MCP output)

Dispatch rules (inherited from Run_CrisTical.bat):
    .cdf / .chr -> scripts/cdf2gltf.py   (animated characters)
    .cgf        -> scripts/cgf2gltf.py   (static geometry)
    .cga        -> scripts/cga2gltf.py   (animated geometry)

Environment replicated from the .bat: Bin/ (assimp.dll, 7za.exe) prepended
to PATH, TMP/TEMP isolated inside the project, UV_CACHE_DIR inside the
project, cwd = project root, project venv interpreter.

Each conversion launches the chosen converter script as a subprocess of the
venv interpreter (isolation: a converter crash cannot take down the MCP
server; the scripts' own temp/ cleanup semantics are preserved) and the tool
returns:

    - the full argv line actually executed,
    - every stdout/stderr line the pipeline printed (verbose log),
    - exit code and duration,
    - the list of output files written (parsed from the pipeline log +
      directory scan), including a directory listing when asked.

Tools:
    cristical_convert   — main entry: one call for .cdf/.chr/.cgf/.cga
    cristical_scan      — dry-run inspection: parse input, report what
                          a conversion would produce (no files written)
    cristical_list      — list files in an output directory with sizes
    cristical_version   — environment/version report (python, scripts, bat)

Run standalone (stdio MCP server):
    python MCP_CrisTical_bridge.py

Kilo Code / kilo.json registration (local type):
    "cristical": {
      "type": "local",
      "command": ["K:\\work\\CrisTical_Crysis3DConverter\\cris_env\\Scripts\\python.exe",
                  "K:\\work\\CrisTical_Crysis3DConverter\\scripts\\MCP_CrisTical_bridge.py"],
      "enabled": true,
      "timeout": 600000
    }
"""

import os
import sys
import glob
import json
import shutil
import datetime
import threading

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Paths — resolved from this file's location so the bridge is position independent
# ---------------------------------------------------------------------------

PROJ_DIR = os.path.dirname(SCRIPT_DIR)                       # project root
BAT_PATH = os.path.join(PROJ_DIR, "Run_CrisTical.bat")   # human entry point (GUI/CLI); bridge does NOT call it
VENV_PYTHON = os.path.join(PROJ_DIR, "cris_env", "Scripts", "python.exe")
BIN_DIR = os.path.join(PROJ_DIR, "Bin")
OUTPUT_DIR = os.path.join(PROJ_DIR, "output")

TOOLS = {
    "cdf": os.path.join(SCRIPT_DIR, "cdf2gltf.py"),   # animated characters
    "cgf": os.path.join(SCRIPT_DIR, "cgf2gltf.py"),   # static geometry
    "cga": os.path.join(SCRIPT_DIR, "cga2gltf.py"),   # animated geometry
}

UNPACK_SCRIPT = os.path.join(SCRIPT_DIR, "unpack_crysis.py")  # standalone pak unpacker

mcp = FastMCP(
    "cristical",
)

# Files produced by the last conversion (for cristical_list default).
_LAST_OUTPUT = {"files": [], "dir": None}

_VALID_INPUTS = {
    "cdf": (".cdf", ".chr"),   # .chr is accepted by --cdf (character path)
    "cgf": (".cgf",),
    "cga": (".cga",),
}


# ---------------------------------------------------------------------------
# Environment: replicate Run_CrisTical.bat portability setup
# ---------------------------------------------------------------------------

def _bat_env():
    """Environment for the subprocess: PATH with Bin first, project temp dirs.

    Mirrors the .bat exactly: UV_CACHE_DIR, TMP/TEMP inside the project,
    Bin (assimp.dll / 7za.exe) prepended to PATH, cwd = project root.
    """
    env = os.environ.copy()
    env["PATH"] = BIN_DIR + os.pathsep + env.get("PATH", "")
    env["UV_CACHE_DIR"] = os.path.join(PROJ_DIR, ".cache", "uv")
    for d in (os.path.join(PROJ_DIR, "temp"), os.path.join(PROJ_DIR, ".cache"),
              os.path.join(PROJ_DIR, "output")):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    env["TMP"] = os.path.join(PROJ_DIR, "temp")
    env["TEMP"] = env["TMP"]
    return env


def _detect_mode(path):
    """Pick the converter from the file extension — same rule as the .bat."""
    ext = os.path.splitext(path)[1].lower()
    for mode, exts in _VALID_INPUTS.items():
        if ext in exts:
            return mode
    return None


def _build_argv(mode, path, gamedir, out, anim, tex, split_anim, glb):
    """Build the exact CLI argv the .bat would pass to the chosen script."""
    flag = "--" + mode
    argv = [flag, path]
    for d in gamedir:
        argv += ["--gamedir", d]
    if out:
        argv += ["--out", out]
    if anim is False:
        argv.append("--no-anim")
    if tex is False:
        argv.append("--no-tex")
    if split_anim:
        argv.append("--split-anim")
    if glb:
        argv.append("--glb")
    return argv


def _run_converter(mode, argv, timeout):
    """Run the chosen converter script with argv via the venv interpreter.

    This is the .bat's dispatch logic inlined: instead of letting the batch
    file pick scripts\\cdf2gltf.py|cgf2gltf.py|cga2gltf.py by flag sniffing,
    the bridge already knows the mode from the file extension and launches
    ``<venv python> <script> <argv>`` directly. The environment (PATH with
    Bin/ first, project TMP/TEMP, cwd = project root) is what Run_CrisTical.bat
    would have set.

    Output is captured line by line (stdout+stderr merged); lines are
    returned fully (no truncation) along with the exit code. Real-time
    streaming to MCP progress is not part of stdio tool semantics, so the
    full log is returned at once — verbose by design.
    """
    cmd = [VENV_PYTHON, TOOLS[mode]] + argv
    started = datetime.datetime.now()
    try:
        import subprocess
        proc = subprocess.Popen(
            cmd,
            cwd=PROJ_DIR,
            env=_bat_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        lines = []
        reader_error = []

        def _reader():
            try:
                for line in proc.stdout:
                    lines.append(line.rstrip("\r\n"))
            except Exception as exc:  # pragma: no cover
                reader_error.append(str(exc))

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return (lines + ["[TIMEOUT] conversion exceeded %s s — process killed" % timeout],
                    -1, started)
        t.join(timeout=5)
        if reader_error:
            lines.append("[WARN] stdout reader failed: %s" % reader_error[0])
        return lines, rc, started
    except Exception as exc:
        return (["[ERROR] failed to launch %s: %s" % (TOOLS[mode], exc)], -1, started)


def _collect_outputs(out_gltf, gamedir, started):
    """Collect the files this run produced: log-parsed + directory scan.

    Sources:
      1. Every 'Output:' / 'Done:' / 'Log:' line from the pipeline log.
      2. Fallback scan of the output directory for files newer than start.
    """
    files = set()

    # --- primary: paths named by the pipeline itself ---
    # (filled by caller from parsed log lines)

    # --- fallback: anything created after `started` near the output ---
    if out_gltf:
        base = os.path.dirname(out_gltf)
        name = os.path.splitext(os.path.basename(out_gltf))[0]
    else:
        base = OUTPUT_DIR
        name = ""
    for d in {base, os.path.join(base, name + "_anims")} if os.path.isdir(base) or os.path.isdir(
            os.path.join(base, name + "_anims")) else []:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            p = os.path.join(d, f)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) >= (started.timestamp() - 60):
                    files.add(p)
            except OSError:
                continue
    return sorted(files)


def _fmt_files(files):
    """Format the output file list with sizes and mtimes."""
    if not files:
        return "(no output files detected)"
    lines = []
    for p in files:
        try:
            st = os.stat(p)
            lines.append("  %-70s %10d bytes  %s" % (
                p, st.st_size,
                datetime.datetime.fromtimestamp(st.st_mtime).strftime("%H:%M:%S")))
        except OSError:
            lines.append("  %s" % p)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------

def _norm_dirs(game_dirs):
    """Normalize gamedir input (string | list) to a list of existing dirs."""
    if not game_dirs:
        return []
    if isinstance(game_dirs, str):
        game_dirs = [game_dirs]
    out = []
    for d in game_dirs:
        if not d:
            continue
        d = os.path.normpath(os.path.abspath(d))
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def _resolve_virtual(path, game_dirs):
    """Materialize a virtual (in-pak) path through the VFS.

    Real files pass through unchanged. A virtual path without any
    gamedir, or one that is not found in the mounted VFS, raises a
    ValueError with a human-readable explanation (the MCP tool turns
    it into the error payload the agent sees).
    """
    from cristical_core.path_resolve import resolve_geometry_path
    try:
        resolved = resolve_geometry_path(path, game_dirs)
    except FileNotFoundError as e:
        raise ValueError(str(e))
    return resolved


def _require_input(path, game_dirs=None):
    """Validate the input file: absolute, exists (real or in-pak), known
    extension. Virtual paths are materialized to a real temp file first."""
    if not path:
        raise ValueError("path is required (absolute path to .cdf/.chr/.cgf/.cga "
                         "or a virtual in-pak path like Objects/3dtext/a.cgf)")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        dirs = _norm_dirs(game_dirs)
        if not dirs:
            raise ValueError(
                "file not found: %s\n"
                "if this is a virtual path inside a game .pak, pass gamedir "
                "(the game root folder) so the bridge can materialize it" % path)
        path = _resolve_virtual(path, dirs)
    mode = _detect_mode(path)
    if mode is None:
        raise ValueError(
            "unsupported extension '%s' — expected .cdf, .chr, .cgf or .cga: %s"
            % (os.path.splitext(path)[1], path))
    return path, mode


def _default_out(path, out, glb):
    """Default output: <project>/output/<name>.gltf|.glb — same as GUI default."""
    if out:
        return os.path.abspath(out)
    base = os.path.splitext(os.path.basename(path))[0]
    ext = ".glb" if glb else ".gltf"
    return os.path.join(OUTPUT_DIR, base + ext)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def cristical_convert(
    path: str,
    gamedir: str | list[str] | None = None,
    out: str | None = None,
    anim: bool = True,
    textures: bool = True,
    split_anim: bool = False,
    glb: bool = False,
    timeout: int = 600,
) -> str:
    """Convert a Crysis file to glTF 2.0 / GLB — the main CrisTical pipeline.

    Dispatches exactly like Run_CrisTical.bat (logic inlined in the bridge —
    no .bat call, no shell indirection):
      - .cdf / .chr -> animated character (skeleton + skin + attachments +
        .mtl materials + DDS textures + .dba animations)
      - .cgf        -> static geometry (vegetation/props, vertex colors
        COLOR_0 + tangents preserved)
      - .cga        -> animated geometry (node hierarchy + .anm animations)

    The chosen converter script runs as a subprocess of the project venv
    interpreter with the same environment the .bat would set (Bin/ on PATH,
    project temp isolation), so the pipeline is identical to CLI usage.

    Args:
        path: absolute path to the .cdf / .chr / .cgf / .cga file, or a
            virtual in-pak path like ``Objects/3dtext/a.cgf`` (requires
            gamedir; the bridge materializes it through the VFS).
        gamedir: game root folder(s) for textures/animations/materials
            lookup — a single string or a list. Priority order is kept
            (recommended: Remaster first, original second, unpacked
            content last). Repeatable, same as the --gamedir CLI flag.
            Also used to materialize virtual paths from game .paks
            (Crysis 1/2/3/Remastered).
        out: output path. Default: <project>/output/<name>.gltf (or .glb).
        anim: inject animations (default True; False = --no-anim).
        textures: convert .mtl materials + DDS->PNG (default True; False = --no-tex).
        split_anim: one glTF per animation (characters only; default False).
        glb: single binary .glb instead of .gltf + .bin (default False).
        timeout: hard limit in seconds for the subprocess (default 600).

    Returns:
        Verbose report: executed command line, full pipeline log,
        exit code, duration, and the list of output files with sizes.
    """
    import subprocess

    dirs = _norm_dirs(gamedir)
    src, mode = _require_input(path, dirs)
    out_gltf = _default_out(src, out, glb)
    out_dir = os.path.dirname(out_gltf)
    os.makedirs(out_dir, exist_ok=True)

    argv = _build_argv(mode, src, dirs, out_gltf, anim, textures, split_anim, glb)
    cmd_display = "%s %s %s" % (os.path.basename(VENV_PYTHON),
                                os.path.basename(TOOLS[mode]), " ".join(argv))

    header = [
        "=" * 70,
        "CrisTical MCP bridge — conversion",
        "  mode:      %s (%s)" % (mode, TOOLS[mode]),
        "  input:     %s" % src,
        "  gamedir:   %s" % ("; ".join(dirs) if dirs else "(none)"),
        "  output:    %s" % out_gltf,
        "  options:   anim=%s tex=%s split=%s glb=%s" % (anim, textures, split_anim, glb),
        "  execute:   %s" % cmd_display,
        "=" * 70,
    ]

    lines, rc, started = _run_converter(mode, argv, timeout)
    dur = (datetime.datetime.now() - started).total_seconds()

    # Collect every file this run produced:
    #   1. all .gltf/.glb/.bin/.log/.png/.json named in the log lines,
    #   2. plus a directory scan of the output dir (files newer than start),
    #      which catches textures and split-anim outputs not named verbatim.
    named = set()
    for ln in lines:
        for token in ln.replace(",", " ").split():
            if token.lower().endswith((".gltf", ".glb", ".bin", ".log", ".png", ".json")):
                named.add(os.path.abspath(os.path.join(out_dir, os.path.basename(token))))
    named = {p for p in named if os.path.isfile(p)}
    scanned = _collect_outputs(out_gltf, dirs, started)
    files = sorted(named | set(scanned))
    _LAST_OUTPUT["files"] = files
    _LAST_OUTPUT["dir"] = out_dir

    report = header + [
        "",
        "--- PIPELINE LOG (full, verbose) ---",
    ] + lines + [
        "",
        "--- RESULT ---",
        "exit code:  %s" % ("OK (0)" if rc == 0 else "FAILED (%s)" % rc),
        "duration:   %.1f s" % dur,
        "",
        "OUTPUT FILES (%d):" % len(files),
        _fmt_files(files),
    ]
    return "\n".join(report)


@mcp.tool()
def cristical_scan(
    path: str,
    gamedir: str | list[str] | None = None,
) -> str:
    """Inspect a Crysis file WITHOUT converting — dry-run analysis.

    Reports what a conversion would produce: chunk versions, bone counts,
    mesh statistics, referenced materials, animations in the .dba, the
    resolved .mtl path, and problems (missing textures, unreadable chunks).

    Works with .cdf, .chr, .cgf, .cga, .anm, .dba, .cal, .mtl.

    Args:
        path: absolute path to the file to inspect, or a virtual in-pak
            path (requires gamedir; materialized through the VFS).
        gamedir: optional game root folder(s) for resolving references
            (.dba lookup, .mtl resolution, virtual paths) — string or list.

    Returns:
        Human-readable inspection report; no files are written.
    """
    if not path:
        raise ValueError("path is required")
    dirs = _norm_dirs(gamedir)
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        if not dirs:
            raise ValueError(
                "file not found: %s\n"
                "if this is a virtual path inside a game .pak, pass gamedir "
                "(the game root folder) so the bridge can materialize it" % src)
        src = _resolve_virtual(src, dirs)
        orig_input = path
    else:
        orig_input = src

    lines = [
        "=" * 70,
        "CrisTical MCP bridge — scan (dry run, nothing is written)",
        "  input:   %s" % orig_input,
        "  gamedir: %s" % ("; ".join(dirs) if dirs else "(none)"),
        "=" * 70,
    ]

    ext = os.path.splitext(src)[1].lower()
    script = TOOLS.get(_detect_mode(src))
    if script:
        lines.append("converter: %s" % script)
    lines.append("")

    # Lightweight parsing via cristical_core (read-only library calls —
    # safe: no pipeline side effects, no files written).
    try:
        if ext in (".cdf", ".chr"):
            from cristical_core import read_chr_or_cdf, read_cdf
            if ext == ".cdf":
                info = read_cdf(src)
                lines.append("CDF model: %s" % info.get("model_path", "?"))
                for aname, apath in info.get("skin_attachments", []):
                    lines.append("CDF attachment: %s <- %s" % (aname, apath))
            data = read_chr_or_cdf(src)
            bones = data["skeleton"]
            prims = data["mesh"]["primitives"]
            lines.append("Bones:      %d" % len(bones))
            lines.append("Primitives: %d (total verts %d)" % (
                len(prims), sum(len(p["positions"]) for p in prims)))
            mats = sorted(set(p.get("material", "?") for p in prims))
            lines.append("Materials referenced: %s" % (", ".join(mats[:20]) or "none"))
            cal = os.path.splitext(src)[0] + ".cal"
            lines.append("CAL file:   %s" % (cal if os.path.isfile(cal) else "not found"))
        elif ext == ".cgf":
            from cristical_core import read_cgf_meshes
            from cristical_core.mtl_resolve import resolve_mtl
            prims = read_cgf_meshes(src)
            lines.append("Primitives: %d (total verts %d)" % (
                len(prims), sum(len(p["positions"]) for p in prims)))
            for p in prims[:10]:
                lines.append("  %-24s mat=%-24s verts=%d colors=%s" % (
                    p["node_name"], p["material"], len(p["positions"]),
                    "yes" if p["colors"] else "no"))
            mtl = resolve_mtl(src, dirs, strip_suffixes=True, verbose=False, log=lambda *_: None)
            lines.append("Resolved MTL: %s" % mtl)
        elif ext == ".cga":
            from cristical_core.crycga import read_cga
            data = read_cga(src)
            lines.append("Nodes: %d  Primitives: %d" % (data["num_nodes"], data["num_prims"]))
            base = os.path.splitext(os.path.basename(src))[0]
            anms = [f for f in os.listdir(os.path.dirname(src) or ".")
                    if f.lower().endswith(".anm") and f.lower().startswith(base.lower())]
            lines.append("Sibling .anm files: %s" % (", ".join(sorted(anms)) or "none"))
        elif ext == ".anm":
            from cristical_core.crycga import read_anm
            data = read_anm(src)
            lines.append("ANM nodes: %d  controller chunks: %d" % (
                data["num_nodes"], data["controller_chunks"]))
            lines.append("Tracks: pos=%d rot=%d  keys: pos=%d rot=%d" % (
                data["num_pos_tracks"], data["num_rot_tracks"],
                data["total_keys_pos"], data["total_keys_rot"]))
        elif ext == ".dba":
            from cristical_core import read_dba, read_dba_version
            dba = read_dba(src)
            lines.append("DBA version: %s" % read_dba_version(src))
            lines.append("Animations:  %d" % len(dba.animations))
            for a in dba.animations[:30]:
                lines.append("  %s" % os.path.basename(a.name))
            if len(dba.animations) > 30:
                lines.append("  ... and %d more" % (len(dba.animations) - 30))
        elif ext == ".cal":
            # Inline .cal parsing (same logic as cdf2gltf.parse_cal) so the
            # scan stays side-effect free: importing cdf2gltf would run its
            # module-level _clean_temp().
            import re
            cal = {}
            with open(src, "r", encoding="utf-8", errors="replace") as f:
                for cal_line in f:
                    cal_line = cal_line.strip()
                    if not cal_line or cal_line.startswith("//"):
                        continue
                    m = re.match(r"^\$(\w+)\s*=\s*(.+)$", cal_line)
                    if m:
                        cal[m.group(1)] = m.group(2).strip()
            lines.append("CAL entries: %s" % json.dumps(cal, indent=2))
        elif ext == ".mtl":
            from cristical_core.mtl_resolve import resolve_mtl
            mtl = resolve_mtl(src, dirs, strip_suffixes=False, verbose=False, log=lambda *_: None)
            lines.append("MTL resolved: %s" % mtl)
        else:
            lines.append("unsupported extension for scan: %s" % ext)
    except Exception as exc:
        lines.append("ERROR during scan: %s" % exc)

    lines += ["", "Scan complete — no files were written."]
    return "\n".join(lines)


@mcp.tool()
def cristical_list(
    directory: str | None = None,
) -> str:
    """List files produced by CrisTical — default: last conversion output.

    Args:
        directory: optional directory to list instead of the last output
            (e.g. 'K:/.../output' or any folder with converted files).

    Returns:
        Directory path plus every file (name, size, modified time),
        newest last; or a notice that nothing was recorded yet.
    """
    d = directory or _LAST_OUTPUT.get("dir") or OUTPUT_DIR
    d = os.path.abspath(d)
    if not os.path.isdir(d):
        return "Directory not found: %s" % d
    lines = ["Output dir: %s" % d, "-" * 70]
    files = []
    for f in os.listdir(d):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            st = os.stat(p)
            files.append((st.st_mtime, f, st.st_size))
    files.sort()
    if not files:
        lines.append("(empty)")
    for mt, f, sz in files:
        lines.append("%-44s %10d bytes  %s" % (
            f, sz, datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M:%S")))
    # nested anims dir
    for f in os.listdir(d):
        sub = os.path.join(d, f)
        if os.path.isdir(sub) and f.endswith("_anims"):
            lines.append("")
            lines.append("subdir %s:" % f)
            for a in sorted(os.listdir(sub)):
                p = os.path.join(sub, a)
                if os.path.isfile(p):
                    lines.append("  %-42s %10d bytes" % (a, os.path.getsize(p)))
    return "\n".join(lines)


@mcp.tool()
def cristical_version() -> str:
    """Environment & version report: bridge, converter scripts, Python,
    Bin/ tools, output dir.

    Use this first to sanity-check that the MCP bridge sees a healthy
    CrisTical installation before running conversions.

    Returns:
        Verbose environment report.
    """
    lines = [
        "=" * 70,
        "CrisTical MCP bridge v1.0",
        "=" * 70,
        "project root:  %s" % PROJ_DIR,
        "bridge file:   %s" % os.path.abspath(__file__),
        "venv python:   %s  [%s]" % (
            VENV_PYTHON, "OK" if os.path.isfile(VENV_PYTHON) else "MISSING"),
        "output dir:    %s  [%s]" % (OUTPUT_DIR, "OK" if os.path.isdir(OUTPUT_DIR) else "missing"),
        "",
        "converter scripts (dispatch table):",
    ]
    for mode, script in TOOLS.items():
        lines.append("  --%-4s %s  [%s]" % (mode, script,
                     "OK" if os.path.isfile(script) else "MISSING"))
    lines += ["", "Bin/ tools:"]
    for tool in ("assimp.dll", "7za.exe"):
        p = os.path.join(BIN_DIR, tool)
        lines.append("  %-12s %s  [%s]" % (tool, p, "OK" if os.path.isfile(p) else "MISSING"))
    lines += ["", "python:        %s" % sys.version.split()[0],
              "mcp library:    FastMCP (mcp package)"]
    try:
        import mcp as _mcp
        lines[-1] += " v%s" % getattr(_mcp, "__version__",
                                       getattr(_mcp, "version", "?"))
    except Exception:
        pass
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Content catalog (pak-only discovery)
# ---------------------------------------------------------------------------

_CATALOG_EXTS = {
    "models": (".cdf", ".chr", ".cgf", ".cga"),
    "anims": (".dba", ".caf", ".anm", ".lmg", ".cal", ".chrparams"),
    "textures": (".dds",),
    "materials": (".mtl",),
    "all": None,
}


def _catalog(gamedirs, kind="models", prefix="", limit=200):
    """Return (total, listing, by_ext) of VFS asset paths in the shared index."""
    from cristical_core.cryvfs import mount_game
    idx = mount_game([str(d) for d in gamedirs])
    exts = _CATALOG_EXTS.get(kind) or None
    pre = prefix.replace("\\", "/").lstrip("/").lower()
    matched = []
    for k in idx.keys():
        if pre and not k.startswith(pre):
            continue
        if exts is not None and not k.endswith(exts):
            continue
        matched.append(k)
    matched.sort()
    by_ext = {}
    for k in matched:
        e = os.path.splitext(k)[1].lower()
        by_ext[e] = by_ext.get(e, 0) + 1
    return matched, len(matched), by_ext


@mcp.tool()
def cristical_catalog(
    gamedir: str | list[str] | None = None,
    kind: str = "models",
    prefix: str = "",
    limit: int = 200,
) -> str:
    """Browse / catalog game content through the VFS (loose files AND .pak).

    Lets you discover models or side assets inside packed game data without
    guessing a virtual path first — list by kind and (optionally) a path
    prefix, then feed the resulting virtual path back to cristical_convert.

    Args:
        gamedir: game data root(s) (priority order). At least one required.
        kind: "models" (.cdf/.chr/.cgf/.cga), "anims", "textures",
            "materials", or "all".
        prefix: optional virtual-dir prefix filter, e.g.
            "objects/characters/alien" (case-insensitive, forward slashes).
            Empty = from the root.
        limit: max asset paths to print (0 = print none, only counts).

    Returns:
        Per-extension counts plus the sorted list of matching virtual paths
        (truncated to ``limit``), so you can pick one and pass it to
        cristical_convert with the same gamedir.
    """
    dirs = _norm_dirs(gamedir)
    if not dirs:
        raise ValueError("gamedir is required to browse game content")
    if kind not in _CATALOG_EXTS:
        raise ValueError("unknown kind '%s' — expected %s"
                         % (kind, ", ".join(sorted(_CATALOG_EXTS))))
    matched, total, by_ext = _catalog(dirs, kind=kind, prefix=prefix, limit=limit)
    lines = [
        "=" * 70,
        "CrisTical MCP bridge — content catalog",
        "  gamedir: %s" % ("; ".join(dirs)),
        "  kind:    %s   prefix: %s" % (kind, prefix or "(root)"),
        "  total:   %d assets" % total,
        "  by type: %s" % (", ".join("%s=%d" % (e, n) for e, n in sorted(by_ext.items())) or "—"),
        "-" * 70,
    ]
    if limit and matched:
        lines += ["  " + p for p in matched[:limit]]
        if total > limit:
            lines.append("  ... and %d more (narrow 'prefix' or raise 'limit')"
                         % (total - limit))
    elif matched:
        lines.append("  (counts only — pass limit > 0 to list)")
    else:
        lines.append("  (no assets match kind=%s prefix=%s)" % (kind, prefix or "(root)"))
    lines.append("Tip: feed one path above to cristical_convert with this gamedir.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Unpack: same subprocess mechanism as the converters (scripts/unpack_crysis.py)
# ---------------------------------------------------------------------------

def _unpack_log_path(path: str, out: str | None) -> str:
    """Deterministic log path for an unpack target (status polls find it)."""
    import hashlib
    key = "%s|%s" % (os.path.abspath(path).lower(),
                    (os.path.abspath(out) if out else "").lower())
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
    return os.path.join(PROJ_DIR, "temp", "unpack_%s.log" % h)


def _start_detached_unpack(argv_extra: list[str], log_path: str) -> str:
    """Launch unpack_crysis.py detached; MCP call returns immediately."""
    import subprocess
    cmd = [VENV_PYTHON, UNPACK_SCRIPT] + argv_extra
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    log_fh = open(log_path, "a", encoding="utf-8")
    try:
        subprocess.Popen(
            cmd,
            cwd=PROJ_DIR,
            env=_bat_env(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            shell=False,
        )
    finally:
        log_fh.close()
    return log_path


@mcp.tool()
def cristorical_unpack(path: str, out: str | None = None, dry_run: bool = False,
                       rewrite: bool = False, wait: bool = False,
                       status: bool = False, crypto: str = "auto") -> str:
    """Unpack CryEngine .pak content to a loose tree (like the bundled
    ``__CONTENT`` folder next to the game).

    Long unpacks run as a DETACHED background subprocess of
    ``scripts/unpack_crysis.py`` — the same subprocess-of-the-venv mechanism
    the converters use — so this MCP call returns immediately instead of
    blocking. Verbose log + tqdm progress go to a log file; call again with
    ``status=True`` (same path/out) to read the tail.

    Args:
        path: a single ``.pak`` archive, OR a folder to unpack — either a
            data root (folder with ``*.pak``) or an install directory (the
            data root is auto-detected via its ``GameData.pak``).
        out: directory that will hold the ``<name>.pak_Unpacked`` folders.
            For a game folder, defaults to ``<parent of root>/__CONTENT``.
            For a single .pak, defaults to the .pak's own directory.
        dry_run: for a game folder, only resolve + list the paks and target
            dir, writing nothing (useful to preview a multi-GB unpack).
        rewrite: delete an existing ``<name>_Unpacked`` folder and re-extract
            it (otherwise finished paks are skipped — re-running RESUMES).
        wait: block until done (up to 900 s) and return the full log — for
            single small paks. Default False: start detached, return at once.
        status: start nothing — report the progress of a previously started
            unpack for the same ``path``/``out``: running / finished /
            aborted, plus the tail of its log.
        crypto: Twofish-CTR backend for encrypted (Crysis 3) paks:
            ``auto`` (default; probe numba -> cupy -> python), ``python``,
            ``numba`` (JIT), or ``cupy`` (GPU). Passed through to
            unpack_crysis.py as ``--crypto``.

    Returns:
        A report: what was unpacked where, files written/skipped, errors —
        or, for a detached start, the log file to watch and how to poll.
    """
    import subprocess
    import time

    from cristical_core.pak_unpack import plan_game_unpack, unpack_pak

    abspath = os.path.abspath(path)

    # ---- status mode: report a running/finished unpack --------------------
    if status:
        log_path = _unpack_log_path(abspath, out)
        if not os.path.isfile(log_path):
            return ("no unpack log for this path/out yet: %s\n"
                    "(call cristorical_unpack without status=True to start one)"
                    % log_path)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        done = "UNPACK_DONE rc=" in content
        try:
            fresh = (time.time() - os.path.getmtime(log_path)) < 120
        except OSError:
            fresh = False
        if done:
            state = "FINISHED"
        elif fresh:
            state = "RUNNING (log updated less than 2 min ago)"
        else:
            state = ("ABORTED or stalled (no UNPACK_DONE marker and the log "
                     "has not been touched for >2 min) — safe to re-run, "
                     "finished paks are skipped")
        return "\n".join([
            "=" * 70,
            "CrisTical — unpack status",
            "  path:  %s" % abspath,
            "  log:   %s" % log_path,
            "  state: %s" % state,
            "-" * 70,
            "(log tail, last ~3000 chars)",
            content[-3000:],
        ])

    # ---- single .pak ------------------------------------------------------
    if abspath.lower().endswith(".pak"):
        if not os.path.isfile(abspath):
            raise ValueError("pak not found: %s" % abspath)
        out_root = os.path.abspath(out) if out else os.path.dirname(abspath)
        if crypto != "auto":
            from cristical_core.twofish_fast import set_backend as set_crypto_backend
            set_crypto_backend(crypto)
        if wait:
            dest, written, skipped = unpack_pak(abspath, out_root)
            return "\n".join([
                "=" * 70,
                "CrisTical — unpack pak (waited)",
                "  pak:    %s" % abspath,
                "  dest:   %s" % dest,
                "  files:  %d written, %d skipped" % (written, skipped),
                "Done: %s" % dest,
            ])
        log_path = _unpack_log_path(abspath, out)
        argv = ["-i", abspath, "-o", out_root, "--crypto", crypto]
        if rewrite:
            argv.append("-rewrite")
        _start_detached_unpack(argv, log_path)
        return "\n".join([
            "=" * 70,
            "CrisTical — unpack pak (started in background)",
            "  pak:    %s" % abspath,
            "  dest:   %s" % out_root,
            "  crypto: %s" % crypto,
            "  log:    %s" % log_path,
            "Poll: call cristorical_unpack with status=True (same path/out).",
        ])

    # ---- folder mode ------------------------------------------------------
    if not os.path.isdir(abspath):
        raise ValueError("not a .pak or a folder: %s" % abspath)

    plan = plan_game_unpack(abspath, os.path.abspath(out) if out else None)
    out_root = plan["out_root"]

    if dry_run:
        lines = [
            "=" * 70,
            "CrisTical — unpack game content (DRY RUN, nothing written)",
            "  root:   %s" % plan["root"],
            "  out:    %s" % out_root,
            "  paks:   %d   total: %.1f MB" % (
                plan["pak_count"], plan["total_bytes"] / (1024.0 * 1024.0)),
            "-" * 70,
        ]
        for name, size in plan["paks"]:
            lines.append("  %-28s %10.1f MB" % (name, size / (1024.0 * 1024.0)))
        lines.append("Re-run without dry_run to unpack.")
        return "\n".join(lines)

    if wait:
        # blocking variant, same contract as the converters (up to 900 s)
        log_path = _unpack_log_path(abspath, out)
        argv = ["-i", abspath, "-o", out_root, "--crypto", crypto]
        if rewrite:
            argv.append("-rewrite")
        cmd = [VENV_PYTHON, UNPACK_SCRIPT] + argv
        started = datetime.datetime.now()
        with open(log_path, "a", encoding="utf-8") as log_fh:
            try:
                rc = subprocess.call(
                    cmd, cwd=PROJ_DIR, env=_bat_env(),
                    stdout=log_fh, stderr=subprocess.STDOUT,
                    timeout=900, shell=False)
            except subprocess.TimeoutExpired:
                return ("[TIMEOUT] unpack exceeded 900 s — the subprocess was "
                        "terminated; finished paks are kept, safe to re-run "
                        "without wait=True (it resumes). Log: %s" % log_path)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        dur = (datetime.datetime.now() - started).total_seconds()
        return "\n".join([
            "=" * 70,
            "CrisTical — unpack game content (waited %.0f s, rc=%s)" % (dur, rc),
            "  root:  %s" % plan["root"],
            "  out:   %s" % out_root,
            "-" * 70,
            "(log tail, last ~4000 chars)",
            content[-4000:],
        ])

    # default: detached background start
    log_path = _unpack_log_path(abspath, out)
    argv = ["-i", abspath, "-o", out_root, "--crypto", crypto]
    if rewrite:
        argv.append("-rewrite")
    _start_detached_unpack(argv, log_path)
    return "\n".join([
        "=" * 70,
        "CrisTical — unpack game content (started in background)",
        "  root:   %s" % plan["root"],
        "  out:    %s" % out_root,
        "  paks:   %d   total: %.1f MB" % (
            plan["pak_count"], plan["total_bytes"] / (1024.0 * 1024.0)),
        "  log:    %s" % log_path,
        "Resume: re-running skips finished paks; -rewrite forces re-extract.",
        "Poll: call cristorical_unpack with status=True (same path/out).",
    ])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
