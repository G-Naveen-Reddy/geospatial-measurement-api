"""Orchestrates: parse file -> normalise to EPSG:4326 -> measure -> persist."""
import json
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from shapely.geometry import mapping
from sqlalchemy.orm import Session

from app.models import Feature, UploadedFile
from app.services.measure import measure_geometry
from app.services.parser import GeoFileError, load_geodataframe


def describe_crs(crs) -> str:
    """'EPSG:xxxx' when the CRS has an EPSG code, otherwise its name."""
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.name


def process_file(db: Session, record: UploadedFile, path: Path) -> UploadedFile:
    """Fill ``record`` and its features. Never raises: failures become status=FAILED."""
    try:
        gdf = load_geodataframe(path)
        record.crs = describe_crs(gdf.crs)

        # All measuring happens on WGS84 geometries; reproject if the source is e.g. UTM/State Plane.
        gdf_wgs84 = gdf if gdf.crs.to_epsg() == 4326 else gdf.to_crs(4326)
        property_columns = [c for c in gdf.columns if c != gdf.geometry.name]

        features = []
        for index, (geom, row) in enumerate(zip(gdf_wgs84.geometry, gdf[property_columns].to_dict("records"))):
            result = measure_geometry(geom)
            has_geom = geom is not None and not geom.is_empty
            features.append(
                Feature(
                    file_id=record.id,
                    index=index,
                    geometry_type=geom.geom_type if has_geom else None,
                    geometry_json=json.dumps(mapping(geom)) if has_geom else None,
                    properties_json=json.dumps({k: _to_json_safe(v) for k, v in row.items()}),
                    measurement_type=result.type,
                    measurement_value=result.value,
                    measurement_unit=result.unit,
                    projected_crs=result.projected_crs,
                    note=result.note,
                )
            )
        db.add_all(features)
        record.feature_count = len(features)
        record.status = "COMPLETED"
    except GeoFileError as exc:
        record.status, record.error = "FAILED", str(exc)
    except Exception as exc:  # unexpected: still don't crash the request
        record.status, record.error = "FAILED", f"Unexpected processing error: {exc}"
    db.commit()
    return record


def _to_json_safe(value):
    """Attribute tables contain numpy/pandas types and NaN that json can't serialise."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, np.generic):
        return _to_json_safe(value.item())
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
