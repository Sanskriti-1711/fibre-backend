"""The upload gate: a photo nobody could review is refused before storage.

The rules are about readability, not composition: size, exposure and focus.
Framing and glare belong to the subject and are reported by the tape reader.
"""

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image, ImageDraw

from projects.photo_gallery import photo_quality


def photo(width=1000, height=800, colour=(200, 30, 30), stripes=True):
    image = Image.new('RGB', (width, height), colour)
    if stripes:
        draw = ImageDraw.Draw(image)
        for x in range(0, width, 8):
            draw.line((x, 0, x, height), fill=(30, 200, 30) if (x // 8) % 2 else (200, 200, 30))
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    return SimpleUploadedFile('site.png', buffer.getvalue(), content_type='image/png')


class PhotoQualityTests(SimpleTestCase):
    def test_a_sharp_daylight_photo_is_accepted(self):
        self.assertIsNone(photo_quality(photo()))

    def test_a_thumbnail_is_refused(self):
        self.assertIn('too small', photo_quality(photo(320, 240)))

    def test_a_black_frame_is_refused(self):
        self.assertIn('almost black', photo_quality(photo(colour=(5, 5, 5), stripes=False)))

    def test_a_washed_out_frame_is_refused(self):
        self.assertIn(
            'washed out', photo_quality(photo(colour=(252, 252, 252), stripes=False))
        )

    def test_a_frame_with_no_detail_is_refused_as_out_of_focus(self):
        self.assertIn('out of focus', photo_quality(photo(colour=(128, 128, 128), stripes=False)))

    def test_a_file_that_is_not_an_image_is_refused(self):
        upload = SimpleUploadedFile('site.png', b'not really a png', content_type='image/png')

        self.assertIn('could not be read', photo_quality(upload))
