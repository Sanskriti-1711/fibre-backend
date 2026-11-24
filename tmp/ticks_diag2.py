"""How tall are the marks on each lattice: ticks or printed numbers?

Run from fiber-backend with the venv python. Read-only.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
from PIL import Image, ImageOps

from supervision import tape_clip, tape_reader, tape_ticks

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
SUMMARY = 'tmp/field_reader_summary.json'

rows = json.load(open(SUMMARY, encoding='utf-8'))['rows']
confirmed = [row for row in rows if row.get('blade_confirmed')]

print(f'{"photo":34} {"strip":10} {"pitch":6} {"n":4} {"len_med":8} {"share":6} {"len_max":8}')
for row in confirmed:
    path = os.path.join(FOLDER, row['photo'])
    with open(path, 'rb') as handle:
        data = handle.read()
    rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))
    clip = tape_clip.find_clip(rgb)
    strip, _scale = tape_reader._rectify(rgb, clip['band'])
    lattice = tape_ticks.read_strip(strip)
    if not lattice or not lattice.get('positions'):
        print(f'{row["photo"][:34]:34} {strip.size[0]}x{strip.size[1]:<5} no lattice')
        continue
    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    height = pixels.shape[0]
    lengths = tape_ticks._tick_lengths(pixels, lattice['positions'])
    med = float(np.median(lengths)) if lengths else 0.0
    print(
        f'{row["photo"][:34]:34} {strip.size[0]}x{strip.size[1]:<5} {lattice["pitch"]:6.2f}'
        f' {lattice["count"]:4} {med:8.1f} {med / max(1, height):6.2f}'
        f' {(max(lengths) if lengths else 0):8.1f}'
    )
