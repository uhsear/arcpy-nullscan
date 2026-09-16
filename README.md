# nullscan

Report NULL and blank values across every layer and standalone table in an ArcGIS Pro map.

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

Exit codes: `0` ran, `1` project has no maps, `2` no project found.

## Reading the output

`null` is a true NULL. `blank` is text that is empty or whitespace-only, which is the only way a shapefile can record missing data. A field can show both.

`~ filtered` means the layer has a definition query or an active selection. Cursors honour both, so only visible rows were scanned.

`! ...` means that layer or table could not be read, usually a broken or moved source. Basemaps and group layers are left out of the report entirely, never scanned and never flagged.

## Configuration

At the top of `nullscan.py`:

| Setting | Default | Effect |
|---|---|---|
| `SCAN_MAPS` | `"active"` | `"active"` scans the open map, and every map when run standalone. `"all"` always scans every map. |
| `REPORT_BLANKS` | `True` | Count `''` and whitespace-only text as blank. |
| `UNSCANNABLE` | `{"Geometry", "Raster", "Blob"}` | Field types the row scan skips. Geometry is handled separately by SQL. |
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

## Performance

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

**No enterprise geodatabase testing.** Everything here ran against file geodatabases and shapefiles. On SDE the per-field `IS NULL` pushdown that lost above may win. Re-measure before trusting these numbers on versioned enterprise data.

**Blob and Raster nulls go unreported.** Both return `None` when null, but reading them pulls the whole binary payload per row. Skipped on cost, not correctness.

**Shapefile numeric nulls cannot be detected.** DBF stores them as `0`, which no script can tell from a real zero. Esri documents extreme sentinels (`-1.797e308`, `-2147483648`), but a real `FeatureClassToFeatureClass` export writes plain `0`, `0`, `' '` and `None`. Only the text case survives, as `blank`.

**Output uses `arcpy.AddMessage` alone.** That covers the Python window, script tools and standalone runs. Adding `print()` next to it double-prints every line standalone. If output ever fails to appear, put the `print()` inside `report()`.

## Tests

```
"C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe" test_nullscan.py
```

32 assertions against a fixture the test builds itself: a file geodatabase with nulls across text, numeric, date and geometry fields, a non-nullable text field holding blanks, a clean class, an empty class, a standalone table, a standalone table in its own geodatabase, a shapefile, a group layer, a nested table, a broken data source and a definition query.

Two assertions pin the original script's bugs so they cannot come back quietly.

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
