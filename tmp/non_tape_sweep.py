"""What the reader now says about the five blades the locator had confirmed.

Two of the five are not tape (a burned-in caption and a length of metal), and two
are tape. Every one of them used to be routed to the clip stage first.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supervision import tape_reader  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAMES = [
    'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.25 (1).jpeg',
    '20261124_140332.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
]

for name in NAMES:
    with open(os.path.join(FIELD, name), 'rb') as handle:
        data = handle.read()
    started = time.monotonic()
    result = tape_reader.read_bytes(data)
    elapsed = (time.monotonic() - started) * 1000.0
    print('%s\n   stage=%s usable=%s %.0f ms\n   %s'
          % (name, result['stage'], result['usable'], elapsed, result['reason']))
