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

# Uploads are shrunk to this longest edge before OCR. Bigger costs time without
# making a printed digit easier to read.
TAPE_MAX_EDGE = 1600

# pytesseract reports confidence 0–100, and a digit it is unsure about is worse
# than no digit: the reading would move the measured span.
MIN_OCR_CONFIDENCE = 55

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


def _tape_strip(image):
    """The brightest long band in the frame, or None when no blade stands out.

    A tape is a bright, low-variance strip running across the photo. Cropping to
    it before OCR removes the trench walls, the site clutter and the timestamp
    burn-in that otherwise become candidate numbers.
    """
    small = image.copy()
    small.thumbnail((320, 320))
    width, height = small.size
    if width < 8 or height < 8:
        return None
    pixels = small.load()
    bright_rows = []
    for y in range(height):
        bright_rows.append(sum(1 for x in range(width) if pixels[x, y] > 145) / width)

    step = max(1, height // 40)
    shortest = max(4, height // 10)
    longest = max(shortest + 1, int(height * 0.45))
    best = (0.0, 0, shortest)
    for span in range(shortest, longest, step):
        for top in range(0, height - span + 1, step):
            coverage = sum(bright_rows[top : top + span]) / span
            # `>=`, not `>`: a tape is bright edge to edge, so a thin sliver and
            # the whole blade tie on coverage. Prefer the taller crop, or the
            # printed numbers sit outside the strip and are never read.
            if coverage >= best[0]:
                best = (coverage, top, span)
    coverage, top, span = best
    if coverage < 0.45:
        return None

    pad = max(2, span // 8)
    top = max(0, top - pad)
    bottom = min(height, top + span + 2 * pad)
    scale = image.height / height
    return image.crop((0, int(top * scale), image.width, min(image.height, int(bottom * scale))))


def _digits(raw) -> str:
    text = str(raw or '').translate(_LOOKALIKES)
    text = ''.join(character for character in text if character.isdigit())
    return text if 1 <= len(text) <= 5 else ''


def _ocr_readings(image, rotation: int) -> list[dict]:
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
            config='--oem 3 --psm 6',
            output_type=pytesseract.Output.DICT,
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
        if confidence < MIN_OCR_CONFIDENCE:
            continue
        value = int(text)
        # Zero is legitimate: it is the tape's own datum, and a measurement that
        # starts there would otherwise be short by exactly that distance.
        if not (0 <= value <= MAX_READING_MM):
            continue
        left = float(data['left'][index])
        token_width = float(data['width'][index])
        tokens.append(
            {
                'value': value,
                'x': (left + token_width / 2) / width,
                'confidence': confidence / 100.0,
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
    return len(run) * 10.0 - irregular * 8.0 + mean_confidence * 5.0


def _infer_unit(values: list[int]) -> str:
    """Centimetre tapes stop in the hundreds; millimetre tapes print thousands."""
    return 'mm' if max(values) >= 1000 else 'cm'


def read_tape_bytes(data: bytes) -> dict | None:
    """The tape's readings, or None when no blade could be read."""
    image = _prepare(data)
    if image is None:
        return None

    crops = [('frame', image)]
    strip = _tape_strip(image)
    if strip is not None:
        crops.append(('blade', strip))

    best: list[dict] = []
    best_score = 0.0
    for _label, crop in crops:
        # The blade may run in any direction; OCR only reads digits upright, so
        # the same crop is tried at four quarter turns and the best run wins.
        for rotation in (0, 90, 180, 270):
            run = _reading_run(_ocr_readings(crop, rotation))
            score = _run_score(run)
            if score > best_score:
                best, best_score = run, score

    if len(best) < 2:
        return None

    unit = _infer_unit([token['value'] for token in best])
    return {
        'engine': 'tesseract',
        'unit': unit,
        'readings': [
            {
                'value': token['value'],
                'unit': unit,
                'x': round(token['x'], 4),
                'confidence': round(token['confidence'], 2),
            }
            for token in best
        ],
    }


def read_tape(photo) -> dict | None:
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
    return read_tape_bytes(data)


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
