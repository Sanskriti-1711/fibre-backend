"""Read the numbers printed at the blade's marks, and turn its pitch into millimetres.

A ladder alone is a pitch in pixels. Nothing in the pitch says whether one mark
is a millimetre or a centimetre, so the digits are what make it a measurement:
they sit on the same lattice as the ticks, so a handful of them fixes the scale,
and every later pixel distance converts without asking the OCR to read the blade
again.

The regions are the printed numbers themselves, not equal slices of the ladder.
A number is five to seven mark-spacings wide on a centimetre-tick blade, so a
cell cut to one pitch chops the number in half and reads nothing; and a fixed
width cannot work for both a millimetre blade and a centimetre one. Ink in the
number band is grouped into labels and each label is attached to the mark it is
printed over, which also means only the marks that carry a number cost an OCR
call.

The cells are cut from the band beside the marks, located from how far the ink
hangs off the blade's edge. Cropping the full blade height puts the marks inside
the cell, and a vertical stroke beside a digit reads as a stray ``1`` ("10"
becomes "110").

The fit is robust on purpose: a blade prints its numbers evenly, so a digit that
does not land on that line is a misread and is dropped rather than allowed to
tilt the scale. The surviving scale is then checked against the tick pitch — the
pitch and the numbers are independent measurements of the same blade — and a
scale the marks cannot explain is refused, because a confident wrong scale is
worse than no reading at all.

Env: none. Tesseract needs no key.
"""

from __future__ import annotations

import numpy as np

from supervision.tape_ticks import MIN_INK_OFFSET

# Labels OCR'd per blade. A long blade prints far more than the scale needs, and
# every label is a subprocess on the free host.
MAX_CELLS = 14

# Cells are upscaled to this height before OCR. A printed digit on a rectified
# strip is often 12-20 px tall, which tesseract reads poorly without help.
CELL_UPSCALE_HEIGHT = 56

# White margin around a cell: tesseract reads a lone number better with space
# than with its strokes touching the crop's edge.
CELL_PADDING = 4
CELL_PAD_SHARE = 0.15

# Labels are separated by at least this many band heights of blank blade, while
# glyphs inside one label are much closer than that.
LABEL_GAP_SHARE = 0.6

# A label is at least this share of the band height wide, and no wider than this
# many band heights: anything outside is blade edge or branding, not a number.
MIN_LABEL_SHARE = 0.2
MAX_LABEL_HEIGHTS = 8.0

# A label belongs to the mark it is printed over; this is how far off-centre it
# may sit and still be that mark's number.
MARK_REACH = 1.5

# A digit tesseract is not at least this sure of is worse than no digit: it would
# land on the wrong mark and drag the fitted scale towards itself.
MIN_LABEL_CONFIDENCE = 0.45

# Two points always fit a line perfectly, so this is the least number that can
# still be checked against the pitch.
MIN_ANCHORS = 2

# A cell shorter than this is a sliver, not a lane of printed numbers.
MIN_BAND_HEIGHT = 8

# A row that is ink from edge to edge is the strip's own border — the ground
# beyond the blade — not printing, and counting it fuses every number into one
# full-width run.
MAX_ROW_INK = 0.85

# The share of anchors that must survive outlier trimming. If more than a
# quarter of them are off the line, the numbers are not one printed ladder and
# the scale they imply is not the blade's.
MIN_AGREEING_SHARE = 0.7

# Standard spacings between printed marks on a survey tape, in millimetres.
# The fitted scale must land on one of these: a scale that says the marks are
# 3.2 mm apart describes a misread, not a tape.
STANDARD_TICK_MM = (0.5, 1.0, 2.0, 5.0, 10.0)
TICK_TOLERANCE = 0.12

# The two halves of the blade are two independent scales; they must agree with
# each other to this share, or one of them has been misread.
SCALE_AGREEMENT = 0.02

# `--psm 7` is one line of text, which is one label; the whitelist stops the
# engine offering letters, which the digit filter would only have to drop.
CELL_OCR_CONFIG = '--oem 1 --psm 7 -c tessedit_char_whitelist=0123456789'


def _refused(reason: str, anchors: list[dict] | None = None, **extra) -> dict:
    """A calibration that did not earn a scale, with the reason it failed."""
    return {
        'usable': False,
        'reason': reason,
        'anchors': anchors or [],
        'unit': None,
        'mm_per_tick': None,
        'mm_per_px': None,
        'strip_mm': None,
        'residual_mm': None,
        'agreement': None,
        **extra,
    }


def _edge_runs(ink: np.ndarray, positions: list[int]) -> tuple[list[int], list[int]]:
    """How far a mark's ink runs from its lowest ink and from its highest.

    Measured from the column's own ink, not from the strip's border. A warped
    blade never prints to the exact edge row, and starting at the border then
    reports no ink at all, which makes the whole strip look like one huge label.
    """
    height, width = ink.shape
    bottom: list[int] = []
    top: list[int] = []
    for x in positions:
        column = ink[:, min(max(int(x), 0), width - 1)]
        rows = np.flatnonzero(column)
        if not rows.size:
            bottom.append(0)
            top.append(0)
            continue
        count = 1
        row = int(rows[-1]) - 1
        while row >= 0 and column[row]:
            count += 1
            row -= 1
        bottom.append(count)
        count = 1
        row = int(rows[0]) + 1
        while row < height and column[row]:
            count += 1
            row += 1
        top.append(count)
    return bottom, top


def _digit_bands(ink: np.ndarray, positions: list[int]) -> list[tuple[int, int, bool]]:
    """Candidate bands for the printed numbers, likeliest first.

    Marks hang off one long edge, so the numbers are usually beyond the longest
    mark. But a blade printing its numbers under the marks is just as real, and
    which edge the ink hangs from is a guess made from run lengths. Both sides
    are offered, and the caller keeps whichever actually contains numbers.
    """
    height = ink.shape[0]
    if height < 2 * MIN_BAND_HEIGHT or not positions:
        return []
    bottom, top = _edge_runs(ink, positions)
    if not bottom:
        return []
    # The 95th percentile rather than the maximum: one mark bled into a scratch
    # must not collapse a band to nothing.
    bottom_reach = max(2, int(np.percentile(np.array(bottom, dtype=float), 95)))
    top_reach = max(2, int(np.percentile(np.array(top, dtype=float), 95)))

    bands: list[tuple[int, int, bool]] = []
    candidates = (
        (1, min(height - 1, height - bottom_reach - 1), True),
        (max(1, top_reach + 1), height - 1, False),
    )
    for band_start, band_stop, from_bottom in candidates:
        if band_stop - band_start >= MIN_BAND_HEIGHT:
            bands.append((band_start, band_stop, from_bottom))
    return bands


def _printed_rows(shares: np.ndarray) -> tuple[int, int] | None:
    """The rows of a band that carry printing, as a half-open range.

    A warped strip has its own border in the frame — a row or two of ink from
    edge to edge — and a blurred blade edge can leave a nearly-empty row stuck to
    it. Neither is printing, so the run carrying the most ink wins rather than
    the widest span: a line of numbers is tall and dense, a blade edge is one
    thin row.
    """
    printed = (shares > 0.0) & (shares < MAX_ROW_INK)
    runs: list[tuple[int, int]] = []
    start = None
    for row, on in enumerate(printed):
        if on and start is None:
            start = row
        elif not on and start is not None:
            runs.append((start, row))
            start = None
    if start is not None:
        runs.append((start, len(printed)))
    if not runs:
        return None
    best = max(runs, key=lambda run: float(np.sum(shares[run[0] : run[1]])))
    return int(best[0]), int(best[1])


def _label_boxes(
    ink: np.ndarray, positions: list[int], pitch_px: float, band: tuple[int, int]
) -> tuple[list[dict], tuple[int, int]]:
    """Boxes around the printed numbers, and the rows they occupy.

    The numbers are found by their own ink instead of by a fixed slice of the
    ladder. A printed number spans several marks, so slicing per mark cuts it;
    and its width differs between a centimetre blade and a millimetre one, so a
    single share of the pitch cannot fit both.

    The gap that separates one number from the next is measured against the rows
    that actually carry ink, not the whole band: a blade's number band usually
    has empty blade above the digits, and measuring gaps against that would fuse
    neighbouring numbers into one run.
    """
    start, stop = band
    if stop - start < MIN_BAND_HEIGHT or not positions:
        return [], (start, stop)
    shares = ink[start:stop].mean(axis=1)
    printed = _printed_rows(shares)
    if printed is None:
        return [], (start, stop)
    tight = (start + printed[0], start + printed[1])
    height = tight[1] - tight[0]
    occupied = ink[tight[0] : tight[1]].any(axis=0)

    gap = max(2, int(round(height * LABEL_GAP_SHARE)))
    runs: list[tuple[int, int]] = []
    left: int | None = None
    last = -1
    for x, filled in enumerate(occupied):
        if filled:
            if left is None:
                left = x
            last = x
        elif left is not None and x - last > gap:
            runs.append((left, last + 1))
            left = None
    if left is not None:
        runs.append((left, last + 1))

    marks = np.array(positions, dtype=float)
    narrowest = max(3, int(round(height * MIN_LABEL_SHARE)))
    widest = max(narrowest + 1, int(round(height * MAX_LABEL_HEIGHTS)))
    boxes: list[dict] = []
    for run_left, run_right in runs:
        width = run_right - run_left
        if width < narrowest or width > widest:
            continue
        centre = (run_left + run_right) / 2.0
        index = int(np.argmin(np.abs(marks - centre)))
        if abs(marks[index] - centre) > MARK_REACH * pitch_px:
            continue
        boxes.append(
            {
                'left': run_left,
                'right': run_right,
                'centre': centre,
                'index': index,
            }
        )
    return boxes[:MAX_CELLS], tight


def _cell_image(strip, box: tuple[int, int, int, int], stretch: float = 1.0):
    """One label's cell, upscaled, unsquashed and padded for tesseract.

    ``stretch`` undoes the strip's vertical squash. The warp resamples a blade to
    a clamped height, so a thick blade's digits arrive flattened — wide and
    short — and tesseract reads nothing until they are given back their shape.
    """
    from PIL import Image, ImageOps

    cell = ImageOps.autocontrast(strip.convert('L').crop(box))
    scale = max(1.0, CELL_UPSCALE_HEIGHT / max(1, cell.height))
    if scale > 1.0:
        cell = cell.resize(
            (
                max(1, int(round(cell.width * scale))),
                max(1, int(round(cell.height * scale))),
            ),
            Image.Resampling.LANCZOS,
        )
    if abs(stretch - 1.0) > 0.02:
        cell = cell.resize(
            (cell.width, max(1, int(round(cell.height * stretch)))),
            Image.Resampling.LANCZOS,
        )
    padded = Image.new(
        'L', (cell.width + 2 * CELL_PADDING, cell.height + 2 * CELL_PADDING), 255
    )
    padded.paste(cell, (CELL_PADDING, CELL_PADDING))
    return padded


def _default_ocr(cell, mark=None) -> tuple[str, float]:
    """Tesseract's best digits-only reading of one cell: (digits, confidence).

    ``mark`` is the lattice entry the label was attached to; the default reader
    does not need it, but injected readers are handed it so they can answer per
    mark.
    """
    try:
        import pytesseract

        data = pytesseract.image_to_data(
            cell, config=CELL_OCR_CONFIG, output_type=pytesseract.Output.DICT
        )
    except Exception:
        return '', 0.0
    text, confidence = '', 0.0
    for raw, score in zip(data.get('text') or [], data.get('conf') or []):
        digits = ''.join(character for character in str(raw) if character.isdigit())
        try:
            value = float(score) / 100.0
        except (TypeError, ValueError):
            continue
        if digits and value > confidence:
            text, confidence = digits, value
    return text, confidence


def _line(xs: np.ndarray, values: np.ndarray) -> tuple[float, float] | None:
    """(label units per pixel, intercept) of the blade's line of numbers.

    Theil-Sen, not least squares: one misread label is a whole step off the
    line, and least squares would tilt the fit towards it and then call the
    honest anchors the outliers.
    """
    if xs.size < 2:
        return None
    slopes: list[float] = []
    for index in range(xs.size - 1):
        dx = xs[index + 1 :] - xs[index]
        dy = values[index + 1 :] - values[index]
        usable = dx != 0
        slopes.extend((dy[usable] / dx[usable]).tolist())
    if not slopes:
        return None
    slope = float(np.median(slopes))
    if not np.isfinite(slope) or slope <= 0:
        return None
    return slope, float(np.median(values - slope * xs))


def _trim_outliers(anchors: list[dict]) -> list[dict]:
    """Drop digits that do not sit on the blade's line of numbers.

    A blade prints its numbers evenly, so a digit a whole step off that line was
    misread. Dropping it keeps a single bad cell from tilting the scale that
    every measurement would later inherit.
    """
    kept = list(anchors)
    for _ in range(2):
        xs = np.array([anchor['x_px'] for anchor in kept], dtype=float)
        values = np.array([anchor['value'] for anchor in kept], dtype=float)
        fit = _line(xs, values)
        if fit is None:
            return []
        slope, intercept = fit
        steps = np.abs(np.diff(values))
        tolerance = max(0.25 * float(np.median(steps)) if steps.size else 1.0, 0.4)
        survivors = [
            anchor
            for anchor in kept
            if abs(anchor['value'] - (intercept + slope * anchor['x_px'])) <= tolerance
        ]
        if len(survivors) == len(kept):
            break
        if len(survivors) < MIN_ANCHORS:
            return []
        kept = survivors
    return kept


def _infer_unit(values: list[int]) -> str:
    """Centimetre tapes stop in the hundreds; millimetre tapes print thousands."""
    return 'mm' if max(values) >= 1000 else 'cm'


def _nearest_standard(mm_per_tick: float) -> tuple[float, float]:
    """(closest standard spacing, relative error) for a fitted spacing."""
    best, error = STANDARD_TICK_MM[0], float('inf')
    for standard in STANDARD_TICK_MM:
        current = abs(mm_per_tick - standard) / standard
        if current < error:
            best, error = standard, current
    return best, error


def _calibrate(
    anchors: list[dict], pitch_px: float, band: tuple[int, int, bool], strip_width: int
) -> dict:
    """Millimetres per pixel from anchors that fit one line, or a refusal."""
    values = [anchor['value'] for anchor in anchors]
    unit = _infer_unit(values)
    unit_mm = 10.0 if unit == 'cm' else 1.0

    xs = np.array([anchor['x_px'] for anchor in anchors], dtype=float)
    measured = np.array(values, dtype=float)
    fit = _line(xs, measured)
    if fit is None:
        return _refused('the numbers on the blade do not step upward', anchors, unit=unit)
    slope, intercept = fit

    residual_mm = (
        float(np.sqrt(np.mean((measured - (intercept + slope * xs)) ** 2))) * unit_mm
    )
    # A blade's numbers lie on one line. A residual of a whole label step means
    # the survivors are not one ladder, even though they fit a line each.
    steps = np.abs(np.diff(measured))
    step_mm = float(np.median(steps)) * unit_mm if steps.size else residual_mm + 1.0
    if residual_mm > max(1.0, 0.03 * step_mm):
        return _refused(
            'the printed numbers disagree along the blade',
            anchors,
            unit=unit,
            residual_mm=round(residual_mm, 2),
        )

    mm_per_px = slope * unit_mm
    mm_per_tick = mm_per_px * pitch_px
    standard, tick_error = _nearest_standard(mm_per_tick)
    if tick_error > TICK_TOLERANCE:
        return _refused(
            'the numbers and the marks disagree about the scale',
            anchors,
            unit=unit,
            mm_per_tick=round(mm_per_tick, 3),
            mm_per_px=round(mm_per_px, 4),
            residual_mm=round(residual_mm, 2),
        )

    # Two halves of the blade are two independent scales. A single bad anchor
    # that survived the trim shows up as a slope only one half agrees with.
    agreement = None
    if len(anchors) >= 4:
        middle = len(anchors) // 2
        halves = []
        for subset in (anchors[:middle], anchors[middle:]):
            local_xs = np.array([anchor['x_px'] for anchor in subset], dtype=float)
            local_values = np.array([anchor['value'] for anchor in subset], dtype=float)
            local = _line(local_xs, local_values)
            if local is not None:
                halves.append(local[0])
        if len(halves) == 2:
            agreement = abs(halves[0] - halves[1]) / slope
            if agreement > SCALE_AGREEMENT:
                return _refused(
                    'the two ends of the blade disagree about the scale',
                    anchors,
                    unit=unit,
                    mm_per_tick=round(mm_per_tick, 3),
                    mm_per_px=round(mm_per_px, 4),
                    residual_mm=round(residual_mm, 2),
                    agreement=round(agreement, 4),
                )

    return {
        'usable': True,
        'reason': None,
        'anchors': anchors,
        'unit': unit,
        'mm_per_tick': round(mm_per_tick, 3),
        'mm_per_px': round(mm_per_px, 4),
        'strip_mm': round(mm_per_px * strip_width, 1),
        'residual_mm': round(residual_mm, 2),
        'agreement': round(agreement, 4) if agreement is not None else None,
        'standard_tick_mm': standard,
        'band': {'rows': [band[0], band[1]], 'from_bottom': band[2]},
    }


def read_labels(
    strip, lattice: dict | None, ocr=None, stretch: float = 1.0
) -> dict | None:
    """The blade's printed numbers pinned to its lattice, as a scale.

    ``lattice`` is the dict ``tape_ticks.read_strip`` returns; each printed
    number is attached to the mark whose position it sits over. ``ocr(cell,
    mark)`` returns ``(digits, confidence)`` and defaults to tesseract - tests
    pass their own so the calibration can be checked without an OCR install.
    ``stretch`` is the strip's true thickness over its height, which undoes the
    vertical squash a clamped warp leaves in the glyphs.

    Returns None when there is no lattice to align to, otherwise a dict whose
    ``usable`` says whether the numbers earned a scale; a refusal still carries
    the anchors it did read, which is what makes a failed field read diagnosable.
    """
    if strip is None or not lattice:
        return None
    positions = [
        int(round(float(position))) for position in lattice.get('positions') or []
    ]
    pitch_px = float(lattice.get('pitch') or 0.0)
    if len(positions) < 2 or pitch_px < 2.0:
        return None
    if not lattice.get('usable'):
        return _refused('the marks are not even enough to align numbers to')

    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    if pixels.ndim != 2 or pixels.shape[1] < 40:
        return None
    ink = pixels < (float(np.median(pixels)) - MIN_INK_OFFSET)
    best: tuple[tuple[int, int, bool], list[dict], tuple[int, int]] | None = None
    for candidate in _digit_bands(ink, positions):
        found, tight = _label_boxes(ink, positions, pitch_px, (candidate[0], candidate[1]))
        if found and (best is None or len(found) > len(best[1])):
            best = (candidate, found, tight)
    if best is None:
        return _refused('no printed numbers could be told apart from the marks')
    band, boxes, tight = best
    start, stop = tight
    _from_bottom = band[2]

    read = ocr or _default_ocr
    anchors: list[dict] = []
    for box in boxes:
        pad = max(2, int(round((box['right'] - box['left']) * CELL_PAD_SHARE)))
        left = max(0, box['left'] - pad)
        right = min(pixels.shape[1], box['right'] + pad)
        if right - left < 4:
            continue
        cell = _cell_image(strip, (left, start, right, stop), stretch)
        text, confidence = read(
            cell, {'index': box['index'], 'x_px': int(round(box['centre']))}
        )
        if not text or not str(text).isdigit() or confidence < MIN_LABEL_CONFIDENCE:
            continue
        anchors.append(
            {
                'index': box['index'],
                'x_px': round(float(box['centre']), 1),
                'text': str(text),
                'value': int(text),
                'confidence': round(float(confidence), 2),
            }
        )

    if len(anchors) < MIN_ANCHORS:
        return _refused('too few printed numbers were readable to set a scale', anchors)

    trimmed = _trim_outliers(anchors)
    if len(trimmed) < MIN_ANCHORS or len(trimmed) < len(anchors) * MIN_AGREEING_SHARE:
        return _refused('the numbers on the blade disagree about their spacing', anchors)
    return _calibrate(trimmed, pitch_px, band, pixels.shape[1])
