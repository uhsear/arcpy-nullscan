"""
nullscan. Report NULL and blank values in every layer and standalone table of
an ArcGIS Pro map, or in one feature class or table.

Run it in the Pro Python window or a notebook (uses the open project), or
standalone against a project file, or against a dataset path:

    python nullscan.py path\\to\\project.aprx
    python nullscan.py path\\to\\data.gdb\\parcels

A dataset scan can also refuse. --max-null-pct PARCEL=10 exits 3 when more
than 10 percent of PARCEL is NULL or blank, which is the shape of a join that
matched no keys: every row arrives, every attribute is empty, and a row-count
gate sees nothing wrong.

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

import argparse
import os
import sys


def _import_arcpy():
    """Import arcpy only when a real dataset is about to be read.

    arcpy ships only with ArcGIS Pro's bundled Python, and cannot be pip
    installed. Without this, a first run on the wrong interpreter is an opaque
    ImportError that says nothing about which interpreter to use. Imported here
    rather than at module level so --self-test runs on any Python."""
    try:
        import arcpy
    except ModuleNotFoundError:
        sys.exit(
            "arcpy was not found. Run this with the Python that ships with ArcGIS Pro:\n"
            r'  "C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" '
            "nullscan.py\n"
            r"or the propy.bat in ...\Pro\bin\Python\Scripts\." "\n"
            "Only --self-test runs without arcpy."
        )
    return arcpy


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


# ------------------------------------------------------------- decision core
# Pure. No arcpy, no geodatabase. Everything below takes numbers and strings
# and returns numbers and strings, which is what --self-test exercises.


def is_project_path(path):
    """True for an ArcGIS Pro project, False for a dataset path. A project is
    scanned map by map; anything else is read as one feature class or table."""
    return bool(path) and path.lower().endswith(".aprx")


def parse_limits(pairs):
    """Turn ["PARCEL=10", "NAME=2.5"] into {"PARCEL": 0.10, "NAME": 0.025}.

    Percent in, fraction out, because the scan counts rows and the comparison
    is against a fraction. Raises ValueError on anything it cannot read: a
    limit the operator got wrong must stop the run, not silently not apply."""
    limits = {}
    for pair in pairs or ():
        name, sep, raw = pair.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ValueError("--max-null-pct wants FIELD=PCT, got %r" % pair)
        try:
            pct = float(raw)
        except ValueError:
            raise ValueError("--max-null-pct %s: %r is not a number" % (name, raw))
        if not 0.0 <= pct <= 100.0:
            raise ValueError("--max-null-pct %s: %s is not a percentage" % (name, raw))
        if name in limits:
            raise ValueError("--max-null-pct names %s twice" % name)
        limits[name] = pct / 100.0
    return limits


def null_fraction(affected, rows):
    """Share of rows where a field is NULL or blank. None when there are no
    rows, because 0 of 0 is not 0 percent clean, it is unknown."""
    if not rows:
        return None
    return affected / rows


def judge(res, limits):
    """Decide whether one scan result passes its --max-null-pct limits. Returns
    the refusal reasons, empty when the scan passes. Pure: res is the dict
    scan() returns, limits is the dict parse_limits() returns.

    A field named in limits that the dataset does not carry REFUSES rather than
    being skipped. The guard this was rebuilt from filtered missing fields out
    of its own check list, so renaming a field disabled the guard that watched
    it and the nightly job went on passing. A limit that cannot be evaluated is
    a control that can never report red, so it refuses instead."""
    if not limits:
        return []
    if res.get("error"):
        return ["REFUSED: %s could not be read (%s), so no limit was checked"
                % (res.get("label") or "the source", res["error"])]

    present = set(res.get("fields") or ())
    unscanned = set(res.get("unscanned") or ())
    rows = res.get("rows", 0)
    reasons = []
    for name in sorted(limits):
        if name not in present:
            reasons.append(
                "REFUSED: field %s is not in this dataset, so its limit could "
                "not be checked. Fields present: %s"
                % (name, ", ".join(sorted(present)) or "none"))
            continue
        if name in unscanned:
            reasons.append(
                "REFUSED: field %s holds a type nullscan never reads, so its "
                "limit could never report red" % name)
            continue
        fraction = null_fraction(
            res["nulls"].get(name, 0) + res["blanks"].get(name, 0), rows)
        if fraction is None:
            reasons.append(
                "REFUSED: the dataset holds no rows, so the limit on %s is "
                "undefined" % name)
            continue
        if fraction > limits[name]:
            reasons.append(
                "REFUSED: %s is %.1f%% NULL or blank (%d of %d rows), limit "
                "is %.1f%%"
                % (name, fraction * 100,
                   res["nulls"].get(name, 0) + res["blanks"].get(name, 0),
                   rows, limits[name] * 100))
    return reasons


def render(res):
    """The report lines for one scan result. Pure, so the layout is asserted
    without a geodatabase."""
    if res["error"]:
        return ["\n%s\n    ! %s" % (res["label"], res["error"])]
    lines = ["\n%s  (%d rows)" % (res["label"], res["rows"])]
    if res["note"]:
        lines.append("    ~ %s" % res["note"])
    if not res["nulls"] and not res["blanks"]:
        lines.append("    clean")
        return lines
    for name in sorted(set(res["nulls"]) | set(res["blanks"])):
        bits = []
        if res["nulls"].get(name):
            bits.append("%7d null" % res["nulls"][name])
        if res["blanks"].get(name):
            bits.append("%7d blank" % res["blanks"][name])
        pct = ""
        if res["rows"]:
            # NULL and blank are disjoint per cell, so these add up rather
            # than overlap.
            affected = res["nulls"].get(name, 0) + res["blanks"].get(name, 0)
            pct = "  (%.1f%%)" % (affected / res["rows"] * 100)
        lines.append("    %-*s%s%s" % (NAME_WIDTH, name, "  ".join(bits), pct))
    return lines


# ------------------------------------------------------------------ arcpy i/o


def scan(source, label):
    """Count NULLs, blanks and rows for one layer or table. Returns a dict."""
    arcpy = _import_arcpy()
    out = {"label": label, "rows": 0, "nulls": {}, "blanks": {},
           "fields": [], "unscanned": [], "note": "", "error": ""}

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

    # Every field name, scanned or not, so judge() can tell "this field has no
    # nulls" from "this field is not here" -- a --max-null-pct on a renamed
    # field must refuse, not quietly pass.
    out["fields"] = [f.name for f in fields]

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
    geom_checked = False
    if shape_field:
        try:
            where = f"{arcpy.AddFieldDelimiters(source, shape_field)} IS NULL"
            with arcpy.da.SearchCursor(source, ["OID@"], where_clause=where) as cur:
                n = sum(1 for _ in cur)
            if n:
                out["nulls"][shape_field] = n
            geom_checked = True
        except Exception:
            out["note"] = (out["note"] + "; " if out["note"] else "") + \
                          "geometry NULLs not checked (source rejected IS NULL)"

    # Fields whose nulls this scan can never see: Blob and Raster are skipped on
    # cost, and geometry is skipped when the IS NULL query was rejected. A
    # --max-null-pct on one of these would report clean forever.
    checked = set(names) | ({shape_field} if geom_checked else set())
    out["unscanned"] = [f.name for f in fields
                        if f.type in UNSCANNABLE and f.name not in checked]

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
    _import_arcpy().AddMessage(line)


def scan_project(aprx_path):
    """Report every layer and table of a Pro project. Returns an exit code."""
    arcpy = _import_arcpy()
    try:
        aprx = arcpy.mp.ArcGISProject(aprx_path or "CURRENT")
    except OSError:
        report('No open project. Pass a .aprx path, or a feature class or '
               'table path, when running outside ArcGIS Pro:\n'
               '    python nullscan.py path\\to\\project.aprx\n'
               '    python nullscan.py path\\to\\data.gdb\\parcels')
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
        report("\n%s\nMap: %s\n%s" % ("=" * 64, m.name, "=" * 64))
        for source, label in collect(m):
            res = scan(source, label)
            if res["nulls"] or res["blanks"]:
                flagged += 1
            for line in render(res):
                report(line)

    report("\n%d layer(s) or table(s) with NULL or blank values." % flagged)
    return 0


def scan_dataset(path, limits):
    """Report one feature class or table, and refuse when a --max-null-pct
    limit is exceeded. Returns an exit code."""
    arcpy = _import_arcpy()
    if not arcpy.Exists(path):
        report("Not a project, feature class or table: %s" % path)
        return 2

    res = scan(path, os.path.basename(path.rstrip("\\/")) or path)
    for line in render(res):
        report(line)

    reasons = judge(res, limits)
    if not limits:
        return 0
    report("")
    if reasons:
        for reason in reasons:
            report(reason)
        return 3
    report("VERDICT: within every --max-null-pct limit.")
    return 0


# ----------------------------------------------------------------- self-test

def self_test():
    """Assertions over the decision core. No arcpy, no geodatabase."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def result(**kw):
        """A scan() result with everything defaulted, so each check names only
        the one thing it is about."""
        out = {"label": "parcels", "rows": 100, "nulls": {}, "blanks": {},
               "fields": ["PARCEL", "OWNER"], "unscanned": [],
               "note": "", "error": ""}
        out.update(kw)
        return out

    print("nullscan self-test: no arcpy, no geodatabase")
    print("-" * 68)

    # ---- which mode a path selects
    check(is_project_path("C:\\gis\\qa.aprx"), "a .aprx path selects project mode")
    check(is_project_path("C:\\gis\\QA.APRX"), "the .aprx test ignores case")
    check(not is_project_path("C:\\gis\\data.gdb\\parcels"),
          "a feature class path selects dataset mode")
    check(not is_project_path("C:\\gis\\pts.shp"),
          "a shapefile path selects dataset mode")
    check(not is_project_path(None), "no path at all is not a project path")

    # ---- reading the limits
    check(parse_limits([]) == {}, "no --max-null-pct means no limits")
    check(parse_limits(None) == {}, "an absent --max-null-pct means no limits")
    check(parse_limits(["PARCEL=10"]) == {"PARCEL": 0.10},
          "percent in, fraction out")
    check(parse_limits(["PARCEL=10", "NAME=2.5"]) == {"PARCEL": 0.10, "NAME": 0.025},
          "two limits are both read")
    check(parse_limits([" PARCEL =10"]) == {"PARCEL": 0.10},
          "the field name is stripped")
    check(parse_limits(["PARCEL=0"]) == {"PARCEL": 0.0}, "0 percent parses")
    check(parse_limits(["PARCEL=100"]) == {"PARCEL": 1.0}, "100 percent parses")
    raises(lambda: parse_limits(["PARCEL"]), "a limit with no = raises")
    raises(lambda: parse_limits(["=10"]), "a limit with no field name raises")
    raises(lambda: parse_limits(["PARCEL=ten"]), "a non-numeric limit raises")
    raises(lambda: parse_limits(["PARCEL=-1"]), "a negative limit raises")
    raises(lambda: parse_limits(["PARCEL=101"]), "a limit over 100 percent raises")
    raises(lambda: parse_limits(["PARCEL=10", "PARCEL=20"]),
           "the same field limited twice raises")

    # ---- the fraction itself
    check(null_fraction(0, 100) == 0.0, "no nulls is 0 percent")
    check(null_fraction(60, 100) == 0.6, "60 of 100 is 60 percent")
    check(null_fraction(1, 3) > 0.333, "the fraction is not rounded to whole percent")
    check(null_fraction(0, 0) is None,
          "the fraction is undefined with no rows, not 0")

    # ---- the verdict
    check(judge(result(nulls={"PARCEL": 99}), {}) == [],
          "with no limits nothing is ever refused, which is the default")
    check(judge(result(nulls={"PARCEL": 5}), {"PARCEL": 0.10}) == [],
          "5 percent null passes a 10 percent limit")
    check(judge(result(nulls={"PARCEL": 10}), {"PARCEL": 0.10}) == [],
          "exactly the limit passes, the limit is inclusive")
    r = judge(result(nulls={"PARCEL": 11}), {"PARCEL": 0.10})
    check(len(r) == 1, "11 percent null is refused at a 10 percent limit")
    check("PARCEL" in r[0] and "11.0%" in r[0],
          "the refusal names the field and the fraction")
    check(judge(result(), {"PARCEL": 0.0}) == [],
          "a limit of 0 passes a field with no nulls at all")
    check(len(judge(result(nulls={"PARCEL": 1}), {"PARCEL": 0.0})) == 1,
          "a limit of 0 refuses a single null")

    # blanks are the only missing-data signal a shapefile has, so a guard that
    # counted nulls alone would report clean on a DBF full of '   '.
    check(len(judge(result(blanks={"PARCEL": 60}), {"PARCEL": 0.10})) == 1,
          "blanks count toward the limit, not just NULLs")
    check(len(judge(result(nulls={"PARCEL": 6}, blanks={"PARCEL": 6}),
                    {"PARCEL": 0.10})) == 1,
          "nulls and blanks add up against the limit")

    # ---- THE PINNED DEFECT: a limit that cannot be evaluated must refuse
    r = judge(result(), {"SITUS": 0.10})
    check(len(r) == 1,
          "a limit on a field the dataset lacks REFUSES  <-- pinned defect")
    check("not in this dataset" in r[0], "the refusal says the field is absent")
    check("PARCEL" in r[0], "the refusal lists the fields that are present")
    r = judge(result(fields=["PARCEL", "PHOTO"], unscanned=["PHOTO"]),
              {"PHOTO": 0.10})
    check(len(r) == 1, "a limit on a Blob or Raster field REFUSES, because that "
                       "scan could never report red")
    r = judge(result(rows=0), {"PARCEL": 0.10})
    check(len(r) == 1, "a limit against an empty dataset refuses")
    check("no rows" in r[0], "the refusal says there were no rows")
    r = judge(result(error="data source is broken"), {"PARCEL": 0.10})
    check(len(r) == 1, "a limit against an unreadable source refuses")
    check("could not be read" in r[0], "the refusal names the read failure")
    check(judge(result(error="data source is broken"), {}) == [],
          "an unreadable source with no limits is reported, not refused")
    check(len(judge(result(nulls={"PARCEL": 50, "OWNER": 50}),
                    {"PARCEL": 0.10, "OWNER": 0.10})) == 2,
          "both failing fields are reported, not just the first")

    # ---- the report layout
    check(render(result())[-1] == "    clean", "a result with nothing missing reads clean")
    check(render(result(error="data source is broken"))[0].endswith(
          "! data source is broken"), "an unreadable source renders with !")
    lines = render(result(nulls={"PARCEL": 3}, blanks={"PARCEL": 3}))
    check("3 null" in lines[-1] and "3 blank" in lines[-1],
          "null and blank counts share one line")
    check("(6.0%)" in lines[-1], "the percentage covers nulls and blanks together")
    check("blank" not in render(result(nulls={"PARCEL": 3}))[-1],
          "a field with no blanks shows only its null count")
    check("null" not in render(result(blanks={"PARCEL": 3}))[-1],
          "a field with no nulls shows only its blank count")
    check("%" not in render(result(rows=0, nulls={"PARCEL": 1}))[-1],
          "no percentage is printed against 0 rows")
    check("~ filtered" in "".join(render(result(note="filtered"))),
          "a filtered layer is flagged in the report")

    # ---- the property guard around arcpy message 89013
    check(_prop(object(), "isBroken") is False,
          "a missing arcpy property falls back to its default")
    check(_prop(object(), "definitionQuery", "") == "",
          "the fallback is the caller's default, not always False")

    # ---- argument handling
    a = _parse([])
    check(a.max_null_pct == [], "--max-null-pct is off by default")
    check(a.path is None, "the path is optional, for the Pro Python window")
    check(_parse(["--self-test"]).self_test, "--self-test parses")
    check(_parse(["x.aprx"]).path == "x.aprx", "a path is read positionally")
    check(_parse(["d.gdb\\p", "--max-null-pct", "A=1",
                  "--max-null-pct", "B=2"]).max_null_pct == ["A=1", "B=2"],
          "--max-null-pct is repeatable")
    check(main(["--max-null-pct", "A=1"]) == 64,
          "--max-null-pct with no dataset path is a usage error")
    check(main(["qa.aprx", "--max-null-pct", "A=1"]) == 64,
          "--max-null-pct against a project is a usage error")
    check(main(["d.gdb\\p", "--max-null-pct", "A"]) == 64,
          "a malformed --max-null-pct is a usage error, not an ignored limit")

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="nullscan.py",
        description="Report NULL and blank values in an ArcGIS Pro map, a "
                    "feature class or a table.",
        epilog="Without --max-null-pct nullscan only reports, and exits 0.")
    ap.add_argument("path", nargs="?",
                    help="a .aprx project, or a feature class or table path. "
                         "Omit it inside ArcGIS Pro to scan the open project.")
    ap.add_argument("--max-null-pct", dest="max_null_pct", action="append",
                    metavar="FIELD=PCT", default=[],
                    help="exit 3 when more than PCT percent of FIELD is NULL "
                         "or blank. Repeatable. Needs a feature class or table "
                         "path, not a project. Off by default.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    try:
        limits = parse_limits(args.max_null_pct)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    if limits and not (args.path and not is_project_path(args.path)):
        # A project holds many layers and most of them will not carry the field.
        # Refusing every one of those is not a verdict anybody can act on, so the
        # limits stay where a single answer means something.
        print("error: --max-null-pct judges one dataset. Pass a feature class "
              "or table path, not a project.", file=sys.stderr)
        return 64

    if args.path and not is_project_path(args.path):
        return scan_dataset(args.path, limits)
    return scan_project(args.path)


if __name__ == "__main__":
    # Only exit on failure. The documented way to run this in the Pro Python
    # window is exec(open(...).read()), where __name__ is already "__main__" --
    # so an unconditional sys.exit() would raise SystemExit into the user's
    # session on a perfectly clean run. Returning normally still exits 0 in a
    # shell, so the documented exit codes are unaffected.
    _code = main()
    if _code:
        sys.exit(_code)
