"""
nullscan. Report NULL and blank values in every layer and standalone table of
an ArcGIS Pro map.

Run it in the Pro Python window or a notebook (uses the open project), or
standalone against a project file:

    python nullscan.py path\\to\\project.aprx

Notes that change what you see:
  * Output goes through arcpy.AddMessage, which covers the Python window, script
    tools and standalone runs. Adding print() alongside it double-prints every
    line standalone. If output ever fails to appear somewhere, add the print()
    inside report() rather than at the call sites.
  * Cursors honour each layer's definition query and selection set, so a filtered
    layer is only reported on its visible rows. Filtered layers are flagged.
  * Shapefiles cannot store NULL: numbers become 0 and text becomes ' '. Their
    fields report isNullable=False, so the NULL scan finds nothing by design --
    the BLANK column is the real signal there.
"""

import sys

try:
    import arcpy
except ModuleNotFoundError:
    # arcpy ships only with ArcGIS Pro's bundled Python, and cannot be pip
    # installed. Without this, a first run on the wrong interpreter is an
    # opaque ImportError that says nothing about which interpreter to use.
    sys.exit(
        "arcpy was not found. Run this with the Python that ships with ArcGIS Pro:\n"
        r'  "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" '
        "nullscan.py\n"
        r"or the propy.bat in ...\Pro\bin\Python\Scripts\."
    )

# --------------------------------------------------------------------- config

# "active" scans the map you are looking at in Pro, and every map when run
# standalone (activeMap is None outside the application). "all" always scans
# every map in the project.
SCAN_MAPS = "active"

# Count empty and whitespace-only text as BLANK alongside true NULLs. This is
# the only missing-data signal a shapefile has, since DBF cannot store NULL.
REPORT_BLANKS = True

# Field types the row scan never reads. Geometry is handled separately by a SQL
# IS NULL query. Blob and Raster ARE readable and do return None when null, but
# reading them materialises the whole binary payload per row, which can mean
# megabytes of attachments fetched just to test emptiness, so they are skipped
# on cost, and their nulls go unreported.
UNSCANNABLE = {"Geometry", "Raster", "Blob"}

# Width of the field-name column in the report. Geodatabase field names can
# reach 64 characters; widen this if yours wrap.
NAME_WIDTH = 28

# ----------------------------------------------------------------------------


def _prop(obj, name, default=False):
    """arcpy's is* properties raise AttributeError on layer types that don't
    support them (message 89013). Only that case is defaulted, anything else
    should surface rather than silently drop a layer from the report."""
    try:
        return getattr(obj, name)
    except AttributeError:
        return default


def scan(source, label):
    """Count NULLs, blanks and rows for one layer or table. Returns a dict."""
    out = {"label": label, "rows": 0, "nulls": {}, "blanks": {},
           "note": "", "error": ""}

    try:
        fields = arcpy.ListFields(source)
    except Exception:
        # A standalone-table object (arcpy.mp.Table) makes ListFields raise
        # OSError: it treats the object as a dataset NAME and looks it up in the
        # unset workspace. The object's dataSource path resolves fine, and the
        # cursor below still runs on the object, so its definition query and
        # selection are honored. (Layer objects don't hit this; nor do tables
        # whose gdb a sibling layer already opened, which is why it hides in a
        # mixed map and only bites a table whose source stands alone.)
        try:
            fields = arcpy.ListFields(source.dataSource)
        except Exception as exc:                  # e.g. basemap -> ERROR 999999
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
    if not fields:                                # broken source -> [] -> cursor TypeError
        out["error"] = ("data source is broken" if _prop(source, "isBroken")
                        else "no readable fields")
        return out

    try:
        selected = source.getSelectionSet()
    except Exception:
        selected = None
    if _prop(source, "definitionQuery", "") or selected:
        out["note"] = "filtered (definition query and/or selection active)"

    # Text fields are scanned even when non-nullable: a NOT NULL text column
    # cannot hold NULL but very much can hold '' or '   ', and on shapefiles
    # that is the only way missing data can be represented at all.
    names = [f.name for f in fields
             if (f.isNullable or (REPORT_BLANKS and f.type == "String"))
             and f.type not in UNSCANNABLE]
    text = {f.name for f in fields if f.type == "String"} if REPORT_BLANKS else set()

    # Geometry: only SHAPE@ / SHAPE@WKT report NULL as None. The bare field name
    # and SHAPE@XY both return a (None, None) tuple, which is why a plain
    # `value is None` test silently misses every geometryless feature.
    # A SQL IS NULL query avoids materialising geometry at all: measured 106ms
    # vs 3195ms for SHAPE@ over 200k polygons, and verified to return the same
    # count as the cursor, so the speed costs no coverage here.
    shape_field = next((f.name for f in fields
                        if f.type == "Geometry" and f.isNullable), None)
    if shape_field:
        try:
            where = f"{arcpy.AddFieldDelimiters(source, shape_field)} IS NULL"
            with arcpy.da.SearchCursor(source, ["OID@"], where_clause=where) as cur:
                n = sum(1 for _ in cur)
            if n:
                out["nulls"][shape_field] = n
        except Exception:
            out["note"] = (out["note"] + "; " if out["note"] else "") + \
                          "geometry NULLs not checked (source rejected IS NULL)"

    if not names:                                 # nothing scannable; still count rows
        try:
            with arcpy.da.SearchCursor(source, ["OID@"]) as cur:
                out["rows"] = sum(1 for _ in cur)
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    # One pass, counting rather than just flagging: counts cost ~3% more than
    # existence-only and make the report actionable, so there's no reason to
    # keep a separate early-exit code path.
    nulls = dict.fromkeys(names, 0)
    blanks = dict.fromkeys(names, 0)
    try:
        with arcpy.da.SearchCursor(source, names) as cur:
            for row in cur:
                out["rows"] += 1
                for i, value in enumerate(row):
                    if value is None:
                        nulls[names[i]] += 1
                    elif names[i] in text and not value.strip():
                        blanks[names[i]] += 1
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    out["nulls"].update({k: v for k, v in nulls.items() if v})
    out["blanks"] = {k: v for k, v in blanks.items() if v}
    return out


def collect(m):
    """Layers and standalone tables of a map, including inside group layers."""
    items = []
    for lyr in m.listLayers():                    # already recurses into groups
        if _prop(lyr, "isGroupLayer") or _prop(lyr, "isBasemapLayer"):
            continue
        # isBroken is only a hint. It reports False for sources that are in fact
        # unreadable, so broken layers are kept here and scan() decides.
        if not _prop(lyr, "isFeatureLayer") and not _prop(lyr, "isBroken"):
            continue                              # raster, web, annotation, 3D...
        items.append((lyr, lyr.longName))
    for tbl in m.listTables():                    # NOT returned by listLayers()
        items.append((tbl, f"{tbl.name} (table)"))
    return items


def report(line):
    # AddMessage already writes to stdout standalone and to the Pro Python window
    # and script-tool log; adding print() on top just double-prints everything.
    arcpy.AddMessage(line)


def main(aprx_path=None):
    try:
        aprx = arcpy.mp.ArcGISProject(aprx_path or "CURRENT")
    except OSError:
        report('No open project. Pass a .aprx path when running outside '
               'ArcGIS Pro:\n    python nullscan.py path\\to\\project.aprx')
        return 2

    if SCAN_MAPS == "all" or not aprx.activeMap:
        maps = aprx.listMaps()
    else:
        maps = [aprx.activeMap]
    if not maps:
        report("Project contains no maps.")
        return 1

    flagged = 0
    for m in maps:
        report(f"\n{'=' * 64}\nMap: {m.name}\n{'=' * 64}")
        for source, label in collect(m):
            res = scan(source, label)
            if res["error"]:
                report(f"\n{label}\n    ! {res['error']}")
                continue
            head = f"\n{label}  ({res['rows']} rows)"
            if res["note"]:
                head += f"\n    ~ {res['note']}"
            if not res["nulls"] and not res["blanks"]:
                report(head + "\n    clean")
                continue
            flagged += 1
            report(head)
            for name in sorted(set(res["nulls"]) | set(res["blanks"])):
                bits = []
                if res["nulls"].get(name):
                    bits.append(f"{res['nulls'][name]:>7} null")
                if res["blanks"].get(name):
                    bits.append(f"{res['blanks'][name]:>7} blank")
                pct = ""
                if res["rows"]:
                    # NULL and blank are disjoint per cell, so these add up
                    # rather than overlap.
                    affected = res["nulls"].get(name, 0) + res["blanks"].get(name, 0)
                    pct = f"  ({affected / res['rows']:.1%})"
                report(f"    {name:<{NAME_WIDTH}}{'  '.join(bits)}{pct}")

    report(f"\n{flagged} layer(s) or table(s) with NULL or blank values.")
    return 0


if __name__ == "__main__":
    # Only exit on failure. The documented way to run this in the Pro Python
    # window is exec(open(...).read()), where __name__ is already "__main__" --
    # so an unconditional sys.exit() would raise SystemExit into the user's
    # session on a perfectly clean run. Returning normally still exits 0 in a
    # shell, so the documented exit codes are unaffected.
    _code = main(sys.argv[1] if len(sys.argv) > 1 else None)
    if _code:
        sys.exit(_code)
