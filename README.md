# nullscan

Report NULL, blank and placeholder values across every layer and standalone table in an ArcGIS Pro map, or in one feature class or table. With `--max-null-pct` and `--max-placeholder-pct` it also refuses: a scan exits non-zero when a field you named is emptier than you allow.

## The failure this is for

A nightly job rebuilds a permits table from a vendor feed. One morning the feed changes. A join key stops matching, and the export fills every missing number with `99999` and every status code with a trailing space: `'A '` instead of `'A'`.

The row count is right. A NULL scan reports the table clean, because nothing is NULL. The domain on `STATUS` does not help either. Esri's overview says a domain-bound field "will not accept" a value outside the domain, but it describes entry. These rows were loaded, not typed. Two days later someone asks why a dashboard shows a building of 99,999 square feet.

nullscan began as the NULL scan. The placeholder tier is the part that sees this case: it counts the values that are present but mean "missing".

```
================================================================
Map: Parcels QA
================================================================

Parcels  (48213 rows)
    OWNER_NAME                      312 null  (0.6%)
    SITUS_ADDR                       84 null       19 blank  (0.2%)
    Shape                             3 null  (0.0%)

Zoning\Overlays  (1204 rows)
    clean

permits (table)  (9877 rows)
    ISSUED_DT                      1102 null  (11.2%)

3 layer(s) or table(s) with NULL or blank values.
```

## What already exists

- Attribute domains in the geodatabase. They stop a bad value on entry. The Esri overview page quoted above does not say that stored rows are checked against a domain. In the test geodatabase built for this repo, `arcpy.da.InsertCursor` wrote `'Z'` and `'A '` into a text field bound to a coded domain without an error. Domains are the right control for editing, and nullscan only reads them.
- ArcGIS Data Reviewer. Esri describes it as validating "data using different checks that test spatial relationships and integrity". It is the heavier tool, for a rule set that a team maintains. nullscan is one file for one question.
- A search of GitHub (`gh search repos`) for "arcpy domain", "arcpy nullscan" and "arcgis domain violations" found no repository that scans for declared placeholder tokens beside coded domains. The closest result, `madhurdevkota/arcgis-data-integrity-workflow`, describes itself as scripting for schema, subtypes, domains and validation. By its description it builds domains, and I have not run it.

## Usage

Requires ArcGIS Pro. Tested on 3.6. No dependencies beyond arcpy.

In the Pro Python window, scanning the map you have open:

```python
exec(open(r"C:\path\to\nullscan.py").read())
```

Standalone, against a saved project:

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" nullscan.py C:\path\to\project.aprx
```

Use that interpreter. arcpy only exists inside Pro's conda environment, not in a system Python install.

Against one feature class or table, with limits:

```
nullscan.py C:\data\city.gdb\permits --max-null-pct ISSUED_DT=5
nullscan.py C:\data\city.gdb\permits --placeholder "*=99999,N/A" --check-domains --max-placeholder-pct STATUS=1
```

| Flag | What it does |
|---|---|
| `--max-null-pct FIELD=PCT` | Exit 3 when more than PCT percent of FIELD is NULL or blank. Repeatable. Dataset path only. |
| `--placeholder FIELD=TOKEN[,TOKEN]` | Declare values that mean missing. `*` as the field covers every field. Repeatable. |
| `--check-domains` | Also flag near-miss codes and off-domain values on every field that has a coded value domain. |
| `--max-placeholder-pct FIELD=PCT` | Exit 3 when more than PCT percent of FIELD is a declared placeholder, a near-miss or off-domain. Needs `--placeholder` or `--check-domains`. Dataset path only. |
| `--self-test` | Run the offline assertions and exit. |

Exit codes: `0` ran, `1` project has no maps, `2` no project found, `3` a limit was exceeded, `64` bad arguments. The argument parser also exits `2` on an unknown flag.

nullscan never writes to the data it reads. It has no `--apply`, because there is nothing to apply. Every parser sets `allow_abbrev=False`, so a prefix such as `--placeh` is refused instead of being read as a flag. The self-test pins this.

## The placeholder tier

Four kinds of value are counted, each with up to three examples:

| Column | What it is | Needs |
|---|---|---|
| `placeholder` | A value equal to a declared token. Text compares without case and without padding. A number matches a token with the same numeric value, so `99999.0` matches `99999`. A midnight date matches `1900-01-01`. | `--placeholder` |
| `near-miss` | Not a domain code, but one once spacing, case and leading zeros are ignored. `'A '` and `'a'` beside the code `'A'`, or `'007'` beside `'7'`. | `--check-domains` |
| `off-domain` | Present, not a code, not a near-miss and not a declared token. | `--check-domains` |
| coded-domain exclusion | A value that is exactly a code of the field's coded value domain is real and never flagged, even when it equals a declared token. A domain that deliberately holds `99999` for "unknown" stays clean. | any of the above |

Each value is judged once, in this order: NULL, blank, exact domain code, near-miss, declared token, off-domain. The order has one consequence worth knowing. With the code `99999` in the domain, the value `'99999 '` is reported as a near-miss, not as a placeholder, because it is a misspelled code.

A real run on a file geodatabase built for this test (eight rows, a coded domain on `STATUS` and on `ZONE`, a range domain on `SQFT`):

```
$ nullscan.py real.gdb\permits --placeholder "*=99999,29999,N/A" --check-domains

permits  (8 rows)
    NOTE                              1 null        2 placeholder  (37.5%)
      e.g. placeholder N/A
    SQFT                              1 null        1 placeholder  (25.0%)
      e.g. placeholder 99999
    STATUS                            1 null        2 near-miss        1 off-domain  (50.0%)
      e.g. near-miss 'A ' ~ 'A'; near-miss 'a' ~ 'A'; off-domain 'Z'
    ZONE                              1 null        1 off-domain  (25.0%)
      e.g. off-domain 5
```

`STATUS` holds a real `99999` code and `ZONE` a real `29999` code. Both are in the domain, so neither is flagged. `SQFT` has only a range domain, which has no codes, so its `99999.0` is flagged.

Field names are matched without regard to case here. A `--placeholder` or limit that names no field in the dataset is reported, never ignored: `--placeholder` prints a note, and `--max-placeholder-pct` refuses.

`--max-placeholder-pct` refuses a field that was never examined, which means no token names it and no coded domain was read. Such a limit could never report red, so nullscan will not let it pass.

## Reading the output

`null` is a true NULL. `blank` is text that is empty or whitespace-only, which is the only way a shapefile can record missing data. A field can show both.

`placeholder`, `near-miss` and `off-domain` are described above. The percentage adds every column.

`~ filtered` means the layer has a definition query or an active selection. Cursors honour both, so only visible rows were scanned.

`~ coded domains not read (...)` means the geodatabase refused the domain lookup. The scan carries on, but the coded-domain exclusion is lost, so a real `99999` code is then flagged as a placeholder. The note says so, and a failed read is never silent.

`! ...` means that layer or table could not be read, usually a broken or moved source. Basemaps and group layers are left out of the report entirely, never scanned and never flagged.

## Configuration

At the top of `nullscan.py`:

| Setting | Default | Effect |
|---|---|---|
| `SCAN_MAPS` | `"active"` | `"active"` scans the open map, and every map when run standalone. `"all"` always scans every map. |
| `REPORT_BLANKS` | `True` | Count `''` and whitespace-only text as blank. |
| `UNSCANNABLE` | `{"Geometry", "Raster", "Blob"}` | Field types the row scan skips. Geometry is handled separately by SQL. |
| `NO_PLACEHOLDER` | `{"OID", "GlobalID"}` | Field types the placeholder tier skips, because their values are identifiers. |
| `PLACEHOLDER_SAMPLES` | `3` | Examples printed per field. Counts are always complete. |
| `NAME_WIDTH` | `28` | Width of the field-name column. Widen if your names wrap. |

## Four bugs this avoids

Writing this the obvious way, by listing every field and testing `value is None` in a cursor, fails in four ways. All four were reproduced on ArcGIS Pro 3.6 before the fix was written.

**Crashes on the first basemap.** `arcpy.mp.Layer` has no `isTable` property:

```python
if layer.isFeatureLayer or layer.isTable:   # AttributeError
```

`or` short-circuits, so this only fires once `isFeatureLayer` is False, meaning the first raster, annotation or basemap layer. Most maps have a basemap.

**Crashes on broken layers, earlier still.** `arcpy.ListFields()` returns `[]` when a source has moved, and an empty field list is rejected:

```
TypeError: 'field_names' must be string or non empty list or tuple of strings
```

**Misses NULL geometry silently.** Reading the geometry column by field name returns a tuple, not `None`:

| accessor | row with NULL geometry |
|---|---|
| `Shape` (by name) | `(None, None)`, so `is None` is **False** |
| `SHAPE@XY` | `(None, None)`, so `is None` is **False** |
| `SHAPE@` | `None` |
| `SHAPE@WKT` | `None` |

A feature with no geometry never gets reported. On the benchmark below the naive version finds 20 fields with nulls. nullscan finds 21. The extra one is `Shape`.

**Never checks standalone tables.** `Map.listLayers()` does not return them. They come from `Map.listTables()`.

There is a fifth problem that does not crash. A `NOT NULL` text column cannot hold NULL but can hold `''` or `'   '`, so filtering fields on `isNullable` alone skips exactly the fields most likely to carry padded blanks. An early version of nullscan had this bug too.

The placeholder tier adds a sixth, in the same family. A `NOT NULL` numeric column cannot hold NULL either, so a sentinel such as `99999` is exactly what it holds. The `*` token reaches those fields on purpose.

## Performance

The figures below are for the NULL and blank scan. The placeholder tier was not benchmarked. It adds one domain lookup per source and one comparison per examined cell, and it is off unless you pass `--placeholder` or `--check-domains`.

200,000 polygons, 30 attribute fields, file geodatabase:

| | naive scan | nullscan |
|---|---|---|
| time | 2995 ms | **1015 ms** |
| fields flagged | 20 | **21** |
| per-field counts | no | yes |

Three things account for the difference.

Fields reporting `isNullable=False` are skipped, which drops the OID and GlobalID for free. Geometry, Raster and Blob stay out of the row scan.

Geometry nulls come from a `WHERE Shape IS NULL` query, which never materialises geometry:

| approach | time | correct |
|---|---|---|
| `SHAPE@` cursor scan | 3195 ms | yes |
| `SHAPE@XY` cursor scan | 1907 ms | yes |
| bare `Shape` + `is None` | 1821 ms | **no, finds 0** |
| **SQL `Shape IS NULL`** | **106 ms** | yes |

That is 30x cheaper than `SHAPE@`, and it returns the same count.

Counting costs about 3% more than only checking existence, 675 ms against 656 ms. Cheap enough that there is no separate early-exit path to maintain.

Two approaches lost and were dropped. Per-field `IS NULL` pushdown took 1255 ms, since 32 queries lose to one scan on a file geodatabase. `TableToNumPyArray` raises `TypeError` on text nulls, and giving it a `null_value` makes a real `'-9999'` indistinguishable from a null.

## Limits

**No enterprise geodatabase testing.** Everything here ran against file geodatabases and shapefiles. On SDE the per-field `IS NULL` pushdown that lost above may win. Re-measure before trusting these numbers on versioned enterprise data. The domain lookup was also only run against a file geodatabase.

**Subtype domains are not read.** The field-level domain is. Esri's domain overview says features grouped into subtypes can each have a different domain, and nullscan does not look at subtypes. A field whose domain differs per subtype is judged against its default domain only.

**Project mode with the placeholder tier ran only against a stand-in arcpy.** The dataset path ran against a real file geodatabase: a table, and a feature class inside a feature dataset. No saved `.aprx` was built for this test.

**Range domains are skipped.** A range domain has no codes to compare with, so only a declared token can flag a value in such a field.

**A near-miss is a guess.** It means "equal to a code once spacing, case and leading zeros are ignored". Two codes with the same key, such as `A` and `a`, share one near-miss target, the first in the domain.

**Blob and Raster nulls go unreported.** Both return `None` when null, but reading them pulls the whole binary payload per row. Skipped on cost, not correctness.

**Shapefile numeric nulls cannot be detected.** DBF stores them as `0`, which no script can tell from a real zero. Esri documents extreme sentinels (`-1.797e308`, `-2147483648`), but a real `FeatureClassToFeatureClass` export writes plain `0`, `0`, `' '` and `None`. Only the text case survives, as `blank`. A shapefile has no domains, so the exclusion and `--check-domains` have nothing to read there. Declared tokens still work on a shapefile or `.dbf`, and that was run for real.

**Output uses `arcpy.AddMessage` alone.** That covers the Python window, script tools and standalone runs. Adding `print()` next to it double-prints every line standalone. If output ever fails to appear, put the `print()` inside `report()`.

## Tests

```
python nullscan.py --self-test
```

The self-test needs no arcpy and no geodatabase. It drives the scan through a small stand-in for arcpy that is written inside the script, so the cursor, domain and project code all run offline. A real excerpt:

```
nullscan self-test: no arcpy, no geodatabase
--------------------------------------------------------------------
PASS  a .aprx path selects project mode
PASS  the .aprx test ignores case
...
PASS  a limit on a field the dataset lacks REFUSES  <-- pinned defect
...
PASS  99999 that IS a domain code is a real value  <-- pinned defect
PASS  a code that is also a token stays excluded with --check-domains
PASS  99999 with no domain is a placeholder  <-- pinned defect
PASS  99999 with a trailing space beside a 99999 code is a near-miss, not a placeholder  <-- pinned defect
...
PASS  a unique prefix --placeh is refused  <-- pinned defect
PASS  a unique prefix --check-d is refused  <-- pinned defect
PASS  a unique prefix --max-placeholder is refused  <-- pinned defect
...
--------------------------------------------------------------------
205 assertions, 0 failed
```

The count is the same on Windows Python 3.13 (`C:\Python313`, and the Pro 3.6 environment with arcpy installed) and on Linux Python 3.12, which has no arcpy. The tests never import the real arcpy, so no assertion depends on it.

Branch coverage is 100%, measured with `coverage run --branch nullscan.py --self-test`.

Two checks guard the tests themselves. The harness is run a second time on assertions built to fail, which proves that a failure is printed and returns 1. Ten deliberate breaks of the code, such as removing the domain exclusion or letting the parser accept an abbreviation, were each caught by the self-test.

## Where the behaviour was checked

Esri documentation, read on 2026-10-22:

- [`arcpy.da.ListDomains(workspace)`](https://doc.esri.com/en/arcgis-pro/latest/arcpy/data-access/listdomains.html) takes the geodatabase and returns Domain objects. Its sample reads `name`, `domainType` (`"CodedValue"` or `"Range"`), `codedValues` and `range`.
- [Field](https://doc.esri.com/en/arcgis-pro/latest/arcpy/classes/field.html): `domain` is "The name of the associated domain", and the `type` values include `OID`, `GlobalID`, `Blob`, `Raster` and `Geometry`.
- [Dataset properties](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/dataset-properties.html): `workspace` "Returns a `Describe` object with the properties of the input dataset's workspace." The [Describe object page](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/describe-object-properties.html) gives `catalogPath` as "The path of the data." nullscan reads `Describe(path).workspace.catalogPath`, and a real run confirmed that it reaches the geodatabase from a feature class inside a feature dataset.
- [Attribute domains overview](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/an-overview-of-attribute-domains.html): coded value domains "specify a valid set of values for an attribute", and subtypes can each have different domains. The page does not say whether stored values are checked.

Real runs, on ArcGIS Pro 3.6 against a synthetic file geodatabase: the dataset scan with tokens, with `--check-domains`, with both, with `--max-placeholder-pct` passing and refusing, a feature class in a feature dataset, and a `.dbf` with declared tokens. The stand-in arcpy covers the remaining paths and the project scan.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT

## Related

Other single-file tools in this portfolio that pair with this one:

- [geocodesift](https://github.com/uhsear/geocodesift) - the same question asked of a geocoded batch: the match rate is not the accuracy
- [tzrot](https://github.com/uhsear/tzrot) - values that are never NULL and still wrong, because a date was read as UTC
