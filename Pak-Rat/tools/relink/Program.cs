using System;
using System.IO;
using System.Linq;
using System.Collections;
using System.Collections.Generic;
using UAssetAPI;
using UAssetAPI.ExportTypes;
using UAssetAPI.UnrealTypes;
using UAssetAPI.Unversioned;
using UAssetAPI.Kismet;
using UAssetAPI.Kismet.Bytecode;
using UAssetAPI.Kismet.Bytecode.Expressions;

class P {
    static byte[] Serialize(UAsset asset, KismetExpression[] exprs, List<int> starts=null) {
        var ms = new MemoryStream();
        var w = new AssetBinaryWriter(ms, asset);
        foreach (var ex in exprs) { if (starts!=null) starts.Add((int)w.BaseStream.Position); ExpressionSerializer.WriteExpression(ex, w); }
        w.Flush();
        return ms.ToArray();
    }
    static IEnumerable<object> Members(object o) {
        var t = o.GetType();
        foreach (var f in t.GetFields(System.Reflection.BindingFlags.Public|System.Reflection.BindingFlags.Instance)) { object v=null; try{v=f.GetValue(o);}catch{} if(v!=null) yield return v; }
        foreach (var p in t.GetProperties(System.Reflection.BindingFlags.Public|System.Reflection.BindingFlags.Instance)) { if(p.GetIndexParameters().Length>0) continue; object v=null; try{v=p.GetValue(o);}catch{} if(v!=null) yield return v; }
    }
    static void Collect(object o, List<KismetExpression> outp, int d) {
        if (o == null || d > 80) return;
        if (o is KismetExpression ke) {
            outp.Add(ke);
            foreach (var v in Members(ke)) if (v is KismetExpression || (v is IEnumerable && !(v is string))) Collect(v, outp, d+1);
        } else if (o is IEnumerable en && !(o is string)) {
            foreach (var it in en) {
                if (it is KismetExpression) Collect(it, outp, d+1);
                else if (it != null && !it.GetType().IsPrimitive && !(it is string) && it.GetType().Namespace!=null && it.GetType().Namespace.Contains("UAssetAPI"))
                    foreach (var v in Members(it)) if (v is KismetExpression || (v is IEnumerable && !(v is string))) Collect(v, outp, d+1);
            }
        }
    }
    static int Fixed = 0;
    static uint Bump(uint val, int ins, int delta, string tag, List<string> log) {
        if (val >= (uint)ins) { log.Add($"    {tag}: {val} -> {val+(uint)delta}"); Fixed++; return val + (uint)delta; }
        return val;
    }
    static void Relink(string inW, string usmapP, string outW, string newPkg, string newCls, string sibCls, string fn) {
        var asset = new UAsset(inW, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        Import sib = asset.Imports.FirstOrDefault(i => i.ObjectName.ToString() == sibCls);
        Import sibPkg = sib.OuterIndex.ToImport(asset);
        asset.Imports.Add(new Import(sibPkg.ClassPackage, sibPkg.ClassName, FPackageIndex.FromRawIndex(0), new FName(asset, newPkg), false));
        var pkgIdx = FPackageIndex.FromImport(asset.Imports.Count - 1);
        asset.Imports.Add(new Import(sib.ClassPackage, sib.ClassName, pkgIdx, new FName(asset, newCls), false));
        var clsIdx = FPackageIndex.FromImport(asset.Imports.Count - 1);
        // FIX: also add the CDO import Default__<newCls> (class=newCls, classPkg=newPkg, outer=pkg) — the trio a native decoration entry has; missing it crashes the page build.
        asset.Imports.Add(new Import(new FName(asset, newPkg), new FName(asset, newCls), pkgIdx, new FName(asset, "Default__" + newCls), false));
        Console.WriteLine($"added CDO import Default__{newCls}");

        FunctionExport fe = (FunctionExport)asset.Exports.First(e => e is FunctionExport f && f.ObjectName.ToString() == fn);
        var before = Serialize(asset, fe.ScriptBytecode);
        // target the SetArray that already holds the SIBLING class (append next to it, like Grok)
        int sibIdx = FPackageIndex.FromImport(asset.Imports.FindIndex(i => i.ObjectName.ToString()==sibCls)).Index;
        var allNodes = new List<KismetExpression>(); foreach (var r in fe.ScriptBytecode) Collect(r, allNodes, 0);
        EX_SetArray sa = (EX_SetArray)allNodes.First(n => n is EX_SetArray s && s.Elements.Any(el => el is EX_ObjectConst oc && oc.Value!=null && oc.Value.Index==sibIdx));
        Console.WriteLine($"target SetArray = the one holding {sibCls} (had {sa.Elements.Length} elems)");
        var lst = new List<KismetExpression>(sa.Elements){ new EX_ObjectConst { Value = clsIdx } };
        sa.Elements = lst.ToArray();
        var after = Serialize(asset, fe.ScriptBytecode);
        int ins = 0; while (ins < before.Length && ins < after.Length && before[ins]==after[ins]) ins++;
        int serDelta = after.Length - before.Length;
        fe.CreateBeforeSerializationDependencies ??= new List<FPackageIndex>();
        fe.CreateBeforeSerializationDependencies.Add(clsIdx);
        // REAL delta from on-disk ScriptBytecodeSize (Serialize helper under-measures EX_ObjectConst vs asset.Write's cooked 9-byte format)
        int oldBc = fe.ScriptBytecodeSize;
        asset.Write(outW);
        var tmpA = new UAsset(outW, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        var tmpFe = (FunctionExport)tmpA.Exports.First(e => e is FunctionExport f && f.ObjectName.ToString() == fn);
        int delta = tmpFe.ScriptBytecodeSize - oldBc;
        int realIns = (int)((long)ins * tmpFe.ScriptBytecodeSize / after.Length);
        Console.WriteLine($"serInsOff={ins} realIns={realIns} realDelta={delta} (Serialize said {serDelta})");
        var nodes = new List<KismetExpression>(); foreach (var r in fe.ScriptBytecode) Collect(r, nodes, 0);
        if (nodes.Any(n => n is EX_ComputedJump)) { Console.WriteLine("ABORT: EX_ComputedJump present"); return; }
        var log = new List<string>();
        foreach (var n in nodes) {
            switch (n) {
                case EX_Jump j: j.CodeOffset = Bump(j.CodeOffset, realIns, delta, "Jump", log); break;
                case EX_JumpIfNot j: j.CodeOffset = Bump(j.CodeOffset, realIns, delta, "JumpIfNot", log); break;
                case EX_Skip s: s.CodeOffset = Bump(s.CodeOffset, realIns, delta, "Skip", log); break;
                case EX_PushExecutionFlow p: p.PushingAddress = Bump(p.PushingAddress, realIns, delta, "PushExecFlow", log); break;
                case EX_SkipOffsetConst k: k.Value = Bump(k.Value, realIns, delta, "SkipOffsetConst", log); break;
                case EX_SwitchValue sw:
                    sw.EndGotoOffset = Bump(sw.EndGotoOffset, realIns, delta, "Switch.End", log);
                    for (int c=0;c<sw.Cases.Length;c++){ var cs=sw.Cases[c]; cs.NextOffset=Bump(cs.NextOffset,realIns,delta,"Switch.Case",log); sw.Cases[c]=cs; }
                    break;
            }
        }
        foreach (var l in log) Console.WriteLine(l);
        asset.Write(outW);
        Console.WriteLine("WROTE " + outW);
    }
    static List<uint> Offsets(UAsset asset, string fn) {
        var fe = (FunctionExport)asset.Exports.First(e => e is FunctionExport f && f.ObjectName.ToString()==fn);
        var nodes = new List<KismetExpression>(); foreach (var r in fe.ScriptBytecode) Collect(r, nodes, 0);
        var outp = new List<uint>();
        foreach (var n in nodes) {
            if (n is EX_Jump j) outp.Add(j.CodeOffset);
            else if (n is EX_JumpIfNot k) outp.Add(k.CodeOffset);
            else if (n is EX_Skip s) outp.Add(s.CodeOffset);
            else if (n is EX_PushExecutionFlow p) outp.Add(p.PushingAddress);
            else if (n is EX_SkipOffsetConst so) outp.Add((uint)so.Value);
            else if (n is EX_SwitchValue sw){ outp.Add(sw.EndGotoOffset); foreach(var c in sw.Cases) outp.Add(c.NextOffset); }
        }
        return outp;
    }
    static void Cmp(string a, string b, string usmapP, string fn) {
        var m = new Usmap(usmapP);
        var oa = Offsets(new UAsset(a, EngineVersion.VER_UE5_4, m), fn);
        var ob = Offsets(new UAsset(b, EngineVersion.VER_UE5_4, m), fn);
        Console.WriteLine($"ORACLE cmp {fn}: base offsets={string.Join(",",oa)}");
        Console.WriteLine($"             grok offsets={string.Join(",",ob)}");
        Console.WriteLine($"             deltas={string.Join(",", ob.Zip(oa,(x,y)=>(long)x-y))}");
    }
    static void Verify(string w, string usmapP, string fn, string needle) {
        // reflect EX_ObjectConst.Value member kind/type
        var tt = typeof(EX_ObjectConst);
        var f = tt.GetField("Value"); var pr = tt.GetProperty("Value");
        Console.WriteLine($"EX_ObjectConst.Value: field={(f!=null?f.FieldType.Name:"none")} property={(pr!=null?pr.PropertyType.Name:"none")}");
        var asset = new UAsset(w, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        var fe = (FunctionExport)asset.Exports.First(e => e is FunctionExport ff && ff.ObjectName.ToString()==fn);
        var allN = new List<KismetExpression>(); foreach (var r in fe.ScriptBytecode) Collect(r, allN, 0);
        int sai=0;
        foreach (var sa in allN.OfType<EX_SetArray>()) {
            sai++;
            var strs = sa.Elements.OfType<EX_ObjectConst>().Select(oc => {
                var v=oc.Value; return v==null?"NULL":(v.Index==0?"ZERO":$"{v.Index}:{(v.IsImport()?v.ToImport(asset).ObjectName.ToString():"exp")}"); });
            if (strs.Any(s=>s.Contains(needle)||s=="NULL"||s=="ZERO"))
                Console.WriteLine($"   SetArray#{sai} Elements={sa.Elements.Length}: {string.Join(" | ", strs)}");
        }
        foreach (var im in asset.Imports) if (im.ObjectName.ToString().Contains(needle))
            Console.WriteLine($"import {im.ObjectName}: class={im.ClassName} classPkg={im.ClassPackage} outer={im.OuterIndex.Index}");
        Console.WriteLine("EDL CreateBeforeSerializationDeps tail: " + string.Join(",", fe.CreateBeforeSerializationDependencies.TakeLast(3)));
    }
    static void Deps(string w, string usmapP, string clsName) {
        var asset = new UAsset(w, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        int idx = 0;
        for (int i=0;i<asset.Imports.Count;i++) if (asset.Imports[i].ObjectName.ToString()==clsName) idx = FPackageIndex.FromImport(i).Index;
        Console.WriteLine($"{clsName} import index = {idx}");
        string[] kinds = {"SerBeforeSer","CreateBeforeSer","SerBeforeCreate","CreateBeforeCreate"};
        foreach (var e in asset.Exports) {
            var arrs = new List<FPackageIndex>[]{ e.SerializationBeforeSerializationDependencies, e.CreateBeforeSerializationDependencies, e.SerializationBeforeCreateDependencies, e.CreateBeforeCreateDependencies };
            for (int k=0;k<4;k++) if (arrs[k]!=null && arrs[k].Any(x=>x.Index==idx))
                Console.WriteLine($"  referenced in export '{e.ObjectName}' [{kinds[k]}]");
        }
        // also: is idx in the class's own export deps? and PackageFlags of the import object
        var im = asset.Imports.First(i=>i.ObjectName.ToString()==clsName);
        Console.WriteLine($"  import bImportOptional={im.bImportOptional}");
    }
    static void Retarget(string inW, string usmapP, string outW, string existingCls, string newPkg, string newCls) {
        var asset = new UAsset(inW, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        var cls = asset.Imports.First(i => i.ObjectName.ToString()==existingCls);
        var pkg = cls.OuterIndex.ToImport(asset);
        string oldPkgPath = pkg.ObjectName.ToString();
        Console.WriteLine($"retarget '{existingCls}' pkg '{oldPkgPath}' -> '{newCls}' pkg '{newPkg}'");
        foreach (var im in asset.Imports) if (im.OuterIndex.Index == cls.OuterIndex.Index) {
            string on = im.ObjectName.ToString(), cn = im.ClassName.ToString(), cp = im.ClassPackage.ToString();
            if (on == existingCls) im.ObjectName = new FName(asset, newCls);
            else if (on == "Default__" + existingCls) im.ObjectName = new FName(asset, "Default__" + newCls);
            if (cn == existingCls) im.ClassName = new FName(asset, newCls);
            if (cp == oldPkgPath) im.ClassPackage = new FName(asset, newPkg);
            Console.WriteLine($"     slot -> {im.ObjectName} (class {im.ClassName}, classPkg {im.ClassPackage})");
        }
        pkg.ObjectName = new FName(asset, newPkg);
        asset.Write(outW);
        Console.WriteLine("WROTE " + outW);
    }
    static void Edl(string w, string usmapP, string fn, string cls1, string cls2) {
        var asset = new UAsset(w, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        int i1 = FPackageIndex.FromImport(asset.Imports.FindIndex(i=>i.ObjectName.ToString()==cls1)).Index;
        int i2 = cls2=="-" ? 0 : FPackageIndex.FromImport(asset.Imports.FindIndex(i=>i.ObjectName.ToString()==cls2)).Index;
        Console.WriteLine($"{cls1}={i1}  {cls2}={i2}");
        var fe = (FunctionExport)asset.Exports.First(e => e is FunctionExport ff && ff.ObjectName.ToString()==fn);
        void show(string nm, List<FPackageIndex> arr) {
            var idxs = arr==null? new List<int>() : arr.Select(x=>x.Index).ToList();
            Console.WriteLine($"  {nm}: has({cls1})={idxs.Contains(i1)} has({cls2})={(i2!=0&&idxs.Contains(i2))}  [{string.Join(",",idxs)}]");
        }
        show("SerBeforeSer", fe.SerializationBeforeSerializationDependencies);
        show("CreateBeforeSer", fe.CreateBeforeSerializationDependencies);
        show("SerBeforeCreate", fe.SerializationBeforeCreateDependencies);
        show("CreateBeforeCreate", fe.CreateBeforeCreateDependencies);
    }
    static void ImpDump(string w, string usmapP, string nm) {
        var asset = new UAsset(w, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        foreach (var im in asset.Imports) if (im.ObjectName.ToString()==nm || im.ObjectName.ToString()==nm.Replace("_C","")) {
            Console.WriteLine($"IMPORT {im.ObjectName}:");
            foreach (var f in typeof(Import).GetFields()) { object v=null; try{v=f.GetValue(im);}catch{} Console.WriteLine($"    {f.Name} = {v}"); }
        }
    }
    static void InsE(string inW, string usmapP, string outW, string existingCls, string sibCls, string fn) {
        // append an EXISTING class (reuse its import, NO new import) into the SetArray holding sibCls + relink.
        var asset = new UAsset(inW, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        var clsIdx = FPackageIndex.FromImport(asset.Imports.FindIndex(i => i.ObjectName.ToString()==existingCls));
        FunctionExport fe = (FunctionExport)asset.Exports.First(e => e is FunctionExport f && f.ObjectName.ToString() == fn);
        var before = Serialize(asset, fe.ScriptBytecode);
        int sibIdx = FPackageIndex.FromImport(asset.Imports.FindIndex(i => i.ObjectName.ToString()==sibCls)).Index;
        var allNodes = new List<KismetExpression>(); foreach (var r in fe.ScriptBytecode) Collect(r, allNodes, 0);
        EX_SetArray sa = (EX_SetArray)allNodes.First(n => n is EX_SetArray s && s.Elements.Any(el => el is EX_ObjectConst oc && oc.Value!=null && oc.Value.Index==sibIdx));
        Console.WriteLine($"target SetArray had {sa.Elements.Length} elems; append existing {existingCls}({clsIdx.Index})");
        var lst = new List<KismetExpression>(sa.Elements){ new EX_ObjectConst { Value = clsIdx } };
        sa.Elements = lst.ToArray();
        var after = Serialize(asset, fe.ScriptBytecode);
        int ins = 0; while (ins < before.Length && ins < after.Length && before[ins]==after[ins]) ins++;
        int serDelta = after.Length - before.Length;
        // REAL delta: the Serialize helper uses a smaller EX_ObjectConst encoding than asset.Write's cooked format.
        // Measure the true byte growth from the on-disk ScriptBytecodeSize (write once, reread).
        int oldBc = fe.ScriptBytecodeSize;
        asset.Write(outW);
        var tmpA = new UAsset(outW, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        var tmpFe = (FunctionExport)tmpA.Exports.First(e => e is FunctionExport f && f.ObjectName.ToString() == fn);
        int delta = tmpFe.ScriptBytecodeSize - oldBc;
        // real insertion offset: scale my broken-space ins into real space by the element-size ratio (real ScriptBytecodeSize / my Serialize length)
        int realIns = (int)((long)ins * tmpFe.ScriptBytecodeSize / after.Length);
        Console.WriteLine($"serInsOff={ins} realIns={realIns} realDelta={delta} (Serialize said {serDelta})");
        var nodes = new List<KismetExpression>(); foreach (var r in fe.ScriptBytecode) Collect(r, nodes, 0);
        if (nodes.Any(n => n is EX_ComputedJump)) { Console.WriteLine("ABORT ComputedJump"); return; }
        var log = new List<string>();
        foreach (var n in nodes) {
            switch (n) {
                case EX_Jump j: j.CodeOffset = Bump(j.CodeOffset, realIns, delta, "Jump", log); break;
                case EX_JumpIfNot j: j.CodeOffset = Bump(j.CodeOffset, realIns, delta, "JumpIfNot", log); break;
                case EX_Skip s: s.CodeOffset = Bump(s.CodeOffset, realIns, delta, "Skip", log); break;
                case EX_PushExecutionFlow p: p.PushingAddress = Bump(p.PushingAddress, realIns, delta, "Push", log); break;
                case EX_SkipOffsetConst k: k.Value = Bump(k.Value, realIns, delta, "SkipOff", log); break;
                case EX_SwitchValue sw:
                    sw.EndGotoOffset = Bump(sw.EndGotoOffset, realIns, delta, "Sw.End", log);
                    for (int c=0;c<sw.Cases.Length;c++){ var cs=sw.Cases[c]; cs.NextOffset=Bump(cs.NextOffset,realIns,delta,"Sw.Case",log); sw.Cases[c]=cs; }
                    break;
            }
        }
        foreach (var l in log) Console.WriteLine(l);
        asset.Write(outW);
        Console.WriteLine("WROTE " + outW);
    }
    static void SetMem(object o, string name, object val) {
        var t=o.GetType();
        var p=t.GetProperty(name); if(p!=null && p.CanWrite){ try{p.SetValue(o,val);Console.WriteLine($"  set {name}={val}");return;}catch(Exception e){Console.WriteLine($"  {name} prop set failed: {e.Message}");} }
        var f=t.GetField(name); if(f!=null){ try{f.SetValue(o,val);Console.WriteLine($"  set {name}={val} (field)");return;}catch(Exception e){Console.WriteLine($"  {name} field set failed: {e.Message}");} }
        Console.WriteLine($"  (no member {name})");
    }
    static void Clone(string inUa, string usmapP, string outUa, string oldSelf, string newSelf, string oldCls, string newCls, string[] extra) {
        // Re-serialize an exemplar into a NEW unique identity (new PackageGuid+Source) + renamed class/package.
        // `extra` = flat old,new rename pairs for mesh path, mesh name, thumbnail, etc. (all by exact name-map match, any length).
        var asset = new UAsset(inUa, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        var names = asset.GetNameMapIndexList();
        void ren(string o, string n){ if(o==n||o=="-"||n=="-")return; for(int i=0;i<names.Count;i++) if(names[i].Value==o){asset.SetNameReference(i,new FString(n));Console.WriteLine($"  name {o} -> {n}");return;} Console.WriteLine($"  (name not found: {o})"); }
        ren(oldSelf, newSelf);
        ren(oldCls, newCls);
        ren("Default__"+oldCls, "Default__"+newCls);
        for (int i=0;i+1<extra.Length;i+=2) ren(extra[i], extra[i+1]);
        var g = Guid.NewGuid();
        SetMem(asset, "PackageGuid", g);
        SetMem(asset, "PackageSource", (uint)(g.GetHashCode() & 0x7fffffff));
        SetMem(asset, "FolderName", new FString(newSelf));
        asset.Write(outUa);
        Console.WriteLine("WROTE " + outUa);
    }
    static void BcSize(string w, string usmapP, string fn) {
        var asset = new UAsset(w, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        var fe = (FunctionExport)asset.Exports.First(e => e is FunctionExport f && f.ObjectName.ToString()==fn);
        Console.WriteLine($"my-Serialize fn bytecode len = {Serialize(asset, fe.ScriptBytecode).Length}");
        foreach (var m in fe.GetType().GetFields()) if (m.Name.ToLower().Contains("size")||m.Name.ToLower().Contains("bytecode")) Console.WriteLine($"  field {m.Name} = {m.GetValue(fe)}");
    }
    static void Noop(string w, string usmapP, string outW) {
        var asset = new UAsset(w, EngineVersion.VER_UE5_4, new Usmap(usmapP));
        asset.Write(outW);
        Console.WriteLine("noop-write done");
    }
    static void Main(string[] a) {
        if (a[0]=="cmp") Cmp(a[1],a[2],a[3],a[4]);
        else if (a[0]=="verify") Verify(a[1],a[2],a[3],a[4]);
        else if (a[0]=="deps") Deps(a[1],a[2],a[3]);
        else if (a[0]=="noop") Noop(a[1],a[2],a[3]);
        else if (a[0]=="retarget") Retarget(a[1],a[2],a[3],a[4],a[5],a[6]);
        else if (a[0]=="impdump") ImpDump(a[1],a[2],a[3]); else if (a[0]=="edl") Edl(a[1],a[2],a[3],a[4],a[5]);
        else if (a[0]=="inse") InsE(a[1],a[2],a[3],a[4],a[5],a[6]);
        else if (a[0]=="bcsize") BcSize(a[1],a[2],a[3]);
        else if (a[0]=="clone") Clone(a[1],a[2],a[3],a[4],a[5],a[6],a[7],a.Skip(8).ToArray());
        else Relink(a[0],a[1],a[2],a[3],a[4],a[5],a[6]);
    }
}
