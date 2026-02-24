import csv
import json
import os
from pyproj import Transformer

with open('grid.csv') as f:
    rows = list(csv.DictReader(f))

S = 26  # pixel grid size
pad = 2
cell_m = 2200  # metres per cell

# Cell 1 is at grid col=17, row=45. Its UTM easting/northing = 395495, 1649670
# UTM origin: bottom-left of cell 1
# In SVG, Y increases downward; in UTM, northing increases upward
# Cell 1 SVG: x=444, y=1172 -> col=17, row=45

ref_col, ref_row = 17, 45
# ref_easting, ref_northing = 395492, 1649667
# ref_easting, ref_northing = 395493, 1649668
ref_easting, ref_northing = 395495, 1649670

# Goa is UTM zone 43N
transformer = Transformer.from_crs("EPSG:32643", "EPSG:4326", always_xy=True)

features_4326 = []
features_32643 = []
for r in rows:
    cell_id = r['ID']
    px, py = int(r['X']), int(r['Y'])
    pw, ph = int(r['Width']), int(r['Height'])

    col = (px - pad) // S
    row = (py - pad) // S
    span_c = pw // S
    span_r = ph // S

    # UTM coords: col increases right (easting+), row increases down (northing-)
    e_left = ref_easting + (col - ref_col) * cell_m
    e_right = e_left + span_c * cell_m
    n_top = ref_northing + (ref_row - row) * cell_m
    n_bottom = n_top - span_r * cell_m

    utm_coords = [
        [e_left, n_bottom], [e_right, n_bottom],
        [e_right, n_top], [e_left, n_top],
        [e_left, n_bottom],
    ]

    # Convert corners to lon/lat
    lonlat_coords = [
        transformer.transform(e_left, n_bottom),
        transformer.transform(e_right, n_bottom),
        transformer.transform(e_right, n_top),
        transformer.transform(e_left, n_top),
        transformer.transform(e_left, n_bottom),
    ]
    lonlat_coords = [[round(lon, 7), round(lat, 7)] for lon, lat in lonlat_coords]

    props = {"id": int(cell_id)}
    features_4326.append({
        "type": "Feature", "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [lonlat_coords]}
    })
    features_32643.append({
        "type": "Feature", "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [utm_coords]}
    })

os.makedirs('data', exist_ok=True)
for name, features in [('grid_4326.geojson', features_4326), ('grid_32643.geojson', features_32643)]:
    with open(os.path.join('data', name), 'w') as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, indent=2)
    print(f"Created data/{name} with {len(features)} features")

print(f"Cell 1 coords (4326): {features_4326[0]['geometry']['coordinates'][0][:2]}")
