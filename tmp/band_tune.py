"""Does tightening the walk's width window fix the crossing without slivers?

Runs with the ruler allowance disabled (RULER_PERIODICITY high), so each row is
the original gate set against one WIDTH_HIGH. Read-only.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from supervision import tape_band
from supervision.tests_tape_band import band_photo

FRAMES = {
    'plain': band_photo(),
    'paved': band_photo(second_band=(0.0, 74.0, 210.0)),
    'shaded': band_photo(ramp=1.0),
    'clip': band_photo(clip=(0.0, 8.0)),
    'numbers': band_photo(numbers=True),
}

original_high = tape_band.WIDTH_HIGH
tape_band.RULER_PERIODICITY = 99.0  # original gates only

for high in (original_high, 1.8, 1.6, 1.45, 1.3):
    tape_band.WIDTH_HIGH = high
    print(f'--- WIDTH_HIGH={high} (was {original_high})')
    for label, frame in FRAMES.items():
        result = tape_band._find_at(frame, 900)
        if result.get('found'):
            print(
                f'  {label:8} found w={result["width_px"]:6} angle={result["angle_deg"]:7}'
                f' spread={result["width_spread"]:.3f} bend={result["bend"] * result["width_px"]:.1f}px'
                f' per={result["periodicity"]:.3f} score={result["score"]:.3f}'
            )
        else:
            print(f'  {label:8} REFUSED {result.get("reason")}')
tape_band.WIDTH_HIGH = original_high
