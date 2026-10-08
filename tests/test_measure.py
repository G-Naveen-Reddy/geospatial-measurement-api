"""Unit tests for the CRS selection + measurement logic (no HTTP involved)."""
import pytest
from pyproj import Geod
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

from app.services.measure import measure_geometry, select_projected_epsg
from tests.conftest import BENGALURU_SQUARE

GEOD = Geod(ellps="WGS84")


@pytest.mark.parametrize(
    "lon, lat, expected",
    [
        (77.59, 12.97, 32643),    # Bengaluru -> UTM 43N
        (151.2, -33.9, 32756),    # Sydney    -> UTM 56S
        (-0.12, 51.5, 32630),     # London    -> UTM 30N
        (180.0, 10.0, 32660),     # edge of the world must not become zone 61
        (0.0, 89.0, 5041),        # Arctic -> UPS North
        (0.0, -85.0, 5042),       # Antarctic -> UPS South
    ],
)
def test_select_projected_epsg(lon, lat, expected):
    assert select_projected_epsg(lon, lat) == expected


def test_polygon_area_matches_independent_geodesic_value():
    result = measure_geometry(BENGALURU_SQUARE)
    truth = abs(GEOD.geometry_area_perimeter(BENGALURU_SQUARE)[0])
    assert result.type == "area" and result.unit == "m2"
    assert result.projected_crs.startswith("Lambert Azimuthal Equal Area")
    assert result.value == pytest.approx(truth, rel=1e-4)  # equal-area: within 0.01 %
    assert 900_000 < result.value < 1_100_000                # ~1 km2, not ~8e-5 'degrees squared'


def test_line_length_uses_utm_zone_of_the_feature():
    result = measure_geometry(LineString([(77.59, 12.97), (77.60, 12.97)]))
    assert result.projected_crs == "EPSG:32643"


def test_line_length_matches_independent_geodesic_value():
    line = LineString([(77.59, 12.97), (77.60, 12.97)])
    result = measure_geometry(line)
    truth = GEOD.geometry_length(line)
    assert result.type == "length" and result.unit == "m"
    assert result.value == pytest.approx(truth, rel=0.001)


def test_same_degree_size_gives_smaller_area_near_poles():
    """Proves we're not using raw degrees: a 1x1 degree box shrinks with latitude."""
    equator = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
    arctic = Polygon([(0, 70), (1, 70), (1, 71), (0, 71)])
    assert measure_geometry(arctic).value < measure_geometry(equator).value * 0.4


def test_polygon_with_hole_subtracts_the_hole():
    outer = [(77.590, 12.970), (77.599, 12.970), (77.599, 12.979), (77.590, 12.979)]
    hole = [(77.593, 12.973), (77.596, 12.973), (77.596, 12.976), (77.593, 12.976)]
    donut = measure_geometry(Polygon(outer, [hole])).value
    solid = measure_geometry(Polygon(outer)).value
    plug = measure_geometry(Polygon(hole)).value
    assert donut == pytest.approx(solid - plug, rel=1e-3)


def test_multipolygon_area_is_sum_of_parts():
    a = BENGALURU_SQUARE
    b = Polygon([(77.61, 12.970), (77.619, 12.970), (77.619, 12.979), (77.61, 12.979)])
    total = measure_geometry(MultiPolygon([a, b])).value
    assert total == pytest.approx(measure_geometry(a).value + measure_geometry(b).value, rel=1e-6)


def test_z_coordinates_do_not_change_the_result():
    flat = Polygon([(77.590, 12.970), (77.599, 12.970), (77.599, 12.979), (77.590, 12.979)])
    with_z = Polygon([(77.590, 12.970, 900), (77.599, 12.970, 900), (77.599, 12.979, 900), (77.590, 12.979, 900)])
    assert measure_geometry(with_z).value == pytest.approx(measure_geometry(flat).value)


def test_geometry_wider_than_one_utm_zone_uses_geodesic():
    wide = Polygon([(70, 10), (90, 10), (90, 20), (70, 20)])
    result = measure_geometry(wide)
    truth = abs(GEOD.geometry_area_perimeter(wide)[0])
    assert result.projected_crs == "geodesic (WGS84 ellipsoid)"
    assert "more than one UTM zone" in result.note
    assert result.value == pytest.approx(truth, rel=1e-6)


def test_invalid_bowtie_polygon_is_repaired_not_crashed():
    bowtie = Polygon([(77.590, 12.970), (77.599, 12.979), (77.599, 12.970), (77.590, 12.979)])
    assert not bowtie.is_valid
    result = measure_geometry(bowtie)
    assert result.type == "area" and result.value > 0
    assert "repaired" in result.note


def test_point_has_no_measurement_and_no_error():
    result = measure_geometry(Point(77.59, 12.97))
    assert result.type is None and result.value is None
    assert "point" in result.note.lower()


@pytest.mark.parametrize("geom", [None, Polygon()])
def test_missing_or_empty_geometry_is_handled(geom):
    result = measure_geometry(geom)
    assert result.value is None and result.note
