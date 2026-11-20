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
from PIL import Image
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Feature
from supervision import tape_measure
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

    def test_a_depth_below_what_a_trench_can_be_is_refused(self):
        row, note = tape_measure.measure('trench', 'depth', cm_readings(100, 105))

        self.assertIsNone(row)
        self.assertIn('outside', note)

    def test_a_length_beyond_the_plausible_range_is_refused(self):
        row, _ = tape_measure.measure('trench', 'length', cm_readings(0, 99999))

        self.assertIsNone(row)

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

    def test_a_tape_photo_fills_the_depth_field_and_writes_nothing(self):
        before = self.feature.field_measurements

        with mock.patch('supervision.tape_measure.ocr_available', return_value=True), mock.patch(
            'supervision.tape_measure.read_tape', return_value=self.read()
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body['measurement']['key'], 'actual_depth_mm')
        self.assertEqual(body['measurement']['value'], 500)
        self.assertEqual(body['photo_id'], str(self.photo.id))
        self.assertIn('tape reads', body['notes'].lower())

        self.feature.refresh_from_db()
        self.assertEqual(self.feature.field_measurements, before)

    def test_a_tape_that_cannot_be_read_returns_no_measurement(self):
        with mock.patch('supervision.tape_measure.ocr_available', return_value=True), mock.patch(
            'supervision.tape_measure.read_tape', return_value=None
        ):
            response = self.post(
                photo_id=str(self.photo.id), element_type='trench', dimension='depth'
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['measurement'])
        self.assertEqual(response.json()['readings'], [])
        self.assertTrue(response.json()['notes'])

    def test_a_host_without_tesseract_says_so(self):
        with mock.patch('supervision.tape_measure.ocr_available', return_value=False):
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
