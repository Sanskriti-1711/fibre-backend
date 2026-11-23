"""The framing check: an even, whole row of tape numbers passes; anything else
that cannot be measured is sent back for a reshoot with a reason."""
import numpy as np
from django.test import SimpleTestCase

from supervision import tape_framing


def _blade(width=600, height=400, first=60, count=9, pitch=48) -> np.ndarray:
    """A pale blade with a row of printed digits and an edge line above them."""
    canvas = np.full((height, width), 210, dtype=np.uint8)
    # The blade's printed edge line: long, straight and not a digit.
    canvas[100:103, :] = 45

    y, w, h, stroke = 180, 16, 36, 4
    for i in range(count):
        x = first + i * pitch
        canvas[y:y + h, x:x + stroke] = 60                    # left stroke
        canvas[y:y + h, x + w - stroke:x + w] = 60            # right stroke
        canvas[y:y + stroke, x:x + w] = 60                    # top bar
        canvas[y + h - stroke:y + h, x:x + w] = 60            # bottom bar
    return canvas


class TapeFramingTests(SimpleTestCase):
    def test_an_even_ladder_inside_the_frame_passes(self):
        report = tape_framing.check_gray(_blade(), 'depth')

        self.assertTrue(report['ok'], report)
        self.assertEqual(report['verdict'], 'good')
        # The long edge line must be stripped, leaving the row of digits.
        self.assertGreaterEqual(report['detail']['ladder'], 7)

    def test_a_blade_running_out_of_the_frame_asks_for_a_reshoot(self):
        report = tape_framing.check_gray(_blade(first=0), 'depth')

        self.assertFalse(report['ok'])
        self.assertEqual(report['verdict'], 'cut_off')
        self.assertIn('brackets', report['message'])

    def test_a_photo_with_no_printed_row_is_not_a_framing_pass(self):
        blank = np.full((400, 600), 200, dtype=np.uint8)
        blank[::37, ::53] = 30       # speckle, not a printed row

        report = tape_framing.check_gray(blank, 'width')

        self.assertFalse(report['ok'])
        self.assertEqual(report['verdict'], 'no_tape')
        self.assertIn('printed numbers', report['message'])

    def test_bytes_that_are_not_an_image_do_not_raise(self):
        self.assertIsNone(tape_framing.check_bytes(b'not an image'))
