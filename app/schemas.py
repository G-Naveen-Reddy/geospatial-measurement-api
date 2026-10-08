"""Pydantic response models (these also drive the auto-generated /docs page)."""
from typing import Any

from pydantic import BaseModel


class FileInfo(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str | None
    status: str  # PROCESSING | COMPLETED | FAILED
    error: str | None = None


class MeasurementOut(BaseModel):
    type: str  # "area" | "length"
    value: float
    unit: str  # "m2" | "m"
    projected_crs: str


class FeatureMeasurement(BaseModel):
    feature_index: int
    geometry_type: str | None
    crs: str | None  # CRS of the source file
    geometry: dict[str, Any] | None  # GeoJSON, always in EPSG:4326
    properties: dict[str, Any]
    measurement: MeasurementOut | None
    note: str | None = None


class MeasurementSummary(BaseModel):
    total_area_m2: float
    total_length_m: float
    measured_features: int
    unmeasured_features: int


class MeasurementsResponse(BaseModel):
    file_id: str
    crs: str | None
    feature_count: int
    limit: int
    offset: int
    summary: MeasurementSummary
    features: list[FeatureMeasurement]
