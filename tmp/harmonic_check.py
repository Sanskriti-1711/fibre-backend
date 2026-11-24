"""Is a field tape's ladder off its lattice, or fitted at the wrong pitch?

`141314` reports 77 marks where an 8.5 px pitch over 3000 px should find ~350,
and a residual of 2.5 px. A pitch chosen at a harmonic would show exactly that:
half the pitch, twice the marks, and marks that sit between the predicted ones.
This prints the fit at the chosen pitch, at half it and at twice it.
"""

import os
import sys

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_band, tape_ticks  # noqa: E402
from supervision.tape_reader import _rectify  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAMES = ['20261124_140332.jpg.jpeg', '20261124_141314.jpg.jpeg']


def stripes(strip):
    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    height = pixels.shape[0]
    stripe = max(10, int(height * tape_ticks.STRIPE_SHARE))
    step = max(4, height // 10)
    starts = list(range(0, max(1, height - stripe + 1), step))
    if starts and starts[-1] != height - stripe:
        starts.append(height - stripe)
    for start in starts:
        yield start, tape_ticks._detrended(
            tape_ticks._ink_profile(pixels[start:start + stripe, :]))


for name in NAMES:
    path = os.path.join(FIELD, name)
    rgb = np.asarray(ImageOps.exif_transpose(Image.open(path)).convert('RGB'))
    band = tape_band.find_band(rgb)
    print('\n== %s  found=%s confirmed=%s angle=%.1f'
          % (name, band.get('found'), band.get('confirmed'),
             band.get('angle_deg') or 0.0))
    if not band.get('found'):
        continue
    strip, _scale = _rectify(rgb, band['band'])
    print('   strip %dx%d' % (strip.size[0], strip.size[1]))
    for start, profile in stripes(strip):
        pitch, score = tape_ticks.pitch_from_profile(profile)
        if not pitch:
            continue
        row = ['   stripe %3d pitch %5.2f score %.3f' % (start, pitch, score)]
        for factor, tag in ((0.5, 'half'), (1.0, 'as-is'), (2.0, 'double')):
            trial = tape_ticks._fit_lattice(profile, pitch * factor)
            row.append('%s: n=%d res=%.3f (%.2f px) usable=%s'
                       % (tag, trial['count'], trial['residual'],
                          trial['residual'] * trial['pitch'],
                          tape_ticks.ladder_ok(dict(trial, score=score))))
        print('\n      '.join(row))
