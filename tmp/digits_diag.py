"""Why `_label_boxes` returns nothing: print its own intermediate values.

Run from fiber-backend with the venv python. Read-only.
"""
import io
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
from PIL import Image, ImageOps

from supervision import tape_clip, tape_digits, tape_reader, tape_ticks

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
PHOTOS = [
    'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
]

print(
    'constants:'
    f' MIN_BAND_HEIGHT={tape_digits.MIN_BAND_HEIGHT}'
    f' MIN_LABEL_SHARE={tape_digits.MIN_LABEL_SHARE}'
    f' MAX_LABEL_HEIGHTS={tape_digits.MAX_LABEL_HEIGHTS}'
    f' LABEL_GAP_SHARE={tape_digits.LABEL_GAP_SHARE}'
    f' MARK_REACH={tape_digits.MARK_REACH}'
    f' MAX_ROW_INK={tape_digits.MAX_ROW_INK}'
)

for name in PHOTOS:
    path = os.path.join(FOLDER, name)
    with open(path, 'rb') as handle:
        data = handle.read()
    rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))

    clip = tape_clip.find_clip(rgb)
    strip, _scale = tape_reader._rectify(rgb, clip['band'])
    lattice = tape_ticks.read_strip(strip)
    positions = [int(round(p)) for p in (lattice or {}).get('positions') or []]
    pitch = float((lattice or {}).get('pitch') or 0.0)

    print()
    print(f'--- {name}')
    print(f'  strip {strip.size[0]}x{strip.size[1]}  pitch={pitch:.2f}  marks={len(positions)}')

    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    ink = pixels < (tape_digits._background_profile(pixels)[None, :] - tape_digits.MIN_INK_OFFSET)

    for candidate in tape_digits._digit_bands(ink, positions):
        start, stop, from_bottom = candidate
        shares = ink[start:stop].mean(axis=1)
        printed = tape_digits._printed_rows(shares)
        print(
            f'  band rows {start}..{stop} ({stop - start}px, from_bottom={from_bottom})'
            f' row-share max={shares.max():.3f} mean={shares.mean():.3f}'
            f' printed_rows={printed}'
        )
        if printed is None:
            continue
        tight = (start + printed[0], start + printed[1])
        height = tight[1] - tight[0]
        occupied = ink[tight[0] : tight[1]].any(axis=0)
        gap = max(2, int(round(height * tape_digits.LABEL_GAP_SHARE)))
        runs = []
        left = None
        last = -1
        for x, filled in enumerate(occupied):
            if filled:
                if left is None:
                    left = x
                last = x
            elif left is not None and x - last > gap:
                runs.append((left, last + 1))
                left = None
        if left is not None:
            runs.append((left, last + 1))
        narrowest = max(3, int(round(height * tape_digits.MIN_LABEL_SHARE)))
        widest = max(narrowest + 1, int(round(height * tape_digits.MAX_LABEL_HEIGHTS)))
        print(
            f'    tight={tight} height={height} gap={gap}'
            f' runs={len(runs)} accepted_width=[{narrowest}, {widest}]'
            f' ink_columns={int(occupied.sum())}/{occupied.size}'
        )
        widths = sorted((right - left for left, right in runs), reverse=True)[:6]
        print(f'    widest runs: {widths}')
        if positions:
            print(
                f'    marks per run-width: narrowest run / pitch ='
                f' {narrowest / pitch:.2f}'
            )
