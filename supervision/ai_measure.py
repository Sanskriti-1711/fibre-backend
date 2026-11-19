"""Estimate one element's dimensions from a single site photo.

A photograph has no scale, so it cannot yield millimetres on its own. This
module produces a number only when a known size is visible in the frame (a
600 mm chamber cover, a 110 mm duct, a brick); otherwise it returns the
approved design value or the standard catalogue size and says which it is.

The vision model is never asked for a dimension. It reports what it sees and
the on-image span of the reference and of each dimension, and the arithmetic
here turns those into millimetres — so a hallucinated number cannot reach
``field_measurements``. A reference only scales the dimensions it shares a
plane with: a brick lying on the surface does not scale a depth seen on the
far wall, because the two are at different distances from the camera.

Env (all optional; unset means deterministic values only):
  AI_MEASURE_LLM_URL      — e.g. https://api.openai.com/v1/chat/completions
  AI_MEASURE_LLM_API_KEY  — bearer token (falls back to OPENAI_API_KEY, then
                            GEMINI_API_KEY via Gemini's OpenAI-compatible route)
  AI_MEASURE_LLM_MODEL    — model name (default: gpt-4o-mini, or
                            gemini-3-flash-preview when only a Gemini key is set)
"""

from __future__ import annotations

import base64
import io
import json
import math
import os
from typing import Any

# A scaled value is still an estimate, but it is the only one that saw a scale.
CONFIDENCE_REFERENCE = 0.6
CONFIDENCE_PLAN = 0.35
CONFIDENCE_CATALOGUE = 0.3

# The dimensions worth proposing per element, in the order the form shows them:
# (reading key, label, unit, `estimated_*` field, standard catalogue value).
ELEMENT_SPECS: dict[str, list[tuple[str, str, str, str, float]]] = {
    'trench': [
        ('depth', 'Depth', 'mm', 'estimated_depth_mm', 900.0),
        ('width', 'Width', 'mm', 'estimated_width_mm', 400.0),
    ],
    'duct': [
        ('outer_diameter', 'Outer diameter', 'mm', 'estimated_outer_diameter_mm', 110.0),
        ('installed_depth', 'Installed depth', 'mm', 'estimated_installed_depth_mm', 800.0),
    ],
    'cable': [
        ('outer_diameter', 'Outer diameter', 'mm', 'estimated_outer_diameter_mm', 12.0),
    ],
    'chamber': [
        ('opening', 'Opening', 'mm', 'estimated_chamber_opening_mm', 600.0),
        ('depth', 'Depth', 'mm', 'estimated_chamber_depth_mm', 1200.0),
    ],
    'pole': [
        ('height', 'Height', 'mm', 'estimated_height_mm', 9000.0),
        ('diameter', 'Base diameter', 'mm', 'estimated_diameter_mm', 240.0),
    ],
}

# Where the LLD export puts a trench's design depth/width. The export's casing
# varies by run, so every spelling is tried and a case-insensitive pass follows.
PLAN_KEYS: dict[tuple[str, str], tuple[str, ...]] = {
    ('trench', 'depth'): ('DEPTH_MM', 'depth_mm', 'DEPTH'),
    ('trench', 'width'): ('WIDTH_MM', 'width_mm', 'WIDTH'),
}

# Objects whose real size is known well enough to scale a photo, in millimetres.
KNOWN_REFERENCES = {
    'chamber_cover': 600.0,
    'duct': 110.0,
    'brick': 215.0,
}

# Order matters, and it is not alphabetical: `drop_ducts` contains the cable
# word `drop`, so duct has to be read first. A layer that is genuinely both
# (`cable_trench`) is a cable layer.
ELEMENT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ('chamber', ('chamber', 'manhole', 'vault')),
    ('duct', ('duct', 'conduit')),
    ('cable', ('cable', 'fibre', 'fiber', 'drop')),
    ('pole', ('pole', 'mast', 'tower')),
    ('trench', ('trench', 'excavat', 'open cut')),
]


def element_type_from_name(name: Any) -> str | None:
    """The element a span's layer name implies, or None when it names neither."""
    lowered = str(name or '').lower()
    if not lowered.strip():
        return None
    for element_type, words in ELEMENT_KEYWORDS:
        if any(word in lowered for word in words):
            return element_type
    return None


def _as_float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _prop_number(props: dict, keys: tuple[str, ...]) -> float | None:
    """The design value, matching the case the export happened to use."""
    for key in keys:
        number = _as_float(props.get(key))
        if number:
            return number
    lowered = {str(key).lower(): value for key, value in props.items()}
    for key in keys:
        number = _as_float(lowered.get(key.lower()))
        if number:
            return number
    return None


def _plane(value: Any) -> str:
    return str(value or '').strip().lower().replace(' ', '_')


def _dimension_span(observation: dict, element_type: str, key: str) -> tuple[float | None, str]:
    """One dimension's span and the plane it was seen in.

    Replies from before per-dimension spans carried a single ``target_span_norm``
    for the element's primary dimension.
    """
    dimensions = observation.get('dimensions')
    if isinstance(dimensions, dict):
        dimension = dimensions.get(key) or {}
        return _as_float(dimension.get('span_norm')), _plane(dimension.get('plane'))
    if key == ELEMENT_SPECS[element_type][0][0]:
        return _as_float(observation.get('target_span_norm')), ''
    return None, ''


def _scale(observation: dict | None, element_type: str, key: str) -> tuple[float | None, str]:
    """Millimetres for one dimension from the known object's span, or (None, '')."""
    if not observation:
        return None, ''
    reference = observation.get('reference') or {}
    kind = _plane(reference.get('kind'))
    # A cover cannot scale the chamber it covers: the model would be comparing
    # the object with itself.
    if kind == element_type:
        return None, ''
    known = KNOWN_REFERENCES.get(kind)
    reference_span = _as_float(reference.get('span_norm'))
    target_span, plane = _dimension_span(observation, element_type, key)
    if known is None or not reference_span or not target_span or reference_span <= 0:
        return None, ''
    reference_plane = _plane(reference.get('plane'))
    if reference_plane and plane and reference_plane != plane:
        return None, ''
    # Below this the dimension is too few pixels wide for its span to mean
    # anything, whatever the arithmetic says.
    if target_span * MAX_PHOTO_EDGE < MIN_TARGET_PX:
        return None, ''
    ratio = target_span / reference_span
    # A reference and a dimension further apart than 20x are at different
    # distances from the camera, so the ratio is not a scale.
    if ratio < 0.05 or ratio > 20:
        return None, ''
    return known * ratio, kind


def estimate(
    feature, element_type: str, observation: dict | None = None
) -> tuple[str, list[dict], str]:
    """(basis, rows, note) — the rows are proposals, never saved here."""
    props = getattr(feature, 'properties', None)
    props = props if isinstance(props, dict) else {}

    # The model's own element reading: a trench estimate on a photo it calls a
    # duct would be out by an order of magnitude, so drop the observation
    # rather than trust half of it.
    if observation is not None:
        seen = str(observation.get('element') or '').strip().lower()
        if seen not in ('', 'unknown', element_type):
            observation = None

    basis = 'catalogue'
    planned_rows = 0
    catalogue_rows = 0
    kind = ''
    rows: list[dict] = []
    for key, label, unit, field_key, catalogue in ELEMENT_SPECS[element_type]:
        scaled, scaled_from = _scale(observation, element_type, key)
        if scaled is not None:
            value, confidence = round(scaled, 1), CONFIDENCE_REFERENCE
            basis = 'reference'
            kind = scaled_from
        else:
            planned = _prop_number(props, PLAN_KEYS.get((element_type, key), ()))
            if planned is not None:
                value, confidence = round(planned, 1), CONFIDENCE_PLAN
                planned_rows += 1
                if basis != 'reference':
                    basis = 'plan'
            else:
                value, confidence = catalogue, CONFIDENCE_CATALOGUE
                catalogue_rows += 1
        rows.append(
            {
                'key': field_key,
                'label': label,
                'value': value,
                'unit': unit,
                'confidence': confidence,
            }
        )

    if basis == 'reference':
        note = (
            f'Scaled from the {kind.replace("_", " ")} visible in the photo. '
            'Still an estimate — a tape reading is what counts.'
        )
        if planned_rows or catalogue_rows:
            note += ' The rest are a design or catalogue value, not a measurement.'
    elif planned_rows and catalogue_rows:
        note = (
            'No scale reference in the photo: the approved design value where this span '
            'has one, a standard catalogue size elsewhere.'
        )
    elif planned_rows:
        note = (
            'No scale reference in the photo, so these are the approved design values, '
            'not measurements.'
        )
    else:
        note = (
            'No scale reference and no design value on this span, so these are standard '
            'catalogue sizes.'
        )
    return basis, rows, note


VISION_PROMPT = (
    'You are looking at one construction element on a fibre build. Reply with JSON only, '
    'no prose, in this shape: {"element": "<trench|duct|cable|chamber|pole|unknown>", '
    '"reference": {"kind": "<chamber_cover|duct|brick|unclear>", "plane": '
    '"<mouth|wall|face|unclear>", "span_norm": <0-1>}, "dimensions": {"depth": '
    '{"axis": "<horizontal|vertical|ellipse>", "plane": "<mouth|wall|face>", "span_norm": '
    '<0-1>}}}. Use the dimension names depth, width, length, opening, outer_diameter, '
    'installed_depth, height, diameter, and include only a dimension whose two ends you '
    'can actually see. span_norm is the span as a fraction of the image width, measured '
    'along the named axis. The reference must be in the same plane as the dimensions it '
    'scales: a brick on the surface does not scale a depth seen on the far wall. Never '
    'report millimetres or any measurement.'
)


OPENAI_VISION_URL = 'https://api.openai.com/v1/chat/completions'
OPENAI_VISION_MODEL = 'gpt-4o-mini'

# Gemini speaks the OpenAI chat-completions shape on this route, so a project
# that already holds `GEMINI_API_KEY` for its other AI features gets vision
# without provisioning a second provider.
GEMINI_VISION_URL = (
    'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
)
GEMINI_VISION_MODEL = 'gemini-3-flash-preview'

# Enough for the JSON reply, and a hard stop for a model that starts writing
# prose. Gemini counts its own reasoning against the same budget, so this cap
# truncates its reply mid-object and is not sent there.
MAX_REPLY_TOKENS = 200


def _vision_config() -> tuple[str, str, str] | None:
    """Where to send the photo, or None when no model is configured.

    `AI_MEASURE_LLM_*` is the explicit override. Without it the deployment uses
    whichever provider key it already has: `OPENAI_API_KEY` first because it is
    the widest choice, then `GEMINI_API_KEY`, whose models are reached through
    the OpenAI-compatible route. The provider decides the default URL and model,
    because a Gemini model name sent to OpenAI answers 404 and vice versa. No
    key at all means the estimator still answers from the design and catalogue
    values.
    """
    explicit_key = (os.getenv('AI_MEASURE_LLM_API_KEY') or '').strip()
    openai_key = (os.getenv('OPENAI_API_KEY') or '').strip()
    gemini_key = (os.getenv('GEMINI_API_KEY') or '').strip()
    if explicit_key:
        key, default_url, default_model = (
            explicit_key,
            OPENAI_VISION_URL,
            OPENAI_VISION_MODEL,
        )
    elif openai_key:
        key, default_url, default_model = (
            openai_key,
            OPENAI_VISION_URL,
            OPENAI_VISION_MODEL,
        )
    elif gemini_key:
        key, default_url, default_model = (
            gemini_key,
            GEMINI_VISION_URL,
            GEMINI_VISION_MODEL,
        )
    else:
        return None
    url = (os.getenv('AI_MEASURE_LLM_URL') or '').strip() or default_url
    model = (
        (os.getenv('AI_MEASURE_LLM_MODEL') or '').strip()
        or default_model
    )
    return url, key, model


# A dimension narrower than this in a shrunk photo is too small to measure.
MIN_TARGET_PX = 40
# Uploads are shrunk to this longest edge before they are base64'd to the model.
MAX_PHOTO_EDGE = 1280
PHOTO_JPEG_QUALITY = 80


def _shrink(data: bytes) -> bytes | None:
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(data)).convert('RGB')
        image.thumbnail((MAX_PHOTO_EDGE, MAX_PHOTO_EDGE))
        buffer = io.BytesIO()
        image.save(buffer, format='JPEG', quality=PHOTO_JPEG_QUALITY)
        return buffer.getvalue()
    except Exception:
        return None


def _photo_data_url(photo) -> str | None:
    try:
        photo.image.open('rb')
        data = photo.image.read()
        photo.image.close()
    except Exception:
        return None
    if not data:
        return None
    shrunk = _shrink(data)
    if shrunk is not None:
        return 'data:image/jpeg;base64,' + base64.b64encode(shrunk).decode('ascii')
    name = str(getattr(photo.image, 'name', '') or '').lower()
    mime = 'image/png' if name.endswith('.png') else 'image/jpeg'
    return f'data:{mime};base64,' + base64.b64encode(data).decode('ascii')


def observe(photo, element_type: str) -> dict | None:
    """What the model sees in the frame, or None on any failure.

    Failure includes: not configured, network error, non-2xx, a reply that is
    not JSON. Callers fall back to the design/catalogue values.
    """
    cfg = _vision_config()
    if not cfg or photo is None:
        return None
    data_url = _photo_data_url(photo)
    if data_url is None:
        return None

    import requests

    url, key, model = cfg
    payload = {
        'model': model,
        'messages': [
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'text',
                        'text': f'{VISION_PROMPT} The element is expected to be a {element_type}.',
                    },
                    {'type': 'image_url', 'image_url': {'url': data_url}},
                ],
            }
        ],
        'temperature': 0.0,
    }
    if 'generativelanguage.googleapis.com' not in url:
        payload['max_tokens'] = MAX_REPLY_TOKENS
    else:
        # Gemini 3 reasons before it answers and bills those tokens too, which
        # cost 7.9s here against 1.7s at this setting for the same JSON.
        payload['reasoning_effort'] = 'low'
    try:
        resp = requests.post(
            url,
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            data=json.dumps(payload),
            timeout=25,
        )
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        text = (
            (resp.json().get('choices') or [{}])[0].get('message') or {}
        ).get('content') or ''
    except Exception:
        return None
    return _parse_json(text)


def _parse_json(text: str) -> dict | None:
    cleaned = (text or '').strip()
    if cleaned.startswith('```'):
        rest = cleaned[3:]
        cleaned = rest.split('```')[0] if '```' in rest else rest
        if cleaned.lstrip().lower().startswith('json'):
            cleaned = cleaned.lstrip()[4:]
    start, end = cleaned.find('{'), cleaned.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
