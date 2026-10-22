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

The placeholder tier looks for values that are present but mean "missing":

    python nullscan.py data.gdb\\parcels --placeholder "*=99999,-9999,N/A"
    python nullscan.py data.gdb\\parcels --placeholder STATUS=99999 --check-domains

--placeholder FIELD=TOKEN[,TOKEN] declares the tokens ("*" means every field).
A value that is itself a code in the field's coded value domain is a real value
and is never flagged, so a domain that deliberately holds 99999 stays clean.
--check-domains also flags near-miss codes (trailing space, other case,
dropped leading zeros) and values outside the domain. --max-placeholder-pct
FIELD=PCT refuses on them the way --max-null-pct refuses on NULLs.

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

# How many distinct example values the report prints per field for the
# placeholder tier. Counts are always complete, only the examples are capped.
PLACEHOLDER_SAMPLES = 3

# Field types the placeholder tier never reads. An OID or GlobalID is unique by
# construction, so 99999 in one is an identifier, not a placeholder.
NO_PLACEHOLDER = {"OID", "GlobalID"}

# The three placeholder-tier counts a scan result carries, beside nulls/blanks.
PLACEHOLDER_KINDS = ("placeholder", "nearmiss", "offdomain")

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


def parse_limits(pairs, flag="--max-null-pct"):
    """Turn ["PARCEL=10", "NAME=2.5"] into {"PARCEL": 0.10, "NAME": 0.025}.

    Percent in, fraction out, because the scan counts rows and the comparison
    is against a fraction. Raises ValueError on anything it cannot read: a
    limit the operator got wrong must stop the run, not silently not apply.
    flag is only the name the error messages quote."""
    limits = {}
    for pair in pairs or ():
        name, sep, raw = pair.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ValueError("%s wants FIELD=PCT, got %r" % (flag, pair))
        try:
            pct = float(raw)
        except ValueError:
            raise ValueError("%s %s: %r is not a number" % (flag, name, raw))
        if not 0.0 <= pct <= 100.0:
            raise ValueError("%s %s: %s is not a percentage" % (flag, name, raw))
        if name in limits:
            raise ValueError("%s names %s twice" % (flag, name))
        limits[name] = pct / 100.0
    return limits


def parse_tokens(specs):
    """Turn ["STATUS=99999,N/A", "*=-9999"] into
    {"status": ["99999", "N/A"], "*": ["-9999"]}.

    Keys are lowercased because geodatabase field names are matched without
    regard to case here: a declaration that silently named no field would be a
    check that can never report red. "*" means every field. Raises ValueError
    on anything it cannot read, for the same reason parse_limits does."""
    out = {}
    for spec in specs or ():
        name, sep, raw = spec.partition("=")
        name = name.strip()
        if not sep or not name:
            raise ValueError("--placeholder wants FIELD=TOKEN[,TOKEN], got %r" % spec)
        tokens = [t.strip() for t in raw.split(",")]
        if not all(tokens):
            raise ValueError("--placeholder %s: an empty token in %r" % (name, raw))
        if len(set(t.casefold() for t in tokens)) != len(tokens):
            raise ValueError("--placeholder %s: a token is listed twice" % name)
        if name.lower() in out:
            raise ValueError("--placeholder names %s twice" % name)
        out[name.lower()] = tokens
    return out


def unknown_declared(tokens, fields):
    """Lowercased field names that tokens declares but the dataset lacks. "*"
    never counts. A declaration that names no field would otherwise pass as a
    clean scan."""
    have = set(f.lower() for f in fields)
    return sorted(k for k in tokens if k != "*" and k not in have)


def _value_text(value):
    """A date value as the text a person would declare it with: 1900-01-01 for
    midnight, the full timestamp otherwise. Anything else is returned as is."""
    if hasattr(value, "isoformat"):
        if hasattr(value, "date"):                # datetime, not date or time
            if not (value.hour or value.minute or value.second or value.microsecond):
                return value.date().isoformat()
            return value.isoformat(" ")
        return value.isoformat()
    return value


def match_token(value, tokens):
    """The declared token that value equals, or None.

    Text compares stripped and without case, so "n/a " matches N/A. A number
    matches a token that parses to the same number, so 99999 and 99999.0 both
    match "99999" while "99999" typed into a text field matches too. Booleans
    and anything else never match."""
    value = _value_text(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        for token in tokens:
            if token.casefold() == text:
                return token
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        for token in tokens:
            try:
                if float(token) == value:
                    return token
            except ValueError:
                pass
    return None


def domain_key(value):
    """What a code looks like once spacing, case and leading zeros are ignored.
    Text only: a number has no spelling to get wrong, so it returns None."""
    if not isinstance(value, str):
        return None
    key = " ".join(value.split()).casefold()
    if key.isdigit():
        key = key.lstrip("0") or "0"
    return key


def build_domain(codes):
    """(exact, nearby) for one coded value domain: the set of codes, and a map
    from domain_key to the first code with that key."""
    nearby = {}
    for code in codes:
        key = domain_key(code)
        if key is not None:
            nearby.setdefault(key, code)
    return set(codes), nearby


def classify_value(value, tokens, domain, check_domains):
    """Decide one present value. Returns (kind, example), kind one of "" (a
    real value), "placeholder", "nearmiss", "offdomain". Pure.

    Order matters. A value that is exactly a code of the field's coded domain
    is real and is never flagged, even when it equals a declared token: that is
    the coded-domain exclusion, and it keeps a domain that deliberately holds
    99999 clean. Then a near-miss of a code (so "99999 " beside a 99999 code is
    a near-miss, not a placeholder). Then a declared token. A value outside the
    domain that is not a token is off-domain, and only --check-domains looks."""
    if domain is not None:
        exact, nearby = domain
        if value in exact:
            return "", ""
        if check_domains:
            code = nearby.get(domain_key(value))
            if code is not None:
                return "nearmiss", "near-miss %s ~ %s" % (ascii(value), ascii(code))
    token = match_token(value, tokens)
    if token is not None:
        return "placeholder", "placeholder %s" % token
    if domain is not None and check_domains:
        return "offdomain", "off-domain %s" % ascii(value)
    return "", ""


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


def _ci(counts):
    """A count dict keyed by lowercased field name, so a limit typed in another
    case still finds its field."""
    return dict((k.lower(), v) for k, v in (counts or {}).items())


def judge_placeholders(res, limits):
    """Decide whether one scan result passes its --max-placeholder-pct limits.
    Same contract as judge(): the refusal reasons, empty when the scan passes.

    A field counts as affected when it holds a declared token, a near-miss of a
    domain code, or an off-domain value. A limit on a field the scan never
    examined REFUSES: no token was declared for it and no coded domain was read,
    so the limit could never report red."""
    if not limits:
        return []
    if res.get("error"):
        return ["REFUSED: %s could not be read (%s), so no limit was checked"
                % (res.get("label") or "the source", res["error"])]

    present = set(f.lower() for f in res.get("fields") or ())
    examined = set(f.lower() for f in res.get("examined") or ())
    kinds = [_ci(res.get(k)) for k in PLACEHOLDER_KINDS]
    rows = res.get("rows", 0)
    reasons = []
    for name in sorted(limits):
        key = name.lower()
        if key not in present:
            reasons.append(
                "REFUSED: field %s is not in this dataset, so its placeholder "
                "limit could not be checked" % name)
            continue
        if key not in examined:
            reasons.append(
                "REFUSED: field %s was never examined for placeholders (no "
                "--placeholder token for it and no coded domain read), so its "
                "limit could never report red" % name)
            continue
        affected = sum(k.get(key, 0) for k in kinds)
        fraction = null_fraction(affected, rows)
        if fraction is None:
            reasons.append(
                "REFUSED: the dataset holds no rows, so the placeholder limit "
                "on %s is undefined" % name)
            continue
        if fraction > limits[name]:
            reasons.append(
                "REFUSED: %s is %.1f%% placeholder, near-miss or off-domain "
                "(%d of %d rows), limit is %.1f%%"
                % (name, fraction * 100, affected, rows, limits[name] * 100))
    return reasons


def render(res):
    """The report lines for one scan result. Pure, so the layout is asserted
    without a geodatabase."""
    if res["error"]:
        return ["\n%s\n    ! %s" % (res["label"], res["error"])]
    lines = ["\n%s  (%d rows)" % (res["label"], res["rows"])]
    if res["note"]:
        lines.append("    ~ %s" % res["note"])
    # res.get: a result built before the placeholder tier has none of these.
    counted = [("null", res["nulls"]), ("blank", res["blanks"]),
               ("placeholder", res.get("placeholder") or {}),
               ("near-miss", res.get("nearmiss") or {}),
               ("off-domain", res.get("offdomain") or {})]
    names = set()
    for _, counts in counted:
        names |= set(counts)
    if not names:
        lines.append("    clean")
        return lines
    for name in sorted(names):
        bits = ["%7d %s" % (counts[name], label)
                for label, counts in counted if counts.get(name)]
        pct = ""
        if res["rows"]:
            # Every category is disjoint per cell (a value is judged once), so
            # these add up rather than overlap.
            affected = sum(counts.get(name, 0) for _, counts in counted)
            pct = "  (%.1f%%)" % (affected / res["rows"] * 100)
        lines.append("    %-*s%s%s" % (NAME_WIDTH, name, "  ".join(bits), pct))
        examples = (res.get("examples") or {}).get(name)
        if examples:
            lines.append("      e.g. %s" % "; ".join(examples))
    return lines


# ------------------------------------------------------------------ arcpy i/o


def _read_domains(arcpy, path, fields):
    """Coded value domains of the fields that carry one: {field: [codes]}.
    Returns (domains, note). Only CodedValue domains count, a Range domain
    has no codes to compare with.

    The workspace comes from Describe(path).workspace.catalogPath, which
    reaches the geodatabase even from a feature class inside a feature
    dataset. A source with no domain-bearing field is never asked, so a
    shapefile or a plain table costs nothing and cannot fail here. A failure
    is reported in the note, never swallowed: a domain check that silently did
    not run would read as a clean one."""
    named = [(f.name, f.domain) for f in fields if getattr(f, "domain", "")]
    if not named:
        return {}, ""
    try:
        workspace = arcpy.Describe(path).workspace.catalogPath
        by_name = dict((d.name, d) for d in arcpy.da.ListDomains(workspace))
    except Exception as exc:
        return {}, "coded domains not read (%s: %s)" % (type(exc).__name__, exc)
    out = {}
    for field_name, domain_name in named:
        domain = by_name.get(domain_name)
        if domain is not None and domain.domainType == "CodedValue":
            out[field_name] = list(domain.codedValues)
    return out, ""


def scan(source, label, arcpy=None, tokens=None, check_domains=False):
    """Count NULLs, blanks and rows for one layer or table, plus the
    placeholder tier when tokens or check_domains ask for it. Returns a dict.

    arcpy is only passed by the self-test, which hands in a stand-in."""
    arcpy = arcpy or _import_arcpy()
    tokens = tokens or {}
    out = {"label": label, "rows": 0, "nulls": {}, "blanks": {},
           "fields": [], "unscanned": [], "note": "", "error": "",
           "placeholder": {}, "nearmiss": {}, "offdomain": {},
           "examples": {}, "examined": []}

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
            out["error"] = "%s: %s" % (type(exc).__name__, exc)
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

    # The placeholder tier. plan maps a field to (its declared tokens, its
    # coded domain or None). A field is in the plan when a token names it, when
    # "*" is declared, or when --check-domains found a coded domain on it. The
    # wildcard reaches NOT NULL numeric fields on purpose: a NOT NULL column
    # cannot hold NULL, so a sentinel is exactly what it holds.
    domains = {}
    if tokens or check_domains:
        domains, dnote = _read_domains(arcpy, _prop(source, "dataSource", source),
                                       fields)
        if dnote:
            out["note"] = (out["note"] + "; " if out["note"] else "") + dnote
    wild = tokens.get("*", [])
    plan = {}
    for f in fields:
        if f.type in UNSCANNABLE or f.type in NO_PLACEHOLDER:
            continue
        named = tokens.get(f.name.lower(), [])
        coded = check_domains and f.name in domains
        if wild or named or coded:
            plan[f.name] = (named + wild,
                            build_domain(domains[f.name]) if f.name in domains
                            else None)

    # Text fields are scanned even when non-nullable: a NOT NULL text column
    # cannot hold NULL but very much can hold '' or '   ', and on shapefiles
    # that is the only way missing data can be represented at all.
    names = [f.name for f in fields
             if (f.isNullable or (REPORT_BLANKS and f.type == "String")
                 or f.name in plan)
             and f.type not in UNSCANNABLE]
    out["examined"] = [n for n in names if n in plan]
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
            where = "%s IS NULL" % arcpy.AddFieldDelimiters(source, shape_field)
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
            out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out

    # One pass, counting rather than just flagging: counts cost ~3% more than
    # existence-only and make the report actionable, so there's no reason to
    # keep a separate early-exit code path.
    nulls = dict.fromkeys(names, 0)
    blanks = dict.fromkeys(names, 0)
    found = dict((kind, dict.fromkeys(names, 0)) for kind in PLACEHOLDER_KINDS)
    try:
        with arcpy.da.SearchCursor(source, names) as cur:
            for row in cur:
                out["rows"] += 1
                for i, value in enumerate(row):
                    name = names[i]
                    if value is None:
                        nulls[name] += 1
                    elif name in text and not value.strip():
                        blanks[name] += 1
                    elif name in plan:
                        kind, example = classify_value(
                            value, plan[name][0], plan[name][1], check_domains)
                        if kind:
                            found[kind][name] += 1
                            shown = out["examples"].setdefault(name, [])
                            if len(shown) < PLACEHOLDER_SAMPLES \
                                    and example not in shown:
                                shown.append(example)
    except Exception as exc:
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
        return out

    out["nulls"].update({k: v for k, v in nulls.items() if v})
    out["blanks"] = {k: v for k, v in blanks.items() if v}
    for kind in PLACEHOLDER_KINDS:
        out[kind] = dict((k, v) for k, v in found[kind].items() if v)
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
        items.append((tbl, "%s (table)" % tbl.name))
    return items


def report(line, arcpy=None):
    # AddMessage already writes to stdout standalone and to the Pro Python window
    # and script-tool log; adding print() on top just double-prints everything.
    (arcpy or _import_arcpy()).AddMessage(line)


def scan_project(aprx_path, tokens=None, check_domains=False, arcpy=None):
    """Report every layer and table of a Pro project. Returns an exit code."""
    arcpy = arcpy or _import_arcpy()
    try:
        aprx = arcpy.mp.ArcGISProject(aprx_path or "CURRENT")
    except OSError:
        report('No open project. Pass a .aprx path, or a feature class or '
               'table path, when running outside ArcGIS Pro:\n'
               '    python nullscan.py path\\to\\project.aprx\n'
               '    python nullscan.py path\\to\\data.gdb\\parcels', arcpy)
        return 2

    if SCAN_MAPS == "all" or not aprx.activeMap:
        maps = aprx.listMaps()
    else:
        maps = [aprx.activeMap]
    if not maps:
        report("Project contains no maps.", arcpy)
        return 1

    flagged = 0
    pflagged = 0
    for m in maps:
        report("\n%s\nMap: %s\n%s" % ("=" * 64, m.name, "=" * 64), arcpy)
        for source, label in collect(m):
            res = scan(source, label, arcpy, tokens, check_domains)
            if res["nulls"] or res["blanks"]:
                flagged += 1
            if any(res[kind] for kind in PLACEHOLDER_KINDS):
                pflagged += 1
            for line in render(res):
                report(line, arcpy)

    report("\n%d layer(s) or table(s) with NULL or blank values." % flagged, arcpy)
    if tokens or check_domains:
        report("%d layer(s) or table(s) with placeholder, near-miss or "
               "off-domain values." % pflagged, arcpy)
    return 0


def scan_dataset(path, limits, plimits=None, tokens=None, check_domains=False,
                 arcpy=None):
    """Report one feature class or table, and refuse when a --max-null-pct or
    --max-placeholder-pct limit is exceeded. Returns an exit code."""
    arcpy = arcpy or _import_arcpy()
    if not arcpy.Exists(path):
        report("Not a project, feature class or table: %s" % path, arcpy)
        return 2

    res = scan(path, os.path.basename(path.rstrip("\\/")) or path, arcpy,
               tokens, check_domains)
    for line in render(res):
        report(line, arcpy)
    for name in unknown_declared(tokens or {}, res["fields"]):
        report("    ~ --placeholder names %s, which this dataset does not "
               "have" % name, arcpy)

    reasons = judge(res, limits) + judge_placeholders(res, plimits)
    if not limits and not plimits:
        return 0
    report("", arcpy)
    if reasons:
        for reason in reasons:
            report(reason, arcpy)
        return 3
    if limits:
        report("VERDICT: within every --max-null-pct limit.", arcpy)
    if plimits:
        report("VERDICT: within every --max-placeholder-pct limit.", arcpy)
    return 0


# ----------------------------------------------------------------- self-test

class _Field(object):
    """One arcpy.Field, with only what nullscan reads."""

    def __init__(self, name, type="String", nullable=True, domain=""):
        self.name = name
        self.type = type
        self.isNullable = nullable
        self.domain = domain


class _Domain(object):
    """One arcpy.da.Domain. codedValues is {code: description}."""

    def __init__(self, name, kind="CodedValue", codes=()):
        self.name = name
        self.domainType = kind
        self.codedValues = dict((c, "desc") for c in codes)


class _Data(object):
    """One fake dataset. rows hold one value per field, in field order."""

    def __init__(self, fields, rows=(), geom_nulls=0, where_error=False,
                 cursor_error=None, count_error=None, describe_error=None):
        self.fields = list(fields)
        self.rows = list(rows)
        self.geom_nulls = geom_nulls
        self.where_error = where_error
        self.cursor_error = cursor_error
        self.count_error = count_error
        self.describe_error = describe_error


class _Src(object):
    """A layer or table object. Only the attributes given exist, so the
    property guard around arcpy message 89013 is exercised as well."""

    def __init__(self, path, name=None, selected=None, **props):
        self.dataSource = path
        self.longName = name or path
        self.name = name or path
        self._selected = selected
        for key, value in props.items():
            setattr(self, key, value)

    def getSelectionSet(self):
        return self._selected


class _Map(object):
    def __init__(self, name, layers=(), tables=()):
        self.name = name
        self._layers = list(layers)
        self._tables = list(tables)

    def listLayers(self):
        return self._layers

    def listTables(self):
        return self._tables


class _Aprx(object):
    def __init__(self, maps, active=None):
        self._maps = list(maps)
        self.activeMap = active

    def listMaps(self):
        return self._maps


class _Cursor(object):
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return iter(self.rows)

    def __exit__(self, *exc_info):
        return False


class _FakeArcpy(object):
    """The smallest arcpy nullscan can be driven against. It is its own .da and
    .mp. calls records the arcpy functions that ran, so a test can prove that a
    default run never reads a domain."""

    def __init__(self, data=None, domains=None, projects=None,
                 listdomains_error=None):
        self.data = dict(data or {})
        self.domains = dict(domains or {})
        self.projects = dict(projects or {})
        self.listdomains_error = listdomains_error
        self.messages = []
        self.calls = []
        self.da = self
        self.mp = self

    def AddMessage(self, line):
        self.messages.append(line)

    def Exists(self, path):
        return path in self.data

    def AddFieldDelimiters(self, source, name):
        return '"%s"' % name

    def ArcGISProject(self, path):
        if path not in self.projects:
            raise OSError("no such project")
        return self.projects[path]

    def ListFields(self, source):
        if getattr(source, "strict", False):
            raise OSError("an object is not a dataset name")
        path = getattr(source, "dataSource", source)
        if path not in self.data:
            raise RuntimeError("ERROR 999999")
        return self.data[path].fields

    def Describe(self, path):
        self.calls.append("Describe")
        data = self.data[path]
        if data.describe_error:
            raise RuntimeError(data.describe_error)
        workspace = type("_Workspace", (object,), {"catalogPath": "t.gdb"})()
        return type("_Described", (object,), {"workspace": workspace})()

    def ListDomains(self, workspace):
        self.calls.append("ListDomains")
        if self.listdomains_error:
            raise RuntimeError(self.listdomains_error)
        return self.domains.get(workspace, [])

    def SearchCursor(self, source, names, where_clause=None):
        data = self.data[getattr(source, "dataSource", source)]
        if where_clause:
            if data.where_error:
                raise RuntimeError("the source rejected the query")
            return _Cursor([(i,) for i in range(data.geom_nulls)])
        if names == ["OID@"]:
            if data.count_error:
                raise RuntimeError(data.count_error)
            return _Cursor([(i,) for i in range(len(data.rows))])
        if data.cursor_error:
            raise RuntimeError(data.cursor_error)
        columns = [[f.name for f in data.fields].index(n) for n in names]
        return _Cursor([tuple(row[i] for i in columns) for row in data.rows])


def _harness(echo=print):
    """The self-test's check(), raises(), exits() and finish(). A function so
    that the self-test can run a second harness on purpose-failing assertions
    and prove that a failure is reported: a harness that cannot report red is a
    control that can never fail."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            echo("PASS  %s" % label)
        else:
            failed.append(label)
            echo("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def exits(fn, label):
        """Passes when fn raises SystemExit. Returns its code, else None."""
        try:
            fn()
        except SystemExit as exc:
            check(True, label)
            return exc.code
        check(False, "%s (no exit)" % label)
        return None

    def finish():
        echo("-" * 68)
        total = passed[0] + len(failed)
        if failed:
            echo("%d assertions, %d failed" % (total, len(failed)))
            for f in failed:
                echo("  FAILED: %s" % f)
            return 1
        echo("%d assertions, 0 failed" % total)
        return 0

    return check, raises, exits, finish


def self_test():
    """Assertions over the decision core and, through a stand-in arcpy, the
    scan itself. No arcpy, no geodatabase."""
    import datetime
    import runpy
    import types
    from unittest import mock
    global SCAN_MAPS
    check, raises, exits, finish = _harness()

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
    exits(lambda: _parse(["d.gdb\\p", "--max-n", "A=1"]),
          "a unique prefix of --max-null-pct is refused  <-- pinned defect")
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

    # ================================================================ placeholder tier

    def presult(**kw):
        """A scan() result that went through the placeholder tier."""
        out = result(fields=["PARCEL", "STATUS", "PHOTO"], examined=["PARCEL", "STATUS"],
                     placeholder={}, nearmiss={}, offdomain={}, examples={})
        out.update(kw)
        return out

    # ---- reading the token declarations
    check(parse_tokens(["STATUS=99999,N/A"]) == {"status": ["99999", "N/A"]},
          "a token declaration is keyed by lowercased field, tokens in order")
    check(parse_tokens(["*=-9999"]) == {"*": ["-9999"]}, "* declares every field")
    check(parse_tokens(None) == {} and parse_tokens([]) == {},
          "no --placeholder means no tokens")
    check(parse_tokens([" a = 1 , 2 "]) == {"a": ["1", "2"]},
          "field and tokens are stripped")
    raises(lambda: parse_tokens(["STATUS"]), "a declaration with no = raises")
    raises(lambda: parse_tokens(["=99999"]), "a declaration with no field raises")
    raises(lambda: parse_tokens(["STATUS="]), "a declaration with no token raises")
    raises(lambda: parse_tokens(["STATUS=1,,2"]), "an empty token in a list raises")
    raises(lambda: parse_tokens(["STATUS=n/a,N/A"]),
           "the same token twice, ignoring case, raises")
    raises(lambda: parse_tokens(["STATUS=1", "status=2"]),
           "the same field declared twice, ignoring case, raises")
    raises(lambda: parse_limits(["X"], "--max-placeholder-pct"),
           "a malformed placeholder limit raises")
    try:
        parse_limits(["X"], "--max-placeholder-pct")
    except ValueError as exc:
        check("--max-placeholder-pct wants" in str(exc),
              "the error quotes the flag it was given")
    check(unknown_declared({"status": ["1"], "*": ["2"], "ghost": ["3"]},
                           ["STATUS", "OWNER"]) == ["ghost"],
          "a declared field the dataset lacks is named, ignoring case, * excepted")

    # ---- matching one value against the declared tokens
    check(match_token(99999, ["99999"]) == "99999", "an int matches a numeric token")
    check(match_token(99999.0, ["99999"]) == "99999",
          "a float 99999.0 matches the token 99999")
    check(match_token(5, ["99999"]) is None, "another number does not match")
    check(match_token(5, ["N/A", "5"]) == "5",
          "a token that is not a number is skipped, the next one still matches")
    check(match_token(True, ["1"]) is None, "a boolean never matches a numeric token")
    check(match_token("99999", ["99999"]) == "99999", "text matches a text token")
    check(match_token(" n/a ", ["N/A"]) == "N/A",
          "text matches without case and without padding")
    check(match_token("N/A", ["99999"]) is None, "other text does not match")
    check(match_token(b"99999", ["99999"]) is None, "bytes never match")
    d = datetime.datetime
    check(match_token(d(1900, 1, 1), ["1900-01-01"]) == "1900-01-01",
          "a midnight timestamp matches the bare date")
    check(match_token(d(1900, 1, 1, 13, 0), ["1900-01-01"]) is None,
          "a timestamp at 13:00 does not match the bare date")
    check(match_token(d(1900, 1, 1, 13, 0), ["1900-01-01 13:00:00"])
          == "1900-01-01 13:00:00", "a timestamp matches its full text")
    check(match_token(d(1900, 1, 1, 0, 0, 0, 5), ["1900-01-01"]) is None,
          "a microsecond past midnight is not midnight")
    check(match_token(datetime.date(1900, 1, 1), ["1900-01-01"]) == "1900-01-01",
          "a date value matches")
    check(match_token(datetime.time(0, 0), ["00:00:00"]) == "00:00:00",
          "a time-only value matches its text")

    # ---- what a near-miss is
    check(domain_key("A ") == "a" and domain_key("  a  b") == "a b",
          "padding, inner spacing and case are ignored")
    check(domain_key("007") == "7" and domain_key("000") == "0",
          "leading zeros are ignored, an all-zero code stays 0")
    check(domain_key(7) is None, "a number has no spelling to get wrong")
    exact, nearby = build_domain(["A", "a", 7, "07"])
    check(exact == set(["A", "a", 7, "07"]), "the exact set holds the codes")
    check(nearby == {"a": "A", "7": "07"},
          "two codes with one key keep the first, a numeric code has no key")

    # ---- classifying one value (THE PINNED DEFECTS live here)
    dom = build_domain(["A", "B", "99999"])
    tok = ["99999", "N/A"]
    check(classify_value("99999", tok, dom, False) == ("", ""),
          "99999 that IS a domain code is a real value  <-- pinned defect")
    check(classify_value("99999", tok, dom, True) == ("", ""),
          "a code that is also a token stays excluded with --check-domains")
    check(classify_value("99999", tok, None, False)[0] == "placeholder",
          "99999 with no domain is a placeholder  <-- pinned defect")
    check(classify_value("99999 ", tok, dom, True)[0] == "nearmiss",
          "99999 with a trailing space beside a 99999 code is a near-miss, "
          "not a placeholder  <-- pinned defect")
    check(classify_value("99999 ", tok, dom, False)[0] == "placeholder",
          "without --check-domains the padded token still matches as text")
    check(classify_value("A ", tok, dom, True)
          == ("nearmiss", "near-miss 'A ' ~ 'A'"),
          "a trailing space on a code is a near-miss, with the code it nearly was")
    check(classify_value("a", tok, dom, True)[0] == "nearmiss",
          "a code in the wrong case is a near-miss")
    check(classify_value("A ", tok, dom, False) == ("", ""),
          "near-misses are not looked for without --check-domains")
    check(classify_value("Z", tok, dom, True) == ("offdomain", "off-domain 'Z'"),
          "a value outside the domain is off-domain")
    check(classify_value("Z", tok, dom, False) == ("", ""),
          "off-domain values are not looked for without --check-domains")
    check(classify_value("N/A", tok, dom, True)[0] == "placeholder",
          "a token outside the domain is a placeholder, not off-domain")
    check(classify_value(5, tok, build_domain([1, 2]), True)[0] == "offdomain",
          "a number outside a numeric domain is off-domain")
    check(classify_value(2, tok, build_domain([1, 2]), True) == ("", ""),
          "a number inside a numeric domain is real")
    check(classify_value("x", [], None, True) == ("", ""),
          "no tokens and no domain flags nothing")
    check(classify_value(chr(233), tok, dom, True) ==
          ("offdomain", "off-domain '\\xe9'"),
          "an example is escaped to ASCII so any console prints it")

    # ---- the verdict on placeholders
    check(judge_placeholders(presult(placeholder={"PARCEL": 99}), {}) == [],
          "with no placeholder limits nothing is ever refused")
    check(judge_placeholders(presult(placeholder={"PARCEL": 10}), {"PARCEL": 0.10})
          == [], "exactly the placeholder limit passes")
    r = judge_placeholders(presult(placeholder={"PARCEL": 8}, nearmiss={"PARCEL": 2},
                                   offdomain={"PARCEL": 1}), {"PARCEL": 0.10})
    check(len(r) == 1 and "11.0%" in r[0] and "11 of 100" in r[0],
          "placeholders, near-misses and off-domain values add up against the limit")
    check(len(judge_placeholders(presult(placeholder={"parcel": 20}),
                                 {"PARCEL": 0.10})) == 1,
          "a limit typed in another case still finds its field")
    r = judge_placeholders(presult(), {"SITUS": 0.10})
    check(len(r) == 1 and "not in this dataset" in r[0],
          "a placeholder limit on a field the dataset lacks REFUSES  <-- pinned defect")
    r = judge_placeholders(presult(), {"PHOTO": 0.10})
    check(len(r) == 1 and "never examined" in r[0],
          "a placeholder limit on a field never examined REFUSES  <-- pinned defect")
    r = judge_placeholders(presult(rows=0), {"PARCEL": 0.10})
    check(len(r) == 1 and "no rows" in r[0],
          "a placeholder limit against an empty dataset refuses")
    r = judge_placeholders(presult(error="data source is broken"), {"PARCEL": 0.10})
    check(len(r) == 1 and "could not be read" in r[0],
          "a placeholder limit against an unreadable source refuses")
    check(judge_placeholders(presult(error="x"), {}) == [],
          "an unreadable source with no placeholder limits is reported, not refused")

    # ---- the report layout
    lines = render(presult(placeholder={"PARCEL": 3}, nearmiss={"PARCEL": 2},
                           offdomain={"PARCEL": 1}, nulls={"PARCEL": 4},
                           examples={"PARCEL": ["placeholder 99999", "off-domain 'Z'"]}))
    check("3 placeholder" in lines[1] and "2 near-miss" in lines[1]
          and "1 off-domain" in lines[1] and "4 null" in lines[1],
          "every category shares the field's line")
    check("(10.0%)" in lines[1], "the percentage covers every category")
    check(lines[2] == "      e.g. placeholder 99999; off-domain 'Z'",
          "the examples follow the field's line")
    check(render(presult())[-1] == "    clean",
          "a placeholder-tier result with nothing found reads clean")

    # ---- the stand-in arcpy. One geodatabase table, ten rows, five of them
    # carrying a value that means missing.
    g = "geom"
    t = d(2020, 1, 1)
    rows = [
        (1, g, "P1", "A", 1, b"x", 100.0, t),
        (2, g, "P2", "B", 2, None, 99999.0, d(1900, 1, 1)),
        (3, g, "UNKNOWN", "A ", 99999, None, 5.0, d(2020, 1, 1, 13, 0)),
        (4, g, "n/a ", "a", 5, None, None, None),
        (5, g, "P5", "99999", -9999, None, 6.0, d(1900, 1, 1)),
        (6, g, "  ", "N/A", 1, None, 7.0, t),
        (7, g, None, "Z", 2, None, 8.0, t),
        (8, g, "P8", None, 2, None, 9.0, t),
        (9, g, "P9", "  ", 1, None, 10.0, t),
        (10, g, "99999", "B", 1, None, 11.0, t),
    ]
    parcel_fields = [
        _Field("OBJECTID", "OID", False), _Field("SHAPE", "Geometry"),
        _Field("PARCEL"), _Field("STATUS", domain="StatusDom"),
        _Field("ZONE", "SmallInteger", False, "ZoneDom"), _Field("PHOTO", "Blob"),
        _Field("SQFT", "Double", True, "SizeRange"),
        _Field("ISSUED", "Date", True, "Ghost")]
    domains = {"t.gdb": [_Domain("StatusDom", codes=["A", "B", "99999"]),
                         _Domain("ZoneDom", codes=[1, 2, 99999]),
                         _Domain("SizeRange", "Range")]}

    def world(**kw):
        return _FakeArcpy(
            {"t.gdb/parcels": _Data(parcel_fields, rows, geom_nulls=2, **kw)},
            domains)

    wild = parse_tokens(["*=99999,-9999,N/A,UNKNOWN", "issued=1900-01-01"])

    # no tier, no domain read: the existing behaviour, byte for byte
    fa = world()
    res = scan("t.gdb/parcels", "parcels", fa)
    check(res["rows"] == 10 and res["nulls"] == {
        "SHAPE": 2, "PARCEL": 1, "STATUS": 1, "SQFT": 1, "ISSUED": 1},
        "a default scan counts NULLs, geometry NULLs included")
    check(res["blanks"] == {"PARCEL": 1, "STATUS": 1}, "a default scan counts blanks")
    check(res["placeholder"] == {} and res["examined"] == [],
          "a default scan examines no field for placeholders")
    check("ListDomains" not in fa.calls and "Describe" not in fa.calls,
          "a default scan never reads a domain")
    check(res["unscanned"] == ["PHOTO"], "a Blob field is reported unscanned")

    # declared tokens, coded-domain exclusion, no --check-domains
    fa = world()
    res = scan("t.gdb/parcels", "parcels", fa, wild)
    check(res["placeholder"] == {"PARCEL": 3, "STATUS": 1, "ZONE": 1, "SQFT": 1,
                                 "ISSUED": 2},
          "declared tokens are counted per field, dates and floats included")
    check(res["nearmiss"] == {} and res["offdomain"] == {},
          "near-misses and off-domain values wait for --check-domains")
    check(res["examined"] == ["PARCEL", "STATUS", "ZONE", "SQFT", "ISSUED"],
          "every field a token reaches is examined, NOT NULL numerics included")
    check("OBJECTID" not in res["examined"] and "SHAPE" not in res["examined"]
          and "PHOTO" not in res["examined"],
          "the OID, geometry and blob fields are never examined")
    check(res["placeholder"]["STATUS"] == 1,
          "the 99999 that is a STATUS domain code is excluded, N/A is flagged")
    check(res["placeholder"]["ZONE"] == 1,
          "the 99999 that is a ZONE domain code is excluded, -9999 is flagged")
    check(res["placeholder"]["SQFT"] == 1,
          "a Range domain gives no codes, so SQFT keeps its 99999.0 flag")
    check(res["nulls"]["PARCEL"] == 1 and res["blanks"]["PARCEL"] == 1,
          "a null and a blank are decided before any token, never double counted")
    check(fa.calls.count("ListDomains") == 1,
          "the domains of a source are listed once")
    check(res["examples"]["PARCEL"] == ["placeholder UNKNOWN", "placeholder N/A",
                                        "placeholder 99999"],
          "examples are the declared tokens that matched, in row order")

    # --check-domains alone
    res = scan("t.gdb/parcels", "parcels", world(), None, True)
    check(res["examined"] == ["STATUS", "ZONE"],
          "--check-domains examines exactly the fields with a coded domain")
    check(res["nearmiss"] == {"STATUS": 2},
          "'A ' and 'a' are near-misses of the A code")
    check(res["offdomain"] == {"STATUS": 2, "ZONE": 2},
          "with no tokens N/A and Z are off-domain, so are ZONE 5 and -9999")
    check(res["placeholder"] == {}, "no tokens means no placeholders")
    check(res["examples"]["STATUS"] == ["near-miss 'A ' ~ 'A'",
                                        "near-miss 'a' ~ 'A'", "off-domain 'N/A'"],
          "examples are capped at PLACEHOLDER_SAMPLES, counts are not")

    # tokens and --check-domains together
    res = scan("t.gdb/parcels", "parcels", world(), wild, True)
    check(res["placeholder"] == {"PARCEL": 3, "STATUS": 1, "ZONE": 1, "SQFT": 1,
                                 "ISSUED": 2}
          and res["nearmiss"] == {"STATUS": 2}
          and res["offdomain"] == {"STATUS": 1, "ZONE": 1},
          "tokens and --check-domains classify each value once")

    # a domain that cannot be read says so, and loses the exclusion
    fa = world(describe_error="ERROR 000732: workspace does not exist")
    res = scan("t.gdb/parcels", "parcels", fa, wild, True)
    check("coded domains not read (RuntimeError: ERROR 000732" in res["note"],
          "a domain read that fails is reported in the note, not swallowed")
    check(res["examined"] == ["PARCEL", "STATUS", "ZONE", "SQFT", "ISSUED"]
          and res["placeholder"]["STATUS"] == 2,
          "with no domains the exclusion is lost, so the loss is visible")
    res = scan(_Src("t.gdb/parcels", definitionQuery="X > 1"), "p", fa, None, True)
    check(res["note"].startswith("filtered") and "coded domains not read" in res["note"],
          "a domain note joins the filtered note")
    check(res["examined"] == [], "with --check-domains alone a failed read examines nothing")
    fa = world()
    fa.listdomains_error = "ERROR 000601"
    res = scan("t.gdb/parcels", "parcels", fa, None, True)
    check("coded domains not read (RuntimeError: ERROR 000601" in res["note"],
          "a ListDomains failure is reported the same way")

    # a source with no domain-bearing field is never asked
    plain = [_Field("PARCEL"), _Field("OID", "OID", False)]
    fa = _FakeArcpy({"x": _Data(plain, [("N/A", 1)])})
    res = scan("x", "x", fa, parse_tokens(["parcel=N/A"]), True)
    check(res["placeholder"] == {"PARCEL": 1} and fa.calls == [],
          "a source with no domain on any field costs no Describe or ListDomains")

    # the example cap and its de-duplication
    cap = [_Field("CODE")]
    fa = _FakeArcpy({"c": _Data(cap, [(v,) for v in "aabbcdee"])})
    res = scan("c", "c", fa, parse_tokens(["code=a,b,c,d,e"]))
    check(res["placeholder"] == {"CODE": 8}, "every placeholder is counted")
    check(res["examples"]["CODE"] == ["placeholder a", "placeholder b",
                                      "placeholder c"],
          "examples are distinct and capped at three")

    # ---- the rest of scan(), for the cases the new tier did not change
    ex = _FakeArcpy({
        "empty": _Data([]),
        "blob": _Data([_Field("PHOTO", "Blob")], [(None,), (None,)]),
        "blobfail": _Data([_Field("PHOTO", "Blob")], count_error="cursor died"),
        "nogeom": _Data([_Field("SHAPE", "Geometry", False), _Field("N")],
                        [(g, None)]),
        "badwhere": _Data([_Field("SHAPE", "Geometry"), _Field("N")],
                          [(g, 1)], where_error=True),
        "cursorfail": _Data([_Field("N")], [(1,)], cursor_error="row read failed"),
        "gnull0": _Data([_Field("SHAPE", "Geometry"), _Field("N")], [(g, 1)]),
    })
    check(scan("empty", "e", ex)["error"] == "no readable fields",
          "a source with no fields reads as no readable fields")
    check(scan(_Src("empty", isBroken=True), "e", ex)["error"]
          == "data source is broken", "a source flagged broken says so")
    check(scan("missing", "m", ex)["error"].startswith("AttributeError"),
          "a path nobody can list is an error, not a crash")
    check(scan(_Src("missing"), "m", ex)["error"].startswith("RuntimeError"),
          "a layer whose source cannot be listed is an error, not a crash")
    check(scan(_Src("blob", strict=True), "b", ex)["rows"] == 2,
          "a table object that ListFields rejects is read through its dataSource")
    r = scan("blob", "b", ex)
    check(r["rows"] == 2 and r["unscanned"] == ["PHOTO"],
          "a table with nothing scannable still counts its rows")
    check(scan("blobfail", "b", ex)["error"] == "RuntimeError: cursor died",
          "a row count that fails is an error")
    r = scan("nogeom", "n", ex)
    check(r["nulls"] == {"N": 1} and r["unscanned"] == ["SHAPE"],
          "a geometry field that is not nullable is not queried")
    r = scan("badwhere", "b", ex)
    check("geometry NULLs not checked" in r["note"] and r["unscanned"] == ["SHAPE"],
          "a rejected IS NULL query is noted and the field is unscanned")
    check(scan("cursorfail", "c", ex)["error"] == "RuntimeError: row read failed",
          "a row read that fails is an error")
    check(scan("gnull0", "g", ex)["nulls"] == {}, "no geometry NULLs adds no entry")
    check(scan(_Src("gnull0", selected=[1]), "g", ex)["note"].startswith("filtered"),
          "a selection set marks the layer filtered")

    # ---- collect() and scan_project()
    lyr = _Src("t.gdb/parcels", "Parcels", isFeatureLayer=True)
    grp = _Src("grp", "Group", isGroupLayer=True)
    base = _Src("base", "Base", isBasemapLayer=True)
    ras = _Src("ras", "Raster", isFeatureLayer=False)
    brk = _Src("gone", "Gone", isFeatureLayer=False, isBroken=True)
    tbl = _Src("blob", "Permits", strict=True)
    m = _Map("QA", [lyr, grp, base, ras, brk], [tbl])
    check([label for _, label in collect(m)] == ["Parcels", "Gone", "Permits (table)"],
          "groups, basemaps and rasters are left out, broken layers and tables stay")

    ex = world()
    ex.data["gone"] = _Data([])
    ex.data["blob"] = _Data([_Field("PHOTO", "Blob")], [(None,)])
    ex.projects["qa.aprx"] = _Aprx([m], active=m)
    check(scan_project("qa.aprx", None, False, ex) == 0, "a project scan exits 0")
    text_out = "\n".join(ex.messages)
    check("Map: QA" in text_out and "Parcels  (10 rows)" in text_out
          and "Permits (table)  (1 rows)" in text_out and "! data source is broken" in text_out,
          "a project scan reports each map, layer, table and unreadable source")
    check("1 layer(s) or table(s) with NULL or blank values." in text_out,
          "the summary counts the layers with NULLs or blanks")
    check("placeholder, near-miss" not in text_out,
          "the placeholder summary line is absent when the tier is off")

    ex.messages[:] = []
    scan_project("qa.aprx", wild, True, ex)
    text_out = "\n".join(ex.messages)
    check("1 layer(s) or table(s) with placeholder, near-miss or off-domain values."
          in text_out, "the placeholder summary line appears when the tier is on")

    ex.messages[:] = []
    ex.projects["all.aprx"] = _Aprx([m, _Map("Other")], active=None)
    scan_project("all.aprx", None, False, ex)
    check(sum(1 for line in ex.messages if line.startswith("\n=")) == 2,
          "with no active map every map is scanned")
    SCAN_MAPS = "all"
    ex.projects["all2.aprx"] = _Aprx([m, _Map("Other")], active=m)
    ex.messages[:] = []
    scan_project("all2.aprx", None, False, ex)
    check(sum(1 for line in ex.messages if line.startswith("\n=")) == 2,
          "the all setting of SCAN_MAPS scans every map even with an active one")
    SCAN_MAPS = "active"
    ex.projects["none.aprx"] = _Aprx([], active=None)
    check(scan_project("none.aprx", None, False, ex) == 1,
          "a project with no maps exits 1")
    ex.messages[:] = []
    check(scan_project("nothing.aprx", None, False, ex) == 2
          and "No open project" in ex.messages[0],
          "no project at all exits 2 and says how to point at data")

    # ---- scan_dataset()
    ex = world()
    ex.data["/"] = ex.data["t.gdb/parcels"]
    check(scan_dataset("t.gdb/missing", {}, arcpy=ex) == 2
          and "Not a project" in ex.messages[-1], "a path that does not exist exits 2")
    ex.messages[:] = []
    check(scan_dataset("t.gdb/parcels", {}, arcpy=ex) == 0
          and "parcels  (10 rows)" in ex.messages[0], "a plain dataset scan exits 0")
    ex.messages[:] = []
    scan_dataset("/", {}, arcpy=ex)
    check("\n/  (10 rows)" in ex.messages, "a bare slash path is labelled by the path")
    ex.messages[:] = []
    check(scan_dataset("t.gdb/parcels", {"PARCEL": 0.5}, arcpy=ex) == 0
          and ex.messages[-1] == "VERDICT: within every --max-null-pct limit.",
          "a null limit that holds says so")
    ex.messages[:] = []
    check(scan_dataset("t.gdb/parcels", {"PARCEL": 0.0}, arcpy=ex) == 3
          and ex.messages[-1].startswith("REFUSED: PARCEL is 20.0%"),
          "a null limit that is exceeded exits 3")
    ex.messages[:] = []
    code = scan_dataset("t.gdb/parcels", {}, {"PARCEL": 0.5}, wild, False, ex)
    check(code == 0 and ex.messages[-1]
          == "VERDICT: within every --max-placeholder-pct limit.",
          "a placeholder limit that holds says so")
    ex.messages[:] = []
    code = scan_dataset("t.gdb/parcels", {"PARCEL": 0.5}, {"PARCEL": 0.1}, wild,
                        False, ex)
    check(code == 3 and ex.messages[-1].startswith(
          "REFUSED: PARCEL is 30.0% placeholder"),
          "a placeholder limit that is exceeded exits 3")
    code = scan_dataset("t.gdb/parcels", {"PARCEL": 0.5}, {"PARCEL": 0.5}, wild,
                        False, ex)
    check(code == 0 and ex.messages[-2:] == [
        "VERDICT: within every --max-null-pct limit.",
        "VERDICT: within every --max-placeholder-pct limit."],
        "both verdict lines print when both kinds of limit hold")
    ex.messages[:] = []
    scan_dataset("t.gdb/parcels", {}, {}, parse_tokens(["ghost=1", "*=1"]), False, ex)
    check("    ~ --placeholder names ghost, which this dataset does not have"
          in ex.messages, "a declared field the dataset lacks is named in the report")

    # ---- reaching arcpy
    fake_module = types.ModuleType("arcpy")
    with mock.patch.dict(sys.modules, {"arcpy": fake_module}):
        check(_import_arcpy() is fake_module, "the arcpy that is installed is used")
        fake_module.AddMessage = lambda line: ex.messages.append("via " + line)
        report("hello")
        check(ex.messages[-1] == "via hello",
              "report() writes through arcpy.AddMessage and nothing else")
    with mock.patch.dict(sys.modules, {"arcpy": None}):
        code = exits(_import_arcpy, "a missing arcpy exits instead of crashing")
        check("arcpy was not found" in str(code) and "ArcGIS Pro" in str(code),
              "the exit names the interpreter to use")

    # ---- the command line
    a = _parse([])
    check(a.placeholder == [] and a.check_domains is False
          and a.max_placeholder_pct == [], "the placeholder tier is off by default")
    for flag in ("--placeh", "--check-d", "--max-placeholder"):
        exits(lambda: _parse(["d.gdb/p", flag, "A=1"]),
              "a unique prefix %s is refused  <-- pinned defect" % flag)
    a = _parse(["d.gdb/p", "--placeholder", "A=1", "--placeholder", "B=2",
                "--check-domains", "--max-placeholder-pct", "A=5"])
    check(a.placeholder == ["A=1", "B=2"] and a.check_domains
          and a.max_placeholder_pct == ["A=5"],
          "the placeholder flags parse, --placeholder is repeatable")
    check(main(["d.gdb/p", "--placeholder", "A"]) == 64,
          "a malformed --placeholder is a usage error")
    check(main(["d.gdb/p", "--max-placeholder-pct", "A"]) == 64,
          "a malformed --max-placeholder-pct is a usage error")
    check(main(["qa.aprx", "--max-placeholder-pct", "A=1", "--check-domains"]) == 64,
          "--max-placeholder-pct against a project is a usage error")
    check(main(["d.gdb/p", "--max-placeholder-pct", "A=1"]) == 64,
          "--max-placeholder-pct with nothing to examine is a usage error")
    ex = world()
    check(main(["t.gdb/parcels", "--placeholder", "*=99999",
                "--max-placeholder-pct", "PARCEL=5"], ex) == 3,
          "main() refuses when a placeholder limit is exceeded")
    check(main(["t.gdb/parcels", "--check-domains"], ex) == 0,
          "main() reports placeholders and exits 0 with no limit")
    ex.projects["qa.aprx"] = _Aprx([_Map("QA", [lyr])], active=None)
    check(main(["qa.aprx", "--placeholder", "*=99999"], ex) == 0,
          "main() scans a project with the tier on")
    check(main(["t.gdb/parcels", "--max-null-pct", "PARCEL=0"], ex) == 3,
          "main() still refuses on a null limit")

    # the script entry point: only a failure exits
    stand_in = types.ModuleType("arcpy")
    stand_in.mp = types.SimpleNamespace(
        ArcGISProject=lambda path: _Aprx([_Map("QA")], active=None))
    stand_in.AddMessage = lambda line: None
    for argv, want in ((["nullscan.py", "--max-null-pct", "A=1"], 64),
                       (["nullscan.py"], None)):
        with mock.patch.dict(sys.modules, {"arcpy": stand_in}), \
                mock.patch.object(sys, "argv", argv):
            try:
                runpy.run_path(__file__, run_name="__main__")
                outcome = None
            except SystemExit as exc:
                outcome = exc.code
        check(outcome == want, "run as a script, the exit code is %r" % (want,))
    check("main" in runpy.run_path(__file__, run_name="nullscan_imported"),
          "imported as a module, nothing runs and nothing exits")

    # ---- the harness itself reports red
    lines = []
    c2, r2, e2, f2 = _harness(lines.append)
    c2(True, "fine")
    c2(False, "broken")
    r2(lambda: int("x"), "a ValueError is the expected error")
    r2(lambda: {}["k"], "the wrong error")
    r2(lambda: None, "no error at all")
    e2(lambda: None, "no exit")
    check(e2(lambda: sys.exit("why"), "an exit") == "why",
          "exits() hands back the exit code")
    check(f2() == 1, "a harness holding a failure returns 1")
    check("FAIL  broken" in lines and "  FAILED: broken" in lines
          and "FAIL  the wrong error (wrong exception KeyError('k'))" in lines
          and "FAIL  no error at all (no error raised)" in lines
          and "FAIL  no exit (no exit)" in lines
          and "7 assertions, 4 failed" in lines,
          "every kind of failure is named in the output")
    lines[:] = []
    c3, _, _, f3 = _harness(lines.append)
    c3(True, "fine")
    check(f3() == 0 and lines[-1] == "1 assertions, 0 failed",
          "a harness holding only passes returns 0")

    return finish()


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="nullscan.py", allow_abbrev=False,
        description="Report NULL, blank and placeholder values in an ArcGIS Pro "
                    "map, a feature class or a table.",
        epilog="Without a --max-*-pct flag nullscan only reports, and exits 0. "
               "nullscan never writes to the data it reads.")
    ap.add_argument("path", nargs="?",
                    help="a .aprx project, or a feature class or table path. "
                         "Omit it inside ArcGIS Pro to scan the open project.")
    ap.add_argument("--max-null-pct", dest="max_null_pct", action="append",
                    metavar="FIELD=PCT", default=[],
                    help="exit 3 when more than PCT percent of FIELD is NULL "
                         "or blank. Repeatable. Needs a feature class or table "
                         "path, not a project. Off by default.")
    ap.add_argument("--placeholder", dest="placeholder", action="append",
                    metavar="FIELD=TOKEN[,TOKEN]", default=[],
                    help="declare values that mean missing, such as 99999 or "
                         "N/A. FIELD '*' covers every field. A value that is a "
                         "code of the field's coded domain is never flagged. "
                         "Repeatable. Off by default.")
    ap.add_argument("--check-domains", dest="check_domains", action="store_true",
                    help="also flag near-miss codes (trailing space, other "
                         "case, dropped leading zeros) and values outside the "
                         "coded value domain of a field")
    ap.add_argument("--max-placeholder-pct", dest="max_placeholder_pct",
                    action="append", metavar="FIELD=PCT", default=[],
                    help="exit 3 when more than PCT percent of FIELD is a "
                         "declared placeholder, a near-miss or off-domain. "
                         "Needs --placeholder or --check-domains and a "
                         "feature class or table path. Repeatable.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None, arcpy=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    try:
        limits = parse_limits(args.max_null_pct)
        plimits = parse_limits(args.max_placeholder_pct, "--max-placeholder-pct")
        tokens = parse_tokens(args.placeholder)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    dataset = bool(args.path and not is_project_path(args.path))
    for flag, wanted in (("--max-null-pct", limits),
                         ("--max-placeholder-pct", plimits)):
        if wanted and not dataset:
            # A project holds many layers and most of them will not carry the
            # field. Refusing every one of those is not a verdict anybody can
            # act on, so the limits stay where a single answer means something.
            print("error: %s judges one dataset. Pass a feature class or table "
                  "path, not a project." % flag, file=sys.stderr)
            return 64
    if plimits and not (tokens or args.check_domains):
        print("error: --max-placeholder-pct needs --placeholder or "
              "--check-domains, or nothing is examined.", file=sys.stderr)
        return 64

    if dataset:
        return scan_dataset(args.path, limits, plimits, tokens,
                            args.check_domains, arcpy)
    return scan_project(args.path, tokens, args.check_domains, arcpy)


if __name__ == "__main__":
    # Only exit on failure. The documented way to run this in the Pro Python
    # window is exec(open(...).read()), where __name__ is already "__main__" --
    # so an unconditional sys.exit() would raise SystemExit into the user's
    # session on a perfectly clean run. Returning normally still exits 0 in a
    # shell, so the documented exit codes are unaffected.
    _code = main()
    if _code:
        sys.exit(_code)
