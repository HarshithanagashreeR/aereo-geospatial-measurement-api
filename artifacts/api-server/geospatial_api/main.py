"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse

from .database import initialize_database
from .routes import router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    initialize_database()
    yield


app = FastAPI(
    title="Aereo Geospatial Measurement API",
    description=(
        "Upload KML or zipped Shapefile data, retain source features and properties, "
        "and calculate polygon areas or line lengths in a suitable projected CRS."
    ),
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.include_router(router, prefix="/api")


@app.get("/openapi.json", include_in_schema=False)
@app.get("/api/openapi.json", include_in_schema=False)
def openapi_schema() -> JSONResponse:
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def local_docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")


@app.get("/api/docs", include_in_schema=False)
def preview_docs():
    return get_swagger_ui_html(
        openapi_url="/api/openapi.json", title=f"{app.title} - Swagger UI"
    )
