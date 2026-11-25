"""Tape OCR: the arithmetic, the unreadable cases, and the endpoint.

Three things the field crew depends on are pinned here:

* the measured span is the difference between the outermost tape numbers, in
  millimetres — the tape's own scale, so no camera calibration is involved;
* a reading outside what the dimension can physically be is refused instead of
  filling the form with a misread digit;
* the endpoint refuses an unknown dimension, another span's photo, an
  unassigned engineer and a submitted inspection, writes nothing, and says so
  plainly on a host without tesseract.

Tesseract is stubbed in every test, so the suite never needs the binary.
"""

import io
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image, ImageDraw
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Feature
from supervision import tape_cloud, tape_measure
from supervision.models import FeaturePhoto
from supervision.storage import LocalEvidenceStorage, reset_evidence_storage
from supervision.tests_ai_measure import LocalEvidenceMixin, png_bytes
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


def cm_readings(*values):
    return [{'value': value, 'unit': 'cm', 'x': index / 10, 'confidence': 0.9} for index, value in enumerate(values)]


class MeasureArithmeticTests(SimpleTestCase):
    def test_the_span_is_the_difference_between_the_outer_readings(self):
        row, note = tape_measure.measure('trench', 'depth', cm_readings(120, 130, 140, 150, 160, 170))

        self.assertEqual(row['key'], 'actual_depth_mm')
        self.assertEqual(row['value'], 500)
        self.assertEqual(row['unit'], 'mm')
        self.assertEqual(row['confidence'], 0.9)
        self.assertIn('120 to 170 cm', note)

    def test_a_tape_that_started_past_zero_still_measures_the_span(self):
        # 30 cm to 130 cm is one metre wherever the zero end sits.
        row, _ = tape_measure.measure('trench', 'depth', cm_readings(30, 130))

        self.assertEqual(row['value'], 1000)

    def test_a_millimetre_tape_is_read_as_millimetres(self):
        readings = [{'value': 1200, 'unit': 'mm'}, {'value': 1700, 'unit': 'mm'}]

        row, _ = tape_measure.measure('trench', 'depth', readings, unit='mm')

        self.assertEqual(row['value'], 500)

    def test_length_is_written_in_metres(self):
        row, _ = tape_measure.measure('trench', 'length', cm_readings(0, 250, 500, 750, 1250))

        self.assertEqual(row['key'], 'measured_length_m')
        self.assertEqual(row['value'], 12.5)
        self.assertEqual(row['unit'], 'm')

    def test_two_readings_are_less_trusted_than_a_full_run(self):
        two, _ = tape_measure.measure('trench', 'depth', cm_readings(100, 180))
        four, _ = tape_measure.measure('trench', 'depth', cm_readings(100, 110, 120, 180))

        self.assertLess(two['confidence'], four['confidence'])

    def test_a_single_readable_number_is_not_a_measurement(self):
        row, note = tape_measure.measure('trench', 'depth', cm_readings(170))

        self.assertIsNone(row)
        self.assertIn('only one number', note.lower())

    def test_a_depth_below_what_a_trench_can_be_is_still_given(self):
        # The crew has the tape in hand; a span outside the spec's range is a
        # caveat on the reading, not a reason to withhold it.
        row, note = tape_measure.measure('trench', 'depth', cm_readings(100, 105))

        self.assertEqual(row['value'], 50)
        self.assertIn('outside', note)
        self.assertIn('150', note)

    def test_a_length_beyond_the_plausible_range_is_still_given(self):
        row, note = tape_measure.measure('trench', 'length', cm_readings(0, 99999))

        self.assertEqual(row['unit'], 'm')
        self.assertEqual(row['value'], 999.99)
        self.assertIn('outside', note)

    def test_a_dimension_the_element_does_not_have_is_refused(self):
        row, _ = tape_measure.measure('cable', 'depth', cm_readings(0, 100))

        self.assertIsNone(row)

    def test_values_are_not_assumed_to_be_ordered(self):
        row, _ = tape_measure.measure('trench', 'depth', cm_readings(170, 120, 150))

        self.assertEqual(row['value'], 500)


class ReadTapeTests(SimpleTestCase):
    """The OCR pass keeps a run and rejects everything else."""

    def read(self, tokens_by_rotation):
        def fake(image, rotation, config=tape_measure.DENSE_OCR_CONFIG):
            return tokens_by_rotation.get(rotation, [])

        with mock.patch('supervision.tape_measure._ocr_readings', side_effect=fake):
            return tape_measure.read_tape_bytes(png_bytes(600, 300))

    def test_a_tape_run_becomes_readings_in_centimetres(self):
        tokens = [
            {'value': 120, 'x': 0.1, 'confidence': 0.92},
            {'value': 130, 'x': 0.3, 'confidence': 0.90},
            {'value': 140, 'x': 0.5, 'confidence': 0.88},
            {'value': 150, 'x': 0.7, 'confidence': 0.91},
        ]

        result = self.read({0: tokens})

        self.assertEqual(result['unit'], 'cm')
        self.assertEqual([row['value'] for row in result['readings']], [120, 130, 140, 150])
        self.assertEqual(result['engine'], 'tesseract')

    def test_numbers_that_do_not_run_in_order_are_not_a_tape(self):
        tokens = [
            {'value': 900, 'x': 0.1, 'confidence': 0.9},
            {'value': 120, 'x': 0.4, 'confidence': 0.9},
            {'value': 45, 'x': 0.8, 'confidence': 0.9},
        ]

        result = self.read({0: tokens})

        self.assertIsNone(result)

    def test_a_span_bound_by_a_weak_read_is_refused(self):
        # A date stamp read at 0.96 beside a smudge at 0.41 is what turned a
        # field photo into a 2 m trench depth.
        run = [
            {'value': 8, 'x': 0.1, 'confidence': 0.41},
            {'value': 2026, 'x': 0.8, 'confidence': 0.96},
        ]

        self.assertFalse(tape_measure._run_is_usable(run))

    def test_a_number_off_the_blades_pitch_is_refused(self):
        run = [
            {'value': 0, 'x': 0.1, 'confidence': 0.8},
            {'value': 5, 'x': 0.3, 'confidence': 0.8},
            {'value': 9, 'x': 0.5, 'confidence': 0.8},
            {'value': 55, 'x': 0.8, 'confidence': 0.8},
        ]

        self.assertFalse(tape_measure._run_is_usable(run))

    def test_no_digits_anywhere_is_none(self):
        self.assertIsNone(self.read({}))

    def test_a_rotated_blade_is_read_from_the_quarter_turn_that_works(self):
        tokens = [
            {'value': 100, 'x': 0.2, 'confidence': 0.9},
            {'value': 200, 'x': 0.8, 'confidence': 0.9},
        ]

        result = self.read({90: tokens})

        self.assertEqual([row['value'] for row in result['readings']], [100, 200])


@override_settings(EVIDENCE_S3_BUCKET='', EVIDENCE_PUBLIC_BASE_URL='')
class FeatureTapeMeasurementEndpointTests(LocalEvidenceMixin, TestCase):
    """One photo in, one tape reading out, no writes."""

    def setUp(self):
        self.pin_local_evidence()
        self.engineer = make_user('tape-measure@example.com', role=User.Role.ENGINEER)
        self.project = make_survey_copy(make_ftth_project())
        self.feature = make_feature(
            self.project,
            layer_id='final_trenches',
            layer_name='final_trenches',
            properties={'DEPTH_MM': 1200, 'WIDTH_MM': 450},
        )
        AssignmentJob.objects.create(
            project=self.project,
            scope=AssignmentJob.SCOPE_PROJECT,
            assignee=self.engineer,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.engineer)
        self.url = f'/api/features/{self.feature.id}/tape-measurements/'
        self.photo = self.store_photo(self.feature)

    def store_photo(self, feature):
        photo = FeaturePhoto.objects.create(
            feature=feature,
            project=self.project,
            image=SimpleUploadedFile('tape.png', png_bytes(), content_type='image/png'),
            uploaded_by=self.engineer,
        )
        self.addCleanup(FeaturePhoto.objects.get(pk=photo.pk).image.delete, save=False)
        return photo

    def post(self, **body):
        return self.client.post(self.url, body, format='json')

    def read(self):
        return {
            'engine': 'tesseract',
            'unit': 'cm',
            'readings': cm_readings(120, 130, 140, 150, 160, 170),
        }

    def clip(self, millimetres=380.0, low=False, stage=None, reason=None):
        """What ``tape_reader`` answers for this photo, as the endpoint sees it.

        The capture path takes its number from this reader since 2026-11-25, so
        the endpoint tests stand in for it here instead of for the old span
        reader.
        """
        usable = stage is None
        return {
            'usable': usable,
            'reason': reason,
            'stage': stage,
            'reading_mm': millimetres if usable else None,
            'unit': 'cm',
            'reference_face': [820.0, 272.0] if usable else None,
            'label': {'value': 39, 'text': '39', 'x_px': 719.5} if usable else None,
            'divisions_to_face': 1.0 if usable else None,
            'confidence': ('low' if low else 'high') if usable else None,
            'low_confidence': bool(low),
            'confidence_note': 'the marks on this blade are too faint' if low else None,
            'clip': {'blade_confirmed': True, 'tape_angle_deg': 12.0},
        }

    def test_a_tape_photo_fills_the_depth_field_as_an_editable_draft(self):
        # The app fills the field from the response, but the reading is also
        # kept in field_measurements so reopening the span still shows it --
        # as a draft the engineer can overwrite, never a locked value.
        self.feature.field_schema = [
            {'key': 'DEPTH_MM', 'label': 'Depth Mm', 'editable': True, 'role': 'measurement'}
        ]
        self.feature.field_measurements = {'surface_type': 'Asphalt'}
        self.feature.save(update_fields=['field_schema', 'field_measurements'])

        with mock.patch('supervision.tape_measure.ocr_available', return_value=True), mock.patch(
            'supervision.views.tape_reader.read_bytes', return_value=self.clip(380.0)
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body['measurement']['key'], 'actual_depth_mm')
        self.assertEqual(body['measurement']['value'], 380)
        self.assertEqual(body['photo_id'], str(self.photo.id))
        self.assertIn('tape reads', body['notes'].lower())

        self.feature.refresh_from_db()
        self.assertEqual(
            self.feature.field_measurements,
            {
                'surface_type': 'Asphalt',
                'DEPTH_MM': {'value': 380, 'unit': 'mm'},
            },
        )

    def test_a_read_puts_the_reading_on_the_photo(self):
        # The panel on the evidence is what the app and the review page show, so
        # a read has to refresh it -- the upload-time copy predates the reading.
        with mock.patch('supervision.tape_measure.ocr_available', return_value=True), mock.patch(
            'supervision.views.tape_reader.read_bytes', return_value=self.clip(380.0)
        ), mock.patch('supervision.tape_framing.check_photo', return_value=None):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.photo.refresh_from_db()
        self.assertTrue(self.photo.annotated_image)
        storage = self.photo.image.storage
        self.assertTrue(storage.exists(self.photo.annotated_image))
        self.addCleanup(storage.delete, self.photo.annotated_image)

    def test_a_read_that_succeeds_is_not_turned_into_a_reshoot_by_framing(self):
        # The framing pass is a heuristic; a read is evidence. A blade whose
        # printed pitch is wider than the ladder expects reads fine and was yet
        # reported as "No even row of printed numbers" -- the app then showed
        # the reshoot instruction instead of the measurement it already had.
        framing_miss = {
            'ok': False,
            'verdict': 'no_tape',
            'message': 'No even row of printed numbers was found.',
            'detail': {},
        }

        with mock.patch('supervision.tape_measure.ocr_available', return_value=True), mock.patch(
            'supervision.views.tape_reader.read_bytes', return_value=self.clip(380.0)
        ), mock.patch(
            'supervision.views.tape_framing.check_photo', return_value=framing_miss
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body['measurement']['value'], 380)
        self.assertTrue(body['framing']['ok'])
        self.assertEqual(body['framing']['caveat'], framing_miss['message'])

    def test_a_tape_that_cannot_be_read_returns_no_measurement(self):
        with mock.patch('supervision.tape_measure.ocr_available', return_value=True), mock.patch(
            'supervision.views.tape_reader.read_bytes',
            return_value=self.clip(
                stage='numbers', reason='too few printed numbers were readable to set a scale'
            ),
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['measurement'])
        self.assertEqual(response.json()['readings'], [])
        self.assertTrue(response.json()['notes'])

    def test_the_endpoint_returns_the_clip_reading_at_its_reference_face(self):
        # Reversed deliberately 2026-11-25: the clip reading is now the form
        # value. Before, it travelled beside a span taken between printed
        # labels, and that span is the one that read 22 cm for a 200 mm tape.
        clip = {
            'usable': True,
            'reason': None,
            'stage': None,
            'reading_mm': 380.0,
            'unit': 'cm',
            'reference_face': [820.0, 272.0],
            'label': {'value': 39, 'text': '39', 'x_px': 719.5},
            'divisions_to_face': 1.0,
            'clip': {'blade_confirmed': True, 'tape_angle_deg': 12.0},
        }

        with mock.patch(
            'supervision.tape_measure.ocr_available', return_value=True
        ), mock.patch(
            'supervision.views.tape_reader.read_bytes', return_value=clip
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertTrue(body['clip']['usable'])
        self.assertEqual(body['clip']['reading_mm'], 380.0)
        self.assertEqual(body['clip']['reference_face'], [820.0, 272.0])
        self.assertTrue(body['clip']['blade_confirmed'])
        self.assertIn('380 mm', body['notes'])
        # One reader, one number: the measurement is the clip's own reading.
        self.assertEqual(body['measurement']['value'], 380)
        self.assertEqual(body['readings'][0]['value'], 380.0)
        self.assertEqual(body['readings'][0]['unit'], 'mm')

    def test_an_unconfirmed_blade_comes_back_as_a_refusal_not_a_reading(self):
        refusal = {
            'usable': False,
            'reason': (
                'the blade was not confirmed by a second working resolution, so no '
                'reading is taken from it'
            ),
            'stage': 'blade',
            'reading_mm': None,
            'clip': {'blade_confirmed': False, 'tape_angle_deg': -33.0},
        }

        with mock.patch(
            'supervision.tape_measure.ocr_available', return_value=True
        ), mock.patch(
            'supervision.tape_measure.read_tape', return_value=None
        ), mock.patch(
            'supervision.views.tape_reader.read_bytes', return_value=refusal
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertFalse(body['clip']['usable'])
        self.assertEqual(body['clip']['stage'], 'blade')
        self.assertFalse(body['clip']['blade_confirmed'])
        self.assertEqual(body['clip']['tape_angle_deg'], -33.0)
        self.assertIsNone(body['clip']['reading_mm'])
        self.assertIn('second working resolution', body['clip']['reason'])
        # The refusal is also spelled out where the engineer is looking.
        self.assertIn('The clip could not be read', body['notes'])
        self.assertIsNone(body['measurement'])

    def test_a_host_without_tesseract_says_so(self):
        # No engine at all: no tesseract, no vision key, no Cloud Vision key.
        with mock.patch(
            'supervision.tape_measure.ocr_available', return_value=False
        ), mock.patch(
            'supervision.tape_vision.configured', return_value=False
        ), mock.patch('supervision.tape_cloud.configured', return_value=False):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn('OCR', response.json()['detail'])

    def test_an_unknown_element_type_is_refused(self):
        response = self.post(photo_id=str(self.photo.id), element_type='spaceship', dimension='depth')

        self.assertEqual(response.status_code, 400)

    def test_a_dimension_the_element_does_not_have_is_refused(self):
        response = self.post(photo_id=str(self.photo.id), element_type='cable', dimension='depth')

        self.assertEqual(response.status_code, 400)

    def test_a_photo_belonging_to_another_span_is_refused(self):
        other = make_feature(
            self.project, layer_id='final_trenches', layer_name='final_trenches'
        )
        other_photo = self.store_photo(other)

        response = self.post(
            photo_id=str(other_photo.id), element_type='trench', dimension='depth'
        )

        self.assertEqual(response.status_code, 404)

    def test_an_unassigned_engineer_is_refused(self):
        stranger = make_user('tape-measure-stranger@example.com', role=User.Role.ENGINEER)
        self.client.force_authenticate(user=stranger)

        response = self.post(
            photo_id=str(self.photo.id), element_type='trench', dimension='depth'
        )

        self.assertEqual(response.status_code, 403)

    def test_a_submitted_inspection_is_locked(self):
        Feature.objects.filter(pk=self.feature.pk).update(status=Feature.STATUS_UNDER_REVIEW)

        response = self.post(
            photo_id=str(self.photo.id), element_type='trench', dimension='depth'
        )

        self.assertEqual(response.status_code, 409)

    def test_a_signed_out_caller_is_refused(self):
        self.client.force_authenticate(user=None)

        response = self.post(
            photo_id=str(self.photo.id), element_type='trench', dimension='depth'
        )

        self.assertIn(response.status_code, (401, 403))

    def test_the_paid_engines_do_not_override_the_clip_reader(self):
        # Reversed deliberately 2026-11-25. These two engines used to answer when
        # the span reader came up short, and their numbers went straight into the
        # form with no blade or ladder check: on the field photos that path
        # produced 22/23 cm for a 200 mm tape. The capture value now comes from
        # `tape_reader` only, so neither engine is consulted for it.
        with mock.patch(
            'supervision.tape_measure.ocr_available', return_value=True
        ), mock.patch(
            'supervision.views.tape_reader.read_bytes',
            return_value=self.clip(stage='numbers', reason='no scale'),
        ), mock.patch(
            'supervision.tape_cloud.configured', return_value=True
        ), mock.patch(
            'supervision.tape_cloud.read_tape', side_effect=AssertionError('cloud consulted')
        ), mock.patch(
            'supervision.tape_vision.configured', return_value=True
        ), mock.patch(
            'supervision.tape_vision.read_tape', side_effect=AssertionError('vision consulted')
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertIsNone(response.json()['measurement'])
        self.assertEqual(response.json()['engine'], 'tape_reader')

    def test_a_photo_the_reader_cannot_read_says_where_it_stopped(self):
        # Reversed deliberately 2026-11-25. This used to read the retake reasons
        # out of the span reader's diagnostics ("the photo shows glare"); that
        # reader is off this path, so the refusal names the reader's own stage
        # and reason instead of a quality measure nobody computed.
        with mock.patch(
            'supervision.tape_measure.ocr_available', return_value=True
        ), mock.patch(
            'supervision.views.tape_reader.read_bytes',
            return_value=self.clip(
                stage='numbers', reason='too few printed numbers were readable to set a scale'
            ),
        ), mock.patch('supervision.tape_cloud.configured', return_value=False):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        body = response.json()
        self.assertIsNone(body['measurement'])
        self.assertIn('too few printed numbers', body['notes'])


class GluedNumberSplitTests(SimpleTestCase):
    """Tesseract can return every mark on the blade as one word."""

    def test_six_two_digit_marks_glued_together_come_back_as_six_numbers(self):
        self.assertEqual(
            tape_measure._split_merged('102030405060'), [10, 20, 30, 40, 50, 60]
        )

    def test_three_digit_marks_glued_together_come_back_as_three_numbers(self):
        self.assertEqual(tape_measure._split_merged('100110120'), [100, 110, 120])

    def test_a_blob_that_does_not_step_regularly_is_not_split(self):
        self.assertEqual(tape_measure._split_merged('1000000001'), [])

    def test_a_single_printed_number_is_left_alone(self):
        self.assertEqual(tape_measure._split_merged('1234'), [])


class ObjectEdgeTests(SimpleTestCase):
    """The blade runs past the trench or duct; the reading stops at its edge."""

    def scene(self, middle: int = 210, width: int = 600, height: int = 300):
        image = Image.new('L', (width, height), 40)
        pixels = image.load()
        for y in range(height):
            for x in range(width // 3, (width * 2) // 3):
                pixels[x, y] = middle
        return image

    def test_two_edges_across_the_blade_bound_the_object(self):
        bounds = tape_measure._object_bounds(self.scene(), {'top': 120, 'bottom': 180})

        self.assertIsNotNone(bounds)
        self.assertAlmostEqual(bounds['left'], 1 / 3, delta=0.06)
        self.assertAlmostEqual(bounds['right'], 2 / 3, delta=0.06)

    def test_a_flat_frame_has_no_edges(self):
        image = Image.new('L', (600, 300), 128)

        self.assertIsNone(tape_measure._object_bounds(image, {'top': 120, 'bottom': 180}))

    def test_numbers_printed_past_the_edge_are_dropped(self):
        run = [
            {'value': value, 'x': x, 'confidence': 0.9}
            for value, x in ((10, 0.1), (20, 0.3), (30, 0.5), (40, 0.8), (50, 0.9))
        ]

        clipped = tape_measure._clip_to_bounds(run, {'left': 0.2, 'right': 0.6})

        self.assertEqual([row['value'] for row in clipped], [20, 30])

    def test_a_false_edge_never_empties_a_reading(self):
        run = [
            {'value': value, 'x': x, 'confidence': 0.9}
            for value, x in ((10, 0.1), (20, 0.3))
        ]

        self.assertEqual(tape_measure._clip_to_bounds(run, {'left': 0.5, 'right': 0.9}), run)


class BladeAngleTests(SimpleTestCase):
    """A handheld blade is rarely level; the tilt is measured, not assumed."""

    def scene(self, angle: float):
        blade = Image.new('L', (560, 90), 220)
        canvas = Image.new('L', (700, 360), 45)
        tilted = blade.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=45)
        canvas.paste(
            tilted,
            ((canvas.width - tilted.width) // 2, (canvas.height - tilted.height) // 2),
        )
        return canvas

    def test_a_level_blade_needs_no_turn(self):
        self.assertEqual(tape_measure._blade_angle(self.scene(0)), 0.0)

    def test_a_tilted_blade_is_recognised(self):
        self.assertGreaterEqual(abs(tape_measure._blade_angle(self.scene(12))), 8.0)

    def test_straightening_the_frame_leaves_the_blade_level(self):
        scene = self.scene(12)

        straightened = tape_measure._upright(scene, tape_measure._blade_angle(scene))

        self.assertEqual(tape_measure._blade_angle(straightened), 0.0)

    def test_glare_and_blur_become_reasons_to_retake(self):
        diagnostic = {'glare': 0.4, 'sharpness': 3.0}

        reasons = tape_measure.retake_reasons(diagnostic)

        self.assertEqual(len(reasons), 2)
        self.assertTrue(any('glare' in reason for reason in reasons))

    def test_a_sharp_clean_photo_has_no_excuses(self):
        self.assertEqual(tape_measure.retake_reasons({'glare': 0.0, 'sharpness': 400.0}), [])


class ThresholdingTests(SimpleTestCase):
    """Tiled thresholding and the ink profile that removes dust."""

    def two_lit_halves(self):
        image = Image.new('L', (400, 100), 240)
        draw = ImageDraw.Draw(image)
        draw.rectangle((200, 0, 400, 100), fill=150)
        for x in (30, 90, 230, 290):
            draw.rectangle((x, 30, x + 24, 80), fill=200 if x < 200 else 110)
        return image

    def test_each_tile_finds_ink_at_its_own_brightness(self):
        binary = tape_measure._binarized(self.two_lit_halves())

        self.assertLess(binary.getpixel((40, 50)), 60)
        self.assertLess(binary.getpixel((240, 50)), 60)
        self.assertGreater(binary.getpixel((150, 50)), 200)
        self.assertGreater(binary.getpixel((350, 50)), 200)

    def test_the_output_is_only_black_and_white(self):
        values = set(tape_measure._binarized(self.two_lit_halves()).getdata())

        self.assertTrue(values <= {0, 255})

    def test_dust_is_removed_and_the_marks_are_kept(self):
        image = Image.new('L', (400, 100), 245)
        draw = ImageDraw.Draw(image)
        for x in (40, 100, 160, 220, 280, 340):
            draw.rectangle((x, 30, x + 26, 80), fill=20)
        draw.point((374, 8), fill=15)

        cleaned = tape_measure._digit_strip(image)

        self.assertLess(cleaned.getpixel((50, 50)), 60)
        self.assertGreater(cleaned.getpixel((374, 8)), 200)

    def test_a_frame_with_nothing_digit_like_is_left_alone(self):
        flat = Image.new('L', (300, 120), 200)

        self.assertIs(tape_measure._digit_strip(flat), flat)


class CloudFallbackTests(SimpleTestCase):
    """Cloud Vision is the fallback engine, and only with a key configured."""

    def words(self):
        boxes = ((80, 140), (280, 340), (480, 540))
        return [
            {
                'description': str(value),
                'boundingPoly': {
                    'vertices': [
                        {'x': left, 'y': 40},
                        {'x': right, 'y': 40},
                        {'x': right, 'y': 90},
                        {'x': left, 'y': 90},
                    ]
                },
            }
            for value, (left, right) in zip((10, 20, 30), boxes)
        ]

    def test_without_a_key_the_fallback_is_inert(self):
        with override_settings(GOOGLE_VISION_API_KEY=''):
            self.assertFalse(tape_cloud.configured())
            self.assertIsNone(tape_cloud.read_tape_bytes(png_bytes(200, 100)))

    def test_word_boxes_become_an_increasing_run(self):
        run = tape_measure._reading_run(
            tape_measure._cluster_tokens(tape_cloud._tokens(self.words(), 1000, 200))
        )

        self.assertEqual([token['value'] for token in run], [10, 20, 30])

    @override_settings(GOOGLE_VISION_API_KEY='test-key')
    def test_a_cloud_read_comes_back_in_the_same_shape(self):
        body = {
            'responses': [
                {'textAnnotations': [{'description': '10 20 30'}] + self.words()}
            ]
        }
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = body

        with mock.patch('supervision.tape_cloud.requests.post', return_value=response):
            read = tape_cloud.read_tape_bytes(png_bytes(1000, 200))

        self.assertEqual(read['engine'], 'google-vision')
        self.assertEqual(read['unit'], 'cm')
        self.assertEqual([row['value'] for row in read['readings']], [10, 20, 30])

    @override_settings(GOOGLE_VISION_API_KEY='test-key')
    def test_a_failed_cloud_call_is_not_an_error(self):
        response = mock.Mock()
        response.raise_for_status.side_effect = RuntimeError('403 forbidden')

        with mock.patch('supervision.tape_cloud.requests.post', return_value=response):
            self.assertIsNone(tape_cloud.read_tape_bytes(png_bytes(200, 100)))
