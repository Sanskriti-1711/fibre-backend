"""Run the real reader, end to end, on the field set and print the verdict.

    python tmp/read_field.py [--live WIDTH]
"""

from __future__ import annotations

import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageOps  # noqa: E402

from supervision import tape_reader  # noqa: E402

FIELD = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
NAMES = [
    '20261124_140332.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.21.jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.28 (1).jpeg',
    'WhatsApp Image 2026-11-24 at 08.38.25 (1).jpeg',
]
LIVE_WIDTH = int(sys.argv[sys.argv.index('--live') + 1]) if '--live' in sys.argv else 0


def payload(path: str) -> bytes:
    if not LIVE_WIDTH:
        # What the phone actually uploads: the untouched JPEG.
        with open(path, 'rb') as handle:
            return handle.read()
    image = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
    if LIVE_WIDTH and image.width > LIVE_WIDTH:
        height = int(round(image.height * LIVE_WIDTH / image.width))
        image = image.resize((LIVE_WIDTH, height), Image.BILINEAR)
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=60)
    return buffer.getvalue()


for name in NAMES:
    data = payload(os.path.join(FIELD, name))
    started = time.monotonic()
    result = tape_reader.read_bytes(data)
    elapsed = int((time.monotonic() - started) * 1000)
    print('\n== %s  %d KB  %d ms' % (name, len(data) // 1024, elapsed))
    print(
        '   usable=%s stage=%s reading=%s mm'
        % (result['usable'], result['stage'], result['reading_mm'])
    )
    if result['usable']:
        print(
            '   label %s at clip face %s · %s div · confirmed=%s'
            % (
                result['label']['text'],
                result['reference_face'],
                result['divisions_to_face'],
                result['blade_confirmed'],
            )
        )
    else:
        print('   reason: %s' % result['reason'])
