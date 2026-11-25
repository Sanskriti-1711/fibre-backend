"""Read the tape at the clip's reference face: one printed number, millimetres counted.

The clip says *where* the reading is; it is not the ruler. Its width along the
blade is foreshortened by an unknown amount, so nothing measured through it can
be trusted, and this module never uses it for that. What the reading needs is the
blade's own printed ladder: millimetres per pixel come from the numbers and the
tick pitch, and the clip only decides which point on that ladder is reported.

Three stages feed it, each already refusing rather than guessing:

* ``tape_band`` finds the blade and says whether a second working resolution
  agreed about it (``confirmed``). When it refuses the blade on shape alone it
  still hands back the geometry the walk established, so a blade the frame cut
  short can be read rather than dismissed before OCR.
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

Policy, changed with the crew on 2026-11-25: the printed numbers are the final
authority. A blade carrying numbers that step along it earns a millimetre
reading even when the locator could not confirm the blade, the marks are too
faint to check the scale, or the clip sits more than one division from the
nearest number. Those come back with ``confidence='low'`` and a
``confidence_note`` naming what is weak, so the field sees the number and knows
what it is worth. A refusal is kept only for a photo with nothing to read: no
blade, no clip, or numbers that set no scale at all.

Env: none. Tesseract needs no key.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from . import tape_band, tape_clip, tape_digits, tape_ticks

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
        'confidence': None,
        'low_confidence': False,
        'confidence_note': None,
        **extra,
    }


def _ladder_refusal(problem: str) -> str:
    """Why a blade's ladder cannot be measured from.

    "No tape found" is only said when the evidence says there is no ladder: both
    genuine field tapes *do* have one, and their marks simply wander. Saying the
    tape is missing on a photo of a tape would send the engineer to re-aim at a
    tape they had already framed.
    """
    if problem == 'uneven':
        return 'the marks on this blade are not even and regular enough to set a scale'
    return 'no tape found: this blade carries no tick ladder'


def _ladder_refusal_stage(problem: str) -> str:
    """The stage that stopped a ladder: the blade if there is none, else the marks."""
    return 'marks' if problem == 'uneven' else 'blade'


def _ladder_evidence(lattice: dict | None) -> bool:
    """Whether a fit is printed marks, whatever its marks do about one pitch.

    This is the second opinion a blade can be confirmed with. The two-resolution
    policy exists because the locator can return a confident-looking band that is
    not the tape; a ladder of ``MIN_LADDER_TICKS`` marks printed at
    ``MIN_LADDER_SCORE`` periodicity is not something ground texture produces,
    and it is checked on the blade's own strip rather than on a resampled copy.

    Deliberately *not* a residual test: a tape lying over a trench sags, and its
    marks then scatter around a pitch instead of sitting on it. The millimetres
    come from the printed numbers, so a ladder only has to be a ladder here.
    """
    if not lattice:
        return False
    return int(lattice.get('count') or 0) >= tape_ticks.MIN_LADDER_TICKS and float(
        lattice.get('score') or 0.0
    ) >= tape_ticks.MIN_LADDER_SCORE


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

    A straight warp, because ``tape_band`` already fits the blade's centre line
    and reports the axis it was tracked along; the strip is that line made
    horizontal, wide enough to hold the whole band and tall enough to hold its
    full width.

    A bend-aware warp that followed the walk's per-column centres was built and
    reverted: those centres are not a curve. On the field photos they span up to
    1.9x the blade width, because the walk jumps between bright regions, and
    following them doubled and tripled the ladder's pitch (140332: 5.6 -> 27.1 px)
    while raising the residual it was meant to lower (141314: 0.24 -> 0.33). It
    also warped the caption enough that its letters passed the periodicity floor
    and were reported as a tape. See `tmp/bend_check.py`.
    """
    centre = np.array(band['centre'], dtype=float)
    axis = np.array(band['axis'], dtype=float)
    across = np.array(band['across'], dtype=float)
    length = max(1.0, float(band['length_px']))
    width = max(1.0, float(band['width_px']))

    columns = max(120, int(round(length * min(1.0, STRIP_MAX_WIDTH / length))))
    # The strip is measured in its own pixels, so the scale is its real
    # resampling factor rather than the shrink alone: a blade the frame edge cut
    # short is resampled *up* to the floor instead of refused for being short.
    # The ladder needs a strip wide enough to fit in, which is a resolution
    # answer, not a reading answer.
    scale = columns / length
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

    strip = None
    scale = 1.0
    lattice = None
    blade_partial = False
    if clip is None:
        # The tape is decided before the clip is looked for. A photo of a
        # caption, or of a length of metal, has no clip on it either, so "no
        # clip found" sends the engineer looking for the wrong thing and costs a
        # clip detection to say it. "No tape found" is the honest answer, and it
        # arrives on the first stage instead of the second.
        # At the resolution the clip detector uses: the locator's answer, and
        # whether a second resolution agreed with it, depends on the scale it
        # works at, and the clip is looked for on the same frame either way.
        blade = tape_band.find_band(rgb, work_edge=tape_clip.WORK_EDGE)
        if not blade.get('found'):
            # Policy, changed with the crew 2026-11-25: the printed numbers are
            # the final authority, so a blade the locator rejects on *shape* is
            # still read. Span TR-000010 (2026-11-25, field photo) answered "the
            # bright band is too short to be a tape" because the blade ran out
            # of the top of the frame -- before OCR ran. The screen then said
            # "no tape numbers could be read" about a photo whose numbers were
            # never looked at. The geometry the walk did establish is rectified
            # and read here; the numbers still have to set a scale, and the
            # reading is marked low-confidence.
            candidate = blade.get('candidate')
            if not candidate:
                return _refused(
                    'no tape found: %s' % (blade.get('reason') or 'no blade was found'),
                    'blade',
                    blade=blade,
                )
            blade_partial = True
            blade = {
                'found': True,
                'reason': None,
                'confirmed': False,
                'confirmed_by': None,
                'rejected': blade.get('reason'),
                **candidate,
                'band': candidate,
            }
        # A band the locator believes in is still not a tape until it carries a
        # ladder: a caption has letters where the marks should be, and a pipe has
        # nothing at all. This is checked before the clip so the answer arrives
        # without a clip detection, and reported on the marks stage because the
        # blade itself was found.
        strip, scale = _rectify(rgb, blade['band'])
        lattice = tape_ticks.read_strip(strip)
        problem = tape_ticks.ladder_problem(lattice)

        # Policy, decided with the crew 2026-11-24: a reading is only taken on a
        # blade two working resolutions agree about, *or* on one whose strip
        # carries a printed ladder. The locator can return a confident-looking
        # band that is not the tape (`20261124_140332` returns a band at -33
        # degrees where the tape runs at +12), and a millimetre value measured on
        # that would be a number taken off a trench edge -- but a band that
        # prints 24+ marks at 0.70 periodicity is not a trench edge either. That
        # second opinion is what `20261124_141314` has: a genuine tape whose
        # locator only ever reports `confirmed=None`.
        # Policy, changed with the crew 2026-11-25: an unconfirmed blade is not
        # refused here any more. The printed numbers are the final authority, so
        # the run goes on to the clip and the numbers, and a reading taken on a
        # weak or unconfirmed blade is marked low-confidence instead. Refused
        # here only when the strip has no ladder at all: a caption or a smooth
        # bar repeats nothing, and sending the engineer to find a clip on one
        # would be the wrong instruction.
        if not blade.get('confirmed') and not _ladder_evidence(lattice) and problem == 'absent':
            return _refused(
                'no tape found: this blade carries no tick ladder, so there is '
                'nothing printed to measure from',
                'blade',
                blade=blade,
            )
        # `uneven` is not a refusal to read: a tape sagging over a trench has
        # marks that scatter around its pitch (measured 2.3 px on an 8.5 px
        # pitch, `20261124_141314`), and the pitch is not where the millimetres
        # come from. The printed numbers set the scale and the pitch is checked
        # against the standard spacings there, so an uneven ladder still reads.
        if problem and problem != 'uneven':
            return _refused(
                _ladder_refusal(problem), _ladder_refusal_stage(problem), blade=blade
            )
        if _ladder_evidence(lattice):
            # The clip stage reads the confirmation flag off this same geometry
            # and refuses on it a second time, so the ladder's answer is carried
            # into what it is handed. Without this the blade passed the gate
            # above and was refused again below, which is how `141314` still
            # reported "not confirmed by a second working resolution" after the
            # ladder had answered the question.
            blade = {**blade, 'confirmed': True, 'confirmed_by': 'ladder'}
        clip = tape_clip.find_clip(rgb, band=blade)
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
    # Policy, changed with the crew 2026-11-25: an unconfirmed blade is no
    # longer a refusal. The locator can return a confident-looking band that is
    # not the tape (`20261124_140332` returns a band at -33 degrees where the
    # tape runs at +12), so confirmation still matters -- but a blade whose
    # printed numbers step along it is a tape whatever the two resolutions made
    # of it. The reading goes out marked low-confidence instead of withheld.
    blade_unconfirmed = not clip.get('blade_confirmed')

    band = clip.get('band') or {}
    if not band.get('centre') or not band.get('length_px'):
        return _refused('the clip came without a blade to read along', 'blade', clip=clip)

    faces = clip.get('faces') or []
    if len(faces) != 2:
        return _refused('the clip did not report the two faces of its reading', 'clip', clip=clip)

    if strip is None:
        strip, scale = _rectify(rgb, band)
        lattice = tape_ticks.read_strip(strip)
    problem = tape_ticks.ladder_problem(lattice)
    # Same rule as the blade stage above: `uneven` still reads, because the
    # millimetres come from the numbers and not from the pitch. It is carried
    # through to the response instead, so the engineer can see the marks
    # wandered rather than being told there is no reading.
    if problem and problem != 'uneven':
        return _refused(
            _ladder_refusal(problem), _ladder_refusal_stage(problem), clip=clip
        )
    lattice_note = (
        'the marks wander along this blade, so the scale is taken from the '
        'printed numbers'
        if problem == 'uneven'
        else None
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
    caveats: list[str] = []
    if blade_partial:
        caveats.append(
            'only part of the blade could be located in the frame, so the '
            'reading is a best effort'
        )
    elif blade_unconfirmed:
        caveats.append(
            'the blade was not confirmed by a second working resolution, so the '
            'number rests on its printed numbers alone'
        )
    if problem is not None:
        caveats.append(
            'the marks on this blade are too faint or uneven to check the scale, '
            'which is taken from the printed numbers'
        )
    if counted > 1.0 + DIVISION_TOLERANCE:
        # Policy, changed with the crew 2026-11-25: the reading is given even
        # when the clip runs past the division the count started from, rather
        # than refused. That count is a best effort, and the caveat says so.
        caveats.append(
            'the clip is more than one printed division from the nearest readable '
            'number, so the count to its face is a best effort'
        )

    unit_mm = 10.0 if numbers.get('unit') == 'cm' else 1.0
    reading_mm = reference_value * unit_mm
    if not np.isfinite(reading_mm):
        return _refused(
            'the reading that face implies is not a number', 'reading',
            clip=clip, label=label,
        )
    if reading_mm < MIN_READING_MM:
        # A tape has no reading at or before its zero. A face there means the
        # line of numbers was misread, and a negative millimetre value is a
        # wrong reading rather than a low-confidence one: measured on
        # `20261124_140444`, where tesseract 5.4.0 extrapolated -175.2 mm at a
        # clip standing beside the printed 30. "Always give the reading" is a
        # promise about a reading that exists, not licence to report one that
        # cannot.
        return _refused(
            "the clip sits at or before the tape's zero, so the printed numbers "
            'were misread',
            'reading',
            clip=clip,
            label=label,
            numbers=numbers,
        )
    if reading_mm > MAX_READING_MM:
        caveats.append(
            'the reading is outside the range a tape measures, so the scale is '
            'probably misread'
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
        'blade_confirmed': not blade_unconfirmed,
        'confidence': 'low' if caveats else 'high',
        'low_confidence': bool(caveats),
        'confidence_note': '; '.join(caveats) if caveats else None,
        'lattice_note': lattice_note,
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
