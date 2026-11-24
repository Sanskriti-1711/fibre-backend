"""Why the sloping synthetic blade reads as curving: the tracked centre series."""

import os
import sys

import numpy as np

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), 'tmp'))

from supervision import tape_band
from supervision.tests_tape_band import band_photo


def look(label, rgb):
    print('\n== %s' % label)
    image_gray = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    seeds = tape_band._seeds(image_gray)
    print('   seeds=%d' % len(seeds))
    for index, seed in enumerate(seeds):
        band = {key: seed[key] for key in ('centre', 'axis', 'across', 'length', 'width')}
        track = tape_band._walk(image_gray, band)
        fit = tape_band._fit(track)
        print('   seed #%d angle=%.1f W=%.1f  -> %s'
              % (index, seed['angle'], seed['width'],
                 'ok' if fit.get('ok') else fit.get('reason')))
        if not fit.get('ok'):
            continue
        us = track['us']
        centres = track['centres']
        widths = track['widths']
        valid = np.isfinite(centres)
        print('      valid=%d/%d  cov=%.2f  W=%.1f  spread=%.2f  bend=%.2f (%.1f px)'
              % (int(valid.sum()), us.size, fit['coverage'], fit['width'],
                 fit['spread'], fit['bend'], fit['bend'] * fit['width']))
        chosen = np.flatnonzero(valid)
        picks = chosen[np.linspace(0, chosen.size - 1, 12).astype(int)]
        print('      ' + ' '.join('%.0f:%.1f/%.0f'
                                   % (us[i], centres[i], widths[i]) for i in picks))


def widen(label, rgb):
    print('\n== %s' % label)
    gray = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    for index, seed in enumerate(tape_band._seeds(gray)):
        band = {key: seed[key] for key in ('centre', 'axis', 'across', 'length', 'width')}
        widened = tape_band._widen(gray, band)
        print('   seed #%d angle=%.1f seedW=%.1f  widen=%s'
              % (index, seed['angle'], seed['width'],
                 None if widened is None
                 else 'W=%.1f offset=%.1f' % widened))


look('straight', band_photo())
look('sloping 22deg', band_photo(angle_deg=22.0))
widen('shaded', band_photo(ramp=1.0))
