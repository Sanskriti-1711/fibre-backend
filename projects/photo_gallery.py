"""The one implementation of the evidence-photo rules.

``supervision.models.FeaturePhoto`` and ``survey.models.SurveyFeaturePhoto`` hold
the same fields and enforce the same limits — content hash, type allow-list,
per-file cap, per-feature cap and ordering — and had been copied into each app.
That is how the two ended up numbering photos differently: one used
``Max('order') + 1``, the other ``count()``, so after a middle photo was deleted
they disagreed about the next position. Both apps call this module now.
"""

import hashlib

from django.db.models import Max
from rest_framework import status
from rest_framework.response import Response


def digest(upload):
    """(sha256 hex digest, byte count) of an upload, read once and rewound.

    The digest is what makes a photo provable: the same bytes re-uploaded, or a
    file that changed after review, can be told apart without keeping a second
    copy. ``upload.chunks()`` streams, so a 10 MB photo is never held twice.
    """
    hasher = hashlib.sha256()
    size = 0
    upload.seek(0)
    for chunk in upload.chunks():
        hasher.update(chunk)
        size += len(chunk)
    upload.seek(0)
    return hasher.hexdigest(), size


def validate_photo(upload, *, allowed_content_types, max_bytes):
    """Return an error string for an unacceptable upload, or None."""
    if upload.content_type not in allowed_content_types:
        return 'Invalid file type. Only JPEG and PNG are allowed.'
    if not upload.size:
        return 'The file is empty.'
    if upload.size > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        return f'File too large. Maximum size is {limit_mb:g}MB.'
    return None


def next_order(photos) -> int:
    """The position for the next photo in a gallery.

    ``Max('order') + 1`` rather than ``count()``: after deleting a middle photo
    the count collides with a position already in use, which reorders the
    gallery on the next read.
    """
    last = photos.aggregate(last=Max('order'))['last']
    return 0 if last is None else last + 1


def attach_photos(
    *,
    photos,
    model,
    owner_field,
    owner,
    uploads,
    user,
    allowed_content_types,
    max_bytes,
    max_per_feature,
):
    """Append uploads to a gallery, returning ``(created, error_response)``.

    Validates the whole batch first so a bad file in the middle does not leave a
    half-saved gallery.
    """
    existing = photos.count()
    remaining = max_per_feature - existing
    if len(uploads) > remaining:
        return None, Response(
            {
                'detail': (
                    f'Maximum {max_per_feature} photos per feature. '
                    f'{existing} already uploaded, {max(remaining, 0)} slot(s) left.'
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    for upload in uploads:
        error = validate_photo(
            upload, allowed_content_types=allowed_content_types, max_bytes=max_bytes
        )
        if error:
            return None, Response(
                {'detail': f'{upload.name}: {error}'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    first = next_order(photos)
    created = []
    for offset, upload in enumerate(uploads):
        created.append(
            model.objects.create(
                **{
                    owner_field: owner,
                    'image': upload,
                    'uploaded_by': user if user and user.is_authenticated else None,
                    'order': first + offset,
                }
            )
        )
    return created, None
