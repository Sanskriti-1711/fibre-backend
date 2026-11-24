"""How far each blade leaves the straight line it was fitted with.

The warp should follow a tape bending over a pipe and leave a straight blade
alone: a caption warped by tracking noise changed its profile enough to be
reported as a tape, which is a worse error than an unbent strip.
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
    'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.25 (1).jpeg',
    '20261124_140332.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
]

for name in NAMES:
    rgb = np.asarray(
        ImageOps.exif_transpose(Image.open(os.path.join(FIELD, name))).convert('RGB')
    )
    blade = tape_band.find_band(rgb, work_edge=tape_clip.WORK_EDGE)
    if not blade.get('found'):
        print('%s: no band' % name)
        continue
    band = blade['band']
    strip, _scale = _rectify(rgb, band)
    lattice = tape_ticks.read_strip(strip)
    # A bend-aware warp was measured here and reverted: the walk's per-column
    # centres are not a curve (they span up to 1.9x the blade width, because the
    # walk jumps between bright regions), so the record kept is the straight one.
    print('%s\n   bend %.1f px of a %.0f px blade\n   strip %dx%d ladder %s'
          % (name, blade['bend'] * blade['width_px'], blade['width_px'],
             strip.width, strip.height,
             None if not lattice else {
                 'pitch': round(lattice['pitch'], 2),
                 'score': lattice['score'],
                 'count': lattice['count'],
                 'residual': round(lattice['residual'], 3),
                 'usable': lattice['usable'],
                 'problem': tape_ticks.ladder_problem(lattice),
             }))
