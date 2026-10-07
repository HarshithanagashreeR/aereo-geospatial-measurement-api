# Aereo Geospatial Measurement API

Backend API for uploading KML and zipped Shapefiles, preserving source features, and measuring polygon areas and line lengths in a projected CRS.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the FastAPI service on the assigned `PORT`
- `cd artifacts/api-server && python3 -m pytest -v` — run the backend tests
- `pnpm --filter @workspace/api-server run build` — compile-check the Python service
- `pnpm --filter @workspace/api-server run typecheck` — typecheck the retained workspace API scaffold
- Optional env: `AEREO_DATABASE_URL` — SQLite URL; defaults to `artifacts/api-server/data/aereo.sqlite3`

## Stack

- Python 3.11, FastAPI, Pydantic, Uvicorn
- GeoPandas, Shapely, PyProj, GDAL-backed Shapefile reading
- SQLite with SQLAlchemy
- Pytest with FastAPI TestClient
- Docker image based on Python 3.11 slim

## Where things live

- `artifacts/api-server/geospatial_api/routes.py` — HTTP endpoints
- `artifacts/api-server/geospatial_api/processing.py` — KML/Shapefile processing and CRS measurements
- `artifacts/api-server/geospatial_api/models.py` — SQLite persistence models
- `artifacts/api-server/geospatial_api/schemas.py` — Pydantic responses
- `artifacts/api-server/tests/` — upload and measurement tests
- `artifacts/api-server/README.md` — setup, API, CRS strategy, Docker, limitations, and GitHub steps

## Architecture decisions

- KML Placemark IDs and ExtendedData are retained by parsing KML XML into a GeoPandas GeoDataFrame; GeoPandas handles CRS transformations and measurements.
- Missing or invalid Shapefile CRS is not guessed; features are preserved and measurements are skipped.
- Upload source files are temporary; SQLite retains file metadata, properties, geometries, and results.

## Product

Accepts `.kml` files and `.zip` archives containing one Shapefile, computes polygon areas and line lengths in meters, and exposes feature geometry, properties, CRS, and processing status.

## User preferences

The user explicitly requested a backend-only Python/FastAPI implementation with real GIS processing; do not replace it with a frontend or mocked measurements.

## Gotchas

- Shapefile ZIP archives require matching `.shp`, `.shx`, and `.dbf` components; `.prj` is needed for measurements.
- The API is routed under `/api`; Swagger is available at `/docs` locally and `/api/docs` through the Replit preview route.

## Pointers

- Full implementation notes and supported geometry scope are in `artifacts/api-server/README.md`.
