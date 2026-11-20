"""The one implementation of the evidence-photo rules.

``supervision.models.FeaturePhoto`` and ``survey.models.SurveyFeaturePhoto`` hold
the same fields and enforce the same limits — content hash, type allow-list,
per-file cap, per-feature cap and ordering — and had been copied into each app.
That is how the two ended up numbering photos differently: one used
``Max('order') + 1``, the other ``count()``, so after a middle photo was deleted
they disagreed about the next position. Both apps call this module now.
"""

import hashlib
import io

import numpy as np
from django.db.models import Max
from rest_framework import status
from rest_framework.response import Response

# A photo whose long edge is under this cannot show a tape's digits or a duct
# joint when it is reviewed later.
MIN_LONG_EDGE = 800

# Mean gray outside this is dusk inside a chamber or a blown-out highlight:
# the photo proves nothing about the work.
MIN_MEAN_GRAY = 40
MAX_MEAN_GRAY = 230

# Variance of the Laplacian from 1600 px, matching
# supervision.tape_measure.SHARPNESS_MIN so an upload and a tape read agree on
# what "out of focus" means.
MIN_SHARPNESS = 12.0
MAX_QUALITY_EDGE = 1600


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


def photo_quality(upload) -> str | None:
    """Return an error string for an image too poor to be evidence, or None.

    Only whether the photo can be read at all is judged here. Framing, glare
    over a blade and whether the right two marks are in shot belong to the
    subject, and the tape reader already returns a retake reason for them.

    shortcut: mean gray stands in for exposure — a frame that is half black and
    half blown white still averages mid-gray; add a clipping check if the field
    crew reports it.
    """
    try:
        upload.seek(0)
        payload = upload.read()
    finally:
        # The upload is hashed after this check, so its stream must stay open and
        # rewound: PIL closing the file it was handed is what broke the digest.
        upload.seek(0)

    try:
        from PIL import Image

        image = Image.open(io.BytesIO(payload))
        image.load()
        width, height = image.size
    except Exception:
        return 'That file could not be read as an image.'

    try:
        if max(width, height) < MIN_LONG_EDGE:
            return (
                f'The photo is {width}x{height} px, too small to review. Take it '
                f'again closer, or set the camera to at least {MIN_LONG_EDGE} px '
                'on the long edge.'
            )

        # Measured at the same scale the tape reader uses, and small enough that
        # a 12 MP photo does not become a 400 MB float array.
        image.thumbnail((MAX_QUALITY_EDGE, MAX_QUALITY_EDGE))
        gray = np.asarray(image.convert('L'), dtype=np.float32)
        mean = float(gray.mean())
        if mean < MIN_MEAN_GRAY:
            return 'The photo is almost black. Add light or use the flash and try again.'
        if mean > MAX_MEAN_GRAY:
            return (
                'The photo is washed out by light. Take it again with the sun '
                'behind you.'
            )
        if gray.shape[0] > 2 and gray.shape[1] > 2:
            laplacian = (
                gray[:-2, 1:-1]
                + gray[2:, 1:-1]
                + gray[1:-1, :-2]
                + gray[1:-1, 2:]
            ) - 4.0 * gray[1:-1, 1:-1]
            if float(laplacian.var()) < MIN_SHARPNESS:
                return (
                    'The photo is out of focus. Hold still, tap the subject on '
                    'the screen, then take it again.'
                )
        return None
    finally:
        image.close()


def validate_photo(upload, *, allowed_content_types, max_bytes):
    """Return an error string for an unacceptable upload, or None."""
    if upload.content_type not in allowed_content_types:
        return 'Invalid file type. Only JPEG and PNG are allowed.'
    if not upload.size:
        return 'The file is empty.'
    if upload.size > max_bytes:
        limit_mb = max_bytes / (1024 * 1024)
        return f'File too large. Maximum size is {limit_mb:g}MB.'
    return photo_quality(upload)


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
