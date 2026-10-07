from __future__ import annotations

from io import BytesIO
import zipfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import GeometryCollection, LineString, Point, Polygon
from sqlalchemy import select

from geospatial_api.processing import process_geodataframe


def kml_for(geometry: str, feature_id: str = "field-42") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2">
      <Document><Placemark id="{feature_id}">
        <name>Test feature</name>
        <ExtendedData><Data name="land_use"><value>meadow</value></Data></ExtendedData>
        {geometry}
      </Placemark></Document>
    </kml>"""


def polygon_geometry() -> str:
    return """<Polygon><outerBoundaryIs><LinearRing><coordinates>
      3,51,0 3.001,51,0 3.001,51.001,0 3,51.001,0 3,51,0
    </coordinates></LinearRing></outerBoundaryIs></Polygon>"""


def upload(client, filename: str, body: bytes, content_type: str = "application/octet-stream"):
    return client.post(
        "/api/files/",
        files={"upload": (filename, body, content_type)},
    )


def create_shapefile_zip(
    directory: Path, *, crs: str | None = "EPSG:4326", geometry=None
) -> bytes:
    geometry = geometry or Polygon(
        [(3, 51), (3.001, 51), (3.001, 51.001), (3, 51.001), (3, 51)]
    )
    source = directory / "field.shp"
    frame = gpd.GeoDataFrame(
        {"parcel": ["west-field"]},
        geometry=[geometry],
        crs=crs,
    )
    frame.to_file(source, driver="ESRI Shapefile", index=False)
    archive_path = directory / "field.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part in directory.glob("field.*"):
            if part.suffix.lower() != ".zip":
                archive.write(part, arcname=part.name)
    return archive_path.read_bytes()


def test_real_kml_polygon_is_measured_in_projected_crs_and_saved(client):
    response = upload(
        client,
        "parcel.kml",
        kml_for(polygon_geometry()).encode(),
        "application/vnd.google-earth.kml+xml",
    )

    assert response.status_code == 201, response.text
    file_data = response.json()
    assert file_data["status"] == "completed"
    assert file_data["source_crs"] == "EPSG:4326"
    assert file_data["measurement_crs"].startswith("EPSG:326")
    assert file_data["feature_count"] == 1

    details = client.get(f"/api/files/{file_data['id']}/").json()
    assert details["original_filename"] == "parcel.kml"

    measurements = client.get(
        f"/api/files/{file_data['id']}/measurements/"
    ).json()
    feature = measurements["features"][0]
    assert feature["source_feature_id"] == "field-42"
    assert feature["geometry_type"] == "Polygon"
    assert feature["properties"]["land_use"] == "meadow"
    assert feature["measurement_status"] == "measured"
    assert feature["measurement_unit"] == "square_meters"
    assert 7_000 < feature["measurement_value"] < 9_000
    assert feature["geometry"]["type"] == "Polygon"


def test_real_kml_linestring_length_is_in_meters(client):
    geometry = """<LineString><coordinates>
      3,0,0 3.01,0,0
    </coordinates></LineString>"""
    response = upload(client, "route.kml", kml_for(geometry).encode())

    assert response.status_code == 201, response.text
    file_id = response.json()["id"]
    result = client.get(f"/api/files/{file_id}/measurements/").json()
    feature = result["features"][0]
    assert result["source_crs"] == "EPSG:4326"
    assert result["measurement_crs"].startswith("EPSG:326")
    assert feature["measurement_unit"] == "meters"
    assert 1_100 < feature["measurement_value"] < 1_120


def test_real_zipped_shapefile_is_processed(client, tmp_path):
    archive = create_shapefile_zip(tmp_path)
    response = upload(client, "field-data.zip", archive)

    assert response.status_code == 201, response.text
    file_id = response.json()["id"]
    measurements = client.get(f"/api/files/{file_id}/measurements/").json()
    feature = measurements["features"][0]
    assert measurements["source_crs"] == "EPSG:4326"
    assert feature["properties"]["parcel"] == "west-field"
    assert feature["geometry_type"] == "Polygon"
    assert feature["measurement_status"] == "measured"
    assert feature["measurement_value"] > 7_000


def test_point_feature_needs_no_measurement(client):
    geometry = "<Point><coordinates>3,51,0</coordinates></Point>"
    response = upload(client, "marker.kml", kml_for(geometry).encode())

    assert response.status_code == 201, response.text
    feature = client.get(
        f"/api/files/{response.json()['id']}/measurements/"
    ).json()["features"][0]
    assert feature["geometry_type"] == "Point"
    assert feature["measurement_status"] == "not_required"
    assert feature["measurement_value"] is None


def test_missing_crs_is_reported_without_calculating_in_degrees(client, tmp_path):
    archive = create_shapefile_zip(tmp_path, crs=None)
    response = upload(client, "no-crs.zip", archive)

    assert response.status_code == 201, response.text
    file_data = response.json()
    assert file_data["source_crs"] is None
    assert file_data["measurement_crs"] is None
    assert file_data["status"] == "completed_with_warnings"
    feature = client.get(
        f"/api/files/{file_data['id']}/measurements/"
    ).json()["features"][0]
    assert feature["measurement_status"] == "missing_crs"
    assert feature["measurement_value"] is None


def test_invalid_projection_file_is_treated_as_missing_crs(client, tmp_path):
    archive_bytes = create_shapefile_zip(tmp_path, crs=None)
    archive_path = tmp_path / "invalid-projection.zip"
    with zipfile.ZipFile(BytesIO(archive_bytes)) as source:
        members = [(member.filename, source.read(member.filename)) for member in source.infolist()]
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, content in members:
            target.writestr(name, content)
        target.writestr("field.prj", "this is not a valid CRS")

    response = upload(client, "invalid-projection.zip", archive_path.read_bytes())

    assert response.status_code == 201, response.text
    file_data = response.json()
    assert file_data["source_crs"] is None
    assert file_data["measurement_crs"] is None
    assert file_data["status"] == "completed_with_warnings"
    feature = client.get(
        f"/api/files/{file_data['id']}/measurements/"
    ).json()["features"][0]
    assert feature["measurement_status"] == "missing_crs"
    assert feature["measurement_value"] is None


def test_unsupported_geometry_does_not_abort_other_feature_measurements():
    frame = gpd.GeoDataFrame(
        {"name": ["mixed", "valid-line"]},
        geometry=[
            GeometryCollection([Point(3, 51), LineString([(3, 51), (3.01, 51)])]),
            LineString([(3, 51), (3.01, 51)]),
        ],
        crs="EPSG:4326",
    )

    result = process_geodataframe(frame)

    assert result["status"] == "completed_with_warnings"
    assert result["features"][0]["measurement_status"] == "unsupported"
    assert result["features"][1]["measurement_status"] == "measured"
    assert 680 < result["features"][1]["measurement_value"] < 720


def test_unsupported_kml_feature_does_not_abort_upload(client):
    source = b"""<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2"
         xmlns:gx="http://www.google.com/kml/ext/2.2">
      <Document>
        <Placemark id="unsupported-track"><gx:Track><gx:coord>3 51 0</gx:coord></gx:Track></Placemark>
        <Placemark id="valid-line"><LineString><coordinates>3,51 3.01,51</coordinates></LineString></Placemark>
      </Document>
    </kml>"""
    response = upload(client, "mixed-geometries.kml", source)

    assert response.status_code == 201, response.text
    file_data = response.json()
    assert file_data["status"] == "completed_with_warnings"
    assert file_data["feature_count"] == 2
    features = client.get(
        f"/api/files/{file_data['id']}/measurements/"
    ).json()["features"]
    assert features[0]["source_feature_id"] == "unsupported-track"
    assert features[0]["measurement_status"] == "unsupported"
    assert features[1]["source_feature_id"] == "valid-line"
    assert features[1]["measurement_status"] == "measured"


def test_invalid_kml_fails_with_persisted_processing_status(client):
    response = upload(client, "broken.kml", b"<kml><Placemark>")

    assert response.status_code == 422
    file_id = response.json()["detail"]["file_id"]
    summary = client.get(f"/api/files/{file_id}/").json()
    assert summary["status"] == "failed"
    assert "Invalid KML" in summary["error_message"]


def test_invalid_zip_fails_with_clear_error(client):
    response = upload(client, "missing-shapefile.zip", b"not a zip")

    assert response.status_code == 422
    assert "valid archive" in response.json()["detail"]["message"]


def test_docs_openapi_health_and_missing_file(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/api/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == (
        "Aereo Geospatial Measurement API"
    )
    assert client.get("/api/openapi.json").status_code == 200
    assert client.get("/api/healthz").json() == {"status": "ok"}
    assert client.get("/api/files/999999/").status_code == 404
