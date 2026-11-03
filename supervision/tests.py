"""Evidence storage: what a photo is, where its bytes go, and what is refused.

The point of these tests is the contract the field crew depends on: an upload
either lands with a checksum that identifies the exact bytes, or it is refused
before it reaches storage. Bytes go to whatever ``get_evidence_storage()``
points at — local disk here, an S3-compatible bucket in deployment — so the S3
wiring is checked against a recording stub rather than the network.
"""

import hashlib
import io
import shutil
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from projects.models import Feature, Project
from supervision.models import FeaturePhoto
from supervision.storage import S3EvidenceStorage, get_evidence_storage, reset_evidence_storage
from users.models import User


def png_bytes(width=8, height=8):
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (200, 30, 30)).save(buffer, format='PNG')
    return buffer.getvalue()


class FeaturePhotoEvidenceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Evidence test')
        self.feature = Feature.objects.create(
            project=self.project,
            layer_name='final_trenches',
            layer_id='final_trenches',
        )
        self.admin = User.objects.create_user(
            email='evidence-admin@example.com',
            password='demo1234',
            full_name='Evidence Admin',
            role=User.Role.SUBADMIN,
        )
        token = str(RefreshToken.for_user(self.admin).access_token)
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer %s' % token}
        self.payload = png_bytes()

    def url(self, suffix=''):
        return '/api/features/%s/photos/%s' % (self.feature.id, suffix)

    def upload(self, name='site.png', payload=None, content_type='image/png'):
        return self.client.post(
            self.url(),
            {'photos': SimpleUploadedFile(name, payload if payload is not None else self.payload, content_type)},
            **self.auth,
        )

    def test_upload_records_checksum_and_size(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.content[:300])
        body = response.json()['uploaded'][0]

        expected = hashlib.sha256(self.payload).hexdigest()
        self.assertEqual(body['sha256'], expected)
        self.assertEqual(body['size_bytes'], len(self.payload))
        self.assertEqual(body['storage_backend'], 'local')

        photo = FeaturePhoto.objects.get(id=body['id'])
        self.addCleanup(photo.image.delete, save=False)
        self.assertTrue(photo.image.storage.exists(photo.image.name))
        self.assertEqual(photo.sha256, expected)

    def test_identical_bytes_give_the_same_checksum(self):
        first = self.upload('a.png').json()['uploaded'][0]
        second = self.upload('b.png').json()['uploaded'][0]
        self.addCleanup(FeaturePhoto.objects.get(id=first['id']).image.delete, save=False)
        self.addCleanup(FeaturePhoto.objects.get(id=second['id']).image.delete, save=False)

        self.assertEqual(first['sha256'], second['sha256'])
        self.assertNotEqual(first['id'], second['id'])

    def test_different_bytes_give_a_different_checksum(self):
        first = self.upload('a.png').json()['uploaded'][0]
        other = png_bytes(9, 9)
        second = self.upload('b.png', other).json()['uploaded'][0]
        self.addCleanup(FeaturePhoto.objects.get(id=first['id']).image.delete, save=False)
        self.addCleanup(FeaturePhoto.objects.get(id=second['id']).image.delete, save=False)

        self.assertNotEqual(first['sha256'], second['sha256'])

    def test_oversized_file_is_refused_before_storage(self):
        oversized = b'\x89PNG\r\n\x1a\n' + b'\0' * (FeaturePhoto.MAX_BYTES + 1)
        response = self.upload('huge.png', oversized)

        self.assertEqual(response.status_code, 400)
        self.assertIn('Maximum size is 10MB', response.json()['detail'])
        self.assertEqual(FeaturePhoto.objects.count(), 0)

    def test_empty_file_is_refused(self):
        response = self.upload('empty.png', b'')

        self.assertEqual(response.status_code, 400)
        self.assertIn('empty', response.json()['detail'].lower())

    def test_wrong_type_is_refused(self):
        response = self.upload('notes.txt', b'not an image', 'text/plain')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(FeaturePhoto.objects.count(), 0)

    def test_delete_removes_the_stored_object(self):
        body = self.upload().json()['uploaded'][0]
        photo = FeaturePhoto.objects.get(id=body['id'])
        storage, name = photo.image.storage, photo.image.name
        self.assertTrue(storage.exists(name))

        response = self.client.delete(self.url('%s/' % body['id']), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(FeaturePhoto.objects.filter(id=body['id']).exists())
        self.assertFalse(storage.exists(name))


class S3EvidenceStorageTests(TestCase):
    """The S3 path without a network: a stub records what would be sent."""

    class RecordingClient:
        def __init__(self, present=True):
            self.calls = []
            # `head_object` answering for every key makes `exists()` always true,
            # and Django's `Storage.save()` then walks an endless
            # "name, name_1, name_2..." loop looking for a free one. A save test
            # therefore needs the stub to behave like a bucket that does not have
            # the key yet.
            self.present = present

        def upload_fileobj(self, **kwargs):
            self.calls.append(('upload', kwargs))

        def head_object(self, **kwargs):
            self.calls.append(('head', kwargs))
            if not self.present:
                raise LookupError('NoSuchKey')
            return {'ContentLength': 42}

        def delete_object(self, **kwargs):
            self.calls.append(('delete', kwargs))

        def generate_presigned_url(self, operation, Params, ExpiresIn):
            self.calls.append(('presign', {'operation': operation, **Params, 'ExpiresIn': ExpiresIn}))
            return 'https://bucket.example/%s?X-Amz-Signature=abc' % Params['Key']

    def make_storage(self, **kwargs):
        storage = S3EvidenceStorage(bucket='evidence', **kwargs)
        storage._client = self.RecordingClient()
        return storage

    def test_save_uploads_with_the_content_type(self):
        storage = self.make_storage()
        storage.client.present = False
        upload = SimpleUploadedFile('site.png', png_bytes(), content_type='image/png')

        name = storage.save('feature_photos/2026/11/02/site.png', upload)

        # The name-availability probe (`head`) comes first; the upload is last.
        operation, kwargs = storage.client.calls[-1]
        self.assertEqual(operation, 'upload')
        self.assertEqual(kwargs['Bucket'], 'evidence')
        self.assertEqual(kwargs['Key'], name)
        self.assertEqual(kwargs['ExtraArgs'], {'ContentType': 'image/png'})

    def test_public_base_url_serves_a_stable_link(self):
        storage = self.make_storage(public_base_url='https://media.example.com/')

        self.assertEqual(
            storage.url('feature_photos/site.png'),
            'https://media.example.com/feature_photos/site.png',
        )

    def test_private_bucket_hands_out_a_presigned_url(self):
        storage = self.make_storage()

        url = storage.url('feature_photos/site.png')

        self.assertIn('X-Amz-Signature', url)
        operation, kwargs = storage.client.calls[0]
        self.assertEqual(operation, 'presign')
        self.assertEqual(kwargs['Bucket'], 'evidence')
        self.assertEqual(kwargs['ExpiresIn'], 3600)

    def test_delete_and_exists_use_the_configured_bucket(self):
        storage = self.make_storage()

        self.assertTrue(storage.exists('feature_photos/site.png'))
        self.assertEqual(storage.size('feature_photos/site.png'), 42)
        storage.delete('feature_photos/site.png')

        self.assertEqual([call[0] for call in storage.client.calls], ['head', 'head', 'delete'])
        self.assertEqual(storage.client.calls[-1][1]['Bucket'], 'evidence')

    def test_the_configured_store_is_the_s3_one_when_a_bucket_is_set(self):
        from django.test import override_settings

        from supervision.storage import reset_evidence_storage

        with override_settings(EVIDENCE_S3_BUCKET='evidence', EVIDENCE_S3_ENDPOINT_URL='https://x.r2.cloudflarestorage.com'):
            reset_evidence_storage()
            self.addCleanup(reset_evidence_storage)
            storage = get_evidence_storage()

        self.assertIsInstance(storage, S3EvidenceStorage)
        self.assertEqual(storage.bucket, 'evidence')
        self.assertEqual(storage.endpoint_url, 'https://x.r2.cloudflarestorage.com')


class MediaServingTests(TestCase):
    """The route that makes a stored photo's URL resolve on the deployed host.

    Django mounts MEDIA_URL from `static()` only while DEBUG is on, so the host
    answered 404 for files it was holding. These tests pin the replacement route,
    including the DEBUG=false case that was broken.
    """

    def setUp(self):
        self.media_root = Path(tempfile.mkdtemp(prefix='evidence-media-'))
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.name = 'feature_photos/2026/11/02/site.png'
        target = self.media_root / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(png_bytes())

    def get(self, path=None, **kwargs):
        with override_settings(MEDIA_ROOT=self.media_root):
            return self.client.get('/media/%s' % (path or self.name), **kwargs)

    def head(self, path=None):
        with override_settings(MEDIA_ROOT=self.media_root):
            return self.client.head('/media/%s' % (path or self.name))

    def test_a_stored_photo_is_served_with_debug_off(self):
        with override_settings(DEBUG=False):
            response = self.get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), png_bytes())
        self.assertEqual(response['Content-Type'], 'image/png')
        self.assertEqual(response['Cache-Control'], 'private, max-age=86400')

    def test_head_returns_headers_but_no_body(self):
        with override_settings(DEBUG=False):
            response = self.head()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')

    def test_a_missing_file_is_404_not_a_crash(self):
        with override_settings(DEBUG=False):
            response = self.client.get('/media/feature_photos/nope.png')

        self.assertEqual(response.status_code, 404)

    def test_traversal_cannot_read_outside_media_root(self):
        with override_settings(DEBUG=False):
            response = self.client.get('/media/..%2Fconfig%2Fsettings.py')

        # Django rejects the path before the view (400) or safe_join does (404);
        # either way the file must not come back.
        self.assertIn(response.status_code, (400, 404))
        self.assertNotIn(b'SECRET_KEY', response.content)

    def test_writes_are_refused(self):
        response = self.client.post('/media/%s' % self.name)

        self.assertEqual(response.status_code, 405)

    def test_object_storage_redirects_instead_of_streaming(self):
        with override_settings(
            EVIDENCE_S3_BUCKET='evidence',
            EVIDENCE_PUBLIC_BASE_URL='https://media.example.com',
        ):
            reset_evidence_storage()
            self.addCleanup(reset_evidence_storage)
            response = self.get()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response['Location'], 'https://media.example.com/%s' % self.name
        )
