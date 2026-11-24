"""Where the band tracker gives up, photo by photo, plus a lookable copy."""

import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), 'tmp'))

from supervision import tape_band

SET_A = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
OUT = os.path.join(os.getcwd(), 'tmp', 'view')

PHOTOS = [
    '20261124_140332.jpg.jpeg',
    '20261124_140529.jpg.jpeg',
    '20261124_141314.jpg.jpeg',
    '20261124_140357_1.jpg.jpeg',
]


def main():
    os.makedirs(OUT, exist_ok=True)
    for name in PHOTOS:
        path = os.path.join(SET_A, name)
        if not os.path.exists(path):
            print('missing', name)
            continue
        rgb = np.asarray(Image.open(path).convert('RGB'))
        print('\n== %s  %dx%d' % (name, rgb.shape[1], rgb.shape[0]))

        image = Image.fromarray(rgb).convert('RGB')
        image.thumbnail((tape_band.WORK_EDGE, tape_band.WORK_EDGE), Image.BILINEAR)
        gray = np.asarray(image, dtype=np.float32) @ np.array(
            [0.299, 0.587, 0.114], dtype=np.float32)
        seeds_image = image.copy()
        seeds_image.thumbnail((tape_band.SEED_EDGE, tape_band.SEED_EDGE), Image.BILINEAR)
        seeds_gray = np.asarray(seeds_image, dtype=np.float32) @ np.array(
            [0.299, 0.587, 0.114], dtype=np.float32)
        level = tape_band.otsu_level(seeds_gray)
        mask = seeds_gray > level
        print('   work %dx%d  seeds %dx%d  otsu=%.1f  p88=%.1f  bright=%.3f'
              % (image.width, image.height, seeds_image.width, seeds_image.height,
                 level, np.percentile(seeds_gray, tape_band.SEED_PERCENTILE),
                 mask.mean()))
        to_work = seeds_image.width / float(image.width)
        for index, seed in enumerate(tape_band._seeds(seeds_gray)):
            print('     seed #%d share=%.2f  W=%.1f L=%.1f  angle=%.1f'
                  % (index, seed['share'], seed['width'] / to_work,
                     seed['length'] / to_work, seed['angle']))
            proposed = {
                'centre': seed['centre'] / to_work,
                'axis': seed['axis'],
                'across': seed['across'],
                'length': seed['length'] / to_work,
                'width': seed['width'] / to_work,
            }
            result = tape_band._grade(proposed, gray)
            if result.get('ok'):
                print('        grade ok  cov=%.2f sup=%.2f spr=%.2f bend=%.2f '
                      'per=%.2f score=%.2f  W=%.1f'
                      % (result['coverage'], result['support'],
                         result['width_spread'], result['bend'],
                         result['periodicity'], result['score'],
                         result['width']))
            else:
                print('        grade no  %s' % result['reason'])

        look = Image.fromarray(rgb)
        look.thumbnail((900, 900), Image.BILINEAR)
        look.save(os.path.join(OUT, 'look_' + name.split('.')[0] + '.jpg'), quality=85)


if __name__ == '__main__':
    main()
