"""Find the blade in a field photo and straighten it into a level strip.

Every later step wants the tape on a horizontal axis. A blade lying at 7 degrees
smears each tick across several image rows, which is why column profiles on the
raw photo found soil texture instead of marks (measured 2026-11-23: gap
histogram 3, 8, 9, 10, 16, 18, 30 … with a coefficient of variation of 1.24).
This module localises the blade, fits its centre line, and warps it into a
canonical strip so ticks, digits and the object's edges become one-dimensional
problems.

Two choices were forced by the field set:

* The band is picked by how *regular* a strip its content forms, not by
  brightness. The bright-sand photo put the first candidate band on sunlit
  gravel at the bottom of the frame, 200 px thick instead of the blade's 53.
* The mask is an Otsu split of that band, because a blade's flat middle matches
  its own blurred background and a local-contrast mask keeps only the rim.

Deliberately not a learned detector: a tape is the longest regular structure in
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

# A plausible blade occupies this share of its band's height. Thinner is a
# cable or a kerb, thicker is a wall or a sheet of paper.
BAND_MIN_EXTENT_SHARE = 0.04
BAND_MAX_EXTENT_SHARE = 0.62

# A blade thinner than this share of its own length is a cable; thicker than the
# upper share is a wall.
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
    """Rows of the bright bands worth testing, thinnest first.

    Reuses the reader's proven band search; the import stays inside the function
    because the reader calls back here for its diagnostics.
    """
    try:
        from supervision import tape_measure

        bands = tape_measure._tape_bands(Image.fromarray(gray), limit=4)
    except Exception:
        bands = []
    rows = [(int(band['top']), int(band['bottom'])) for band in bands]
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
    the densest one cuts those neighbours away before the line fit sees them —
    on the rendered blade that alone took the quad from 418 px thick to the
    blade's own 136.
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


def _band_quality(mask: np.ndarray, band_height: int) -> float:
    """How much this mask looks like one long strip of constant thickness.

    A blade gives the same number of bright rows in almost every column, so the
    per-column extent is large and even. Sunlit gravel gives a jagged extent and
    a patch of white paint gives a short one; both score low.
    """
    if not mask.any():
        return 0.0
    extent = mask.sum(axis=0)
    used = extent[extent > 0]
    if used.size < 20:
        return 0.0
    share = float(used.size) / mask.shape[1]
    mean = float(used.mean())
    if not (
        BAND_MIN_EXTENT_SHARE
        <= mean / max(1, band_height)
        <= BAND_MAX_EXTENT_SHARE
    ):
        return 0.0
    regularity = mean / (mean + float(used.std()))
    return share * regularity


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


def _frame_once(gray: np.ndarray) -> dict | None:
    """The blade as a level strip, searched in this orientation only."""
    if gray is None or getattr(gray, 'ndim', 0) != 2:
        return None
    gray = np.ascontiguousarray(gray.astype(np.uint8, copy=False))
    height, width = gray.shape
    if min(height, width) < 60:
        return None

    ratio = MASK_EDGE / float(max(width, height))
    small = Image.fromarray(gray).resize(
        (max(1, int(width * ratio)), max(1, int(height * ratio))), Image.BILINEAR
    )
    small_width, small_height = small.size
    base = np.asarray(small, dtype=np.float32)

    best_mask = None
    best_quality = 0.0
    for top, bottom in _candidate_bands(gray):
        keep = np.zeros(small_height, dtype=bool)
        start = max(0, int(top * small_height / height))
        stop = min(small_height, max(start + 1, int(np.ceil(bottom * small_height / height))))
        if stop - start < 6:
            continue
        keep[start:stop] = True

        level = _otsu(base[keep, :])
        mask = np.zeros_like(base, dtype=bool)
        mask[keep, :] = base[keep, :] > level
        # Pale ground beside the blade passes the same Otsu split, so keep only
        # the densest horizontal window before measuring anything from it.
        window = _densest_window(mask)
        if window is None:
            continue
        trimmed = np.zeros_like(mask)
        trimmed[window] = mask[window]
        quality = _band_quality(trimmed, window.stop - window.start)
        if quality > best_quality:
            best_quality, best_mask = quality, trimmed
    if best_mask is None or best_quality <= 0.0:
        return None

    ys, xs = np.nonzero(best_mask)
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
    slope, intercept, half_thickness = fit
    if half_thickness < 1.0:
        return None
    angle = base_angle + float(np.degrees(np.arctan(slope)))

    u0, u1 = np.percentile(u, 1.0), np.percentile(u, 99.0)
    if u1 - u0 < 40:
        return None
    if not (
        MIN_THICKNESS_FRACTION
        <= (half_thickness * 2.0) / (u1 - u0)
        <= MAX_THICKNESS_FRACTION
    ):
        return None

    def to_small(uu: float, vv: float) -> tuple[float, float]:
        return (
            float(centre[0] + uu * cos - vv * sin),
            float(centre[1] + uu * sin + vv * cos),
        )

    # PIL's QUAD order is upper-left, lower-left, lower-right, upper-right, and
    # `v` grows downwards, so "upper" is the negative side of the centre line.
    quad_small = [
        to_small(u0, slope * u0 + intercept - half_thickness),
        to_small(u0, slope * u0 + intercept + half_thickness),
        to_small(u1, slope * u1 + intercept + half_thickness),
        to_small(u1, slope * u1 + intercept - half_thickness),
    ]
    scale_x = width / float(small_width)
    scale_y = height / float(small_height)
    quad = [coordinate for x, y in quad_small for coordinate in (x * scale_x, y * scale_y)]

    target_width = min(
        STRIP_MAX_WIDTH, max(STRIP_MIN_WIDTH, int(round((u1 - u0) * scale_x)))
    )
    target_height = min(
        STRIP_MAX_HEIGHT,
        max(STRIP_MIN_HEIGHT, int(round(half_thickness * 2.0 * scale_y))),
    )
    strip = Image.fromarray(gray).transform(
        (target_width, target_height),
        Image.Transform.QUAD,
        quad,
        Image.Resampling.BICUBIC,
    )

    histogram, _ = np.histogram(
        u[(u >= u0) & (u <= u1)], bins=COVER_BINS, range=(u0, u1)
    )
    return {
        'strip': strip,
        'angle': round(angle, 2),
        'quad': [round(value, 1) for value in quad],
        'thickness_px': int(round(half_thickness * 2.0 * scale_y)),
        'width_px': target_width,
        'height_px': target_height,
        'coverage': round(float((histogram > 0).mean()), 2),
        'band_quality': round(best_quality, 3),
    }


def _from_transposed(found: dict, gray: np.ndarray) -> dict:
    """The strip of a transposed search, mapped back onto the real frame.

    The blade runs down the frame in a depth shot, so the corners come back
    swapped; a quarter-turn ordering keeps the ladder lying across the strip.
    """
    quad = found['quad']
    points = [(quad[index], quad[index + 1]) for index in range(0, 8, 2)]
    mapped = [(y, x) for x, y in points]
    # The transposed corners already run along the blade's length, which PIL
    # maps onto the strip's width, so no reordering is needed here.
    order = mapped
    flat = [coordinate for point in order for coordinate in point]
    strip = Image.fromarray(gray).transform(
        (found['width_px'], found['height_px']),
        Image.Transform.QUAD,
        flat,
        Image.Resampling.BICUBIC,
    )
    angle = float(
        np.degrees(np.arctan2(order[3][1] - order[0][1], order[3][0] - order[0][0]))
    )
    return {
        **found,
        'strip': strip,
        'quad': [round(value, 1) for value in flat],
        'angle': round(angle, 2),
    }


def frame_gray(gray: np.ndarray) -> dict | None:
    """The blade as a level strip, or None when no blade can be located."""
    if gray is None or getattr(gray, 'ndim', 0) != 2:
        return None
    gray = np.ascontiguousarray(gray.astype(np.uint8, copy=False))
    found = _frame_once(gray)
    if found is not None:
        return found
    # A blade running down the frame — the depth shot, with the tape against a
    # trench wall — has no horizontal bright band for the band search to find.
    # The same search on the transposed frame does, and the strip is then warped
    # a quarter turn so its ladder still lies across the result.
    upright = _frame_once(np.ascontiguousarray(gray.T))
    return None if upright is None else _from_transposed(upright, gray)


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
