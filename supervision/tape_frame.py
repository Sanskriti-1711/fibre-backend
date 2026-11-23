"""Find the blade in a field photo and straighten it into a level strip.

Every later step wants the tape on a horizontal axis. A blade lying at 7 degrees
smears each tick across several image rows, which is why column profiles on the
raw photo found soil texture instead of marks (measured 2026-11-23: gap
histogram 3, 8, 9, 10, 16, 18, 30 … with a coefficient of variation of 1.24).
This module localises the blade, fits its centre line, and warps it into a
canonical strip so ticks, digits and the object's edges become one-dimensional
problems.

The candidate band is chosen by the ladder its strip yields, not by geometry.
An extent threshold could not separate a blade from pale gravel beside it: two
of six field photos were thrown away because their bright neighbour pushed the
mask past the limit. A ladder is a far stronger signal than brightness — the
blade is the only thing in the frame that prints one — so each candidate is
warped, graded by the ticks it produces, and the best grade wins.

Two other choices were forced by the field set:

* The mask is an Otsu split of the band, because a blade's flat middle matches
  its own blurred background and a local-contrast mask keeps only the rim.
* A densest-window trim removes pale ground above and below before the line fit;
  on the rendered blade it took the quad from 418 px thick to the blade's 136.

The strip is squared up from four detected corners, not from a centre line and a
constant thickness. A tape's long edges are straight even where perspective makes
them converge, so two lines through the mask's edges meet at the blade's corners,
and a projective warp of those four points undoes the foreshortening of a handheld
photo. A rectangle cannot: the tick pitch then drifts along the strip, which is
exactly how field-1 to field-5 failed their lattice fit (residual 1.3-4.4).

Deliberately not a learned detector: a tape is the longest printed structure in
the frame, and a detector would need a labelled set this project does not have.

Env: none.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

# The mask is built on a small copy: the quad only needs a pixel of accuracy at
# full size, and 400 px keeps the percentile maths in microseconds.
MASK_EDGE = 400

# A blade thinner than this share of its own length is a cable or a kerb;
# thicker than the upper share is a wall or a sheet of paper.
MIN_THICKNESS_FRACTION = 0.012
MAX_THICKNESS_FRACTION = 0.45

# The canonical strip's height follows the blade's thickness, clamped, so a
# printed digit keeps its aspect ratio.
STRIP_MIN_HEIGHT = 64
STRIP_MAX_HEIGHT = 180
STRIP_MIN_WIDTH = 320
STRIP_MAX_WIDTH = 2400

# Bins used to fit the centre line and to measure how much of the blade length
# is really blade.
FIT_BINS = 24
COVER_BINS = 40


def _candidate_bands(gray: np.ndarray) -> list[tuple[int, int]]:
    """Rows of the bright bands worth testing.

    Reuses the reader's proven band search; the import stays inside the function
    because the reader calls back here for its diagnostics.
    """
    try:
        from supervision import tape_measure

        bands = tape_measure._tape_bands(Image.fromarray(gray), limit=4)
    except Exception:
        bands = []
    rows = [(int(band['top']), int(band['bottom'])) for band in bands]
    # The search prefers short spans of bright rows, so on a blade carrying a
    # blown highlight it can return the row of printed numbers and the row of
    # marks as two separate bands. Their union covers both, which is what the
    # strip needs: the ladder comes from the marks and the scale from the
    # numbers printed above them.
    if len(rows) >= 2:
        union = (min(top for top, _ in rows), max(bottom for _, bottom in rows))
        if union[1] > union[0] and union not in rows:
            rows.append(union)
    rows.append((0, gray.shape[0]))
    return rows


def _otsu(values: np.ndarray) -> float:
    """The gray level that splits the band into ground and blade.

    A local-contrast mask cannot work here: a blade's interior matches its own
    box-blurred background, so only the rim survives. A uniform band has no
    split at all, so it is sent above white and the mask comes back empty.
    """
    histogram = np.bincount(
        np.clip(values, 0, 255).astype(np.uint8).ravel(), minlength=256
    ).astype(np.float64)
    total = histogram.sum()
    if not total:
        return 255.0
    weights = np.cumsum(histogram)
    sums = np.cumsum(histogram * np.arange(256.0))
    mean_total = sums[-1] / total
    spread = (mean_total * weights - sums) ** 2 / np.maximum(
        weights * (total - weights), 1e-9
    )
    if not np.isfinite(spread).any() or float(spread.max()) <= 0.0:
        return 255.0
    return float(np.argmax(spread))


def _densest_window(mask: np.ndarray) -> slice | None:
    """The thinnest horizontal window that holds most of the mask.

    A blade is a band of roughly constant height; pale ground or a white patch
    beside it is not. Sliding a window 2.5x the median column extent and keeping
    the densest one cuts those neighbours away before the line fit sees them.
    """
    if not mask.any():
        return None
    extent = mask.sum(axis=0)
    used = extent[extent > 0]
    if used.size < 20:
        return None
    height = mask.shape[0]
    window = max(4, int(float(np.median(used)) * 2.5))
    if window >= height:
        return slice(0, height)
    row_totals = np.concatenate([[0], np.cumsum(mask.sum(axis=1))])
    totals = row_totals[window:] - row_totals[:-window]
    start = int(np.argmax(totals))
    return slice(start, start + window)


def _mask_for_band(base: np.ndarray, start: int, stop: int) -> np.ndarray | None:
    """The bright class of one candidate band, trimmed to its densest window."""
    keep = np.zeros(base.shape[0], dtype=bool)
    keep[start:stop] = True
    level = _otsu(base[keep, :])
    mask = np.zeros_like(base, dtype=bool)
    mask[keep, :] = base[keep, :] > level
    window = _densest_window(mask)
    if window is None:
        return None
    trimmed = np.zeros_like(mask)
    trimmed[window] = mask[window]
    return trimmed if int(trimmed.sum()) >= 200 else None


def _centre_line(u: np.ndarray, v: np.ndarray) -> tuple[float, float, float] | None:
    """(slope, intercept, half thickness) of the blade along u, in v units.

    Binned medians, not PCA alone: one bright patch of gravel drags a principal
    axis, while a median per bin along the blade ignores it. The thickness comes
    from the median absolute deviation, which for a uniform strip equals a
    quarter of its height, so it survives a few stray pixels where a percentile
    range would not.
    """
    order = np.argsort(u)
    u, v = u[order], v[order]
    edges = np.linspace(0, len(u), FIT_BINS + 1).astype(int)
    centres: list[float] = []
    middles: list[float] = []
    for start, stop in zip(edges, edges[1:]):
        if stop - start < 20:
            continue
        centres.append(float(np.median(u[start:stop])))
        middles.append(float(np.median(v[start:stop])))
    if len(centres) < 3:
        return None
    slope, intercept = np.polyfit(np.array(centres), np.array(middles), 1)
    residual = v - (slope * u + intercept)
    half_thickness = 2.0 * float(np.median(np.abs(residual - np.median(residual))))
    return float(slope), float(intercept), half_thickness


def _edge_lines(
    u: np.ndarray, v: np.ndarray, slope: float, intercept: float
) -> tuple[float, float, float, float, float] | None:
    """Lines along the blade's two long edges, and the mean gap between them.

    A tape's long edges are straight on the tape and stay straight in the photo,
    even when perspective makes them converge. Two lines fitted to each bin's
    spread therefore describe the blade's real outline, where a centre line plus
    a constant thickness could only ever describe one photographed square-on.

    The spread is a median plus or minus twice the median absolute deviation, not
    a percentile range: two MADs are exactly a uniform band's half height, and
    they ignore the stray bright pixel that a range would follow. A range also
    made the strip taller than the blade's body on the rendered photos, which
    crushed the printed numbers into the marks and cost the ladder.
    """
    residual = v - (slope * u + intercept)
    order = np.argsort(u)
    u, residual = u[order], residual[order]
    edges = np.linspace(0, len(u), FIT_BINS + 1).astype(int)
    centres: list[float] = []
    middles: list[float] = []
    spreads: list[float] = []
    for start, stop in zip(edges, edges[1:]):
        if stop - start < 20:
            continue
        window = residual[start:stop]
        middle = float(np.median(window))
        centres.append(float(np.median(u[start:stop])))
        middles.append(middle)
        spreads.append(2.0 * float(np.median(np.abs(window - middle))))
    if len(centres) < 3:
        return None

    centres_array = np.array(centres, dtype=float)
    middle_array = np.array(middles, dtype=float)
    spread_array = np.array(spreads, dtype=float)
    low_array = middle_array - spread_array
    high_array = middle_array + spread_array
    for _ in range(2):
        low_slope, low_intercept = np.polyfit(centres_array, low_array, 1)
        high_slope, high_intercept = np.polyfit(centres_array, high_array, 1)
        low_miss = np.abs(low_array - (low_slope * centres_array + low_intercept))
        high_miss = np.abs(high_array - (high_slope * centres_array + high_intercept))
        tolerance = 2.5 * max(
            float(np.median(low_miss)), float(np.median(high_miss)), 0.5
        )
        keep = (low_miss <= tolerance) & (high_miss <= tolerance)
        if keep.all() or int(keep.sum()) < 3:
            break
        centres_array, low_array, high_array = (
            centres_array[keep],
            low_array[keep],
            high_array[keep],
        )

    low_slope, low_intercept = np.polyfit(centres_array, low_array, 1)
    high_slope, high_intercept = np.polyfit(centres_array, high_array, 1)
    thickness = float(np.median(high_array - low_array))
    return (
        float(low_slope),
        float(low_intercept),
        float(high_slope),
        float(high_intercept),
        thickness,
    )


def _perspective_coefficients(
    corners: list[tuple[float, float]], width: int, height: int
) -> tuple[float, ...] | None:
    """PIL's eight coefficients pulling each strip pixel back onto the blade.

    ``corners`` run upper-left, upper-right, lower-right, lower-left and map onto
    the corners of a ``width`` by ``height`` rectangle. PIL wants the inverse map
    — strip pixel to frame pixel — so solving it in this direction makes the warp
    a single pass, with no intermediate image and no second resample.
    """
    blade = np.array(corners, dtype=float)
    strip = np.array(
        [
            [0.0, 0.0],
            [float(width - 1), 0.0],
            [float(width - 1), float(height - 1)],
            [0.0, float(height - 1)],
        ],
        dtype=float,
    )
    rows: list[list[float]] = []
    for (x, y), (screen_x, screen_y) in zip(blade, strip):
        rows.append(
            [screen_x, screen_y, 1.0, 0.0, 0.0, 0.0, -x * screen_x, -x * screen_y]
        )
        rows.append(
            [0.0, 0.0, 0.0, screen_x, screen_y, 1.0, -y * screen_x, -y * screen_y]
        )
    matrix = np.array(rows, dtype=float)
    if abs(float(np.linalg.det(matrix.T @ matrix))) < 1e-12:
        return None
    solution, *_ = np.linalg.lstsq(matrix, blade.ravel(), rcond=None)
    return tuple(float(value) for value in solution)


def _strip_from_mask(
    gray: np.ndarray, mask: np.ndarray, scale_x: float, scale_y: float
) -> dict | None:
    """Warp the masked strip into a level blade, or None if it is not one."""
    ys, xs = np.nonzero(mask)
    if len(ys) < 200:
        return None
    points = np.stack([xs, ys], axis=1).astype(np.float64)
    centre = points.mean(axis=0)
    centred = points - centre

    covariance = centred.T @ centred / max(1, len(points) - 1)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    axis = eigenvectors[:, int(np.argmax(eigenvalues))]
    if axis[0] < 0:
        axis = -axis
    base_angle = float(np.degrees(np.arctan2(axis[1], axis[0])))

    cos, sin = np.cos(np.radians(base_angle)), np.sin(np.radians(base_angle))
    u = centred[:, 0] * cos + centred[:, 1] * sin
    v = -centred[:, 0] * sin + centred[:, 1] * cos

    fit = _centre_line(u, v)
    if fit is None:
        return None
    slope, intercept, _half_thickness = fit

    edges = _edge_lines(u, v, slope, intercept)
    if edges is None:
        return None
    low_slope, low_intercept, high_slope, high_intercept, thickness = edges
    if thickness < 2.0:
        return None
    angle = base_angle + float(np.degrees(np.arctan((low_slope + high_slope) / 2.0)))

    u0, u1 = np.percentile(u, 1.0), np.percentile(u, 99.0)
    if u1 - u0 < 40:
        return None
    if not (
        MIN_THICKNESS_FRACTION <= thickness / (u1 - u0) <= MAX_THICKNESS_FRACTION
    ):
        return None

    low_0 = low_slope * u0 + low_intercept
    low_1 = low_slope * u1 + low_intercept
    high_0 = high_slope * u0 + high_intercept
    high_1 = high_slope * u1 + high_intercept
    # Edges that meet have no blade between them; that is a wedge, not a tape.
    if min(high_0 - low_0, high_1 - low_1) <= 1.0:
        return None

    def to_full(uu: float, vv: float) -> tuple[float, float]:
        return (
            (centre[0] + uu * cos - vv * sin) * scale_x,
            (centre[1] + uu * sin + vv * cos) * scale_y,
        )

    upper_left = to_full(u0, low_0)
    upper_right = to_full(u1, low_1)
    lower_right = to_full(u1, high_1)
    lower_left = to_full(u0, high_0)
    corners = [upper_left, upper_right, lower_right, lower_left]

    target_width = min(
        STRIP_MAX_WIDTH, max(STRIP_MIN_WIDTH, int(round((u1 - u0) * scale_x)))
    )
    target_height = min(
        STRIP_MAX_HEIGHT, max(STRIP_MIN_HEIGHT, int(round(thickness * scale_y)))
    )
    coefficients = _perspective_coefficients(corners, target_width, target_height)
    if coefficients is None:
        return None
    strip = Image.fromarray(gray).transform(
        (target_width, target_height),
        Image.Transform.PERSPECTIVE,
        coefficients,
        Image.Resampling.BICUBIC,
        fillcolor=255,
    )

    histogram, _ = np.histogram(
        u[(u >= u0) & (u <= u1)], bins=COVER_BINS, range=(u0, u1)
    )
    # PIL's own quad order is upper-left, lower-left, lower-right, upper-right;
    # `quad` keeps that shape for callers that still read it.
    quad = [upper_left, lower_left, lower_right, upper_right]
    return {
        'strip': strip,
        'angle': round(angle, 2),
        'quad': [round(value, 1) for corner in quad for value in corner],
        'corners': [[round(x, 1), round(y, 1)] for x, y in corners],
        'thickness_px': int(round(thickness * scale_y)),
        'width_px': target_width,
        'height_px': target_height,
        'coverage': round(float((histogram > 0).mean()), 2),
    }


def _grade_ticks(framed: dict) -> None:
    """Score this strip by the ladder it yields and keep that summary.

    The grade is what chooses between candidate bands, and it is also what the
    endpoint reports, so a field failure can say "blade found, marks uneven"
    rather than only "no reading".
    """
    try:
        from supervision import tape_ticks

        lattice = tape_ticks.read_strip(framed['strip'])
    except Exception:
        lattice = None
    if lattice is None:
        framed['lattice'] = None
        framed['ticks'] = None
        framed['tick_pitch'] = None
        framed['tick_residual'] = None
        framed['tick_score'] = 0.0
        framed['tick_usable'] = False
        return
    # Private to the caller: the mark positions are what the digit stage
    # aligns its cells to, and they are too numerous to ship in a response.
    framed['lattice'] = lattice
    framed['ticks'] = {
        'pitch_px': round(lattice['pitch'], 2),
        'residual': round(lattice['residual'], 3),
        'count': lattice['count'],
        'coverage': round(lattice['coverage'], 2),
        'major_every': lattice['major_every'],
        'usable': bool(lattice['usable']),
    }
    framed['tick_pitch'] = framed['ticks']['pitch_px']
    framed['tick_residual'] = framed['ticks']['residual']
    framed['tick_score'] = float(lattice.get('score') or 0.0)
    framed['tick_usable'] = bool(lattice['usable'])


def _rank(framed: dict | None) -> tuple:
    """A usable ladder beats an unusable one, then a taller strip, then tightness.

    Height carries real meaning for what comes next: a strip cut to the mark row
    alone reads its ticks perfectly and leaves the printed numbers above the marks
    outside the frame, where the number reader can never reach them. The blade is
    the strip, not its most regular sliver.

    A ladder therefore only ranks below another when it is materially worse: two
    strips whose residuals both sit well inside the reader's accept threshold tie
    on tightness and the taller one wins. Bucketing by a tenth of a pitch let a
    sliver at 0.02 outrank the whole blade at 0.05, and the blade's numbers then
    had nowhere to be read from.
    """
    if framed is None:
        return (-1, 0, -1, 0.0)
    raw = framed.get('tick_residual')
    residual = 9.0 if raw is None else float(raw)
    try:
        from supervision import tape_ticks

        good = float(tape_ticks.MAX_RESIDUAL) / 2.0
    except Exception:
        good = 0.09
    tightness = 0 if residual <= good else -round(residual, 1)
    return (
        1 if framed.get('tick_usable') else 0,
        tightness,
        int(framed.get('height_px') or 0),
        float(framed.get('tick_score') or 0.0),
    )


def _frame_once(gray: np.ndarray) -> dict | None:
    """The best blade strip in this orientation, or None."""
    height, width = gray.shape
    if min(height, width) < 60:
        return None

    ratio = MASK_EDGE / float(max(width, height))
    small = Image.fromarray(gray).resize(
        (max(1, int(width * ratio)), max(1, int(height * ratio))), Image.BILINEAR
    )
    small_width, small_height = small.size
    base = np.asarray(small, dtype=np.float32)
    scale_x = width / float(small_width)
    scale_y = height / float(small_height)

    best: dict | None = None
    for top, bottom in _candidate_bands(gray):
        start = max(0, int(top * small_height / height))
        stop = min(
            small_height,
            max(start + 1, int(np.ceil(bottom * small_height / height))),
        )
        if stop - start < 6:
            continue
        mask = _mask_for_band(base, start, stop)
        if mask is None:
            continue
        framed = _strip_from_mask(gray, mask, scale_x, scale_y)
        if framed is None:
            continue
        _grade_ticks(framed)
        if best is None or _rank(framed) > _rank(best):
            best = framed
    return best


def _from_transposed(found: dict, gray: np.ndarray) -> dict:
    """The strip of a transposed search, mapped back onto the real frame.

    The blade runs down the frame in a depth shot, so the corners come back with
    their axes swapped. Transposing each corner puts it back where it is in the
    photo while the blade's length still lies along the strip's width, and the
    same projective warp then reads the real frame.
    """
    corners = [(float(y), float(x)) for x, y in found['corners']]
    coefficients = _perspective_coefficients(
        corners, found['width_px'], found['height_px']
    )
    if coefficients is None:
        return found
    strip = Image.fromarray(gray).transform(
        (found['width_px'], found['height_px']),
        Image.Transform.PERSPECTIVE,
        coefficients,
        Image.Resampling.BICUBIC,
        fillcolor=255,
    )
    angle = float(
        np.degrees(
            np.arctan2(corners[1][1] - corners[0][1], corners[1][0] - corners[0][0])
        )
    )
    quad = [corners[0], corners[3], corners[2], corners[1]]
    return {
        **found,
        'strip': strip,
        'quad': [round(value, 1) for corner in quad for value in corner],
        'corners': [[round(x, 1), round(y, 1)] for x, y in corners],
        'angle': round(angle, 2),
    }


def frame_gray(gray: np.ndarray) -> dict | None:
    """The blade as a level strip, or None when no blade can be located."""
    if gray is None or getattr(gray, 'ndim', 0) != 2:
        return None
    gray = np.ascontiguousarray(gray.astype(np.uint8, copy=False))

    found = _frame_once(gray)
    if found is not None and found.get('tick_usable'):
        return found

    # A blade running down the frame — the depth shot, with the tape against a
    # trench wall — has no horizontal bright band for the band search to find.
    # The same search on the transposed frame does, and the strip is then warped
    # a quarter turn so its ladder still lies across the result.
    upright = _frame_once(np.ascontiguousarray(gray.T))
    if upright is None:
        return found
    mapped = _from_transposed(upright, gray)
    # A quarter turn is a rescue for a blade that runs down the frame, not a
    # competitor: it has to actually yield a ladder, or the horizontal result is
    # kept even when that result is imperfect. Without this, a horizontal blade
    # came back reported at 92 degrees because a transposed nonsense strip
    # happened to have a slightly smaller residual.
    if mapped.get('tick_usable'):
        return mapped if _rank(mapped) > _rank(found) else found
    return found if found is not None else mapped


def frame_image(image) -> dict | None:
    """The blade strip of a PIL image, or None."""
    if image is None:
        return None
    return frame_gray(np.asarray(image.convert('L')))


def frame_bytes(data: bytes) -> dict | None:
    """The blade strip of an uploaded image, or None when it is not one."""
    try:
        from PIL import ImageOps

        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('L')
    except Exception:
        return None
    return frame_image(image)
