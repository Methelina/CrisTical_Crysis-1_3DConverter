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


def _require_input(path):
    """Validate the input file: absolute, exists, known extension."""
    if not path:
        raise ValueError("path is required (absolute path to .cdf/.chr/.cgf/.cga)")
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise ValueError("file not found: %s" % path)
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
        path: absolute path to the .cdf / .chr / .cgf / .cga file.
        gamedir: game root folder(s) for textures/animations/materials
            lookup — a single string or a list. Priority order is kept
            (recommended: Remaster first, original second, unpacked
            content last). Repeatable, same as the --gamedir CLI flag.
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

    src, mode = _require_input(path)
    dirs = _norm_dirs(gamedir)
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
def cristorical_scan(
    path: str,
    gamedir: str | list[str] | None = None,
) -> str:
    """Inspect a Crysis file WITHOUT converting — dry-run analysis.

    Reports what a conversion would produce: chunk versions, bone counts,
    mesh statistics, referenced materials, animations in the .dba, the
    resolved .mtl path, and problems (missing textures, unreadable chunks).

    Works with .cdf, .chr, .cgf, .cga, .anm, .dba, .cal, .mtl.

    Args:
        path: absolute path to the file to inspect.
        gamedir: optional game root folder(s) for resolving references
            (.dba lookup, .mtl resolution) — string or list.

    Returns:
        Human-readable inspection report; no files are written.
    """
    if not path:
        raise ValueError("path is required")
    src = os.path.abspath(path)
    if not os.path.isfile(src):
        raise ValueError("file not found: %s" % src)
    dirs = _norm_dirs(gamedir)

    lines = [
        "=" * 70,
        "CrisTical MCP bridge — scan (dry run, nothing is written)",
        "  input:   %s" % src,
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
            from cristical_core.crycga import read_cga
            data = read_cga(src)
            lines.append("ANM parsed: %d controller chunk(s)" % len(data.get("nodes", [])))
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
def cristorical_list(
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
def cristorical_version() -> str:
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
