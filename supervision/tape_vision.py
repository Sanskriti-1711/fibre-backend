"""Read a tape with a vision model when the local OCR cannot.

The local reader (``supervision.tape_measure``) is free, but a real field photo
is not a clean scan: the blade is dirty, half in shadow, at an angle, and the
printed numbers arrive a dozen pixels tall after WhatsApp's compression. On the
six photos from the Madipakkam survey tesseract did not read a single blade
correctly, and its crops of the same blades read ``"6789 2.13.14.1 2 6 4"``
where the blade says 2 … 14 cm. A vision model reads the printed numbers in
that same crop.

This module is the middle engine, used only after the free local read came up
short and before the Cloud Vision text pass:

    tesseract  →  vision model  →  Cloud Vision TEXT_DETECTION

The numbers a model returns travel the same rails as OCR tokens — the run
logic, the pitch and endpoint guard, and ``measure`` — so a model reading
cannot mean something different from an OCR reading. A reply with fewer than
three numbers is refused outright: the two-number pair is exactly the shape
that turned a NoteCam date stamp into a 2 m trench depth, and a model that can
read a blade at all reads three numbers on it.

Nothing is written here. The endpoint hands the reading to the app and the
engineer confirms it.

Env: the provider resolution is ``ai_measure``'s — ``AI_MEASURE_LLM_API_KEY``,
then ``OPENAI_API_KEY``, then ``GEMINI_API_KEY`` — but the reader only runs on a
host that also sets ``TAPE_VISION=1``.

It is off by default because the span it reports is the span of the blade, not
the span of the object. On the six Madipakkam field photos (2026-11-22) the
model transcribed the blade's printed ladder rather than the numbers visible in
the frame: 10, 20 … 60 cm on a blade whose photographed run is 11 … 26 cm, and
20 … 50 cm on one whose run is 10 … 21 cm. Those spans measure the whole blade
in shot, so wiring them into the form would fill a confident wrong depth — the
failure this feature exists to prevent. With the anti-extrapolation prompt it
mostly refuses instead (<3 or >6 numbers, or off-pitch), which is safe but not
yet useful. Until the blade's own ends are located in the frame, this stays an
experiment an engineer or a developer can switch on.
"""

from __future__ import annotations

import base64
import io
import os
import time

from . import ai_measure, tape_measure

# A whole frame first, then at most this many blade crops. Each one is a paid
# call, and the endpoint has a user waiting on it.
MAX_CROPS = 3
# Stop asking once the photo has cost this long; a slow model must not hold the
# tape endpoint open behind it.
TIME_BUDGET_S = 25.0
REQUEST_TIMEOUT_S = 20
# The model needs the digits large; a 40 px band upscaled is still a smear.
MIN_CROP_HEIGHT = 60
CROP_MAX_EDGE = 1600
CROP_UPSCALE_WIDTH = 1200
JPEG_QUALITY = 88
MIN_NUMBERS = 3
# A field crop shows a handful of large printed numbers. A reply with more is
# the model completing the blade's sequence — 0…30 on a metre tape, 0…100 on a
# 25 cm rule — instead of transcribing what is actually printed, so it is
# refused rather than measured. On the Madipakkam photos every recitation came
# back as 6 to 48 numbers.
MAX_NUMBERS = 6
MODEL_CONFIDENCE = 0.9

PROMPT = (
    'This photo shows a tape measure (a scale printed on a metal or plastic '
    'blade) lying along the thing being measured. Transcribe only the large '
    'printed numbers whose digits you can actually see on the blade, with where '
    'each one sits in the image as a fraction of the image width and height '
    '(0 = left/top, 1 = right/bottom). Rules: do not continue, complete or '
    'extrapolate the sequence; do not count the small graduation lines; never '
    'supply a number you cannot see, even if it obviously belongs between two '
    'visible ones — a blade that shows 20 and 22 but not 21 returns just those '
    'two. Ignore timestamps, camera watermarks, street signs and any other '
    'text. If you cannot actually see printed digits on the blade, return an '
    'empty list. Reply with JSON only, no prose: {"numbers": [{"value": 120, '
    '"x": 0.18, "y": 0.55}, …], "unit": "cm" or "mm"}'
)


def configured() -> bool:
    """Whether the vision reader is switched on here and a model is available.

    Off unless ``TAPE_VISION`` says otherwise: its span is the blade's, not the
    object's, so it is not yet a measurement source (see the module docstring).
    """
    if (os.getenv('TAPE_VISION') or '').strip().lower() not in ('1', 'true', 'yes'):
        return False
    return ai_measure._vision_config() is not None


def _prepare(data: bytes):
    """The uploaded photo as an upright RGB image, or None."""
    try:
        from PIL import Image, ImageOps

        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    except Exception:
        return None
    image.thumbnail((tape_measure.TAPE_MAX_EDGE, tape_measure.TAPE_MAX_EDGE))
    return image


def _crops(image):
    """The frame first, then the bright bands the local reader trusts as blade.

    The bands are the same ones ``tape_measure`` crops from, so the model is
    asked about the strip that actually looks like a blade before it is asked
    about the whole frame a third time.
    """
    yield image
    try:
        bands = tape_measure._tape_bands(image)
    except Exception:
        return
    for band in bands:
        crop = band.get('crop')
        if crop is None or crop.height < MIN_CROP_HEIGHT:
            continue
        if crop.height >= image.height * 0.8:
            # That is the frame; it was already sent.
            continue
        yield crop


def _data_url(crop) -> str | None:
    """The crop as a JPEG data URL, enlarged enough for the digits to be clear."""
    try:
        from PIL import Image

        image = crop.copy()
        if image.width < CROP_UPSCALE_WIDTH:
            scale = min(3.0, CROP_UPSCALE_WIDTH / max(1, image.width))
            image = image.resize(
                (int(image.width * scale), int(image.height * scale)),
                Image.LANCZOS,
            )
        image.thumbnail((CROP_MAX_EDGE, CROP_MAX_EDGE))
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=JPEG_QUALITY)
    except Exception:
        return None
    return 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def _ask(url: str, key: str, model: str, data_url: str) -> dict | None:
    """One chat-completions call with the crop, or None on any failure."""
    import json

    import requests

    payload = {
        'model': model,
        'messages': [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': PROMPT},
                    {'type': 'image_url', 'image_url': {'url': data_url}},
                ],
            }
        ],
        'temperature': 0.0,
    }
    if 'generativelanguage.googleapis.com' not in url:
        payload['max_tokens'] = ai_measure.MAX_REPLY_TOKENS
    else:
        # Gemini 3 reasons before answering and bills those tokens; this keeps
        # the reply quick on a host with a user waiting.
        payload['reasoning_effort'] = 'low'
    try:
        response = requests.post(
            url,
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            data=json.dumps(payload),
            timeout=REQUEST_TIMEOUT_S,
        )
        if response.status_code < 200 or response.status_code >= 300:
            return None
        text = (
            (response.json().get('choices') or [{}])[0].get('message') or {}
        ).get('content') or ''
    except Exception:
        return None
    return ai_measure._parse_json(text)


def _tokens(reply: dict) -> list[dict]:
    """The model's numbers as the token shape the run logic expects.

    The model reports where each number sits in the image, so the run logic and
    the pitch guard see its real positions: a recitation spread evenly from 0
    to 1 is what a made-up sequence looks like, while a blade seen at an angle
    puts its numbers at the places they were printed.
    """
    tokens: list[dict] = []
    for raw in reply.get('numbers') or []:
        if isinstance(raw, dict):
            value, x, y = raw.get('value'), raw.get('x'), raw.get('y')
        else:
            value, x, y = raw, None, None
        try:
            number = int(round(float(str(value).replace(',', '').strip())))
        except (TypeError, ValueError):
            continue
        if not (0 <= number <= tape_measure.MAX_READING_MM):
            continue
        try:
            x = float(x)
            y = float(y) if y is not None else 0.5
        except (TypeError, ValueError):
            x, y = None, 0.5
        if x is None or not (0.0 <= x <= 1.0):
            # No usable position: place it after the previous number so the
            # order the model gave is still what the run logic sees.
            x = (len(tokens) + 0.5) / max(1, MAX_NUMBERS)
        tokens.append(
            {
                'value': number,
                'x': x,
                'y': min(1.0, max(0.0, y)),
                'confidence': MODEL_CONFIDENCE,
            }
        )
    return tokens


def read_tape_bytes(data: bytes) -> dict | None:
    """The tape's readings as a model sees them, or None when it sees no tape."""
    config = ai_measure._vision_config()
    if config is None or not data:
        return None
    image = _prepare(data)
    if image is None:
        return None
    url, key, model = config
    started = time.monotonic()
    for index, crop in enumerate(_crops(image)):
        if index >= MAX_CROPS or time.monotonic() - started > TIME_BUDGET_S:
            break
        data_url = _data_url(crop)
        if data_url is None:
            continue
        reply = _ask(url, key, model, data_url)
        if not reply:
            continue
        tokens = _tokens(reply)
        if len(tokens) < MIN_NUMBERS or len(tokens) > MAX_NUMBERS:
            continue
        run = tape_measure._reading_run(
            tape_measure._drop_outliers(tape_measure._cluster_tokens(tokens))
        )
        # The same guard the OCR reading has to pass: a span that is not
        # printed on the blade is not a measurement, whichever engine saw it.
        if len(run) < MIN_NUMBERS or not tape_measure._run_is_usable(run):
            continue
        unit = tape_measure._infer_unit([token['value'] for token in run])
        return {
            'engine': 'vision-model',
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
    return None


def read_tape(photo) -> dict | None:
    """The readings of the tape stored in ``photo`` from the vision model."""
    if photo is None or not configured():
        return None
    try:
        photo.image.open('rb')
        data = photo.image.read()
        photo.image.close()
    except Exception:
        return None
    if not data:
        return None
    return read_tape_bytes(data)
