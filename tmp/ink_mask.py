"""Save the ink mask `read_labels` works from, beside the strip it came from.

    python tmp/ink_mask.py 20261124_141314.jpg.jpeg 900 500
"""

import io
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
from PIL import Image, ImageOps

from supervision import tape_clip, tape_digits, tape_reader, tape_ticks

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAME = sys.argv[1] if len(sys.argv) > 1 else '20261124_141314.jpg.jpeg'
LEFT = int(sys.argv[2]) if len(sys.argv) > 2 else 900
WIDTH = int(sys.argv[3]) if len(sys.argv) > 3 else 500
SCALE = 3

with open(os.path.join(FOLDER, NAME), 'rb') as handle:
    data = handle.read()
rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))
clip = tape_clip.find_clip(rgb)
strip, _scale = tape_reader._rectify(rgb, clip['band'])
lattice = tape_ticks.read_strip(strip)
positions = [int(round(float(p))) for p in (lattice or {}).get('positions') or []]
pitch = float((lattice or {}).get('pitch') or 0.0)

print(f'strip {strip.size} pitch={pitch:.2f} marks={len(positions)}')
print(f'positions: {positions}')
spacings = np.diff(sorted(positions))
if spacings.size:
    print(
        'spacings: median=%.1f min=%d max=%d  first=%d last=%d'
        % (np.median(spacings), spacings.min(), spacings.max(), min(positions), max(positions))
    )

pixels = np.asarray(strip.convert('L'), dtype=np.float32)
profile = tape_digits._background_profile(pixels)
ink = pixels < (profile[None, :] - tape_digits.MIN_INK_OFFSET)
print(f'ink covers {100.0 * ink.mean():.1f}% of the strip')

for number, (band_start, band_stop, _from_bottom) in enumerate(
    tape_digits._digit_bands(ink, positions)
):
    mask = (ink[band_start:band_stop, LEFT : LEFT + WIDTH] * 255).astype(np.uint8)
    image = Image.fromarray(255 - mask)
    image = image.resize((WIDTH * SCALE, (band_stop - band_start) * SCALE), Image.NEAREST)
    out = f'tmp/{NAME.split(".")[0]}_mask_{number}.png'
    image.save(out)
    print(f'band {number} rows {band_start}..{band_stop}: saved {out} {image.size}')

    pure = strip.convert('L').crop((LEFT, band_start, LEFT + WIDTH, band_stop))
    pure = pure.resize((WIDTH * SCALE, (band_stop - band_start) * SCALE), Image.LANCZOS)
    out = f'tmp/{NAME.split(".")[0]}_crop_{number}.png'
    pure.save(out)
    print(f'  strip crop: {out} {pure.size}')
