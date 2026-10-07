"""Public Pydantic response schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class FileSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_filename: str
    content_type: str | None
    format: str
    size_bytes: int
    status: str
    source_crs: str | None
    measurement_crs: str | None
    feature_count: int
    error_message: str | None
    uploaded_at: datetime


class FeatureMeasurement(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_feature_id: str
    geometry_type: str
    geometry: dict[str, Any] | None
    properties: dict[str, Any]
    measurement_value: float | None
    measurement_unit: str | None
    measurement_status: str
    measurement_note: str | None


class MeasurementsResponse(BaseModel):
    file: FileSummary
    source_crs: str | None
    measurement_crs: str | None
    features: list[FeatureMeasurement]
