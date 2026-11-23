"""The printed numbers: labels found by their own ink, and a scale that is checked.

The OCR is injected in every test here. That is deliberate: the arithmetic that
turns "10 at x=20, 20 at x=140" into a millimetre scale is what these tests are
about, and a real tesseract run on a rendered blade would make them depend on a
binary and a version rather than on this module.
"""
import numpy as np
from django.test import SimpleTestCase
from PIL import Image

from supervision import tape_digits, tape_ticks


def _strip(width=1200, height=90, pitch=12.0, every=10, upside_down=False, ink=True):
    """A pale blade with minor and major marks and a label over every `every`th.

    The label blocks are ink only: the OCR that reads them is injected, so what
    matters is where a number sits, not what it says.
    """
    canvas = np.full((height, width), 215, dtype=np.uint8)
    x, index = 20.0, 0
    while x < width - 4:
        major = index % 10 == 0
        length = 50 if major else 26
        left = int(round(x))
        if upside_down:
            canvas[:length, left : left + 2] = 40
            label_rows = (length + 6, length + 26)
        else:
            canvas[height - length :, left : left + 2] = 40
            label_rows = (height - 74, height - 54)
        if ink and every and index % every == 0:
            top, stop = label_rows
            # Two glyphs with a small gap, so a label is one run and the next
            # label is far away.
            canvas[top:stop, max(0, left - 5) : left - 2] = 35
            canvas[top:stop, left + 2 : left + 6] = 35
        x += pitch
        index += 1
    return Image.fromarray(canvas)


class FakeOCR:
    """Returns the table's text per mark, and records what it was handed."""

    def __init__(self, table, confidence=0.9):
        self.table = dict(table)
        self.confidence = confidence
        self.calls = []

    def __call__(self, cell, mark):
        self.calls.append((mark, cell.size))
        text = self.table.get(mark['index'])
        if not text:
            return '', 0.0
        return text, self.confidence


def _lattice(strip):
    lattice = tape_ticks.read_strip(strip)
    assert lattice is not None and lattice['usable'], lattice
    return lattice


class DigitLabelTests(SimpleTestCase):
    def test_labels_on_the_marks_set_a_centimetre_scale(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)

        scale = tape_digits.read_labels(
            strip, lattice, ocr=FakeOCR({index: str(10 + index) for index in range(0, 120, 10)})
        )

        self.assertTrue(scale['usable'], scale)
        self.assertEqual(scale['unit'], 'cm')
        self.assertAlmostEqual(scale['mm_per_tick'], 10.0, delta=0.2)
        # 12 px per centimetre, so 10 mm over 12 px.
        self.assertAlmostEqual(scale['mm_per_px'], 10.0 / 12.0, delta=0.01)
        self.assertAlmostEqual(scale['strip_mm'], 1000.0, delta=25.0)
        self.assertEqual(scale['standard_tick_mm'], 10.0)
        self.assertLess(scale['residual_mm'], 0.5, scale)
        # Every tenth mark carries a label, and each label is one anchor.
        self.assertEqual(
            len(scale['anchors']), len(range(0, len(lattice['positions']), 10))
        )

    def test_a_millimetre_blade_is_read_in_millimetres(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)

        scale = tape_digits.read_labels(
            strip,
            lattice,
            ocr=FakeOCR({index: str(1000 + index) for index in range(0, 120, 10)}),
        )

        self.assertTrue(scale['usable'], scale)
        self.assertEqual(scale['unit'], 'mm')
        # A millimetre a mark, printed as 1000, 1010 … one mark per millimetre.
        self.assertAlmostEqual(scale['mm_per_tick'], 1.0, delta=0.05)
        self.assertAlmostEqual(scale['mm_per_px'], 1.0 / 12.0, delta=0.01)

    def test_one_misread_number_does_not_tilt_the_scale(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)
        table = {index: str(10 + index) for index in range(0, 120, 10)}
        table[50] = '300'

        scale = tape_digits.read_labels(strip, lattice, ocr=FakeOCR(table))

        self.assertTrue(scale['usable'], scale)
        self.assertAlmostEqual(scale['mm_per_px'], 10.0 / 12.0, delta=0.01)
        self.assertNotIn('300', [anchor['text'] for anchor in scale['anchors']])

    def test_numbers_that_do_not_share_one_spacing_earn_no_scale(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)
        # The first labels step one per mark, the far ones 1.4: no single
        # printed spacing, which is what a misaligned blade looks like.
        table = {}
        for index in range(0, 120, 10):
            table[index] = str(10 + index if index <= 40 else 50 + 14 * (index - 50) // 10)

        scale = tape_digits.read_labels(strip, lattice, ocr=FakeOCR(table))

        self.assertFalse(scale['usable'], scale)
        self.assertIn('disagree', scale['reason'])

    def test_numbers_the_marks_cannot_explain_earn_no_scale(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)
        # Numbers stepping 3 cm per 1 cm mark: a misread a single pass would
        # happily turn into a scale.
        scale = tape_digits.read_labels(
            strip,
            lattice,
            ocr=FakeOCR({index: str(10 + 3 * index) for index in range(0, 120, 10)}),
        )

        self.assertFalse(scale['usable'], scale)
        self.assertIn('disagree', scale['reason'])

    def test_a_blade_with_no_readable_numbers_says_so(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)

        scale = tape_digits.read_labels(strip, lattice, ocr=FakeOCR({}))

        self.assertFalse(scale['usable'], scale)
        self.assertIn('too few', scale['reason'])
        self.assertEqual(scale['anchors'], [])

    def test_a_blade_without_printed_numbers_gets_no_regions(self):
        strip = _strip(pitch=12.0, every=10, ink=False)
        lattice = _lattice(strip)
        readings = FakeOCR({})

        scale = tape_digits.read_labels(strip, lattice, ocr=readings)

        self.assertFalse(scale['usable'], scale)
        self.assertIn('no printed numbers', scale['reason'])
        self.assertEqual(readings.calls, [])

    def test_each_label_is_cropped_whole_and_clear_of_the_marks(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)
        readings = FakeOCR({index: str(10 + index) for index in range(0, 120, 10)})

        tape_digits.read_labels(strip, lattice, ocr=readings)

        self.assertEqual(
            len(readings.calls), len(range(0, len(lattice['positions']), 10))
        )
        for mark, (width, height) in readings.calls:
            # One label, not the whole blade, and tall enough to read.
            self.assertLess(width, 120, mark)
            self.assertGreaterEqual(height, tape_digits.CELL_UPSCALE_HEIGHT, mark)
            self.assertEqual(mark['index'] % 10, 0, mark)

    def test_upside_down_numbers_are_looked_for_below_the_marks(self):
        strip = _strip(pitch=12.0, every=10, upside_down=True)
        lattice = _lattice(strip)

        scale = tape_digits.read_labels(
            strip,
            lattice,
            ocr=FakeOCR({index: str(10 + index) for index in range(0, 120, 10)}),
        )

        self.assertTrue(scale['usable'], scale)
        self.assertFalse(scale['band']['from_bottom'])

    def test_a_blade_without_marks_cannot_align_anything(self):
        strip = _strip(pitch=12.0, every=10)

        self.assertIsNone(tape_digits.read_labels(None, None))
        self.assertIsNone(
            tape_digits.read_labels(strip, {'positions': [], 'pitch': 12.0})
        )

    def test_an_uneven_ladder_is_refused_before_any_ocr_runs(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = dict(_lattice(strip))
        lattice['usable'] = False
        readings = FakeOCR({})

        scale = tape_digits.read_labels(strip, lattice, ocr=readings)

        self.assertFalse(scale['usable'], scale)
        self.assertEqual(readings.calls, [])
