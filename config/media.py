"""Serve uploaded media in production.

Django only mounts ``MEDIA_URL`` for you while ``DEBUG`` is on, and nothing else
in the deployed topology serves it: the host terminates TLS and forwards
straight to gunicorn, and WhiteNoise only answers ``STATIC_URL``. So every
evidence photo URL the API handed out on the host answered **404** even when the
row and the file both existed — the URLconf was the thing saying no, not the
disk.

This is that missing route. It stays a stopgap for local-disk storage: a
container's filesystem is wiped on every redeploy, so the bytes only outlive a
deploy once ``EVIDENCE_S3_*`` points at a bucket (see ``supervision/storage.py``
and INTEGRATION_PLAN.md §6). Serving them correctly is still required either
way.

When the evidence store *is* object storage the view redirects instead of
streaming: the bytes are not on this machine, and the store already knows how to
hand out a public or presigned link.
"""

from django.conf import settings
from django.http import HttpResponseNotAllowed, HttpResponseRedirect
from django.views.static import serve as serve_static

from supervision.storage import get_evidence_storage

# A stored upload never changes: the filename is unique and a re-upload of
# different bytes lands beside the old file rather than over it. So a browser may
# keep it for a day without revalidating — long enough to make a photo gallery
# cheap, short enough that a deleted photo disappears on its own.
MEDIA_CACHE_CONTROL = 'private, max-age=86400'


def serve_media(request, path):
    """``GET /media/<path>`` — the production answer for uploaded evidence.

    Path traversal is refused by Django's own ``safe_join`` inside
    ``django.views.static.serve``; a request for ``..`` gets a 404, not a file.
    """
    if request.method not in ('GET', 'HEAD'):
        return HttpResponseNotAllowed(['GET', 'HEAD'])

    storage = get_evidence_storage()
    if getattr(storage, 'label', '') != 'local':
        return HttpResponseRedirect(storage.url(path))

    response = serve_static(request, path, document_root=settings.MEDIA_ROOT)
    if response.status_code == 200:
        response['Cache-Control'] = MEDIA_CACHE_CONTROL
    return response
