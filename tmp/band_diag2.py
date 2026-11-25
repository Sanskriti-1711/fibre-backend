"""Per-seed geometry and tick periodicity, to pick the discriminating signal.

Run from fiber-backend with the venv python. Read-only.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

import numpy as np
from PIL import Image

from supervision import tape_band
from supervision.tests_tape_band import band_photo


def candidates(frame, edge=900):
    image = Image.fromarray(frame).convert('RGB')
    if max(image.width, image.height) > edge:
        image.thumbnail((edge, edge), Image.BILINEAR)
    gray = np.asarray(image, dtype=np.float32) @ np.array(
        [0.299, 0.587, 0.114], dtype=np.float32
    )
    seeds_image = image.copy()
    seeds_image.thumbnail((tape_band.SEED_EDGE, tape_band.SEED_EDGE), Image.BILINEAR)
    seeds_gray = np.asarray(seeds_image, dtype=np.float32) @ np.array(
        [0.299, 0.587, 0.114], dtype=np.float32
    )
    to_work = seeds_image.width / float(image.width)
    rows = []
    for seed in tape_band._seeds(seeds_gray):
        band = {
            'centre': seed['centre'] / to_work,
            'axis': seed['axis'],
            'across': seed['across'],
            'length': seed['length'] / to_work,
            'width': seed['width'] / to_work,
            'share': seed['share'],
        }
        angle = float(np.degrees(np.arctan2(band['axis'][1], band['axis'][0])))
        widened = tape_band._widen(gray, band)
        if widened is not None:
            band['width'], offset = widened
            band['centre'] = band['centre'] + band['across'] * offset
        track = tape_band._walk(gray, band)
        fit = tape_band._fit(track)
        if not fit['ok']:
            rows.append((round(angle, 1), round(band['width'], 1), None, fit['reason']))
            continue
        graded = tape_band._band_from_fit(track, fit, band)
        graded['periodicity'] = tape_band._periodicity(track, fit)
        rows.append((
            round(angle, 1),
            round(graded['width'], 1),
            {
                'periodicity': round(graded['periodicity'], 3),
                'spread': round(graded['width_spread'], 3),
                'bend_px': round(graded['bend'] * graded['width'], 1),
                'coverage': round(graded['coverage'], 2),
                'support': round(graded['support'], 2),
                'length/width': round(graded['length'] / graded['width'], 1),
            },
            None,
        ))
    return rows


for label, frame in (
    ('plain', band_photo()),
    ('with paved', band_photo(second_band=(0.0, 74.0, 210.0))),
    ('paved only', band_photo(length=0.001, second_band=(0.0, 74.0, 210.0))),
    ('shaded', band_photo(ramp=1.0)),
    ('clip', band_photo(clip=(0.0, 8.0))),
):
    print(f'--- {label}')
    for angle, width, stats, reason in candidates(frame):
        print(f'  angle={angle:6} w={width:6} {stats if stats else reason}')
