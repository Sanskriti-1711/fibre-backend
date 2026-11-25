"""Does closing small gaps inside a label recover whole digits from faint ink?

    python tmp/close_gap.py 20261124_141314.jpg.jpeg
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


def close_1d(flags: np.ndarray, half: int) -> np.ndarray:
    """Bridge gaps of up to `half` columns between inked columns (1-D closing)."""
    if half <= 0:
        return flags
    out = flags.copy()
    for offset in range(1, half + 1):
        left = np.concatenate([np.zeros(offset, bool), flags[:-offset]])
        right = np.concatenate([flags[offset:], np.zeros(offset, bool)])
        out |= left & right | (flags & left) | (flags & right)
    return out


def runs_of(flags: np.ndarray, gap: int) -> list[tuple[int, int]]:
    runs, left, last = [], None, -1
    for x, on in enumerate(flags):
        if on:
            if left is None:
                left = x
            last = x
        elif left is not None and x - last > gap:
            runs.append((left, last + 1))
            left = None
    if left is not None:
        runs.append((left, last + 1))
    return runs


with open(os.path.join(FOLDER, NAME), 'rb') as handle:
    data = handle.read()
rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))
clip = tape_clip.find_clip(rgb)
strip, _scale = tape_reader._rectify(rgb, clip['band'])
lattice = tape_ticks.read_strip(strip)
positions = [int(round(float(p))) for p in (lattice or {}).get('positions') or []]
pitch = float((lattice or {}).get('pitch') or 0.0)
pixels = np.asarray(strip.convert('L'), dtype=np.float32)
ink = pixels < (tape_digits._background_profile(pixels)[None, :] - tape_digits.MIN_INK_OFFSET)

for number, (band_start, band_stop, _from_bottom) in enumerate(
    tape_digits._digit_bands(ink, positions)
):
    boxes, tight = tape_digits._label_boxes(ink, positions, pitch, (band_start, band_stop))
    height = tight[1] - tight[0]
    occupied = ink[tight[0] : tight[1]].any(axis=0)
    print()
    print(f'band {number} tight={tight} height={height} current boxes={len(boxes)}')
    for half in (2, 3, 4, 6, 8):
        closed = close_1d(occupied, half)
        runs = runs_of(closed, 0)
        widths = [right - left for left, right in runs if right - left >= 9]
        print(f'  half={half}: {len(runs)} runs, widths {sorted(widths, reverse=True)[:8]}')
        if half not in (3, 5):
            continue
        for left, right in runs[:12]:
            if right - left < 9:
                continue
            cell = tape_digits._cell_image(strip, (left, tight[0], right, tight[1]))
            text, confidence = tape_digits._default_ocr(cell)
            print(f'    [{left:4d},{right:4d}) w={right - left:3d} text={text!r:6} conf={confidence:.2f}')
