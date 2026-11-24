"""Save the rectified strip of each confirmed blade, for a look.

Run from fiber-backend with the venv python. Writes PNGs into tmp/.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
from PIL import Image, ImageOps

from supervision import tape_clip, tape_reader

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
SUMMARY = 'tmp/field_reader_summary.json'
TAGS = {
    '20261124_140332.jpg.jpeg': '140332',
    '20261124_141314.jpg.jpeg': '141314',
    'WhatsApp Image 2026-11-24 at 08.38.25 (1).jpeg': '25',
}

rows = json.load(open(SUMMARY, encoding='utf-8'))['rows']
confirmed = {row['photo'] for row in rows if row.get('blade_confirmed')}

for name, tag in TAGS.items():
    if name not in confirmed:
        print(f'{tag}: not confirmed in the sweep')
        continue
    with open(os.path.join(FOLDER, name), 'rb') as handle:
        data = handle.read()
    rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))
    clip = tape_clip.find_clip(rgb)
    strip, _scale = tape_reader._rectify(rgb, clip['band'])
    view = strip.convert('L')
    if view.width > 900:
        view = view.resize((900, max(1, int(round(view.height * 900.0 / view.width)))), Image.LANCZOS)
    view.save(f'tmp/strip_{tag}.png')
    print(f'{tag}: strip {strip.size[0]}x{strip.size[1]} -> tmp/strip_{tag}.png ({view.size[0]}x{view.size[1]})')
