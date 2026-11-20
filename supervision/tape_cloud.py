"""Read a tape through Google Cloud Vision when the local engine cannot.

The local reader (``supervision.tape_measure``) is free and runs on our own
host, but a field photo — glare, dust, a blade at 20 degrees — is exactly what
its OCR struggles with. This is the fallback: one synchronous Cloud Vision
``TEXT_DETECTION`` call per photo, used only after the local read found fewer
than two numbers, so the volume stays inside the 1,000 free units a month.

The numbers are ordered by the same run logic the local reader uses, so a
reading means the same thing whichever engine produced it. Nothing is written
here; the endpoint hands the reading to the app and the engineer confirms it.

Env: ``GOOGLE_VISION_API_KEY``. Without it this module is inert and the endpoint
answers exactly as it did before.
"""

from __future__ import annotations

import base64
import io

import requests

from . import tape_measure

VISION_URL = 'https://vision.googleapis.com/v1/images:annotate'
TIMEOUT_SECONDS = 20


def api_key() -> str:
    """The configured Cloud Vision key, or an empty string."""
    from django.conf import settings

    return str(getattr(settings, 'GOOGLE_VISION_API_KEY', '') or '').strip()


def configured() -> bool:
    return bool(api_key())


def _tokens(words: list[dict], width: int, height: int) -> list[dict]:
    """Word boxes from Vision as the token shape the run logic expects.

    Vision returns a box but no confidence, so every word is given the same
    mid-high confidence and the tape grammar (a regular, increasing run) is what
    decides whether the digits are a tape.
    """
    tokens: list[dict] = []
    for word in words:
        text = tape_measure._digits(word.get('description'))
        if not text:
            continue
        vertices = (word.get('boundingPoly') or {}).get('vertices') or []
        if not vertices:
            continue
        xs = [float(point.get('x', 0)) for point in vertices]
        ys = [float(point.get('y', 0)) for point in vertices]
        if not xs or not ys:
            continue
        groups = tape_measure._split_merged(text) or [int(text)]
        span = max(xs) - min(xs)
        for position, value in enumerate(groups):
            if not (0 <= value <= tape_measure.MAX_READING_MM):
                continue
            tokens.append(
                {
                    'value': value,
                    'x': (min(xs) + span * (position + 0.5) / len(groups)) / max(1, width),
                    'y': ((min(ys) + max(ys)) / 2) / max(1, height),
                    'confidence': 0.9,
                }
            )
    return tokens


def read_tape_bytes(data: bytes) -> dict | None:
    """The tape's readings from Cloud Vision, or None when nothing usable came back."""
    key = api_key()
    if not key or not data:
        return None
    try:
        from PIL import Image, ImageOps

        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('L')
        image.thumbnail((tape_measure.TAPE_MAX_EDGE, tape_measure.TAPE_MAX_EDGE))
    except Exception:
        return None

    # Send the same shrunken copy the local reader works on: it is a fraction of
    # the upload and the digits are still tens of pixels tall.
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=90)
    payload = {
        'requests': [
            {
                'image': {'content': base64.b64encode(buffer.getvalue()).decode()},
                'features': [{'type': 'TEXT_DETECTION'}],
                'imageContext': {'languageHints': ['en']},
            }
        ]
    }
    try:
        response = requests.post(
            VISION_URL,
            params={'key': key},
            json=payload,
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except Exception:
        return None

    annotations = (body.get('responses') or [{}])[0].get('textAnnotations') or []
    # The first annotation is the whole text; the rest are the words, and their
    # boxes are what the run ordering needs.
    tokens = _tokens(annotations[1:], image.width, image.height)
    run = tape_measure._reading_run(
        tape_measure._drop_outliers(tape_measure._cluster_tokens(tokens))
    )
    if len(run) < 2:
        return None

    unit = tape_measure._infer_unit([token['value'] for token in run])
    return {
        'engine': 'google-vision',
        'unit': unit,
        'agreement': 1,
        'bounds': None,
        'readings': [
            {
                'value': token['value'],
                'unit': unit,
                'x': round(token['x'], 4),
                'confidence': round(token['confidence'], 2),
            }
            for token in run
        ],
    }


def read_tape(photo) -> dict | None:
    """The readings of the tape stored in ``photo`` from Cloud Vision, or None."""
    if photo is None or not configured():
        return None
    try:
        photo.image.open('rb')
        data = photo.image.read()
        photo.image.close()
    except Exception:
        return None
    return read_tape_bytes(data)
