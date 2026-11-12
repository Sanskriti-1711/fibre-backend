"""Refuse an oversized upload before Django writes it to disk.

The per-photo cap (``EVIDENCE_MAX_BYTES``) is checked inside the view, against
``upload.size`` — by then the multipart body has already been parsed and every
part over 2.5 MB has been written to a temp file. One authenticated client
posting a multi-gigabyte body therefore fills the host's disk before any view
runs, which on a small container takes the API (and the liveness probe that
shares it) down.

``CONTENT_LENGTH`` is known before the body is read, so the check belongs here.
It is limited to the photo endpoints on purpose: a GeoPackage import is
legitimately large, and an evidence batch is the only body whose size the
application can bound in advance.
"""

import json
import re

from django.conf import settings
from django.http import HttpResponse

# The gallery endpoints: `/api/features/<id>/photos/`, the legacy
# `upload-photo/` route and the survey equivalents.
_PHOTO_PATH = re.compile(r'/photos/?$|/upload-photo/?$')


def _limit_for(path: str) -> int | None:
    if _PHOTO_PATH.search(path):
        return getattr(settings, 'MAX_PHOTO_UPLOAD_BYTES', None)
    return None


class RequestBodyLimitMiddleware:
    """Answer ``413`` for a photo upload whose declared body is too large.

    ``CONTENT_LENGTH`` is a client-supplied header, so this is a fast refusal,
    not a security boundary: a client that omits it still reaches the per-file
    check in the view. What it buys is that the ordinary oversized request never
    lands in ``/tmp`` at all.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        limit = _limit_for(request.path_info)
        if limit:
            try:
                declared = int(request.META.get('CONTENT_LENGTH') or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > limit:
                limit_mb = limit / (1024 * 1024)
                return HttpResponse(
                    json.dumps(
                        {
                            'detail': (
                                f'Upload too large. A photo batch may not exceed '
                                f'{limit_mb:g}MB.'
                            )
                        }
                    ),
                    status=413,
                    content_type='application/json',
                )
        return self.get_response(request)
