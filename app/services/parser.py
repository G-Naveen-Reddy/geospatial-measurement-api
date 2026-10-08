"""Reads an uploaded .kml or .zip(Shapefile) into a GeoDataFrame.

Everything that can go wrong with user-supplied files is turned into a
``GeoFileError`` with a human-readable message, so the API can answer with a
clean 4xx instead of a stack trace.
"""
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

from app import config


class GeoFileError(Exception):
    """The uploaded file is unusable (bad format, missing CRS, empty, ...)."""


SUPPORTED_EXTENSIONS = (".zip", ".kml")

# GDAL adds these KML display/styling columns to every layer; they are not user data.
_KML_STYLE_COLUMNS = {
    "timestamp", "begin", "end", "altitudeMode", "tessellate", "extrude",
    "visibility", "drawOrder", "icon", "snippet",
}


def load_geodataframe(path: Path) -> gpd.GeoDataFrame:
    """Dispatch on file extension and return features with a defined CRS."""
    suffix = path.suffix.lower()
    if suffix == ".kml":
        gdf = _read_kml(path)
    elif suffix == ".zip":
        gdf = _read_shapefile_zip(path)
    else:
        raise GeoFileError(f"Unsupported file type '{suffix}'. Upload a .zip (Shapefile) or .kml file.")

    if gdf.empty:
        raise GeoFileError("The file was read successfully but contains no features.")
    if gdf.crs is None:
        raise GeoFileError(
            "The file has no coordinate reference system (CRS). "
            "For Shapefiles, include the .prj file in the zip."
        )
    return gdf


# --------------------------------------------------------------------------- KML
def _read_kml(path: Path) -> gpd.GeoDataFrame:
    """KML is always WGS84 (EPSG:4326). A KML can hold several layers (folders)."""
    try:
        layers = [name for name, _ in pyogrio.list_layers(path)]
        frames = []
        for layer in layers:
            df = gpd.read_file(path, layer=layer, engine="pyogrio")
            if df.empty:
                continue
            df = df.drop(columns=[c for c in df.columns if c in _KML_STYLE_COLUMNS])
            # drop attribute columns that are empty for every feature (e.g. an unused 'id')
            empty = [c for c in df.columns if c != df.geometry.name and df[c].isna().all()]
            df = df.drop(columns=empty)
            if len(layers) > 1:
                df["layer"] = layer
            frames.append(df)
    except Exception as exc:  # GDAL raises assorted error types for malformed XML
        raise GeoFileError(f"Could not read KML file: {exc}") from exc

    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    gdf = pd.concat(frames, ignore_index=True)
    return gpd.GeoDataFrame(gdf, geometry="geometry", crs="EPSG:4326")


# ---------------------------------------------------------------------- Shapefile
def _read_shapefile_zip(path: Path) -> gpd.GeoDataFrame:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        _safe_extract(path, tmp_dir)

        shp_files = sorted(p for p in tmp_dir.rglob("*.shp") if not p.name.startswith("._"))
        if not shp_files:
            raise GeoFileError("No .shp file found inside the zip archive.")
        if len(shp_files) > 1:
            names = ", ".join(p.name for p in shp_files)
            raise GeoFileError(f"The zip contains {len(shp_files)} Shapefiles ({names}); upload one at a time.")

        shp = shp_files[0]
        if not shp.with_suffix(".dbf").exists():
            raise GeoFileError("The Shapefile is incomplete: the matching .dbf file is missing.")
        try:
            # Read fully while the temp dir still exists
            return gpd.read_file(shp, engine="pyogrio")
        except Exception as exc:
            raise GeoFileError(f"Could not read Shapefile: {exc}") from exc


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract a zip defensively: no path traversal ('zip slip'), no zip bombs."""
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise GeoFileError("The uploaded file is not a valid zip archive.") from exc

    with archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > config.MAX_UNZIPPED_BYTES:
            raise GeoFileError("The zip archive is too large when uncompressed.")

        dest_resolved = dest.resolve()
        for info in archive.infolist():
            target = (dest / info.filename).resolve()
            if dest_resolved != target and dest_resolved not in target.parents:
                raise GeoFileError("The zip archive contains an unsafe file path.")
        archive.extractall(dest)
