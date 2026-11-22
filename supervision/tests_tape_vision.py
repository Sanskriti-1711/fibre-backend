"""The vision-model tape reader: its provider, its refusals, its shape.

Three things are pinned here. Without a key it makes no call and answers
nothing. Numbers read off a blade come back through ``measure`` as the same
span an OCR reading would, in the same shape. And a reply that is not a blade —
too few numbers, or numbers that do not sit on the blade's pitch — is refused
rather than filled into the form.

The provider is stubbed, so the suite needs no key and uploads no image.
"""

import json
import os
from unittest import mock

from django.test import SimpleTestCase
from PIL import Image

from supervision import tape_measure, tape_vision
from supervision.tests_ai_measure import png_bytes


def reply(numbers, unit='cm'):
    """A chat-completions answer whose message holds the model's JSON."""
    content = json.dumps(
        {'numbers': list(numbers), 'unit': unit, 'blade_visible': True}
    )
    response = mock.Mock()
    response.status_code = 200
    response.json.return_value = {'choices': [{'message': {'content': content}}]}
    return response


class VisionTapeTests(SimpleTestCase):
    """The model is asked about a blade, and its answer is guarded like OCR."""

    def read(self, *responses, env=None):
        environment = {'GEMINI_API_KEY': 'gm-test'} if env is None else env
        posted = []

        def post(url, **kwargs):
            posted.append(url)
            return responses[min(len(posted) - 1, len(responses) - 1)]

        with mock.patch.dict(os.environ, environment, clear=True), mock.patch(
            'requests.post', side_effect=post
        ):
            result = tape_vision.read_tape_bytes(png_bytes(600, 300))
        return result, posted

    def test_the_reader_is_off_unless_asked_for(self):
        with mock.patch.dict(os.environ, {'GEMINI_API_KEY': 'gm-test'}, clear=True):
            self.assertFalse(tape_vision.configured())

        with mock.patch.dict(
            os.environ, {'GEMINI_API_KEY': 'gm-test', 'TAPE_VISION': '1'}, clear=True
        ):
            self.assertTrue(tape_vision.configured())

    def test_without_a_key_nothing_is_sent(self):
        result, posted = self.read(env={})

        self.assertIsNone(result)
        self.assertEqual(posted, [])

    def test_the_blades_numbers_become_the_span(self):
        result, _ = self.read(reply([120, 130, 140, 150]))

        self.assertEqual(result['engine'], 'vision-model')
        self.assertEqual(result['unit'], 'cm')
        self.assertEqual(
            [row['value'] for row in result['readings']], [120, 130, 140, 150]
        )
        row, _ = tape_measure.measure(
            'trench', 'depth', result['readings'], result['unit']
        )
        self.assertEqual(row['value'], 300)

    def test_a_two_number_reply_is_refused(self):
        # The shape that turned a date stamp into a 2 m trench depth.
        result, _ = self.read(reply([8, 2026]))

        self.assertIsNone(result)

    def test_numbers_off_the_blades_pitch_are_refused(self):
        # 50 cm does not belong on a blade printing 10, 20, 30.
        result, _ = self.read(reply([10, 20, 30, 50]))

        self.assertIsNone(result)

    def test_a_completed_sequence_is_refused(self):
        # The model reciting 0…30 cm on a metre tape, as it did on the field
        # photos, is a sequence rather than a transcription.
        result, _ = self.read(reply(range(0, 31)))

        self.assertIsNone(result)

    def test_numbers_are_placed_where_the_model_saw_them(self):
        answer = {
            'numbers': [
                {'value': 120, 'x': 0.12, 'y': 0.5},
                {'value': 130, 'x': 0.41, 'y': 0.5},
                {'value': 140, 'x': 0.72, 'y': 0.5},
            ],
            'unit': 'cm',
        }
        result, _ = self.read(reply(answer['numbers']))

        xs = [row['x'] for row in result['readings']]
        self.assertEqual(xs, [0.12, 0.41, 0.72])

    def test_a_reply_that_is_not_json_falls_through_to_the_next_crop(self):
        text_only = mock.Mock()
        text_only.status_code = 200
        text_only.json.return_value = {
            'choices': [{'message': {'content': 'I see a tape measure.'}}]
        }

        first, second = Image.new('RGB', (600, 300)), Image.new('RGB', (400, 200))
        with mock.patch(
            'supervision.tape_vision._crops', return_value=iter([first, second])
        ):
            result, _ = self.read(text_only, reply([2, 3, 4, 5]))

        self.assertEqual([row['value'] for row in result['readings']], [2, 3, 4, 5])

    def test_a_failed_call_is_not_an_error(self):
        failed = mock.Mock()
        failed.status_code = 500

        self.assertIsNone(self.read(failed)[0])

    def test_the_gemini_route_is_used_when_only_that_key_exists(self):
        _, posted = self.read(reply([2, 3, 4]))

        self.assertIn('generativelanguage.googleapis.com', posted[0])
