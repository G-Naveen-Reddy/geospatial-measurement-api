"""CRS selection and measurement calculation.

Why this module exists
----------------------
Coordinates in EPSG:4326 are in *degrees*, and a degree is not a fixed length
(it shrinks towards the poles). So area/length can never be computed straight
from lat/lon. Strategy used here:

1. Normalise every geometry to EPSG:4326 (done by the caller).
2. Polygon -> area (m2): reproject to a Lambert Azimuthal EQUAL-AREA projection
   centred on the feature itself. Equal-area means area is preserved by
   definition, so there is no systematic error.
   (UTM was rejected for area: it preserves shape, not area, and over- or
   under-states area by roughly 0.1-0.2 % depending on distance from the
   zone's central meridian.)
3. LineString -> length (m): reproject to the UTM zone containing the
   feature's centre. UTM is metric with a scale error of only ~0.05 % inside
   its 6-degree zone, which is fine for lengths.

Edge cases
----------
* Geometry wider than one UTM zone  -> fall back to a geodesic calculation on
  the WGS84 ellipsoid (no projection distortion at all).
* Polar regions (beyond UTM's valid latitudes) -> UPS polar stereographic.
* Invalid polygons (self-intersections) -> repaired with ``make_valid`` first.
* Points / unsupported types -> returned without a measurement, never an error.
"""
from dataclasses import dataclass
from functools import lru_cache

from pyproj import CRS, Geod, Transformer
from shapely import make_valid
from shapely.geometry.base import BaseGeometry
import numpy as np
import shapely

WGS84 = CRS.from_epsg(4326)
_GEOD = Geod(ellps="WGS84")

POLYGONAL = {"Polygon", "MultiPolygon"}
LINEAR = {"LineString", "MultiLineString"}
POINTLIKE = {"Point", "MultiPoint"}


@dataclass
class Measurement:
    type: str | None = None  # "area" | "length" | None
    value: float | None = None
    unit: str | None = None  # "m2" | "m"
    projected_crs: str | None = None  # CRS (or method) used for the calculation
    note: str | None = None


def select_projected_epsg(lon: float, lat: float) -> int:
    """Return the EPSG code of the UTM zone (or UPS polar CRS) covering lon/lat."""
    if lat > 84:
        return 5041  # WGS 84 / UPS North
    if lat < -80:
        return 5042  # WGS 84 / UPS South
    zone = int((lon + 180) // 6) + 1
    zone = min(max(zone, 1), 60)  # lon == 180 would give 61
    return (32600 if lat >= 0 else 32700) + zone


@lru_cache(maxsize=128)
def _utm_transformer(epsg: int) -> Transformer:
    return Transformer.from_crs(WGS84, CRS.from_epsg(epsg), always_xy=True)


@lru_cache(maxsize=256)
def _laea_transformer(lon_0: float, lat_0: float) -> Transformer:
    """Equal-area CRS centred on (lon_0, lat_0). Centre is rounded by the caller so the cache stays small."""
    laea = CRS.from_proj4(f"+proj=laea +lat_0={lat_0} +lon_0={lon_0} +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs")
    return Transformer.from_crs(WGS84, laea, always_xy=True)


def _project(geom: BaseGeometry, transformer: Transformer) -> BaseGeometry:
    def _apply(coords: np.ndarray) -> np.ndarray:
        x, y = transformer.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(geom, _apply)


def measure_geometry(geom: BaseGeometry | None) -> Measurement:
    """Measure a geometry that is expressed in EPSG:4326."""
    if geom is None or geom.is_empty:
        return Measurement(note="Feature has no geometry.")

    geom_type = geom.geom_type
    if geom_type in POINTLIKE:
        return Measurement(note="No measurement is defined for point geometries.")
    if geom_type not in POLYGONAL | LINEAR:
        return Measurement(note=f"Geometry type '{geom_type}' is not supported for measurement.")

    geom = shapely.force_2d(geom)  # KML often carries a Z (altitude); it must not affect 2D area
    notes: list[str] = []

    if geom_type in POLYGONAL and not geom.is_valid:
        geom = make_valid(geom)
        notes.append("Geometry was invalid and was repaired before measuring.")
        # make_valid can return a collection; keep only the polygonal parts
        if geom.geom_type not in POLYGONAL:
            parts = [g for g in getattr(geom, "geoms", []) if g.geom_type in POLYGONAL]
            if not parts:
                return Measurement(note="; ".join(notes + ["No valid area remained after repair."]))
            geom = shapely.union_all(parts)

    minx, miny, maxx, maxy = geom.bounds
    center_lon, center_lat = (minx + maxx) / 2, (miny + maxy) / 2
    measuring_area = geom_type in POLYGONAL

    if (maxx - minx) > 6:
        # Spans more than one UTM zone: a single projection would distort it.
        value = _geodesic_area(geom) if measuring_area else _geodesic_length(geom)
        crs_label = "geodesic (WGS84 ellipsoid)"
        notes.append("Geometry spans more than one UTM zone; measured geodesically instead.")
    elif measuring_area:
        lon_0, lat_0 = round(center_lon, 3), round(center_lat, 3)
        value = _project(geom, _laea_transformer(lon_0, lat_0)).area
        crs_label = f"Lambert Azimuthal Equal Area (lon_0={lon_0}, lat_0={lat_0})"
    else:
        epsg = select_projected_epsg(center_lon, center_lat)
        value = _project(geom, _utm_transformer(epsg)).length
        crs_label = f"EPSG:{epsg}"

    return Measurement(
        type="area" if measuring_area else "length",
        value=round(float(value), 4),
        unit="m2" if measuring_area else "m",
        projected_crs=crs_label,
        note=" ".join(notes) or None,
    )


# ------------------------------------------------------------------ geodesic helpers
def _geodesic_area(geom: BaseGeometry) -> float:
    polygons = geom.geoms if hasattr(geom, "geoms") else [geom]
    return sum(abs(_GEOD.geometry_area_perimeter(p)[0]) for p in polygons)


def _geodesic_length(geom: BaseGeometry) -> float:
    return _GEOD.geometry_length(geom)
