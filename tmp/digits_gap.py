"""How many label runs at each merge gap, and what the strip looks like.

Run from fiber-backend with the venv python. Read-only apart from two PNGs in
tmp/ for inspection.
"""
import io
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
from PIL import Image, ImageOps

from supervision import tape_clip, tape_digits, tape_reader, tape_ticks

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
PHOTOS = {
    '21': 'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
    '28': 'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
}

for tag, name in PHOTOS.items():
    with open(os.path.join(FOLDER, name), 'rb') as handle:
        data = handle.read()
    rgb = np.asarray(ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB'))
    clip = tape_clip.find_clip(rgb)
    strip, _scale = tape_reader._rectify(rgb, clip['band'])
    lattice = tape_ticks.read_strip(strip)
    positions = [int(round(p)) for p in lattice['positions']]
    pitch = float(lattice['pitch'])
    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    ink = pixels < (tape_digits._background_profile(pixels)[None, :] - tape_digits.MIN_INK_OFFSET)

    print()
    print(f'--- {tag}: strip {strip.size[0]}x{strip.size[1]} pitch={pitch:.2f} marks={len(positions)}')
    for candidate in tape_digits._digit_bands(ink, positions):
        start, stop, _from_bottom = candidate
        shares = ink[start:stop].mean(axis=1)
        printed = tape_digits._printed_rows(shares)
        tight = (start + printed[0], start + printed[1]) if printed else (start, stop)
        occupied = ink[tight[0] : tight[1]].any(axis=0)
        line = f'  band {candidate} tight={tight}'
        for gap in (4, 8, 12, 16, 20, 25, 30):
            runs = 0
            left = None
            last = -1
            for x, filled in enumerate(occupied):
                if filled:
                    if left is None:
                        left = x
                    last = x
                elif left is not None and x - last > gap:
                    runs += 1
                    left = None
            if left is not None:
                runs += 1
            line += f' gap{gap}={runs}'
        print(line)

    view = strip.convert('L')
    scale = max(1.0, 900.0 / max(1, view.width))
    if scale > 1.0:
        view = view.resize((int(view.width * scale), int(view.height * scale)), Image.LANCZOS)
    view.save(f'tmp/strip_{tag}.png')
    print(f'  saved tmp/strip_{tag}.png ({view.size[0]}x{view.size[1]})')
