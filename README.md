# Geospatial File Measurement API

A backend service built with FastAPI. You upload a Shapefile (as a `.zip`) or a KML file, and it reads every feature in the file and returns the area of polygons and the length of lines, in metres.

The main thing I had to get right here is the coordinate system. Most files use latitude/longitude (EPSG:4326), which is measured in degrees, and a degree is not a fixed distance. So I never calculate area or length from raw lat/lon. Every geometry is projected into a metric CRS first. The details are in the [CRS handling](#crs-handling) section.

## Setup

You need Python 3.10 or newer. I developed and tested this on Python 3.13 on Windows.

```bash
python -m venv .venv
.venv\Scripts\activate            # on Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

I did not need to install GDAL separately. The `geopandas` and `pyogrio` wheels come with it bundled.

### Run the app

```bash
uvicorn app.main:app --reload
```

The API runs at http://localhost:8000 and the interactive Swagger docs are at http://localhost:8000/docs. You can upload the files from the `samples/` folder there to try it quickly.

By default it uses SQLite (`data/app.db`) and stores uploaded files in `data/uploads/`. These can be changed with the environment variables `DATABASE_URL`, `UPLOAD_DIR`, `MAX_UPLOAD_BYTES` and `MAX_UNZIPPED_BYTES`.

### Run the tests

```bash
pytest
```

There are 31 tests. Some are unit tests for the CRS selection and measurement code, where I compare my results with an independent geodesic calculation from `pyproj`. The others are API tests that create real Shapefiles and KML files and upload them.

## API

| Method | Path | What it does |
|--------|------|--------------|
| POST | `/api/files/` | Upload and process a `.zip` (Shapefile) or `.kml` file |
| GET | `/api/files/{id}/` | Get information and status of an uploaded file |
| GET | `/api/files/{id}/measurements/` | Get the measurements for each feature, with totals. Supports `?limit=` and `?offset=` |

### Upload a file

```bash
curl -F "file=@samples/survey.kml" http://localhost:8000/api/files/
```

```json
{
  "id": "e19f0a6d9ee6",
  "filename": "survey.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "error": null
}
```

### Get file information

```bash
curl http://localhost:8000/api/files/e19f0a6d9ee6/
```

This returns the same object as above. `status` is either `COMPLETED` or `FAILED`.

### Get measurements

```bash
curl http://localhost:8000/api/files/e19f0a6d9ee6/measurements/
```

```json
{
  "file_id": "e19f0a6d9ee6",
  "crs": "EPSG:4326",
  "feature_count": 3,
  "limit": 100,
  "offset": 0,
  "summary": {
    "total_area_m2": 972236.6744,
    "total_length_m": 1085.6077,
    "measured_features": 2,
    "unmeasured_features": 1
  },
  "features": [
    {
      "feature_index": 0,
      "geometry_type": "Polygon",
      "crs": "EPSG:4326",
      "geometry": { "type": "Polygon", "coordinates": [[[77.59, 12.97, 900.0], "..."]] },
      "properties": { "Name": "Plot A", "description": "survey plot" },
      "measurement": {
        "type": "area",
        "value": 972236.6744,
        "unit": "m2",
        "projected_crs": "Lambert Azimuthal Equal Area (lon_0=77.595, lat_0=12.974)"
      },
      "note": null
    },
    {
      "feature_index": 2,
      "geometry_type": "Point",
      "crs": "EPSG:4326",
      "geometry": { "type": "Point", "coordinates": [77.595, 12.975, 0.0] },
      "properties": { "Name": "Well" },
      "measurement": null,
      "note": "No measurement is defined for point geometries."
    }
  ]
}
```

A few things to know about the response:

- `crs` is the CRS of the uploaded file. The `geometry` is always returned as GeoJSON in EPSG:4326, so the client always gets the same format.
- `measurement` is `null` for features that can't be measured (points, empty geometries, unsupported types). The reason is given in `note`.
- The `summary` covers the whole file, not just the current page.

### Errors

| Status | When it happens |
|--------|-----------------|
| 400 | The file extension is not `.zip` or `.kml` |
| 404 | The file id does not exist |
| 409 | Measurements were requested for a file that is not `COMPLETED` |
| 413 | The upload is bigger than `MAX_UPLOAD_BYTES` |
| 422 | The file can't be used: corrupt zip, no `.shp` inside, missing `.dbf`, no CRS (missing `.prj`), malformed KML, or no features |

Failed uploads are saved too, so `GET /api/files/{id}/` shows `FAILED` along with the reason.

## Architecture

### Application structure

```
app/
  main.py              creates the FastAPI app, sets up tables and folders on startup
  config.py            settings, can be overridden with environment variables
  database.py          SQLAlchemy engine and session
  models.py            two tables: UploadedFile and Feature
  schemas.py           Pydantic response models
  routers/files.py     the three endpoints (only HTTP handling)
  services/
    parser.py          reads KML and zipped Shapefiles into a GeoDataFrame
    measure.py         picks the CRS and calculates area / length
    processing.py      ties it together: parse, normalise, measure, save
scripts/
  compare_area_methods.py   small script comparing UTM vs equal-area vs geodesic
tests/                 unit tests and API tests
```

I kept the router thin on purpose. All the geospatial logic is in `services/`, and `measure.py` is just functions that take a geometry and return a measurement. That made it easy to unit test without starting a server.

### File-processing flow

1. `POST /api/files/` checks the extension and saves the upload to disk in 1 MB chunks, rejecting it if it goes over the size limit.
2. `parser.py` reads the file:
   - For KML, it reads every layer (a KML folder becomes a layer). KML is always EPSG:4326. I drop the styling columns GDAL adds (like `tessellate` and `extrude`) so the properties only contain real data.
   - For a zip, it first checks for unsafe paths and a huge uncompressed size, then extracts to a temp folder. It expects exactly one `.shp`, requires the `.dbf`, and rejects the file if there is no CRS.
3. If the file is not in EPSG:4326 (for example UTM or State Plane), the geometries are converted to EPSG:4326 so the next step always works with one CRS.
4. Each feature is measured, and the file record plus all features are saved to the database.
5. Any error is caught and saved as `status = FAILED` with a message, so the request never ends with a raw stack trace.

### Measurement flow

For each feature (geometry in EPSG:4326):

```
no geometry / empty         -> no measurement, with a note
Point / MultiPoint          -> no measurement, with a note
any other type              -> no measurement, "not supported" note
Polygon / MultiPolygon      -> drop Z, repair if invalid, project to equal-area CRS, take area
LineString / MultiLineString -> drop Z, project to UTM zone, take length
```

### CRS handling

| What | CRS used | Why |
|------|----------|-----|
| Polygon area | Lambert Azimuthal Equal Area, centred on the feature | Equal-area projections keep area correct by definition |
| Line length | The UTM zone of the feature's centre (EPSG:326xx north, 327xx south) | Metric, and the scale error is small (about 0.05%) inside a zone |
| Feature wider than 6 degrees of longitude | Geodesic calculation on the WGS84 ellipsoid | One projection would distort something that large |
| Beyond 84°N or 80°S | UPS polar CRS (EPSG:5041 / 5042) | UTM is not defined there |

## Design decisions

**Equal-area for area, UTM for length.** My first version used UTM for everything. Then I compared its area against `pyproj`'s geodesic area, which I used as the reference. For the sample plot UTM was about 0.12% too large. That is expected, because UTM preserves shape, not area, and its scale factor is 0.9996 on the central meridian and grows towards the edges of the zone. For measuring land, I did not want a built-in error, so polygons now use an equal-area projection (the difference from the reference is below 0.01%). I kept UTM for lengths because its error is small there and it is the standard choice. You can reproduce the comparison with `python scripts/compare_area_methods.py`.

I also considered using a geodesic calculation for everything. It is the most accurate, but the assignment asks for a projected CRS, so I only use it as a fallback for very wide features and as the reference in tests.

**Synchronous processing.** The file is processed inside the upload request, so the response is ready to use straight away and there is no queue or worker to run. The `status` field and the `PROCESSING` state are already in the model, so moving to background processing later would not change the API. I considered Celery or RQ, but for the file sizes I expect it would add complexity without much benefit.

**FastAPI instead of Django.** This is a small API with no admin, users or templates, so FastAPI was enough. I also get typed request and response models and the `/docs` page without extra work.

**Features are stored in the database.** This way the measurements endpoint is a simple query with pagination and totals calculated in SQL, instead of reading the file again on every request. SQLite is the default, and other databases work by changing `DATABASE_URL`.

**Problems are reported, not raised.** An unsupported or empty geometry gives `measurement: null` with a note. An invalid polygon (for example a self-crossing "bowtie") is repaired with `make_valid` and the note says so. A bad file returns a 422 with a clear reason and is saved as `FAILED`.

**Basic upload safety.** The client's filename is reduced to its base name, zip entries are checked for path traversal, the uncompressed size is limited, and uploads are streamed with a size limit.

**Output is always GeoJSON in EPSG:4326**, with the original CRS reported separately.

## Limitations

- Processing is synchronous, so a very large file will make the upload request slow.
- Features that cross the antimeridian (longitude 180) are not handled specially.
- A zip must contain exactly one Shapefile.
- Only polygons and lines are measured. Geometry collections are reported as unsupported.

## What I learned

- A degree of longitude gets shorter as you move towards the poles. A 1° x 1° box is about 12,300 km² at the equator but only about 4,200 km² at 70°N, so calculating on raw degrees gives wrong answers. There is a test for this.
- UTM is not equal-area. I only found this by checking my results against an independent calculation instead of just checking that the code ran, and it changed my design.
- Real geospatial files are messy. Shapefiles can be missing the `.prj`, KML files carry altitude values and extra styling columns, and polygons can be invalid.
- A zip file from a user has to be treated carefully. I added checks for path traversal and for very large uncompressed sizes.

## Future scope

- Move processing to a background worker (Celery or RQ) so large files don't block the request, and let the client poll the status.
- Handle features that cross the antimeridian.
- Support more formats such as GeoJSON and GeoPackage. The parser already has one function per format, so this should be easy to add.
- Return a GeoJSON `FeatureCollection` and a CSV export of the measurements.
- Add polygon perimeter and let the client choose units (hectares, acres, kilometres, miles).
- Add authentication, per-user files, and deleting or expiring old files.
- Use PostgreSQL/PostGIS with Alembic migrations, and add a Dockerfile and CI.
