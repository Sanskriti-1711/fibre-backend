"""Why the rendered clip photo now stops at the blade stage instead of the clip."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_band, tape_clip, tape_ticks  # noqa: E402
from supervision.tape_reader import _rectify, read  # noqa: E402
from supervision.tests_tape_clip import photo  # noqa: E402

for label, image in (('no clip', photo()), ('black clip', photo((420, 452, (20, 20, 20))))):
    result = read(image)
    print('%s -> stage %s: %s' % (label, result['stage'], result['reason']))
    blade = tape_band.find_band(image, work_edge=tape_clip.WORK_EDGE)
    print('   band found=%s confirmed=%s %s'
          % (blade.get('found'), blade.get('confirmed'), blade.get('reason') or ''))
    if not blade.get('found'):
        continue
    strip, _scale = _rectify(image, blade['band'])
    lattice = tape_ticks.read_strip(strip)
    print('   strip %dx%d lattice=%s' % (strip.width, strip.height, lattice and {
        key: lattice[key] for key in ('pitch', 'score', 'count', 'residual', 'usable')
    }))
    if lattice:
        print('   ladder_ok=%s residual_limit=%.3f'
              % (tape_ticks.ladder_ok(lattice),
                 min(tape_ticks.MAX_RESIDUAL, tape_ticks.MAX_RESIDUAL_PX / lattice['pitch'])))
