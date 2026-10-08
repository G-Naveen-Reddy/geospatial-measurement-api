"""End-to-end API tests: upload real (generated) files and inspect the responses."""
import geopandas as gpd
import pytest
from pyproj import Geod
from shapely.geometry import LineString

from tests.conftest import BENGALURU_SQUARE, KML_MIXED, make_shapefile_zip, upload

TRUE_AREA = abs(Geod(ellps="WGS84").geometry_area_perimeter(BENGALURU_SQUARE)[0])


# ------------------------------------------------------------------ KML
def test_upload_kml_returns_file_info(client):
    r = upload(client, "survey.kml", KML_MIXED.encode())
    assert r.status_code == 201
    body = r.json()
    assert body["filename"] == "survey.kml"
    assert body["feature_count"] == 3
    assert body["crs"] == "EPSG:4326"
    assert body["status"] == "COMPLETED"

    again = client.get(f"/api/files/{body['id']}/")
    assert again.status_code == 200 and again.json() == body


def test_kml_measurements_polygon_line_and_point(client):
    file_id = upload(client, "survey.kml", KML_MIXED.encode()).json()["id"]
    data = client.get(f"/api/files/{file_id}/measurements/").json()

    by_type = {f["geometry_type"]: f for f in data["features"]}
    assert set(by_type) == {"Polygon", "LineString", "Point"}

    poly = by_type["Polygon"]
    assert poly["measurement"]["type"] == "area"
    assert poly["measurement"]["value"] == pytest.approx(TRUE_AREA, rel=1e-4)
    assert poly["measurement"]["projected_crs"].startswith("Lambert Azimuthal Equal Area")
    assert poly["properties"] == {"Name": "Plot A", "description": "survey plot"}  # no GDAL styling noise
    assert poly["crs"] == "EPSG:4326"
    assert poly["geometry"]["type"] == "Polygon"

    line = by_type["LineString"]
    assert line["measurement"]["type"] == "length"
    assert line["measurement"]["projected_crs"] == "EPSG:32643"
    assert 1_000 < line["measurement"]["value"] < 1_200  # 0.01 deg of longitude at 13 N is ~1.08 km

    point = by_type["Point"]
    assert point["measurement"] is None
    assert point["note"]  # explains why there is no measurement

    summary = data["summary"]
    assert summary["measured_features"] == 2 and summary["unmeasured_features"] == 1
    assert summary["total_area_m2"] == pytest.approx(poly["measurement"]["value"])


def test_measurements_pagination(client):
    file_id = upload(client, "survey.kml", KML_MIXED.encode()).json()["id"]
    page = client.get(f"/api/files/{file_id}/measurements/?limit=1&offset=1").json()
    assert [f["feature_index"] for f in page["features"]] == [1]
    assert page["feature_count"] == 3  # totals describe the whole file, not the page


# ------------------------------------------------------------------ Shapefile
def test_upload_shapefile_zip_in_wgs84(client):
    gdf = gpd.GeoDataFrame({"plot": ["A"], "owner": ["Ravi"]}, geometry=[BENGALURU_SQUARE], crs="EPSG:4326")
    r = upload(client, "plots.zip", make_shapefile_zip(gdf), "application/zip")
    assert r.status_code == 201 and r.json()["crs"] == "EPSG:4326"

    feat = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["features"][0]
    assert feat["measurement"]["value"] == pytest.approx(TRUE_AREA, rel=1e-4)
    assert feat["properties"] == {"plot": "A", "owner": "Ravi"}


def test_projected_shapefile_gives_same_area_as_geographic_one(client):
    """A file already in UTM metres must be normalised and measured consistently."""
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[BENGALURU_SQUARE], crs="EPSG:4326").to_crs(32643)
    r = upload(client, "utm.zip", make_shapefile_zip(gdf), "application/zip")
    assert r.status_code == 201 and r.json()["crs"] == "EPSG:32643"

    feat = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["features"][0]
    assert feat["crs"] == "EPSG:32643"                       # source CRS is reported...
    assert feat["geometry"]["coordinates"][0][0][0] < 180    # ...geometry is returned as lon/lat
    assert feat["measurement"]["value"] == pytest.approx(TRUE_AREA, rel=1e-4)


def test_shapefile_linestrings(client):
    gdf = gpd.GeoDataFrame(
        {"name": ["r1"]}, geometry=[LineString([(77.59, 12.97), (77.60, 12.97)])], crs="EPSG:4326"
    )
    r = upload(client, "roads.zip", make_shapefile_zip(gdf), "application/zip")
    feat = client.get(f"/api/files/{r.json()['id']}/measurements/").json()["features"][0]
    assert feat["measurement"]["type"] == "length"


# ------------------------------------------------------------------ error handling
def test_shapefile_without_prj_is_rejected_with_clear_message(client):
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[BENGALURU_SQUARE], crs="EPSG:4326")
    r = upload(client, "noprj.zip", make_shapefile_zip(gdf, include_prj=False), "application/zip")
    assert r.status_code == 422
    assert "CRS" in r.json()["detail"]

    # the failure is recorded and visible through the normal endpoints
    info = client.get(f"/api/files/{r.json()['id']}/").json()
    assert info["status"] == "FAILED" and "CRS" in info["error"]
    assert client.get(f"/api/files/{r.json()['id']}/measurements/").status_code == 409


def test_unsupported_extension_is_rejected(client):
    assert upload(client, "notes.txt", b"hello").status_code == 400


def test_corrupt_zip_is_rejected(client):
    r = upload(client, "broken.zip", b"this is not a zip")
    assert r.status_code == 422 and "zip" in r.json()["detail"].lower()


def test_zip_without_shapefile_is_rejected(client):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "no shapes here")
    r = upload(client, "empty.zip", buf.getvalue())
    assert r.status_code == 422 and ".shp" in r.json()["detail"]


def test_zip_slip_path_traversal_is_blocked(client):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.shp", "x")
    r = upload(client, "evil.zip", buf.getvalue())
    assert r.status_code == 422 and "unsafe" in r.json()["detail"]


def test_malformed_kml_is_rejected(client):
    r = upload(client, "bad.kml", b"<kml><oops")
    assert r.status_code == 422


def test_unknown_file_id_returns_404(client):
    assert client.get("/api/files/doesnotexist/").status_code == 404
    assert client.get("/api/files/doesnotexist/measurements/").status_code == 404
