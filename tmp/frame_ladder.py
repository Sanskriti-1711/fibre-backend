"""Straight strip vs the projective strip, on the field tapes.

`_rectify` warps the blade with one scale along the whole length, so a blade
whose edges converge in the photo has a pitch that drifts: the marks are still
printed at one pitch on the tape, they just do not land on one pitch in pixels.
`tape_frame` fits both long edges and maps the quad projectively, which is the
distortion a rectangle cannot undo. This prints both ladders side by side.

    python tmp/frame_ladder.py
"""

from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_band, tape_frame, tape_ticks  # noqa: E402
from supervision.tape_reader import _rectify  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAMES = [
    '20261124_140332.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.25 (1).jpeg',
]


def line(label: str, lattice: dict | None) -> str:
    if not lattice:
        return '   %-10s no ladder' % label
    return (
        '   %-10s pitch %5.2f score %.3f count %3d cov %.2f residual %.3f (%.2f px) '
        'usable %-5s %s'
        % (
            label,
            lattice['pitch'],
            lattice['score'],
            lattice['count'],
            lattice['coverage'],
            lattice['residual'],
            lattice['residual'] * lattice['pitch'],
            lattice['usable'],
            tape_ticks.ladder_problem(lattice) or 'LADDER OK',
        )
    )


for name in NAMES:
    gray = np.asarray(
        ImageOps.exif_transpose(Image.open(os.path.join(FIELD, name))).convert('L')
    )
    print('\n== %s  %dx%d' % (name, gray.shape[1], gray.shape[0]))

    band = tape_band.find_band(np.stack([gray] * 3, axis=-1))
    if band.get('found'):
        strip, _scale = _rectify(np.stack([gray] * 3, axis=-1), band['band'])
        print('   straight strip %dx%d' % (strip.width, strip.height))
        print(line('straight', tape_ticks.read_strip(strip)))
    else:
        print('   straight: no band (%s)' % band.get('reason'))

    framed = tape_frame.frame_image(Image.fromarray(gray))
    if not framed:
        print('   framed: no blade')
        continue
    print(
        '   framed strip %dx%d angle %.1f thickness %d coverage %.2f'
        % (
            framed['width_px'],
            framed['height_px'],
            framed['angle'],
            framed['thickness_px'],
            framed['coverage'],
        )
    )
    print(line('framed', framed['lattice']))
