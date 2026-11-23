"""Locating and straightening the blade: a tilted tape must come out level.

The failure these tests lock down is the one measured on the field set: the old
angle search reported 0 degrees on a blade visibly tilted 7 degrees, so every
column profile afterwards mixed the marks with the ground. They also pin the
second failure: two field photos were thrown away because bright ground beside
the blade pushed a geometry threshold past its limit, so the band is now chosen
by the ladder it yields.
"""
import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from supervision import tape_frame, tape_ticks


def _photo(angle: float = 7.0, width: int = 1000, height: int = 650) -> np.ndarray:
    """A printed blade on gravel, tilted by ``angle`` degrees."""
    rng = np.random.default_rng(7)
    soil = rng.normal(95, 12, (height, width)).clip(0, 255).astype(np.uint8)

    blade = np.full((70, 820), 215, dtype=np.uint8)
    for i, x in enumerate(range(30, 800, 8)):
        top = 70 - (46 if i % 10 == 0 else 26)
        blade[top:, x : x + 2] = 40
    for i, x in enumerate(range(30, 800, 8)):
        if i % 10 == 0:
            blade[4:20, x - 2 : x + 22] = 35

    tape = Image.fromarray(blade).rotate(
        angle, resample=Image.BICUBIC, expand=True, fillcolor=95
    )
    canvas = Image.fromarray(soil)
    canvas.paste(tape, ((width - tape.width) // 2, (height - tape.height) // 2))
    return np.asarray(canvas)


class TapeFrameTests(SimpleTestCase):
    def test_a_tilted_blade_is_found_and_brought_level(self):
        framed = tape_frame.frame_gray(_photo(7.0))

        self.assertIsNotNone(framed)
        # The sign depends on the rotation direction; the magnitude is what the
        # warp needs, and the old estimator returned 0 here.
        self.assertLess(abs(abs(framed['angle']) - 7.0), 2.0)
        self.assertGreaterEqual(framed['height_px'], tape_frame.STRIP_MIN_HEIGHT)
        self.assertLessEqual(framed['height_px'], tape_frame.STRIP_MAX_HEIGHT)
        self.assertGreater(framed['width_px'], 600)

        # The proof the warp worked: the straightened strip's own tick ladder is
        # periodic at the pitch the blade was printed with (8 px).
        lattice = tape_ticks.read_strip(framed['strip'])
        self.assertIsNotNone(lattice)
        self.assertLess(abs(lattice['pitch'] - 8.0) / 8.0, 0.15, lattice)
        self.assertTrue(lattice['usable'], lattice)
        self.assertTrue(framed['tick_usable'], framed['ticks'])

    def test_a_vertical_blade_is_still_brought_level(self):
        framed = tape_frame.frame_gray(np.rot90(_photo(0.0), k=1))

        self.assertIsNotNone(framed)
        lattice = tape_ticks.read_strip(framed['strip'])
        self.assertIsNotNone(lattice)
        self.assertTrue(lattice['usable'], lattice)

    def test_bright_ground_does_not_outrank_the_blade(self):
        # A wide, bright, textured patch of gravel sits under the blade. It wins
        # any brightness or area comparison, and it has no ladder, so the band
        # chosen by tick score must still be the blade.
        photo = _photo(0.0).copy()
        rng = np.random.default_rng(11)
        photo[470:640, 30:970] = rng.normal(228, 12, (170, 940)).clip(0, 255).astype(
            np.uint8
        )

        framed = tape_frame.frame_gray(np.asarray(photo))

        self.assertIsNotNone(framed)
        self.assertTrue(framed['tick_usable'], framed.get('ticks'))
        self.assertLess(abs(framed['angle']), 2.0)

    def test_a_frame_with_no_blade_is_refused(self):
        self.assertIsNone(tape_frame.frame_gray(np.full((400, 600), 200, dtype=np.uint8)))

    def test_bytes_that_are_not_an_image_do_not_raise(self):
        self.assertIsNone(tape_frame.frame_bytes(b'not an image'))
