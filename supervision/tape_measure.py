"""Read the numbers printed on a tape measure and turn them into one reading.

A tape is already a scale, so this path needs no known-size object and no
camera calibration: the digits on the blade *are* the distance. The OCR pass
returns only the numbers it can see and where they sit along the blade; the
arithmetic here keeps the contiguous run and subtracts the first reading from
the last — the comparison a surveyor makes by eye when the tape runs across a
trench or down its wall.

Nothing is written here. The endpoint hands the reading to the app, which fills
the span form with it; the engineer confirms it against the tape and saves.

Tesseract is resolved from ``PATH`` through ``pytesseract``. When it is absent
the endpoint says so instead of guessing, so a host without the OCR package can
never look like a host that read the tape and disagreed.

Env: none. Tesseract needs no key.
"""

from __future__ import annotations

import io
import statistics
import time

import numpy as np

# Uploads are shrunk to this longest edge before OCR. Bigger costs time without
# making a printed digit easier to read — but a blurred field photo does lose
# its run at 1280, so this stays at 1600.
TAPE_MAX_EDGE = 1600

# pytesseract reports confidence 0–100, and a digit it is unsure about is worse
# than no digit: the reading would move the measured span.
MIN_OCR_CONFIDENCE = 55

# A field photo is rarely as clean as a test card. A digit tesseract is only
# half sure of is still useful when it lands on the same x as the same value
# from another equalisation, and `_run_score` penalises the weak ones that
# never find that support.
LOW_OCR_CONFIDENCE = 35

# The two numbers that bound the span are the whole reading. On a real field
# photo a date stamp read at 0.96 next to a smudge at 0.41 became a 2 m trench
# depth, so an endpoint has to be read, not guessed — a digit with support from
# another equalisation clears this, an isolated one does not.
SPAN_MIN_CONFIDENCE = 0.6

# Gray-level step (0–255) between the two sides of a boundary before it counts
# as the end of the trench or duct rather than a stone or a shadow.
EDGE_MIN_STEP = 12.0

# `--oem 1` (LSTM only) reads a printed digit in about half the time of the
# default engine. ``psm 6`` treats the crop as one block of text, which is what
# a blade is; ``psm 11`` finds scattered words, which is what is left when dust,
# shadow or a gap between the marks breaks the blade into islands.
DENSE_OCR_CONFIG = '--oem 1 --psm 6'
SPARSE_OCR_CONFIG = '--oem 1 --psm 11'

# One tesseract run is a subprocess with its own timeout. Without it a single
# wedged process holds the request open forever, which is exactly what a
# 0.1-CPU host does under load.
OCR_PASS_TIMEOUT = 25

# Wall-clock budgets for the whole read. The free host runs the same passes
# 10–40x slower than a laptop, so a full sweep of every crop, equalisation and
# quarter turn can outlast the phone's patience; past the soft budget the
# reader returns the run it already has. Past the hard one it stops even with
# nothing, because a photo with no readable blade must still answer.
TAPE_OCR_BUDGET_S = 55.0
TAPE_OCR_HARD_BUDGET_S = 80.0

# A handheld photo is rarely level, and tesseract's accuracy falls off well
# before 10 degrees of tilt. The blade's angle is measured on a thumbnail first
# and the frame turned upright when it is worth it.
ANGLE_SEARCH_DEGREES = (-20, -15, -11, -8, -5, -3, 0, 3, 5, 8, 11, 15, 20)
ANGLE_MIN_DEGREES = 2.0

# Above this share of near-white pixels the crop carries glare, and below this
# Laplacian variance it is out of focus — both are worth telling the engineer
# instead of only 'nothing could be read'.
GLARE_SHARE = 0.06
SHARPNESS_MIN = 12.0

# A real tape number is bigger than this and no tape is longer than that.
MIN_READING_MM = 30
MAX_READING_MM = 30000

TAPE_UNIT_MM = {'cm': 10.0, 'mm': 1.0}

# One entry per dimension the engineer can photograph the tape against.
# min/max are the plausible as-built ranges: they catch a misread (a "170" read
# as "70" turns a 5 m length into 1 m) without pretending to know the design.
DIMENSION_SPECS: dict[str, list[dict]] = {
    'trench': [
        {
            'id': 'depth',
            'label': 'Actual depth',
            'key': 'actual_depth_mm',
            'unit': 'mm',
            'min_mm': 150.0,
            'max_mm': 5000.0,
        },
        {
            'id': 'width',
            'label': 'Actual width',
            'key': 'actual_width_mm',
            'unit': 'mm',
            'min_mm': 100.0,
            'max_mm': 3000.0,
        },
        {
            'id': 'length',
            'label': 'Measured length',
            'key': 'measured_length_m',
            'unit': 'm',
            'min_mm': 300.0,
            'max_mm': 500000.0,
        },
    ],
    'duct': [
        {
            'id': 'outer_diameter',
            'label': 'Duct outer diameter',
            'key': 'actual_outer_diameter_mm',
            'unit': 'mm',
            'min_mm': 10.0,
            'max_mm': 500.0,
        },
        {
            'id': 'installed_depth',
            'label': 'Installed depth',
            'key': 'actual_installed_depth_mm',
            'unit': 'mm',
            'min_mm': 200.0,
            'max_mm': 6000.0,
        },
    ],
    'cable': [
        {
            'id': 'outer_diameter',
            'label': 'Cable outer diameter',
            'key': 'actual_outer_diameter_mm',
            'unit': 'mm',
            'min_mm': 5.0,
            'max_mm': 100.0,
        },
    ],
    'chamber': [
        {
            'id': 'opening',
            'label': 'Chamber opening',
            'key': 'actual_chamber_opening_mm',
            'unit': 'mm',
            'min_mm': 200.0,
            'max_mm': 2000.0,
        },
        {
            'id': 'depth',
            'label': 'Chamber depth',
            'key': 'actual_chamber_depth_mm',
            'unit': 'mm',
            'min_mm': 300.0,
            'max_mm': 5000.0,
        },
    ],
    'pole': [
        {
            'id': 'height',
            'label': 'Pole height',
            'key': 'actual_pole_height_mm',
            'unit': 'mm',
            'min_mm': 2000.0,
            'max_mm': 20000.0,
        },
        {
            'id': 'diameter',
            'label': 'Pole base diameter',
            'key': 'actual_pole_diameter_mm',
            'unit': 'mm',
            'min_mm': 50.0,
            'max_mm': 1000.0,
        },
    ],
}

# OCR commonly confuses these with digits. Mapping them before the digits are
# extracted costs nothing when tesseract was right and rescues a reading when it
# was not.
_LOOKALIKES = str.maketrans(
    {
        'O': '0',
        'o': '0',
        'D': '0',
        'l': '1',
        'I': '1',
        '|': '1',
        'Z': '2',
        'S': '5',
        'B': '8',
        'G': '6',
        'g': '9',
    }
)


def dimension_spec(element_type: str, dimension: str) -> dict | None:
    """The dimension's field mapping, or None when the element has no such dimension."""
    for spec in DIMENSION_SPECS.get(str(element_type or '').strip().lower(), []):
        if spec['id'] == str(dimension or '').strip().lower():
            return spec
    return None


def _pytesseract():
    """The OCR module when its binary is usable, otherwise None."""
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return pytesseract
    except Exception:
        return None


def ocr_available() -> bool:
    return _pytesseract() is not None


def _prepare(data: bytes):
    """A grayscale copy small enough to OCR quickly, or None for a bad upload."""
    try:
        from PIL import Image, ImageOps

        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image)
        image = image.convert('L')
        image.thumbnail((TAPE_MAX_EDGE, TAPE_MAX_EDGE))
        return image
    except Exception:
        return None


def _blade_angle(image) -> float:
    """How far the blade is tilted from level, in degrees.

    A level blade is one sharp step up and down in the row brightness. Rotating
    a small copy to each candidate angle and keeping the one with the strongest
    step finds the tilt without needing to read a single digit.
    """
    from PIL import Image

    small = image.copy()
    small.thumbnail((420, 420))
    best_angle, best_score = 0.0, 0.0
    for angle in ANGLE_SEARCH_DEGREES:
        rotated = (
            small.rotate(angle, resample=Image.Resampling.BILINEAR, expand=True)
            if angle
            else small
        )
        rows = np.asarray(rotated, dtype=np.float32).mean(axis=1)
        if rows.size < 3:
            continue
        rows = np.convolve(rows, np.ones(3) / 3.0, mode='same')
        score = float(np.abs(np.diff(rows)).max())
        if score > best_score:
            best_angle, best_score = float(angle), score
    if abs(best_angle) < ANGLE_MIN_DEGREES or best_score < 6.0:
        return 0.0
    return best_angle


def _upright(image, angle: float):
    """The frame turned so the blade lies level, padded with its own gray."""
    from PIL import Image

    if not angle:
        return image
    fill = int(np.median(np.asarray(image)))
    return image.rotate(
        angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fill
    )


def _quality(image) -> dict:
    """The cheap reasons a photo will not read: glare and focus."""
    pixels = np.asarray(image, dtype=np.float32)
    glare = float((pixels >= 245).mean()) if pixels.size else 0.0
    sharpness = 0.0
    if pixels.ndim == 2 and pixels.shape[0] > 2 and pixels.shape[1] > 2:
        centre = pixels[1:-1, 1:-1]
        laplacian = (
            pixels[:-2, 1:-1] + pixels[2:, 1:-1] + pixels[1:-1, :-2] + pixels[1:-1, 2:]
        ) - 4.0 * centre
        sharpness = float(laplacian.var())
    return {'glare': round(glare, 3), 'sharpness': round(sharpness, 1)}


def retake_reasons(diagnostics: dict | None) -> list[str]:
    """What to tell the engineer when the blade could not be read."""
    diagnostics = diagnostics or {}
    reasons: list[str] = []
    if float(diagnostics.get('glare') or 0) >= GLARE_SHARE:
        reasons.append('glare is washing out part of the blade')
    if diagnostics.get('sharpness') is not None and float(diagnostics['sharpness']) <= SHARPNESS_MIN:
        reasons.append('the blade looks out of focus')
    return reasons


def _tape_bands(image, limit: int = 2) -> list[dict]:
    """The brightest long bands in the frame, best first.

    A tape is a bright, low-variance strip running across the photo. Cropping to
    it before OCR removes the trench walls, the site clutter and the timestamp
    burn-in that otherwise become candidate numbers. More than one band is kept
    because a white site hut, a sheet of paper or a sunlit path can outshine the
    blade, and the reader scores whichever band actually yields a tape run.
    """
    small = image.copy()
    small.thumbnail((320, 320))
    width, height = small.size
    if width < 8 or height < 8:
        return []
    pixels = small.load()
    bright_rows = []
    for y in range(height):
        bright_rows.append(sum(1 for x in range(width) if pixels[x, y] > 145) / width)

    step = max(1, height // 40)
    shortest = max(4, height // 10)
    longest = max(shortest + 1, int(height * 0.45))
    candidates = []
    for span in range(shortest, longest, step):
        for top in range(0, height - span + 1, step):
            coverage = sum(bright_rows[top : top + span]) / span
            if coverage >= 0.45:
                candidates.append((coverage, span, top))
    if not candidates:
        return []
    candidates.sort(reverse=True)

    scale = image.height / height
    bands: list[dict] = []
    for coverage, span, top in candidates:
        # One band per stretch of the frame: two crops of the same blade would
        # only add OCR time.
        if any(abs(top - band['small_top']) < max(span, band['small_span']) for band in bands):
            continue
        pad = max(2, span // 8)
        crop_top = max(0, top - pad)
        crop_bottom = min(height, top + span + 2 * pad)
        band_top = int(crop_top * scale)
        band_bottom = min(image.height, int(crop_bottom * scale))
        bands.append(
            {
                'small_top': top,
                'small_span': span,
                'coverage': coverage,
                'top': band_top,
                'bottom': band_bottom,
                'crop': image.crop((0, band_top, image.width, band_bottom)),
            }
        )
        if len(bands) >= limit:
            break
    return bands


def _tape_strip(image):
    """The brightest long band in the frame, or None when no blade stands out."""
    bands = _tape_bands(image, limit=1)
    return bands[0]['crop'] if bands else None


def _digits(raw) -> str:
    text = str(raw or '').translate(_LOOKALIKES)
    text = ''.join(character for character in text if character.isdigit())
    # Up to five digits is one printed number; longer is either a blob of glued
    # numbers (see `_split_merged`) or noise, and is sorted out by the caller.
    return text if 1 <= len(text) <= 14 else ''


def _split_merged(text: str) -> list[int]:
    """One glued OCR word split back into the numbers printed on the blade.

    Tesseract returns ``102030405060`` for six separate marks when the digits
    touch, and a single 0-confidence word is unusable. Splitting it into
    equal-width groups that step regularly recovers the run; anything that does
    not decompose that way is left alone.
    """
    if not text.isdigit() or len(text) <= 5:
        return []
    for width in (3, 2):
        if len(text) % width or len(text) // width < 3:
            continue
        groups = [int(text[index : index + width]) for index in range(0, len(text), width)]
        gaps = [later - earlier for earlier, later in zip(groups, groups[1:])]
        if any(gap <= 0 for gap in gaps):
            continue
        step = statistics.median(gaps)
        if all(abs(gap - step) <= step * 0.2 for gap in gaps):
            return groups
    return []


def _variants(image) -> list[tuple[str, object]]:
    """The same blade under a few equalisations, cheapest first.

    A field photo arrives with glare, shadow, a dusty blade or the wrong
    exposure, and a digit that is unreadable in the raw crop often comes back
    after autocontrast or an upscale with an unsharp mask. The reader takes the
    best run across the variants, so a bad equalisation costs nothing.
    """
    from PIL import ImageFilter, ImageOps

    variants: list[tuple[str, object]] = [('raw', image)]
    try:
        variants.append(('auto', ImageOps.autocontrast(image)))
    except Exception:
        pass
    try:
        # Only the strip, not a full frame: a 2x upscale of a 1600 px photo is
        # three times the pixels for digits that are already large enough.
        if image.width <= 900:
            from PIL import Image as PILImage

            large = image.resize(
                (image.width * 2, image.height * 2), PILImage.Resampling.LANCZOS
            )
            variants.append(
                (
                    'sharp',
                    large.filter(
                        ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
                    ),
                )
            )
    except Exception:
        pass
    try:
        variants.append(('binary', _binarized(image)))
    except Exception:
        pass
    try:
        variants.append(('digits', _digit_strip(image)))
    except Exception:
        pass
    return variants


def _otsu(image) -> int:
    """The gray level that best separates this crop's two populations."""
    histogram = image.histogram()
    total = sum(histogram)
    if not total:
        return 128
    weighted = sum(level * count for level, count in enumerate(histogram))
    background, background_sum = 0, 0.0
    best_variance, threshold = -1.0, 128
    for level, count in enumerate(histogram):
        background += count
        foreground = total - background
        if not background or not foreground:
            continue
        background_sum += level * count
        mean_back = background_sum / background
        mean_front = (weighted - background_sum) / foreground
        variance = background * foreground * (mean_back - mean_front) ** 2
        if variance > best_variance:
            best_variance, threshold = variance, level
    return threshold


def _binarized(image):
    """Black digits on a white blade, thresholded tile by tile.

    Sun on the near end and shadow on the far end of one blade is the normal
    case in a trench, and a single threshold for the whole crop loses one end
    or the other. Each tile picks its own level and the pieces are stitched
    back together.
    """
    width, height = image.size
    columns, rows = 4, 3
    cleaned = image.copy()
    for row in range(rows):
        top = height * row // rows
        bottom = height * (row + 1) // rows
        for column in range(columns):
            left = width * column // columns
            right = width * (column + 1) // columns
            if right <= left or bottom <= top:
                continue
            tile = image.crop((left, top, right, bottom))
            cut = _otsu(tile)
            cleaned.paste(
                tile.point(lambda value, level=cut: 255 if value > level else 0),
                (left, top),
            )
    return cleaned


def _digit_strip(image):
    """The strip with the non-numeric ink whited out.

    On a tape the marks are the only ink in a regular row, so the column ink
    density finds each mark. Dust on the blade, the branding printed along its
    edge and the shadow underneath are removed before tesseract reads it, which
    is what keeps a dirty field photo readable.
    """
    from PIL import ImageDraw

    ink = (np.asarray(_binarized(image), dtype=np.uint8) < 128).sum(axis=0)
    if not ink.any():
        return image
    cut = max(1.0, float(ink.max()) * 0.15)
    spans: list[tuple[int, int]] = []
    start = None
    for x, value in enumerate(ink):
        if value >= cut and start is None:
            start = x
        elif value < cut and start is not None:
            spans.append((start, x))
            start = None
    if start is not None:
        spans.append((start, len(ink)))
    keeps = [span for span in spans if 4 <= span[1] - span[0] <= image.width * 0.1]
    if len(keeps) < 2:
        return image
    cleaned = image.copy()
    draw = ImageDraw.Draw(cleaned)
    previous = 0
    for left, right in keeps:
        if left > previous:
            draw.rectangle((previous, 0, left, image.height), fill=255)
        previous = right
    if previous < image.width:
        draw.rectangle((previous, 0, image.width, image.height), fill=255)
    return cleaned


def _cluster_tokens(tokens: list[dict], tolerance: float = 0.02) -> list[dict]:
    """One token per position along the blade, from every variant that saw it.

    Two equalisations that agree on a value at the same place are worth more
    than either alone; one that reads something else there votes against
    itself and the confident majority wins.
    """
    ordered = sorted(tokens, key=lambda token: token['x'])
    clusters: list[list[dict]] = []
    for token in ordered:
        if clusters and abs(token['x'] - clusters[-1][-1]['x']) <= tolerance:
            clusters[-1].append(token)
        else:
            clusters.append([token])

    merged: list[dict] = []
    for rows in clusters:
        best = max(rows, key=lambda row: row['confidence'])
        agreeing = [row for row in rows if row['value'] == best['value']]
        merged.append(
            {
                'value': best['value'],
                'x': sum(row['x'] for row in agreeing) / len(agreeing),
                'y': sum(row.get('y', 0.5) for row in agreeing) / len(agreeing),
                'confidence': min(1.0, best['confidence'] + 0.05 * (len(agreeing) - 1)),
            }
        )
    return merged


def _smoothed(values: list[float], radius: int = 2) -> list[float]:
    if radius <= 0 or not values:
        return list(values)
    return [
        sum(values[max(0, index - radius) : index + radius + 1])
        / len(values[max(0, index - radius) : index + radius + 1])
        for index in range(len(values))
    ]


def _steps(profile: list[float], window: int) -> list[float]:
    """Gray-level change at every column, measured over a window each side."""
    count = len(profile)
    prefix = [0.0]
    for value in profile:
        prefix.append(prefix[-1] + value)

    def mean(start: int, end: int):
        start, end = max(0, min(count, start)), max(0, min(count, end))
        return (prefix[end] - prefix[start]) / (end - start) if end > start else None

    steps = []
    for x in range(count):
        left, right = mean(x - window, x), mean(x, x + window)
        steps.append(abs(right - left) if left is not None and right is not None else 0.0)
    return steps


def _peaks(values: list[float], min_gap: int, threshold: float) -> list[int]:
    """The strongest separated local maxima, left to right."""
    candidates = [
        (values[x], x)
        for x in range(1, len(values) - 1)
        if values[x] >= threshold and values[x] >= values[x - 1] and values[x] >= values[x + 1]
    ]
    candidates.sort(reverse=True)
    chosen: list[int] = []
    for _score, x in candidates:
        if all(abs(x - other) >= min_gap for other in chosen):
            chosen.append(x)
        if len(chosen) == 4:
            break
    return sorted(chosen)


def _blade_rows(image, centre: float, fallback: dict) -> dict:
    """The blade's own rows, from how bright each row is.

    A bright blade on darker ground is a step up and then a step down in the row
    means, and those two rows bracket the digits. Without them the edge search
    samples rows that are still on the blade, so it never sees the ground the
    blade crosses and can never find the object's edge.
    """
    try:
        small = image.copy()
        small.thumbnail((360, 1200))
        width, height = small.size
        if width < 8 or height < 24:
            return fallback
        pixels = small.load()
        stride = max(1, width // 180)
        columns = range(0, width, stride)
        means = [
            sum(pixels[x, y] for x in columns) / len(columns) for y in range(height)
        ]
        steps = _steps(_smoothed(means), max(2, height // 50))
        peaks = _peaks(steps, max(3, height // 40), EDGE_MIN_STEP)
        middle = centre * (height - 1)
        above = [row for row in peaks if row < middle - 2]
        below = [row for row in peaks if row > middle + 2]
        if not above or not below:
            return fallback
        # Outermost, not nearest: the blade's own edges are the first and last
        # steps, while the rows between them belong to the numbers printed on
        # it. A band made of the inner pair sits on the blade and sees nothing.
        scale = image.height / height
        band = {'top': int(min(above) * scale), 'bottom': int(max(below) * scale)}
        if band['bottom'] - band['top'] < 4:
            return fallback
        if band['bottom'] - band['top'] > image.height * 0.85:
            return fallback
        return band
    except Exception:
        return fallback


def _band_for(crop, run: list[dict], rotation: int = 0) -> dict:
    """The rows the blade occupies, from the strip if it found one.

    When no bright band stood out (a dirty blade, a dull day, a wide shot) the
    digits say roughly where the blade is and the row brightness says how thick
    it is, so the rows above and below can still be searched for the edge.
    """
    if crop.get('small_top') is not None:
        return {'top': crop['top'], 'bottom': crop['bottom']}
    image = crop['crop']
    height = image.height
    # A half-turned crop reports its rows upside down; the blade is not.
    ys = [
        (1 - token.get('y', 0.5)) if rotation == 180 else token.get('y', 0.5)
        for token in run
    ] or [0.5]
    centre = statistics.median(ys)
    spread = max(0.04, (max(ys) - min(ys)) * 0.8)
    fallback = {
        'top': max(0, int((centre - spread) * height)),
        'bottom': min(height, int((centre + spread) * height)),
    }
    return _blade_rows(image, centre, fallback)


def _drop_outliers(run: list[dict]) -> list[dict]:
    """End the run where the pitch breaks, not at the furthest number found.

    A blade prints 10, 20, 30 … at a steady pitch, so a jump several times that
    pitch is a street number or a depth stamp caught by the same pass. Genuine
    skipped marks stay: their gap is a small multiple of the pitch.
    """
    if len(run) < 3:
        return run
    gaps = [later['value'] - earlier['value'] for earlier, later in zip(run, run[1:])]
    pitch = statistics.median(gaps)
    if pitch <= 0:
        return run
    kept = [run[0]]
    for token, gap in zip(run[1:], gaps):
        if gap > pitch * 4:
            break
        kept.append(token)
    return kept


def _object_bounds(image, band, rotation: int = 0) -> dict | None:
    """Where the object the blade crosses ends, as x fractions along the blade.

    The blade runs past the trench wall or the duct rim, so the numbers beyond
    the object are printed on ground that is not being measured. The rows just
    above and just below the blade carry that boundary: at the object's edge
    both change at the same x, while a passing stone or a shadow shows on one
    side only. Two such edges bound the measurement; when only texture noise is
    found the reading is left whole.
    """
    # A quarter turn puts the blade on its side, so "along the blade" is no
    # longer the image's x axis and these profiles mean nothing.
    if rotation in (90, 270):
        return None
    try:
        small = image.copy()
        small.thumbnail((360, 2000))
        width, height = small.size
        if width < 16 or height < 8:
            return None
        # The strip search can hand back rows from inside a tall blade, and a
        # band that sits on the blade samples the blade's own brightness, where
        # the object's edge is invisible. Widen it to the blade's own edges.
        band = _blade_rows(
            image,
            (band['top'] + band['bottom']) / 2 / max(1, image.height),
            band,
        )
        scale = height / image.height
        top, bottom = band['top'] * scale, band['bottom'] * scale
        span = max(2.0, bottom - top)
        above = (max(0.0, top - span * 0.7), max(1.0, top - span * 0.1))
        below = (min(height - 1.0, bottom + span * 0.1), min(float(height), bottom + span * 0.7))
        if above[1] - above[0] < 1 or below[1] - below[0] < 1:
            return None

        pixels = small.load()

        def profile(bounds) -> list[float]:
            start = int(bounds[0])
            end = max(start + 1, int(bounds[1]))
            rows = range(start, min(end, height))
            if not rows:
                return [0.0] * width
            return [sum(pixels[x, y] for y in rows) / len(rows) for x in range(width)]

        window = max(2, width // 40)
        above_steps = _steps(_smoothed(profile(above)), window)
        below_steps = _steps(_smoothed(profile(below)), window)
        # Both sides have to agree: one band changing alone is clutter, not an edge.
        combined = [min(a, b) for a, b in zip(above_steps, below_steps)]
        peaks = _peaks(combined, max(3, width // 30), EDGE_MIN_STEP)
        if len(peaks) < 2:
            return None
        left = peaks[0] / (width - 1)
        right = peaks[-1] / (width - 1)
        if right - left < 0.25:
            return None
        if rotation == 180:
            # The numbers were read off a half-turned crop, so their x runs the
            # other way; mirror the bounds to match them.
            left, right = 1 - right, 1 - left
        return {'left': round(left, 3), 'right': round(right, 3)}
    except Exception:
        return None


def _clip_to_bounds(run: list[dict], bounds: dict | None) -> list[dict]:
    """Drop the tape numbers printed past the detected end of the object.

    Only applied when it leaves a usable run: a false edge must never turn a
    good reading into no reading.
    """
    if not bounds or len(run) < 3:
        return run
    inside = [
        token
        for token in run
        if bounds['left'] - 0.02 <= token['x'] <= bounds['right'] + 0.02
    ]
    return inside if len(inside) >= 2 else run


def _ocr_readings(image, rotation: int, config: str = DENSE_OCR_CONFIG) -> list[dict]:
    """Digit tokens in one rotation of an image: value, x (0–1), confidence."""
    pytesseract = _pytesseract()
    if pytesseract is None:
        return []
    rotated = image.rotate(rotation, expand=True) if rotation else image
    try:
        # No `tessedit_char_whitelist` here: with it Tesseract glues every
        # number on the blade into one word ("102030405060708") with confidence
        # 0, which no run can use. Without it the same crop comes back as
        # separate 95%-confident words, and `_digits` still drops anything that
        # is not a number.
        data = pytesseract.image_to_data(
            rotated,
            config=config,
            output_type=pytesseract.Output.DICT,
            timeout=OCR_PASS_TIMEOUT,
        )
    except Exception:
        return []

    width = max(1, rotated.width)
    tokens: list[dict] = []
    for index, raw in enumerate(data.get('text') or []):
        text = _digits(raw)
        if not text:
            continue
        try:
            confidence = float(data['conf'][index])
        except (KeyError, TypeError, ValueError):
            continue
        if confidence < LOW_OCR_CONFIDENCE:
            continue
        left = float(data['left'][index])
        token_width = float(data['width'][index])
        groups = _split_merged(text) or [int(text)]
        for position, value in enumerate(groups):
            # Zero is legitimate: it is the tape's own datum, and a measurement
            # that starts there would otherwise be short by that distance.
            if not (0 <= value <= MAX_READING_MM):
                continue
            if len(groups) == 1:
                x = left + token_width / 2
            else:
                # Glued numbers sit evenly across the blob they came from.
                x = left + token_width * (position + 0.5) / len(groups)
            tokens.append(
                {
                    'value': value,
                    'x': x / width,
                    'y': (float(data['top'][index]) + float(data['height'][index]) / 2)
                    / max(1, rotated.height),
                    'height': float(data['height'][index]) / max(1, rotated.height),
                    'confidence': min(1.0, confidence / 100.0),
                }
            )
    return tokens


def _reading_run(tokens: list[dict]) -> list[dict]:
    """The longest left-to-right run of strictly increasing numbers.

    A tape prints 100, 110, 120 … in order, so the run is what makes a set of
    digits a tape rather than a mix of a depth stamp, a date and a duct size.
    """
    ordered = sorted(tokens, key=lambda token: token['x'])
    best: list[dict] = []
    for start, token in enumerate(ordered):
        run = [token]
        for candidate in ordered[start + 1 :]:
            if candidate['value'] > run[-1]['value'] and candidate['x'] > run[-1]['x'] + 0.005:
                run.append(candidate)
        if len(run) > len(best):
            best = run
    return best


def _run_score(run: list[dict]) -> float:
    """Longer, more regular, more confident runs win."""
    if len(run) < 2:
        return 0.0
    gaps = [later['value'] - earlier['value'] for earlier, later in zip(run, run[1:])]
    step = statistics.median(gaps)
    if step <= 0:
        return 0.0
    irregular = sum(1 for gap in gaps if abs(gap - step) > step * 0.6)
    mean_confidence = sum(token['confidence'] for token in run) / len(run)
    # A weak digit only helps when another variant read the same value there;
    # on its own it is as likely to be a stone as a tape mark.
    weak = sum(1 for token in run if token['confidence'] < 0.55)
    return len(run) * 10.0 - irregular * 8.0 + mean_confidence * 5.0 - weak * 4.0


def _run_is_usable(run: list[dict]) -> bool:
    """Whether a run is a tape and not a coincidence of numbers.

    Two things earn a reading. The numbers that bound the span must have been
    read confidently, because they are what the measurement is. And the numbers
    must sit on a steady pitch: a blade prints every number at the same
    spacing, so one number several pitches away is a street sign, a depth stamp
    or a date caught by the same pass, and a span built from it is wrong even
    when each digit was read well.
    """
    if len(run) < 2:
        return False
    if min(run[0]['confidence'], run[-1]['confidence']) < SPAN_MIN_CONFIDENCE:
        return False
    gaps = [later['value'] - earlier['value'] for earlier, later in zip(run, run[1:])]
    if not gaps or any(gap <= 0 for gap in gaps):
        return False
    step = statistics.median(gaps)
    return all(abs(gap - step) <= step * 0.6 for gap in gaps)


def _infer_unit(values: list[int]) -> str:
    """Centimetre tapes stop in the hundreds; millimetre tapes print thousands."""
    return 'mm' if max(values) >= 1000 else 'cm'


def read_tape_bytes(data: bytes, diagnostics: dict | None = None) -> dict | None:
    """The tape's readings, or None when no blade could be read.

    ``diagnostics`` is filled with the cheap quality measures (glare, focus,
    blade angle, blade and tick geometry) so the endpoint can say why a photo
    failed instead of only that it did — and so a read on a phone can be
    diagnosed from its own response, without server logs.
    """
    started = time.monotonic()
    try:
        return _read_tape_bytes(data, diagnostics)
    finally:
        if diagnostics is not None:
            diagnostics['elapsed_ms'] = int((time.monotonic() - started) * 1000)


def _geometry(image, diagnostics: dict) -> None:
    """Blade and tick geometry for the response, never for the reading.

    The reader below still works on the whole frame. This summary is what makes
    a field failure diagnosable: it says whether the blade was found, how far
    it was tilted and what pitch its ladder printed — the information a "no
    reading" toast cannot carry.
    """
    try:
        from supervision import tape_frame

        framed = tape_frame.frame_gray(np.asarray(image))
        if framed is None:
            diagnostics['blade'] = None
            diagnostics['ticks'] = None
            diagnostics['digits'] = None
            return
        diagnostics['blade'] = {
            'angle': round(framed['angle'], 1),
            'thickness_px': framed['thickness_px'],
            'coverage': round(framed['coverage'], 2),
        }
        # The frame grades the ladder while choosing the band, so this is the
        # same verdict the choice was made on — not a second, disagreeing read.
        diagnostics['ticks'] = framed.get('ticks')
        # The strip and its lattice are already here, so the printed numbers can
        # be pinned to their marks without framing the photo a second time.
        digits = None
        if framed.get('strip') is not None and framed.get('lattice'):
            try:
                from supervision import tape_digits

                digits = tape_digits.read_labels(framed['strip'], framed['lattice'])
            except Exception:
                digits = None
        diagnostics['digits'] = digits
    except Exception:
        # A diagnosis must never be the reason a read fails.
        diagnostics['blade'] = 'unavailable'


def _read_tape_bytes(data: bytes, diagnostics: dict | None = None) -> dict | None:
    image = _prepare(data)
    if image is None:
        return None
    if diagnostics is not None:
        diagnostics.update(_quality(image))
        _geometry(image, diagnostics)

    angle = _blade_angle(image)
    if diagnostics is not None:
        diagnostics['angle'] = round(angle, 1)

    frame = {'top': 0, 'bottom': image.height, 'crop': image, 'small_top': None}
    # A band that covers most of the frame is the frame: scanning it twice only
    # spends seconds the free host does not have.
    bands = [band for band in _tape_bands(image) if band['crop'].height < image.height * 0.8]

    best: list[dict] = []
    best_score = 0.0
    best_crop = frame
    best_rotation = 0
    spans: list[float] = []
    started = time.monotonic()
    deadline = started + TAPE_OCR_BUDGET_S
    hard_deadline = started + TAPE_OCR_HARD_BUDGET_S

    def out_of_time() -> bool:
        """Stop paying for passes: nothing readable here, or out of time."""
        now = time.monotonic()
        return now > hard_deadline or (len(best) >= 2 and now > deadline)

    def look(crop, rotations: tuple[int, ...]) -> None:
        """Score one crop at these turns, keeping the best run seen anywhere.

        Every turn feeds one pool for the crop: two equalisations that read the
        same value at the same x reinforce each other, and a mark one turn sees
        fills the gap another left. That agreement is what turns a shaky field
        photo into a run, so it must not be thrown away to save a pass.
        """
        nonlocal best, best_score, best_crop, best_rotation
        pool: list[dict] = []
        for _label, variant in _variants(crop['crop']):
            for rotation in rotations:
                if out_of_time():
                    return
                pool.extend(_ocr_readings(variant, rotation))
                run = _reading_run(_drop_outliers(_cluster_tokens(pool)))
                score = _run_score(run)
                if len(run) >= 2:
                    spans.append(run[-1]['value'] - run[0]['value'])
                if _run_is_usable(run) and score > best_score:
                    best, best_score = run, score
                    best_crop, best_rotation = crop, rotation
            # Three numbers in order already are a tape; further equalisations
            # of the same crop cost seconds for no gain.
            if len(_reading_run(_cluster_tokens(pool))) >= 3:
                break
        if len(_reading_run(_cluster_tokens(pool))) >= 2:
            return
        # Nothing whole here: the marks are broken into islands by dust, glare
        # or bare blade between them, so ask the sparse model before giving up.
        for rotation in rotations:
            if out_of_time():
                return
            pool.extend(_ocr_readings(crop['crop'], rotation, config=SPARSE_OCR_CONFIG))
            run = _reading_run(_drop_outliers(_cluster_tokens(pool)))
            score = _run_score(run)
            if len(run) >= 2:
                spans.append(run[-1]['value'] - run[0]['value'])
            if score > best_score:
                best, best_score = run, score
                best_crop, best_rotation = crop, rotation

    # The whole frame at 0 and 180 is usually one or two passes away from the
    # full run, so it goes first: a band crop can hold only part of the blade
    # and read two marks where the frame reads seven, and on a slow host the
    # budget would be spent before the frame was ever tried. The bands are the
    # cheap follow-up when the frame came back weak.
    look(frame, (0, 180))
    if len(best) < 4:
        for band in bands:
            look(band, (0, 180))
    if len(best) < 2 and angle:
        # The blade is tilted enough that no level read found the run: turn the
        # frame upright and try again before asking for a quarter turn.
        straight = _upright(image, angle)
        upright = {
            'top': 0,
            'bottom': straight.height,
            'crop': straight,
            'small_top': None,
            'deskewed': True,
        }
        look(upright, (0, 180))
        for band in _tape_bands(straight):
            look({**band, 'deskewed': True}, (0, 180))
    # A blade running up a portrait frame is upright only after a quarter turn.
    if len(best) < 2:
        look(frame, (90, 270))

    if len(best) < 2:
        return None

    band = _band_for(best_crop, best, best_rotation)
    # A straightened crop's rows no longer line up with the frame's own axes,
    # so the object's edge cannot be located in it.
    bounds = None if best_crop.get('deskewed') else _object_bounds(image, band, best_rotation)
    clipped = _clip_to_bounds(best, bounds)
    if bounds and len(clipped) < len(best):
        bounds = {**bounds, 'clipped': len(best) - len(clipped), 'stopped_at': clipped[-1]['value']}
        best = clipped
    else:
        bounds = None

    unit = _infer_unit([token['value'] for token in best])
    span = best[-1]['value'] - best[0]['value']
    # How many independent reads landed on the same span: one is a guess, two
    # from different equalisations and turns is a reading worth trusting.
    agreement = sum(1 for other in spans if abs(other - span) <= max(1.0, span * 0.02))
    return {
        'engine': 'tesseract',
        'unit': unit,
        'agreement': max(1, agreement),
        'readings': [
            {
                'value': token['value'],
                'unit': unit,
                'x': round(token['x'], 4),
                'confidence': round(token['confidence'], 2),
            }
            for token in best
        ],
        'bounds': bounds,
    }


def read_tape(photo, diagnostics: dict | None = None) -> dict | None:
    """The readings of the tape stored in ``photo``, or None."""
    if photo is None:
        return None
    try:
        photo.image.open('rb')
        data = photo.image.read()
        photo.image.close()
    except Exception:
        return None
    if not data:
        return None
    return read_tape_bytes(data, diagnostics)


def measure(
    element_type: str, dimension: str, readings: list[dict], unit: str = 'cm'
) -> tuple[dict | None, str]:
    """(proposed reading, note) from the numbers OCR found on the blade.

    The span is the difference between the last and the first number, in
    millimetres. The first number is not assumed to be zero: a tape laid from
    the surface down a 1.2 m wall reads 0 at the top and 120 at the bottom, and
    starting the tape at 30 cm still gives the same 1.2 m difference.
    """
    spec = dimension_spec(element_type, dimension)
    if spec is None:
        return None, 'That dimension does not belong to this element.'

    scale = TAPE_UNIT_MM.get(str(unit or 'cm').lower(), TAPE_UNIT_MM['cm'])
    values: list[float] = []
    for reading in readings or []:
        try:
            number = float(reading.get('value'))
        except (AttributeError, TypeError, ValueError):
            continue
        if number >= 0:
            values.append(round(number * scale, 1))
    values = sorted(set(values))

    if len(values) < 2:
        return None, (
            'Only one number on the tape was readable. Lay the blade along the '
            'dimension and keep two printed numbers in the frame.'
        )

    span = values[-1] - values[0]
    gaps = [later - earlier for earlier, later in zip(values, values[1:])]
    step = statistics.median(gaps)
    irregular = sum(1 for gap in gaps if step <= 0 or abs(gap - step) > step * 0.6)

    if not (spec['min_mm'] <= span <= spec['max_mm']):
        return None, (
            f'The tape spans {span:.0f} mm, outside the '
            f'{spec["min_mm"]:.0f}–{spec["max_mm"]:.0f} mm a '
            f'{spec["label"].lower()} can be. Check the photo — a misread digit '
            'is the likely cause.'
        )

    confidence = 0.9 if len(values) >= 4 else 0.8 if len(values) == 3 else 0.65
    if irregular:
        confidence = max(0.4, confidence - 0.15)

    value = round(span / 1000.0, 2) if spec['unit'] == 'm' else round(span)
    first = f'{values[0] / scale:g}'
    last = f'{values[-1] / scale:g}'
    return (
        {
            'key': spec['key'],
            'label': spec['label'],
            'value': value,
            'unit': spec['unit'],
            'confidence': round(confidence, 2),
        },
        (
            f'Tape reads {first} to {last} {unit}: {value} {spec["unit"]}. '
            'Filled as a tape reading for you to confirm before saving.'
        ),
    )
