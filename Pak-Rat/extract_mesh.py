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

# ui format -> output extension. Geometry is always exported from CUE4Parse as
# UEFormat (.uemodel) — proven robust, no glTF/SkiaSharp export path — then FBX/OBJ/
# glTF are produced by importing that .uemodel in the vendored Blender via the
# UEFormat addon (the SAME importer the Add-Asset uemodel path uses, confirmed to
# work in-game) and re-exporting. `blender_ext` None => hand the .uemodel back as-is.
FORMATS = {
    "uemodel": None,      # direct, no Blender
    "fbx":     ".fbx",
    "obj":     ".obj",
    "gltf":    ".glb",
    "glb":     ".glb",
    "stl":     ".stl",
    "ply":     ".ply",
    "dae":     ".dae",
    "blend":   ".blend",
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


def _blender_uemodel_convert(uemodel: str, out: str, progress=None) -> None:
    """Import a .uemodel in the vendored Blender (UEFormat addon — the same importer
    the Add-Asset uemodel path uses) and re-export it as FBX / OBJ / glTF."""
    import cook
    # cook helpers call progress(msg, pct); the extract worker's callback takes one
    # arg. Adapt so ensure_blender's 2-arg calls don't blow up before Blender runs.
    p2 = (lambda m, pct=None: progress(m)) if progress else None
    bl = cook.ensure_blender(p2)
    script = cook.home() / "_meshx_convert.py"
    script.write_text(_UEMODEL_CONVERT_SCRIPT, encoding="utf-8")
    addons = str(core.VENDOR("blender_addons"))
    r = core._run([bl, "--background", "--python", str(script),
                   "--", uemodel, out, addons])
    if not os.path.isfile(out):
        raise RuntimeError("Blender uemodel->mesh conversion failed:\n%s"
                           % ((r.stderr or r.stdout or "")[-800:]))


_UEMODEL_CONVERT_SCRIPT = r'''
import bpy, sys
uemodel, out, addons = sys.argv[-3], sys.argv[-2], sys.argv[-1]
bpy.ops.wm.read_factory_settings(use_empty=True)
sys.path.insert(0, addons)
from io_scene_ueformat.importer.logic import UEFormatImport
from io_scene_ueformat.options import UEModelOptions
UEFormatImport(UEModelOptions()).import_file(uemodel)
e = out.lower().rsplit(".", 1)[-1]
if e == "obj":
    bpy.ops.wm.obj_export(filepath=out)
elif e in ("gltf", "glb"):
    bpy.ops.export_scene.gltf(filepath=out, export_format="GLB")
elif e == "stl":
    bpy.ops.wm.stl_export(filepath=out)
elif e == "ply":
    bpy.ops.wm.ply_export(filepath=out)
elif e == "dae":
    bpy.ops.wm.collada_export(filepath=out)
elif e == "blend":
    bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
else:  # fbx
    bpy.ops.export_scene.fbx(filepath=out, use_selection=False,
        object_types={"MESH"}, add_leaf_bones=False, path_mode="COPY")
print("PAKRAT_MESHX", out)
'''


def export_mesh(mount: str, dest_dir: str, fmt: str, progress=None) -> list[str]:
    """Extract the cooked StaticMesh at `mount` into `dest_dir/<leaf>/` as `fmt`.

    fmt in FORMATS ('uemodel'|'fbx'|'obj'|'gltf'). Geometry is exported from
    CUE4Parse as .uemodel (robust); for other formats that .uemodel is re-exported
    through the vendored Blender/UEFormat addon. The mesh's textures are carried
    alongside. Returns written paths; raises on failure (caller skips + reports).
    """
    fmt = fmt.lower()
    ext = FORMATS[fmt]
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
        opts.MeshFormat = EMeshFormat.UEFormat        # always uemodel from CUE4Parse
        MeshExporter(sm, opts).TryWriteToDir(DirectoryInfo(stage))

        uemodel = None
        for f in glob.glob(os.path.join(stage, "**", "*"), recursive=True):
            if os.path.isfile(f) and f.lower().endswith(".uemodel"):
                uemodel = f
        if not uemodel:
            raise RuntimeError(f"CUE4Parse produced no .uemodel for '{leaf}'")

        out_dir = os.path.join(dest_dir, leaf)
        os.makedirs(out_dir, exist_ok=True)
        written = []
        if ext is None:            # uemodel — hand back the mesh + its textures as-is
            for f in glob.glob(os.path.join(stage, "**", "*"), recursive=True):
                if os.path.isfile(f):
                    dst = os.path.join(out_dir, os.path.basename(f))
                    shutil.copy2(f, dst)
                    written.append(dst)
        else:                      # fbx/obj/gltf — convert the uemodel via Blender
            out = os.path.join(out_dir, leaf + ext)
            if progress:
                progress(f"Converting {leaf} → {fmt.upper()}…")
            _blender_uemodel_convert(uemodel, out, progress=progress)
            written.append(out)
            # carry EVERY texture/material sidecar along (recursively) — same as
            # uemodel — so the model's maps aren't lost even if the FBX/OBJ doesn't
            # embed them. (.uemodel itself is skipped; it was just the conversion src.)
            for f in glob.glob(os.path.join(stage, "**", "*"), recursive=True):
                if os.path.isfile(f) and not f.lower().endswith(".uemodel"):
                    dst = os.path.join(out_dir, os.path.basename(f))
                    if not os.path.exists(dst):
                        shutil.copy2(f, dst)
                        written.append(dst)
        return written
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _load_texture(mount: str):
    """Load the UTexture2D export from the package at `mount` (no extension)."""
    pkg = _provider().LoadPackage(mount)
    from CUE4Parse.UE4.Assets.Exports.Texture import UTexture2D
    for i in range(400):
        try:
            e = pkg.GetExport(i)
        except Exception:  # noqa: BLE001  (index past the end throws)
            break
        if isinstance(e, UTexture2D):
            return e
    raise RuntimeError(f"no Texture2D export found in '{mount}'")


# Raster image formats the extractor can write (Pillow-backed) — parity with the
# importer's accepted images. DDS is handled specially (exact cooked BC via raw).
IMAGE_FORMATS = ("png", "jpg", "jpeg", "bmp", "tga", "tif", "tiff", "webp", "gif")


def export_texture(mount: str, dest_dir: str, fmt: str = "png",
                   progress=None) -> list[str]:
    """Extract the cooked UTexture2D at `mount` into `dest_dir/<leaf>/` as an image.

    Decodes via CUE4Parse-Conversion's TextureDecoder (the reader FModel uses) to a
    SkiaSharp bitmap, writes a PNG, then (for any non-PNG raster `fmt`) converts that
    PNG to the requested format with Pillow — png/jpg/bmp/tga/tiff/webp/gif parity with
    the importer's accepted images. Returns written paths; raises on failure.
    """
    leaf = mount.rstrip("/").split("/")[-1]
    fmt = (fmt or "png").lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if progress:
        progress(f"Reading {leaf} (texture)…")
    tex = _load_texture(mount)

    from CUE4Parse.UE4.Assets.Exports.Texture import ETexturePlatform
    from CUE4Parse_Conversion.Textures import TextureDecoder
    from SkiaSharp import SKEncodedImageFormat, SKImage
    from System.IO import File

    bmp = TextureDecoder.Decode(tex, ETexturePlatform.DesktopMobile)
    if bmp is None:
        raise RuntimeError(f"CUE4Parse could not decode texture '{leaf}'")

    out_dir = os.path.join(dest_dir, leaf)
    os.makedirs(out_dir, exist_ok=True)
    png_path = os.path.join(out_dir, leaf + ".png")
    File.WriteAllBytes(
        png_path,
        SKImage.FromBitmap(bmp).Encode(SKEncodedImageFormat.Png, 100).ToArray())
    if fmt in ("png", ""):
        return [png_path]

    # convert the decoded PNG to the requested raster format via Pillow; drop the PNG
    out = os.path.join(out_dir, f"{leaf}.{fmt}")
    from PIL import Image
    try:
        im = Image.open(png_path)
        if fmt in ("jpg", "jfif", "jpe"):
            im = im.convert("RGB")          # JPEG has no alpha channel
        im.save(out)
        im.close()
    finally:
        try:
            os.remove(png_path)
        except OSError:
            pass
    return [out]


# ---------------------------------------------------------------------------
# Source enumeration — group the mounted VFS by owning pak so the extractor can
# offer "base game" vs a chosen downloaded mod (the ~mods paks). Every file's
# source pak is its FPakEntry.Vfs.Name (verified: base = RetroRewind-Windows.pak,
# each ~mods/*.pak = one downloaded mod).
# ---------------------------------------------------------------------------
BASE_PAK_PREFIX = "RetroRewind-Windows"


def list_mod_paks() -> list[str]:
    """Source-pak names of the user's downloaded mods — every mounted pak except the
    base RetroRewind pak (i.e. the ~mods/*.pak files). Sorted."""
    prov = _provider()
    names = set()
    for kv in prov.Files:
        try:
            v = kv.Value.Vfs.Name
        except Exception:  # noqa: BLE001
            continue
        if v and not v.startswith(BASE_PAK_PREFIX):
            names.add(v)
    return sorted(names)


def list_assets(source_pak: str) -> dict:
    """Extractable meshes + textures in ONE source pak, as mount paths (no extension).

    Classifies each .uasset by its export type (UStaticMesh vs UTexture2D) — accurate,
    and cheap for mod-sized paks (dozens of files). Intended for a chosen downloaded
    mod (`source_pak` from list_mod_paks); base-game listing stays on the prebuilt
    core.load_meshes()/load_assets() manifests (21k+ files — too many to load here).
    Returns {'meshes': [...], 'textures': [...]} sorted.
    """
    prov = _provider()
    from CUE4Parse.UE4.Assets.Exports.StaticMesh import UStaticMesh
    from CUE4Parse.UE4.Assets.Exports.Texture import UTexture2D
    snd = _sound_cls()
    meshes, textures, sounds, other = [], [], [], []
    for kv in prov.Files:
        gf, k = kv.Value, kv.Key
        if not k.lower().endswith(".uasset"):
            continue
        try:
            if gf.Vfs.Name != source_pak:
                continue
        except Exception:  # noqa: BLE001
            continue
        mount = k[:-len(".uasset")]
        try:
            pkg = prov.LoadPackage(mount)
        except Exception:  # noqa: BLE001  (broken/partial asset — skip)
            continue
        kind = None
        for i in range(400):
            try:
                e = pkg.GetExport(i)
            except Exception:  # noqa: BLE001  (index past the end throws)
                break
            if isinstance(e, UStaticMesh):
                kind = "mesh"; break
            if isinstance(e, UTexture2D):
                kind = "texture"; break
            if snd is not None and isinstance(e, snd):
                kind = "sound"; break
        if kind == "mesh":
            meshes.append(mount)
        elif kind == "texture":
            textures.append(mount)
        elif kind == "sound":
            sounds.append(mount)
        else:
            other.append(mount)          # any other cooked asset — extract raw sidecars
    return {"meshes": sorted(meshes), "textures": sorted(textures),
            "sounds": sorted(sounds), "other": sorted(other)}


def _sound_cls():
    """USoundWave type, or None if this CUE4Parse build lacks the Sound export."""
    try:
        from CUE4Parse.UE4.Assets.Exports.Sound import USoundWave
        return USoundWave
    except Exception:  # noqa: BLE001
        return None


_SOUND_HINTS = ("sound", "audio", "sfx", "voice", "music", "foley", "ambient",
                "/vo/", "_sw", "_cue", "dialog")


def list_sounds(source_pak: "str | None" = None) -> list[str]:
    """USoundWave mounts. If `source_pak` is given → just that mod pak (full scan, small).
    Else the whole mounted set (base + mods), PATH-PREFILTERED to sound-ish folders so we
    don't LoadPackage all 21k base files. Returns sorted mounts (no extension)."""
    prov = _provider()
    snd = _sound_cls()
    if snd is None:
        return []
    out = []
    for kv in prov.Files:
        k = kv.Key
        if not k.lower().endswith(".uasset"):
            continue
        if source_pak is not None:
            try:
                if kv.Value.Vfs.Name != source_pak:
                    continue
            except Exception:  # noqa: BLE001
                continue
        elif not any(h in k.lower() for h in _SOUND_HINTS):
            continue          # base+mods: cheap path prefilter before the load
        mount = k[:-len(".uasset")]
        try:
            pkg = prov.LoadPackage(mount)
        except Exception:  # noqa: BLE001
            continue
        for i in range(60):
            try:
                e = pkg.GetExport(i)
            except Exception:  # noqa: BLE001
                break
            if isinstance(e, snd):
                out.append(mount)
                break
    return sorted(set(out))


def _classify_export(mount: str) -> "str | None":
    """'mesh' | 'texture' | 'sound' | None for the package at `mount`, by export type."""
    from CUE4Parse.UE4.Assets.Exports.StaticMesh import UStaticMesh
    from CUE4Parse.UE4.Assets.Exports.Texture import UTexture2D
    snd = _sound_cls()
    try:
        pkg = _provider().LoadPackage(mount)
    except Exception:  # noqa: BLE001
        return None
    for i in range(400):
        try:
            e = pkg.GetExport(i)
        except Exception:  # noqa: BLE001  (index past the end throws)
            break
        if isinstance(e, UStaticMesh):
            return "mesh"
        if isinstance(e, UTexture2D):
            return "texture"
        if snd is not None and isinstance(e, snd):
            return "sound"
    return None


def _save_raw(mount: str, dest_dir: str, progress=None) -> list[str]:
    """Save the raw cooked sidecars (.uasset/.uexp/.ubulk) for `mount` straight out of
    the mounted provider — works for BASE and ~mods paks (unlike the base-pak repak
    route). Returns written paths."""
    prov = _provider()
    leaf = mount.rstrip("/").split("/")[-1]
    out_dir = os.path.join(dest_dir, leaf)
    os.makedirs(out_dir, exist_ok=True)
    from System.IO import File
    written = []
    data = prov.SavePackage(mount)          # {vfs-path: byte[]} for every sidecar
    for kv in data:
        ext = kv.Key.rsplit(".", 1)[-1].lower()
        if ext not in ("uasset", "uexp", "ubulk"):
            continue
        out = os.path.join(out_dir, leaf + "." + ext)
        File.WriteAllBytes(out, kv.Value)
        written.append(out)
    return written


def export_any(mount: str, dest_dir: str, tex_fmt: str = "png",
               mesh_fmt: str = "uemodel", progress=None) -> list[str]:
    """Provider-based extract of ANY asset — works for BASE *and* ~mods paks (the
    provider mounts them all, so this reaches assets the base-pak repak route can't).

    Textures -> PNG; static meshes -> geometry (`mesh_fmt`) unless mesh_fmt=='uasset';
    everything else / 'uasset' -> raw cooked sidecars. This is the ~mods extraction
    path (letting users pull assets out of downloaded mods to combine them).
    """
    kind = _classify_export(mount)
    if kind == "texture":
        if (tex_fmt or "").lower().lstrip(".") == "dds":
            return _save_raw(mount, dest_dir, progress=progress)  # exact cooked BC + mips
        return export_texture(mount, dest_dir, tex_fmt, progress=progress)
    if kind == "mesh" and mesh_fmt and mesh_fmt != "uasset":
        return export_mesh(mount, dest_dir, mesh_fmt, progress=progress)
    return _save_raw(mount, dest_dir, progress=progress)
