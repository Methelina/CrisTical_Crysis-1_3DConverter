#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cristical_gui.py — CrisTical: converter GUI (DearPyGui)
Authors: Soror L.'.L.'. aka Methelina    Project: CrisTical
Version: 1.5

Tabbed GUI. Game directories and the output directory are shared
infrastructure for every tool, so they sit above the tabs:

  * Game Directories / Output — common to all pipelines (model, level, unpack).
  * CDF Pipeline — model conversion: CDF/CHR/CGF file, animation/texture
    modes, live CLI preview, run button.
  * Map — level tooling (currently Level -> JSON export; the unpacked level
    block that used to sit inline in the file pipeline). Future level
    features (terrain, geometry, vegetation placement) belong here.

Header, the shared inputs above the tabs, status strip and the log are
common across tabs.

=== Run ===
  python cristical_gui.py   (no args — the GUI is interactive)
"""

import os
import queue
import sys
import textwrap
import threading
import webbrowser
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from cdf2gltf import run_pipeline, read_chr_or_cdf, parse_cal, resolve_dba
from cdf2gltf import _resolve_mtl as _cdf_resolve_mtl
from cgf2gltf import run_pipeline as run_cgf_pipeline
from cgf2gltf import _resolve_mtl as _cgf_resolve_mtl
from cga2gltf import run_pipeline as run_cga_pipeline
from cristical_core import read_cdf, read_dba_version, read_cgf, read_cgf_meshes
from cristical_core.path_resolve import resolve_geometry_path
from cristical_core.game_profile import GameTitle, find_data_root, classify_game_dir
import dearpygui.dearpygui as dpg

SC = "status_circle"
ST = "status_text"
GC = "gamedir_circle"
GT = "gamedir_text"
LG = "log_area"
CK = "autoscroll_cb"
CDF_INPUT = "cdf_input"
OUTPUT_INPUT = "output_input"
SCAN_LABEL = "scan_label"
DETECT_LABEL = "detect_label"
ANIM_MODE_COMBO = "anim_mode_combo"
TEX_MODE_COMBO = "tex_mode_combo"
GLB_CHECK = "glb_check"
COLLISION_CHECK = "collision_check"
RUN_BTN = "run_btn"
CLI_LABEL = "cli_label"
GD_GROUP = "gamedirs_group"
GD_ADD_BTN = "gamedirs_add_btn"
GAME_TITLE_COMBO = "game_title_combo"
LEVEL_INPUT = "level_input"
LEVEL_SKIP_VEG_CHECK = "level_skip_veg_check"
LEVEL_VISUAL_CHECK = "level_visual_check"
LEVEL_RUN_BTN = "level_run_btn"
TAB_BAR = "tabs_main"
TAB_CDF = "tab_cdf_pipeline"
TAB_MAP = "tab_map"
GAME_AUTO = "Auto-detect"
GAME_TITLES = (GAME_AUTO,) + tuple(t.value for t in GameTitle)

ANIM_MODES = ("Full pipeline (single file)", "Split per animation", "Skip animations")
TEX_MODES  = ("Auto-PBR", "Keep as-is",  "Skip textures")

_q = queue.Queue()
_sd = threading.Event()
_running = False
_logbuf = []
_game_dirs = []
_auto_game_root = None
_game_title = None

PROJ_ROOT = SCRIPT_DIR.parent
RESOURCES = PROJ_ROOT / "resources"
OUTPUT_DEFAULT = PROJ_ROOT / "output"

for d in (OUTPUT_DEFAULT, PROJ_ROOT / "temp"):
    d.mkdir(exist_ok=True)


# ===================================================================
#  File pickers (tkinter first, DPG fallback)
# ===================================================================

def _bundled_tcl_dir():
    """Locate a bundled Tcl/Tk script library (e.g. python_embeded/tcl).

    The project ships with an embedded Python whose tkinter has no
    usable init.tcl on the default search path. Returns the directory
    containing tcl8.6/ and tk8.6/ subfolders, or None.
    """
    for base in (PROJ_ROOT, SCRIPT_DIR, SCRIPT_DIR.parent):
        tcl = base / "python_embeded" / "tcl"
        if (tcl / "tcl8.6" / "init.tcl").is_file() and (tcl / "tk8.6" / "tk.tcl").is_file():
            return tcl
    return None


def _make_tk_root():
    """Create a Tk root, repairing Tcl library paths for embedded Python."""
    import tkinter as tk
    try:
        return tk.Tk()
    except tk.TclError:
        tcl = _bundled_tcl_dir()
        if tcl is None:
            raise
        os.environ["TCL_LIBRARY"] = str(tcl / "tcl8.6")
        os.environ["TK_LIBRARY"] = str(tcl / "tk8.6")
        return tk.Tk()


def _tk_pick_file(title, filetypes, initialdir=None):
    """Tkinter file-open dialog.

    Returns (path, cancelled). Raises Exception if tkinter is
    unavailable or the dialog fails — the caller then falls back
    to the DPG file dialog.
    """
    import tkinter as tk
    from tkinter import filedialog
    root = _make_tk_root()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        path = filedialog.askopenfilename(title=title, filetypes=filetypes,
                                           initialdir=initialdir)
    finally:
        root.destroy()
    if not path or not os.path.isfile(path):
        return "", True
    return path, False


def _tk_pick_dir(title, initialdir=None):
    """Tkinter directory dialog.

    Returns (path, cancelled). Raises Exception if tkinter is
    unavailable or the dialog fails — the caller then falls back
    to the DPG file dialog.
    """
    import tkinter as tk
    from tkinter import filedialog
    root = _make_tk_root()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        path = filedialog.askdirectory(title=title, initialdir=initialdir)
    finally:
        root.destroy()
    if not path or not os.path.isdir(path):
        return "", True
    return path, False


# ===================================================================
#  CLI preview
# ===================================================================

def _build_cli_preview():
    cdf = dpg.get_value(CDF_INPUT).strip().strip('"')
    out = dpg.get_value(OUTPUT_INPUT).strip().strip('"')
    anim_mode = dpg.get_value(ANIM_MODE_COMBO)
    tex_mode  = dpg.get_value(TEX_MODE_COMBO)
    is_cgf = cdf.lower().endswith(".cgf")
    is_cga = cdf.lower().endswith(".cga")

    parts = []
    if cdf:
        if is_cgf:
            parts.append('--cgf "%s"' % cdf)
        elif is_cga:
            parts.append('--cga "%s"' % cdf)
        else:
            parts.append('--cdf "%s"' % cdf)
    game_dirs_to_show = list(_game_dirs) if _game_dirs else ([_auto_game_root] if _auto_game_root else [])
    for gd in game_dirs_to_show:
        parts.append('--gamedir "%s"' % gd)
    if out:
        cdf_name = os.path.splitext(os.path.basename(cdf))[0] if cdf else "model"
        ext = ".glb" if dpg.get_value(GLB_CHECK) else ".gltf"
        out_file = os.path.join(out, cdf_name + ext)
        parts.append('--out "%s"' % out_file)
    if not is_cgf:
        if anim_mode == "Skip animations":
            parts.append("--no-anim")
        elif anim_mode == "Split per animation":
            parts.append("--split-anim")
    if tex_mode == "Skip textures":
        parts.append("--no-tex")
    if dpg.get_value(GLB_CHECK):
        parts.append("--glb")
    if dpg.get_value(COLLISION_CHECK):
        parts.append("--extract-collision")

    line = "Run_CrisTical.bat " + " ".join(parts) if parts else "Run_CrisTical.bat  (fill CDF/CGF path)"
    dpg.set_value(CLI_LABEL, line)


def _on_option_changed(sender, app_data, user_data):
    _build_cli_preview()


# ===================================================================
#  Logging
# ===================================================================

def ct(t, g):
    u = g.upper()
    sym = " "
    if "ERROR" in u or "TRACEBACK" in u or "FAIL" in u:
        sym = "!"
    elif "WARN" in u:
        sym = "*"
    elif "DONE" in u or "[OK]" in g[:4]:
        sym = "+"
    elif ">>>" in g[:4]:
        sym = ">"
    elif "[1/" in g[:5] or "[2/" in g[:5] or "[3/" in g[:5]:
        sym = "."
    line = f"[{t.center(19)}] {sym} {g}"
    print(line, flush=True)
    _q.put(("log", line))


_wrap_cache = {"chars": 0}


def _render_log(force=False):
    if not dpg.does_item_exist(LG):
        return
    try:
        w = dpg.get_item_rect_size(LG)[0]
    except Exception:
        return
    chars = max(40, int((w - 40) / 7))
    if not force and chars == _wrap_cache["chars"]:
        return
    _wrap_cache["chars"] = chars
    out = []
    for line in _logbuf:
        out.extend(textwrap.wrap(line, width=chars, replace_whitespace=False,
                                  drop_whitespace=False) or [""])
    if len(out) > 800:
        out = out[-800:]
    dpg.set_value(LG, "\n".join(out))


def dq():
    for _ in range(200):
        try:
            k, p = _q.get_nowait()
        except Exception:
            break
        if k == "log":
            _logbuf.append(p)
            if len(_logbuf) > 400:
                del _logbuf[:200]
            _render_log(force=True)
        elif k == "status":
            if dpg.does_item_exist(SC):
                clr, label = p
                dpg.configure_item(SC, fill=clr)
            if dpg.does_item_exist(ST):
                dpg.set_value(ST, p[1] if isinstance(p, tuple) else str(p))
                if isinstance(p, tuple):
                    dpg.configure_item(ST, color=p[0])
        elif k == "gamedir_status":
            if dpg.does_item_exist(GC):
                clr, label = p
                dpg.configure_item(GC, fill=clr)
            if dpg.does_item_exist(GT):
                dpg.set_value(GT, p[1] if isinstance(p, tuple) else str(p))
                if isinstance(p, tuple):
                    dpg.configure_item(GT, color=p[0])
        elif k == "running":
            global _running
            _running = p
            if dpg.does_item_exist(RUN_BTN):
                dpg.configure_item(RUN_BTN, enabled=not p)
    if dpg.does_item_exist(CK) and dpg.does_item_exist(LG):
        dpg.configure_item(LG, tracked=dpg.get_value(CK))


def set_status(color, label):
    _q.put(("status", (color, label)))


def set_gamedir_status(color, label):
    _q.put(("gamedir_status", (color, label)))


# ===================================================================
#  Auto-detect game root from .cdf path
# ===================================================================

_GAME_MARKERS = ("Animations.pak", "system.cfg", "GameInfo.ini", "Scripts.pak", "GameData")
_OBJECTS_DIR = "Objects"
_SIBLING_DIRS = ("Game", "game", "Data")


def _score_dir(d):
    s = 0
    for marker in _GAME_MARKERS:
        p = os.path.join(d, marker)
        if os.path.isfile(p) or os.path.isdir(p):
            s += 2
    if os.path.isdir(os.path.join(d, _OBJECTS_DIR)):
        s += 1
    return s


def _detect_game_roots_from_cdf(cdf_path):
    cdf_dir = os.path.dirname(os.path.abspath(cdf_path))
    candidates = {}
    cur = cdf_dir
    for _ in range(10):
        s = _score_dir(cur)
        if s > 0:
            candidates[os.path.normcase(cur)] = (s, cur)
        if os.path.basename(cur).lower() == _OBJECTS_DIR.lower():
            parent = os.path.dirname(cur)
            sp = _score_dir(parent)
            if sp > 0:
                candidates[os.path.normcase(parent)] = (sp, parent)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        for sib in _SIBLING_DIRS:
            sib_path = os.path.join(parent, sib)
            ss = _score_dir(sib_path)
            if ss > 0:
                candidates[os.path.normcase(sib_path)] = (ss, sib_path)
        cur = parent
    sorted_candidates = sorted(candidates.values(), key=lambda x: x[0], reverse=True)
    return [path for score, path in sorted_candidates]


def _detect_game_root_from_cdf(cdf_path):
    roots = _detect_game_roots_from_cdf(cdf_path)
    return roots[0] if roots else None


def _read_cdf_version(file_path):
    try:
        with open(file_path, "rb") as f:
            header = f.read(24)
    except OSError:
        return None

    if header[:6] == b"CryTek":
        import struct
        fv = struct.unpack_from("<I", header, 12)[0]
        return "v%04X" % fv

    if header[:1] == b"<":
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(file_path)
            model = tree.getroot().find("Model")
            if model is not None:
                ref = model.get("File", "")
                if ref:
                    base_dir = os.path.dirname(os.path.abspath(file_path))
                    ref_clean = ref.replace("\\", os.sep).replace("/", os.sep)
                    chr_path = os.path.join(base_dir, ref_clean)
                    if os.path.isfile(chr_path):
                        return _read_cdf_version(chr_path)
        except Exception:
            pass
    return None


def _detect_file_type(file_path):
    """Определяет реальный тип файла по структуре чанков.
    Возвращает: 'cdf', 'chr', 'cgf', 'cga' или None."""
    try:
        with open(file_path, "rb") as f:
            raw = f.read(8192)  # читаем заголовок и таблицу чанков
    except OSError:
        return None

    if len(raw) < 24:
        return None

    # Проверка сигнатуры чанка (первые 6 байт файла)
    if raw[:6] != b"CryTek":
        return None

    import struct
    ft, fv, cto, nch = struct.unpack_from("<IIII", raw, 8)
    
    # Проверяем валидность таблицы чанков
    if cto + 4 > len(raw) or nch > 10000:
        return None
    
    entry_size = 20 if fv == 0x0745 else 16
    if cto + 4 + nch * entry_size > len(raw):
        return None

    # Читаем таблицу чанков
    chunks = []
    for i in range(min(nch, 500)):  # ограничиваем для производительности
        off = cto + 4 + i * (20 if fv == 0x0745 else 16)
        if off + 16 > len(raw):
            break
        t, v, o, cid = struct.unpack_from("<IIII", raw, off)
        chunks.append((t, v, o, cid))

    # Определяем наличие ключевых чанков
    has_compiled_bones = any(t == 0xACDC0000 for t, v, o, cid in chunks)
    has_mesh = any(t == 0xCCCC0000 for t, v, o, cid in chunks)
    has_node = any(t == 0xCCCC000B for t, v, o, cid in chunks)
    has_controller = any(t == 0xCCCC000D for t, v, o, cid in chunks)  # TCB controller
    has_mtlname = any(t == 0xCCCC0014 for t, v, o, cid in chunks)

    # CHR/CDF (персонажи) — имеют CompiledBones чанк (0xACDC0000)
    if any(t == 0xACDC0000 for t, v, o, cid in chunks):
        # Если есть XML внутри — это CDF, иначе CHR
        if b"<" in raw and b">" in raw:
            return "cdf"
        return "chr"
    
    # CGA (анимационная геометрия) — имеет TCB контроллеры (0xCCCC000D) и Mesh/MtlName,
    # но НЕТ CompiledBones
    if any(t == 0xCCCC000D for t, v, o, cid in chunks) and not any(t == 0xACDC0000 for t, v, o, cid in chunks):
        return "cga"
    
    # Статический CGF — имеет Mesh (0xCCCC0000) и Node (0xCCCC000B) чанки,
    # НЕТ CompiledBones, НЕТ TCB контроллеров
    if any(t == 0xCCCC0000 for t, v, o, cid in chunks) and any(t == 0xCCCC000B for t, v, o, cid in chunks):
        return "cgf"
    
    return None

    if len(raw) < 24:
        return None

    # Проверка сигнатуры чанка (первые 6 байт файла)
    if raw[:6] != b"CryTek":
        return None

    import struct
    ft, fv, cto, nch = struct.unpack_from("<IIII", raw, 8)
    
    # Проверяем валидность таблицы чанков
    if cto + 4 > len(raw) or nch > 1000:
        return None
    
    entry_size = 20 if fv == 0x0745 else 16
    if cto + 4 + nch * entry_size > len(raw):
        return None

    # Читаем таблицу чанков
    chunks = []
    for i in range(nch):
        off = cto + 4 + i * (20 if fv == 0x0745 else 16)
        if off + 16 > len(raw):
            break
        t, v, o, cid = struct.unpack_from("<IIII", raw, off)
        chunks.append((t, v, o, cid))

    # Определяем тип по чанкам
    has_compiled_bones = any(t == 0xACDC0000 for t, v, o, cid in chunks)
    has_mesh = any(t == 0xCCCC0000 for t, v, o, cid in chunks)
    has_node = any(t == 0xCCCC000B for t, v, o, cid in chunks)
    has_compiled_morphtargets = any(t == 0xACDC0002 for t, v, o, cid in chunks)

    # CGA файлы имеют свой формат (проверяем по расширению)
    # Они могут не иметь чанк-заголовка или иметь другой формат
    
    # CHR/CDF (персонажи) — имеют CompiledBones чанк
    if any(t == 0xACDC0000 for t, v, o, cid in chunks):
        # Если есть XML внутри — это CDF, иначе CHR
        # Проверяем наличие XML в файле (после заголовка)
        if b"<" in raw and b">" in raw:
            return "cdf"
        return "chr"
    
    # Статический CGF — имеет Mesh и Node чанки, но нет CompiledBones
    if has_mesh and has_node and not any(t == 0xACDC0000 for t, v, o, cid in chunks):
        return "cgf"
    
    return None


def _validate_game_dirs(game_dirs):
    if not game_dirs:
        return (110, 110, 110), "No directories"
    all_ok = True
    any_missing = False
    for d in game_dirs:
        if not os.path.isdir(d):
            all_ok = False
            any_missing = True
            continue
        has_marker = False
        for marker in _GAME_MARKERS:
            if os.path.isfile(os.path.join(d, marker)):
                has_marker = True
                break
        if not has_marker and os.path.isdir(os.path.join(d, "Objects")):
            has_marker = True
        if not has_marker:
            all_ok = False
    if all_ok:
        return (80, 210, 100), "Valid"
    if any_missing:
        return (230, 70, 70), "Not found"
    return (240, 200, 60), "No markers"


# ===================================================================
#  Game directories list
# ===================================================================

def _rebuild_gamedirs_ui():
    if not dpg.does_item_exist(GD_GROUP):
        return
    dpg.delete_item(GD_GROUP, children_only=True)

    if not _game_dirs and _auto_game_root:
        with dpg.group(horizontal=True, parent=GD_GROUP):
            dpg.add_text(_auto_game_root, color=(120, 180, 120))
            dpg.add_text(" (auto-detected)", color=(100, 130, 100))

    for i, d in enumerate(_game_dirs):
        with dpg.group(horizontal=True, parent=GD_GROUP):
            dpg.add_text(d, color=(180, 185, 195))
            dpg.add_button(label="Del", small=True, width=30, height=18,
                           callback=_remove_game_dir, user_data=i, tag=f"gdbtn_{i}")
            with dpg.tooltip(f"gdbtn_{i}"):
                dpg.add_text("Remove this directory from the list")
    _build_cli_preview()
    _validate_and_update_gamedir_status()


def _validate_and_update_gamedir_status():
    game_dirs = list(_game_dirs) if _game_dirs else ([_auto_game_root] if _auto_game_root else [])
    clr, label = _validate_game_dirs(game_dirs)
    set_gamedir_status(clr, label)
    _update_game_detect_text(game_dirs)


def _update_game_detect_text(game_dirs):
    """Show the current edition next to the Game combo: the manually chosen
    title, or (in Auto mode) the detected title from the data root."""
    if not dpg.does_item_exist("game_detect_text"):
        return
    try:
        sel = dpg.get_value(GAME_TITLE_COMBO)
    except Exception:
        sel = None
    if sel and sel != GAME_AUTO:
        text = sel
    elif game_dirs:
        try:
            det = classify_game_dir(game_dirs[0])
        except Exception:
            det = None
        if det and det["title"]:
            text = det["title"].value
        elif det and det["family"] == "zip":
            text = "Crysis 1 / Warhead / Wars"
        else:
            text = "Auto-detect"
    else:
        text = "Auto-detect"
    dpg.set_value("game_detect_text", text)


def _remove_game_dir(sender, app_data, user_data):
    idx = user_data
    if 0 <= idx < len(_game_dirs):
        del _game_dirs[idx]
    _rebuild_gamedirs_ui()
    scan_model()


def _add_game_dir():
    try:
        path, cancelled = _tk_pick_dir("Select game directory")
    except Exception:
        dpg.show_item("file_dialog_gamedir")
        return
    if cancelled:
        return
    _append_game_dir(path)


def _append_game_dir(path):
    global _auto_game_root
    path = os.path.normpath(path)
    target = find_data_root(path) if _selected_game_title() is not None else path
    if target not in _game_dirs:
        _game_dirs.append(target)
    if _auto_game_root:
        _auto_game_root = None
    _rebuild_gamedirs_ui()
    scan_model()


def _gamedir_dlg_callback(sender, app_data):
    path = app_data.get("file_path_name", "")
    if path:
        _append_game_dir(path)


def _selected_game_title():
    """The GameTitle currently chosen in the GUI combo, or None for Auto."""
    try:
        val = dpg.get_value(GAME_TITLE_COMBO)
    except Exception:
        return None
    if not val or val == GAME_AUTO:
        return None
    for t in GameTitle:
        if t.value == val:
            return t
    return None


def _on_game_title_change():
    """React to the Game edition combo: normalise any already-added install
    roots to their canonical data root for the chosen title, then rescan."""
    if _selected_game_title() is not None and _game_dirs:
        mapped = []
        for d in _game_dirs:
            root = find_data_root(d)
            if root not in mapped:
                mapped.append(root)
        if mapped != _game_dirs:
            _game_dirs[:] = mapped
    _rebuild_gamedirs_ui()
    scan_model()


# ===================================================================
#  CDF / Output pickers
# ===================================================================

def browse_cdf():
    try:
        path, cancelled = _tk_pick_file(
            "Select CDF, CHR or CGF file",
            [("Crysis model", "*.cdf *.chr *.cgf"), ("All files", "*.*")])
    except Exception:
        dpg.show_item("file_dialog_cdf")
        return
    if cancelled:
        return
    dpg.set_value(CDF_INPUT, path)
    _build_cli_preview()
    scan_model()


def browse_output():
    try:
        path, cancelled = _tk_pick_dir("Select output directory",
                                        initialdir=str(OUTPUT_DEFAULT))
    except Exception:
        dpg.show_item("file_dialog_out")
        return
    if cancelled:
        return
    dpg.set_value(OUTPUT_INPUT, path)
    _build_cli_preview()


def browse_level():
    try:
        path, cancelled = _tk_pick_dir("Select unpacked level directory")
    except Exception:
        dpg.show_item("file_dialog_level")
        return
    if cancelled:
        return
    if os.path.isfile(os.path.join(path, "levelinfo.xml")):
        dpg.set_value(LEVEL_INPUT, path)
    else:
        ct("GUI", "WARN: no levelinfo.xml in %s — not an unpacked level dir?" % path)


def run_level_export():
    """Run level2json.py (subprocess) on the level directory chosen above."""
    level_dir = dpg.get_value(LEVEL_INPUT).strip().strip('"')
    if not level_dir or not os.path.isdir(level_dir):
        ct("GUI", "ERROR: level directory not set or not found")
        return
    out_dir = dpg.get_value(OUTPUT_INPUT).strip().strip('"') or str(OUTPUT_DEFAULT)
    level_name = os.path.basename(os.path.normpath(level_dir))
    out_json = os.path.join(out_dir, level_name + ".json")
    game_dirs = list(_game_dirs) if _game_dirs else ([_auto_game_root] if _auto_game_root else [])

    argv = [level_dir, out_json]
    if game_dirs:
        argv += ["--game-dir", game_dirs[0]]
    if dpg.get_value(LEVEL_VISUAL_CHECK):
        argv.append("--visual-only")
    if dpg.get_value(LEVEL_SKIP_VEG_CHECK):
        argv.append("--no-vegetation")

    script = os.path.join(SCRIPT_DIR, "level2json.py")
    python_exe = sys.executable
    ct("GUI", "Level export: %s -> %s" % (level_dir, out_json))
    ct("GUI", "CLI equivalent: %s level2json.py %s" % (
        os.path.basename(python_exe), " ".join(argv)))

    def _run():
        global _running
        _running = True
        _q.put(("running", True))
        set_status((240, 200, 60), "RUNNING")
        try:
            import subprocess
            proc = subprocess.Popen(
                [python_exe, script] + argv,
                cwd=str(PROJ_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in proc.stdout:
                ct("Level", line.rstrip("\r\n"))
            rc = proc.wait()
            if rc == 0:
                ct("GUI", "Level export OK: %s" % out_json)
                set_status((80, 210, 100), "DONE")
            else:
                ct("GUI", "ERROR: level2json.py exited with code %s" % rc)
                set_status((230, 70, 70), "ERROR")
        except Exception as e:
            ct("GUI", "ERROR: %s" % str(e))
            set_status((230, 70, 70), "ERROR")
        finally:
            _running = False
            _q.put(("running", False))

    threading.Thread(target=_run, daemon=True).start()


def _file_callback(sender, app_data):
    if sender == "file_dialog_cdf":
        sel = app_data.get("selections", {})
        file_path = list(sel.keys())[0] if sel else app_data.get("file_path_name", "")
        if file_path and os.path.isfile(file_path):
            dpg.set_value(CDF_INPUT, file_path)
            _build_cli_preview()
            scan_model()
    elif sender == "file_dialog_out":
        path = app_data.get("file_path_name", "")
        if path:
            dpg.set_value(OUTPUT_INPUT, path)
            _build_cli_preview()
    elif sender == "file_dialog_level":
        path = app_data.get("file_path_name", "")
        if path and os.path.isdir(path):
            dpg.set_value(LEVEL_INPUT, path)


# ===================================================================
#  Model scanner
# ===================================================================
#  Helper: character scan processing
# ===================================================================

def _process_character_scan(cdf_path, data, chr_path, att_count, game_dirs, mtl_count, mtl_path, fmt_ver, ct, dpg, SCAN_LABEL, DETECT_LABEL, set_status):
    """Обрабатывает сканирование персонажа (CHR/CDF) и обновляет UI."""
    bones = data["skeleton"]
    mesh = data["mesh"]
    prim_count = len(mesh["primitives"])

    mtl_count = 0
    mtl_path = _cdf_resolve_mtl(chr_path, game_dirs,
                                [p.get("material") for p in mesh["primitives"]],
                                log=ct)
    if mtl_path and os.path.isfile(mtl_path):
        import xml.etree.ElementTree as ET2
        try:
            tree = ET2.parse(mtl_path)
            root = tree.getroot()
            sub = root.find("SubMaterials")
            mtl_count = len(sub.findall("Material")) if sub is not None else (
                1 if root.tag == "Material" else 0)
        except Exception:
            pass

    anim_info = ""
    dba_ver = None
    cal_path = os.path.splitext(chr_path)[0] + ".cal"
    if os.path.isfile(cal_path):
        cal = parse_cal(cal_path)
        dba_rel = cal.get("TracksDatabase", "")
        if dba_rel:
            if not dba_rel.lower().endswith(".dba"):
                dba_rel += ".dba"
            if game_dirs:
                dba_test = resolve_dba(dba_rel, game_dirs)
                if dba_test:
                    dba_ver = read_dba_version(dba_test)
                    from cristical_core import read_dba as rd
                    try:
                        d = rd(dba_test)
                        anim_info = ("%d animations in %s"
                                     % (len(d.animations), os.path.basename(dba_test)))
                    except Exception:
                        anim_info = "detected (%s)" % dba_rel
                else:
                    anim_info = "DBA not found (%s)" % dba_rel
            else:
                anim_info = "%s (add game dir to verify)" % dba_rel
        else:
            anim_info = "none"
    else:
        # Roadmap 4.2: no .cal — report .chrparams diagnostics instead.
        anim_info = "none"
        try:
            from cristical_core.crychrparams import collect_clip_refs
            refs = collect_clip_refs(chr_path, game_dirs)
            if refs is not None:
                n_caf = sum(1 for c in refs.clips if c[2] == ".caf")
                n_dba = sum(1 for c in refs.clips if c[2] == ".dba")
                anim_info = ("chrparams: %d loose .caf, %d dba refs"
                             % (n_caf, n_dba))
                if refs.missing_includes:
                    ct("Scan", "  chrparams: missing includes: %s"
                       % ", ".join(refs.missing_includes))
                if refs.empty_wildcards:
                    ct("Scan", "  chrparams: empty wildcards: %s"
                       % ", ".join(refs.empty_wildcards))
                for name, path, ext in refs.clips[:5]:
                    ct("Scan", "  chrparams clip: %s -> %s" % (name, path))
                if len(refs.clips) > 5:
                    ct("Scan", "  chrparams: ... and %d more" % (len(refs.clips) - 5))
        except Exception:
            pass

    lmg_info = "none"
    # Virtual (game-relative) path of the model: for pak-materialized inputs
    # the real chr_path lives in temp, so resolve the sibling .chrparams
    # through the CDF's model_ref instead. collect_lmg_refs derives it from
    # chr_path itself when the file lies inside a game root (loose inputs).
    chr_virtual = None
    try:
        if cdf_path.lower().endswith(".cdf"):
            _cdf_scan = read_cdf(cdf_path, game_dirs)
            chr_virtual = _cdf_scan.get("model_ref")
    except Exception:
        chr_virtual = None
    lmg_result = collect_lmg_refs(chr_path, game_dirs, virtual=chr_virtual)
    if lmg_result and lmg_result.get("groups"):
        lmg_info = "%d groups" % len(lmg_result["groups"])
        ct("Scan", "  LMG: %d groups (%s)" % (len(lmg_result["groups"]), lmg_result["source"]))
    else:
        ct("Scan", "  LMG: none")

    scan_text = ("Bones: %d   Primitives: %d   Attachments: %d"
                 % (len(bones), prim_count, att_count))
    detect_text = "Materials: %d   Animations: %s   LMG: %s" % (mtl_count, anim_info, lmg_info)
    dpg.set_value(SCAN_LABEL, scan_text)
    dpg.set_value(DETECT_LABEL, detect_text)
    ct("Scan", scan_text)
    ct("Scan", detect_text)

    if dba_ver:
        set_status((80, 210, 100), "Valid (%s)" % dba_ver)
    elif fmt_ver:
        set_status((80, 210, 100), "Valid (%s)" % fmt_ver)
    else:
        set_status((80, 210, 100), "Valid (Unknown)")


# ===================================================================
#  Model scanner
# ===================================================================

def scan_model():
    global _auto_game_root
    cdf_path = dpg.get_value(CDF_INPUT).strip().strip('"')

    if not cdf_path:
        dpg.set_value(SCAN_LABEL, "CDF file not found")
        dpg.set_value(DETECT_LABEL, "")
        set_status((110, 110, 110), "No file")
        return

    # Virtual (in-pak) path support: materialize through the VFS before
    # any read_cgf*/read_cdf call. Real files pass through unchanged.
    if not os.path.isfile(cdf_path):
        game_dirs_probe = list(_game_dirs) if _game_dirs else ([_auto_game_root] if _auto_game_root else [])
        try:
            resolved = resolve_geometry_path(cdf_path, game_dirs_probe)
        except FileNotFoundError as e:
            ct("GUI", "ERROR: %s" % e)
            dpg.set_value(SCAN_LABEL, "Path not found (real or virtual)")
            dpg.set_value(DETECT_LABEL, "")
            set_status((110, 110, 110), "No file")
            return
        if resolved != cdf_path:
            ct("GUI", "virtual path materialized: %s -> %s" % (cdf_path, resolved))
            cdf_path = resolved

    try:
        if not _game_dirs:
            all_roots = _detect_game_roots_from_cdf(cdf_path)
            if all_roots:
                _auto_game_root = all_roots[0]
                ct("GUI", "+ auto-detected game root: %s" % _auto_game_root)
                if len(all_roots) > 1:
                    for r in all_roots[1:3]:
                        ct("GUI", "  also: %s" % r)
                _rebuild_gamedirs_ui()
            else:
                if _auto_game_root is not None:
                    ct("GUI", "* auto-detected game root lost")
                _rebuild_gamedirs_ui()

        _validate_and_update_gamedir_status()

        dpg.set_value(SCAN_LABEL, "Scanning...")
        dpg.set_value(DETECT_LABEL, "")

        fmt_ver = _read_cdf_version(cdf_path)
        game_dirs = list(_game_dirs) if _game_dirs else ([_auto_game_root] if _auto_game_root else [])
        if not game_dirs:
            ct("GUI", "WARN: No game directories — material/animation search limited")

        # Определяем реальный тип файла по содержимому
        real_type = _detect_file_type(cdf_path)
        ext = cdf_path.lower().split('.')[-1]
        
        # CGA файлы определяются по расширению (у них свой формат)
        if cdf_path.lower().endswith(".cga"):
            from cristical_core import read_cga
            data = read_cga(cdf_path)
            ct("Scan", "Animated .cga   Nodes: %d   Primitives: %d" % (data["num_nodes"], data["num_prims"]))
            dpg.set_value(SCAN_LABEL, "Animated .cga   Nodes: %d   Primitives: %d" % (data["num_nodes"], data["num_prims"]))
            dpg.set_value(DETECT_LABEL, "Animated geometry (auto-finds sibling .anm)")
            set_status((80, 210, 100), "Valid (CGA)")
            return

        # Если файл .cgf по расширению, но содержимое — чанк CHR/CDF
        if cdf_path.lower().endswith(".cgf") and real_type in ("chr", "cdf"):
            ct("GUI", "INFO: File has .cgf extension but content is %s — treating as character" % real_type.upper())
            # Обрабатываем как персонаж
            data = read_chr_or_cdf(cdf_path)
            bones = data["skeleton"]
            mesh = data["mesh"]
            prim_count = len(mesh["primitives"])

            chr_path = cdf_path
            if real_type == "cdf":
                cdf_info = read_cdf(cdf_path)
                att_count = len(cdf_info.get("skin_attachments", []))
                if cdf_info.get("model_path"):
                    chr_path = cdf_info["model_path"]
            else:
                att_count = 0

            _process_character_scan(cdf_path, data, chr_path, att_count, game_dirs, fmt_ver, ct, dpg, SCAN_LABEL, DETECT_LABEL, set_status)
            return

        # Обычный статический CGF
        is_cgf = cdf_path.lower().endswith(".cgf")
        if is_cgf:
            data = read_cgf(cdf_path)
            prims = read_cgf_meshes(cdf_path)
            prim_count = len(prims)
            att_count = 0
            chr_path = cdf_path

            mtl_count = 0
            mtl_path = _cgf_resolve_mtl(
                cdf_path, game_dirs,
                [p.get("material") for p in prims] if prims else None)
            if mtl_path and os.path.isfile(mtl_path):
                import xml.etree.ElementTree as ET2
                try:
                    tree = ET2.parse(mtl_path)
                    root = tree.getroot()
                    sub = root.find("SubMaterials")
                    mtl_count = len(sub.findall("Material")) if sub is not None else (
                        1 if root.tag == "Material" else 0)
                except Exception:
                    pass

            anim_info = "static (no animation)"
            scan_text = ("Static .cgf   Primitives: %d   Meshes: %d"
                         % (prim_count, len(data["mesh_chunks"])))
            detect_text = "Materials: %d   Animations: %s" % (mtl_count, anim_info)
            dpg.set_value(SCAN_LABEL, scan_text)
            dpg.set_value(DETECT_LABEL, detect_text)
            ct("Scan", scan_text)
            ct("Scan", detect_text)
            set_status((80, 210, 100), "Valid (static CGF)")
            return

        data = read_chr_or_cdf(cdf_path)
        bones = data["skeleton"]
        mesh = data["mesh"]
        prim_count = len(mesh["primitives"])

        chr_path = cdf_path
        if cdf_path.lower().endswith(".cdf"):
            cdf_info = read_cdf(cdf_path)
            att_count = len(cdf_info.get("skin_attachments", []))
            if cdf_info.get("model_path"):
                chr_path = cdf_info["model_path"]
        else:
            att_count = 0

        mtl_count = 0
        mtl_path = _cdf_resolve_mtl(chr_path, game_dirs)
        if mtl_path and os.path.isfile(mtl_path):
            import xml.etree.ElementTree as ET2
            try:
                tree = ET2.parse(mtl_path)
                root = tree.getroot()
                sub = root.find("SubMaterials")
                mtl_count = len(sub.findall("Material")) if sub is not None else (
                    1 if root.tag == "Material" else 0)
            except Exception:
                pass

        anim_info = ""
        dba_ver = None
        cal_path = os.path.splitext(chr_path)[0] + ".cal"
        if os.path.isfile(cal_path):
            cal = parse_cal(cal_path)
            dba_rel = cal.get("TracksDatabase", "")
            if dba_rel:
                if not dba_rel.lower().endswith(".dba"):
                    dba_rel += ".dba"
                if game_dirs:
                    dba_test = resolve_dba(dba_rel, game_dirs)
                    if dba_test:
                        dba_ver = read_dba_version(dba_test)
                        from cristical_core import read_dba as rd
                        try:
                            d = rd(dba_test)
                            anim_info = ("%d animations in %s"
                                         % (len(d.animations), os.path.basename(dba_test)))
                        except Exception:
                            anim_info = "detected (%s)" % dba_rel
                    else:
                        anim_info = "DBA not found (%s)" % dba_rel
                else:
                    anim_info = "%s (add game dir to verify)" % dba_rel
            else:
                anim_info = "none"
        else:
            anim_info = "no .cal file"

        scan_text = ("Bones: %d   Primitives: %d   Attachments: %d"
                     % (len(bones), prim_count, att_count))
        detect_text = "Materials: %d   Animations: %s" % (mtl_count, anim_info)
        dpg.set_value(SCAN_LABEL, scan_text)
        dpg.set_value(DETECT_LABEL, detect_text)
        ct("Scan", scan_text)
        ct("Scan", detect_text)

        if dba_ver:
            set_status((80, 210, 100), "Valid (%s)" % dba_ver)
        elif fmt_ver:
            set_status((80, 210, 100), "Valid (%s)" % fmt_ver)
        else:
            set_status((80, 210, 100), "Valid (Unknown)")

    except Exception as e:
        dpg.set_value(SCAN_LABEL, "Scan failed")
        dpg.set_value(DETECT_LABEL, str(e))
        set_status((230, 70, 70), "Invalid")


# ===================================================================
#  Conversion runner
# ===================================================================

def run_conversion():
    cdf_path = dpg.get_value(CDF_INPUT).strip().strip('"')
    out_dir = dpg.get_value(OUTPUT_INPUT).strip().strip('"')
    anim_mode = dpg.get_value(ANIM_MODE_COMBO)
    tex_mode  = dpg.get_value(TEX_MODE_COMBO)

    if not cdf_path:
        ct("GUI", "ERROR: CDF file not found")
        return

    game_dirs = list(_game_dirs) if _game_dirs else ([_auto_game_root] if _auto_game_root else [])

    # Virtual (in-pak) path support: same resolution as scan_model().
    if not os.path.isfile(cdf_path):
        try:
            resolved = resolve_geometry_path(cdf_path, game_dirs)
        except FileNotFoundError as e:
            ct("GUI", "ERROR: %s" % e)
            return
        if resolved != cdf_path:
            ct("GUI", "virtual path materialized: %s -> %s" % (cdf_path, resolved))
            cdf_path = resolved

    if not game_dirs:
        ct("GUI", "WARN: No game directories specified")
        ct("GUI", "WARN: Texture/animation resolution may fail")
    elif not _game_dirs and _auto_game_root:
        ct("GUI", "Using auto-detected game root: %s" % _auto_game_root)

    cdf_name = os.path.splitext(os.path.basename(cdf_path))[0]

    if not out_dir:
        out_dir = str(OUTPUT_DEFAULT)
        dpg.set_value(OUTPUT_INPUT, out_dir)

    out_dir = os.path.normpath(out_dir)
    out_gltf = os.path.join(out_dir, cdf_name + ".gltf")
    os.makedirs(out_dir, exist_ok=True)

    do_anim = anim_mode != "Skip animations"
    split_anim = (anim_mode == "Split per animation")
    do_tex = tex_mode != "Skip textures"
    use_glb = dpg.get_value(GLB_CHECK)
    extract_coll = dpg.get_value(COLLISION_CHECK)

    ct("GUI", "Output dir: %s" % out_dir)
    ct("GUI", "Output file: %s" % out_gltf)
    ct("GUI", "CLI equivalent: " + dpg.get_value(CLI_LABEL))

    is_cga = cdf_path.lower().endswith(".cga")
    is_cgf = cdf_path.lower().endswith(".cgf")

    def _run():
        global _running
        _running = True
        _q.put(("running", True))
        set_status((240, 200, 60), "RUNNING")
        try:
            if is_cga:
                run_cga_pipeline(cdf_path, game_dirs, out_gltf,
                                 do_anim=do_anim, do_tex=do_tex,
                                 progress_cb=lambda msg: ct("Pipeline", msg),
                                 extract_collision=extract_coll)
            elif is_cgf:
                run_cgf_pipeline(cdf_path, game_dirs, out_gltf,
                                 do_tex=do_tex, glb=use_glb,
                                 progress_cb=lambda msg: ct("Pipeline", msg),
                                 extract_collision=extract_coll)
            else:
                run_pipeline(cdf_path, game_dirs, out_gltf,
                             do_anim=do_anim, do_tex=do_tex, split_anim=split_anim,
                             glb=use_glb,
                             progress_cb=lambda msg: ct("Pipeline", msg),
                             extract_collision=extract_coll)
            set_status((80, 210, 100), "DONE")
        except Exception as e:
            ct("GUI", "ERROR: %s" % str(e))
            import traceback
            ct("GUI", traceback.format_exc())
            set_status((230, 70, 70), "ERROR")
        finally:
            _running = False
            _q.put(("running", False))

    threading.Thread(target=_run, daemon=True).start()


# ===================================================================
#  GUI builder
# ===================================================================

def build_gui():
    dpg.create_context()

    with dpg.theme() as gt:
        with dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (25, 25, 35))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (20, 22, 26))
            dpg.add_theme_color(dpg.mvThemeCol_Button, (45, 55, 70))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (65, 80, 100))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (85, 100, 120))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (40, 42, 50))
            dpg.add_theme_color(dpg.mvThemeCol_Text, (220, 220, 220))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 6)
            dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 4)
    dpg.bind_theme(gt)

    fonts_ok = False
    try:
        font_path = RESOURCES / "ModeSevenBETAVHS.ttf"
        if font_path.is_file():
            with dpg.font_registry():
                with dpg.font(str(font_path), 16, tag="font_regular"):
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Cyrillic)
            dpg.bind_font("font_regular")
            fonts_ok = True
    except Exception:
        pass

    with dpg.theme() as minus_btn_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (70, 40, 40))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (120, 50, 50))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (90, 30, 30))

    with dpg.theme() as add_btn_theme:
        with dpg.theme_component(dpg.mvButton):
            dpg.add_theme_color(dpg.mvThemeCol_Button, (40, 70, 40))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (50, 120, 50))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (30, 90, 30))

    # --- file dialogs (DPG fallback) ---
    with dpg.file_dialog(
        directory_selector=False, show=False, callback=_file_callback,
        tag="file_dialog_cdf", width=600, height=400
    ):
        dpg.add_file_extension(".cdf", color=(80, 210, 100))
        dpg.add_file_extension(".chr", color=(255, 200, 100))
        dpg.add_file_extension(".cgf", color=(100, 160, 255))
        dpg.add_file_extension(".*")

    with dpg.file_dialog(
        directory_selector=True, show=False, callback=_file_callback,
        tag="file_dialog_out", width=600, height=400
    ):
        dpg.add_file_extension("")

    with dpg.file_dialog(
        directory_selector=True, show=False, callback=_gamedir_dlg_callback,
        tag="file_dialog_gamedir", width=600, height=400
    ):
        dpg.add_file_extension("")

    with dpg.file_dialog(
        directory_selector=True, show=False, callback=_file_callback,
        tag="file_dialog_level", width=600, height=400
    ):
        dpg.add_file_extension("")

    # --- main window ---
    with dpg.window(tag="main_window",
                     label="CrisTical - Crysis3D Converter",
                     autosize=True, no_resize=False, no_collapse=True,
                     min_size=(680, 640)):
        with dpg.group(horizontal=True):
            dpg.add_text("CrisTical Crysis3D Converter v2.1", color=(255, 200, 100))
            dpg.add_text("  |  by Soror L.'.L.'. aka Methelina", color=(140, 145, 155))
            dpg.add_button(label="github", small=True, tag="btn_github",
                           callback=lambda: webbrowser.open("https://github.com/Methelina/CrisTical_Crysis-1_3DConverter"))
            with dpg.tooltip("btn_github"):
                dpg.add_text("Open project repository on GitHub")
        dpg.add_separator()

        dpg.add_spacer(height=4)

        # --- shared status strip (both pipelines report into it) ---
        with dpg.group(indent=4):
            with dpg.group(horizontal=True):
                with dpg.drawlist(width=20, height=20):
                    dpg.draw_circle(center=(10, 10), radius=7, tag=SC, fill=(110, 110, 110))
                dpg.add_text("Status:", color=(255, 200, 100))
                dpg.add_text("IDLE", tag=ST, color=(110, 110, 110))

        dpg.add_spacer(height=6)

        # --- Game Directories (shared: CDF pipeline, Map, unpack, ...) ---
        with dpg.group(indent=4):
            with dpg.group(horizontal=True):
                with dpg.drawlist(width=20, height=20):
                    dpg.draw_circle(center=(10, 10), radius=7, tag=GC, fill=(110, 110, 110))
                dpg.add_text("Game Directories", color=(255, 200, 100))
                dpg.add_text("No directories", tag=GT, color=(110, 110, 110))
                dpg.add_button(label="+", small=True, width=22, height=18,
                               callback=_add_game_dir, tag=GD_ADD_BTN)
                dpg.bind_item_theme(GD_ADD_BTN, add_btn_theme)
            with dpg.tooltip(GD_ADD_BTN):
                dpg.add_text("Add a Crysis game directory (folder with Animations.pak or Objects/)")
            dpg.add_text('e.g. F:\\Games\\Crysis_Remastered\\Game  (folder with Animations.pak)', color=(90, 95, 105))

            with dpg.child_window(height=80, border=True, tag=GD_GROUP):
                pass

            with dpg.group(horizontal=True):
                dpg.add_text("Game:", color=(255, 200, 100))
                dpg.add_text("Auto-detect", tag="game_detect_text", color=(235, 205, 130))
                dpg.add_combo(items=list(GAME_TITLES), default_value=GAME_AUTO,
                              width=170, tag=GAME_TITLE_COMBO,
                              callback=_on_game_title_change)
                with dpg.tooltip(GAME_TITLE_COMBO):
                    dpg.add_text("Edition: Crysis 1 / 2 / 3, Warhead, Remastered, Wars.\n"
                                 "Auto-detect reads the data root from the pak. Choosing a title\n"
                                 "maps an added install folder to its canonical data root.")

        dpg.add_spacer(height=6)

        # --- Output (shared) ---
        with dpg.group(indent=4):
            dpg.add_text("Output Directory", color=(255, 200, 100))
            with dpg.group(horizontal=True):
                dpg.add_input_text(default_value=str(OUTPUT_DEFAULT), width=420, height=24,
                                    tag=OUTPUT_INPUT, hint="All outputs (.gltf, .bin, textures, .log) placed here")
                dpg.add_button(label="Browse", callback=browse_output, small=True, tag="btn_browse_out")
                with dpg.tooltip("btn_browse_out"):
                    dpg.add_text("Select where to save converted files (.gltf, .bin, textures, log)")

        dpg.add_spacer(height=6)

        # --- top-level tabs: model pipeline vs level/map tooling ---
        with dpg.tab_bar(tag=TAB_BAR):
            # ---------------- Tab 1: CDF Pipeline ----------------
            with dpg.tab(label="CDF Pipeline", tag=TAB_CDF):
                with dpg.group(indent=4):
                    dpg.add_text("CDF File", color=(255, 200, 100))
                    with dpg.group(horizontal=True):
                        dpg.add_input_text(default_value="", width=420, height=24, tag=CDF_INPUT,
                                            hint="Path to .cdf or .chr file")
                        dpg.add_button(label="Browse", callback=browse_cdf, small=True, tag="btn_browse_cdf")
                        with dpg.tooltip("btn_browse_cdf"):
                            dpg.add_text("Select a .cdf (Character Definition File) or .chr model file")
                        dpg.add_button(label="Scan", callback=scan_model, small=True, tag="btn_scan")
                        with dpg.tooltip("btn_scan"):
                            dpg.add_text("Detect bones, primitives, materials, and animations in the file")

                    dpg.add_spacer(height=2)
                    dpg.add_text("", tag=SCAN_LABEL, color=(160, 165, 175))
                    dpg.add_text("", tag=DETECT_LABEL, color=(140, 145, 155))

                dpg.add_spacer(height=6)

                # --- Pipeline options ---
                with dpg.group(indent=4):
                    dpg.add_text("Pipeline Options", color=(255, 200, 100))
                    with dpg.group(horizontal=True):
                        dpg.add_text("Animation mode:", color=(200, 200, 200))
                        dpg.add_combo(items=ANIM_MODES, default_value=ANIM_MODES[0],
                                        width=200, tag=ANIM_MODE_COMBO, callback=_on_option_changed)
                        dpg.add_spacer(width=12)
                        dpg.add_text("Texture mode:", color=(200, 200, 200))
                        dpg.add_combo(items=TEX_MODES, default_value=TEX_MODES[0],
                                        width=140, tag=TEX_MODE_COMBO, callback=_on_option_changed)
                        dpg.add_spacer(width=12)
                        dpg.add_checkbox(label="Output .glb", default_value=False,
                                          tag=GLB_CHECK, callback=_on_option_changed)
                        dpg.add_checkbox(label="Extract collision mesh", default_value=False,
                                          tag=COLLISION_CHECK, callback=_on_option_changed)

                dpg.add_spacer(height=4)

                # --- CLI preview ---
                with dpg.group(indent=4):
                    dpg.add_text("", tag=CLI_LABEL, color=(120, 125, 135))

                dpg.add_spacer(height=8)

                # --- Run button ---
                with dpg.group(indent=4):
                    dpg.add_button(label=">> CONVERT <<", tag=RUN_BTN, callback=run_conversion,
                                   width=200, height=30)
                    with dpg.tooltip(RUN_BTN):
                        dpg.add_text("Start the conversion pipeline: skeleton + mesh + materials + textures + animations")

            # ---------------- Tab 2: Map ----------------
            with dpg.tab(label="Map", tag=TAB_MAP):
                with dpg.group(indent=4):
                    dpg.add_text("Level -> JSON (unpacked level directory)", color=(255, 200, 100))
                    dpg.add_text("Level tooling is a separate concern from the model pipeline "
                                 "and lives on its own tab.", color=(90, 95, 105))
                    with dpg.group(horizontal=True):
                        dpg.add_input_text(default_value="", width=420, height=24, tag=LEVEL_INPUT,
                                            hint="Path to level_Unpacked (levelinfo.xml, terrain\\terrain.dat)")
                        dpg.add_button(label="Browse", callback=browse_level, small=True, tag="btn_browse_level")
                        with dpg.tooltip("btn_browse_level"):
                            dpg.add_text("Select an unpacked level directory (level.pak extracted, e.g. via unpack tool)")
                    with dpg.group(horizontal=True):
                        dpg.add_checkbox(label="Visual-only entities", default_value=False, tag=LEVEL_VISUAL_CHECK)
                        dpg.add_spacer(width=12)
                        dpg.add_checkbox(label="Skip vegetation instances", default_value=False, tag=LEVEL_SKIP_VEG_CHECK)
                        dpg.add_spacer(width=12)
                        dpg.add_button(label="Export level JSON", callback=run_level_export,
                                       small=True, tag=LEVEL_RUN_BTN)
                        with dpg.tooltip(LEVEL_RUN_BTN):
                            dpg.add_text("Run level2json.py: entities, lights, brushes, vegetation -> single JSON\n"
                                         "Uses the first game directory for profile detection (Crysis Remastered enables terrain.dat)")

                dpg.add_spacer(height=8)

                # --- future Map functions go here ---
                with dpg.group(indent=4):
                    dpg.add_text("Further level features", color=(255, 200, 100))
                    dpg.add_text("(terrain, geometry, vegetation placement export etc. — roadmap)", color=(100, 105, 115))

        dpg.add_separator()
        dpg.add_spacer(height=2)

        # --- Log (shared across all tabs) ---
        with dpg.group(indent=4):
            dpg.add_checkbox(label="Auto-scroll", tag=CK, default_value=True)
            dpg.add_input_text(tag=LG, multiline=True, readonly=True, width=-1, height=-1,
                                tracked=True)

    dpg.create_viewport(title="CrisTical - Crysis3D Converter v2.1", width=720, height=700)
    if fonts_ok:
        dpg.bind_item_font("main_window", "font_regular")
    dpg.setup_dearpygui()
    dpg.show_viewport()
    dpg.maximize_viewport()
    dpg.set_primary_window("main_window", True)

    _rebuild_gamedirs_ui()
    _build_cli_preview()


def main():
    ct("GUI", "CrisTical GUI starting...")
    ct("GUI", "Project root: %s" % PROJ_ROOT)
    ct("GUI", "Output: %s" % OUTPUT_DEFAULT)

    build_gui()

    try:
        while dpg.is_dearpygui_running():
            dq()
            _render_log()
            dpg.render_dearpygui_frame()
    finally:
        _sd.set()
        dpg.destroy_context()

    return 0


if __name__ == "__main__":
    sys.exit(main())
