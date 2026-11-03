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
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
        self.payload = png_bytes()

    def url(self, suffix=''):
        return f'/api/features/{self.feature.id}/photos/{suffix}'

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

        response = self.client.delete(self.url('{}/'.format(body['id'])), **self.auth)

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
            return 'https://bucket.example/{}?X-Amz-Signature=abc'.format(Params['Key'])

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
        response = self.client.post(f'/media/{self.name}')

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
            response['Location'], f'https://media.example.com/{self.name}'
        )


class DesignChangeLoopTests(TestCase):
    """Approved deviation → request for the next LLD version.

    The contract: the request is recorded on the *LLD* timeline, the planners are
    told, asking twice about one deviation stays one request, and nothing in the
    design stages is written.
    """

    def setUp(self):
        from projects.models import ProjectMember

        self.project = Project.objects.create(
            name='Design loop test',
            source_lld_run_id='ftth-proj:LLD-V05',
        )
        self.feature = Feature.objects.create(
            project=self.project,
            layer_name='final_trenches',
            layer_id='final_trenches',
            properties={'depth': 1.2},
            field_measurements={'depth': {'value': 1.8, 'unit': 'm'}},
        )
        self.reviewer = User.objects.create_user(
            email='reviewer@example.com',
            password='demo1234',
            full_name='Reviewer',
            role=User.Role.SUBADMIN,
        )
        self.planner = User.objects.create_user(
            email='planner@example.com',
            password='demo1234',
            full_name='Planner',
            role=User.Role.SUBADMIN,
        )
        ProjectMember.objects.create(
            project=self.project,
            user=self.planner,
            role=ProjectMember.Role.PLANNER,
        )
        token = str(RefreshToken.for_user(self.reviewer).access_token)
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {token}'}

    @property
    def url(self):
        return f'/api/features/{self.feature.id}/request-design-revision/'

    def record_deviation(self, reason='gas main in the way'):
        from supervision.models import FeatureDecisionRecord

        return FeatureDecisionRecord.objects.create(
            feature=self.feature,
            project=self.project,
            decision=FeatureDecisionRecord.DECISION_APPROVED_WITH_DEVIATION,
            reason=reason,
            deviation={
                'tolerance': 0.1,
                'measured': 1,
                'outside_tolerance': 1,
                'has_deviation': True,
                'fields': [
                    {
                        'key': 'depth',
                        'planned': 1.2,
                        'as_built': 1.8,
                        'delta': -0.6,
                        'unit': 'm',
                        'within_tolerance': False,
                    }
                ],
            },
            reviewer=self.reviewer,
        )

    def test_no_deviation_is_a_conflict(self):
        from projects.models import StageEvent

        response = self.client.post(self.url, {}, **self.auth)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(StageEvent.objects.filter(event='design_revision_requested').exists())

    def test_request_lands_on_the_lld_timeline(self):
        from projects.models import StageEvent

        record = self.record_deviation()
        response = self.client.post(self.url, {'note': 'move the chamber east'}, **self.auth)

        self.assertEqual(response.status_code, 201, response.content[:300])
        body = response.json()
        self.assertFalse(body['duplicate'])
        self.assertEqual(body['record_id'], str(record.id))
        self.assertEqual(body['design_version'], 'ftth-proj:LLD-V05')
        self.assertIn(f'ftth-lld-review.html?project_id={self.project.id}', body['review_url'])

        event = StageEvent.objects.get(event='design_revision_requested')
        self.assertEqual(event.stage, StageEvent.Stage.LLD)
        self.assertEqual(event.entity_id, str(self.feature.id))
        self.assertEqual(event.metadata['record_id'], str(record.id))
        self.assertEqual(event.metadata['fields'], ['depth'])
        self.assertEqual(event.actor_id, self.reviewer.id)

    def test_planner_is_notified_once(self):
        from notifications.models import Notification

        self.record_deviation()
        self.client.post(self.url, {}, **self.auth)
        self.client.post(self.url, {}, **self.auth)

        rows = Notification.objects.filter(recipient=self.planner)
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.category, Notification.CATEGORY_DESIGN)
        self.assertIn('ftth-lld-review.html', row.url)
        self.assertIn('Design revision requested', row.title)

    def test_asking_twice_is_one_request(self):
        from projects.models import StageEvent

        self.record_deviation()
        first = self.client.post(self.url, {}, **self.auth).json()
        second = self.client.post(self.url, {}, **self.auth)

        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()['duplicate'])
        self.assertEqual(second.json()['requested_at'], first['requested_at'])
        self.assertEqual(StageEvent.objects.filter(event='design_revision_requested').count(), 1)

    def test_a_later_deviation_can_be_revised_again(self):
        from projects.models import StageEvent

        self.record_deviation()
        self.client.post(self.url, {}, **self.auth)
        self.record_deviation('second pass, still short')

        response = self.client.post(self.url, {}, **self.auth)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(StageEvent.objects.filter(event='design_revision_requested').count(), 2)

    def test_decision_list_carries_the_requests(self):
        record = self.record_deviation()
        self.client.post(self.url, {}, **self.auth)

        body = self.client.get(
            f'/api/features/{self.feature.id}/decisions/', **self.auth
        ).json()

        self.assertEqual(body['count'], 1)
        self.assertEqual(len(body['revision_requests']), 1)
        self.assertEqual(body['revision_requests'][0]['record_id'], str(record.id))
        self.assertEqual(body['revision_requests'][0]['requested_by'], 'Reviewer')

    def test_the_design_stages_are_not_touched(self):
        """The request is a proposal: no LLD row, no feature edit."""
        from ftth_lld.models import LldRun

        self.record_deviation()
        bid_before = LldRun.objects.count()
        before = Feature.objects.get(id=self.feature.id)

        self.client.post(self.url, {}, **self.auth)

        after = Feature.objects.get(id=self.feature.id)
        self.assertEqual(LldRun.objects.count(), bid_before)
        self.assertEqual(before.properties, after.properties)
        self.assertEqual(before.field_measurements, after.field_measurements)
        self.assertEqual(before.status, after.status)
