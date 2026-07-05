"""
Pak Rat — relink engine (Python, via pythonnet + UAssetAPI).

Replaces the old vendored `relink.exe` C# tool. The .NET apphost stub tripped
BitDefender (Gen:Suspicious.Cloud.15.2); loading the SAME proven UAssetAPI.dll
in-process via pythonnet removes the flagged binary while keeping UAssetAPI's
battle-tested UE5.4 package + Kismet serialisation.

The four operations inject.py needs:
  clone         — re-serialise an exemplar into a NEW package identity (+ any-length
                  name-map renames for the mesh/thumbnail swap).
  setcdo        — LENGTH-NEUTRAL edit of the item CDO: swap the localisation
                  title-key (same byte length) + patch the inline price double.
                  (The old tool changed the key's length, shifting price/thumbnail
                  in the opaque CDO blob → price 0 / blank thumb / purchase crash.)
  staddkey      — add a key -> display-name pair to the Interface StringTable.
  widget_insert — append a new product class into the catalogue widget's
                  bytecode, re-linking jump offsets by the real +9B cooked delta.

All heavy lifting stays in UAssetAPI.dll; this module is just the logic + a thin
CLR bridge. Ships the .NET runtime alongside (no user .NET install required).
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import core

# ---------------------------------------------------------------------------
# CLR bridge — host coreclr once, load UAssetAPI, expose the types we use.
# ---------------------------------------------------------------------------
_LOADED = False
_T: dict = {}          # resolved .NET types, filled by _ensure()


def _dotnet_root() -> str | None:
    """Locate a .NET 8 runtime for pythonnet to host.

    Order: the runtime we bundle under vendor/dotnet (self-contained — this is
    what ships, so it must win over ambient machine state; a stray DOTNET_ROOT or
    a system install must not hijack hosting); then an explicit DOTNET_ROOT; then
    the dev-box install in LOCALAPPDATA. Returns a Windows path string, or None to
    let clr_loader fall back to its own discovery.
    """
    bundled = core.VENDOR("dotnet")                      # shipping layout — wins
    if (bundled / "host").exists():
        return str(bundled)
    env = os.environ.get("DOTNET_ROOT")
    if env and Path(env).exists():
        return env
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dev = Path(local) / "Microsoft" / "dotnet"       # dev install
        if dev.exists():
            return str(dev)
    return None


def _host_coreclr(root: str | None) -> None:
    """Host coreclr, pinned to the bundled runtime whenever we have one.

    Passing clr_loader an explicit DotnetCoreRuntimeSpec makes it skip its own
    runtime discovery — which otherwise consults the `dotnet` CLI / PATH / the
    machine's registered runtimes — so hosting depends ONLY on the runtime we
    ship, never on ambient machine state. (That discovery is why the same build
    ran on a dev box yet died on a clean machine.)

    pythonnet re-wraps any failure as an opaque "Failed to create a .NET runtime
    (coreclr) using the parameters {}" and callers upstream only kept str(e), so
    the true fault was invisible. Here we unwrap the chained __cause__ and raise
    it verbatim, so a missing/quarantined file, access-denied, bad-image, or
    missing-UCRT error actually reaches the user and the log.
    """
    from pythonnet import load, set_runtime
    try:
        if root:
            os.environ["DOTNET_ROOT"] = root
            from clr_loader import DotnetCoreRuntimeSpec, get_coreclr
            fw = Path(root) / "shared" / "Microsoft.NETCore.App"
            vers = sorted(
                (p.name for p in fw.iterdir() if p.is_dir() and p.name[:1].isdigit()),
                key=lambda v: tuple(int(x) for x in v.split("-")[0].split(".")),
            )
            if not vers:
                raise RuntimeError(f"no Microsoft.NETCore.App framework under {fw}")
            ver = vers[-1]
            spec = DotnetCoreRuntimeSpec("Microsoft.NETCore.App", ver, fw / ver)
            set_runtime(get_coreclr(dotnet_root=root, runtime_spec=spec))
        load("coreclr")   # honours a pinned runtime; else clr_loader self-discovers
    except Exception as exc:                              # noqa: BLE001
        cause = exc.__cause__ or exc
        raise RuntimeError(
            f"Failed to host the .NET 8 runtime (root={root!r}). "
            f"Underlying cause — {type(cause).__name__}: {cause}"
        ) from exc


def _ensure() -> None:
    """Idempotently host the CLR and import the UAssetAPI types we need."""
    global _LOADED
    if _LOADED:
        return
    _host_coreclr(_dotnet_root())
    import clr  # noqa: F401  (activates the CLR import hook)
    clr.AddReference(str(core.VENDOR("relink", "UAssetAPI.dll")))

    import System
    from System.IO import MemoryStream
    from System.Reflection import BindingFlags
    from System.Collections import IEnumerable as NetEnumerable
    from UAssetAPI import UAsset, Import, AssetBinaryWriter
    from UAssetAPI.UnrealTypes import EngineVersion, FString, FName, FPackageIndex
    from UAssetAPI.Unversioned import Usmap
    from UAssetAPI.ExportTypes import (
        RawExport, StringTableExport, FunctionExport, NormalExport)
    from UAssetAPI.Kismet.Bytecode import ExpressionSerializer, KismetExpression
    from UAssetAPI.Kismet.Bytecode.Expressions import (
        EX_SetArray, EX_ObjectConst, EX_Jump, EX_JumpIfNot, EX_Skip,
        EX_PushExecutionFlow, EX_SkipOffsetConst, EX_SwitchValue, EX_ComputedJump)

    _T.update(
        System=System, MemoryStream=MemoryStream, BindingFlags=BindingFlags,
        NetEnumerable=NetEnumerable, UAsset=UAsset, EngineVersion=EngineVersion,
        FString=FString, FName=FName, FPackageIndex=FPackageIndex, Usmap=Usmap,
        Import=Import, AssetBinaryWriter=AssetBinaryWriter, RawExport=RawExport,
        StringTableExport=StringTableExport, FunctionExport=FunctionExport,
        NormalExport=NormalExport, ExpressionSerializer=ExpressionSerializer,
        KismetExpression=KismetExpression, EX_SetArray=EX_SetArray,
        EX_ObjectConst=EX_ObjectConst, EX_Jump=EX_Jump, EX_JumpIfNot=EX_JumpIfNot,
        EX_Skip=EX_Skip, EX_PushExecutionFlow=EX_PushExecutionFlow,
        EX_SkipOffsetConst=EX_SkipOffsetConst, EX_SwitchValue=EX_SwitchValue,
        EX_ComputedJump=EX_ComputedJump)
    _LOADED = True


def _usmap():
    return _T["Usmap"](str(core.VENDOR("RetroRewindMappings.usmap")))


def _open(path):
    """Open a .uasset (+ its .uexp/.ubulk siblings) at UE5.4 with the RR usmap."""
    _ensure()
    return _T["UAsset"](str(path), _T["EngineVersion"].VER_UE5_4, _usmap())


# ---------------------------------------------------------------------------
# clone — re-serialise an exemplar into a NEW unique package identity.
#
# Renames name-map entries (self path, class token, Default__ token, + any extra
# old/new pairs for the mesh + thumbnail), then stamps a fresh PackageGuid /
# PackageSource / FolderName. Renames are exact name-map string swaps (any
# length — UAssetAPI recomputes offsets on Write). Faithful port of the C# Clone.
# ---------------------------------------------------------------------------
def clone(in_ua, out_ua, old_self: str, new_self: str,
          old_cls: str, new_cls: str, *extra: str) -> None:
    asset = _open(in_ua)
    names = asset.GetNameMapIndexList()

    def ren(old: str, new: str) -> None:
        if old == new or old == "-" or new == "-":
            return
        for i in range(names.Count):
            if names[i].Value == old:
                asset.SetNameReference(i, _T["FString"](new))
                return
        # not found -> silently skip (mirrors C#; some exemplars lack a slot)

    ren(old_self, new_self)
    ren(old_cls, new_cls)
    ren("Default__" + old_cls, "Default__" + new_cls)
    for i in range(0, len(extra) - 1, 2):
        ren(extra[i], extra[i + 1])

    g = _T["System"].Guid.NewGuid()
    asset.PackageGuid = g
    asset.PackageSource = g.GetHashCode() & 0x7FFFFFFF
    asset.FolderName = _T["FString"](new_self)
    asset.Write(str(out_ua))


# ---------------------------------------------------------------------------
# setcdo — LENGTH-NEUTRAL edit of the item CDO (fixes #6/#7).
#
# The CDO is a RawExport: an opaque unversioned-property blob laid out as
#   [int32 len][title-key FString + \0][8-byte price double][thumbnail/unlock…]
# The old C# tool swapped the title-key for a DIFFERENT-length string, sliding
# the price double + everything after it → price 0 / blank thumbnail / crash.
# We require the new key to be the SAME byte length as the old (inject picks the
# token length to match), so the swap + price patch are pure in-place overwrites:
# the blob length never changes, nothing shifts.
# ---------------------------------------------------------------------------
def _find_title_key(d: bytes) -> tuple[int, int, str]:
    """Locate the inline localisation title-key FString in a CDO blob.
    Returns (char_offset, char_count_incl_null, string). Mirrors the C# scan:
    an int32-length-prefixed ASCII FString starting 'Interface_' and naming a
    Product / _Title / ModKit key."""
    i = 0
    while i + 4 < len(d):
        n = struct.unpack_from("<i", d, i)[0]
        if 4 < n < 200 and i + 4 + n <= len(d) and d[i + 4 + n - 1] == 0:
            s = d[i + 4:i + 4 + n - 1].decode("ascii", "ignore")
            if s.startswith("Interface_") and (
                    "Product" in s or "_Title" in s or "ModKit" in s):
                return i + 4, n, s          # offset of the chars, count incl null
        i += 1
    raise RuntimeError("title-key not found in CDO blob")


def setcdo(in_ua, out_ua, cdo_name: str, new_key: str,
           price: float | None = None) -> None:
    asset = _open(in_ua)
    cdo = next(e for e in asset.Exports if e.ObjectName.ToString() == cdo_name)
    d = bytearray(bytes(cdo.Data))
    coff, n, olds = _find_title_key(d)
    old_chars = n - 1                        # excluding the null terminator
    kb = new_key.encode("ascii")
    if len(kb) != old_chars:
        raise ValueError(
            f"setcdo requires a LENGTH-NEUTRAL key: '{new_key}' is {len(kb)} chars "
            f"but the slot is {old_chars} (old key '{olds}'). Match the token length.")
    d[coff:coff + old_chars] = kb            # overwrite the key chars in place (null stays)
    if price is not None:
        poff = coff + n                      # the price double sits right after the FString
        struct.pack_into("<d", d, poff, float(price))
    from System import Array, Byte
    cdo.Data = Array[Byte](bytes(d))
    asset.Write(str(out_ua))


# ---------------------------------------------------------------------------
# box_repoint — retarget a vending-box's product-class ARRAY to one product.
#
# Snack/drink/toy catalogue items are 2-TIER: the catalogue sells a vending BOX
# (SnackBox_Snack_C / DrinkBox_C / ToysBox_C) whose CDO holds an ARRAY of product
# BP-class refs (the shelf it dispenses). The box CDO is a RawExport whose blob
# contains, as one property value, [int32 N][N x int32 FPackageIndex] — each
# element an import ref to a product class. We overwrite EVERY element with the
# import index of `product_cls` (the user's cloned product), so the box shows /
# dispenses only the custom product. Length-neutral (indices only), nothing shifts.
# ---------------------------------------------------------------------------
def box_repoint(in_ua, out_ua, box_cdo_name: str, product_cls: str) -> None:
    asset = _open(in_ua)
    j = _imp_index(asset, product_cls)
    if j < 0:
        raise RuntimeError(f"box_repoint: product class '{product_cls}' is not an import")
    raw_index = -(j + 1)                       # FPackageIndex for import j
    n_imports = asset.Imports.Count
    cdo = next(e for e in asset.Exports if e.ObjectName.ToString() == box_cdo_name)
    d = bytearray(bytes(cdo.Data))
    # Locate the array: an int32 count C (2..128) immediately followed by C int32s
    # that are ALL valid import indices (-n_imports..-1). The product-class array is
    # the only run of that shape in the blob.
    off = None
    i = 0
    while i + 4 <= len(d):
        c = struct.unpack_from("<i", d, i)[0]
        if 2 <= c <= 128 and i + 4 + 4 * c <= len(d) and all(
                -n_imports <= struct.unpack_from("<i", d, i + 4 + 4 * k)[0] <= -1
                for k in range(c)):
            off, count = i + 4, c
            break
        i += 1
    if off is None:
        raise RuntimeError("box_repoint: product-class array not found in box CDO blob")
    for k in range(count):
        struct.pack_into("<i", d, off + 4 * k, raw_index)
    from System import Array, Byte
    cdo.Data = Array[Byte](bytes(d))
    asset.Write(str(out_ua))


# ---------------------------------------------------------------------------
# staddkey — add a key -> display-name pair to the Interface StringTable.
# The parsed StringTableExport.Table is a clean map (chainable across items).
# ---------------------------------------------------------------------------
def staddkey(in_ua, out_ua, key: str, val: str) -> None:
    asset = _open(in_ua)
    e = next(x for x in asset.Exports if isinstance(x, _T["StringTableExport"]))
    e.Table[_T["FString"](key)] = _T["FString"](val)
    asset.Write(str(out_ua))


# ---------------------------------------------------------------------------
# widget_insert — append a new product class into the catalogue widget's Kismet
# bytecode (port of the C# default `Relink`). Adds the package/class/CDO imports,
# appends an EX_ObjectConst(new class) into the SetArray that already holds the
# sibling class, then RE-LINKS every jump/skip/switch offset by the REAL on-disk
# bytecode delta (asset.Write's cooked EX_ObjectConst is 9B; the in-memory
# serialize under-measures it, so we measure the true growth from ScriptBytecodeSize).
# ---------------------------------------------------------------------------
def _members(o):
    t = o.GetType()
    flags = _T["BindingFlags"].Public | _T["BindingFlags"].Instance
    out = []
    for f in t.GetFields(flags):
        try:
            v = f.GetValue(o)
        except Exception:
            v = None
        if v is not None:
            out.append(v)
    for p in t.GetProperties(flags):
        if p.GetIndexParameters().Length > 0:
            continue
        try:
            v = p.GetValue(o)
        except Exception:
            v = None
        if v is not None:
            out.append(v)
    return out


def _is_enum(o) -> bool:
    return isinstance(o, _T["NetEnumerable"]) and not isinstance(o, str)


def _collect(o, out, d: int) -> None:
    if o is None or d > 80:
        return
    KE = _T["KismetExpression"]
    if isinstance(o, KE):
        out.append(o)
        for v in _members(o):
            if isinstance(v, KE) or _is_enum(v):
                _collect(v, out, d + 1)
    elif _is_enum(o):
        for it in o:
            if isinstance(it, KE):
                _collect(it, out, d + 1)
            elif it is not None and not isinstance(it, str):
                try:
                    ns = it.GetType().Namespace
                    prim = it.GetType().IsPrimitive
                except Exception:
                    continue
                if not prim and ns and "UAssetAPI" in ns:
                    for v in _members(it):
                        if isinstance(v, KE) or _is_enum(v):
                            _collect(v, out, d + 1)


def _serialize(asset, exprs) -> bytes:
    ms = _T["MemoryStream"]()
    w = _T["AssetBinaryWriter"](ms, asset)
    for ex in exprs:
        _T["ExpressionSerializer"].WriteExpression(ex, w)
    w.Flush()
    return bytes(ms.ToArray())


def _bump(val, ins, delta):
    return val + delta if val >= ins else val


def _imp_index(asset, name: str) -> int:
    for i in range(asset.Imports.Count):
        if asset.Imports[i].ObjectName.ToString() == name:
            return i
    return -1


def widget_insert(in_ua, out_ua, new_pkg: str, new_cls: str,
                  sib_cls: str, fn: str) -> None:
    _ensure()
    Import = _T["Import"]
    FName = _T["FName"]
    FPI = _T["FPackageIndex"]
    asset = _open(in_ua)

    sib = next(i for i in asset.Imports if i.ObjectName.ToString() == sib_cls)
    sib_pkg = sib.OuterIndex.ToImport(asset)
    asset.Imports.Add(Import(sib_pkg.ClassPackage, sib_pkg.ClassName,
                             FPI.FromRawIndex(0), FName(asset, new_pkg), False))
    pkg_idx = FPI.FromImport(asset.Imports.Count - 1)
    asset.Imports.Add(Import(sib.ClassPackage, sib.ClassName, pkg_idx,
                             FName(asset, new_cls), False))
    cls_idx = FPI.FromImport(asset.Imports.Count - 1)
    # the CDO import Default__<newCls> — a native catalogue entry has this trio;
    # missing it crashes the catalogue page build.
    asset.Imports.Add(Import(FName(asset, new_pkg), FName(asset, new_cls),
                             pkg_idx, FName(asset, "Default__" + new_cls), False))

    fe = next(e for e in asset.Exports
              if isinstance(e, _T["FunctionExport"]) and e.ObjectName.ToString() == fn)
    before = _serialize(asset, fe.ScriptBytecode)

    sib_idx = FPI.FromImport(_imp_index(asset, sib_cls)).Index
    nodes = []
    for r in fe.ScriptBytecode:
        _collect(r, nodes, 0)
    sa = next(n for n in nodes if isinstance(n, _T["EX_SetArray"]) and any(
        isinstance(el, _T["EX_ObjectConst"]) and el.Value is not None
        and el.Value.Index == sib_idx for el in n.Elements))

    from System import Array
    oc = _T["EX_ObjectConst"]()
    oc.Value = cls_idx
    lst = list(sa.Elements) + [oc]
    sa.Elements = Array[_T["KismetExpression"]](lst)

    after = _serialize(asset, fe.ScriptBytecode)
    ins = 0
    while ins < len(before) and ins < len(after) and before[ins] == after[ins]:
        ins += 1

    if fe.CreateBeforeSerializationDependencies is None:
        from System.Collections.Generic import List
        fe.CreateBeforeSerializationDependencies = List[_T["FPackageIndex"]]()
    fe.CreateBeforeSerializationDependencies.Add(cls_idx)

    # REAL delta: the in-memory serialize under-measures EX_ObjectConst vs the
    # cooked 9B form; measure true growth from on-disk ScriptBytecodeSize.
    old_bc = fe.ScriptBytecodeSize
    asset.Write(str(out_ua))
    tmp = _open(out_ua)
    tmp_fe = next(e for e in tmp.Exports
                  if isinstance(e, _T["FunctionExport"]) and e.ObjectName.ToString() == fn)
    delta = tmp_fe.ScriptBytecodeSize - old_bc
    real_ins = int(ins * tmp_fe.ScriptBytecodeSize / len(after)) if after else 0

    nodes2 = []
    for r in fe.ScriptBytecode:
        _collect(r, nodes2, 0)
    if any(isinstance(n, _T["EX_ComputedJump"]) for n in nodes2):
        raise RuntimeError("widget_insert ABORT: EX_ComputedJump present (unrelinkable)")

    for n in nodes2:
        if isinstance(n, _T["EX_Jump"]):
            n.CodeOffset = _bump(n.CodeOffset, real_ins, delta)
        elif isinstance(n, _T["EX_JumpIfNot"]):
            n.CodeOffset = _bump(n.CodeOffset, real_ins, delta)
        elif isinstance(n, _T["EX_Skip"]):
            n.CodeOffset = _bump(n.CodeOffset, real_ins, delta)
        elif isinstance(n, _T["EX_PushExecutionFlow"]):
            n.PushingAddress = _bump(n.PushingAddress, real_ins, delta)
        elif isinstance(n, _T["EX_SkipOffsetConst"]):
            n.Value = _bump(n.Value, real_ins, delta)
        elif isinstance(n, _T["EX_SwitchValue"]):
            n.EndGotoOffset = _bump(n.EndGotoOffset, real_ins, delta)
            cases = n.Cases
            for c in range(cases.Length):
                cs = cases[c]
                cs.NextOffset = _bump(cs.NextOffset, real_ins, delta)
                cases[c] = cs
    asset.Write(str(out_ua))
