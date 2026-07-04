"""
Pak Rat — mesh extraction (cooked StaticMesh -> uemodel / glTF / FBX / OBJ).

Reads the game's COOKED StaticMesh assets via CUE4Parse (the same reader FModel
uses), hosted in-process on the bundled .NET 8 runtime that relink already stands
up. CUE4Parse-Conversion exports `.uemodel` and `.glb` (glTF2) directly — plus the
mesh's textures/materials; FBX and OBJ are produced by converting the `.glb`
through the vendored Blender. This is the reverse of the Add-Asset cook path, so a
game mesh can be pulled out and re-imported.

All wiring here was proven end-to-end against the real RR pak (21.9k files mounted,
oodle + RR .usmap, LA_Chair_A_01 -> .uemodel/.glb).
"""
from __future__ import annotations

import glob
import os
import shutil
import tempfile
from pathlib import Path

import core

# ui format -> (CUE4Parse EMeshFormat, output ext, produced-via-Blender-from-glb)
FORMATS = {
    "uemodel": ("UEFormat", ".uemodel", False),
    "gltf":    ("Gltf2",    ".glb",     False),
    "fbx":     ("Gltf2",    ".fbx",     True),
    "obj":     ("Gltf2",    ".obj",     True),
}
GEOMETRY_FORMATS = ("uasset",) + tuple(FORMATS)   # "uasset" = raw cooked (no reader)

_LOADED = False
_PROVIDER = None


def _ensure_clr() -> None:
    """Host coreclr (via relink) and make CUE4Parse resolvable from vendor/cue4parse."""
    global _LOADED
    if _LOADED:
        return
    import relink
    relink._ensure()                       # bundled .NET 8 runtime + `clr` import hook
    import clr
    from System import AppDomain, Reflection, ResolveEventHandler
    vdir = str(core.VENDOR("cue4parse"))
    # native deps (SkiaSharp, etc.) resolve from the vendor dir in the frozen exe
    try:
        os.add_dll_directory(vdir)
    except (OSError, AttributeError):
        pass

    def _resolve(_sender, args):
        name = args.Name.split(",")[0]
        p = os.path.join(vdir, name + ".dll")
        return Reflection.Assembly.LoadFrom(p) if os.path.isfile(p) else None

    AppDomain.CurrentDomain.AssemblyResolve += ResolveEventHandler(_resolve)
    clr.AddReference(os.path.join(vdir, "CUE4Parse.dll"))
    clr.AddReference(os.path.join(vdir, "CUE4Parse-Conversion.dll"))
    _LOADED = True


def _provider():
    """Cached CUE4Parse provider mounted on the installed RR game paks."""
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER
    _ensure_clr()
    from CUE4Parse.Compression import OodleHelper
    from CUE4Parse.Encryption.Aes import FAesKey
    from CUE4Parse.FileProvider import DefaultFileProvider
    from CUE4Parse.MappingsProvider import FileUsmapTypeMappingsProvider
    from CUE4Parse.UE4.Objects.Core.Misc import FGuid
    from CUE4Parse.UE4.Versions import EGame, VersionContainer
    from System.IO import SearchOption

    OodleHelper.Initialize(str(core.VENDOR("oo2core_9_win64.dll")))
    paks = str(core._rr_paks_dir())        # the game's <...>/Content/Paks (native path)
    prov = DefaultFileProvider(
        paks, SearchOption.AllDirectories, VersionContainer(EGame.GAME_UE5_4))
    prov.Initialize()
    # RR paks are unencrypted — the zero key mounts them.
    prov.SubmitKey(FGuid(0, 0, 0, 0), FAesKey("0x" + "00" * 32))
    prov.MappingsContainer = FileUsmapTypeMappingsProvider(
        str(core.VENDOR("RetroRewindMappings.usmap")))
    _PROVIDER = prov
    return prov


def _load_static_mesh(mount: str):
    """Load the UStaticMesh export from the package at `mount` (no extension)."""
    pkg = _provider().LoadPackage(mount)          # sets up the CLR first
    from CUE4Parse.UE4.Assets.Exports.StaticMesh import UStaticMesh
    for i in range(400):
        try:
            e = pkg.GetExport(i)
        except Exception:  # noqa: BLE001  (index past the end throws)
            break
        if isinstance(e, UStaticMesh):
            return e
    raise RuntimeError(f"no StaticMesh export found in '{mount}'")


def _blender_glb_convert(glb: str, out: str, progress=None) -> None:
    """Import a .glb in the vendored Blender and export it as FBX or OBJ."""
    import cook
    bl = cook.ensure_blender(progress)
    script = cook.home() / "_meshx_convert.py"
    script.write_text(_GLB_CONVERT_SCRIPT, encoding="utf-8")
    r = core._run([bl, "--background", "--python", str(script), "--", glb, out])
    if not os.path.isfile(out):
        raise RuntimeError("Blender glb->mesh conversion failed:\n%s"
                           % (r.stderr or r.stdout))


_GLB_CONVERT_SCRIPT = r'''
import bpy, sys
glb, out = sys.argv[-2], sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)
e = out.lower().rsplit(".", 1)[-1]
if e == "obj":
    bpy.ops.wm.obj_export(filepath=out, export_selected_objects=False)
else:  # fbx
    bpy.ops.export_scene.fbx(filepath=out, use_selection=False,
        object_types={"MESH"}, add_leaf_bones=False, path_mode="COPY")
print("PAKRAT_MESHX", out)
'''


def export_mesh(mount: str, dest_dir: str, fmt: str, progress=None) -> list[str]:
    """Extract the cooked StaticMesh at `mount` into `dest_dir/<leaf>/` as `fmt`.

    fmt in FORMATS ('uemodel'|'gltf'|'fbx'|'obj'). Returns the written file paths.
    uemodel/gltf are handed back with their textures; fbx/obj are converted from
    the glb via Blender. Raises on failure (caller skips + reports per asset).
    """
    fmt = fmt.lower()
    mformat, ext, via_blender = FORMATS[fmt]
    leaf = mount.rstrip("/").split("/")[-1]
    if progress:
        progress(f"Reading {leaf} ({fmt})…")

    sm = _load_static_mesh(mount)

    from CUE4Parse_Conversion import ExporterOptions
    from CUE4Parse_Conversion.Meshes import EMeshFormat, MeshExporter
    from System.IO import DirectoryInfo

    stage = tempfile.mkdtemp(prefix="pakrat_meshx_")
    try:
        opts = ExporterOptions()
        opts.MeshFormat = getattr(EMeshFormat, mformat)
        MeshExporter(sm, opts).TryWriteToDir(DirectoryInfo(stage))

        primary = None
        for f in glob.glob(os.path.join(stage, "**", "*"), recursive=True):
            if os.path.isfile(f) and f.lower().endswith((".glb", ".uemodel")):
                primary = f
        if not primary:
            raise RuntimeError(f"exporter produced no mesh file for '{leaf}'")

        out_dir = os.path.join(dest_dir, leaf)
        os.makedirs(out_dir, exist_ok=True)
        written = []
        if via_blender:
            out = os.path.join(out_dir, leaf + ext)
            if progress:
                progress(f"Converting {leaf} → {fmt.upper()}…")
            _blender_glb_convert(primary, out, progress=progress)
            written.append(out)
            # carry the textures alongside the converted mesh
            for f in glob.glob(os.path.join(stage, "*.png")):
                dst = os.path.join(out_dir, os.path.basename(f))
                shutil.copy2(f, dst)
                written.append(dst)
        else:
            # hand back the mesh + its textures/materials as-is
            for f in glob.glob(os.path.join(stage, "**", "*"), recursive=True):
                if os.path.isfile(f):
                    dst = os.path.join(out_dir, os.path.basename(f))
                    shutil.copy2(f, dst)
                    written.append(dst)
        return written
    finally:
        shutil.rmtree(stage, ignore_errors=True)
