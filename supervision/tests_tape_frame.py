"""The live viewfinder's frame route: refusals, auth, and no writes.

The overlay reads through the same ``tape_reader`` the stored-photo route uses,
so what is pinned here is the contract around it rather than the detector:

* a signed-out caller cannot poll frames;
* a frame is required, and an oversized one is refused before it is read;
* a frame the reader trips on comes back as a refusal naming the stage instead
  of a 500 — the viewfinder polls, so one bad frame must not end the session;
* nothing is persisted, because a frame was never kept.
"""

import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from supervision.models import FeaturePhoto
from testutils.factories import make_user
from users.models import User

URL = '/api/tape/frame/'


def jpeg_bytes(size=(240, 320), colour=(128, 128, 128)):
    image = Image.new('RGB', size, colour)
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG')
    return buffer.getvalue()


class TapeFrameEndpointTests(TestCase):
    def setUp(self):
        self.engineer = make_user('live-frame@example.com', role=User.Role.ENGINEER)
        self.client = APIClient()
        self.client.force_authenticate(user=self.engineer)

    def post(self, data=None, name='frame.jpg', content_type='image/jpeg'):
        payload = {}
        if data is not None:
            payload['frame'] = SimpleUploadedFile(name, data, content_type=content_type)
        return self.client.post(URL, payload, format='multipart')

    def test_a_signed_out_caller_cannot_poll_frames(self):
        self.client.force_authenticate(user=None)

        response = self.post(jpeg_bytes())

        self.assertIn(response.status_code, (401, 403))

    def test_a_missing_frame_is_a_400(self):
        response = self.post(None)

        self.assertEqual(response.status_code, 400)

    def test_an_oversized_frame_is_refused_before_it_is_read(self):
        with mock.patch('supervision.views.tape_reader.read_bytes') as read:
            response = self.post(b'x' * (4 * 1024 * 1024 + 1))

        self.assertEqual(response.status_code, 400)
        read.assert_not_called()

    def test_a_plain_frame_answers_with_a_clip_verdict_and_the_framing(self):
        response = self.post(jpeg_bytes())

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn('clip', body)
        self.assertIn('usable', body['clip'])
        self.assertIn('framing', body)
        self.assertGreaterEqual(body['elapsed_ms'], 0)

    def test_a_frame_with_no_tape_in_it_is_refused_rather_than_guessed(self):
        response = self.post(jpeg_bytes())

        body = response.json()
        self.assertFalse(body['clip']['usable'])
        self.assertTrue(body['clip']['stage'])
        self.assertTrue(body['clip']['reason'])

    def test_a_reader_exception_is_a_refusal_not_a_500(self):
        with mock.patch(
            'supervision.views.tape_reader.read_bytes', side_effect=RuntimeError('boom')
        ):
            response = self.post(jpeg_bytes())

        self.assertEqual(response.status_code, 200)
        clip = response.json()['clip']
        self.assertFalse(clip['usable'])
        self.assertEqual(clip['stage'], 'reader')

    def test_bytes_that_are_not_an_image_are_refused_with_the_photo_stage(self):
        response = self.post(b'not an image at all')

        self.assertEqual(response.status_code, 200)
        clip = response.json()['clip']
        self.assertFalse(clip['usable'])
        self.assertEqual(clip['stage'], 'photo')

    def test_polling_frames_writes_nothing(self):
        before = FeaturePhoto.objects.count()

        self.post(jpeg_bytes())

        self.assertEqual(FeaturePhoto.objects.count(), before)
