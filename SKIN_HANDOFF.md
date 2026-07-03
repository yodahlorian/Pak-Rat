# Add-Asset Mesh-Skin — Handoff to Tron (2026-07-03)

**Owner: Tron** (needs UE 5.4.4 + the game — Ares can't run the UE cook, which is why this
kept getting deferred; that ends here). Ares owns direction; report progress via the grid.

## Where we are (v3.0.0-beta10 SHIPPED)
The whole Add-Asset flow works EXCEPT the custom mesh skin:
- ✅ name + price (relink.setcdo, length-neutral), catalogue registration (relink.widget_insert),
  StringTable name (relink.staddkey), version, #5 reset, #8 L10N, AV (relink.py replaces relink.exe).
- ✅ custom thumbnail = best-effort + NON-FATAL (inject user image → re-identity via relink.clone).
- ⛔ **custom texture (mesh skin) = NOT done.** beta10 removed the broken path (it injected the
  user image into the MESH uasset → crash) and made the wizard picker a no-op note. Your job:
  wire the REAL skin using the proven cook path.

## The proven mechanism to reuse (don't reinvent)
Cook mode already does "cook a mesh + apply a texture to it": it **retargets the mesh's material
slots to a REAL game material** (not the FBX's own), then rides the user's image through
`core.stage_texture(texture_mount, image, stage)`. See `Pak-Rat/DESIGN_NOTES.md` Step 4.
Key code:
- `cook.cook_meshes(env, items)` @cook.py:614 — cooks with material retarget via `_IMPORT_SCRIPT`
  @cook.py:240 (stubs MaterialInstanceConstants at the target's MI paths, assigns slots).
- `cook.resolve_mesh_materials(mesh_mount)` @cook.py:415 — the ordered MI_ paths a game mesh uses.
- `cook.run_cook_pipeline_multi(items, tex_items)` @cook.py:704 — cook + `stage_texture` per tex.
- `core.stage_texture(mount, img, stage)` — inject an image into a texture + stage it.

## The task
Make the Add-Asset item render the user's model wearing the user's texture, per-item (must NOT
clobber the exemplar's shared material/texture — the vanilla Couch must stay unchanged).

Exemplar = Couch (`inject.py` EXEMPLARS["Decoration"]): mesh `LA_Chair_A_01`, its material via
`resolve_mesh_materials(.../meshes/LA_Chair_A_01)`, and that material's base-colour `T_*_bc`.

Suggested approach (validate in-game, iterate):
1. Cook the user mesh (`inject._cook_user_mesh` @inject.py:~450) RETARGETED to the exemplar mesh's
   material (reuse cook_meshes' import path instead of the current plain FBX import) so it renders
   with a real material, not a missing one.
2. For a custom skin: per-item clone the material + its base-colour texture to new identities
   (`relink.clone`, any-length name renames — same tool that clones the item), reskin the texture
   with the user's image (`inject._reskin_and_stage`, already fixed to use `--save_folder`), and
   retarget the mesh's material ref → the per-item material. Ship mesh + MI_item + T_item_bc.
3. Wire into `inject.build_added_item` @inject.py:510 where the `texture` arg is currently a no-op
   note. **Keep it NON-FATAL** — a skin failure must never fail the add (name+price+model always build).
4. Re-enable the wizard texture picker text in `pak_rat.py` AddInputPage once it works.

## Env provisioning on your box (repo is host-only on Ares H:; clone from GitHub)
```
git clone git@github.com:yodahlorian/Pak-Rat.git   # branch 3.0.0-beta
cd Pak-Rat
dotnet publish tools/relink -c Release -r win-x64 --self-contained  # -> vendor/relink/UAssetAPI.dll (+deps)
# copy a .NET 8 runtime's host/ + shared/ into Pak-Rat/vendor/dotnet  (pythonnet hosts coreclr from there)
python.exe -m pip install pythonnet pyinstaller
python.exe Pak-Rat/pak_rat.py     # run the GUI; UE 5.4.4 required for Add-Asset (cooks the mesh)
```
relink.py test pattern: run via python.exe from the repo (UAssetAPI needs Windows paths).

## Gates for Yodah (do not proceed past these without approval)
- **Repo access**: clone from GitHub (above) OR Yodah mounts Ares H: on your box — Yodah's call.
- **vendor provisioning**: the `dotnet publish` + `vendor/dotnet` copy + pip installs — confirm OK.
- Report blockers via grid; Ares directs the fix.
