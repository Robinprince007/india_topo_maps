# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pillow",
#     "numpy",
# ]
# ///

import hashlib
import json
import time
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

# Inner black frame corners (constant across all images)
# Determined by detecting black lines with >500px count
FRAME_TL = (284, 224)   # (x, y)
FRAME_TR = (6781, 224)
FRAME_BL = (284, 6720)
FRAME_BR = (6781, 6720)

RAW_DIR = Path('data/raw')
INTER_DIR = Path('inter')
EXPORT_DIR = Path('export/gtiffs')
BOUNDS_DIR = Path('export/bounds')

SRC_CRS = 'EPSG:32643'
DST_CRS = 'EPSG:3857'


def run_external(cmd):
    print(f'  $ {cmd}')
    start = time.time()
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    elapsed = time.time() - start
    if res.returncode != 0:
        print(f'  STDOUT: {res.stdout}')
        print(f'  STDERR: {res.stderr}')
        raise Exception(f'command failed (exit {res.returncode}): {cmd}')
    print(f'  done in {elapsed:.1f}s')


def load_grid():
    with open('data/grid_32643.geojson') as f:
        gj = json.load(f)
    grid = {}
    for feat in gj['features']:
        cell_id = feat['properties']['id']
        coords = feat['geometry']['coordinates'][0]
        # coords: [BL, BR, TR, TL, BL] — standard polygon winding
        grid[cell_id] = coords
    return grid


def create_cutline(utm_coords, out_file):
    cutline = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {},
            "geometry": {
                "type": "Polygon",
                "coordinates": [utm_coords]
            }
        }]
    }
    with open(out_file, 'w') as f:
        json.dump(cutline, f)


def process_image(cell_id, grid):
    img_file = RAW_DIR / f'{cell_id}.png'
    if not img_file.exists():
        print(f'[{cell_id}] image not found, skipping')
        return False

    work_dir = INTER_DIR / str(cell_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    export_file = EXPORT_DIR / f'{cell_id}.tif'

    if export_file.exists():
        print(f'[{cell_id}] already exported, skipping')
        return True

    utm_coords = grid[cell_id]
    # utm_coords: [BL, BR, TR, TL, BL]
    bl, br, tr, tl = utm_coords[0], utm_coords[1], utm_coords[2], utm_coords[3]

    # Map pixel corners to UTM corners
    # Pixel: TL, TR, BL, BR -> UTM: TL, TR, BL, BR
    gcps = [
        (FRAME_TL, tl),
        (FRAME_TR, tr),
        (FRAME_BL, bl),
        (FRAME_BR, br),
    ]

    gcp_str = ''
    for (px, py), (e, n) in gcps:
        gcp_str += f' -gcp {px} {py} {e} {n}'

    georef_file = work_dir / 'georef.tif'
    final_file = work_dir / 'final.tif'
    cutline_file = work_dir / 'cutline.geojson'

    # Step 1: gdal_translate with GCPs
    if not georef_file.exists():
        print(f'[{cell_id}] georeferencing...')
        perf = '--config GDAL_CACHEMAX 128 --config GDAL_NUM_THREADS ALL_CPUS'
        co = '-co TILED=YES -co COMPRESS=DEFLATE -co PREDICTOR=2'
        cmd = f'gdal_translate {perf} {co} {gcp_str} -a_srs "{SRC_CRS}" -of GTiff {img_file} {georef_file}'
        run_external(cmd)

    # Step 2: gdalwarp from 32643 -> 3857
    if not final_file.exists():
        print(f'[{cell_id}] warping...')
        create_cutline(utm_coords, cutline_file)
        cutline_opts = f'-cutline {cutline_file} -cutline_srs "{SRC_CRS}" -crop_to_cutline --config GDALWARP_IGNORE_BAD_CUTLINE YES -wo CUTLINE_ALL_TOUCHED=TRUE'
        quality = '-co COMPRESS=JPEG -co JPEG_QUALITY=75 -co TILED=YES'
        reproj = f'-tps -r bilinear -t_srs "{DST_CRS}"'
        nodata = '-dstalpha'
        perf = '-multi -wo NUM_THREADS=ALL_CPUS --config GDAL_CACHEMAX 1024 -wm 1024'
        cmd = f'gdalwarp -overwrite {perf} {nodata} {reproj} {quality} {cutline_opts} {georef_file} {final_file}'
        run_external(cmd)

    # Step 3: Export bounds as GeoJSONL with metadata
    bounds_file = BOUNDS_DIR / f'{cell_id}.geojsonl'
    if not bounds_file.exists():
        print(f'[{cell_id}] exporting bounds...')
        # Get bounds geometry in EPSG:4326 via ogr2ogr
        tmp_bounds = work_dir / 'bounds_tmp.geojsonl'
        cmd = f'ogr2ogr -t_srs EPSG:4326 -s_srs "{SRC_CRS}" -f GeoJSONSeq {tmp_bounds} {cutline_file}'
        run_external(cmd)

        with open(tmp_bounds, 'r') as f:
            feature = json.loads(f.read().strip())

        # Compute digest of source image
        digest = hashlib.sha256(img_file.read_bytes()).hexdigest()

        # Pixel cutline (frame corners: TL, TR, BR, BL, TL)
        pixel_cutline = [
            list(FRAME_TL), list(FRAME_TR),
            list(FRAME_BR), list(FRAME_BL),
            list(FRAME_TL),
        ]

        feature['properties'] = {
            'id': str(cell_id),
            'crs': SRC_CRS,
            'gcps': [[[px, py], [e, n]] for (px, py), (e, n) in gcps],
            'pixel_cutline': pixel_cutline,
            'digest': digest,
        }

        with open(bounds_file, 'w') as f:
            f.write(json.dumps(feature) + '\n')

        tmp_bounds.unlink()

    # Step 4: Export as COG
    print(f'[{cell_id}] exporting...')
    co = '-co TILED=YES -co COMPRESS=JPEG -co JPEG_QUALITY=75'
    mask = '--config GDAL_TIFF_INTERNAL_MASK YES -b 1 -b 2 -b 3 -mask 4'
    perf = '--config GDAL_CACHEMAX 512'
    cmd = f'gdal_translate {perf} {mask} {co} {final_file} {export_file}'
    run_external(cmd)

    return True


def main():
    INTER_DIR.mkdir(exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    BOUNDS_DIR.mkdir(parents=True, exist_ok=True)

    grid = load_grid()

    if len(sys.argv) > 1:
        ids = [int(x) for x in sys.argv[1:]]
    else:
        ids = sorted(grid.keys())

    total = len(ids)
    for i, cell_id in enumerate(ids):
        print(f'\n=== [{i+1}/{total}] Cell {cell_id} ===')
        if cell_id not in grid:
            print(f'  cell {cell_id} not in grid, skipping')
            continue
        try:
            process_image(cell_id, grid)
        except Exception as e:
            print(f'  ERROR: {e}')


if __name__ == '__main__':
    main()
