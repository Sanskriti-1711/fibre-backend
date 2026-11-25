"""Why the tape loses to a wider bright band: print every attempt's verdict.

Run from fiber-backend with the venv python. Read-only.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np

from supervision import tape_band
from supervision.tests_tape_band import band_photo

frame = band_photo(second_band=(0.0, 74.0, 210.0))

print('=== find_band ===')
result = tape_band.find_band(frame, work_edge=900)
for key in ('found', 'reason', 'width_px', 'angle_deg', 'score', 'confirmed', 'competitors'):
    print(f'  {key}: {result.get(key)}')
print('  attempts:', result.get('attempts'))

print()
print('=== each working resolution ===')
for edge in (900, 702, 1080):
    attempt = tape_band._find_at(frame, edge)
    print(f'edge {edge}: found={attempt.get("found")} reason={attempt.get("reason")}')
    if attempt.get('found'):
        for key in ('width_px', 'angle_deg', 'score', 'periodicity', 'bend', 'coverage',
                    'support', 'width_spread', 'competitors'):
            print(f'    {key}: {attempt.get(key)}')

print()
print('=== candidates at 900 (graded and refused) ===')
from PIL import Image

image = Image.fromarray(frame).convert('RGB')
if max(image.width, image.height) > 900:
    image.thumbnail((900, 900), Image.BILINEAR)
gray = np.asarray(image, dtype=np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)

seeds_image = image.copy()
seeds_image.thumbnail((tape_band.SEED_EDGE, tape_band.SEED_EDGE), Image.BILINEAR)
seeds_gray = np.asarray(seeds_image, dtype=np.float32) @ np.array(
    [0.299, 0.587, 0.114], dtype=np.float32
)
to_work = seeds_image.width / float(image.width)
seeds = tape_band._seeds(seeds_gray)
print(f'seeds: {len(seeds)}')
for index, seed in enumerate(seeds):
    scaled = {
        'centre': seed['centre'] / to_work,
        'axis': seed['axis'],
        'across': seed['across'],
        'length': seed['length'] / to_work,
        'width': seed['width'] / to_work,
        'share': seed['share'],
    }
    graded = tape_band._grade(scaled, gray)
    print(
        f'  [{index}] w={scaled["width"]:.0f} l={scaled["length"]:.0f} share={scaled["share"]:.2f}'
        f' ok={graded.get("ok")} score={graded.get("score")}'
        f' w_out={graded.get("width")} per={graded.get("periodicity")}'
        f' reason={graded.get("reason")}'
    )
