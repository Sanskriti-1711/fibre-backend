"""What an uneven ladder and a perspective ladder cost, in pixels and in pitches.

The residual gate is a share of a pitch today; on a coarse ladder that share is
several pixels. This prints both units for the cases the gate has to separate.
"""

import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_ticks  # noqa: E402
from supervision.tests_tape_ticks import _strip  # noqa: E402


def show(name, strip):
    lattice = tape_ticks.read_strip(strip)
    if not lattice:
        print('%-22s no ladder' % name)
        return
    pitch = lattice['pitch']
    print('%-22s pitch %5.2f score %.3f count %3d residual %.3f (%.2f px) usable %s'
          % (name, pitch, lattice['score'], lattice['count'], lattice['residual'],
             lattice['residual'] * pitch, lattice['usable']))


show('regular 6 px', _strip(pitch=6.0))
show('regular 20 px', _strip(pitch=20.0))
show('regular 22 px', _strip(pitch=22.0))
show('perspective 5 px', _strip(pitch=5.0, drift=0.06))
show('perspective 8 px', _strip(pitch=8.0, drift=0.04))
rng = np.random.default_rng(3)
show('texture', Image.fromarray(
    rng.normal(190, 26, (120, 600)).clip(0, 255).astype(np.uint8)))
show('blank', Image.fromarray(np.full((120, 600), 210, dtype=np.uint8)))
