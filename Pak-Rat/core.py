"""
Pak Rat — core functionality (real pipeline).

Self-contained: original assets are live-extracted from the installed game's
base pak via the vendored **repak**; textures are read/encoded/injected via the
vendored **injector** (Matyalatte UE4-DDS-Tools + texconv.dll), called as a
subprocess through its own bundled python. No FModel / no shipped dump.

GUI seams (unchanged signatures): load_assets, load_meshes, validate_image_ext,
prepare_image, validate_mesh_ext, run_pipeline, deploy_to_rr, finish_to_documents.
New: prepare_target (the AssetPage "Next" seam) + TargetSpec.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

UE_VERSION = "5.4"           # RR is UE 5.4 (verified via injector 'check')
PAK_VERSION = "V11"
PAK_MOUNT = "../../../"
# Base RetroRewind-Windows.pak path-hash-seed (0xC04CF817). The game does not
# enforce a seed on loose ~mods paks, but we match the base for cleanliness.
PAK_SEED = str(0xC04CF817)


# ---------------------------------------------------------------------------
# Path resolution (frozen onedir vs loose source)
# ---------------------------------------------------------------------------
def _app_dir() -> Path:
    """Dir of the exe (frozen) or this source file."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    """Where bundled resources (vendor/, icon) live: _MEIPASS when frozen."""
    return Path(getattr(sys, "_MEIPASS", "") or _app_dir())


def _find(*relparts: str, roots=None) -> Path:
    """First existing path of relparts joined under each candidate root."""
    roots = roots or [_bundle_dir(), _app_dir(), Path(__file__).resolve().parent]
    rel = Path(*relparts)
    for r in roots:
        cand = r / rel
        if cand.exists():
            return cand
    return _bundle_dir() / rel


def _data_dir() -> Path:
    # data/ stays EXTERNAL (refreshable without a rebuild): prefer next to exe.
    # The clean lists are not shipped — they're generated on first use from the
    # installed game (see _ensure_clean_lists) and cached here.
    for r in (_app_dir(), _app_dir() / "Pak-Rat", Path(__file__).resolve().parent):
        if (r / "data").is_dir():
            return r / "data"
    return _app_dir() / "data"


VENDOR = lambda *p: _find("vendor", *p)            # noqa: E731
REPAK = lambda: VENDOR("repak.exe")                # noqa: E731
INJ_PY = lambda: VENDOR("injector", "python", "python.exe")     # noqa: E731
INJ_SRC = lambda: VENDOR("injector", "src")                     # noqa: E731
INJ_MAIN = lambda: VENDOR("injector", "src", "main.py")         # noqa: E731

TEXTURES_CLEAN = lambda: _data_dir() / "textures_clean.txt"     # noqa: E731

VALID_IMAGE_EXTS = {".png", ".dds"}
VALID_MESH_EXTS = {".uasset", ".fbx"}  # placeholder — confirm at step 4


# ---------------------------------------------------------------------------
# Game install discovery (base pak + ~mods)
# ---------------------------------------------------------------------------
def _steam_library_roots() -> list[Path]:
    """Every Steam library root on this PC, authoritatively.

    Reads Steam's own libraryfolders.vdf (so libraries with ANY folder name, on
    any drive, are found — not just ones literally called SteamLibrary), seeded
    from the registry's SteamPath and the standard install dirs.
    """
    steam_dirs: list[Path] = []
    try:
        import winreg
        for hk, key, val in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        ):
            try:
                with winreg.OpenKey(hk, key) as k:
                    steam_dirs.append(Path(winreg.QueryValueEx(k, val)[0]))
            except OSError:
                pass
    except Exception:
        pass
    steam_dirs += [Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")]

    roots: list[Path] = []
    seen = set()
    for sd in steam_dirs:
        if sd not in seen and sd.is_dir():
            seen.add(sd); roots.append(sd)            # the Steam dir is itself a library
        vdf = sd / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                txt = vdf.read_text(encoding="utf-8", errors="ignore")
                for m in re.findall(r'"path"\s*"([^"]+)"', txt):
                    p = Path(m.replace("\\\\", "\\"))
                    if p not in seen:
                        seen.add(p); roots.append(p)
            except Exception:
                pass
    return roots


def _rr_paks_dir() -> Path:
    """Locate the official Steam install's Paks folder.

    We only ever read a legitimately installed copy of the game: Steam libraries
    (via libraryfolders.vdf) first, then a few common drive-letter guesses as a
    fallback. No Documents/dev folders.
    """
    rel = r"steamapps\common\RetroRewind\RetroRewind\Content\Paks"
    candidates = [r / rel for r in _steam_library_roots()]
    for drive in "CDEFGH":                              # fallback guesses
        candidates.append(Path(rf"{drive}:\SteamLibrary") / rel)
        candidates.append(Path(rf"{drive}:\Steam") / rel)
    for p in candidates:
        if (p / "RetroRewind-Windows.pak").is_file():
            return p
    raise RuntimeError(
        "Retro Rewind (Steam) install not found. Looked for "
        "RetroRewind-Windows.pak across all Steam libraries.")


def base_pak() -> Path:
    return _rr_paks_dir() / "RetroRewind-Windows.pak"


def rr_mods_dir() -> Path:
    return _rr_paks_dir() / "~mods"


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------
def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command with no console window, capturing output."""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, text=True,
                          creationflags=flags, **kw)


def _repak(*args: str) -> subprocess.CompletedProcess:
    r = _run([str(REPAK()), *args])
    if r.returncode != 0:
        raise RuntimeError(f"repak failed: {r.stderr or r.stdout}")
    return r


def _injector(args: list[str]) -> subprocess.CompletedProcess:
    r = _run([str(INJ_PY()), str(INJ_MAIN()), *args])
    if r.returncode != 0:
        raise RuntimeError(f"injector failed: {r.stderr or r.stdout}")
    return r


# ---------------------------------------------------------------------------
# Oodle — fetched at first run, never shipped (proprietary, non-redistributable)
# ---------------------------------------------------------------------------
OODLE_DLL = "oo2core_9_win64.dll"


def _oodle_dst() -> Path:
    """Where repak looks for Oodle: next to repak.exe in the vendor dir."""
    return REPAK().parent / OODLE_DLL


def ensure_oodle() -> str:
    """Guarantee repak can find Oodle without us redistributing it.

    Oodle (RAD/Epic) is proprietary and is deliberately NOT bundled — keeping the
    download small and clean (no shipped binary to trip AV). On the first run we
    copy oo2core out of the user's own Steam install of the game (UE ships it in
    Binaries\\Win64) and cache it next to repak.exe. We only ever read the Steam
    install resolved by _rr_paks_dir(). Idempotent. Returns the path, or ''.
    """
    dst = _oodle_dst()
    if dst.is_file():
        return str(dst)
    try:
        paks = _rr_paks_dir()   # Steam install only
    except Exception:
        return ""
    # oo2core ships in the game's Binaries\Win64 — project or engine side.
    cands = []
    if len(paks.parents) > 1:
        cands.append(paks.parents[1] / "Binaries" / "Win64" / OODLE_DLL)
    if len(paks.parents) > 2:
        cands.append(paks.parents[2] / "Engine" / "Binaries" / "Win64" / OODLE_DLL)
    # bounded fallback: scan the game root once
    root = paks.parents[2] if len(paks.parents) > 2 else paks

    def _place(src: Path) -> str:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return str(dst)

    for c in cands:
        if c.is_file():
            return _place(c)
    try:
        for c in Path(root).rglob(OODLE_DLL):
            if c.is_file():
                return _place(c)
    except Exception:
        pass
    return ""  # not found in the Steam install — repak will report it can't read


# ---------------------------------------------------------------------------
# Asset inventory — the texture/mesh dropdown lists.
#
# These are NOT shipped. They're derived from the player's own installed game
# (one repak scan of the base pak, filtered + classified) on first use, then
# cached in the data dir. Keeps the lists correct per game version, and means
# there's nothing to bundle or let drift out of date.
# ---------------------------------------------------------------------------
MESHES_CLEAN = lambda: _data_dir() / "meshes_clean.txt"            # noqa: E731

_NO_GAME = ("(placeholder) couldn't read the game's assets — is Retro Rewind "
            "installed?")


_MESH_ROOT = "RetroRewind/Content/VideoStore/asset/meshes/"


def _generate_clean_lists() -> None:
    """Scan the installed base pak and (re)write textures_clean.txt + meshes_clean.txt.
    Texture rule (per Yodah): a swappable texture is T_<name>_bc — the base colour.
    Any other T_ asset is an NPC/other map we deliberately leave alone.
    Mesh rule (per Yodah): LA_/SM_ static meshes living under the game's mesh
    folder (RetroRewind/Content/VideoStore/asset/meshes) — this excludes L10N
    duplicates, NPC/character meshes, and effect-room props elsewhere."""
    tex, mesh = [], []
    for e in _pak_entries():
        if not e.endswith(".uasset"):
            continue
        m = e[:-7]
        leaf = m.rsplit("/", 1)[-1]
        if leaf.startswith("T_") and leaf.endswith("_bc"):
            tex.append(m)
        elif m.startswith(_MESH_ROOT) and leaf.startswith(("LA_", "SM_")):
            mesh.append(m)
    d = _data_dir()
    d.mkdir(parents=True, exist_ok=True)
    TEXTURES_CLEAN().write_text("\n".join(sorted(set(tex))) + "\n", encoding="utf-8")
    MESHES_CLEAN().write_text("\n".join(sorted(set(mesh))) + "\n", encoding="utf-8")


def _ensure_clean_lists() -> bool:
    """Make sure both dropdown lists exist, generating them from the installed
    game if not. False if the game can't be read (not installed / repak fail)."""
    if TEXTURES_CLEAN().exists() and MESHES_CLEAN().exists():
        return True
    try:
        _generate_clean_lists()
    except Exception:
        return False
    return TEXTURES_CLEAN().exists() and MESHES_CLEAN().exists()


def _read_list(p: Path) -> list[str]:
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def load_assets() -> list[str]:
    """Texture dropdown source: the _bc base-colour list (generated on demand)."""
    return _read_list(TEXTURES_CLEAN()) if _ensure_clean_lists() else [_NO_GAME]


def load_meshes() -> list[str]:
    """Mesh dropdown source: LA_/SM_ static meshes (generated on demand)."""
    return _read_list(MESHES_CLEAN()) if _ensure_clean_lists() else [_NO_GAME]


# ---------------------------------------------------------------------------
# Target spec — produced on the AssetPage "Next" press (validatePage seam)
# ---------------------------------------------------------------------------
@dataclass
class TargetSpec:
    asset: str          # mount path, no ext (…/T_Box_Moving_01_bc)
    uasset_path: str    # extracted .uasset on disk
    dxgi_format: str    # e.g. BC1_UNORM
    width: int
    height: int
    mips: int
    tex_type: str       # 2D / Cube / Array / 3D
    preview_png: str    # decoded original, for the "before" thumbnail
    work_dir: str       # per-session scratch (cleaned by cleanup_target)


_QUERY = (
    "import sys, json; sys.path.insert(0, sys.argv[2]);"
    "from unreal.uasset import Uasset;"
    "a = Uasset(sys.argv[1], version='%s');"
    "t = a.get_texture_list()[0]; w, h = t.get_max_size();"
    "print(json.dumps({'format': t.dxgi_format.name, 'width': w, 'height': h,"
    "'mips': len(t.mipmaps), 'type': t.get_texture_type()}))" % UE_VERSION
)


def prepare_target(asset: str) -> TargetSpec:
    """Live-extract + read spec + export preview for the selected texture.

    Called when the user clicks Next on the asset page, BEFORE the image page.
    Raises on any failure so the wizard can block the advance.
    """
    asset = asset.strip().rstrip("/")
    ensure_oodle()  # base pak is Oodle-compressed; make sure repak can read it
    work = Path(tempfile.mkdtemp(prefix="pakrat_"))
    unpacked = work / "unpacked"

    # 1. live-extract the three sidecars from the base pak
    _repak("unpack", "-o", str(unpacked),
           "-i", f"{asset}.uasset", "-i", f"{asset}.uexp", "-i", f"{asset}.ubulk",
           str(base_pak()))
    uasset = unpacked / (asset + ".uasset")
    if not uasset.is_file():
        raise RuntimeError(f"Extraction did not produce {uasset.name}")

    # 2. read format / WxH / mips via the injector (as a library, bundled py)
    q = _run([str(INJ_PY()), "-c", _QUERY, str(uasset), str(INJ_SRC())])
    if q.returncode != 0:
        raise RuntimeError(f"Could not read texture spec: {q.stderr or q.stdout}")
    spec = json.loads(q.stdout.strip().splitlines()[-1])

    # 3. export original -> PNG for the "before" preview thumbnail
    preview_dir = work / "preview"
    preview = ""
    try:
        _injector([str(uasset), "--mode", "export", "--version", UE_VERSION,
                   "--export_as", "png", "--save_folder", str(preview_dir)])
        hits = list(preview_dir.rglob("*.png"))
        preview = str(hits[0]) if hits else ""
    except Exception:
        preview = ""  # preview is non-essential; never block on it

    return TargetSpec(
        asset=asset, uasset_path=str(uasset),
        dxgi_format=spec["format"], width=int(spec["width"]),
        height=int(spec["height"]), mips=int(spec["mips"]),
        tex_type=spec["type"], preview_png=preview, work_dir=str(work))


def cleanup_target(spec: "TargetSpec | None") -> None:
    if spec and spec.work_dir and os.path.isdir(spec.work_dir):
        shutil.rmtree(spec.work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Extract mode — hand the user the original texture (PNG to edit / DDS exact)
# ---------------------------------------------------------------------------
EXPORT_FORMATS = ("png", "dds", "tga", "bmp", "jpg")


def export_texture(spec: "TargetSpec", dest_path: str, fmt: str = "png") -> str:
    """Decode the already-extracted original texture to `fmt`, write it to
    dest_path, and return the written path. Raises on failure.

    Reuses spec.uasset_path (live-extracted in prepare_target); the injector
    decodes the top mip to the requested image format. PNG is the easy-to-edit
    deliverable; DDS preserves the exact BC format + mips for a clean re-inject.
    """
    fmt = fmt.lower().lstrip(".")
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"Unsupported export format: {fmt}")
    out_dir = Path(spec.work_dir) / f"export_{fmt}"
    shutil.rmtree(out_dir, ignore_errors=True)
    _injector([spec.uasset_path, "--mode", "export", "--version", UE_VERSION,
               "--export_as", fmt, "--save_folder", str(out_dir)])
    hits = list(out_dir.rglob(f"*.{fmt}"))
    if not hits:
        raise RuntimeError(f"Export produced no .{fmt} file.")
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.copy2(hits[0], dest)
    return str(dest)


# Texture suffix family — picking T_X_bc should offer T_X_n, T_X_ram, …
_TEX_SUFFIX = re.compile(
    r'_(bc|n|ram|d|m|r|ao|e|orm|mask|h|s|emi|rough|metal|spec|opacity)$', re.I)


def related_textures(asset: str, limit: int = 16) -> list[str]:
    """Sibling textures of the same material set (same folder + shared stem,
    different suffix). e.g. T_Toy_RubixCube_A_01_bc -> …_n, …_ram.

    Scans the base pak's entry list. Returns mount paths (no ext), excluding the
    input. Empty on any failure.
    """
    asset = asset.strip().rstrip("/")
    out, seen = [], {asset}
    try:
        leaf = asset.rsplit("/", 1)[-1]
        folder = asset.rsplit("/", 1)[0]
        stem = _TEX_SUFFIX.sub("", leaf)
        if stem == leaf or not stem:
            return []  # no recognizable suffix to group on
        for e in _pak_entries():
            if not e.endswith(".uasset"):
                continue
            m = e[:-7]
            if m in seen:
                continue
            ml = m.rsplit("/", 1)[-1]
            if m.rsplit("/", 1)[0] == folder and ml.startswith(stem + "_") \
                    and _classify(m) == "texture":
                seen.add(m)
                out.append(m)
    except Exception:
        return out[:limit]
    return sorted(out)[:limit]


def export_many(assets: list[str], dest_dir: str, fmt: str = "png",
                progress=None) -> list[str]:
    """Extract + decode several textures to dest_dir/<leaf>.<fmt>.

    Returns the written paths. Skips (doesn't abort on) any single failure so one
    bad asset can't sink the batch; reports failures via progress.
    """
    written = []
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    for i, asset in enumerate(assets, 1):
        leaf = asset.rstrip("/").split("/")[-1]
        if progress:
            progress(f"Extracting {leaf}  ({i}/{len(assets)})…")
        spec = None
        try:
            spec = prepare_target(asset)
            out = export_texture(spec, str(Path(dest_dir) / f"{leaf}.{fmt}"), fmt)
            written.append(out)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"  ! skipped {leaf}: {e}")
        finally:
            cleanup_target(spec)
    return written


def mesh_overlay_assets(mesh: str) -> list[str]:
    """A mesh's editable dependencies for extraction: its MaterialInstance (MI_)
    assets AND the base-colour (_bc) textures those materials use. One pak walk;
    materials first (then textures), as sorted mount paths."""
    mesh = mesh.strip().rstrip("/")
    ensure_oodle()
    mats, texs = set(), set()
    with tempfile.TemporaryDirectory() as tmp:
        ua = _extract_asset(mesh, tmp)
        if not ua:
            return []
        for ref in _scan_refs(ua, ua[:-7] + ".uexp"):
            if _classify(ref) != "material":
                continue
            mats.add(ref)
            mua = _extract_asset(ref, tmp)
            if mua:
                for r2 in _scan_refs(mua, mua[:-7] + ".uexp"):
                    if _classify(r2) == "texture" and r2.endswith("_bc"):
                        texs.add(r2)
    return sorted(mats) + sorted(texs)


def related_assets(asset: str, limit: int = 16) -> list[str]:
    """Siblings to auto-include when extracting `asset`. Stays within the
    canonical set — base-colour textures (T_*_bc), the meshes folder, and (for a
    mesh) the materials it depends on. Never _n/_ram/_d suffix maps or shaders.

    Texture -> other T_*_bc in the same folder. Mesh -> its MI_ materials + the
    _bc textures they use."""
    kind = _classify(asset)
    if kind == "texture":
        folder = asset.rsplit("/", 1)[0]
        return sorted(m for m in load_assets()
                      if m != asset and m.rsplit("/", 1)[0] == folder)[:limit]
    if kind == "mesh":
        return mesh_overlay_assets(asset)[:limit]
    return []


def export_assets(assets: list[str], dest_dir: str, fmt: str = "png",
                  progress=None) -> list[str]:
    """Extract several assets to dest_dir. Textures are decoded to `fmt`
    (PNG/DDS); meshes (and anything else) are handed back as their raw cooked
    sidecars (.uasset/.uexp/.ubulk), flattened to dest_dir/<leaf>.<ext>. Skips
    failures so one bad asset can't sink the batch. Returns written paths."""
    written = []
    Path(dest_dir).mkdir(parents=True, exist_ok=True)
    ensure_oodle()
    for i, asset in enumerate(assets, 1):
        leaf = asset.rstrip("/").split("/")[-1]
        if progress:
            progress(f"Extracting {leaf}  ({i}/{len(assets)})…")
        try:
            if _classify(asset) == "texture":
                spec = None
                try:
                    spec = prepare_target(asset)
                    written.append(export_texture(
                        spec, str(Path(dest_dir) / f"{leaf}.{fmt}"), fmt))
                finally:
                    cleanup_target(spec)
            else:  # mesh / material / other -> raw cooked sidecars
                tmp = tempfile.mkdtemp(prefix="pakrat_extract_")
                try:
                    ua = _extract_asset(asset, tmp)
                    if ua:
                        base = ua[:-7]  # strip .uasset
                        for e in ("uasset", "uexp", "ubulk"):
                            src = Path(base + "." + e)
                            if src.is_file():
                                dst = Path(dest_dir) / f"{leaf}.{e}"
                                shutil.copy2(src, dst)
                                written.append(str(dst))
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"  ! skipped {leaf}: {e}")
    return written


# ---------------------------------------------------------------------------
# Replacement image validation + prep
# ---------------------------------------------------------------------------
@dataclass
class ImageInfo:
    path: str
    ext: str
    encoding: str          # the target BC it will be packed as
    prepared_png: str      # RGBA, resized to target WxH, ready for inject
    spec: TargetSpec       # carries uasset/work so run_pipeline is self-sufficient


def validate_image_ext(path: str) -> bool:
    return Path(path).suffix.lower() in VALID_IMAGE_EXTS


def prepare_image(path: str, spec: TargetSpec) -> ImageInfo:
    """Resize the chosen image to the target's exact WxH (LANCZOS, RGBA).

    Resolution is LOCKED to the original (decided 2026-06-25). The actual BC
    encode happens at inject time (injector matches spec.dxgi_format).
    """
    from PIL import Image  # lazy: keeps module import light for GUI selftest
    ext = Path(path).suffix.lower()
    out = Path(spec.work_dir) / "replacement.png"
    im = Image.open(path).convert("RGBA")
    if im.size != (spec.width, spec.height):
        im = im.resize((spec.width, spec.height), Image.LANCZOS)
    im.save(out)
    return ImageInfo(path=path, ext=ext, encoding=spec.dxgi_format,
                     prepared_png=str(out), spec=spec)


def decode_preview(mount: str) -> str | None:
    """Best-effort decoded PNG thumbnail of a texture asset.

    Returns a path to a standalone temp PNG, or None for non-textures / any
    failure. Self-contained: extracts, decodes, copies the preview out, and
    cleans its own scratch dir so nothing accumulates.
    """
    leaf = mount.rstrip("/").split("/")[-1]
    if not leaf.startswith("T_"):          # only 2D textures have a preview
        return None
    spec = None
    try:
        spec = prepare_target(mount)
        src = getattr(spec, "preview_png", None)
        if src and os.path.isfile(src):
            fd, dst = tempfile.mkstemp(suffix=".png", prefix="pakrat_prev_")
            os.close(fd)
            shutil.copy2(src, dst)
            return dst
    except Exception:
        return None
    finally:
        cleanup_target(spec)
    return None


def decode_pak_preview(pak_path: str, mount: str) -> str | None:
    """Decoded PNG preview of a texture asset AS IT EXISTS IN A GIVEN pak.

    Used for combine-mode hover previews (shows the modded texture, not the base
    game's). None for non-textures / any failure. Self-contained temp output.
    """
    leaf = mount.rstrip("/").split("/")[-1]
    if not leaf.startswith("T_"):
        return None
    work = None
    try:
        ensure_oodle()
        work = Path(tempfile.mkdtemp(prefix="pakrat_hov_"))
        unpacked = work / "u"
        _repak("unpack", "-o", str(unpacked),
               "-i", f"{mount}.uasset", "-i", f"{mount}.uexp",
               "-i", f"{mount}.ubulk", str(pak_path))
        uasset = unpacked / (mount + ".uasset")
        if not uasset.is_file():
            return None
        out = work / "p"
        _injector([str(uasset), "--mode", "export", "--version", UE_VERSION,
                   "--export_as", "png", "--save_folder", str(out)])
        hits = list(out.rglob("*.png"))
        if not hits:
            return None
        fd, dst = tempfile.mkstemp(suffix=".png", prefix="pakrat_hov_")
        os.close(fd)
        shutil.copy2(hits[0], dst)
        return dst
    except Exception:
        return None
    finally:
        if work and os.path.isdir(work):
            shutil.rmtree(work, ignore_errors=True)


def stage_texture(texture_mount: str, image_path: str, stage_dir, progress=None):
    """Extract a game texture, inject the user's image, and copy the resulting
    uasset/uexp/ubulk into stage_dir under the texture's mount tree.

    Shared by the texture pipeline and the cooker (so a cooked mesh can ship
    with its new textures in the same V11 pak). Cleans its own scratch.
    """
    tex = texture_mount.rstrip("/")
    leaf = tex.split("/")[-1]
    spec = None
    try:
        spec = prepare_target(tex)
        prepared = prepare_image(image_path, spec)
        injected = Path(spec.work_dir) / "injected"
        _injector([spec.uasset_path, prepared.prepared_png, "--mode", "inject",
                   "--version", UE_VERSION, "--save_folder", str(injected)])
        rel_parts = tex.split("/")[:-1]
        dst = Path(stage_dir).joinpath(*rel_parts)
        dst.mkdir(parents=True, exist_ok=True)
        for ext in ("uasset", "uexp", "ubulk"):
            src = injected / f"{leaf}.{ext}"
            if src.is_file():
                shutil.copy2(src, dst / src.name)
    finally:
        cleanup_target(spec)


def validate_mesh_ext(path: str) -> bool:
    return Path(path).suffix.lower() in VALID_MESH_EXTS  # TODO[step4]


# ---------------------------------------------------------------------------
# Injection + packaging  (runs inside the ProcessPage spinner)
# ---------------------------------------------------------------------------
def run_pipeline(asset: str, image: ImageInfo, mesh_path: str | None = None,
                 progress=None) -> str:
    """Inject the prepared image into the uasset and pack a .pak. Returns path."""
    def say(msg: str):
        if progress:
            progress(msg)

    spec = image.spec
    work = Path(spec.work_dir)
    leaf = asset.rstrip("/").split("/")[-1]

    # 1. inject the prepared PNG (injector encodes to spec.dxgi_format + mips)
    say("Injecting texture into .uasset…")
    injected = work / "injected"
    _injector([spec.uasset_path, image.prepared_png, "--mode", "inject",
               "--version", UE_VERSION, "--save_folder", str(injected)])

    if mesh_path:
        say("Injecting mesh…")
        # TODO[step4]: real mesh injection

    # 2. stage the modified sidecars under their full mount tree and pack
    say("Packaging .pak…")
    rel_dir = "/".join(asset.rstrip("/").split("/")[:-1])     # …/T_Box_Moving_A_01
    stage = work / "stage"
    stage_leaf = stage / rel_dir
    stage_leaf.mkdir(parents=True, exist_ok=True)
    for ext in ("uasset", "uexp", "ubulk"):
        src = injected / f"{leaf}.{ext}"
        if src.is_file():
            shutil.copy2(src, stage_leaf / src.name)

    out_pak = work / f"zzz_PakRat_{leaf}_P.pak"
    _repak("pack", "--version", PAK_VERSION, "--mount-point", PAK_MOUNT,
           "--path-hash-seed", PAK_SEED, str(stage), str(out_pak))
    say("Done.")
    return str(out_pak)


def run_pipeline_multi(items: list[dict], progress=None) -> str:
    """Inject several textures and pack them all into ONE pak.

    items = [{"texture": <mount, no ext>, "image": <png/dds path>}, …].
    Each texture is extracted for its exact format/size, the chosen image is
    resized + injected, and every result is staged under its real mount tree so
    a single repak run produces one drop-in pak. Returns the pak path.
    """
    def say(msg: str):
        if progress:
            progress(msg)
    if not items:
        raise RuntimeError("No textures to pack.")

    work = Path(tempfile.mkdtemp(prefix="pakrat_tex_multi_"))
    stage = work / "stage"
    specs = []
    try:
        n = len(items)
        for i, it in enumerate(items, 1):
            tex = it["texture"].rstrip("/")
            leaf = tex.split("/")[-1]
            say(f"Extracting {leaf}  ({i}/{n})…")
            spec = prepare_target(tex)
            specs.append(spec)
            say(f"Injecting {leaf}  ({i}/{n})…")
            prepared = prepare_image(it["image"], spec)
            injected = Path(spec.work_dir) / "injected"
            _injector([spec.uasset_path, prepared.prepared_png, "--mode", "inject",
                       "--version", UE_VERSION, "--save_folder", str(injected)])
            rel_dir = "/".join(tex.split("/")[:-1])
            stage_leaf = stage / rel_dir
            stage_leaf.mkdir(parents=True, exist_ok=True)
            for ext in ("uasset", "uexp", "ubulk"):
                src = injected / f"{leaf}.{ext}"
                if src.is_file():
                    shutil.copy2(src, stage_leaf / src.name)

        say("Packaging .pak…")
        first = items[0]["texture"].rstrip("/").split("/")[-1]
        name = first if n == 1 else f"{first}_plus{n - 1}"
        out_pak = work / f"zzz_PakRat_{name}_P.pak"
        _repak("pack", "--version", PAK_VERSION, "--mount-point", PAK_MOUNT,
               "--path-hash-seed", PAK_SEED, str(stage), str(out_pak))
        say("Done.")
        return str(out_pak)
    finally:
        for s in specs:
            cleanup_target(s)


# ---------------------------------------------------------------------------
# Combine mode — merge chosen assets from several existing paks into ONE pak.
#
# Pure repak (no injection / no UE): list each source pak, group its sidecars
# into logical assets, let the user cherry-pick (conflict-aware — one winner per
# content path), then unpack the picks and repack into a single drop-in pak.
# Lets a player take the soda from mod A and the candy from mod B without having
# to choose between whole paks, and outputs one clean shareable _P.pak.
# ---------------------------------------------------------------------------
_SIDECAR_EXTS = ("uasset", "uexp", "ubulk")


@dataclass
class PakAsset:
    mount: str          # grouping + conflict key: entry path minus sidecar ext
    kind: str           # mesh|texture|material|shader|other (best-effort)
    entries: list       # actual pak entry paths to unpack (the sidecars)
    pak: str            # source .pak this asset came from

    @property
    def leaf(self) -> str:
        return self.mount.rsplit("/", 1)[-1]


def list_mod_paks() -> list[str]:
    """Every .pak currently installed in the game's ~mods folder, sorted.

    Best-effort: returns [] if the game install can't be located — the combine
    page still works via its 'Add pak…' browse button."""
    try:
        d = rr_mods_dir()
    except Exception:
        return []
    try:
        return sorted(str(p) for p in d.glob("*.pak") if p.is_file())
    except Exception:
        return []


def pak_assets(pak_path: str) -> list["PakAsset"]:
    """List a pak's logical assets: sidecars (uasset/uexp/ubulk) sharing a stem
    are grouped into one asset; any other entry stands alone. Classified for
    display. Raises (via _repak) if the pak can't be read."""
    ensure_oodle()  # source paks may be Oodle-compressed
    r = _repak("list", str(pak_path))
    groups: dict[str, set] = {}
    for ln in r.stdout.splitlines():
        e = ln.strip().replace("\\", "/")
        if not e:
            continue
        leaf = e.rsplit("/", 1)[-1]
        ext = leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""
        stem = e[: -(len(ext) + 1)] if ext in _SIDECAR_EXTS else e
        groups.setdefault(stem, set()).add(e)
    out = [PakAsset(mount=stem, kind=_classify(stem),
                    entries=sorted(ents), pak=str(pak_path))
           for stem, ents in groups.items()]
    return sorted(out, key=lambda a: (a.kind, a.mount))


def find_conflicts(selected: list["PakAsset"]) -> dict[str, list["PakAsset"]]:
    """Group selected assets by content path; return only the paths chosen from
    more than one pak — the real collisions that need a winner picked."""
    by_mount: dict[str, list[PakAsset]] = {}
    for a in selected:
        by_mount.setdefault(a.mount, []).append(a)
    return {m: v for m, v in by_mount.items() if len(v) > 1}


def combine_paks(selected: list["PakAsset"], out_name: str | None = None,
                 progress=None) -> str:
    """Unpack each selected asset from its source pak and repack into ONE pak.

    `selected` must already be conflict-resolved: at most one PakAsset per
    content path (the chosen winner). Each asset's sidecars are unpacked under
    their real content tree so one repak run yields a drop-in pak. Returns the
    built pak path. Packs as the base format (V11 + base seed) — loose ~mods
    paks aren't seed-enforced, but matching the base keeps it clean."""
    def say(m):
        if progress:
            progress(m)
    if not selected:
        raise RuntimeError("Nothing selected to combine.")
    dupes = find_conflicts(selected)
    if dupes:
        raise RuntimeError("Unresolved asset conflict(s): " + ", ".join(sorted(dupes)))
    ensure_oodle()
    work = Path(tempfile.mkdtemp(prefix="pakrat_combine_"))
    stage = work / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    n = len(selected)
    for i, a in enumerate(selected, 1):
        say(f"Pulling {a.leaf}  ({i}/{n})…")
        args = ["unpack", "-o", str(stage), "-f"]
        for e in a.entries:
            args += ["-i", e]
        _repak(*args, str(a.pak))
    say("Packaging .pak…")
    out_pak = work / finalize_pak_name(out_name or "Combined")
    _repak("pack", "--version", PAK_VERSION, "--mount-point", PAK_MOUNT,
           "--path-hash-seed", PAK_SEED, str(stage), str(out_pak))
    say("Done.")
    return str(out_pak)


# ---------------------------------------------------------------------------
# Final actions
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Mesh + Texture mode — PACKAGER (user supplies cooked files; we stage + pack).
# We do not cook meshes; we walk the dependency tree, show vanilla for
# reference, and swap in the user's cooked files. Pack matches the proven
# statue mods: V8B, mount ../../../, no seed.
# ---------------------------------------------------------------------------
MESH_PAK_VERSION = "V8B"

_GAME_REF = re.compile(rb'/Game/[A-Za-z0-9_/]+')
_pak_entry_cache: set | None = None


def _pak_entries() -> set:
    """Set of every entry path in the base pak (cached). Used to know which
    sidecars (uasset/uexp/ubulk) actually exist for an asset."""
    global _pak_entry_cache
    if _pak_entry_cache is None:
        ensure_oodle()
        r = _repak("list", str(base_pak()))
        _pak_entry_cache = {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    return _pak_entry_cache


def _game_to_mount(ref: str) -> str:
    return ref.replace("/Game/", "RetroRewind/Content/", 1)


def _scan_refs(*files: str) -> list[str]:
    """All /Game/ asset refs (as mount paths) found in the given binaries."""
    out, seen = [], set()
    for f in files:
        if not f or not os.path.isfile(f):
            continue
        for m in _GAME_REF.findall(Path(f).read_bytes()):
            mp = _game_to_mount(m.decode("ascii", "ignore"))
            if mp not in seen:
                seen.add(mp)
                out.append(mp)
    return out


def _classify(asset_mount: str) -> str:
    leaf = asset_mount.rsplit("/", 1)[-1]
    low = asset_mount.lower()
    if "/core/shader/" in low or leaf.startswith("M_"):
        return "shader"
    if leaf.startswith("MI_"):
        return "material"
    if leaf.startswith("T_"):
        return "texture"
    if leaf.startswith(("LA_", "SM_", "SK_", "SKM_")):
        return "mesh"
    return "other"


def _is_shared(asset_mount: str) -> bool:
    """Heuristic: library assets (directly under textures/surfaces/Prop) are
    reused across props; dedicated assets sit in a named subfolder."""
    parent = asset_mount.rsplit("/", 2)[-2] if "/" in asset_mount else ""
    return parent in ("textures", "surfaces", "Prop")


def _sidecars_in_pak(asset_mount: str) -> list[str]:
    ents = _pak_entries()
    return [e for e in ("uasset", "uexp", "ubulk") if f"{asset_mount}.{e}" in ents]


def _extract_asset(asset_mount: str, out_dir: str) -> str:
    """Extract an asset's existing sidecars from the base pak. Returns the
    extracted .uasset path ('' if none)."""
    exts = _sidecars_in_pak(asset_mount)
    if not exts:
        return ""
    args = ["unpack", "-o", out_dir]
    for e in exts:
        args += ["-i", f"{asset_mount}.{e}"]
    _repak(*args, str(base_pak()))
    ua = os.path.join(out_dir, *f"{asset_mount}.uasset".split("/"))
    return ua if os.path.isfile(ua) else ""


def resolve_overlay_textures(mesh: str) -> list[str]:
    """Light walk for Dropdown 2: the base-colour (_bc) textures the mesh's
    materials use. Returns sorted mount paths."""
    mesh = mesh.strip().rstrip("/")
    ensure_oodle()
    with tempfile.TemporaryDirectory() as tmp:
        ua = _extract_asset(mesh, tmp)
        if not ua:
            return []
        uexp = ua[:-7] + ".uexp"
        texs = set()
        for ref in _scan_refs(ua, uexp):
            if _classify(ref) == "material":
                mua = _extract_asset(ref, tmp)
                if mua:
                    for r2 in _scan_refs(mua, mua[:-7] + ".uexp"):
                        if _classify(r2) == "texture" and r2.endswith("_bc"):
                            texs.add(r2)
    return sorted(texs)


def resolve_all_textures(mesh: str) -> list[str]:
    """Every texture the mesh uses — base colour AND the rest (normal `_n`,
    `_ram`, etc.). Unlike resolve_overlay_textures (which keeps only `_bc` for the
    light mesh-mode dropdown), this surfaces all swappable texture slots so the
    cook flow can offer them when a mesh has more than one texture. Textures
    referenced directly by the mesh are included too. Sorted; [] on failure."""
    mesh = mesh.strip().rstrip("/")
    ensure_oodle()
    with tempfile.TemporaryDirectory() as tmp:
        ua = _extract_asset(mesh, tmp)
        if not ua:
            return []
        texs = set()
        for ref in _scan_refs(ua, ua[:-7] + ".uexp"):
            k = _classify(ref)
            if k == "texture":
                texs.add(ref)
            elif k == "material":
                mua = _extract_asset(ref, tmp)
                if mua:
                    for r2 in _scan_refs(mua, mua[:-7] + ".uexp"):
                        if _classify(r2) == "texture":
                            texs.add(r2)
    return sorted(texs)


@dataclass
class RequiredFile:
    asset: str          # mount path, no ext
    kind: str           # mesh|material|texture|shader|other
    swappable: bool
    shared: bool
    sidecars: list      # exts present in pak
    vanilla_uasset: str
    preview_png: str    # textures only


@dataclass
class MeshPlan:
    mesh: str
    required: list      # list[RequiredFile]
    work_dir: str

    def swappable(self):
        return [r for r in self.required if r.swappable]


def prepare_mesh(mesh: str) -> MeshPlan:
    """Full dependency walk for the selected mesh: extract vanilla of every
    required asset, classify, decode texture previews. Raises on failure."""
    mesh = mesh.strip().rstrip("/")
    ensure_oodle()
    work = Path(tempfile.mkdtemp(prefix="pakrat_mesh_"))
    van = work / "vanilla"
    required: list[RequiredFile] = []
    seen: set[str] = set()

    def add(asset: str):
        if asset in seen:
            return None
        seen.add(asset)
        kind = _classify(asset)
        sidecars = _sidecars_in_pak(asset)
        if not sidecars and kind != "mesh":
            return None  # not in base pak (engine/transient) -> not a real slot
        ua = _extract_asset(asset, str(van))
        rf = RequiredFile(
            asset=asset, kind=kind,
            swappable=(kind != "shader"),
            shared=_is_shared(asset),
            sidecars=sidecars,
            vanilla_uasset=ua, preview_png="")
        required.append(rf)
        return rf

    mesh_rf = add(mesh)
    if not mesh_rf or not mesh_rf.vanilla_uasset:
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError(f"Mesh not found in base pak: {mesh}")

    # mesh -> materials/textures; recurse one level into materials
    for ref in _scan_refs(mesh_rf.vanilla_uasset, mesh_rf.vanilla_uasset[:-7] + ".uexp"):
        k = _classify(ref)
        if k in ("material", "texture", "shader", "mesh") and ref != mesh:
            rf = add(ref)
            if rf and k == "material" and rf.vanilla_uasset:
                for r2 in _scan_refs(rf.vanilla_uasset, rf.vanilla_uasset[:-7] + ".uexp"):
                    if _classify(r2) in ("texture", "shader"):
                        add(r2)

    # decode texture previews (best-effort)
    for rf in required:
        if rf.kind == "texture" and rf.vanilla_uasset:
            try:
                pdir = work / "preview" / rf.asset.rsplit("/", 1)[-1]
                _injector([rf.vanilla_uasset, "--mode", "export", "--version",
                           UE_VERSION, "--export_as", "png", "--save_folder", str(pdir)])
                hits = list(pdir.rglob("*.png"))
                rf.preview_png = str(hits[0]) if hits else ""
            except Exception:
                rf.preview_png = ""

    return MeshPlan(mesh=mesh, required=required, work_dir=str(work))


def cleanup_mesh(plan: "MeshPlan | None") -> None:
    if plan and plan.work_dir and os.path.isdir(plan.work_dir):
        shutil.rmtree(plan.work_dir, ignore_errors=True)


def validate_mesh_file(path: str, slot_asset: str) -> bool:
    """The user's cooked file must be a .uasset named for the target slot."""
    if Path(path).suffix.lower() != ".uasset":
        return False
    return Path(path).stem == slot_asset.rsplit("/", 1)[-1]


def run_mesh_pipeline(plan: MeshPlan, user_files: dict, progress=None) -> str:
    """Stage each user-supplied cooked file (+ its sidecars) at its content
    path and pack a V8B .pak. user_files maps slot asset -> user .uasset path."""
    def say(m):
        if progress:
            progress(m)

    work = Path(plan.work_dir)
    stage = work / "stage"
    say("Staging cooked files…")
    for rf in plan.swappable():
        src = user_files.get(rf.asset)
        if not src:
            raise RuntimeError(f"Missing file for slot: {rf.asset}")
        src = Path(src)
        dest_dir = stage / rf.asset.rsplit("/", 1)[0]
        dest_dir.mkdir(parents=True, exist_ok=True)
        leaf = rf.asset.rsplit("/", 1)[-1]
        # copy the .uasset and auto-grab matching sidecars beside it
        for e in ("uasset", "uexp", "ubulk"):
            cand = src.with_suffix("." + e)
            if cand.is_file():
                shutil.copy2(cand, dest_dir / f"{leaf}.{e}")

    say("Packaging .pak…")
    out_pak = work / f"zzz_PakRat_{plan.mesh.rsplit('/', 1)[-1]}_P.pak"
    _repak("pack", "--version", MESH_PAK_VERSION, "--mount-point", PAK_MOUNT,
           str(stage), str(out_pak))
    say("Done.")
    return str(out_pak)


def finalize_pak_name(raw: str) -> str:
    """User's chosen name -> a safe pak filename ending in _P.pak.

    Mods must end with _P.pak to load as patch paks, so we enforce the suffix.
    """
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (raw or "").strip()).strip()
    if not base:
        base = "PakRatMod"
    if base.lower().endswith(".pak"):
        base = base[:-4]
    if not base.endswith("_P"):
        base += "_P"
    return base + ".pak"


def rename_pak(pak_path: str, raw_name: str) -> str:
    """Rename the built .pak to the user's chosen name. Returns new path."""
    p = Path(pak_path)
    dest = p.with_name(finalize_pak_name(raw_name))
    if dest != p:
        if dest.exists():
            dest.unlink()
        shutil.move(str(p), str(dest))
    return str(dest)


def deploy_to_rr(pak_path: str) -> str:
    """Copy the .pak into the RR ~mods folder. Returns destination path."""
    dest_dir = rr_mods_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(pak_path).name
    shutil.copy2(pak_path, dest)
    return str(dest)


def finish_to_documents(pak_path: str) -> str:
    """Copy the .pak to Documents and reveal it in the file explorer."""
    docs = Path(os.path.expanduser("~")) / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    dest = docs / Path(pak_path).name
    shutil.copy2(pak_path, dest)
    reveal_in_explorer(dest)
    return str(dest)


def reveal_in_explorer(path: str | Path) -> None:
    """Open the OS file explorer with `path` selected (Windows-first)."""
    path = str(path)
    try:
        if os.name == "nt":
            os.system(f'explorer /select,"{path}"')
        else:
            os.system(f'explorer.exe /select,"$(wslpath -w \"{path}\")" 2>/dev/null')
    except Exception:
        pass
