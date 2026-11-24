"""How many marks the lattice finds, against how many its own pitch implies.

`_fit_lattice` searched a fixed grid with a +/-0.3 pitch window. A pitch that is
off by a fraction of a percent drifts a whole pitch over 3000 px, so the window
runs away from the ladder and most marks are never seen.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from supervision import tape_ticks  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES = ['strip_140332.png', 'strip_141314.png', 'strip_21.png', 'strip_25.png', 'strip_28.png']

for name in NAMES:
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        print('%s: missing' % name)
        continue
    strip = Image.open(path)
    lattice = tape_ticks.read_strip(strip)
    if not lattice:
        print('%s (%dx%d): no lattice' % (name, strip.width, strip.height))
        continue
    expected = strip.width / lattice['pitch']
    print('%s (%dx%d) stripe %d: pitch %.2f count %d (%.0f of %.0f implied, %.0f%%) '
          'residual %.3f (%.2f px) score %.3f usable %s'
          % (name, strip.width, strip.height, lattice['stripe'],
             lattice['pitch'], lattice['count'], lattice['count'], expected,
             100.0 * lattice['count'] / max(1.0, expected),
             lattice['residual'], lattice['residual'] * lattice['pitch'],
             lattice['score'], lattice['usable']))
