"""Tests for the blade tracker, on frames whose answer is known by construction.

The field set is what these numbers have to hold up against, but a test cannot
assert on a photo. Each case below is a tape drawn at a known angle and width,
with the things the real photos contain — millimetre ticks, printed numbers, a
clip, a brightness ramp — and one case with a second bright band that would win
on area alone, which is the failure that sent this module back to the drawing
board.
"""

import numpy as np
from django.test import SimpleTestCase

from supervision import tape_band

BLADE = (232, 226, 205)
GROUND = (58, 55, 50)
MARK = (40, 38, 36)


def band_photo(
    angle_deg=0.0,
    centre=(480.0, 350.0),
    length=700.0,
    width=110.0,
    clip=None,
    numbers=False,
    ramp=0.0,
    second_band=None,
):
    """A tape on a dark ground, drawn at `angle_deg` with the given geometry."""
    height, width_px = 700, 960
    yy, xx = np.mgrid[0:height, 0:width_px].astype(np.float32)
    angle = np.radians(angle_deg)
    axis = np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)
    across = np.array([-np.sin(angle), np.cos(angle)], dtype=np.float32)
    du = xx - centre[0]
    dv = yy - centre[1]
    u = du * axis[0] + dv * axis[1]
    v = du * across[0] + dv * across[1]

    gray = np.full((height, width_px), GROUND[0], dtype=np.float32)
    if second_band is not None:
        gap, run_angle, run_width = second_band
        bare = np.radians(run_angle)
        s_axis = np.array([np.cos(bare), np.sin(bare)], dtype=np.float32)
        s_across = np.array([-np.sin(bare), np.cos(bare)], dtype=np.float32)
        su = (xx - 240.0) * s_axis[0] + (yy - 200.0) * s_axis[1]
        sv = (xx - 240.0) * s_across[0] + (yy - 200.0) * s_across[1]
        paved = (np.abs(sv) < run_width / 2.0) & (np.abs(su) < 900.0)
        gray[paved] = 224.0
        del gap

    tape = (np.abs(v) < width / 2.0) & (np.abs(u) < length / 2.0)
    gray[tape] = BLADE[0]
    if ramp:
        gray[tape] = BLADE[0] * (1.0 - ramp * (u[tape] / length + 0.5) * 0.35)

    # Millimetre ticks: a row of marks every 6 px along the middle of the blade,
    # with bare blade left above and below, as on the real tape.
    if tape.any():
        tick = tape & (np.abs(np.mod(np.abs(u) + 0.5, 6.0) - 0.5) < 0.8)
        tick &= np.abs(v) < 0.35 * width
        gray[tick] = MARK[0]

    if numbers:
        for index, start in enumerate(range(-40, 260, 60)):
            digit = (np.abs(u - start) < 9.0) & (np.abs(v) < 0.26 * width)
            gray[digit] = MARK[0] + index

    if clip is not None:
        centre_u, half = clip
        block = (np.abs(u - centre_u) <= half) & (np.abs(v) < 0.8 * width)
        gray[block] = 30.0

    rgb = np.stack(
        [gray, gray * 0.97, gray * 0.88], axis=2
    )
    rgb += np.random.default_rng(7).normal(0.0, 2.0, rgb.shape)
    return np.clip(rgb, 0, 255).astype(np.uint8)


class BandTrackingTests(SimpleTestCase):
    def test_a_straight_blade_is_found_with_its_width(self):
        result = tape_band.find_band(band_photo(), work_edge=900)
        self.assertTrue(result['found'], result.get('reason'))
        self.assertAlmostEqual(result['width_px'], 110.0, delta=13.0)
        self.assertAlmostEqual(result['angle_deg'], 0.0, delta=2.0)
        self.assertGreater(result['length_px'], 0.8 * 700.0)

    def test_a_sloping_blade_is_found_with_its_angle(self):
        result = tape_band.find_band(band_photo(angle_deg=22.0), work_edge=900)
        self.assertTrue(result['found'], result.get('reason'))
        self.assertAlmostEqual(result['angle_deg'], 22.0, delta=2.5)
        self.assertAlmostEqual(result['width_px'], 110.0, delta=15.0)

    def test_the_band_is_tracked_past_a_clip(self):
        # A clip splits the blade in two; the track has to cross the gap, or the
        # reader will look for the reference face at the end of the band.
        result = tape_band.find_band(
            band_photo(clip=(0.0, 8.0)), work_edge=900
        )
        self.assertTrue(result['found'], result.get('reason'))
        self.assertGreater(result['length_px'], 0.85 * 700.0)
        self.assertAlmostEqual(result['width_px'], 110.0, delta=15.0)

    def test_printed_numbers_do_not_pull_the_track_off_the_blade(self):
        result = tape_band.find_band(band_photo(numbers=True), work_edge=900)
        self.assertTrue(result['found'], result.get('reason'))
        self.assertAlmostEqual(result['width_px'], 110.0, delta=18.0)

    def test_a_shaded_blade_is_still_found(self):
        result = tape_band.find_band(band_photo(ramp=1.0), work_edge=900)
        self.assertTrue(result['found'], result.get('reason'))
        self.assertAlmostEqual(result['width_px'], 110.0, delta=20.0)

    def test_ground_with_no_blade_is_refused(self):
        frame = np.full((700, 960, 3), float(GROUND[0]), dtype=np.float32)
        frame += np.random.default_rng(3).normal(0.0, 2.0, frame.shape)
        result = tape_band.find_band(
            np.clip(frame, 0, 255).astype(np.uint8)
        )
        self.assertFalse(result['found'])
        self.assertTrue(result['reason'])

    def test_the_tape_wins_over_a_wider_bright_band(self):
        # The 20261124_140357_1 failure: a paved edge, brighter by area and
        # wider than the blade, in the same tone. The ruler's ticks decide.
        result = tape_band.find_band(
            band_photo(second_band=(0.0, 74.0, 210.0)), work_edge=900
        )
        self.assertTrue(result['found'], result.get('reason'))
        self.assertAlmostEqual(result['width_px'], 110.0, delta=16.0)
        # The angle matters as much as the width: a merged band came back 22
        # degrees off a horizontal blade once, and it passed a width-only
        # assertion. A band at the wrong angle is the wrong band.
        self.assertAlmostEqual(result['angle_deg'], 0.0, delta=3.0)
