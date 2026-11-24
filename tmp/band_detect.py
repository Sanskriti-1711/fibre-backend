"""Phase 1 check: does the blade tracker find the tape in the real photos?

Prints one line per photo and writes an annotated overlay beside the crops in
`tmp/view/` so the band can be looked at rather than trusted.
"""

import glob
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), 'tmp'))

from supervision import tape_band

SET_A = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
ROOT = 'D:/Downloads_D/Q-GIS'
OUT = os.path.join(os.getcwd(), 'tmp', 'view')


def report(path):
    name = os.path.basename(path).split('.')[0]
    rgb = np.asarray(Image.open(path).convert('RGB'))
    result = tape_band.find_band(rgb)
    if result['found']:
        print('%-24s ok  angle=%6.1f  W=%6.1fpx  L=%7.1fpx  cov=%.2f sup=%.2f '
              'spr=%.2f per=%.2f  rivals=%d'
              % (name, result['angle_deg'], result['width_px'], result['length_px'],
                 result['coverage'], result['support'], result['width_spread'],
                 result['periodicity'], result['competitors']))
    else:
        print('%-24s REFUSED  %s' % (name, result['reason']))
    overlay = tape_band.annotate(rgb, result, width=1000)
    os.makedirs(OUT, exist_ok=True)
    overlay.save(os.path.join(OUT, 'band_' + name + '.png'))


def main():
    phone = sorted(glob.glob(os.path.join(SET_A, '2026*.jpg.jpeg')))
    whatsapp = {
        os.path.basename(path): path
        for path in glob.glob(
            os.path.join(ROOT, '**', 'WhatsApp Image 2026-11-24 at 08.38.*.jpeg'),
            recursive=True,
        )
    }
    print('== phone originals (%d)' % len(phone))
    for path in phone:
        report(path)
    print('== whatsapp photos (%d)' % len(whatsapp))
    for path in whatsapp.values():
        report(path)


if __name__ == '__main__':
    main()
