# Aereo Geospatial Measurement API

Aereo accepts real KML files and ZIP archives containing one ESRI Shapefile. It stores upload metadata and processing outcomes in SQLite, keeps each feature's source geometry and attributes, and calculates polygon areas and line lengths in meters.

## Requirements

- Python 3.11 or newer (the Docker image uses Python 3.11)
- `pip`

## Run locally

```bash
cd artifacts/api-server
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
PORT=8080 python -m uvicorn geospatial_api.main:app --host 0.0.0.0 --port 8080 --reload
```

The Replit API workflow starts the same FastAPI app using the assigned `PORT`. The default SQLite database is `data/aereo.sqlite3`; set `AEREO_DATABASE_URL` to a different SQLite URL to change its location.

- Swagger UI: `http://localhost:8080/docs`
- OpenAPI JSON: `http://localhost:8080/openapi.json`
- Replit preview routes: `/api/docs`, `/api/openapi.json`, and `/api/healthz`

## API

### `POST /api/files/`

Upload a `.kml` file or a `.zip` containing exactly one Shapefile (`.shp`, `.shx`, and `.dbf` are required; `.prj` is recommended).

```bash
curl -F "upload=@parcel.kml" http://localhost:8080/api/files/
curl -F "upload=@survey.zip" http://localhost:8080/api/files/
```

Successful uploads return HTTP 201 with the file ID, original filename, byte size, format, status, source CRS, measurement CRS, and feature count. A valid dataset with unsupported geometries or missing CRS is retained with `completed_with_warnings`; details are reported per feature. Invalid uploads return a 4xx response and include the persisted `file_id` inside `detail`.

### `GET /api/files/{id}/`

Returns upload metadata, processing status, detected source CRS, measurement CRS, and any processing error.

```bash
curl http://localhost:8080/api/files/1/
```

### `GET /api/files/{id}/measurements/`

Returns source and measurement CRS values plus each feature's source ID, geometry type, GeoJSON geometry, properties, measurement, units, and measurement status.

```bash
curl http://localhost:8080/api/files/1/measurements/
```

Polygon and MultiPolygon values use `square_meters`; LineString and MultiLineString values use `meters`. Point features have `not_required` status. Unsupported types, empty geometries, missing CRS, and unavailable projections are reported per feature instead of aborting the upload.

## CRS and measurement strategy

The original CRS is retained as `source_crs`. Geometry with an identified CRS is transformed with GeoPandas into a meter-based projected CRS before measuring; GeoPandas' UTM estimate is preferred where it applies. If UTM cannot be estimated, the service attempts a local azimuthal-equidistant projection centered on the input extent. A source CRS already projected in meters is used directly.

KML uses EPSG:4326 as defined by the KML format. A Shapefile with no usable CRS is not silently assigned one: its geometries and properties are preserved, but area and length are skipped with `missing_crs` status. Invalid polygon rings are repaired for measurement only; the returned geometry remains the original source geometry.

Standard KML geometry is read with GeoPandas' Pyogrio engine, backed by GDAL. Placemark XML is also read to preserve source IDs and ExtendedData that GDAL drivers may normalize or omit; those source attributes are merged back onto the GDAL geometries. If a KML extension cannot be parsed by GDAL, the service logs the fallback and uses the parsed XML geometry so unsupported features remain visible and do not discard other Placemarks. ZIP Shapefiles are read through GeoPandas' GDAL-backed reader after the archive is checked for path traversal, excessive expansion, and multiple Shapefiles. Uploads are limited to 50 MiB compressed and ZIP contents to 256 MiB expanded.

## Architecture

```text
geospatial_api/
  main.py         FastAPI app, docs, lifecycle
  routes.py       Upload, file detail, measurement endpoints
  processing.py   KML/ZIP ingestion, CRS selection, measurement
  models.py       SQLAlchemy upload and feature records
  schemas.py      Pydantic response models
  database.py     SQLite engine and request-scoped sessions
tests/
  conftest.py     Isolated SQLite test database
  test_api.py     Real KML and zipped-Shapefile integration tests
```

The upload stream is processed in a temporary directory and removed after the request. SQLite stores upload metadata, status, properties, and normalized GeoJSON feature geometry, not a permanent copy of the original upload. This keeps the first version simple and avoids retaining user files longer than needed.

## Tests

```bash
cd artifacts/api-server
python -m pytest -v
```

The suite exercises real KML and Shapefile files, projected measurements from EPSG:4326, polygon area, line length, points, missing CRS, unsupported geometries, invalid files, persistence, health, and Swagger/OpenAPI routes.

## Docker

From the repository root:

```bash
docker build -f artifacts/api-server/Dockerfile -t aereo-geospatial-api artifacts/api-server
docker run --rm -p 8080:8080 -v aereo-data:/app/data aereo-geospatial-api
```

Persist `/app/data` with a volume for SQLite data. Configure `AEREO_DATABASE_URL` if using a different SQLite path.

## Design decisions and limitations

- SQLite and SQLAlchemy keep local development and Docker deployments self-contained; upload metadata is written before parsing so failures remain inspectable.
- Unsupported or malformed individual geometries produce feature-level warnings; one unsupported feature does not discard valid features in the same dataset.
- ZIP uploads must contain exactly one Shapefile and require `.shp`, `.shx`, and `.dbf`; zipped GeoPackages and other vector formats are not accepted.
- Missing Shapefile CRS cannot be inferred reliably, so measurements are intentionally omitted rather than guessed.
- KML parsing supports standard KML Point, LineString, LinearRing, Polygon, MultiGeometry, and ExtendedData. Network links, KML tours, and Google-specific `gx:` tracks are not processed.
- UTM is appropriate for local/regional datasets. A dataset spanning continents or the antimeridian may need a domain-specific equal-area or geodesic measurement policy.
- This API does not provide authentication, quotas, asynchronous job queues, original-file retention, or long-term storage outside the configured SQLite database.

## Learning and future scope

- Keep source geometry and CRS distinct from the projected geometry used for measurements; never calculate areas or lengths directly in geographic degrees.
- Vector drivers can normalize KML feature metadata, so preserve Placemark IDs and ExtendedData from the source document while using GDAL-backed geometry ingestion.
- Future production work could add authentication and quotas, asynchronous jobs for large uploads, durable original-file storage, database migrations, and domain-specific geodesic or equal-area measurement policies for very broad datasets.


