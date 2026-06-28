# Pak Rat

**Version 2.0.7**

**Automatic asset packager + mesh cooker for Retro Rewind (UE 5.4).**

Pak Rat builds a ready-to-use `.pak` mod from your own art through a simple
step-by-step wizard — no command line, no manual repacking. Swap textures, swap
pre-cooked meshes, **cook your own 3D models** with Unreal, pull originals out to
edit, or **combine mods** you already have into one pak.

---

## Requirements

- **Windows**
- **Retro Rewind installed via Steam.** Pak Rat reads the game's base pak to pull
  the original assets it needs (and builds its texture/mesh lists from your copy
  on first run).
- **Unreal Engine 5.4 (optional)** — only for the **Cook Mesh** mode. Install it
  free from the Epic Games Launcher. Everything else works without it.

Nothing else to install — the app is otherwise self-contained.

---

## Running it

Double-click **`Pak-Rat.exe`**.

> First launch may show a Windows SmartScreen prompt ("Windows protected your
> PC") because the app isn't code-signed. Click **More info → Run anyway**.

> ⚠️ Keep `Pak-Rat.exe` and the `_internal` folder **together** — move the whole
> Pak-Rat folder as a unit. Deleting `_internal` breaks the app. (A small `data`
> folder is created next to the exe on first run to cache the asset lists; it's
> rebuilt automatically if removed.)

---

## What it does

On Page 1 you pick a mode:

### Regular Texture
Swap one or many textures — all packed into a single `.pak`.

1. Pick a texture from the dropdown (type to filter).
2. Choose a replacement **PNG or DDS** for it. Add as many more textures as you
   like, each with its own image.
3. Each image is automatically resized to its target's exact dimensions and
   re-encoded to the matching BC format.
4. Pak Rat injects them all and packs one `.pak`.

### Mesh + Texture
Swap a pre-cooked mesh and the assets it depends on.

1. Pick the mesh, then its overlay texture.
2. Pak Rat walks the mesh's dependencies (materials → textures) and lists every
   slot on the **Required Files** page.
3. Provide your **cooked `.uasset`** replacement for each swappable slot
   (the `.uexp`/`.ubulk` sidecars are picked up automatically). Parent shaders
   are shown as "vanilla, not swappable"; shared assets are flagged.
4. Pak Rat packs everything into the `.pak`.

### Cook Mesh from a 3D file  *(needs Unreal Engine 5.4)*
Bring your own model in almost any format — Pak Rat cooks it for you.

1. On first use, Pak Rat downloads a portable Blender and builds a small cook
   project (one-time, cached in your user folder).
2. Pick the game mesh your model stands in for.
3. Choose your model file (**FBX, OBJ, glTF/GLB, STL, PLY, DAE or .blend**); add
   more meshes to pack together if you want.
4. Optionally swap the textures the mesh uses — a fresh mesh usually wants fresh
   textures. This step is offered next and is skippable.
5. Pak Rat converts it with Blender, cooks it with your installed Unreal Engine
   5.4, retargets it onto the game's real materials, and packs the `.pak` (your
   textures, if any, go in the same pak).

> This mode appears only when an Unreal Engine install is detected.

### Extract Asset
Pull an original asset out of the game to use as a starting point — textures
(decoded to PNG/DDS) or meshes (handed back as their cooked `.uasset`).

1. Pick a texture or mesh from the dropdown — grouped by type (Meshes/Textures)
   and family, or just type to filter. Use **Add another…** for a searchable,
   grouped picker. Related sibling assets are auto-added — untick any you don't
   want.
2. Pak Rat shows a preview thumbnail of each texture (hover to enlarge).
3. Choose the texture export format:
   - **PNG** — decoded, easy to edit in any image editor (recommended).
   - **DDS** — the exact BC format + full mip chain, for a clean re-inject.
4. Pick a folder; Pak Rat extracts everything (with a progress screen) and opens it.

Edit a PNG, then come back through **Regular Texture** to swap your version
back in.

### Combine Mods
Merge mods you already have into a single `.pak`.

1. Pak Rat lists the paks in your `~mods` folder; add any others with **Add pak**.
2. It shows every asset across them and flags conflicts (the same asset in more
   than one pak). Tick the assets you want; for conflicts, pick the winner.
3. Pak Rat merges your selection into one conflict-free `.pak`.

### Finishing
When the `.pak` is built, name it, then choose:

- **Deploy** — installs it straight into Retro Rewind's `~mods` folder.
- **Finish** — saves it to your Documents and opens the folder.

---

## Notes & limitations

- The texture/mesh lists are built from your own installed game on first run and
  cached. Texture list = the game's swappable `T_…_bc` base-colour textures; mesh
  list = the static meshes (`LA_`/`SM_`) under the game's mesh folder. Skeletal
  meshes (`SK_`) and virtual textures aren't supported.
- **Cook Mesh** needs a local Unreal Engine 5.4 install (Epic Games Launcher) and
  downloads a portable Blender on first use.
- **Combine Mods** packs only the assets you tick — if a swapped mesh needs a
  custom material that you leave unticked, it can render wrong. Pack the assets
  that belong together.

---

## Building from source

This repo is the complete, single source — sources, PyInstaller `.spec`, data,
and all redistributable tooling under `Pak-Rat/vendor/` (repak, the *UE4-DDS-
Tools* injector + its embedded Python 3.10, and `texconv.dll`, all MIT/Apache).

```
pip install pyinstaller PySide6 pillow
pyinstaller Pak-Rat.spec --noconfirm
```

**Oodle is the one thing not in the box.** `oo2core_*.dll` is proprietary
(RAD/Epic) and is never committed or shipped. Pak Rat obtains it on the user's
own machine the first time it runs — copied from the installed Retro Rewind / UE
game (which ships it), or downloaded by repak from Epic's official OodleUE
distribution. See `core.ensure_oodle()`.

---

## Changelog

### 2.0.7
- **Cook now forces loose cooked files** (`-nozenstore`). On some setups the cooker
  wrote its output into the Zen/IoStore cache instead of loose `.uasset` files, so
  a perfectly good mesh looked like it "cooked to nothing." Pak Rat now also
  searches the whole cooked tree for the asset, writes the full cook log to
  `last_cook.log`, and — if it still can't find the file — lists exactly what the
  cooker produced so the cause is obvious.

### 2.0.6
- **Clearer error when a source mesh has no geometry.** A model that exports with
  a material but no faces (un-joined parts, stray empties, or unapplied modifiers)
  used to import "successfully" and then fail at the cook stage with a cryptic
  *"Cook produced no .uasset"*. Pak Rat now detects the empty mesh at **import**
  and tells you exactly what's wrong, and the cook-stage error now includes the
  relevant Unreal log lines when it does fire.

### 2.0.5
- **Fixed the Blender download getting a 403 Forbidden** on first-run Cook setup —
  the download CDN rejects the default request agent; Pak Rat now sends a proper
  User-Agent.

### 2.0.4
- **Fixed first-run Cook setup failing** with `urlopen error unknown url type:
  https` — the build was missing its SSL libraries, so it couldn't download the
  portable Blender. They're now included.

### 2.0.3
- Asset pickers are now **grouped by category** — Meshes / Textures, then by
  family (BackAlley, Candy, …) — with type-to-filter still spanning everything.
- The **Add another…** picker is now searchable and grouped (a tree), instead of
  a flat list — much easier in the mixed mesh+texture extractor.
- Fixed: duplicate leaf names (localised copies) could resolve to the wrong
  folder's asset in a dropdown; picks now map to the exact asset.

### 2.0.2
- Bigger **hover previews** (512px) across the texture swapper, cooker, extractor
  and combiner, with a "hover to enlarge" caption under each.
- A neon **loading spinner** shows in each thumbnail while its preview decodes.

### 2.0.1
- **New synthwave look** themed from the app icon (neon cyan/magenta on dark).
- **Texture previews are back** — Regular Texture and Extract show a thumbnail of
  each original (and your chosen replacement), in a scrollable list.
- **Cook Mesh now offers textures** — after picking your model, optionally swap
  the textures it uses; they pack into the same `.pak`.
- **Combine** shows a texture preview on hover.
- **Extract** saves through a progress page, then a done page.
- Dropdowns show just the asset name; remove (✕) buttons are visible again;
  Back on a "done" page restarts at the beginning.

### 2.0.0
- **Cook Mesh from a 3D file** — bring an FBX/OBJ/glTF/GLB/STL/PLY/DAE/.blend and
  Pak Rat converts it with a bundled portable Blender and cooks it with your
  installed Unreal Engine 5.4, retargeting it onto the game's real materials.
- **Combine Mods** — cherry-pick assets from paks you already have and merge them
  into one conflict-aware `.pak`.
- **Regular Texture** now packs one *or many* textures into a single pak.
- Texture/mesh lists are generated from your own installed game on first run
  (nothing baked in) and Steam-library discovery is more robust.

### 1.0.2
- **Oodle is no longer bundled.** `oo2core` is proprietary (RAD/Epic) and can't
  be redistributed, so Pak Rat now copies it from your own Steam install of the
  game on first run. The download is smaller and clean — no shipped DLL to trip
  antivirus.
- More robust Steam-install discovery (finds `SteamLibrary` on any drive).

### 1.0.1
- Added **Extract Texture** mode — pull an original texture out of the game as
  PNG (easy to edit) or DDS (exact format + mips), then swap your edit back in
  via Regular Texture.

### 1.0.0
- Initial release: **Regular Texture** and **Mesh + Texture** packaging.

---
