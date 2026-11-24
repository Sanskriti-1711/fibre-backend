"""The metal bar's ladder, at the locator's resolution and the clip's.

The bar is refused as "no tape" at one working resolution and reaches the numbers
stage at another, so the gate has to be measured at both before its threshold can
be trusted.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

from supervision import tape_band, tape_clip, tape_ticks  # noqa: E402
from supervision.tape_reader import _rectify  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAMES = [
    'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
    '20261124_140332.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
]

print('clip detector edge: %s' % tape_clip.WORK_EDGE)
for name in NAMES:
    rgb = np.asarray(
        ImageOps.exif_transpose(Image.open(os.path.join(FIELD, name))).convert('RGB')
    )
    print('\n== %s' % name)
    for edge in (tape_band.WORK_EDGE, tape_clip.WORK_EDGE):
        blade = tape_band.find_band(rgb, work_edge=edge)
        if not blade.get('found'):
            print('   edge %s: no band (%s)' % (edge, blade.get('reason')))
            continue
        strip, _scale = _rectify(rgb, blade['band'])
        lattice = tape_ticks.read_strip(strip)
        print('   edge %s: confirmed=%s angle=%.1f strip %dx%d ladder %s'
              % (edge, blade.get('confirmed'), blade['angle_deg'],
                 strip.width, strip.height,
                 None if not lattice else {
                     'pitch': round(lattice['pitch'], 2),
                     'score': lattice['score'],
                     'count': lattice['count'],
                     'residual': round(lattice['residual'], 3),
                     'usable': lattice['usable'],
                 }))
