"""What the ladder says about each confirmed blade, in numbers.

Prints the band's own evidence and then the tick fit for the rectified strip,
with the residual in *pixels* as well as in pitches, so a gate can be set on the
pixels the marks are actually off by.
"""

from __future__ import annotations

import glob
import os
import sys

import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_band, tape_ticks  # noqa: E402
from supervision.tape_reader import _rectify  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
TOKENS = ['08.38.21', '08.38.28 (1)', '08.38.25 (1)', '140332', '141314']


def hits(token: str) -> list[str]:
    found = [
        os.path.join(FIELD, name)
        for name in os.listdir(FIELD)
        if token in name and 'annotated' not in name
    ]
    return sorted(found)


def show(path: str) -> None:
    name = os.path.basename(path)
    rgb = np.asarray(ImageOps.exif_transpose(Image.open(path)).convert('RGB'))
    band = tape_band.find_band(rgb)
    print('\n== %s  %dx%d' % (name, rgb.shape[1], rgb.shape[0]))
    if not band.get('found'):
        print('   no band: %s' % band.get('reason'))
        return
    print('   band found=%s confirmed=%s angle=%.1f width=%.1f length=%.1f '
          'cov=%.2f spread=%.2f bend=%.2f periodicity=%.2f score=%.3f'
          % (band['found'], band.get('confirmed'), band['angle_deg'],
             band['width_px'], band['length_px'], band['coverage'],
             band['width_spread'], band['bend'], band['periodicity'],
             band['score']))
    strip, scale = _rectify(rgb, band['band'])
    print('   strip %dx%d (scale %.2f)' % (strip.width, strip.height, scale))
    lattice = tape_ticks.read_strip(strip)
    if not lattice:
        print('   no ladder at all')
        return
    pitch = lattice['pitch']
    print('   ladder pitch=%.2f score=%.3f count=%d coverage=%.2f '
          'residual=%.3f (%.2f px) usable=%s major_every=%s'
          % (pitch, lattice['score'], lattice['count'], lattice['coverage'],
             lattice['residual'], lattice['residual'] * pitch,
             lattice['usable'], lattice['major_every']))
    positions = lattice['positions']
    if len(positions) >= 3:
        steps = np.round((np.array(positions, float) - positions[0]) / pitch)
        slope, intercept = np.polyfit(steps, np.array(positions, float), 1)
        dev = np.array(positions, float) - (intercept + slope * steps)
        print('   mark deviations px: max %.2f  p90 %.2f  rms %.2f'
              % (float(np.abs(dev).max()), float(np.percentile(np.abs(dev), 90)),
                 float(np.sqrt(np.mean(dev ** 2)))))


for token in TOKENS:
    found = hits(token)
    if not found:
        print('\n== %s: no photo matched' % token)
    for item in found:
        show(item)
