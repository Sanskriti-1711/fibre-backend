"""Per-resolution results and fragment verdicts for the shaded blade.

Run from fiber-backend with the venv python. Read-only.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from supervision import tape_band
from supervision.tests_tape_band import band_photo

for label, frame in (
    ('shaded', band_photo(ramp=1.0)),
    ('paved', band_photo(second_band=(0.0, 74.0, 210.0))),
):
    print(f'=== {label}')
    attempts = []
    for edge in (900, 702, 1080):
        result = tape_band._find_at(frame, edge)
        if not result.get('found'):
            print(f'  edge {edge}: REFUSED {result.get("reason")}')
            continue
        attempts.append(result)
        print(
            f'  edge {edge}: w={result["width_px"]:.1f} angle={result["angle_deg"]:.1f}'
            f' centre=({result["centre"][0]:.0f},{result["centre"][1]:.0f})'
            f' score={result["score"]:.3f} per={result["periodicity"]:.3f}'
        )

    for i, left in enumerate(attempts):
        for j, right in enumerate(attempts):
            if i == j:
                continue
            verdict = tape_band._is_fragment(
                tape_band._as_band(left), tape_band._as_band(right)
            )
            if verdict:
                print(f'    fragment: [{i}] w={left["width_px"]:.0f} of [{j}] w={right["width_px"]:.0f}')

    final = tape_band.find_band(frame, work_edge=900)
    print(
        f'  FINAL: found={final.get("found")} w={final.get("width_px")}'
        f' angle={final.get("angle_deg")} reason={final.get("reason")}'
        f' attempts={final.get("attempts")}'
    )
