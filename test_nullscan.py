"""Self-check for nullscan.py. Builds a throwaway fixture GDB + .aprx, then
asserts the scanner's exact output. Run with the ArcGIS Pro python:

    "C:\\Program Files\\ArcGIS\\Pro\\bin\\Python\\envs\\arcgispro-py3\\python.exe" test_nullscan.py
"""

import datetime
import os
import shutil
import tempfile

import arcpy

from nullscan import collect, scan

arcpy.env.overwriteOutput = True
TMP = os.path.join(tempfile.gettempdir(), "nullscan_selftest")
# arcpy cannot create an .aprx from scratch, so the fixture clones one that ships
# with Pro. Located via GetInstallInfo rather than hardcoded: Pro also installs
# per-user under %LOCALAPPDATA%, and not everyone is on C:.
BLANK = os.path.join(arcpy.GetInstallInfo()["InstallDir"],
                     r"Resources\ArcToolBox\Services\routingservices\data\Blank.aprx")
assert os.path.isfile(BLANK), f"Pro template .aprx not found at {BLANK}"


def build():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    gdb = os.path.join(TMP, "t.gdb")
    arcpy.management.CreateFileGDB(TMP, "t.gdb")
    sr = arcpy.SpatialReference(4326)

    fc = arcpy.management.CreateFeatureclass(gdb, "mixed", "POINT", spatial_reference=sr)[0]
    for name, ftype in (("TXT", "TEXT"), ("NUM", "LONG"), ("DT", "DATE"),
                        ("CLEAN", "TEXT"), ("BLANKS", "TEXT")):
        arcpy.management.AddField(fc, name, ftype, field_length=40)
    # NOT NULL text cannot hold NULL but can hold '' -- the case that made the
    # nullable-only field filter miss blanks entirely.
    arcpy.management.AddField(fc, "STRICT", "TEXT", field_length=40,
                              field_is_nullable="NON_NULLABLE")
    arcpy.management.AddGlobalIDs(fc)
    rows = [
        ((0.0, 0.0), "a", 1, datetime.datetime(2020, 1, 1), "ok", "v", "s"),
        ((1.0, 1.0), None, 2, datetime.datetime(2020, 1, 2), "ok", "", "s"),
        ((2.0, 2.0), "c", None, None, "ok", "   ", "  "),
        (None, "d", 4, datetime.datetime(2020, 1, 4), "ok", "w", "s"),   # NULL geometry
    ]
    with arcpy.da.InsertCursor(
            fc, ["SHAPE@XY", "TXT", "NUM", "DT", "CLEAN", "BLANKS", "STRICT"]) as ic:
        for r in rows:
            ic.insertRow(r)

    clean = arcpy.management.CreateFeatureclass(gdb, "clean", "POINT", spatial_reference=sr)[0]
    arcpy.management.AddField(clean, "NAME", "TEXT", field_length=10)
    with arcpy.da.InsertCursor(clean, ["SHAPE@XY", "NAME"]) as ic:
        for i in range(3):
            ic.insertRow(((float(i), float(i)), f"n{i}"))

    empty = arcpy.management.CreateFeatureclass(gdb, "empty", "POINT", spatial_reference=sr)[0]
    arcpy.management.AddField(empty, "NAME", "TEXT", field_length=10)

    tbl = arcpy.management.CreateTable(gdb, "standalone")[0]
    arcpy.management.AddField(tbl, "CODE", "TEXT", field_length=10)
    with arcpy.da.InsertCursor(tbl, ["CODE"]) as ic:
        ic.insertRow(("a",))
        ic.insertRow((None,))

    shp_dir = os.path.join(TMP, "shp")
    os.makedirs(shp_dir)
    shp = arcpy.management.CreateFeatureclass(shp_dir, "pts.shp", "POINT", spatial_reference=sr)[0]
    arcpy.management.AddField(shp, "TXT", "TEXT", field_length=20)
    with arcpy.da.InsertCursor(shp, ["SHAPE@XY", "TXT"]) as ic:
        ic.insertRow(((0.0, 0.0), "a"))
        ic.insertRow(((1.0, 1.0), ""))       # DBF has no NULL; stores a blank

    # A table in its OWN gdb, with no feature layer sharing it. ListFields on
    # the arcpy.mp.Table object then genuinely raises OSError (a table in the
    # main gdb would be masked by a sibling layer priming the workspace), so
    # this is what actually exercises the dataSource fallback in scan().
    lonely = os.path.join(TMP, "lonely.gdb")
    arcpy.management.CreateFileGDB(TMP, "lonely.gdb")
    lt = arcpy.management.CreateTable(lonely, "lonely")[0]
    arcpy.management.AddField(lt, "CODE", "TEXT", field_length=10)
    with arcpy.da.InsertCursor(lt, ["CODE"]) as ic:
        ic.insertRow(("a",))
        ic.insertRow((None,))
    return gdb, shp, lonely


def build_aprx(gdb, shp, lonely):
    aprx_path = os.path.join(TMP, "t.aprx")
    a = arcpy.mp.ArcGISProject(BLANK)
    a.saveACopy(aprx_path)
    del a
    a = arcpy.mp.ArcGISProject(aprx_path)
    m = a.listMaps()[0]
    for ds in ("mixed", "clean", "empty"):
        m.addDataFromPath(os.path.join(gdb, ds))
    m.addDataFromPath(shp)
    m.addDataFromPath(os.path.join(gdb, "standalone"))
    m.addDataFromPath(os.path.join(lonely, "lonely"))
    grp = m.createGroupLayer("Grp")
    lyr = [x for x in m.listLayers() if x.name == "clean"][0]
    m.addLayerToGroup(grp, lyr)
    m.removeLayer(lyr)
    try:
        m.addBasemap("Topographic")
    except Exception:
        pass                                  # offline: basemap coverage just skipped
    a.save()
    return aprx_path, a, m


def main():
    gdb, shp, lonely = build()
    aprx_path, aprx, m = build_aprx(gdb, shp, lonely)
    fails = []

    def check(label, got, want):
        if got == want:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
            fails.append(label)

    print("\n-- scan() on a feature class with mixed nulls")
    r = scan(os.path.join(gdb, "mixed"), "mixed")
    check("row count", r["rows"], 4)
    check("NULL geometry is detected", r["nulls"].get("Shape"), 1)
    check("NULL text", r["nulls"].get("TXT"), 1)
    check("NULL number", r["nulls"].get("NUM"), 1)
    check("NULL date", r["nulls"].get("DT"), 1)
    check("non-null field absent", "CLEAN" in r["nulls"], False)
    check("'' and '   ' counted as blank", r["blanks"].get("BLANKS"), 2)
    check("blanks are not counted as nulls", "BLANKS" in r["nulls"], False)
    check("blanks found in NON_NULLABLE text too", r["blanks"].get("STRICT"), 1)
    check("OID excluded (isNullable=False)", "OBJECTID" in r["nulls"], False)
    check("GlobalID excluded (isNullable=False)", "GlobalID" in r["nulls"], False)
    check("no error", r["error"], "")

    print("\n-- scan() on clean / empty / table")
    r = scan(os.path.join(gdb, "clean"), "clean")
    check("clean has no nulls", (r["nulls"], r["blanks"]), ({}, {}))
    r = scan(os.path.join(gdb, "empty"), "empty")
    check("empty table -> 0 rows, no error", (r["rows"], r["error"]), (0, ""))
    r = scan(os.path.join(gdb, "standalone"), "standalone")
    check("standalone table NULL", r["nulls"].get("CODE"), 1)

    print("\n-- scan() on a shapefile (DBF cannot store NULL)")
    r = scan(shp, "pts")
    check("shapefile reports no NULLs", r["nulls"], {})
    check("shapefile blank IS caught", r["blanks"].get("TXT"), 1)

    print("\n-- collect() over the map")
    items = collect(m)
    names = [lbl for _, lbl in items]
    check("group sublayer included w/ longName", "Grp\\clean" in names, True)
    check("group layer itself excluded", "Grp" in names, False)
    check("standalone table included", "standalone (table)" in names, True)
    check("basemaps excluded", [n for n in names if "Topographic" in n or "Hillshade" in n], [])

    # Scan the Table OBJECT collect() actually yields, not a gdb path. ListFields
    # raises OSError on that object for a table in its own gdb; scanning by path
    # (as the checks above do) hid that entirely. This is the regression guard.
    src_lonely = next(s for s, lbl in items if lbl == "lonely (table)")
    r = scan(src_lonely, "lonely (table)")
    check("standalone table scanned via collect() object, no error", r["error"], "")
    check("standalone table NULL found through the object path", r["nulls"].get("CODE"), 1)

    # Map.listTables() was verified to reach tables nested inside a group layer,
    # so collect() needs no group recursion -- pinned here so that stays true.
    tbl = [t for t in m.listTables() if t.name == "standalone"][0]
    group = [l for l in m.listLayers() if l.isGroupLayer][0]
    m.addTableToGroup(group, tbl)
    m.removeTable(tbl)
    aprx.save()
    check("grouped table still collected",
          "standalone (table)" in [lbl for _, lbl in collect(m)], True)

    print("\n-- broken data source does not crash")
    # Repoint a layer at a workspace that does not exist. Deleting the source
    # instead would fight arcpy's FGDB lock, which is still held by the open map.
    doomed = [l for l in m.listLayers() if l.name == "empty"][0]
    doomed.updateConnectionProperties(gdb, os.path.join(TMP, "nonexistent.gdb"),
                                      validate=False)
    aprx.save()
    assert doomed.isBroken, "fixture setup failed: layer is not broken"

    labels = [lbl for _, lbl in collect(m)]
    check("broken layer still collected", "empty" in labels, True)
    r = scan(doomed, "empty")
    check("scan() on broken source returns error, no raise", bool(r["error"]), True)
    check("broken source reported as such", r["error"], "data source is broken")

    print("\n-- definition query is honoured and flagged")
    lyr = [l for l in m.listLayers() if l.name == "mixed"][0]
    lyr.definitionQuery = "NUM = 1"
    r = scan(lyr, "mixed-filtered")
    check("def query narrows the scan", r["rows"], 1)
    check("filtering is flagged", "filtered" in r["note"], True)
    check("filtered-out nulls not reported", r["nulls"].get("TXT"), None)

    print("\n-- original script's logic on the same map (regression evidence)")
    def original(layer):
        fields = [f.name for f in arcpy.ListFields(layer)]
        found = set()
        with arcpy.da.SearchCursor(layer, fields) as cur:
            for row in cur:
                for i, v in enumerate(row):
                    if v is None:
                        found.add(fields[i])
        return found
    check("original MISSES null geometry", "Shape" in original(os.path.join(gdb, "mixed")), False)
    # The original guarded with `layer.isFeatureLayer or layer.isTable`. Asserting
    # the missing property directly rather than replaying that loop keeps this
    # check meaningful offline, where no basemap exists to reach the short circuit.
    any_layer = [l for l in m.listLayers() if not l.isGroupLayer][0]
    check("arcpy.mp.Layer has no isTable property", hasattr(any_layer, "isTable"), False)

    del aprx
    print("\n" + "=" * 50)
    if fails:
        print(f"{len(fails)} FAILED: {fails}")
        raise SystemExit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
