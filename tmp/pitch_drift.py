"""How much of the mark residual is explained by a pitch that drifts.

`_fit_lattice` fits `position = intercept + slope * tooth`, i.e. one pitch for
the whole blade. A tape photographed with perspective has a pitch that shrinks
along its length: the marks are still printed at one pitch on the tape, they
just do not land on one pitch in the image. This measures the residual of a
straight fit against a quadratic one, on the field photos and on the rendered
non-tape fixtures the reader must keep refusing.
"""

from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_band, tape_ticks  # noqa: E402
from supervision.tape_reader import _rectify  # noqa: E402
from supervision.tests_tape_reader import caption_photo, metal_bar_photo  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAMES = [
    '20261124_140332.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.25 (1).jpeg',
]


def report(label: str, lattice: dict | None) -> None:
    if not lattice or len(lattice['positions']) < 4:
        print('   %-12s no ladder' % label)
        return
    positions = np.array(lattice['positions'], dtype=float)
    pitch = float(lattice['pitch'])
    steps = np.round((positions - positions[0]) / pitch)
    out = ['   %-12s pitch %5.2f count %3d score %.3f' % (label, pitch, len(positions), lattice['score'])]
    for degree in (1, 2):
        slope, intercept = np.polyfit(steps, positions, degree)[:2] if degree == 1 else (None, None)
        if degree == 1:
            fit = slope * steps + intercept
            local = slope
        else:
            coefficients = np.polyfit(steps, positions, 2)
            fit = np.polyval(coefficients, steps)
            local = float(coefficients[0])
        deviation = positions - fit
        rms = float(np.sqrt(np.mean(deviation**2)))
        out.append(
            'deg%d rms %5.2f px (%.3f pitch, %.2f mm-on-tape)'
            % (degree, rms, rms / pitch, rms / pitch)
        )
    print('\n'.join(out))
    if len(positions) >= 4:
        coefficients = np.polyfit(steps, positions, 2)
        first = float(np.polyval(np.polyder(np.polyfit(steps, positions, 1)), steps[0]))
        start = float(2 * coefficients[0] * steps[0] + coefficients[1])
        end = float(2 * coefficients[0] * steps[-1] + coefficients[1])
        print(
            '   pitch along blade: %.2f -> %.2f px  (%.0f%% change)  linear %.2f'
            % (start, end, 100.0 * (end - start) / start, first)
        )


for name in NAMES:
    rgb = np.asarray(
        ImageOps.exif_transpose(Image.open(os.path.join(FIELD, name))).convert('RGB')
    )
    band = tape_band.find_band(rgb)
    print('\n== %s' % name)
    if not band.get('found'):
        print('   no band: %s' % band.get('reason'))
        continue
    strip, _scale = _rectify(rgb, band['band'])
    report('field', tape_ticks.read_strip(strip))

for label, image in [('caption', caption_photo()), ('metal bar', metal_bar_photo())]:
    band = tape_band.find_band(image)
    print('\n== %s fixture' % label)
    if not band.get('found'):
        print('   no band: %s' % band.get('reason'))
        continue
    strip, _scale = _rectify(image, band['band'])
    report(label, tape_ticks.read_strip(strip))
