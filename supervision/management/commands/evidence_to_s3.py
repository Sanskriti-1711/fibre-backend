"""Move already-recorded evidence photos into the configured bucket.

The storage backend is configuration (``EVIDENCE_S3_*``), and a row remembers
which one accepted its bytes (``FeaturePhoto.storage_backend``). Turning the
bucket on therefore changes where *new* photos go, while every photo taken
before it stays on the disk it was written to — and on a host with an ephemeral
disk, that disk is thrown away on the next deploy.

This command closes that gap: it reads each locally stored photo, writes the
same bytes under the same key to the bucket, and only then flips the row's
``storage_backend``. Nothing is deleted locally, so a failed run leaves the
evidence exactly where it was.

    python manage.py evidence_to_s3            # copy every local-disk row
    python manage.py evidence_to_s3 --all      # re-copy rows already on s3
    python manage.py evidence_to_s3 --check    # verify the bucket, copy nothing
    python manage.py evidence_to_s3 --dry-run  # list what would be copied

``--check`` is the one to run after a deploy: it asks the bucket for every
row's object and compares the byte count the row recorded at upload time, so a
half-finished migration reports itself rather than showing up as a 404 in the
audit view weeks later.
"""

from __future__ import annotations

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.management.base import BaseCommand, CommandError

from supervision.models import FeaturePhoto
from supervision.storage import get_evidence_storage


def _human(size):
    value = float(size or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} GB'


class Command(BaseCommand):
    help = (
        'Copy evidence photos recorded on local disk into the configured '
        'S3-compatible bucket (EVIDENCE_S3_BUCKET), or verify they are there.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--check',
            action='store_true',
            help='Verify every row resolves in the bucket; copy nothing.',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            help='Re-copy rows that are already marked as bucket-backed.',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would be copied without touching the bucket.',
        )

    def handle(self, *args, **options):
        storage = get_evidence_storage()
        label = getattr(storage, 'label', '')
        if options['check']:
            self._check(storage)
            return
        if label != 's3':
            msg = (
                'The evidence store is still local disk. Set EVIDENCE_S3_BUCKET '
                '(+ endpoint, region, access key, secret) before running this.'
            )
            raise CommandError(msg)

        local = FileSystemStorage(location=str(settings.MEDIA_ROOT))
        rows = FeaturePhoto.objects.exclude(image='')
        if not options['all']:
            rows = rows.exclude(storage_backend='s3')
        rows = rows.order_by('uploaded_at')

        copied = skipped = missing = failed = 0
        for photo in rows:
            name = photo.image.name
            if not local.exists(name):
                missing += 1
                self.stderr.write(f'  missing on disk: {photo.id} {name} — no bytes to copy')
                continue
            if options['dry_run']:
                copied += 1
                self.stdout.write(f'  would copy {photo.id} {name} ({_human(photo.size_bytes)})')
                continue
            try:
                with local.open(name, 'rb') as handle:
                    # Written straight to the key the row already holds, so the
                    # URL, the id and `order` all survive the move unchanged.
                    storage.client.upload_fileobj(
                        Bucket=storage.bucket,
                        Key=name,
                        Fileobj=handle,
                        ExtraArgs={'ContentType': _content_type(name)},
                    )
            except Exception as exc:  # noqa: BLE001 - report, keep going
                failed += 1
                self.stderr.write(f'  failed {photo.id} {name}: {exc}')
                continue

            photo.storage_backend = 's3'
            photo.save(update_fields=['storage_backend'])
            copied += 1
            self.stdout.write(f'  copied {photo.id} {name} ({_human(photo.size_bytes)})')

        if options['dry_run']:
            self.stdout.write(
                self.style.WARNING(f'Dry run: {copied} row(s) would be copied, {missing} missing.')
            )
            return
        self.stdout.write(
            self.style.SUCCESS(
                f'Copied {copied}, already on the bucket {skipped}, '
                f'missing {missing}, failed {failed}.'
            )
        )
        if failed or missing:
            msg = 'Some photos were not moved; they are still on local disk.'
            raise CommandError(msg)

    def _check(self, storage):
        """Every row must answer with the byte count recorded at upload."""
        rows = FeaturePhoto.objects.exclude(image='').order_by('uploaded_at')
        ok = bad = 0
        for photo in rows:
            name = photo.image.name
            try:
                head = storage.client.head_object(Bucket=storage.bucket, Key=name)
            except Exception as exc:  # noqa: BLE001 - absent is the answer
                bad += 1
                self.stderr.write(f'  NOT IN BUCKET {photo.id} {name}: {exc}')
                continue
            size = head.get('ContentLength')
            if photo.size_bytes and size != photo.size_bytes:
                bad += 1
                self.stderr.write(
                    f'  SIZE MISMATCH {photo.id} {name}: '
                    f'{size} bytes in bucket, {photo.size_bytes} recorded'
                )
                continue
            ok += 1
            self.stdout.write(
                f'  ok {photo.id} {name} ({_human(size)}, ' f'{photo.storage_backend or "unset"})'
            )
        if bad:
            msg = f'{bad} photo(s) do not resolve in the bucket.'
            raise CommandError(msg)
        self.stdout.write(self.style.SUCCESS(f'All {ok} evidence photo(s) resolve in the bucket.'))
        return


def _content_type(name):
    """The type the upload allowlist already accepted, from the extension."""
    lower = name.lower()
    if lower.endswith('.png'):
        return 'image/png'
    return 'image/jpeg'
