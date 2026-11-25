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

The scale is then what turns the object into a number: a trench wall, a chamber
rim and a duct's side are pixel positions along the calibrated blade, and
millimetres per pixel is the only thing between those positions and a span.
Those positions come from the strip itself. Where the blade crosses the edge of
a trench or a chamber it changes brightness along its whole height at one x,
which is exactly what a smudge, a stone or the shadow of one side of the blade
does not do.

Env: none. Tesseract needs no key.
"""

from __future__ import annotations

import numpy as np

from supervision.tape_ticks import MIN_INK_OFFSET, MIN_LADDER_SCORE, MIN_LADDER_TICKS

# Labels OCR'd per blade. A long blade prints far more than the scale needs, and
# every label is a subprocess on the free host.
MAX_CELLS = 14

# Cells OCR'd per read across every printed line tried. A second line is a
# second opinion, and trying each row of each edge band unbounded took the field
# read from 4.2 s to 8.5 s on the free host; two lines' worth is the budget.
MAX_OCR_CELLS = 2 * MAX_CELLS

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

# After the fit, a label may sit this share of a mark spacing from where its
# number says it belongs. A box clipped by the strip's end — or fused with the
# shade at a trench wall — keeps its number but moves its centre onto another
# mark, and that shifted anchor is what made a shaded angled blade's two halves
# disagree while every number had actually read.
POSITION_TOLERANCE = 0.5

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

# A row counts as printed when it carries this share of the band's densest row.
# The blade's own hatching and a couple of specks leave ink in every row, so
# "any ink at all" makes the whole band one printed run and hides the blank
# blade between two lines of numbers.
ROW_INK_FLOOR = 0.3

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

# The object the blade crosses — a trench wall, a chamber rim, a duct's side —
# is a brightness boundary across the whole blade, not a smudge: the strip
# changes level along its height at the same x. Below this gray-level step it is
# soil texture or a shadow, not the end of the object.
EDGE_MIN_STEP = 12.0

# Each half of the blade must see the boundary too, at this share of the full
# step: a mark, a stain or a hand shadow covers one side of the blade and would
# otherwise read as an object edge.
EDGE_BAND_STEP_SHARE = 0.5

# The step is measured over this share of the strip's width each side of a
# column, so a soft photographic boundary still registers as one change.
EDGE_WINDOW_SHARE = 1.0 / 40.0

# Two changes closer than this are one boundary (or a mark), not an object.
EDGE_MIN_SEPARATION = 0.06

# A half of the blade may place the boundary this far from where the whole
# blade does and still be the same boundary.
EDGE_BAND_SHARE = 0.04

# An object narrower than this share of the blade is a mark or a gap, and one
# touching the strip's own ends has no measurable span.
EDGE_MIN_SPAN_SHARE = 0.15
EDGE_BORDER_SHARE = 0.03

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


def _printed_row_runs(shares: np.ndarray) -> list[tuple[int, int]]:
    """Every run of rows that carries printing, in the band's own rows.

    Densest first, because a line of numbers is tall and dense while a blade
    edge is one thin row. A row is printed when its ink clears `ROW_INK_FLOOR`
    of the densest row: the hatching between two printed lines leaves a speck in
    every row, and counting those would join the lines into one run.
    """
    shares = np.asarray(shares, dtype=float)
    if shares.size == 0 or not np.isfinite(shares).all() or shares.max() <= 0.0:
        return []
    floor = float(shares.max()) * ROW_INK_FLOOR
    printed = (shares >= floor) & (shares < MAX_ROW_INK)
    runs: list[tuple[int, int]] = []
    start = None
    for row, on in enumerate(printed):
        if on and start is None:
            start = row
        elif not on and start is not None:
            runs.append((start, row))
            start = None
    if start is not None:
        runs.append((start, int(printed.size)))
    runs.sort(key=lambda run: float(np.sum(shares[run[0] : run[1]])), reverse=True)
    return runs


def _printed_rows(shares: np.ndarray) -> tuple[int, int] | None:
    """The rows of a band that carry printing, as a half-open range.

    A warped strip has its own border in the frame — a row or two of ink from
    edge to edge — and a blurred blade edge can leave a nearly-empty row stuck to
    it. Neither is printing, so the run carrying the most ink wins rather than
    the widest span.
    """
    runs = _printed_row_runs(shares)
    if not runs:
        return None
    return int(runs[0][0]), int(runs[0][1])


def _printed_line_bands(
    ink: np.ndarray, band: tuple[int, int]
) -> list[tuple[int, int]]:
    """Candidate single printed lines inside one band, densest first.

    A survey tape prints more than one line: a repeating units row beside its
    divisions. The two lines do not meet at the strip's middle, so `_digit_bands`
    halving the strip puts one line's numbers across the cut — the cell holds the
    lower half of a number, tesseract answers a stray single digit, and those
    single-digit anchors are what disagreed about their spacing on the crew's
    `20261124_141314`. Each run of printed rows is offered as its own band, with
    the densest run repeated last so a blade whose printing is one tall line is
    read exactly as before.
    """
    start, stop = band
    if stop - start < MIN_BAND_HEIGHT:
        return []
    shares = ink[start:stop].mean(axis=1)
    rows = [
        (start + run[0], start + run[1])
        for run in _printed_row_runs(shares)
        if run[1] - run[0] >= MIN_BAND_HEIGHT
    ]
    return rows


def _widest_ink_run(ink: np.ndarray, rows: tuple[int, int], gap: int) -> int:
    """The longest run of inked columns once gaps under `gap` are closed.

    Merged exactly as `_label_boxes` merges it, because that is the run that got
    rejected: a strict run measures 108 px on a strip whose marks span all 853,
    since the millimetre gaps between them break it. Kept only so a failure can
    say what actually happened — when the row range still contains the marks,
    every column fuses into one run, and "no printed numbers could be told
    apart from the marks" makes that sound like an OCR failure, which is how
    `08.38.21` was misdiagnosed for a day.
    """
    start, stop = rows
    if stop <= start:
        return 0
    occupied = ink[start:stop].any(axis=0)
    longest = 0
    left: int | None = None
    last = -1
    for x, filled in enumerate(occupied):
        if filled:
            if left is None:
                left = x
            last = x
        elif left is not None and x - last > gap:
            longest = max(longest, last + 1 - left)
            left = None
    if left is not None:
        longest = max(longest, last + 1 - left)
    return int(longest)


def _label_boxes(
    ink: np.ndarray,
    positions: list[int],
    pitch_px: float,
    band: tuple[int, int],
    rows: tuple[int, int] | None = None,
) -> tuple[list[dict], tuple[int, int]]:
    """Boxes around the printed numbers, and the rows they occupy.

    ``rows`` is one printed line of a band, already chosen by `_row_bands`; the
    band's own rows are used when it is not given.

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
    if rows is None:
        shares = ink[start:stop].mean(axis=1)
        printed = _printed_rows(shares)
        if printed is None:
            return [], (start, stop)
        tight = (start + printed[0], start + printed[1])
    else:
        tight = (max(start, rows[0]), min(stop, rows[1]))
    height = tight[1] - tight[0]
    if height < MIN_BAND_HEIGHT:
        return [], tight
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


_OCR_STATE: tuple[bool, str] | None = None


def ocr_availability() -> tuple[bool, str]:
    """Whether this host can run tesseract, and what it reported.

    pytesseract is only a wrapper around the binary, so on a host that never
    installed `tesseract-ocr` every cell raises, `_default_ocr` answers ``''``,
    and the blade is refused for "too few printed numbers" -- a reason that
    blames the photo for a server that cannot read any photo. The project's
    `Dockerfile` installs the binary; a host deployed as a plain Python service
    does not. Measured after a server switch: every one of the 30 field photos
    refused at the numbers stage, on a host with no OCR engine.

    Cached because it costs a subprocess and cannot change while the process
    lives.
    """
    global _OCR_STATE
    if _OCR_STATE is not None:
        return _OCR_STATE
    try:
        import pytesseract

        _OCR_STATE = (True, str(pytesseract.get_tesseract_version()))
    except Exception as exc:  # noqa: BLE001 - every failure here is the same answer
        _OCR_STATE = (False, f'{type(exc).__name__}: {exc}')
    return _OCR_STATE


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
    # The sign is kept rather than refused. The strip's axis is normalised to
    # point rightwards whichever way the blade's numbers run, and the ground-truth
    # photo `20261124_140332` prints 42, 41, 40, 39 left to right: a ladder that
    # falls is still one line, and refusing it made that blade unreadable here
    # while the reader needed exactly the direction it prints in.
    if not np.isfinite(slope) or slope == 0.0:
        return None
    return slope, float(np.median(values - slope * xs))


def _trim_outliers(anchors: list[dict], pitch_px: float = 0.0) -> list[dict]:
    """Drop digits that do not sit on the blade's line of numbers.

    A blade prints its numbers evenly, so a digit a whole step off that line was
    misread. Dropping it keeps a single bad cell from tilting the scale that
    every measurement would later inherit.

    The same trim is done on the positions when the mark spacing is known. A
    label whose cell was clipped by the strip's end, or fused with the shade at
    a trench wall, still reads the right number but its centre lands on the
    wrong mark: its value fits the line and its position does not. Left in, that
    one anchor skews a half-blade fit enough to refuse a scale every number
    agreed with (measured on a shaded angled upload, where the span was lost
    for one clipped label at the blade's end).
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
    # Four anchors are the least that can show one of them sitting off its mark:
    # with three, removing one leaves the two the line was drawn through.
    if pitch_px >= 2.0 and len(kept) > MIN_ANCHORS + 1:
        for _ in range(2):
            positions = np.array([anchor['x_px'] for anchor in kept], dtype=float)
            measured = np.array([anchor['value'] for anchor in kept], dtype=float)
            fit = _line(measured, positions)
            if fit is None:
                break
            slope, intercept = fit
            reach = POSITION_TOLERANCE * pitch_px
            survivors = [
                anchor
                for anchor in kept
                if abs(anchor['x_px'] - (intercept + slope * anchor['value'])) <= reach
            ]
            if len(survivors) == len(kept) or len(survivors) < MIN_ANCHORS + 1:
                break
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
        return _refused('the numbers on the blade do not step along the blade', anchors, unit=unit)
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

    # Millimetres per pixel is a distance, so the ladder's direction does not
    # enter it: a descending ladder and an ascending one of the same pitch give
    # the same scale.
    mm_per_px = abs(slope) * unit_mm
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
    # Each half needs three anchors to be a scale at all: a two-label half has
    # one gap in it, so its slope is that single mark spacing shifted by half a
    # pixel of box centre, and two such halves disagree by a per cent on
    # rounding alone. A glared blade with five readable labels was refused for
    # exactly that, while its residual and its tick pitch both agreed.
    agreement = None
    if len(anchors) >= 6:
        middle = len(anchors) // 2
        halves = []
        for subset in (anchors[:middle], anchors[middle:]):
            local_xs = np.array([anchor['x_px'] for anchor in subset], dtype=float)
            local_values = np.array([anchor['value'] for anchor in subset], dtype=float)
            local = _line(local_xs, local_values)
            if local is not None:
                halves.append(local[0])
        if len(halves) == 2:
            agreement = abs(halves[0] - halves[1]) / abs(slope)
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


def _background_profile(pixels: np.ndarray) -> np.ndarray:
    """Each column's blade brightness, with any step across the strip smoothed out.

    A trench or chamber shades whole columns of the blade, and a threshold taken
    from the strip-wide median then calls that shade ink: the printed numbers
    land inside one enormous ink band and disappear into it (measured on a
    shaded upload: the edges were found, the numbers were not). Smoothing the
    column median over a wide window makes a shading step part of the
    background and leaves only the printing below it. An evenly lit blade gives
    the same profile back, so nothing changes there.

    The window is a median, not a mean: averaging ramps the background down
    across the change of level, and every column inside that ramp counts the
    darker side as ink — measured on a shaded upload, that turned the trench
    wall into a full-height ink block that fused all the numbers into one run.
    A median keeps the two levels flat right up to the step.
    """
    profile = np.median(pixels, axis=0)
    width = profile.shape[0]
    span = max(3, int(round(width * 0.08)))
    if width <= span:
        return profile
    padded = np.pad(profile, span // 2, mode='edge')
    windows = np.lib.stride_tricks.sliding_window_view(padded, span)
    return np.median(windows, axis=1)[:width]


def _read_boxes(
    strip,
    width: int,
    boxes: list[dict],
    tight: tuple[int, int],
    read,
    stretch: float,
) -> list[dict]:
    """OCR one printed line's cells into anchors, keeping only sure digits."""
    start, stop = tight
    anchors: list[dict] = []
    for box in boxes:
        pad = max(2, int(round((box['right'] - box['left']) * CELL_PAD_SHARE)))
        left = max(0, box['left'] - pad)
        right = min(width, box['right'] + pad)
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
    return anchors


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
    # An `uneven` ladder is still a ladder. The cells need marks to align to,
    # not a ruler: the millimetres come from the printed numbers (`_calibrate`),
    # and the pitch is only cross-checked against the standard tick spacings.
    # Refusing here was the gate that locked `20261124_141314` out -- a genuine
    # tape with 77 marks at 0.898 -- because a blade sagging over a trench does
    # not hold one pitch (2.3 px of scatter on an 8.5 px pitch).
    if int(lattice.get('count') or 0) < MIN_LADDER_TICKS or float(
        lattice.get('score') or 0.0
    ) < MIN_LADDER_SCORE:
        return _refused('the marks are not even enough to align numbers to')

    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    if pixels.ndim != 2 or pixels.shape[1] < 40:
        return None
    ink = pixels < (_background_profile(pixels)[None, :] - MIN_INK_OFFSET)
    plans: list[tuple[tuple[int, int, bool], list[dict], tuple[int, int]]] = []
    widest = 0
    for candidate in _digit_bands(ink, positions):
        for rows in _printed_line_bands(ink, (candidate[0], candidate[1])):
            found, tight = _label_boxes(
                ink, positions, pitch_px, (candidate[0], candidate[1]), rows
            )
            widest = max(
                widest,
                _widest_ink_run(
                    ink,
                    tight,
                    max(2, int(round((tight[1] - tight[0]) * LABEL_GAP_SHARE))),
                ),
            )
            if found:
                plans.append((candidate, found, tight))
    if not plans:
        # Ink across nine tenths of the strip is the blade's ladder, not a line
        # of numbers: the band was never separated from the marks, so no box
        # could be formed. Saying so is the difference between "re-aim the
        # camera" and "this module needs fixing".
        if widest >= 0.9 * pixels.shape[1]:
            return _refused(
                'the marks on the blade fill the strip, so no printed numbers could '
                'be told apart from them'
            )
        return _refused('no printed numbers could be told apart from the marks')

    read = ocr or _default_ocr
    if ocr is None:
        ready, detail = ocr_availability()
        if not ready:
            return _refused(
                'the server has no OCR engine, so no printed number can be read '
                f'({detail})'
            )
    # The row whose numbers earn a scale is the blade's line of numbers. Trying
    # them in turn, densest first, is what lets one strip carry a repeating units
    # row and its divisions: only one of them steps evenly along the blade.
    plans.sort(key=lambda plan: len(plan[1]), reverse=True)
    refusal: dict | None = None
    spent = 0
    for band, boxes, tight in plans:
        budget = MAX_OCR_CELLS - spent
        if budget < MIN_ANCHORS:
            break
        spent += min(budget, len(boxes))
        anchors = _read_boxes(
            strip, pixels.shape[1], boxes[:budget], tight, read, stretch
        )
        if len(anchors) < MIN_ANCHORS:
            current = _refused(
                'too few printed numbers were readable to set a scale', anchors
            )
        else:
            trimmed = _trim_outliers(anchors, pitch_px)
            if (
                len(trimmed) < MIN_ANCHORS
                or len(trimmed) < len(anchors) * MIN_AGREEING_SHARE
            ):
                current = _refused(
                    'the numbers on the blade disagree about their spacing', anchors
                )
            else:
                current = _calibrate(
                    trimmed,
                    pitch_px,
                    (tight[0], tight[1], band[2]),
                    pixels.shape[1],
                )
                if current.get('usable'):
                    return current
        if refusal is None or len(current['anchors']) > len(refusal['anchors']):
            refusal = current
    return refusal


def _no_span(reason: str) -> dict:
    """A span that could not be measured, with the reason it failed."""
    return {
        'usable': False,
        'reason': reason,
        'span_mm': None,
        'span_m': None,
        'edges_px': None,
        'mm_per_px': None,
        'unit': None,
    }


def span_mm(scale: dict | None, first_px: float, second_px: float) -> dict:
    """The millimetres between two edges along the blade the scale came from.

    An edge is a boundary the blade crosses — a trench wall, a chamber rim, a
    duct's side — and on a rectified strip it is one pixel position along the
    blade. The calibration is what makes that a measurement: the ticks gave
    millimetres per pixel, so the distance between the two edges converts without
    asking the OCR to read the blade again.

    The order of the edges does not matter: a span is a distance, not a direction.
    """
    if not scale:
        return _no_span('no scale was read from the blade')
    if not scale.get('usable') or not scale.get('mm_per_px'):
        return _no_span(scale.get('reason') or 'the numbers did not set a scale')
    try:
        first, second = float(first_px), float(second_px)
    except (TypeError, ValueError):
        return _no_span('the object edges are not positions along the blade')
    per_px = float(scale['mm_per_px'])
    millimetres = abs(second - first) * per_px
    if not np.isfinite(millimetres):
        return _no_span('the object edges are not positions along the blade')
    if millimetres <= 0.0:
        return _no_span('the two edges sit at the same place on the blade')
    return {
        'usable': True,
        'reason': None,
        'span_mm': round(millimetres, 1),
        'span_m': round(millimetres / 1000.0, 3),
        'edges_px': [round(first, 1), round(second, 1)],
        'mm_per_px': per_px,
        'unit': scale.get('unit'),
    }


def _no_edges(reason: str) -> dict:
    """Edges that were not found, with the reason they were not."""
    return {'usable': False, 'reason': reason, 'edges_px': None, 'contrast': None}


def _column_profile(pixels: np.ndarray) -> np.ndarray:
    """Each column's brightness as the median of its rows.

    The median, not the mean: a printed number or a mark darkens only the rows it
    covers, while the object the blade crosses changes every row. On the
    rendered blade the mean profile stepped by 21 gray levels at every printed
    number and the median profile was flat to the level, which is the difference
    between finding a boundary and finding the printing.
    """
    return np.median(pixels, axis=0)


def _smoothed_profile(profile: np.ndarray) -> np.ndarray:
    """A profile with single-column noise averaged out.

    One dusty column or one blown highlight is not the end of a trench; a
    boundary is a change that holds over many columns.
    """
    radius = max(1, profile.size // 80)
    kernel = np.ones(2 * radius + 1) / float(2 * radius + 1)
    return np.convolve(profile.astype(np.float64), kernel, mode='same')


def _signed_steps(profile: np.ndarray, window: int) -> np.ndarray:
    """Brightness change at each column, signed, over a window each side.

    Signed, not absolute: an object edge is a step down into the trench and a
    step back up out of it, and two like-signed steps are two shadows or two
    marks, not the two ends of one thing.
    """
    values = profile.astype(np.float64)
    count = values.size
    window = max(2, min(int(window), max(2, count // 4)))
    prefix = np.concatenate([[0.0], np.cumsum(values)])
    columns = np.arange(count)
    left_start = np.clip(columns - window, 0, count)
    right_stop = np.clip(columns + window, 0, count)
    left_mean = (prefix[columns] - prefix[left_start]) / np.maximum(
        1, columns - left_start
    )
    right_mean = (prefix[right_stop] - prefix[columns]) / np.maximum(
        1, right_stop - columns
    )
    return right_mean - left_mean


def _strong_steps(steps: np.ndarray, min_gap: int, threshold: float) -> list[int]:
    """The strongest separated changes in a signed step profile, left to right."""
    magnitude = np.abs(steps)
    candidates = [
        (float(magnitude[x]), x)
        for x in range(1, magnitude.size - 1)
        if magnitude[x] >= threshold
        and magnitude[x] >= magnitude[x - 1]
        and magnitude[x] >= magnitude[x + 1]
    ]
    candidates.sort(reverse=True)
    chosen: list[int] = []
    for _score, x in candidates:
        if all(abs(x - other) >= min_gap for other in chosen):
            chosen.append(x)
    return sorted(chosen)


def _row_bands(pixels: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The two halves of the blade, clear of the strip's own border rows."""
    height = pixels.shape[0]
    top = pixels[int(height * 0.05) : max(int(height * 0.05) + 1, int(height * 0.45))]
    bottom = pixels[max(1, int(height * 0.55)) : max(2, int(height * 0.95))]
    return top, bottom


def find_edges(strip) -> dict:
    """The two ends of the object the blade crosses, as x positions on the strip.

    On a rectified strip the object's edge is one column: the blade over the
    trench or the chamber opening is in shadow and the same blade over the
    ground is not, so the strip's column brightness steps down at one wall and
    back up at the other. A tape lying along the object instead of across it has
    no such pair, and is refused rather than given a span from two smudges.

    Both halves of the blade have to change at the same x. That is what separates
    the end of an object, which the whole blade crosses, from a mark, a patch of
    dust or the shadow of one side of the tape.
    """
    if strip is None:
        return _no_edges('no strip was rectified to find an edge on')
    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    if pixels.ndim != 2 or pixels.shape[0] < 8 or pixels.shape[1] < 80:
        return _no_edges('the strip is too small to carry an object edge')
    width = pixels.shape[1]
    window = max(2, int(round(width * EDGE_WINDOW_SHARE)))
    profile = _column_profile(pixels)
    steps = _signed_steps(_smoothed_profile(profile), window)

    border = int(round(width * EDGE_BORDER_SHARE))
    gap = max(4, int(round(width * EDGE_MIN_SEPARATION)))
    candidates = [
        x
        for x in _strong_steps(steps, gap, EDGE_MIN_STEP)
        if border <= x < width - border
    ]
    min_span = int(round(width * EDGE_MIN_SPAN_SHARE))
    pair: tuple[float, int, int] | None = None
    for index, left in enumerate(candidates):
        for right in candidates[index + 1 :]:
            if right - left < min_span or steps[left] * steps[right] >= 0.0:
                continue
            score = min(abs(steps[left]), abs(steps[right]))
            if pair is None or score > pair[0]:
                pair = (score, left, right)
    if pair is None:
        return _no_edges('no boundary crosses the blade')

    _score, left, right = pair
    tolerance = max(2, int(round(width * EDGE_BAND_SHARE)))
    for band in _row_bands(pixels):
        band_steps = _signed_steps(_smoothed_profile(_column_profile(band)), window)
        for edge in (left, right):
            low, high = max(0, edge - tolerance), min(width, edge + tolerance + 1)
            local = band_steps[low:high]
            peak = int(np.argmax(np.abs(local)))
            if (
                np.sign(local[peak]) != np.sign(steps[edge])
                or abs(local[peak]) < EDGE_MIN_STEP * EDGE_BAND_STEP_SHARE
            ):
                return _no_edges('the boundary is on one side of the blade only')

    # Between the edges the blade must be over a genuinely different surface:
    # a step without a level behind it is a gradient or a row of printing, not
    # the end of an object.
    inside = profile[left + 2 * window : right - 2 * window]
    if inside.size < 2:
        return _no_edges('the two edges are too close together to measure')
    inside_level = float(np.median(inside))
    outside_left = float(np.median(profile[border : max(border + 1, left - 2 * window)]))
    outside_right = float(
        np.median(profile[min(width - 1, right + 2 * window) : width - border])
    )
    if (
        abs(inside_level - outside_left) < EDGE_MIN_STEP
        or abs(inside_level - outside_right) < EDGE_MIN_STEP
        or np.sign(inside_level - outside_left) != np.sign(inside_level - outside_right)
    ):
        return _no_edges('the blade does not cross a different surface between them')
    return {
        'usable': True,
        'reason': None,
        'edges_px': [float(left), float(right)],
        'contrast': round(
            float(min(abs(steps[left]), abs(steps[right]))), 1
        ),
    }


def read_span(strip, scale: dict | None) -> dict:
    """The millimetres between the two edges of the object the blade crosses.

    ``scale`` is what ``read_labels`` returned for this same strip, so the edge
    positions and the millimetres per pixel are measured on one image and no
    second OCR pass is needed. A refused scale still reports the edges that were
    found, which is what keeps a failed field read diagnosable.
    """
    edges = find_edges(strip)
    if not edges['usable']:
        span = _no_span(edges['reason'])
        span['contrast'] = None
        return span
    span = span_mm(scale, *edges['edges_px'])
    span['edges_px'] = edges['edges_px']
    span['contrast'] = edges['contrast']
    return span
