"""The tick ladder: pitch, residual, and refusing a ladder that is not one.

These tests pin the three failures the spike showed on real photos: peaks that
count digits instead of ticks, a pitch that drifts under perspective, and
texture that looks periodic enough to fool a single threshold.
"""
import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from supervision import tape_ticks


def _strip(width=1200, height=120, pitch=6.0, drift=0.0, upside_down=False):
    """A pale blade with 1-unit ticks and a longer mark every ten."""
    canvas = np.full((height, width), 215, dtype=np.uint8)
    x, index = 20.0, 0
    while x < width - 4:
        major = index % 10 == 0
        length = 52 if major else 30
        left = int(round(x))
        if upside_down:
            canvas[:length, left : left + 2] = 40
            if major:
                canvas[height - 30 : height - 4, max(0, left - 4) : left + 24] = 30
        else:
            canvas[height - length :, left : left + 2] = 40
            if major:
                canvas[height - 92 : height - 54, max(0, left - 4) : left + 24] = 30
        x += pitch + drift * index
        index += 1
    return Image.fromarray(canvas)


class TapeTickTests(SimpleTestCase):
    def test_a_regular_ladder_reports_its_pitch_and_majors(self):
        lattice = tape_ticks.read_strip(_strip(pitch=6.0))

        self.assertIsNotNone(lattice)
        self.assertTrue(lattice['usable'], lattice)
        self.assertLess(abs(lattice['pitch'] - 6.0), 0.4, lattice)
        self.assertLess(lattice['residual'], 0.12, lattice)
        self.assertGreater(lattice['count'], 100)
        self.assertGreater(lattice['coverage'], 0.7)
        self.assertEqual(lattice['major_every'], 10)

    def test_a_blade_photographed_upside_down_still_yields_its_ladder(self):
        lattice = tape_ticks.read_strip(_strip(pitch=6.0, upside_down=True))

        self.assertIsNotNone(lattice)
        self.assertTrue(lattice['usable'], lattice)

    def test_texture_is_not_a_ladder(self):
        rng = np.random.default_rng(3)
        noise = rng.normal(190, 26, (120, 600)).clip(0, 255).astype(np.uint8)

        lattice = tape_ticks.read_strip(Image.fromarray(noise))

        self.assertTrue(lattice is None or not lattice['usable'], lattice)

    def test_a_ladder_stretched_by_perspective_is_refused(self):
        # A blade shot at an angle has one end printing nearer than the other;
        # the marks then are not on one pitch and the scale would drift.
        lattice = tape_ticks.read_strip(_strip(pitch=5.0, drift=0.06))

        self.assertTrue(lattice is None or not lattice['usable'], lattice)
        if lattice is not None:
            self.assertGreater(lattice['residual'], tape_ticks.MAX_RESIDUAL, lattice)

    def test_a_blank_strip_is_refused(self):
        blank = Image.fromarray(np.full((120, 600), 210, dtype=np.uint8))

        self.assertIsNone(tape_ticks.read_strip(blank))
