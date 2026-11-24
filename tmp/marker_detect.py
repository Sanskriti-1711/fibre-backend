"""Run the clip detector over the field sets and compare it with the cyan mask."""
import glob
import os
import sys

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), 'tmp'))

import numpy as np
from PIL import Image

from marker import find as cyan_find
from supervision import tape_clip

ROOT = r'D:/Downloads_D/Q-GIS/Demo_ready'
SET_A = ROOT + '/with_scale_images'


def report(path):
    name = os.path.basename(path).split('.')[0].replace('WhatsApp Image 2026-11-24 at 08.38.', 'wa-')
    data = open(path, 'rb').read()
    rgb = np.asarray(Image.open(path).convert('RGB'))
    result = tape_clip.find_clip(rgb)
    _im, info, _mask, _k = cyan_find(data)
    cyan = 'none'
    if info:
        cyan = '%.0f' % info['clip_across_tape_px']
    if not result['found']:
        print('%-26s REFUSED  %-52s cyan=%s' % (name, result['reason'], cyan),
              flush=True)
        return result
    faces = sorted(face[0] for face in result['faces'])
    print('%-26s %-6s conf=%.2f along=%5.0fpx width=%5.0fpx faces_x=%6.0f/%6.0f'
          ' agree=%s cyan=%s'
          % (name, result['method'], result['confidence'], result['along_tape_px'],
             result['tape_width_px'], faces[0], faces[1],
             ','.join(result['agreement']) or '-', cyan), flush=True)
    return result


def main():
    a = sorted(glob.glob(SET_A + '/2026*.jpg.jpeg'))
    b = {os.path.basename(path): path for path in
         glob.glob(ROOT + '/**/WhatsApp Image 2026-11-24 at 08.38.*.jpeg',
                   recursive=True)}
    print('== phone originals (%d)' % len(a))
    for path in a:
        report(path)
    print('== whatsapp photos (%d)' % len(b))
    for path in b.values():
        report(path)


main()
