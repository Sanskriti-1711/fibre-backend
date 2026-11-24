"""Read the tape at the clip's reference face: one printed number, millimetres counted.

The clip says *where* the reading is; it is not the ruler. Its width along the
blade is foreshortened by an unknown amount, so nothing measured through it can
be trusted, and this module never uses it for that. What the reading needs is the
blade's own printed ladder: millimetres per pixel come from the numbers and the
tick pitch, and the clip only decides which point on that ladder is reported.

Three stages feed it, each already refusing rather than guessing:

* ``tape_band`` finds the blade and says whether a second working resolution
  agreed about it (``confirmed``).
* ``tape_clip`` finds the clip and returns its two faces.
* ``tape_ticks`` fits the ladder, ``tape_digits`` turns printed numbers into
  millimetres per pixel.

The convention, confirmed with the crew on `20261124_140332`, is that the
reading is taken at the face on the side of the lower printed number. That photo
prints 42, 41, 40, 39 and then the clip, and the reading is 38: the ladder
descends towards the clip, so the face carrying the reading is the one *past*
the clip in the direction the numbers fall. The rule here is therefore geometric
rather than a hard-coded left/right: the reference face is the one the ladder
decreases towards, which is the far face on that photo and the near face on a
blade whose numbers climb the other way.

The millimetres between the adjacent readable number and that face are counted
from the calibration, not interpolated across the clip: a face further than one
printed division from the nearest number it can actually read is refused,
because counting past a whole division means the number it started from is a
guess about which division the clip covers.

A reading is refused outright on a blade ``tape_band`` did not confirm (two
working resolutions disagreeing means the "blade" may be a kerb or a trench
edge), and refused when the ladder or the numbers do not earn a scale. A
confident wrong millimetre value in a measurement record is worse than no value.

Env: none. Tesseract needs no key.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from . import tape_clip, tape_digits, tape_ticks

# The blade is resampled to a strip this wide at most, so a 3840 px original
# costs a bounded warp. Every reported position is scaled back up.
STRIP_MAX_WIDTH = 3000

# The strip's height is the blade's own width, clamped: the ladder and the
# printed numbers both live across the blade, and a 6 px blade cannot carry
# either, while a 400 px one would only make the warp slower.
STRIP_MIN_HEIGHT = 24
STRIP_MAX_HEIGHT = 220

# A tape measure reads between these. Outside them the ladder was misread or the
# scale was set from the wrong units.
MIN_READING_MM = 1.0
MAX_READING_MM = 30000.0

# The face may sit this much beyond one printed division and still be counted.
# A division is what a clip roughly covers, so a face one division from its
# number is the normal case; the slack absorbs box centres and print widths.
DIVISION_TOLERANCE = 0.06


def _refused(reason: str, stage: str, **extra) -> dict:
    """A reading that was not taken, with the stage that stopped it."""
    return {
        'usable': False,
        'reason': reason,
        'stage': stage,
        'reading_mm': None,
        'reading_value': None,
        'unit': None,
        'reference_face': None,
        'other_face': None,
        'reference_side': None,
        'label': None,
        'divisions_to_face': None,
        'mm_per_px': None,
        **extra,
    }


def _bilinear(rgb: np.ndarray, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Sample a colour image at fractional positions, edge-clamped."""
    height, width = rgb.shape[0], rgb.shape[1]
    x = np.clip(xs, 0.0, width - 1.001)
    y = np.clip(ys, 0.0, height - 1.001)
    x0 = np.floor(x).astype(int)
    y0 = np.floor(y).astype(int)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    top = rgb[y0, x0].astype(np.float32) * (1.0 - fx) + rgb[y0, x1] * fx
    bottom = rgb[y1, x0].astype(np.float32) * (1.0 - fx) + rgb[y1, x1] * fx
    return top * (1.0 - fy) + bottom * fy


def _rectify(rgb: np.ndarray, band: dict) -> tuple[Image.Image, float]:
    """The blade as a horizontal strip, and the strip's px per photo px.

    A straight warp is enough because ``tape_band`` already fits the blade's
    centre line and reports the axis it was tracked along; the strip is that
    line made horizontal, wide enough to hold the whole band and tall enough to
    hold its full width.
    """
    centre = np.array(band['centre'], dtype=float)
    axis = np.array(band['axis'], dtype=float)
    across = np.array(band['across'], dtype=float)
    length = max(1.0, float(band['length_px']))
    width = max(1.0, float(band['width_px']))

    scale = min(1.0, STRIP_MAX_WIDTH / length)
    columns = max(64, int(round(length * scale)))
    rows = int(round(min(STRIP_MAX_HEIGHT, max(STRIP_MIN_HEIGHT, width))))

    us = np.linspace(-length / 2.0, length / 2.0, columns)
    vs = np.linspace(-width / 2.0, width / 2.0, rows)
    uu, vv = np.meshgrid(us, vs)
    xs = centre[0] + uu * axis[0] + vv * across[0]
    ys = centre[1] + uu * axis[1] + vv * across[1]
    strip = _bilinear(rgb, xs, ys)
    return Image.fromarray(np.clip(strip, 0, 255).astype(np.uint8)), scale


def _strip_position(face, band: dict, scale: float) -> float:
    """A face's position along the strip, in strip pixels."""
    centre = np.array(band['centre'], dtype=float)
    axis = np.array(band['axis'], dtype=float)
    length = float(band['length_px'])
    along = float((np.array(face, dtype=float) - centre) @ axis)
    return (along + length / 2.0) * scale


def _ladder_line(anchors: list[dict]) -> tuple[float, float] | None:
    """(labels per strip px, intercept) of the printed ladder."""
    xs = np.array([anchor['x_px'] for anchor in anchors], dtype=float)
    values = np.array([anchor['value'] for anchor in anchors], dtype=float)
    if xs.size < 2 or float(xs.max() - xs.min()) <= 0.0:
        return None
    slope, intercept = np.polyfit(xs, values, 1)
    if not np.isfinite(slope) or slope == 0.0:
        return None
    return float(slope), float(intercept)


def _nearest_label(anchors: list[dict], position: float) -> dict:
    """The readable number closest to the clip: the one the count starts from."""
    return min(anchors, key=lambda anchor: abs(anchor['x_px'] - position))


def read(
    rgb: np.ndarray, clip: dict | None = None, ocr=None
) -> dict:
    """The tape reading at the clip's reference face, or a refusal.

    ``clip`` is a result from ``tape_clip.find_clip``; passing it in lets a
    caller that already ran the detector (the measurement view does) avoid a
    second full detection. ``ocr(cell, mark)`` overrides tesseract, which is how
    the ladder logic is checked without an OCR install.
    """
    if rgb is None or getattr(rgb, 'ndim', 3) != 3:
        return _refused('the photo is not a colour image', 'photo')

    if clip is None:
        clip = tape_clip.find_clip(rgb)
    if not clip.get('found'):
        return _refused(
            clip.get('reason') or 'no clip was found, so there is no reading to take',
            'clip',
            clip=clip,
        )

    # Policy, decided with the crew 2026-11-24: a reading is only taken on a
    # blade two working resolutions agree about. The locator can return a
    # confident-looking band that is not the tape (`20261124_140332` returns a
    # band at -33 degrees where the tape runs at +12), and a millimetre value
    # measured on that would be a number taken off a trench edge.
    if not clip.get('blade_confirmed'):
        return _refused(
            'the blade was not confirmed by a second working resolution, so no '
            'reading is taken from it',
            'blade',
            clip=clip,
        )

    band = clip.get('band') or {}
    if not band.get('centre') or not band.get('length_px'):
        return _refused('the clip came without a blade to read along', 'blade', clip=clip)

    faces = clip.get('faces') or []
    if len(faces) != 2:
        return _refused('the clip did not report the two faces of its reading', 'clip', clip=clip)

    strip, scale = _rectify(rgb, band)
    lattice = tape_ticks.read_strip(strip)
    if not lattice:
        return _refused('the blade carries no readable mark ladder', 'marks', clip=clip)
    if not lattice.get('usable'):
        return _refused(
            'the marks on the blade are not even enough to measure from', 'marks',
            clip=clip,
        )

    numbers = tape_digits.read_labels(strip, lattice, ocr=ocr)
    if not numbers or not numbers.get('usable'):
        return _refused(
            (numbers or {}).get('reason') or 'the printed numbers did not set a scale',
            'numbers',
            clip=clip,
            numbers=numbers,
        )

    anchors = numbers.get('anchors') or []
    line = _ladder_line(anchors)
    if line is None:
        return _refused('the printed numbers do not step along the blade', 'numbers', clip=clip)
    slope, intercept = line

    positions = [_strip_position(face, band, scale) for face in faces]
    values = [intercept + slope * position for position in positions]

    # The face on the side of the lower printed number: the direction the
    # ladder falls. On `20261124_140332` (42, 41, 40, 39, clip) that is the far
    # face, where the next division down -- 38 -- is the reading.
    reference = 1 if slope < 0.0 else 0
    other = 1 - reference
    reference_value = values[reference]

    middle = 0.5 * (positions[0] + positions[1])
    label = _nearest_label(anchors, middle)
    stepping = np.abs(np.diff(sorted(anchor['value'] for anchor in anchors)))
    division = float(np.median(stepping)) if stepping.size else 0.0
    if division <= 0.0:
        return _refused('the printed numbers do not repeat at one division', 'numbers', clip=clip)

    counted = abs(reference_value - float(label['value'])) / division
    if counted > 1.0 + DIVISION_TOLERANCE:
        return _refused(
            'the clip is more than one printed division from the nearest readable '
            'number, so the count to its face would be a guess',
            'reading',
            clip=clip,
            label=label,
            divisions_to_face=round(float(counted), 2),
        )

    unit_mm = 10.0 if numbers.get('unit') == 'cm' else 1.0
    reading_mm = reference_value * unit_mm
    if not np.isfinite(reading_mm) or not (MIN_READING_MM <= reading_mm <= MAX_READING_MM):
        return _refused(
            'the reading that face implies is outside what a tape measures', 'reading',
            clip=clip, label=label,
        )

    return {
        'usable': True,
        'reason': None,
        'stage': None,
        'reading_mm': round(float(reading_mm), 1),
        'reading_value': round(float(reference_value), 2),
        'unit': numbers.get('unit'),
        'reference_face': [round(float(face), 1) for face in faces[reference]],
        'other_face': [round(float(face), 1) for face in faces[other]],
        'reference_side': 'lower printed numbers',
        'label': {
            'value': int(label['value']),
            'text': label['text'],
            'x_px': round(float(label['x_px']), 1),
        },
        'divisions_to_face': round(float(counted), 3),
        'division_units': round(division, 3),
        'mm_per_px': numbers.get('mm_per_px'),
        'mm_per_tick': numbers.get('mm_per_tick'),
        'blade_confirmed': True,
        'tape_angle_deg': clip.get('tape_angle_deg'),
        'clip': clip,
        'numbers': numbers,
    }


def read_bytes(data: bytes, ocr=None) -> dict:
    """The reading in an uploaded photo, with the EXIF rotation applied."""
    import io

    from PIL import ImageOps

    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    except Exception:
        return _refused('the upload could not be read as a photo', 'photo')
    return read(np.asarray(image), ocr=ocr)
