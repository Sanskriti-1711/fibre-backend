"""Tests for the clip detector: a synthetic blade, with and without a clip.

The field photos are not in the repository, so the geometry is checked against
a rendered blade whose clip position is known to the pixel. The cases are the
three that broke the colour-only prototype: a dark clip, a pale clip whose body
is brighter than the blade, and no clip at all (which must refuse rather than
report the printed numbers as a clip).
"""

from django.test import SimpleTestCase
import numpy as np

from supervision import tape_clip

BLADE = (232, 226, 205)
GROUND = (70, 66, 60)


def photo(clip: tuple[int, int, tuple[int, int, int]] | None = None,
          clip_rows: tuple[int, int] = (232, 330)) -> np.ndarray:
    """A bright blade with millimetre ticks, and optionally a clip across it."""
    rgb = np.zeros((560, 900, 3), dtype=np.uint8)
    rgb[:, :] = GROUND
    rgb[248:312, 40:860] = BLADE
    for x in range(46, 858, 6):
        rgb[300:312, x : x + 1] = (40, 40, 40)
    if clip is not None:
        left, right, colour = clip
        rgb[clip_rows[0] : clip_rows[1], left:right] = colour
    return rgb


class ClipDetectionTests(SimpleTestCase):
    def test_dark_clip_is_found_where_it_is(self):
        result = tape_clip.find_clip(photo((420, 452, (20, 20, 20))), work_edge=700)
        self.assertTrue(result['found'], result.get('reason'))
        left, right = sorted(face[0] for face in result['faces'])
        self.assertAlmostEqual(left, 420, delta=8)
        self.assertAlmostEqual(right, 452, delta=8)
        self.assertAlmostEqual(result['along_tape_px'], 32, delta=12)
        self.assertEqual(result['method'], 'dark')

    def test_pale_clip_is_found_by_the_blade_it_displaces(self):
        # Brighter than the blade itself: no hue, no darkening, only the fact
        # that the blade's own tone is not there for as long as the clip is.
        result = tape_clip.find_clip(photo((420, 452, (252, 250, 246))), work_edge=700)
        self.assertTrue(result['found'], result.get('reason'))
        left, right = sorted(face[0] for face in result['faces'])
        self.assertAlmostEqual(left, 420, delta=8)
        self.assertAlmostEqual(right, 452, delta=8)

    def test_coloured_clip_is_found(self):
        result = tape_clip.find_clip(photo((420, 452, (60, 170, 190))), work_edge=700)
        self.assertTrue(result['found'], result.get('reason'))
        self.assertIn('colour', [result['method'], *result['agreement']])

    def test_clip_faces_come_back_in_a_stable_order(self):
        result = tape_clip.find_clip(photo((300, 340, (20, 20, 20))), work_edge=700)
        self.assertTrue(result['found'], result.get('reason'))
        self.assertLess(result['faces'][0][0], result['faces'][1][0])

    def test_blade_without_a_clip_is_refused(self):
        result = tape_clip.find_clip(photo(), work_edge=700)
        self.assertFalse(result['found'])
        self.assertIn('nothing interrupts it', result['reason'])

    def test_a_dark_block_off_the_blade_is_not_a_clip(self):
        # Something dark on the ground beside the blade must not be taken for
        # the crew's clip: only a departure *in* the band is a candidate.
        rgb = photo()
        rgb[420:520, 300:340] = (20, 20, 20)
        result = tape_clip.find_clip(rgb, work_edge=700)
        self.assertFalse(result['found'])

    def test_a_clip_at_the_end_of_the_blade_is_found(self):
        # `20261124_140444`: the clamp holds the tape's start, so only its rim
        # falls inside the tracked blade. A search that stopped at the blade's
        # ends could never see it, and answered "nothing interrupts it" on a
        # photo whose clip is plainly there.
        result = tape_clip.find_clip(photo((44, 76, (60, 170, 190))), work_edge=700)
        self.assertTrue(result['found'], result.get('reason'))
        left, right = sorted(face[0] for face in result['faces'])
        self.assertAlmostEqual(left, 44, delta=10)
        self.assertAlmostEqual(right, 76, delta=10)

    def test_a_printed_mark_does_not_win_over_the_clip(self):
        # The field failure on `20261124_140332`: a shaded mark scored higher on
        # the single `dark` signal than the cyan clip did, and the reading was
        # taken at the mark. A lone signal is not evidence of a clip.
        rgb = photo((300, 340, (60, 170, 190)))
        rgb[300:312, 600:616] = (8, 8, 8)   # a deeper, narrower printed mark
        result = tape_clip.find_clip(rgb, work_edge=700)
        self.assertTrue(result['found'], result.get('reason'))
        left, right = sorted(face[0] for face in result['faces'])
        self.assertAlmostEqual(left, 300, delta=12)
        self.assertAlmostEqual(right, 340, delta=12)

    def test_a_lone_printed_mark_is_not_a_clip(self):
        # A number darkens the blade as deeply as a clip does, but it sits
        # inside the blade and leaves the ground beyond the edges alone. Without
        # a second signal there is no clip to report.
        rgb = photo()
        rgb[250:268, 600:614] = (18, 18, 18)
        result = tape_clip.find_clip(rgb, work_edge=700)
        self.assertFalse(result['found'], result.get('method'))

    def test_a_blank_frame_is_refused(self):
        blank = np.zeros((400, 400, 3), dtype=np.uint8)
        result = tape_clip.find_clip(blank)
        self.assertFalse(result['found'])

    def test_the_band_runs_along_the_blade(self):
        result = tape_clip.find_clip(photo((420, 452, (20, 20, 20))), work_edge=700)
        self.assertTrue(result['found'], result.get('reason'))
        self.assertAlmostEqual(result['tape_angle_deg'], 0.0, delta=4.0)
        self.assertAlmostEqual(result['tape_width_px'], 64, delta=18)
