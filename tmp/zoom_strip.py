"""Zoom a window of the rectified strip, at full height, to see its number rows.

    python tmp/zoom_strip.py 20261124_141314.jpg.jpeg 700 400
"""

import io
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
from PIL import Image, ImageOps

from supervision import tape_clip, tape_reader

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAME = sys.argv[1] if len(sys.argv) > 1 else '20261124_141314.jpg.jpeg'
LEFT = int(sys.argv[2]) if len(sys.argv) > 2 else 0
WIDTH = int(sys.argv[3]) if len(sys.argv) > 3 else 700
SCALE = 3

with open(os.path.join(FOLDER, NAME), 'rb') as handle:
    data = handle.read()
rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))
clip = tape_clip.find_clip(rgb)
strip, _scale = tape_reader._rectify(rgb, clip['band'])
print(f'strip {strip.size}  clip band width={clip["band"]["width_px"]:.0f}')
print(f'clip centre={clip["band"]["centre"]} axis={clip["band"]["axis"]}')

crop = strip.convert('L').crop((LEFT, 0, LEFT + WIDTH, strip.height))
crop = crop.resize((WIDTH * SCALE, strip.height * SCALE), Image.LANCZOS)
out = f'tmp/{NAME.split(".")[0]}_zoom_{LEFT}.png'
crop.save(out)
print(f'saved {out} {crop.size}')
