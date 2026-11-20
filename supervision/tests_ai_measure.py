"""AI measurements: the arithmetic, its guards, and the endpoint.

An estimate is a proposal with a stated source — the approved design, a
catalogue size, or the span of a known-size object in the photo — and it is
never a tape reading. Three things the field crew depends on are pinned here:

* only a same-plane known size can turn pixels into millimetres, and only
  inside the range where that comparison means anything;
* the model never supplies a number: it reports spans and this module does
  the arithmetic, so a hallucinated millimetre cannot reach the record;
* the endpoint refuses an unknown element, another span's photo, an
  unassigned engineer and a submitted inspection, and writes nothing itself.

The vision call is stubbed in every test, so the suite never touches the
network and does not depend on an API key being present.
"""

import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image, ImageDraw
from rest_framework.test import APIClient

from assignments.models import AssignmentJob
from projects.models import Feature
from supervision import ai_measure
from supervision.models import FeaturePhoto
from supervision.storage import LocalEvidenceStorage, reset_evidence_storage
from testutils.factories import make_feature, make_ftth_project, make_survey_copy, make_user
from users.models import User


def png_bytes(width=1000, height=800):
    """A photo-shaped PNG: big enough and detailed enough to pass upload checks."""
    from PIL import ImageDraw

    image = Image.new('RGB', (width, height), (200, 30, 30))
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 8):
        draw.line((x, 0, x, height), fill=(30, 200, 30) if (x // 8) % 2 else (200, 200, 30))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def live_vision_key() -> str:
    """The key a live vision test would use, or '' when none is configured."""
    return (
        os.getenv('AI_MEASURE_LLM_API_KEY')
        or os.getenv('OPENAI_API_KEY')
        or os.getenv('GEMINI_API_KEY')
        or ''
    ).strip()


def trench_photo_bytes(width=900, height=700):
    """A drawn site photo: a brick of known size beside an open trench.

    A fixture rather than a photograph, but it puts the two things the estimate
    needs in one frame — something of known size to scale from, and an edge to
    measure — without sending the suite to a real site.
    """
    image = Image.new('RGB', (width, height), (235, 232, 226))
    draw = ImageDraw.Draw(image)
    # 215 mm paving brick, lying on the same plane as the trench opening.
    draw.rectangle([80, 470, 295, 560], fill=(150, 74, 58), outline=(90, 40, 30), width=3)
    draw.text((110, 508), 'BRICK 215 mm', fill=(255, 255, 255))
    # Open trench running across the frame.
    draw.rectangle([340, 300, 860, 650], fill=(58, 52, 44), outline=(35, 30, 25), width=4)
    draw.line([600, 300, 600, 650], fill=(200, 200, 200), width=3)
    draw.text((560, 420), 'TRENCH DEPTH', fill=(230, 230, 230))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


class StoredImage:
    """The parts of a FieldFile that ``_photo_data_url`` reads."""

    def __init__(self, data, name='site.png'):
        self._data = data
        self.name = name
        self._handle = None

    def open(self, mode='rb'):
        self._handle = io.BytesIO(self._data)

    def read(self):
        return self._handle.read()

    def close(self):
        self._handle.close()


def trench_observation(**overrides):
    """A brick beside the trench, both seen on the same plane.

    215 mm brick over a 0.2 span, depth over 0.1 — so the depth should come out
    at 215 * (0.1 / 0.2) = 107.5 mm, whatever the design says.
    """
    observation = {
        'element': 'trench',
        'reference': {'kind': 'brick', 'plane': 'face', 'span_norm': 0.2},
        'dimensions': {'depth': {'plane': 'face', 'span_norm': 0.1}},
    }
    observation.update(overrides)
    return observation


class EstimateArithmeticTests(SimpleTestCase):
    """Design value, catalogue size, or scaled span — and nothing else."""

    def feature(self, **properties):
        return SimpleNamespace(properties=properties)

    def estimate(self, feature, element_type='trench', observation=None):
        basis, rows, note = ai_measure.estimate(feature, element_type, observation)
        self.assertTrue(note, 'an estimate must say where the numbers came from')
        return basis, {row['key']: row for row in rows}

    def test_the_approved_design_value_is_used_without_a_photo_reference(self):
        basis, rows = self.estimate(
            self.feature(DEPTH_MM=1200, WIDTH_MM=450), observation=None
        )

        self.assertEqual(basis, 'plan')
        self.assertEqual(rows['estimated_depth_mm']['value'], 1200.0)
        self.assertEqual(rows['estimated_width_mm']['value'], 450.0)
        self.assertEqual(rows['estimated_depth_mm']['confidence'], ai_measure.CONFIDENCE_PLAN)

    def test_a_span_without_design_values_gets_the_catalogue_size(self):
        basis, rows = self.estimate(self.feature(), element_type='chamber')

        self.assertEqual(basis, 'catalogue')
        self.assertEqual(rows['estimated_chamber_opening_mm']['value'], 600.0)
        self.assertEqual(rows['estimated_chamber_depth_mm']['value'], 1200.0)
        self.assertEqual(
            rows['estimated_chamber_opening_mm']['confidence'],
            ai_measure.CONFIDENCE_CATALOGUE,
        )

    def test_design_keys_are_read_in_whatever_case_the_export_used(self):
        _, rows = self.estimate(self.feature(**{'depth_mm': '900'}))

        self.assertEqual(rows['estimated_depth_mm']['value'], 900.0)

    def test_a_same_plane_reference_outranks_the_design_value(self):
        basis, rows = self.estimate(
            self.feature(DEPTH_MM=1200, WIDTH_MM=450), observation=trench_observation()
        )

        self.assertEqual(basis, 'reference')
        self.assertEqual(rows['estimated_depth_mm']['value'], 107.5)
        self.assertEqual(rows['estimated_depth_mm']['confidence'], ai_measure.CONFIDENCE_REFERENCE)
        # No width span was reported, so the width stays the design value.
        self.assertEqual(rows['estimated_width_mm']['value'], 450.0)
        self.assertEqual(rows['estimated_width_mm']['confidence'], ai_measure.CONFIDENCE_PLAN)

    def test_millimetres_in_the_model_reply_are_ignored(self):
        observation = trench_observation()
        observation['dimensions']['depth']['mm'] = 5.0

        _, rows = self.estimate(self.feature(), observation=observation)

        self.assertEqual(rows['estimated_depth_mm']['value'], 107.5)

    def test_rows_only_ever_land_in_estimated_fields(self):
        _, rows = self.estimate(self.feature(DEPTH_MM=1200), observation=trench_observation())

        self.assertEqual(
            sorted(rows), ['estimated_depth_mm', 'estimated_width_mm']
        )
        self.assertFalse([key for key in rows if key.startswith('actual_')])

    def test_an_observation_of_a_different_element_is_dropped(self):
        # A trench estimate made from a photo the model calls a duct would be
        # out by an order of magnitude, so the whole observation is discarded.
        basis, rows = self.estimate(
            self.feature(DEPTH_MM=1200),
            observation=trench_observation(element='duct'),
        )

        self.assertEqual(basis, 'plan')
        self.assertEqual(rows['estimated_depth_mm']['value'], 1200.0)


class ScalingGuardTests(SimpleTestCase):
    """The times the arithmetic would be nonsense, so it is refused."""

    def basis(self, observation, properties=None):
        feature = SimpleNamespace(properties=properties or {'DEPTH_MM': 1200})
        return ai_measure.estimate(feature, 'trench', observation)[0]

    def test_a_reference_on_another_plane_is_refused(self):
        # A brick on the surface cannot scale a depth seen on the far wall:
        # the two are at different distances from the camera.
        observation = trench_observation()
        observation['reference']['plane'] = 'mouth'
        observation['dimensions']['depth']['plane'] = 'wall'

        self.assertEqual(self.basis(observation), 'plan')

    def test_a_dimension_too_narrow_to_measure_is_refused(self):
        observation = trench_observation()
        observation['dimensions']['depth']['span_norm'] = 0.01  # 12.8 px, under MIN_TARGET_PX

        self.assertEqual(self.basis(observation), 'plan')

    def test_a_reference_much_further_away_is_refused(self):
        observation = trench_observation()
        observation['reference']['span_norm'] = 0.004  # ratio 0.04, under the 0.05 floor

        self.assertEqual(self.basis(observation), 'plan')

    def test_a_reference_much_closer_is_refused(self):
        observation = trench_observation()
        observation['reference']['span_norm'] = 0.9
        observation['dimensions']['depth']['span_norm'] = 0.032  # 41 px, ratio 0.036

        self.assertEqual(self.basis(observation), 'plan')

    def test_a_dimension_many_times_the_reference_is_refused(self):
        observation = trench_observation()
        observation['reference']['span_norm'] = 0.01
        observation['dimensions']['depth']['span_norm'] = 0.5  # ratio 50, over the 20x cap

        self.assertEqual(self.basis(observation), 'plan')

    def test_a_zero_span_is_refused(self):
        observation = trench_observation()
        observation['reference']['span_norm'] = 0

        self.assertEqual(self.basis(observation), 'plan')

    def test_the_element_cannot_be_its_own_reference(self):
        observation = trench_observation()
        observation['reference']['kind'] = 'trench'

        self.assertEqual(self.basis(observation), 'plan')

    def test_an_unknown_reference_kind_is_refused(self):
        observation = trench_observation()
        observation['reference']['kind'] = 'coffee cup'

        self.assertEqual(self.basis(observation), 'plan')


class ElementFromLayerNameTests(SimpleTestCase):
    def test_published_layer_names_map_to_their_element(self):
        cases = {
            'final_trenches': 'trench',
            'drop_ducts': 'duct',
            'chambers': 'chamber',
            'pole_route': 'pole',
            # duct is read before cable, and cable before trench: a layer that
            # is genuinely both is the network element it carries.
            'cable_trench': 'cable',
            'FTTX_Chamber_New': 'chamber',
        }

        for name, expected in cases.items():
            with self.subTest(layer=name):
                self.assertEqual(ai_measure.element_type_from_name(name), expected)

    def test_a_layer_that_names_nothing_returns_none(self):
        self.assertIsNone(ai_measure.element_type_from_name('unknown_layer'))
        self.assertIsNone(ai_measure.element_type_from_name(''))
        self.assertIsNone(ai_measure.element_type_from_name(None))


class VisionConfigTests(SimpleTestCase):
    """Explicit overrides first, then whichever provider key the project has."""

    def config(self, **env):
        with mock.patch.dict(os.environ, env, clear=True):
            return ai_measure._vision_config()

    def test_explicit_settings_win(self):
        self.assertEqual(
            self.config(
                AI_MEASURE_LLM_URL='https://llm.example/v1/chat/completions',
                AI_MEASURE_LLM_API_KEY='sk-measure',
                AI_MEASURE_LLM_MODEL='vision-x',
            ),
            ('https://llm.example/v1/chat/completions', 'sk-measure', 'vision-x'),
        )

    def test_the_project_openai_key_is_the_fallback(self):
        self.assertEqual(
            self.config(OPENAI_API_KEY='sk-project'),
            ('https://api.openai.com/v1/chat/completions', 'sk-project', 'gpt-4o-mini'),
        )

    def test_an_explicit_key_beats_the_openai_key(self):
        url, key, _ = self.config(
            OPENAI_API_KEY='sk-project', AI_MEASURE_LLM_API_KEY='sk-measure'
        )

        self.assertEqual((url, key), ('https://api.openai.com/v1/chat/completions', 'sk-measure'))

    def test_a_blank_override_falls_through(self):
        self.assertEqual(
            self.config(OPENAI_API_KEY='sk-project', AI_MEASURE_LLM_MODEL=''),
            ('https://api.openai.com/v1/chat/completions', 'sk-project', 'gpt-4o-mini'),
        )

    def test_a_gemini_key_uses_the_compatible_route(self):
        # Gemini answers the OpenAI chat-completions shape on its own host, and
        # an OpenAI model name sent there is a 404, so the default model has to
        # follow the key rather than be a single constant.
        self.assertEqual(
            self.config(GEMINI_API_KEY='gm-test'),
            (
                'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
                'gm-test',
                'gemini-3-flash-preview',
            ),
        )

    def test_the_openai_key_beats_the_gemini_one(self):
        url, key, model = self.config(OPENAI_API_KEY='sk-project', GEMINI_API_KEY='gm-test')

        self.assertEqual((url, key, model), ('https://api.openai.com/v1/chat/completions', 'sk-project', 'gpt-4o-mini'))

    def test_no_key_at_all_means_no_vision(self):
        self.assertIsNone(self.config())


class ObserveTests(SimpleTestCase):
    """What the vision call sends, and what it does with a bad reply."""

    def setUp(self):
        self.photo = SimpleNamespace(image=StoredImage(png_bytes(900, 700)))

    def reply(self, content):
        response = mock.Mock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': content}}]}
        return response

    def test_without_a_key_the_model_is_never_called(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            'requests.post'
        ) as post:
            self.assertIsNone(ai_measure.observe(self.photo, 'trench'))
            post.assert_not_called()

    def test_the_photo_is_sent_as_a_data_url_and_the_json_reply_parsed(self):
        fenced = '```json\n{"element": "trench", "reference": {"kind": "brick"}}\n```'
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}, clear=True), mock.patch(
            'requests.post', return_value=self.reply(fenced)
        ) as post:
            observation = ai_measure.observe(self.photo, 'trench')

        self.assertEqual(observation, {'element': 'trench', 'reference': {'kind': 'brick'}})
        sent = post.call_args.kwargs
        self.assertTrue(sent['headers']['Authorization'].startswith('Bearer '))
        self.assertIn('data:image/jpeg;base64,', sent['data'])
        self.assertIn('expected to be a trench', sent['data'])

    def test_the_gemini_route_sends_no_token_cap(self):
        # A 200-token cap ended Gemini's reply mid-object (finish_reason length),
        # so the estimator saw an unparsable answer and fell back every time.
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'gm-test'}, clear=True), mock.patch(
            'requests.post', return_value=self.reply('{"element": "trench"}')
        ) as post:
            ai_measure.observe(self.photo, 'trench')

        sent = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(sent['model'], 'gemini-3-flash-preview')
        self.assertNotIn('max_tokens', sent)
        self.assertEqual(sent['reasoning_effort'], 'low')

    def test_the_openai_route_keeps_the_token_cap(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}, clear=True), mock.patch(
            'requests.post', return_value=self.reply('{"element": "trench"}')
        ) as post:
            ai_measure.observe(self.photo, 'trench')

        sent = json.loads(post.call_args.kwargs['data'])
        self.assertEqual(sent['max_tokens'], 200)
        self.assertNotIn('reasoning_effort', sent)

    def test_an_error_response_falls_back_to_the_design_values(self):
        failure = mock.Mock(status_code=500)
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}, clear=True), mock.patch(
            'requests.post', return_value=failure
        ):
            self.assertIsNone(ai_measure.observe(self.photo, 'trench'))

    def test_a_prose_reply_is_ignored(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}, clear=True), mock.patch(
            'requests.post',
            return_value=self.reply('I think the depth is about 900 mm.'),
        ):
            self.assertIsNone(ai_measure.observe(self.photo, 'trench'))

    def test_a_network_failure_falls_back_to_the_design_values(self):
        with mock.patch.dict(os.environ, {'OPENAI_API_KEY': 'sk-test'}, clear=True), mock.patch(
            'requests.post', side_effect=ConnectionError('no route')
        ):
            self.assertIsNone(ai_measure.observe(self.photo, 'trench'))


class LocalEvidenceMixin:
    """Pin evidence storage to local disk for a test that stores a photo.

    ``FeaturePhoto.save()`` hashes the bytes it is given, and the developer's
    shell may point evidence at a bucket, so storing a photo in a test must not
    reach the network.
    """

    def pin_local_evidence(self):
        reset_evidence_storage()
        self.addCleanup(reset_evidence_storage)
        image_field = FeaturePhoto._meta.get_field('image')
        original_storage = image_field.storage
        image_field.storage = LocalEvidenceStorage(
            location=str(settings.MEDIA_ROOT), base_url=settings.MEDIA_URL
        )
        self.addCleanup(setattr, image_field, 'storage', original_storage)


@override_settings(EVIDENCE_S3_BUCKET='', EVIDENCE_PUBLIC_BASE_URL='')
class FeatureAiMeasurementEndpointTests(LocalEvidenceMixin, TestCase):
    """The proposal endpoint: one photo in, a sourced estimate out, no writes."""

    def setUp(self):
        self.pin_local_evidence()
        self.engineer = make_user('ai-measure@example.com', role=User.Role.ENGINEER)
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
        self.url = f'/api/features/{self.feature.id}/ai-measurements/'
        self.photo = self.store_photo(self.feature)

    def store_photo(self, feature):
        photo = FeaturePhoto.objects.create(
            feature=feature,
            project=self.project,
            image=SimpleUploadedFile('site.png', png_bytes(), content_type='image/png'),
            uploaded_by=self.engineer,
        )
        self.addCleanup(FeaturePhoto.objects.get(pk=photo.pk).image.delete, save=False)
        return photo

    def post(self, **body):
        return self.client.post(self.url, body, format='json')

    def test_a_design_based_proposal_is_returned_and_nothing_is_written(self):
        before = self.feature.field_measurements

        with mock.patch('supervision.ai_measure.observe', return_value=None):
            response = self.post(photo_id=str(self.photo.id), element_type='trench')

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body['basis'], 'plan')
        self.assertEqual(body['photo_id'], str(self.photo.id))
        self.assertEqual(
            [row['key'] for row in body['estimates']],
            ['estimated_depth_mm', 'estimated_width_mm'],
        )
        self.assertEqual(body['estimates'][0]['value'], 1200.0)
        self.assertIn('approved design', body['notes'])

        self.feature.refresh_from_db()
        self.assertEqual(self.feature.field_measurements, before)
        self.assertEqual(self.feature.photos.count(), 1)

    def test_a_scaled_observation_is_reported_as_a_reference_estimate(self):
        with mock.patch(
            'supervision.ai_measure.observe', return_value=trench_observation()
        ):
            response = self.post(photo_id=str(self.photo.id), element_type='trench')

        body = response.json()
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(body['basis'], 'reference')
        self.assertEqual(body['estimates'][0]['value'], 107.5)
        self.assertIn('Scaled from the brick', body['notes'])

    def test_an_unknown_element_type_is_refused(self):
        response = self.post(photo_id=str(self.photo.id), element_type='spaceship')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], 'Unknown element type')

    def test_a_photo_belonging_to_another_span_is_refused(self):
        other = make_feature(
            self.project,
            layer_id='final_trenches',
            layer_name='final_trenches',
        )
        other_photo = self.store_photo(other)

        response = self.post(photo_id=str(other_photo.id), element_type='trench')

        self.assertEqual(response.status_code, 404)
        self.assertIn('not found on this feature', response.json()['detail'])

    def test_an_unassigned_engineer_is_refused(self):
        stranger = make_user('ai-measure-stranger@example.com', role=User.Role.ENGINEER)
        self.client.force_authenticate(user=stranger)

        response = self.post(photo_id=str(self.photo.id), element_type='trench')

        self.assertEqual(response.status_code, 403)

    def test_a_submitted_inspection_is_locked(self):
        Feature.objects.filter(pk=self.feature.pk).update(status=Feature.STATUS_UNDER_REVIEW)

        response = self.post(photo_id=str(self.photo.id), element_type='trench')

        self.assertEqual(response.status_code, 409)
        self.assertIn('locked', response.json()['detail'])

    def test_an_unknown_span_is_not_found(self):
        response = self.client.post(
            '/api/features/00000000-0000-0000-0000-000000000000/ai-measurements/',
            {'photo_id': str(self.photo.id), 'element_type': 'trench'},
            format='json',
        )

        self.assertEqual(response.status_code, 404)

    def test_a_signed_out_caller_is_refused(self):
        self.client.force_authenticate(user=None)

        response = self.post(photo_id=str(self.photo.id), element_type='trench')

        self.assertIn(response.status_code, (401, 403))

    def test_the_proposal_does_not_change_the_photo_gallery(self):
        with mock.patch('supervision.ai_measure.observe', return_value=None):
            self.post(photo_id=str(self.photo.id), element_type='trench')

        self.assertEqual(self.feature.photos.count(), 1)


@override_settings(EVIDENCE_S3_BUCKET='', EVIDENCE_PUBLIC_BASE_URL='')
class LiveVisionEndToEndTests(LocalEvidenceMixin, TestCase):
    """One real image through the vision model and the endpoint the app calls.

    Skipped when no key is configured, so the suite still runs offline; when a
    key is present a failure means the model path itself is broken, not that
    the arithmetic changed — the stubbed tests cover the arithmetic.

    Run it on its own with:
        manage.py test supervision.tests_ai_measure.LiveVisionEndToEndTests
    """

    def setUp(self):
        if not live_vision_key():
            raise unittest.SkipTest(
                'No vision key: set OPENAI_API_KEY, GEMINI_API_KEY or AI_MEASURE_LLM_API_KEY.'
            )
        self.pin_local_evidence()
        self.engineer = make_user('ai-measure-live@example.com', role=User.Role.ENGINEER)
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
        self.url = f'/api/features/{self.feature.id}/ai-measurements/'

    def upload(self):
        response = self.client.post(
            f'/api/features/{self.feature.id}/photos/',
            {
                'photos': SimpleUploadedFile(
                    'trench.png', trench_photo_bytes(), content_type='image/png'
                )
            },
        )
        self.assertEqual(response.status_code, 201, response.content[:300])
        photo_id = response.json()['uploaded'][0]['id']
        self.addCleanup(FeaturePhoto.objects.get(pk=photo_id).image.delete, save=False)
        return photo_id

    def test_a_real_image_is_read_by_the_model_and_proposed_to_the_caller(self):
        photo_id = self.upload()

        response = self.client.post(
            self.url, {'photo_id': photo_id, 'element_type': 'trench'}, format='json'
        )

        self.assertEqual(response.status_code, 200, response.content[:300])
        body = response.json()
        self.assertEqual(body['photo_id'], photo_id)
        self.assertIn(body['basis'], ('plan', 'catalogue', 'reference'))
        self.assertTrue(body['notes'])
        self.assertTrue(body['estimates'])
        for row in body['estimates']:
            self.assertTrue(row['key'].startswith('estimated_'), row['key'])
            self.assertGreater(row['value'], 0)
            self.assertLessEqual(row['value'], 20000)
            self.assertIn(row['confidence'], (0.6, 0.35, 0.3))

        # The response above proves the endpoint ran; this proves the model
        # actually answered and its reply was parsed, rather than the estimate
        # quietly falling back to the design values.
        observation = ai_measure.observe(FeaturePhoto.objects.get(pk=photo_id), 'trench')
        self.assertIsInstance(observation, dict, 'the live model returned no parsable JSON')
        self.assertTrue(str(observation.get('element') or '').strip())
