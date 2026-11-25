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


def _two_row_strip(height=120, width=1200, pitch=12.0, every=10):
    """A blade printing two lines of numbers, as a survey tape does.

    The divisions sit above the units row, and the fine hatching of the blade
    runs between them: the gap is not blank, so a row is printed or not by how
    much ink it carries, not by whether it carries any.
    """
    canvas = np.full((height, width), 215, dtype=np.uint8)
    x, index = 20.0, 0
    while x < width - 4:
        major = index % 10 == 0
        length = 40 if major else 22
        left = int(round(x))
        canvas[height - length :, left : left + 2] = 40
        if every and index % every == 0:
            # Divisions: a wide, two-stroke label. Units: one narrower glyph.
            canvas[height - 100 : height - 84, max(0, left - 8) : left + 8] = 35
            canvas[height - 78 : height - 62, max(0, left - 4) : left + 4] = 35
        x += pitch
        index += 1
    # The hatch between the rows: a speck in every row, the width of one glyph.
    canvas[height - 84 : height - 78, 50:52] = 120
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


def _trench_strip(left=300, right=900, shade=70, rows=None):
    """The same blade laid across a trench: darker between two columns.

    ``rows`` restricts the darkening to some rows, which is what a mark, a stain
    or a hand shadow looks like — only part of the blade changes level.
    """
    strip = np.asarray(_strip().convert('L')).copy()
    top, bottom = rows if rows else (0, strip.shape[0])
    strip[top:bottom, left:right] = np.clip(
        strip[top:bottom, left:right].astype(np.int16) - shade, 0, 255
    ).astype(np.uint8)
    return Image.fromarray(strip)


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

    def test_a_weak_ladder_cannot_align_numbers(self):
        # Texture repeats a little; it is not a ladder, whatever its residual.
        strip = _strip(pitch=12.0, every=10)
        lattice = dict(_lattice(strip), usable=False, score=0.40)

        scale = tape_digits.read_labels(
            strip,
            lattice,
            ocr=FakeOCR({index: str(10 + index) for index in range(0, 120, 10)}),
        )

        self.assertFalse(scale['usable'], scale)
        self.assertIn('even', scale['reason'])

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

    def test_a_label_clipped_away_from_its_mark_is_dropped_not_the_scale(self):
        # A box clipped by the strip's end, or fused with the shade at a trench
        # wall, still reads the right number but its centre lands on another
        # mark. Its value fits the line and its position does not; on a shaded
        # angled upload that single anchor made the halves disagree and the span
        # was refused although every number had read.
        anchors = [
            {
                'index': index,
                'x_px': x_px,
                'text': str(value),
                'value': value,
                'confidence': 0.95,
            }
            for index, x_px, value in (
                (11, 155.0, 10),  # clipped: hangs over mark 11, reads the 10
                (10, 260.0, 20),
                (20, 380.0, 30),
                (30, 500.0, 40),
                (40, 620.0, 50),
                (50, 740.0, 60),
                (60, 860.0, 70),
            )
        ]

        kept = tape_digits._trim_outliers(anchors, pitch_px=12.0)
        scale = tape_digits._calibrate(kept, 12.0, (0, 40, True), 1200)

        self.assertNotIn(155.0, [anchor['x_px'] for anchor in kept])
        self.assertTrue(scale['usable'], scale)
        self.assertAlmostEqual(scale['mm_per_px'], 10.0 / 12.0, delta=0.01)

    def test_a_shaded_blade_still_has_readable_labels(self):
        # The trench darkens whole columns of the blade. A threshold taken from
        # the strip-wide median calls that shade ink, the numbers end up inside
        # one huge ink band, and a shaded upload finds its edges but no scale.
        shaded = _trench_strip(300, 900, shade=70)
        lattice = _lattice(_strip(pitch=12.0, every=10))

        scale = tape_digits.read_labels(
            shaded,
            lattice,
            ocr=FakeOCR({index: str(10 + index) for index in range(0, 120, 10)}),
        )

        self.assertTrue(scale['usable'], scale)
        self.assertAlmostEqual(scale['mm_per_px'], 10.0 / 12.0, delta=0.01)
        # Every label well inside the shade is still one anchor; the ones that
        # straddle the change of level are the only ones that may be lost.
        shaded_labels = {
            index
            for index, position in enumerate(lattice['positions'])
            if index % 10 == 0 and 360 <= position <= 840
        }
        self.assertTrue(shaded_labels, lattice['positions'])
        self.assertLessEqual(
            shaded_labels, {anchor['index'] for anchor in scale['anchors']}
        )

    def test_two_printed_rows_are_read_one_row_at_a_time(self):
        # A survey tape prints its divisions and a units row, and the hatching
        # between them keeps a speck of ink in every row. The band used to be
        # read whole, which stacked both lines into one cell: tesseract answered
        # a stray single digit and the anchors disagreed (`20261124_141314`).
        strip = _two_row_strip()
        lattice = _lattice(strip)

        scale = tape_digits.read_labels(
            strip,
            lattice,
            ocr=FakeOCR({index: str(10 + index) for index in range(0, 120, 10)}),
        )

        self.assertTrue(scale['usable'], scale)
        # One printed line, not the two stacked: the divisions are 16 rows tall.
        rows = scale['band']['rows']
        self.assertLessEqual(rows[1] - rows[0], 18, scale['band'])
        self.assertAlmostEqual(scale['mm_per_px'], 10.0 / 12.0, delta=0.01)

    def test_the_hatch_between_two_rows_does_not_join_them(self):
        shares = np.array(
            [0.0, 0.0, 0.5, 0.5, 0.5, 0.0, 0.0, 0.02, 0.4, 0.4, 0.4, 0.0]
        )

        runs = tape_digits._printed_row_runs(shares)

        self.assertEqual(runs[0], (2, 5))
        self.assertEqual(runs[1], (8, 11))

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

    def test_an_uneven_ladder_is_read_from_its_numbers(self):
        # This test used to pin the opposite: an `uneven` ladder refused before
        # any OCR ran. That policy is reversed deliberately, on measurement.
        # `20261124_141314` is a genuine tape that scatters 2.3 px on an 8.5 px
        # pitch because it sags over the trench, and the millimetres come from
        # the printed numbers, not the pitch. Refusing here is what kept the
        # crew's own tape unreadable. The pitch still has to agree with the
        # numbers before a scale exists (`_calibrate`), which is where a false
        # ladder is actually caught.
        strip = _strip(pitch=12.0, every=10)
        lattice = dict(_lattice(strip), usable=False, residual=0.30)

        scale = tape_digits.read_labels(
            strip,
            lattice,
            ocr=FakeOCR({index: str(10 + index) for index in range(0, 120, 10)}),
        )

        self.assertTrue(scale['usable'], scale)
        self.assertEqual(scale['unit'], 'cm')
        self.assertAlmostEqual(scale['mm_per_tick'], 10.0, delta=0.2)
        self.assertAlmostEqual(scale['mm_per_px'], 10.0 / 12.0, delta=0.01)


class EdgeSpanTests(SimpleTestCase):
    """Two edges on the calibrated blade, turned into the millimetres between them.

    A trench wall or a chamber rim is a pixel position along the rectified strip;
    what makes it a measurement is the scale the printed numbers set. No more OCR
    runs here: the calibration is fixed and the arithmetic is what is checked.
    """

    def _scale(self):
        strip = _strip(pitch=12.0, every=10)
        lattice = _lattice(strip)
        scale = tape_digits.read_labels(
            strip,
            lattice,
            ocr=FakeOCR({index: str(10 + index) for index in range(0, 120, 10)}),
        )
        assert scale['usable'], scale
        return scale

    def test_two_edges_become_the_millimetres_between_them(self):
        scale = self._scale()

        span = tape_digits.span_mm(scale, 200.0, 840.0)

        self.assertTrue(span['usable'], span)
        # 12 px per printed centimetre, so 640 px is 10 mm * 640 / 12.
        self.assertAlmostEqual(span['span_mm'], 640.0 * 10.0 / 12.0, delta=0.5)
        self.assertAlmostEqual(span['span_m'], span['span_mm'] / 1000.0, delta=0.002)
        self.assertEqual(span['unit'], 'cm')
        self.assertEqual(span['edges_px'], [200.0, 840.0])

    def test_the_edges_may_be_given_in_either_order(self):
        scale = self._scale()

        forward = tape_digits.span_mm(scale, 200.0, 840.0)
        backward = tape_digits.span_mm(scale, 840.0, 200.0)

        self.assertEqual(forward['span_mm'], backward['span_mm'])
        self.assertEqual(backward['edges_px'], [840.0, 200.0])

    def test_a_refused_scale_measures_nothing(self):
        strip = _strip(pitch=12.0, every=10)
        refused = tape_digits.read_labels(strip, _lattice(strip), ocr=FakeOCR({}))

        span = tape_digits.span_mm(refused, 100.0, 400.0)

        self.assertFalse(span['usable'], span)
        self.assertIn('too few', span['reason'])
        self.assertIsNone(span['span_mm'])

    def test_edges_that_sit_together_are_refused(self):
        span = tape_digits.span_mm(self._scale(), 400.0, 400.0)

        self.assertFalse(span['usable'], span)
        self.assertIn('same place', span['reason'])
        self.assertIsNone(span['span_m'])

    def test_the_edges_found_on_the_strip_become_that_span(self):
        scale = self._scale()

        span = tape_digits.read_span(_trench_strip(300, 900), scale)

        self.assertTrue(span['usable'], span)
        self.assertEqual(len(span['edges_px']), 2)
        self.assertAlmostEqual(span['span_mm'], 500.0, delta=4.0)
        self.assertGreater(span['contrast'], tape_digits.EDGE_MIN_STEP)

    def test_a_shadow_on_one_side_of_the_blade_is_not_an_edge(self):
        # The trench darkens only the lower rows of the blade: a hand shadow or
        # a mark, which must not become a measurement.
        shadow = _trench_strip(300, 900, rows=(0, 55))

        span = tape_digits.read_span(shadow, self._scale())

        self.assertFalse(span['usable'], span)
        self.assertIn('one side', span['reason'])

    def test_printing_that_covers_part_of_the_blade_is_not_an_edge(self):
        # A printed number darkens the rows it covers and leaves the rest of the
        # blade alone; the median column level is what tells that from an edge.
        span = tape_digits.read_span(
            _trench_strip(300, 900, rows=(0, 36)), self._scale()
        )

        self.assertFalse(span['usable'], span)
        self.assertIsNone(span['edges_px'])

    def test_a_blade_lying_along_the_object_has_no_edges(self):
        span = tape_digits.read_span(_strip(), self._scale())

        self.assertFalse(span['usable'], span)
        self.assertIn('crosses the blade', span['reason'])

    def test_edges_are_still_reported_when_the_scale_was_refused(self):
        strip = _strip(pitch=12.0, every=10)
        refused = tape_digits.read_labels(strip, _lattice(strip), ocr=FakeOCR({}))

        span = tape_digits.read_span(_trench_strip(300, 900), refused)

        self.assertFalse(span['usable'], span)
        self.assertIsNone(span['span_mm'])
        # The step is measured over a window, so an edge lands within a few
        # pixels of the boundary itself.
        self.assertAlmostEqual(span['edges_px'][0], 300.0, delta=5.0)
        self.assertAlmostEqual(span['edges_px'][1], 900.0, delta=5.0)
        self.assertGreater(span['edges_px'][1] - span['edges_px'][0], 590.0)
