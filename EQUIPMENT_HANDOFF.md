# Equipment Cloner — Handoff to Tron (from Ares, 2026-07-04)

Ares can't test the cook/UE/game path (no UE/Blender/game on my box). You can. Yodah wants the
Equipment cloner built + the wall-mount cracked. Here's everything I measured from the base pak
(`R:\Content\Paks\RetroRewind-Windows.pak`) + the Grok reference. Build + validate in-game.

## Yodah's Equipment spec
Equipment selected → next → page with TWO buttons up top: **Add Arcade Machine**, **Add Pinball
Machine**. Click → clone the proper asset + populate its **texture + colour-pattern files** for
replacement. User adds multiple of each type, replaces textures, single batched cook → next →
cook + grow the **Equipment** catalogue widget alongside any staged **decorations** (staging must
work for both together) → deploy/save page as normal.

## THE TRUTH (Grok reference: `~mods/zzz_PacMan01_P.pak`)
That "arcade clone" is a **pure texture override** — it ships ONLY two textures and nothing else:
- `RetroRewind/Content/VideoStore/asset/textures/T_Arcade_A_01/T_Arcade_A_01_bc`   (arcade BODY)
- `RetroRewind/Content/VideoStore/asset/textures/T_Arcade_A_01/T_Arcade_A_VideoClips_01_bc` (SCREEN)
So a custom arcade = **replace those two textures**. To ADD a NEW arcade (not override the vanilla),
clone the Arcade BP to a new identity (KEEP its mesh) and repoint its material(s) to NEW copies of
those two textures at new paths — same trick as the decoration custom-thumbnail reskin, but on the
body/screen textures. These two `_bc` textures ARE Yodah's "texture + colour-pattern files".

## Exemplar data (measured from base pak)
ARCADE:
- BP: `prop/arcade/Arcade`  class `Arcade_C`  mesh `/Game/VideoStore/asset/meshes/LA_Arcade_A_01`
- materials: `textures/T_Arcade_A_01/MI_Arcade_A_01`, `MI_Arcade_A_02`  (colour-pattern MIs)
- textures to replace: `T_Arcade_A_01_bc` (body), `T_Arcade_A_VideoClips_01_bc` (screen)
- NO `Interface_Product_*` CDO key (unlike decorations) — so the decoration `setcdo` name/price swap
  does NOT apply. Keep vanilla name/price for now (Yodah didn't ask for custom equipment names).
PINBALL:
- BP: `prop/Pinball/Pinball-Machine_A`  class `Pinball-Machine_A_C` (derives from `Arcade_C`)
- inherits mesh from `Pinball-Machine_Base` (no own mesh ref) — KEEP mesh on clone.

## Build approach (additive — do NOT break the working decoration flow)
1. inject.py EXEMPLARS: add `arcade` + `pinball` entries, category `"Equipmement"`, flag
   `"keep_mesh": True` (+ list the two replaceable textures).
2. `_clone_exemplar(..., keep_mesh=False)`: when keep_mesh, DROP the mesh rename from `extra`
   (clone keeps the vanilla mesh ref); still rename self + class.
3. `build_added_item`: when `ex.get("keep_mesh")` → SKIP `_cook_user_mesh` (no user model) and SKIP
   `relink.setcdo` (equipment has no length-neutral title key). Then repoint the cloned arcade's
   material to NEW body/screen textures built from the user's images (reuse `_reskin_and_stage`,
   which already does extract→inject→re-identity→stage for a texture).
4. CAT_WIDGET["Equipmement"]: register under the **Equipment** catalogue function with an equipment
   sibling (`Arcade_C`). Confirm the exact function name in `UI_Catalogue_Widget` in-game —
   "Return Catalogue Equipment product class" is the likely name (mirrors the Decoration one).
5. Staging is AUTOMATIC: equipment goes through the same `run_add_pipeline` → `asset_library` → one
   pak, so it stages alongside decorations for free. Deploy page unchanged.
6. UI (pak_rat.py): when Equipment is the category, the per-item **Base** dropdown already lists
   `exemplars_for("Equipmement")` = Arcade + Pinball (multi-add already works). For equipment items
   the model picker is NOT needed — gate `isComplete`/`_current_item` so a keep_mesh base needs no
   model, just the two texture pickers (body + screen).

## Wall-mount — STOP tweaking the mesh (Ares tried 6 betas: collision, pivot front/back/73%/rotate)
Symptom is IDENTICAL every time: cloned wall item (Movie Poster base) mounts to the LOCKER SIDE,
never the WALL. Even the poster's OWN mesh cloned fails. => it is NOT the mesh — it's the
**clone/registration**. Investigate in-game: is the clone registered under the wrong catalogue
function (Decoration/Couch sibling) so it gets floor/object placement instead of the poster's wall
placement? Compare how vanilla `PosterFrame` is registered vs the clone. Fix at the registration
layer, not the mesh. (Measured: PosterFrame mesh component is IDENTITY; `Can Be Place On Wall` is a
raw CDO bool preserved by the clone.)

## Current shipped state
beta25 is live (yodahlorian/Pak-Rat, branch `3.0.0-beta`). Includes: wall/floor cloner, multi-add,
uemodel import, CUE4Parse mesh extractor (uemodel/FBX/OBJ/glTF now keep textures), extract UX fixes.
Decoration wall-mount + Equipment cloner are the open items → yours.

## Test checklist
[ ] Equipment: Add Arcade + Pinball, replace body/screen textures, add 2+ of each, one batched cook,
    both appear in the Equipment catalogue tab, deploy works, decorations in the same batch survive.
[ ] Wall-mount: find why the cloned poster won't wall-mount (registration angle).
Report results to `tron-out.md`.
