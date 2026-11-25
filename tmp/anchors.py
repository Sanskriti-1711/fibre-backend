"""Every band's label boxes and the text tesseract reads from each cell.

Run from fiber-backend with tesseract on PATH. Read-only.

    python tmp/anchors.py 20261124_141314.jpg.jpeg
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

with open(os.path.join(FOLDER, NAME), 'rb') as handle:
    data = handle.read()
rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))
clip = tape_clip.find_clip(rgb)
strip, _scale = tape_reader._rectify(rgb, clip['band'])
lattice = tape_ticks.read_strip(strip)
positions = [int(round(float(p))) for p in (lattice or {}).get('positions') or []]
pitch = float((lattice or {}).get('pitch') or 0.0)

print(f'{NAME}: strip {strip.size} pitch={pitch:.2f} marks={len(positions)}')
print(f'  usable={lattice.get("usable")} score={lattice.get("score"):.3f} '
      f'residual={lattice.get("residual"):.2f} coverage={lattice.get("coverage")}')

pixels = np.asarray(strip.convert('L'), dtype=np.float32)
ink = pixels < (tape_digits._background_profile(pixels)[None, :] - tape_digits.MIN_INK_OFFSET)

for number, (band_start, band_stop, from_bottom) in enumerate(
    tape_digits._digit_bands(ink, positions)
):
    boxes, tight = tape_digits._label_boxes(ink, positions, pitch, (band_start, band_stop))
    print()
    print(f'band {number} rows {band_start}..{band_stop} from_bottom={from_bottom} '
          f'tight={tight} boxes={len(boxes)} height={tight[1] - tight[0]}')
    if not boxes:
        continue
    for box in boxes:
        pad = max(2, int(round((box['right'] - box['left']) * tape_digits.CELL_PAD_SHARE)))
        left = max(0, box['left'] - pad)
        right = min(pixels.shape[1], box['right'] + pad)
        cell = tape_digits._cell_image(
            strip, (left, tight[0], right, tight[1]), 1.0
        )
        text, confidence = tape_digits._default_ocr(
            cell, {'index': box['index'], 'x_px': int(round(box['centre']))}
        )
        mark = positions[box['index']] if box['index'] < len(positions) else -1
        print(
            f'  x={box["centre"]:7.1f} w={box["right"] - box["left"]:4d} '
            f'mark#{box["index"]:3d} at {mark:5d} off={box["centre"] - mark:7.1f} '
            f'text={text!r:6} conf={confidence:.2f}'
        )
        cell.save(f'tmp/cell_{number}_{int(box["centre"])}.png')
