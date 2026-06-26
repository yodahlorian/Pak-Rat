"""
Pak Rat — UE cooking toolchain (v2.0.0).

v1 was a pure packager (user supplied pre-cooked .uasset). v2 adds a real
cooker: the user supplies a raw mesh (FBX/OBJ/glTF/…) and Pak Rat drives an
installed Unreal Editor to import + cook it into a drop-in pak.

Proven end-to-end 2026-06-26 (rubixcube → cube/sphere swap, confirmed in-game).

What's automated here:
  * detect installed UE via Epic's LauncherInstalled.dat (+ Program Files + reg)
  * download + unzip a portable Blender (mesh converter / normalizer)
  * generate a stub "RetroRewind" cook project (name matters: /Game →
    RetroRewind/Content, matching the shipped pak's internal paths)
  * cook: blender-convert → UnrealEditor-Cmd import → UnrealEditor-Cmd cook
  * the cooked mesh is then packed by the EXISTING repak back-end in core.py

What the user must do ONCE (cannot be automated — Epic EULA, ~40 GB):
  install Unreal Engine (5.3/5.4) via the Epic Games Launcher.

Material handling (the key trick): we create *stub* MaterialInstanceConstants at
the exact /Game paths the original game mesh used and assign them to the imported
mesh's slots. Only the path+name+class matter — at runtime the loader links to
the REAL game material in the base pak. We pack ONLY the cooked mesh.
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import core  # reuse repak helpers, base-pak discovery, ref scanner, deploy

# --- Blender (portable) ----------------------------------------------------
BLENDER_URL = "https://download.blender.org/release/Blender4.2/blender-4.2.9-windows-x64.zip"

# Mesh source formats we accept (Blender converts non-FBX → FBX for UE import)
MESH_SOURCE_EXTS = {".fbx", ".obj", ".gltf", ".glb", ".stl", ".ply", ".dae", ".blend"}

PROJECT_NAME = "RetroRewind"  # must match the game's content mount


# ---------------------------------------------------------------------------
# Per-user toolchain home (Blender + stub project live here, not next to exe)
# ---------------------------------------------------------------------------
def home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / "PakRat" / "cook"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfg_path() -> Path:
    return home() / "config.json"


def load_cfg() -> dict:
    p = _cfg_path()
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_cfg(cfg: dict) -> None:
    try:
        _cfg_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Unreal Engine detection
# ---------------------------------------------------------------------------
def detect_ue() -> list[dict]:
    """Every UE with a cook binary: [{version, root, cmd}], newest first."""
    found: dict[str, dict] = {}
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pd = os.environ.get("ProgramData", r"C:\ProgramData")

    def add(root: str, ver: str):
        cmd = os.path.join(root, "Engine", "Binaries", "Win64",
                           "UnrealEditor-Cmd.exe")
        if root not in found and os.path.isfile(cmd):
            found[root] = {"version": ver or "?", "root": root, "cmd": cmd}

    # 1) Epic launcher manifest — canonical
    manifest = os.path.join(pd, "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat")
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            for it in json.load(f).get("InstallationList", []):
                if it.get("ArtifactId", "").startswith("UE_"):
                    add(it["InstallLocation"], it.get("AppVersion", "").split("-")[0])
    except Exception:
        pass
    # 2) Standard install dir
    for d in glob.glob(os.path.join(pf, "Epic Games", "UE_*")):
        add(d, os.path.basename(d).replace("UE_", ""))
    # 3) Registry — source / custom builds
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Epic Games\Unreal Engine\Builds")
        i = 0
        while True:
            name, val, _ = winreg.EnumValue(k, i); i += 1
            add(val, name)
    except Exception:
        pass
    return sorted(found.values(), key=lambda e: _verkey(e["version"]), reverse=True)


def _verkey(v: str) -> list[int]:
    return [int(p) if str(p).isdigit() else 0 for p in str(v).split(".")]


def pick_ue(installs: list[dict] | None = None, prefer=("5.4", "5.3")) -> dict | None:
    """Choose the engine to cook with. RR is 5.3/5.4-compatible."""
    installs = installs if installs is not None else detect_ue()
    saved = load_cfg().get("ue_root")
    if saved:
        for e in installs:
            if e["root"] == saved:
                return e
    for pref in prefer:
        for e in installs:
            if e["version"].startswith(pref):
                return e
    return installs[0] if installs else None


def ue_available() -> bool:
    return bool(detect_ue())


# ---------------------------------------------------------------------------
# Blender
# ---------------------------------------------------------------------------
def detect_blender() -> str:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cands = glob.glob(str(home() / "blender" / "*" / "blender.exe"))
    cands += glob.glob(os.path.join(pf, "Blender Foundation", "*", "blender.exe"))
    saved = load_cfg().get("blender_exe")
    if saved and os.path.isfile(saved):
        return saved
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


def ensure_blender(progress=None) -> str:
    """Return blender.exe path, downloading the portable build if missing.

    progress(msg, pct|None) — pct 0..100 for the download/extract bar.
    """
    have = detect_blender()
    if have:
        if progress:
            progress("Blender ready.", 100)
        return have
    dest = home() / "blender"
    dest.mkdir(parents=True, exist_ok=True)
    zp = dest / "_blender.zip"

    def hook(count, bsize, total):
        if progress and total > 0:
            pct = min(99, int(count * bsize * 100 / total))
            progress("Downloading Blender…  %d%%" % pct, pct)

    if progress:
        progress("Downloading Blender (~370 MB)…", 0)
    urllib.request.urlretrieve(BLENDER_URL, str(zp), hook)

    with zipfile.ZipFile(zp) as z:
        members = z.namelist()
        n = len(members) or 1
        for i, m in enumerate(members):
            z.extract(m, dest)
            if progress and (i % 250 == 0 or i == n - 1):
                progress("Extracting Blender…  %d%%" % int(i * 100 / n),
                         int(i * 100 / n))
    try:
        zp.unlink()
    except Exception:
        pass
    bl = detect_blender()
    cfg = load_cfg(); cfg["blender_exe"] = bl; save_cfg(cfg)
    if progress:
        progress("Blender ready.", 100)
    return bl


# ---------------------------------------------------------------------------
# Stub cook project
# ---------------------------------------------------------------------------
def project_dir() -> Path:
    return home() / PROJECT_NAME


def uproject_path() -> Path:
    return project_dir() / f"{PROJECT_NAME}.uproject"


_UPROJECT = """{
\t"FileVersion": 3,
\t"EngineAssociation": "%s",
\t"Category": "",
\t"Description": "Pak Rat v2 cook project (auto-generated). Name must stay RetroRewind so /Game maps to RetroRewind/Content, matching the shipped pak paths.",
\t"Modules": [],
\t"Plugins": [
\t\t{ "Name": "PythonScriptPlugin", "Enabled": true },
\t\t{ "Name": "EditorScriptingUtilities", "Enabled": true }
\t]
}
"""

# Import script driven by a job.json (path passed as the only arg after the
# script). job = {"items": [{fbx, mesh_game, materials:[...]}, ...]} — supports
# packing many meshes into one pak. Baked-in fixes from the PoC: no
# imported_material_slot_name kwarg; force-save with only_if_is_dirty=False.
_IMPORT_SCRIPT = r'''
import unreal, json, sys, traceback

JOB = sys.argv[-1]
RESULT = JOB + ".result"
def note(m):
    with open(RESULT, "a", encoding="utf-8") as f:
        f.write(str(m) + "\n")
    try: unreal.log("PAKRAT " + str(m))
    except Exception: pass

try:
    job = json.load(open(JOB, encoding="utf-8"))
    items = job["items"]
    at  = unreal.AssetToolsHelpers.get_asset_tools()
    eal = unreal.EditorAssetLibrary
    base = unreal.load_asset("/Engine/BasicShapes/BasicShapeMaterial")

    done = 0
    for item in items:
        fbx        = item["fbx"]
        mesh_game  = item["mesh_game"]            # /Game/.../LA_X
        materials  = item.get("materials", [])    # ordered [/Game/.../MI_Y, ...]
        mesh_pkg   = mesh_game.rsplit("/", 1)[0]
        mesh_name  = mesh_game.rsplit("/", 1)[-1]

        opt = unreal.FbxImportUI()
        opt.import_mesh = True
        opt.import_as_skeletal = False
        opt.import_materials = False
        opt.import_textures = False
        opt.mesh_type_to_import = unreal.FBXImportType.FBXIT_STATIC_MESH
        opt.static_mesh_import_data.combine_meshes = True
        opt.static_mesh_import_data.generate_lightmap_u_vs = True

        task = unreal.AssetImportTask()
        task.filename = fbx
        task.destination_path = mesh_pkg
        task.destination_name = mesh_name
        task.replace_existing = True
        task.automated = True
        task.save = True
        task.options = opt
        at.import_asset_tasks([task])

        # stub MaterialInstanceConstants at the game MI paths
        mics = []
        for mi in materials:
            pkg  = mi.rsplit("/", 1)[0]
            name = mi.rsplit("/", 1)[-1]
            if not eal.does_asset_exist(mi):
                m = at.create_asset(name, pkg, unreal.MaterialInstanceConstant,
                                    unreal.MaterialInstanceConstantFactoryNew())
                if base: m.set_editor_property("parent", base)
                eal.save_asset(mi, only_if_is_dirty=False)
            mics.append(unreal.load_asset(mi))

        # assign slots -> game MIs (slot i -> MI[min(i,last)])
        sm = unreal.load_asset(mesh_game)
        slots = sm.get_editor_property("static_materials")
        if mics and slots:
            new = []
            for i in range(len(slots)):
                mic = mics[min(i, len(mics) - 1)]
                new.append(unreal.StaticMaterial(material_interface=mic,
                                                 material_slot_name="Mat%d" % i))
            sm.set_editor_property("static_materials", new)
            sm.modify()
            eal.save_asset(mesh_game, only_if_is_dirty=False)
        done += 1
        note("ITEM %s slots=%d mics=%d" % (mesh_name, len(slots), len(mics)))
    note("DONE %d" % done)
except Exception:
    note("ERROR:\n" + traceback.format_exc())
'''


def import_script_path() -> Path:
    return project_dir() / "pakrat_import.py"


def create_project(ue_version: str = "5.4", progress=None) -> str:
    """Generate the stub uproject + content dir + import script. Idempotent."""
    pd = project_dir()
    (pd / "Content").mkdir(parents=True, exist_ok=True)
    uproject_path().write_text(_UPROJECT % ue_version, encoding="utf-8")
    import_script_path().write_text(_IMPORT_SCRIPT, encoding="utf-8")
    if progress:
        progress("Cook project ready.", 100)
    return str(uproject_path())


# ---------------------------------------------------------------------------
# Setup orchestration (drives the first-run SetupPage progress bar)
# ---------------------------------------------------------------------------
@dataclass
class CookEnv:
    ue_cmd: str
    ue_version: str
    blender_exe: str
    uproject: str


def is_ready() -> bool:
    return bool(pick_ue()) and bool(detect_blender()) and uproject_path().is_file()


def cook_env() -> CookEnv | None:
    ue = pick_ue()
    if not ue:
        return None
    return CookEnv(ue_cmd=ue["cmd"], ue_version=ue["version"],
                   blender_exe=detect_blender(), uproject=str(uproject_path()))


def setup(progress=None) -> CookEnv:
    """First-run setup: confirm UE, install Blender, build the cook project.

    progress(msg, pct|None): pct drives a determinate bar (download/extract);
    None = indeterminate phase. Raises if no UE is installed.
    """
    def say(m, p=None):
        if progress:
            progress(m, p)

    say("Detecting Unreal Engine…", None)
    ue = pick_ue()
    if not ue:
        raise RuntimeError(
            "No Unreal Engine install found. Install UE 5.4 (or 5.3) via the "
            "Epic Games Launcher, then reopen Pak Rat.")
    cfg = load_cfg(); cfg["ue_root"] = ue["root"]; save_cfg(cfg)
    say("Found Unreal Engine %s." % ue["version"], None)

    blender = ensure_blender(progress=progress)
    if not blender:
        raise RuntimeError("Blender download/extract failed.")

    say("Creating cook project…", None)
    create_project(ue["version"], progress=progress)

    say("Setup complete.", 100)
    return CookEnv(ue_cmd=ue["cmd"], ue_version=ue["version"],
                   blender_exe=blender, uproject=str(uproject_path()))


# ---------------------------------------------------------------------------
# Path helpers (game mount path  <->  /Game logical path  <->  cooked output)
# ---------------------------------------------------------------------------
def mount_to_game(mount: str) -> str:
    """RetroRewind/Content/VideoStore/asset/X  ->  /Game/VideoStore/asset/X"""
    mount = mount.strip().rstrip("/")
    if mount.startswith("RetroRewind/Content/"):
        return "/Game/" + mount[len("RetroRewind/Content/"):]
    return "/Game/" + mount.lstrip("/")


def _cooked_dir() -> Path:
    return project_dir() / "Saved" / "Cooked" / "Windows"


# ---------------------------------------------------------------------------
# Material resolution (which game MIs the target mesh uses) — reuse core
# ---------------------------------------------------------------------------
def resolve_mesh_materials(mesh_mount: str) -> list[str]:
    """Ordered /Game paths of the MI_ materials the target game mesh references."""
    core.ensure_oodle()
    with tempfile.TemporaryDirectory() as tmp:
        ua = core._extract_asset(mesh_mount, tmp)
        if not ua:
            return []
        mis, seen = [], set()
        for ref in core._scan_refs(ua, ua[:-7] + ".uexp"):
            if core._classify(ref) == "material" and ref not in seen:
                seen.add(ref)
                mis.append(mount_to_game(ref))
        return mis


# ---------------------------------------------------------------------------
# Smart multi-part detection (e.g. fishbowl = bowl + base + water as separate
# meshes). Two sources: name-stem siblings in the same folder + any mesh-kind
# refs in the target's import table (composite props).
# ---------------------------------------------------------------------------
import re as _re  # noqa: E402


def _variant_prefix(leaf: str) -> str:
    """Strip trailing variant/index tokens: LA_FishBowl_A_01 -> LA_FishBowl,
    LA_FishBowl_Base_A_01 -> LA_FishBowl_Base."""
    toks = leaf.split("_")
    while len(toks) > 2 and (
            toks[-1].isdigit()
            or (len(toks[-1]) == 1 and toks[-1].isalpha())
            or _re.fullmatch(r"[A-Za-z]?\d+", toks[-1])):
        toks.pop()
    return "_".join(toks)


def related_meshes(mesh_mount: str, limit: int = 24) -> list[str]:
    """Sibling/part meshes a user might want to swap alongside this one.

    Conservative: same folder + shares the variant prefix (≥3 tokens so we don't
    match half the game), plus any mesh-kind refs found in the import table.
    Returns mount paths, excluding the input mesh. Empty on any failure.
    """
    mesh_mount = mesh_mount.strip().rstrip("/")
    out, seen = [], {mesh_mount}
    try:
        leaf = mesh_mount.rsplit("/", 1)[-1]
        folder = mesh_mount.rsplit("/", 1)[0]
        prefix = _variant_prefix(leaf)
        # name-stem siblings (instant — string match on the dropdown list).
        # Match on a token boundary (prefix + "_") so LA_Fish doesn't bleed into
        # LA_FishBowl. Skip bare category prefixes that would match everything.
        if len(prefix.split("_")) >= 2 and prefix not in ("LA", "SM", "SK", "SKM"):
            for m in core.load_meshes():
                if m in seen:
                    continue
                if m.rsplit("/", 1)[0] == folder and \
                        m.rsplit("/", 1)[-1].startswith(prefix + "_"):
                    seen.add(m)
                    out.append(m)
        # composite refs (mesh-kind entries in the import table)
        core.ensure_oodle()
        with tempfile.TemporaryDirectory() as tmp:
            ua = core._extract_asset(mesh_mount, tmp)
            if ua:
                for ref in core._scan_refs(ua, ua[:-7] + ".uexp"):
                    if core._classify(ref) == "mesh" and ref not in seen:
                        seen.add(ref)
                        out.append(ref)
    except Exception:
        return out[:limit]
    return sorted(out)[:limit]


# ---------------------------------------------------------------------------
# Cook execution
# ---------------------------------------------------------------------------
def convert_to_fbx(env: CookEnv, src: str, progress=None) -> str:
    """Return an FBX path for `src` (pass-through if already .fbx; else Blender)."""
    if Path(src).suffix.lower() == ".fbx":
        return src
    if progress:
        progress("Converting %s → FBX…" % Path(src).suffix.lstrip("."), None)
    out = str(home() / "_convert" / (Path(src).stem + ".fbx"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    script = home() / "_convert.py"
    script.write_text(_CONVERT_SCRIPT, encoding="utf-8")
    r = core._run([env.blender_exe, "--background", "--python", str(script),
                   "--", src, out])
    if not os.path.isfile(out):
        raise RuntimeError("Blender conversion failed:\n%s" % (r.stderr or r.stdout))
    return out


_CONVERT_SCRIPT = r'''
import bpy, sys
src, out = sys.argv[-2], sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=True)
e = src.lower().rsplit(".", 1)[-1]
if   e == "obj":            bpy.ops.wm.obj_import(filepath=src)
elif e in ("gltf", "glb"):  bpy.ops.import_scene.gltf(filepath=src)
elif e == "stl":            bpy.ops.wm.stl_import(filepath=src)
elif e == "ply":            bpy.ops.wm.ply_import(filepath=src)
elif e == "dae":            bpy.ops.wm.collada_import(filepath=src)
elif e == "blend":          bpy.ops.wm.open_mainfile(filepath=src)
else:                       bpy.ops.import_scene.fbx(filepath=src)
bpy.ops.export_scene.fbx(filepath=out, use_selection=False,
    object_types={"MESH"}, apply_unit_scale=True, mesh_smooth_type="FACE",
    add_leaf_bones=False, path_mode="COPY")
print("PAKRAT_CONVERTED", out)
'''


def _ue(env: CookEnv, *args: str) -> subprocess.CompletedProcess:
    return core._run([env.ue_cmd, env.uproject, *args,
                      "-stdout", "-unattended", "-nopause", "-nosplash"])


def cook_meshes(env: CookEnv, items: list[dict], progress=None) -> list[dict]:
    """Cook many meshes in ONE editor session (efficient — one spin-up/cook).

    items: [{'src': raw mesh file, 'target': game mesh mount}, …]
    Returns [{'uasset', 'uexp', 'mount'}] for each item.
    """
    def say(m, p=None):
        if progress:
            progress(m, p)

    job_items = []
    for it in items:
        fbx = convert_to_fbx(env, it["src"], progress=progress)
        target = it["target"]
        say("Resolving materials for %s…" % target.rsplit("/", 1)[-1], None)
        job_items.append({"fbx": fbx, "mesh_game": mount_to_game(target),
                          "materials": resolve_mesh_materials(target)})

    job_path = home() / "job.json"
    result = Path(str(job_path) + ".result")
    if result.exists():
        result.unlink()
    job_path.write_text(json.dumps({"items": job_items}, indent=2), encoding="utf-8")
    # always refresh the import script so it matches this build (handles upgrades)
    import_script_path().write_text(_IMPORT_SCRIPT, encoding="utf-8")

    say("Importing %d mesh(es) into Unreal…" % len(items), None)
    r = _ue(env, "-ExecutePythonScript=%s %s" % (import_script_path(), job_path))
    res_txt = result.read_text(encoding="utf-8") if result.exists() else ""
    if "DONE" not in res_txt:
        raise RuntimeError("Import failed:\n%s\n%s" % (res_txt, (r.stderr or "")[-800:]))

    say("Cooking (first run builds shaders — may take a few minutes)…", None)
    r = _ue(env, "-run=cook", "-targetplatform=Windows", "-unversioned", "-cookall")
    so = r.stdout or ""
    if "Success" not in so and "Packages Remain 0" not in so:
        raise RuntimeError("Cook failed:\n%s" % (so[-1200:] or (r.stderr or "")[-1200:]))

    out = []
    for it in items:
        target = it["target"]
        base = _cooked_dir() / Path(*target.split("/"))
        ua, ux = base.with_suffix(".uasset"), base.with_suffix(".uexp")
        if not ua.is_file():
            raise RuntimeError("Cook produced no .uasset for %s" % target)
        out.append({"uasset": str(ua), "uexp": str(ux) if ux.is_file() else "",
                    "mount": target})
    say("Cook complete.", None)
    return out


def cook_mesh(env: CookEnv, src_mesh: str, target_mesh_mount: str,
              progress=None) -> dict:
    """Single-mesh convenience wrapper around cook_meshes()."""
    return cook_meshes(env, [{"src": src_mesh, "target": target_mesh_mount}],
                       progress=progress)[0]


def run_cook_pipeline_multi(items: list[dict], progress=None) -> str:
    """Cook every item and pack them into ONE .pak. Reuses core's repak.

    items: [{'src','target'}, …]. Returns the built .pak path.
    """
    import shutil

    def say(m, p=None):
        if progress:
            progress(m, p)

    env = cook_env()
    if env is None:
        raise RuntimeError("Cook environment not set up.")

    cooked = cook_meshes(env, items, progress=progress)

    say("Packaging .pak…", None)
    work = Path(tempfile.mkdtemp(prefix="pakrat_cook_"))
    stage = work / "stage"
    for c in cooked:
        leaf = c["mount"].rsplit("/", 1)[-1]
        rel_dir = c["mount"].rsplit("/", 1)[0]
        d = stage / Path(*rel_dir.split("/"))
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(c["uasset"], d / f"{leaf}.uasset")
        if c["uexp"]:
            shutil.copy2(c["uexp"], d / f"{leaf}.uexp")

    first = cooked[0]["mount"].rsplit("/", 1)[-1]
    label = first if len(cooked) == 1 else f"{first}_plus{len(cooked) - 1}"
    out_pak = work / f"zzz_PakRat_{label}_P.pak"
    core._repak("pack", "--version", core.PAK_VERSION, "--mount-point", core.PAK_MOUNT,
                "--path-hash-seed", core.PAK_SEED, str(stage), str(out_pak))
    say("Done.", 100)
    return str(out_pak)


def run_cook_pipeline(src_mesh: str, target_mesh_mount: str, progress=None) -> str:
    """Single-mesh convenience wrapper around run_cook_pipeline_multi()."""
    return run_cook_pipeline_multi(
        [{"src": src_mesh, "target": target_mesh_mount}], progress=progress)


def valid_mesh_source(path: str) -> bool:
    return Path(path).suffix.lower() in MESH_SOURCE_EXTS
