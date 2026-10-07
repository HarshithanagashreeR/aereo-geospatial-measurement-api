"""File validation, geospatial ingestion, CRS selection, and measurements."""

from __future__ import annotations

import json
import logging
import math
import zipfile
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree

import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely.geometry import (
    GeometryCollection,
    LineString,
    LinearRing,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
)
from shapely.validation import make_valid


MAX_UNCOMPRESSED_ZIP_BYTES = 256 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 200
GEOGRAPHIC_CRS = "EPSG:4326"
logger = logging.getLogger(__name__)


class GeospatialProcessingError(ValueError):
    """Raised when an upload cannot be decoded or safely processed."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_descendant(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    return next((child for child in element.iter() if _local_name(child.tag) == name), None)


def _coordinates(element: ElementTree.Element | None) -> list[tuple[float, ...]]:
    if element is None or not element.text:
        return []
    points: list[tuple[float, ...]] = []
    for raw_point in element.text.split():
        try:
            values = tuple(float(value) for value in raw_point.split(",") if value != "")
        except ValueError as exc:
            raise GeospatialProcessingError("KML contains invalid coordinates.") from exc
        if len(values) < 2 or not all(math.isfinite(value) for value in values):
            raise GeospatialProcessingError("KML contains an invalid coordinate tuple.")
        points.append(values[:3])
    return points


def _parse_kml_geometry(element: ElementTree.Element):
    kind = _local_name(element.tag)
    if kind == "Point":
        coords = _coordinates(_first_descendant(element, "coordinates"))
        return Point(coords[0]) if coords else None
    if kind == "LineString":
        coords = _coordinates(_first_descendant(element, "coordinates"))
        return LineString(coords) if len(coords) >= 2 else None
    if kind == "LinearRing":
        coords = _coordinates(_first_descendant(element, "coordinates"))
        return LinearRing(coords) if len(coords) >= 3 else None
    if kind == "Polygon":
        outer = next(
            (
                node
                for node in element.iter()
                if _local_name(node.tag) == "outerBoundaryIs"
            ),
            None,
        )
        shell = _coordinates(_first_descendant(outer, "coordinates")) if outer is not None else []
        if len(shell) < 3:
            return None
        holes = []
        for boundary in element.iter():
            if _local_name(boundary.tag) == "innerBoundaryIs":
                ring = _coordinates(_first_descendant(boundary, "coordinates"))
                if len(ring) >= 3:
                    holes.append(ring)
        return Polygon(shell, holes)
    if kind == "MultiGeometry":
        children = [
            geometry
            for child in element
            if (geometry := _parse_kml_geometry(child)) is not None
        ]
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        if all(isinstance(child, Point) for child in children):
            return MultiPoint(children)
        if all(isinstance(child, (LineString, LinearRing)) for child in children):
            return MultiLineString([list(child.coords) for child in children])
        if all(isinstance(child, Polygon) for child in children):
            return MultiPolygon(children)
        return GeometryCollection(children)
    return GeometryCollection()


def _read_kml_xml(path: str) -> gpd.GeoDataFrame:
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, OSError) as exc:
        raise GeospatialProcessingError(f"Invalid KML document: {exc}") from exc

    records: list[dict[str, Any]] = []
    for placemark in (node for node in root.iter() if _local_name(node.tag) == "Placemark"):
        geometry_element = next(
            (
                node
                for node in placemark
                if _local_name(node.tag)
                in {
                    "Point",
                    "LineString",
                    "LinearRing",
                    "Polygon",
                    "MultiGeometry",
                    "Track",
                    "MultiTrack",
                    "Model",
                }
            ),
            None,
        )
        geometry = _parse_kml_geometry(geometry_element) if geometry_element is not None else None
        if geometry is None:
            geometry = GeometryCollection()

        properties: dict[str, Any] = {}
        for child in placemark:
            child_name = _local_name(child.tag)
            if child_name in {"name", "description"}:
                properties[child_name] = child.text
            elif child_name == "ExtendedData":
                for data_node in child.iter():
                    data_name = _local_name(data_node.tag)
                    if data_name == "Data" and data_node.get("name"):
                        value_node = _first_descendant(data_node, "value")
                        properties[data_node.get("name", "")] = (
                            value_node.text if value_node is not None else None
                        )
                    elif data_name == "SimpleData" and data_node.get("name"):
                        properties[data_node.get("name", "")] = data_node.text
        placemark_id = placemark.get("id")
        records.append(
            {
                "_source_feature_id": placemark_id or str(len(records)),
                "geometry": geometry,
                "properties": properties,
            }
        )

    if not records:
        raise GeospatialProcessingError("KML contains no Placemark geometries.")
    frame = gpd.GeoDataFrame(records, geometry="geometry", crs=GEOGRAPHIC_CRS)
    return frame


def _read_kml_with_gdal(
    path: str, metadata_frame: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Use GeoPandas/GDAL for standard KML geometry and XML for source metadata."""
    try:
        driver_frame = gpd.read_file(path, engine="pyogrio")
    except Exception as exc:
        logger.warning(
            "GeoPandas/GDAL could not read KML; using the XML fallback: %s", exc
        )
        return metadata_frame

    if driver_frame.empty or len(driver_frame) != len(metadata_frame):
        logger.warning(
            "GeoPandas/GDAL returned %s KML features for %s Placemarks; "
            "using the XML fallback.",
            len(driver_frame),
            len(metadata_frame),
        )
        return metadata_frame

    source_ids = [str(value) for value in metadata_frame["_source_feature_id"]]
    driver_geometries = list(driver_frame.geometry)
    if "id" in driver_frame.columns:
        driver_ids = [
            None if value is None or pd.isna(value) else str(value)
            for value in driver_frame["id"]
        ]
        if (
            None not in driver_ids
            and len(set(driver_ids)) == len(driver_ids)
            and len(set(source_ids)) == len(source_ids)
            and set(driver_ids) == set(source_ids)
        ):
            geometry_by_id = dict(zip(driver_ids, driver_geometries))
            driver_geometries = [geometry_by_id[source_id] for source_id in source_ids]

    merged_geometries = []
    for source_geometry, driver_geometry in zip(
        metadata_frame.geometry, driver_geometries
    ):
        if (
            source_geometry is None
            or source_geometry.is_empty
            or source_geometry.geom_type
            not in {
                "Point",
                "LineString",
                "Polygon",
                "MultiPoint",
                "MultiLineString",
                "MultiPolygon",
            }
        ):
            # Keep XML representations for empty and unsupported geometries.
            merged_geometries.append(source_geometry)
        elif (
            driver_geometry is None
            or driver_geometry.is_empty
            or driver_geometry.geom_type != source_geometry.geom_type
        ):
            logger.warning(
                "GeoPandas/GDAL changed a KML geometry type; using the XML fallback."
            )
            return metadata_frame
        else:
            merged_geometries.append(driver_geometry)

    geometry_crs = driver_frame.crs or metadata_frame.crs
    attributes = metadata_frame.drop(columns=metadata_frame.geometry.name)
    return gpd.GeoDataFrame(
        attributes,
        geometry=gpd.GeoSeries(
            merged_geometries, index=metadata_frame.index, crs=geometry_crs
        ),
        crs=geometry_crs,
    )


def _validate_zip(path: str) -> str:
    if not zipfile.is_zipfile(path):
        raise GeospatialProcessingError("The uploaded ZIP file is not a valid archive.")
    try:
        with zipfile.ZipFile(path) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            total_size = sum(member.file_size for member in members)
            if total_size > MAX_UNCOMPRESSED_ZIP_BYTES:
                raise GeospatialProcessingError(
                    "The ZIP archive expands beyond the 256 MiB processing limit."
                )
            for member in members:
                member_path = PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise GeospatialProcessingError(
                        "The ZIP archive contains an unsafe path."
                    )
                if member.compress_size and member.file_size / member.compress_size > MAX_ZIP_COMPRESSION_RATIO:
                    raise GeospatialProcessingError(
                        "The ZIP archive has an unsafe compression ratio."
                    )
            shapefiles = [
                member.filename
                for member in members
                if PurePosixPath(member.filename).suffix.lower() == ".shp"
            ]
            if not shapefiles:
                raise GeospatialProcessingError(
                    "The ZIP archive must contain a Shapefile (.shp)."
                )
            if len(shapefiles) > 1:
                raise GeospatialProcessingError(
                    "The ZIP archive contains multiple Shapefiles; upload one dataset per archive."
                )

            shapefile = PurePosixPath(shapefiles[0])
            shapefile_stem = shapefile.with_suffix("").as_posix().casefold()
            if not any(
                PurePosixPath(member.filename).suffix.lower() == ".shx"
                and PurePosixPath(member.filename).with_suffix("").as_posix().casefold()
                == shapefile_stem
                for member in members
            ):
                raise GeospatialProcessingError(
                    "The Shapefile is incomplete: a matching .shx index file is required."
                )
            if not any(
                PurePosixPath(member.filename).suffix.lower() == ".dbf"
                and PurePosixPath(member.filename).with_suffix("").as_posix().casefold()
                == shapefile_stem
                for member in members
            ):
                raise GeospatialProcessingError(
                    "The Shapefile is incomplete: a matching .dbf attribute file is required."
                )
            return shapefiles[0]
    except zipfile.BadZipFile as exc:
        raise GeospatialProcessingError("The uploaded ZIP file is not a valid archive.") from exc


def read_geodataframe(path: str, file_format: str) -> gpd.GeoDataFrame:
    """Read KML or a ZIP containing exactly one Shapefile into a GeoDataFrame."""
    if file_format == "kml":
        # Keep IDs and ExtendedData from the source XML, while using GDAL for
        # normal KML geometry. Some KML extensions are not GDAL-readable, so
        # retain the XML geometry fallback for those files.
        metadata_frame = _read_kml_xml(path)
        return _read_kml_with_gdal(path, metadata_frame)

    if file_format == "zip":
        shapefile = _validate_zip(path)
        source = f"zip://{path}!{shapefile}"
        try:
            frame = gpd.read_file(source)
        except Exception as exc:
            raise GeospatialProcessingError(
                f"Could not read the Shapefile in the ZIP archive: {exc}"
            ) from exc
        if frame.empty:
            raise GeospatialProcessingError("The Shapefile contains no features.")
        return frame

    raise GeospatialProcessingError("Only .kml and .zip uploads are supported.")


def _metric_unit(crs: CRS) -> bool:
    return bool(
        crs.axis_info
        and all(
            axis.unit_name.lower() in {"metre", "meter", "metres", "meters"}
            and abs((axis.unit_conversion_factor or 0) - 1.0) < 1e-9
            for axis in crs.axis_info[:2]
        )
    )


def choose_measurement_crs(frame: gpd.GeoDataFrame) -> CRS | None:
    """Choose a meter-based projected CRS; prefer the dataset's UTM estimate."""
    if frame.crs is None or frame.empty:
        return None
    source_crs = CRS.from_user_input(frame.crs)
    if source_crs.is_projected and _metric_unit(source_crs):
        return source_crs

    usable = frame.loc[
        [geometry is not None and not geometry.is_empty for geometry in frame.geometry]
    ]
    if usable.empty:
        return None
    try:
        estimated = usable.estimate_utm_crs()
        if estimated is not None:
            estimated_crs = CRS.from_user_input(estimated)
            if estimated_crs.is_projected and _metric_unit(estimated_crs):
                return estimated_crs
    except (ValueError, RuntimeError, OverflowError):
        pass

    # For very broad datasets where a UTM zone cannot be estimated, use a
    # local azimuthal-equidistant projection centered on the data extent.
    try:
        geographic = usable.to_crs(GEOGRAPHIC_CRS)
        min_x, min_y, max_x, max_y = geographic.total_bounds
        if not all(math.isfinite(value) for value in (min_x, min_y, max_x, max_y)):
            return None
        longitude = (min_x + max_x) / 2
        latitude = max(-89.0, min(89.0, (min_y + max_y) / 2))
        return CRS.from_proj4(
            f"+proj=aeqd +lat_0={latitude:.8f} +lon_0={longitude:.8f} "
            "+datum=WGS84 +units=m +no_defs"
        )
    except (ValueError, RuntimeError, OverflowError):
        return None


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "item"):
        return _json_safe(value.item())
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if pd.isna(value) if not isinstance(value, (list, tuple, dict)) else False:
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _feature_id(row: pd.Series, index: Any) -> str:
    properties = row.get("properties")
    if isinstance(properties, dict):
        for key in ("id", "ID", "FID", "OBJECTID", "OBJECT_ID"):
            value = properties.get(key)
            if value is not None and not pd.isna(value):
                return str(value)
    source_id = row.get("_source_feature_id")
    if source_id is not None and not pd.isna(source_id):
        return str(source_id)
    return str(index)


def process_geodataframe(frame: gpd.GeoDataFrame) -> dict[str, Any]:
    """Normalize features and calculate polygon/line measures in meters."""
    if frame.empty:
        raise GeospatialProcessingError("The uploaded dataset contains no features.")

    source_crs = CRS.from_user_input(frame.crs).to_string() if frame.crs else None
    geometries = list(frame.geometry)
    measurement_needed = any(
        geometry is not None
        and not geometry.is_empty
        and geometry.geom_type in {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}
        for geometry in geometries
    )
    measurement_crs = choose_measurement_crs(frame) if measurement_needed else None

    measured_geometries: list[Any] = []
    if measurement_crs is not None and source_crs is not None:
        measurement_frame = frame.copy()
        repaired = [
            make_valid(geometry) if geometry is not None and not geometry.is_valid else geometry
            for geometry in geometries
        ]
        measurement_frame = measurement_frame.set_geometry(
            gpd.GeoSeries(repaired, index=measurement_frame.index, crs=frame.crs)
        )
        measured_geometries = list(
            measurement_frame.to_crs(measurement_crs).geometry
        )

    output_features: list[dict[str, Any]] = []
    warnings = False
    for position, (index, row) in enumerate(frame.iterrows()):
        geometry = geometries[position]
        geometry_type = geometry.geom_type if geometry is not None else "Unknown"
        properties = row.get("properties")
        if not isinstance(properties, dict):
            properties = {
                str(key): _json_safe(value)
                for key, value in row.drop(labels=[frame.geometry.name], errors="ignore").items()
                if key != "_source_feature_id"
            }
        else:
            properties = _json_safe(properties)

        value: float | None = None
        unit: str | None = None
        note: str | None = None
        if geometry is None or geometry.is_empty:
            status = "unsupported"
            note = "The feature has no usable geometry."
            warnings = True
        elif geometry_type == "Point" or geometry_type == "MultiPoint":
            status = "not_required"
            note = "Point features do not require area or length measurements."
        elif geometry_type not in {
            "Polygon",
            "MultiPolygon",
            "LineString",
            "MultiLineString",
        }:
            status = "unsupported"
            note = f"Measurements are not defined for {geometry_type} geometry."
            warnings = True
        elif source_crs is None:
            status = "missing_crs"
            note = "The source CRS is missing; no measurement was calculated."
            warnings = True
        elif measurement_crs is None or not measured_geometries:
            status = "crs_unavailable"
            note = "A safe projected measurement CRS could not be selected."
            warnings = True
        else:
            metric_geometry = measured_geometries[position]
            if metric_geometry.geom_type in {"Polygon", "MultiPolygon"}:
                value = float(metric_geometry.area)
                unit = "square_meters"
            elif metric_geometry.geom_type in {"LineString", "MultiLineString"}:
                value = float(metric_geometry.length)
                unit = "meters"
            else:
                status = "unsupported"
                note = (
                    "The geometry became a mixed GeometryCollection during "
                    "repair and was not measured."
                )
                warnings = True
                output_features.append(
                    {
                        "source_feature_id": _feature_id(row, index),
                        "geometry_type": geometry_type,
                        "geometry": _json_safe(mapping(geometry)),
                        "properties": properties,
                        "measurement_value": None,
                        "measurement_unit": None,
                        "measurement_status": status,
                        "measurement_note": note,
                    }
                )
                continue
            status = "measured"

        output_features.append(
            {
                "source_feature_id": _feature_id(row, index),
                "geometry_type": geometry_type,
                "geometry": _json_safe(mapping(geometry)) if geometry is not None else None,
                "properties": properties,
                "measurement_value": value,
                "measurement_unit": unit,
                "measurement_status": status,
                "measurement_note": note,
            }
        )

    return {
        "source_crs": source_crs,
        "measurement_crs": (
            measurement_crs.to_string() if measurement_crs is not None else None
        ),
        "features": output_features,
        "status": "completed_with_warnings" if warnings else "completed",
    }


def process_file(path: str, file_format: str) -> dict[str, Any]:
    return process_geodataframe(read_geodataframe(path, file_format))
