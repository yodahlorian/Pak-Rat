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
def _rr_paks_dir() -> Path:
    """Locate the official Steam install's Paks folder.

    Steam can live on any drive (default under Program Files, or a SteamLibrary
    on another drive), so we probe the common roots. We deliberately do NOT look
    anywhere else — no Documents/dev folders — so the app only ever reads a
    legitimately installed copy of the game.
    """
    rel = r"steamapps\common\RetroRewind\RetroRewind\Content\Paks"
    roots = [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    for drive in "CDEFGH":
        roots.append(rf"{drive}:\SteamLibrary")
        roots.append(rf"{drive}:\Steam")
    for r in roots:
        p = Path(r) / rel
        if (p / "RetroRewind-Windows.pak").is_file():
            return p
    raise RuntimeError(
        "Retro Rewind (Steam) install not found. Looked for "
        "RetroRewind-Windows.pak under the standard Steam library locations.")


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
# Asset inventory (consumes Tron's data/textures_clean.txt)
# ---------------------------------------------------------------------------
def load_assets() -> list[str]:
    """Texture dropdown source: the pre-filtered, parse-verified _bc list."""
    tc = TEXTURES_CLEAN()
    if tc.exists():
        return [ln.strip() for ln in tc.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    return ["(placeholder) textures_clean.txt not found — run the asset scan first"]


MESHES_CLEAN = lambda: _data_dir() / "meshes_clean.txt"            # noqa: E731


def load_meshes() -> list[str]:
    """Mesh dropdown source: pre-filtered LA_/SM_ static meshes."""
    mc = MESHES_CLEAN()
    if mc.exists():
        return [ln.strip() for ln in mc.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
    return load_assets()


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
