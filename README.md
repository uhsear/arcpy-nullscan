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
- ArcGIS Data Reviewer. Its [checks overview](https://doc.esri.com/en/arcgis-pro/latest/help/data/validating-data/an-overview-of-data-reviewer-checks-in-arcgis-pro.html) lists a Domain check that "Finds attribute values that do not comply with the coded value or range domains that are associated with an attribute field". That is the off-domain part of nullscan, done well, with a rule set a team maintains. It also lists Query Attributes ("Finds records based on a `WHERE` clause run against row attributes") and Regular Expression checks, and a WHERE clause can express `STATUS = 99999`. What nullscan adds is smaller: tokens declared on the command line, a near-miss report that separates `'A '` from a truly foreign value, the exclusion of a `99999` that is a real code, and a non-zero exit for a nightly job. It is one file, with no rule set to maintain. I did not run Data Reviewer for this repo.
- A search of GitHub (`gh search repos`) on 2026-10-30 for "arcpy domain", "arcpy domains", "arcpy placeholder values", "arcpy sentinel values", "arcgis domain violations" and "arcgis null scan" returned nothing. "arcgis data integrity" returned `madhurdevkota/arcgis-data-integrity-workflow`, which describes itself as scripting for schema, subtypes, domains and validation, and `monicaburagadda/data-validation-toolbox`, a toolbox that exports tables and audits them with Pandas. By their descriptions neither scans for declared placeholder tokens beside coded domains, and I have not run either.

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

Exit codes: `0` ran and read every source, `1` project has no maps, `2` no project or dataset found, `3` a limit was exceeded, `4` a layer, table or dataset could not be read, `64` bad arguments. Exit `4` is distinct from `3` on purpose: a nightly job that cannot read its input has not found clean data. The report still lists every `!` line, and the project footer counts them.

Some codes are shared. The argument parser exits `2` on an unknown flag. An uncaught error and a missing arcpy both exit `1`. A zero-byte `.aprx` prints the "No open project" text and exits `2`, which hides the real cause.

In a notebook, `sys.argv` holds the kernel's own flags (`-f kernel.json`), so running the file there ends in `unrecognized arguments: -f` and exit `2`. Import the file and pass the arguments yourself instead: `import nullscan; nullscan.main([r"C:\data\city.gdb\permits", "--max-null-pct", "ISSUED_DT=5"])`.

nullscan never writes to the data it reads. It has no `--apply`, because there is nothing to apply. Every parser sets `allow_abbrev=False`, so a prefix such as `--placeh` is refused instead of being read as a flag. The self-test pins this.

## The placeholder tier

Four kinds of value are counted, each with up to three examples:

| Column | What it is | Needs |
|---|---|---|
| `placeholder` | A value equal to a declared token. Text compares without case and without padding. A number matches a token with the same numeric value, so `99999.0` matches `99999`. In a Single (32-bit float) field the token is rounded to float32 first, because arcpy returns `-9999.9` there as `-9999.900390625`. A midnight date matches `1900-01-01`. | `--placeholder` |
| `near-miss` | Not a domain code, but one once spacing, case and leading zeros are ignored. `'A '` and `'a'` beside the code `'A'`, or `'007'` beside `'7'`. | `--check-domains` |
| `off-domain` | Present, not a code, not a near-miss and not a declared token. | `--check-domains` |
| coded-domain exclusion | A value that is exactly a code of the field's coded value domain is real and never flagged, even when it equals a declared token. A domain that deliberately holds `99999` for "unknown" stays clean. | any of the above |

Each value is judged once, in this order: NULL, blank, exact domain code, near-miss, declared token, off-domain. The order has one consequence worth knowing. With the code `99999` in the domain and `--check-domains` on, the value `'99999 '` is reported as a near-miss, not as a placeholder, because it is a misspelled code. With `--placeholder` alone the near-miss step is skipped, so the same value is reported as a placeholder.

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

Field names are matched without regard to case in `--placeholder`, `--max-null-pct` and `--max-placeholder-pct`. A `--placeholder` or limit that names no field is reported, never ignored: `--placeholder` prints a note, in a dataset scan and in a project scan, and the limits refuse. In a project scan the note appears when no scanned layer or table has the field. A joined layer lists its fields under qualified names, so `--placeholder lookup.STATUS=99999` reaches a joined field and `STATUS=99999` does not.

`--max-placeholder-pct` refuses a field that was never examined, which means no token names it and no coded domain was read. Such a limit could never report red, so nullscan will not let it pass. `--max-null-pct` does the same for a field whose NULLs were never read: a Blob, Raster or Geometry column, or a `NOT NULL` field that is not text.

## Reading the output

`null` is a true NULL. `blank` is text that is empty or whitespace-only, which is the only way a shapefile can record missing data. A field can show both.

`placeholder`, `near-miss` and `off-domain` are described above. The percentage adds every column.

`~ filtered` means the layer has a definition query or an active selection. Cursors honour both, so only visible rows were scanned.

`~ NULLs not checked in PHOTO (Blob, Raster or Geometry)` names the columns whose NULLs the scan did not read. A table whose only unread column is a Blob still reads `clean` for the rest.

`~ coded domains not read (...)` means the geodatabase refused the domain lookup. The scan carries on. The note reports only that the domains were not read. The exclusion is lost, so a real `99999` code is flagged as a placeholder. That can cause a false refusal and never a false pass.

`! ...` means that layer or table could not be read, usually a broken or moved source. The run then exits `4`. Basemaps and group layers are left out of the report entirely, never scanned and never flagged.

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

**Subtype domains are not read.** The field-level domain is. Esri's domain overview says features grouped into subtypes can each have a different domain, and nullscan does not look at subtypes. A field whose domain differs per subtype is judged against its default domain only. A field that has no field-level domain gets no coded-domain exclusion, so a real `99999` code held only by a subtype domain is flagged as a placeholder.

**Project mode ran for real on one small project.** A saved `.aprx` holding a table, a standalone table and a layer with a join was scanned with the placeholder tier. A copy with its geodatabase moved exited `4` and listed three `!` sources. The project came from a throwaway script that is not in this repo.

**Exit `0` is not proof of a clean source in two cases.** A `--placeholder` whose token can never match the field's type finds nothing: `N/A` declared for a number, or `1/1/1900` for a date (declare a date as `1900-01-01`). A matching `--max-placeholder-pct` then passes. And `--check-domains` on a source with no coded domain, such as a shapefile, a Range-only field or a domain name that `ListDomains` did not return, prints `clean` with no note. Only `--max-placeholder-pct` turns that into a refusal.

**A passing placeholder verdict says nothing about what it did not look for.** `VERDICT: within every --max-placeholder-pct limit.` does not say whether near-misses and off-domain values were examined. Without `--check-domains`, a trailing-space `'A '` passes the gate unseen.

**A decimal limit can refuse at its exact boundary.** The comparison is `pct / 100.0` against `affected / rows` in floating point. With 7 NULLs in 250 rows, `--max-null-pct F=2.8` prints `F is 2.8% ... limit is 2.8%` and exits `3`. Over every whole-number limit from 0 to 100 and every row count up to 150, the float comparison and the exact integer comparison agreed in 1,158,975 cases. With limits of one decimal place they disagreed in 13 of 11,486,475 cases.

**Token parsing is loose.** A token goes through Python's `float()`, so `99_999` is read as 99999. An integer above 2**53 is compared as a float and can miss. A text field holding `'099999'` does not match the token `99999`. A token that holds a comma cannot be declared, because `--placeholder` splits on commas. A decimal token is rounded to float32 for a Single field and compared exactly for a Double field.

**The near-miss key ignores only case and the whitespace that `str.split()` knows.** A zero-width space after a code is not ignored, so `'A'` plus U+200B is off-domain, not a near-miss. A non-breaking space is ignored.

**A layer with a join rejects the geometry `IS NULL` query.** The report says `geometry NULLs not checked (source rejected IS NULL)`, and the geometry column is listed as not checked. Its fields have qualified names, such as `lookup.STATUS`. Both were seen on a real project.

**Scanning a project reads every feature layer in it, web layers included, with the account that Pro is signed in to.** This was not run against a web layer.

**arcpy error text is printed as it comes.** A report line such as `! OSError: "<path>" does not exist` can carry a path, and an enterprise connection error can carry a server or account name. Read a report before you paste it into a ticket.

**A dataset path that `ListFields` rejects reports an `AttributeError`, not the arcpy error.** `scan()` retries with `source.dataSource`, which a plain path string does not have. The self-test pins this message. `scan_dataset()` checks `arcpy.Exists` first, so it needs a path that exists and cannot be listed.

**Path labels are cosmetic on Linux.** The label is the part after the last path separator. A backslash path on Linux keeps its whole text as the label (`a\b` stays `a\b`). Linux has no arcpy, so only the self-test meets this.

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
PASS  a project with an unreadable layer exits 4, not 0  <-- pinned defect
...
PASS  a decimal token matches the float32 a Single field returns  <-- pinned defect
PASS  a null limit typed in another case finds its field  <-- pinned defect
PASS  a null limit on a NOT NULL non-text field REFUSES  <-- pinned defect
PASS  a clean report names the Blob column it did not read  <-- pinned defect
...
PASS  a unique prefix --placeh is refused  <-- pinned defect
PASS  a unique prefix --check-d is refused  <-- pinned defect
PASS  a unique prefix --max-placeholder is refused  <-- pinned defect
...
--------------------------------------------------------------------
238 assertions, 0 failed
```

The count is the same on Windows Python 3.13 (`C:\Python313`, and the Pro 3.6 environment with arcpy installed) and on Linux Python 3.12, which has no arcpy. The tests never import the real arcpy, so no assertion depends on it.

Branch coverage is 100%, measured with `coverage run --branch nullscan.py --self-test`.

Two checks guard the tests themselves. The harness is run a second time on assertions built to fail, which proves that a failure is printed and returns 1. A throwaway script then applied 76 one-line breaks of the code, such as removing the domain exclusion, comparing field names with case, or letting the parser accept an abbreviation, and the self-test failed on each one. That script is not in this repo, so the figure is a claim about one run on 2026-10-30. The first run of such a sweep left six breaks alive, and each now has a pinned assertion.

The real-geodatabase block above and the figures in "Where the behaviour was checked" came from throwaway scripts that built synthetic data. They are not in this repo, so the block is not regenerated here.

## Where the behaviour was checked

Esri documentation, read on 2026-10-22:

- [`arcpy.da.ListDomains(workspace)`](https://doc.esri.com/en/arcgis-pro/latest/arcpy/data-access/listdomains.html) takes the geodatabase and returns Domain objects. Its sample reads `name`, `domainType` (`"CodedValue"` or `"Range"`), `codedValues` and `range`.
- [Field](https://doc.esri.com/en/arcgis-pro/latest/arcpy/classes/field.html): `domain` is "The name of the associated domain", and the `type` values include `OID`, `GlobalID`, `Blob`, `Raster` and `Geometry`.
- [Dataset properties](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/dataset-properties.html): `workspace` "Returns a `Describe` object with the properties of the input dataset's workspace." The [Describe object page](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/describe-object-properties.html) gives `catalogPath` as "The path of the data." nullscan reads `Describe(path).workspace.catalogPath`, and a real run confirmed that it reaches the geodatabase from a feature class inside a feature dataset.
- [Attribute domains overview](https://doc.esri.com/en/arcgis-pro/latest/help/data/geodatabases/overview/an-overview-of-attribute-domains.html): coded value domains "specify a valid set of values for an attribute", and subtypes can each have different domains. The page does not say whether stored values are checked.

Real runs, on ArcGIS Pro 3.6 against synthetic data: the dataset scan with tokens, with `--check-domains`, with both, with `--max-placeholder-pct` passing and refusing, a feature class in a feature dataset, a `.dbf` with declared tokens, a Single field holding `-9999.9`, a table with a Blob column, `--max-null-pct` typed in another case and on a `NOT NULL` number, a 250-row decimal-limit boundary, and a saved project with a join, a typo in a declared field and a moved geodatabase. The stand-in arcpy covers the remaining paths, including exit `4` for a single dataset.

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
