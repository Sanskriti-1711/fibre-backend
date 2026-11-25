"""Tests for the phase-3 reader: the ladder, the reference face, and the refusals.

The field photos are not in the repository, so the ladder is rendered with a
known truth: one tick per millimetre at 6 px, one printed number per centimetre
at 60 px, and a clip drawn over a known span. The OCR is injected -- the same
hook ``tape_digits.read_labels`` takes -- so what is checked here is the
geometry and the counting, not tesseract's eyes.

The convention under test is the one confirmed on `20261124_140332`: the ladder
runs 42, 41, 40, 39 and the reading is 38, i.e. the face *past* the clip in the
direction the numbers fall.
"""

from unittest.mock import patch

from django.test import SimpleTestCase
import numpy as np

from supervision import tape_reader
from supervision.tests_tape_clip import photo as clip_photo

BLADE = (232, 226, 205)
GROUND = (70, 66, 60)
INK = (40, 40, 40)
PALE = (252, 250, 246)

BLADE_TOP, BLADE_BOTTOM = 240, 304      # a 64 px blade
STRIP_LEFT = 40                          # original x of strip x = 0
LABEL_FIRST = 60                         # strip x of the first printed number
LABEL_STEP = 60                          # strip px per printed division (1 cm)
TICK_STEP = 6                            # strip px per millimetre
LABELS = 13

BAND = {
    'centre': [470.0, 272.0],
    'axis': [1.0, 0.0],
    'across': [0.0, 1.0],
    'length_px': 860.0,
    'width_px': 64.0,
}


def ladder_photo(ascending: bool = False, clip: tuple[int, int] = (720, 780)) -> np.ndarray:
    """A blade whose marks are 1 mm apart and whose numbers are 1 cm apart."""
    rgb = np.zeros((560, 940, 3), dtype=np.uint8)
    rgb[:, :] = GROUND
    rgb[BLADE_TOP:BLADE_BOTTOM, STRIP_LEFT:900] = BLADE
    for strip_x in range(TICK_STEP, 860, TICK_STEP):
        if clip[0] <= strip_x < clip[1]:
            continue
        rgb[BLADE_BOTTOM - 12 : BLADE_BOTTOM, STRIP_LEFT + strip_x : STRIP_LEFT + strip_x + 1] = INK
    for index in range(LABELS):
        strip_x = LABEL_FIRST + index * LABEL_STEP
        if clip[0] <= strip_x < clip[1]:
            continue
        left = STRIP_LEFT + strip_x - 6
        rgb[BLADE_TOP + 8 : BLADE_TOP + 30, left : left + 12] = INK
    # Pale, so it displaces the blade's own tone instead of printing ink on it:
    # this is the clip the depth signal cannot see and the breadth signal can.
    rgb[232:330, STRIP_LEFT + clip[0] : STRIP_LEFT + clip[1]] = PALE
    return rgb


def clip_result(clip: tuple[int, int] = (720, 780)) -> dict:
    """The clip the reader is handed: confirmed, with its two faces."""
    left = float(STRIP_LEFT + clip[0])
    right = float(STRIP_LEFT + clip[1])
    return {
        'found': True,
        'reason': None,
        'blade_confirmed': True,
        'faces': [[left, 272.0], [right, 272.0]],
        'tape_angle_deg': 0.0,
        'band': dict(BAND),
    }


def ladder_ocr(ascending: bool = False, readable_to: float | None = None):
    """A perfect OCR of the rendered ladder, keyed on the cell's position."""

    def read_cell(cell, mark):
        x = float(mark['x_px'])
        if readable_to is not None and x > readable_to:
            return '', 0.0
        offset = (x - LABEL_FIRST) / LABEL_STEP
        value = (20.0 + offset) if ascending else (50.0 - offset)
        return str(int(round(value))), 0.95

    return read_cell


def caption_photo() -> np.ndarray:
    """A bright band carrying a line of caption text, not a tape.

    `WhatsApp ... 08.38.21` is this: the NoteCam caption burned into the frame,
    long and thin and the brightest thing in it, so the locator locks onto it.
    """
    rgb = np.zeros((700, 900, 3), dtype=np.uint8)
    rgb[:, :] = GROUND
    rgb[300:360, :] = (238, 236, 232)
    rng = np.random.default_rng(5)
    x = 20
    while x < 860:
        width = int(rng.integers(6, 16))
        height = int(rng.integers(12, 26))
        top = int(rng.integers(310, 336))
        rgb[top : top + height, x : x + width] = (46, 44, 42)
        x += width + int(rng.integers(4, 18))
    return rgb


def metal_bar_photo() -> np.ndarray:
    """A smooth bright bar with nothing printed on it.

    `WhatsApp ... 08.38.28 (1)` is this: a length of pipe, one blade wide and
    bright, which the locator accepted as a blade for a whole session.
    """
    rgb = np.zeros((700, 900, 3), dtype=np.uint8)
    rgb[:, :] = GROUND
    ramp = np.linspace(244.0, 206.0, 900)
    rgb[300:366, :, 0] = ramp
    rgb[300:366, :, 1] = ramp * 0.99
    rgb[300:366, :, 2] = ramp * 0.97
    return rgb


class ReaderTests(SimpleTestCase):
    def test_the_reading_is_the_face_on_the_lower_number_side(self):
        # 42, 41, 40, 39, clip -- the far face is the next division down.
        result = tape_reader.read(
            ladder_photo(), clip=clip_result(), ocr=ladder_ocr()
        )
        self.assertTrue(result['usable'], result.get('reason'))
        self.assertAlmostEqual(result['reading_mm'], 380.0, delta=1.0)
        self.assertEqual(result['unit'], 'cm')
        # The far face of the clip, in photo pixels.
        self.assertAlmostEqual(result['reference_face'][0], 820.0, delta=6.0)

    def test_an_ascending_ladder_takes_the_other_face(self):
        # Numbers climb along the blade, so the lower ones are behind the clip
        # and the near face carries the reading.
        result = tape_reader.read(
            ladder_photo(ascending=True), clip=clip_result(), ocr=ladder_ocr(ascending=True)
        )
        self.assertTrue(result['usable'], result.get('reason'))
        self.assertAlmostEqual(result['reading_mm'], 310.0, delta=1.0)
        self.assertAlmostEqual(result['reference_face'][0], 760.0, delta=6.0)

    def test_the_marks_and_the_numbers_set_the_scale(self):
        result = tape_reader.read(
            ladder_photo(), clip=clip_result(), ocr=ladder_ocr()
        )
        self.assertTrue(result['usable'], result.get('reason'))
        # 1 cm over 60 px, whichever stripe the ladder was fitted on.
        self.assertAlmostEqual(result['mm_per_px'], 10.0 / LABEL_STEP, delta=0.002)
        self.assertEqual(result['blade_confirmed'], True)
        # The number the count starts from is the one nearest the clip; on this
        # fixture that is the label under the clip's edge, and the face is one
        # division from it either way.
        self.assertLessEqual(result['divisions_to_face'], 1.0)

    def test_a_rendered_blade_is_confirmed_by_its_own_ladder(self):
        # The real detector on a rendered blade: it finds the clip but cannot
        # confirm the blade at two working resolutions. This test used to pin
        # the refusal that followed. It is reversed deliberately: a strip that
        # prints a ladder is a printed blade whatever the locator's agreement,
        # and `20261124_141314` is a genuine tape the locator never confirms.
        # The run therefore goes on, and stops where there is genuinely nothing
        # to read -- the fixture draws no printed numbers.
        result = tape_reader.read(clip_photo((420, 452, (20, 20, 20))))
        self.assertFalse(result['usable'])
        self.assertEqual(result['stage'], 'numbers')
        self.assertTrue(result['clip']['blade_confirmed'])
        self.assertNotIn('second working resolution', result['reason'])

    def test_a_blade_that_prints_a_ladder_confirms_itself(self):
        # `20261124_141314` is a genuine tape whose locator only ever reports
        # `confirmed=None`. A strip printing this many marks at this periodicity
        # is not a trench edge, so the ladder is the second opinion and the
        # reading goes on to look for the clip.
        blade = {
            'found': True,
            'confirmed': None,
            'reason': None,
            'centre': BAND['centre'],
            'axis': BAND['axis'],
            'across': BAND['across'],
            'length_px': BAND['length_px'],
            'width_px': BAND['width_px'],
            'band': dict(BAND),
        }
        with patch.object(tape_reader.tape_band, 'find_band', return_value=blade):
            result = tape_reader.read(ladder_photo(), ocr=ladder_ocr())

        self.assertNotEqual(result['stage'], 'blade', result.get('reason'))
        self.assertNotIn('second working resolution', result.get('reason') or '')

    def test_a_blade_without_a_ladder_still_needs_confirmation(self):
        # A blank frame behind a band: nothing printed, so the ladder cannot be
        # the second opinion and the two-resolution policy stands.
        blade = {
            'found': True,
            'confirmed': None,
            'reason': None,
            'centre': BAND['centre'],
            'axis': BAND['axis'],
            'across': BAND['across'],
            'length_px': BAND['length_px'],
            'width_px': BAND['width_px'],
            'band': dict(BAND),
        }
        blank = np.zeros((560, 940, 3), dtype=np.uint8)
        with patch.object(tape_reader.tape_band, 'find_band', return_value=blade):
            result = tape_reader.read(blank)

        self.assertFalse(result['usable'])
        self.assertEqual(result['stage'], 'blade')
        # Reversed deliberately 2026-11-25: with no ladder the reader no longer
        # talks about resolution agreement, it says the thing the engineer can
        # act on -- there is nothing printed here to measure from.
        self.assertIn('no tape found', result['reason'])
        self.assertIn('no tick ladder', result['reason'])

    def test_a_photo_without_a_clip_is_refused(self):
        # The detector's own answer for a blade with nothing interrupting it,
        # injected: on the rendered tape the locator does not *confirm* the
        # blade at any resolution it tries, so an end-to-end run stops one gate
        # earlier. The caption and metal-bar cases pin that earlier gate.
        result = tape_reader.read(
            clip_photo(),
            clip={
                'found': False,
                'reason': 'the blade was found but nothing interrupts it',
                'stage': 'clip',
            },
        )
        self.assertFalse(result['usable'])
        self.assertEqual(result['stage'], 'clip')

    def test_a_face_more_than_one_division_away_reads_at_low_confidence(self):
        # Reversed deliberately 2026-11-25. This used to be a refusal on the
        # `reading` stage: counting past a division was treated as a guess about
        # which division the clip covers. The crew asked for the reading even
        # when the span runs past what the blade can account for, so the number
        # is returned and the caveat says the count is a best effort.
        result = tape_reader.read(
            ladder_photo(), clip=clip_result(), ocr=ladder_ocr(readable_to=500.0)
        )
        self.assertTrue(result['usable'], result.get('reason'))
        self.assertIsNotNone(result['reading_mm'])
        self.assertEqual(result['confidence'], 'low')
        self.assertTrue(result['low_confidence'])
        self.assertIn('division', result['confidence_note'])
        self.assertGreater(result['divisions_to_face'], 1.0)

    def test_a_reading_that_passes_every_check_is_not_flagged(self):
        # The caveat has to mean something: a reading on a confirmed blade with
        # the clip inside one division carries no note at all.
        result = tape_reader.read(
            ladder_photo(), clip=clip_result(), ocr=ladder_ocr()
        )
        self.assertEqual(result['confidence'], 'high')
        self.assertFalse(result['low_confidence'])
        self.assertIsNone(result['confidence_note'])

    def test_a_blank_frame_is_refused(self):
        blank = np.zeros((400, 400, 3), dtype=np.uint8)
        result = tape_reader.read(blank)
        self.assertFalse(result['usable'])
        self.assertIn(result['stage'], ('clip', 'blade'))

    def test_a_caption_on_a_bright_band_is_reported_as_no_tape(self):
        # Three of the five "confirmed" field blades are not tape. The refusal
        # has to say so on the blade stage, before a clip is looked for: there
        # is no clip on a caption, and "no clip found" sends the engineer
        # looking for the wrong thing.
        result = tape_reader.read(caption_photo())
        self.assertFalse(result['usable'])
        self.assertEqual(result['stage'], 'blade')
        self.assertIn('no tape found', result['reason'])

    def test_a_smooth_metal_bar_is_reported_as_no_tape(self):
        result = tape_reader.read(metal_bar_photo())
        self.assertFalse(result['usable'])
        self.assertEqual(result['stage'], 'blade')
        self.assertIn('no tape found', result['reason'])
