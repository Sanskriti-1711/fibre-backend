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

Locally stored bytes are served only behind a signature. Django's ``serve``
answers anyone who knows the path, and the path outlives the engineer: a photo
URL pasted into a chat, a ticket or a browser history stayed readable forever,
including after the uploader's account was removed. ``media_url()`` signs the
path for the API's own responses, and ``serve_media`` refuses an unsigned or
expired one.

When the evidence store *is* object storage the view redirects instead of
streaming: the bytes are not on this machine, and the store already knows how to
hand out a public or presigned link.
"""

from urllib.parse import quote

from django.conf import settings
from django.core import signing
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, HttpResponseRedirect
from django.views.static import serve as serve_static

from supervision.storage import get_evidence_storage

# A stored upload never changes: the filename is unique and a re-upload of
# different bytes lands beside the old file rather than over it.
MEDIA_CACHE_CONTROL = 'private, max-age=86400'

# The signature is what makes the URL a capability: it names the exact path and
# expires, so a link that leaks stops working on its own.
MEDIA_SIGNATURE_SALT = 'media-access'
MEDIA_SIGNATURE_MAX_AGE = 60 * 60 * 24


def signed_media_url(path: str) -> str:
    """``/media/<path>?sig=<timestamped signature>`` for a stored path."""
    signature = signing.TimestampSigner(salt=MEDIA_SIGNATURE_SALT).sign(path)
    return f'{settings.MEDIA_URL}{path}?sig={quote(signature)}'


def media_url(image) -> str:
    """The URL a serializer should hand out for an ``ImageField``.

    Object storage already has its own link (public base URL or presigned) and
    is used as-is; local disk is served by this app, so it is signed here.
    """
    if not image or not image.name:
        return ''
    if getattr(image.storage, 'label', '') != 'local':
        return image.storage.url(image.name)
    return signed_media_url(image.name)


def _signature_is_valid(path: str, signature: str) -> bool:
    if not signature:
        return False
    try:
        signed_path = signing.TimestampSigner(salt=MEDIA_SIGNATURE_SALT).unsign(
            signature, max_age=MEDIA_SIGNATURE_MAX_AGE
        )
    except signing.BadSignature:
        return False
    return signed_path == path


def serve_media(request, path):
    """``GET /media/<path>?sig=...`` — the production answer for uploaded evidence.

    Path traversal is refused by Django's own ``safe_join`` inside
    ``django.views.static.serve`` once the signature matches; a request for
    ``..`` never gets that far, because the signature names the path it signed.
    """
    if request.method not in ('GET', 'HEAD'):
        return HttpResponseNotAllowed(['GET', 'HEAD'])

    if not _signature_is_valid(path, request.GET.get('sig', '')):
        return HttpResponseForbidden('Missing or expired media link.')

    storage = get_evidence_storage()
    if getattr(storage, 'label', '') != 'local':
        return HttpResponseRedirect(storage.url(path))

    response = serve_static(request, path, document_root=settings.MEDIA_ROOT)
    if response.status_code == 200:
        response['Cache-Control'] = MEDIA_CACHE_CONTROL
    return response
