"""Test setup: isolated temp DB/upload dir, plus helpers that build sample geo files."""
import io
import os
import tempfile
import zipfile
from pathlib import Path

# Must be set BEFORE the app is imported so it picks up the temp locations.
_TMP = tempfile.mkdtemp(prefix="geo-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["UPLOAD_DIR"] = f"{_TMP}/uploads"

import geopandas as gpd  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from shapely.geometry import Polygon  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


# ~1 km x 1 km square near Bengaluru (lon 77.59, lat 12.97), in EPSG:4326
BENGALURU_SQUARE = Polygon(
    [(77.590, 12.970), (77.599, 12.970), (77.599, 12.979), (77.590, 12.979), (77.590, 12.970)]
)

KML_MIXED = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
  <Placemark><name>Plot A</name><description>survey plot</description>
    <Polygon><outerBoundaryIs><LinearRing><coordinates>
      77.590,12.970,900 77.599,12.970,900 77.599,12.979,900 77.590,12.979,900 77.590,12.970,900
    </coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
  <Placemark><name>Road 1</name>
    <LineString><coordinates>77.590,12.970,0 77.600,12.970,0</coordinates></LineString></Placemark>
  <Placemark><name>Well</name>
    <Point><coordinates>77.595,12.975,0</coordinates></Point></Placemark>
</Document></kml>
"""


def make_shapefile_zip(gdf: gpd.GeoDataFrame, include_prj: bool = True) -> bytes:
    """Write ``gdf`` as a Shapefile and return it zipped, as bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        shp = Path(tmp) / "survey.shp"
        gdf.to_file(shp, driver="ESRI Shapefile")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for p in Path(tmp).iterdir():
                if p.suffix == ".prj" and not include_prj:
                    continue
                zf.write(p, p.name)
        return buf.getvalue()


def upload(client, name: str, content: bytes, mime: str = "application/octet-stream"):
    return client.post("/api/files/", files={"file": (name, content, mime)})
