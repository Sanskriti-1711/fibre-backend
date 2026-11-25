"""Print each band's per-row ink share on a field photo, to see the two rows.

Run from fiber-backend with the venv python. Read-only.

    python tmp/row_split.py 20261124_141314.jpg.jpeg
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

print(f'{NAME}: strip {strip.size[0]}x{strip.size[1]} pitch={pitch:.2f} marks={len(positions)}')

pixels = np.asarray(strip.convert('L'), dtype=np.float32)
ink = pixels < (tape_digits._background_profile(pixels)[None, :] - tape_digits.MIN_INK_OFFSET)

view = strip.convert('L')
if view.width > 1400:
    view = view.resize((1400, max(1, int(round(view.height * 1400 / view.width)))))
view.save(f'tmp/{NAME.split(".")[0]}_strip.png')
print(f'saved tmp/{NAME.split(".")[0]}_strip.png {view.size}')

lo, hi = (min(positions), max(positions)) if positions else (0, ink.shape[1])
print(f'ladder columns {lo}..{hi} of {ink.shape[1]} ({100.0 * (hi - lo) / ink.shape[1]:.0f}% of the strip)')

for number, (band_start, band_stop, from_bottom) in enumerate(tape_digits._digit_bands(ink, positions)):
    shares = ink[band_start:band_stop].mean(axis=1)
    printed = tape_digits._printed_rows(shares)
    print()
    print(f'band rows {band_start}..{band_stop} ({band_stop - band_start}px) from_bottom={from_bottom}')
    crop = strip.convert('L').crop((0, band_start, strip.width, band_stop))
    crop.resize((1400, max(20, int(round(crop.height * 1400 / crop.width))))).save(
        f'tmp/{NAME.split(".")[0]}_band{number}.png'
    )
    print(f'  _printed_rows -> {printed}  mean={shares.mean():.3f}')
    if printed is None:
        continue
    tight = shares[printed[0] : printed[1]]
    print(f'  tight height={tight.size} mean={tight.mean():.3f}')
    over_ladder = ink[band_start:band_stop, lo:hi].mean(axis=1)
    print(f'  over the ladder only: max={over_ladder.max():.3f} mean={over_ladder.mean():.3f}')
    for offset in range(tight.size):
        row = band_start + printed[0] + offset
        share = tight[offset]
        local = over_ladder[printed[0] + offset]
        bar = '#' * int(round(local * 80))
        print(f'    {row:3d} all={share:.3f} ladder={local:.3f} {bar}')
