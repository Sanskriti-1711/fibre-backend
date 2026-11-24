"""What one viewfinder frame costs the server.

The live overlay is one full read per frame, so its ceiling is this number, not
the camera's. Measured here rather than guessed: if a frame costs 2 s, no amount
of client-side work makes the overlay live, and that is the decision about
on-device processing.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_framing, tape_reader  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAMES = [
    '20261124_140332.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
]

for name in NAMES:
    with open(os.path.join(FIELD, name), 'rb') as handle:
        data = handle.read()
    started = time.monotonic()
    reading = tape_reader.read_bytes(data)
    clip_ms = (time.monotonic() - started) * 1000
    started = time.monotonic()
    tape_framing.check_bytes(data, 'depth')
    framing_ms = (time.monotonic() - started) * 1000
    print('%-52s clip %5.0f ms + framing %5.0f ms = %5.0f ms  (%s)'
          % (name[-52:], clip_ms, framing_ms, clip_ms + framing_ms,
             reading.get('stage') or 'usable'))
