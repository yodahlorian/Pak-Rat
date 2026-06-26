# Pak Rat

**Version 1.0.1**

**Automatic asset packager for Retro Rewind (UE 5.4).**

Pak Rat builds a ready-to-use `.pak` mod from your own art — swap a texture, or
swap a mesh together with its textures — through a simple step-by-step wizard.
No command line, no manual repacking.

---

## Requirements

- **Windows**
- **Retro Rewind installed** (Steam library or `Documents\RR`). Pak Rat reads
  the game's base pak to pull the original assets it needs.

Nothing else to install — the app is self-contained.

---

## Running it

Double-click **`Pak-Rat.exe`**.

> First launch may show a Windows SmartScreen prompt ("Windows protected your
> PC") because the app isn't code-signed. Click **More info → Run anyway**.

> ⚠️ Keep `Pak-Rat.exe`, the `_internal` folder, and `data` folder **together**.
> Move the whole Pak-Rat folder as a unit. Deleting `_internal` breaks the app.

---

## What it does

On Page 1 you pick a mode:

### Regular Texture
Swap a single texture on an existing asset.

1. Pick the texture from the dropdown (type to filter).
2. Pak Rat live-extracts the original and reads its format/size — you'll see a
   preview and the spec (e.g. `BC1_UNORM · 2048×2048 · 12 mips`).
3. Choose your replacement **PNG or DDS**. It's resized to the original's exact
   dimensions and re-encoded to the matching format automatically.
4. Pak Rat injects and packs the `.pak`.

### Mesh + Texture
Swap a mesh and the assets it depends on.

1. Pick the mesh, then its overlay texture.
2. Pak Rat walks the mesh's dependencies (materials → textures) and lists every
   slot on the **Required Files** page.
3. Provide your **cooked `.uasset`** replacement for each swappable slot
   (the `.uexp`/`.ubulk` sidecars are picked up automatically). Parent shaders
   are shown as "vanilla, not swappable"; shared assets are flagged.
4. Pak Rat packs everything into the `.pak`.

> **Note:** Pak Rat *packages* meshes — it does not cook them. You must supply
> already-cooked mesh `.uasset` files (exported/cooked in the Unreal Editor).
> Texture cooking is fully automatic.

### Extract Texture
Pull an original texture out of the game to use as a starting point.

1. Pick the texture from the dropdown (type to filter).
2. Pak Rat live-extracts the original and shows its spec + preview.
3. Choose a format:
   - **PNG** — decoded, easy to edit in any image editor (recommended).
   - **DDS** — the exact BC format + full mip chain, for a clean re-inject.
4. Pick where to save it. Pak Rat writes the file and opens the folder.

Edit the PNG, then come back through **Regular Texture** to swap your version
back in.

### Finishing
When the `.pak` is built, name it, then choose:

- **Deploy** — installs it straight into Retro Rewind's `~mods` folder.
- **Finish** — saves it to your Documents and opens the folder.

---

## Notes & limitations

- Texture list = the game's swappable `_bc` textures. Mesh list = static meshes
  (`LA_`/`SM_`). Skeletal meshes (`SK_`) and virtual textures aren't supported in
  this version.
- Importing raw `.fbx`/`.obj` and cooking meshes for you is planned for a future
  version (it will require a local Unreal Engine 5.4 install).

---

## Building from source

The Python sources, the PyInstaller `.spec`, and the dropdown data are in this
repo. The bundled third-party tooling is **not** committed (it's in the release
archive) — to build the exe yourself, place these under `Pak-Rat/vendor/`:

- **repak** (`repak.exe`) — pak (un)packer.
- **injector** — a build of Matyalatte's *UE4-DDS-Tools* with its own embedded
  Python 3.10 (`vendor/injector/python/`) and `texconv.dll`.
- **Oodle** (`oo2core_9_win64.dll`) — required to read the game's pak. This is
  proprietary (RAD/Epic) and is **not redistributed here**; copy it from your
  own Retro Rewind / UE install.

Then: `pip install pyinstaller PySide6 pillow` and
`pyinstaller Pak-Rat.spec --noconfirm`.

---

## Changelog

### 1.0.1
- Added **Extract Texture** mode — pull an original texture out of the game as
  PNG (easy to edit) or DDS (exact format + mips), then swap your edit back in
  via Regular Texture.

### 1.0.0
- Initial release: **Regular Texture** and **Mesh + Texture** packaging.

---
