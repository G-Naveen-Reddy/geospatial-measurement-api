"""Compare three ways of measuring the same polygon's area.

Run from the project root:   python scripts/compare_area_methods.py

- geodesic : pyproj.Geod on the WGS84 ellipsoid (used here as the reference value)
- UTM      : project to the UTM zone, then take .area
- equal-area: what this project actually uses for polygons (see app/services/measure.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import shapely
from pyproj import Geod, Transformer
from shapely.geometry import Polygon

from app.services.measure import measure_geometry, select_projected_epsg

# roughly 1 km x 1 km plot in Bengaluru, in lon/lat degrees
plot = Polygon([(77.590, 12.970), (77.599, 12.970), (77.599, 12.979), (77.590, 12.979)])

reference = abs(Geod(ellps="WGS84").geometry_area_perimeter(plot)[0])

lon, lat = plot.centroid.x, plot.centroid.y
epsg = select_projected_epsg(lon, lat)
utm = Transformer.from_crs(4326, epsg, always_xy=True)
utm_area = shapely.transform(
    plot, lambda c: np.column_stack(utm.transform(c[:, 0], c[:, 1]))
).area

equal_area = measure_geometry(plot).value

print(f"Area in raw degrees (wrong!) : {plot.area:.10f}")
print(f"Geodesic reference           : {reference:,.2f} m2")
print(f"UTM (EPSG:{epsg})            : {utm_area:,.2f} m2   difference {100 * (utm_area - reference) / reference:+.3f} %")
print(f"Equal-area (used here)       : {equal_area:,.2f} m2   difference {100 * (equal_area - reference) / reference:+.3f} %")