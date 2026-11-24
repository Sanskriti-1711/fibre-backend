"""Why the six confirmed blades get no scale: print the ladder stage for each.

Run from fiber-backend with the venv python. Read-only.
"""
import io
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from PIL import Image, ImageOps

from supervision import tape_clip, tape_digits, tape_reader, tape_ticks

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
SUMMARY = 'tmp/field_reader_summary.json'

rows = json.load(open(SUMMARY, encoding='utf-8'))['rows']
confirmed = [row for row in rows if row.get('blade_confirmed')]
print(f'{len(confirmed)} confirmed photo(s)')

for row in confirmed:
    path = os.path.join(FOLDER, row['photo'])
    with open(path, 'rb') as handle:
        data = handle.read()
    rgb = __import__('numpy').asarray(
        ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    )
    print()
    print(f'--- {row["photo"]}  (sweep stage: {row["stage"]})')

    clip = tape_clip.find_clip(rgb)
    band = clip.get('band') or {}
    print(
        f'  clip: found={clip.get("found")} confirmed={clip.get("blade_confirmed")}'
        f' angle={clip.get("tape_angle_deg")}'
        f' width_px={band.get("width_px")} length_px={band.get("length_px")}'
    )

    strip, scale = tape_reader._rectify(rgb, band)
    print(f'  strip: {strip.size[0]}x{strip.size[1]}  scale={scale:.3f}')

    lattice = tape_ticks.read_strip(strip)
    if lattice is None:
        print('  ladder: None (strip too small or no profile)')
        continue
    print(
        f'  ladder: usable={lattice["usable"]} pitch={lattice["pitch"]:.2f}'
        f' count={lattice["count"]} residual={lattice["residual"]:.3f}'
        f' score={lattice["score"]} stripe={lattice["stripe"]}'
        f' major_every={lattice["major_every"]}'
    )
    if not lattice['usable']:
        print(
            '  ladder gates:'
            f' count>={tape_ticks.MIN_TICKS} ({lattice["count"]}),'
            f' residual<={tape_ticks.MAX_RESIDUAL} ({lattice["residual"]:.3f}),'
            f' score>={tape_ticks.MIN_SCORE} ({lattice["score"]})'
        )
        continue

    numbers = tape_digits.read_labels(strip, lattice)
    print(f'  numbers: usable={(numbers or {}).get("usable")} reason={(numbers or {}).get("reason")}')
    for anchor in (numbers or {}).get('anchors') or []:
        print(f'    anchor {anchor.get("value")} {anchor.get("text")} at x={anchor.get("x_px")}')
