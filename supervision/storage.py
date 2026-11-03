"""Where supervision evidence bytes live.

Rows stay in the platform database (INTEGRATION_PLAN.md §6.2); only the bytes
move. With ``EVIDENCE_S3_BUCKET`` set they go to any S3-compatible bucket —
Cloudflare R2 is the deployment default, because evidence photos are read far
more often than they are written and R2 charges no egress fee. Without it they
stay on local disk, so development and the test suite need no credentials and
no network.

``boto3`` is imported only when a bucket is configured: a checkout that never
stores evidence remotely can run its tests without the SDK installed.
"""

from __future__ import annotations

from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage


class LocalEvidenceStorage(FileSystemStorage):
    """MEDIA_ROOT — the development and test default."""

    label = 'local'


class S3EvidenceStorage(Storage):
    """Any S3-compatible bucket: R2, B2, Wasabi, S3, MinIO.

    ``EVIDENCE_PUBLIC_BASE_URL`` serves the objects from a CDN or a public
    bucket domain; without it ``url()`` hands out a short-lived presigned URL,
    which keeps a private bucket private.
    """

    label = 's3'

    def __init__(
        self,
        bucket,
        endpoint_url=None,
        region=None,
        access_key=None,
        secret_key=None,
        public_base_url=None,
        presign_seconds=3600,
    ):
        self.bucket = bucket
        self.endpoint_url = endpoint_url or None
        self.region = region or None
        self.access_key = access_key or None
        self.secret_key = secret_key or None
        self.public_base_url = (public_base_url or '').rstrip('/') or None
        self.presign_seconds = presign_seconds
        self._client = None

    @property
    def client(self):
        """Built on first use so importing this module needs no credentials."""
        if self._client is None:
            import boto3

            self._client = boto3.client(
                's3',
                endpoint_url=self.endpoint_url,
                region_name=self.region,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        return self._client

    def _save(self, name, content):
        content.seek(0)
        extra = {}
        content_type = getattr(content, 'content_type', None)
        if content_type:
            # Stored with the object so a browser renders it instead of
            # downloading it, whatever the bucket's default is.
            extra['ContentType'] = content_type
        self.client.upload_fileobj(
            Bucket=self.bucket,
            Key=name,
            Fileobj=content,
            ExtraArgs=extra or None,
        )
        return name

    def exists(self, name):
        try:
            self.client.head_object(Bucket=self.bucket, Key=name)
        except Exception:  # noqa: BLE001 - a missing key is the answer, not an error
            return False
        return True

    def size(self, name):
        return self.client.head_object(Bucket=self.bucket, Key=name)['ContentLength']

    def delete(self, name):
        try:
            self.client.delete_object(Bucket=self.bucket, Key=name)
        except Exception:  # noqa: BLE001 - deleting a missing object is a no-op
            pass

    def url(self, name):
        if self.public_base_url:
            return f'{self.public_base_url}/{name}'
        return self.client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket, 'Key': name},
            ExpiresIn=self.presign_seconds,
        )


def _configured_s3_storage():
    """The S3 storage described by settings, or None when no bucket is set."""
    bucket = getattr(settings, 'EVIDENCE_S3_BUCKET', '')
    if not bucket:
        return None
    return S3EvidenceStorage(
        bucket=bucket,
        endpoint_url=getattr(settings, 'EVIDENCE_S3_ENDPOINT_URL', ''),
        region=getattr(settings, 'EVIDENCE_S3_REGION', ''),
        access_key=getattr(settings, 'EVIDENCE_S3_ACCESS_KEY_ID', ''),
        secret_key=getattr(settings, 'EVIDENCE_S3_SECRET_ACCESS_KEY', ''),
        public_base_url=getattr(settings, 'EVIDENCE_PUBLIC_BASE_URL', ''),
        presign_seconds=getattr(settings, 'EVIDENCE_PRESIGN_SECONDS', 3600),
    )


_storage = None


def get_evidence_storage():
    """The evidence store for this process, built once.

    Referenced as a callable from ``FeaturePhoto.image`` so the field follows
    the configuration and migrations serialise the function, not an instance.
    """
    global _storage
    if _storage is None:
        _storage = _configured_s3_storage() or LocalEvidenceStorage(
            location=str(settings.MEDIA_ROOT),
            base_url=settings.MEDIA_URL,
        )
    return _storage


def reset_evidence_storage():
    """Drop the cached store — used by settings overrides and tests."""
    global _storage
    _storage = None
