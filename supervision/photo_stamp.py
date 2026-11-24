"""Write the facts about a photo into one corner of the photo.

An evidence photo has to answer its own questions when it is opened months
later: what was measured, where and when. A NoteCam stamp does that for the
camera; this does it for the app, from the values the app already holds rather
than from pixels burned in at capture time.

Nothing here reads or writes the database, and nothing here decides whether a
reading is right: it draws the lines it is given. That keeps a stamped copy
usable off a phone (a recovered file has no row) and keeps the result honest --
an unknown value is drawn as an unknown, never dropped silently.

The panel is a dark translucent block so the photo stays visible behind it:
evidence that hides the tape is not evidence.
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

# Narrow enough to leave the blade and the trench visible: a panel across the
# whole frame would cover the numbers it is talking about.
PANEL_MAX_WIDTH_SHARE = 0.54
PANEL_ALPHA = 182
BASE_FONT_SHARE = 0.024
MIN_FONT_SIZE = 13
MAX_FONT_SIZE = 44
CORNER_MARGIN_SHARE = 0.018

PANEL = (10, 12, 14)
TITLE = (120, 205, 250)
LABEL = (176, 184, 192)
VALUE = (255, 255, 255)
FOOT = (206, 212, 218)

# Arial and Segoe UI cover the digits, degrees and slashes a stamp needs. The
# DejaVu copies are there for a Linux host that has no system font; the bitmap
# default is the last resort so a stamp never fails for want of a font.
FONT_PATHS = (
    'C:/Windows/Fonts/arial.ttf',
    'C:/Windows/Fonts/segoeui.ttf',
    'C:/Windows/Fonts/calibri.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    '/Library/Fonts/Arial.ttf',
)
BOLD_FONT_PATHS = (
    'C:/Windows/Fonts/arialbd.ttf',
    'C:/Windows/Fonts/segoeuib.ttf',
    'C:/Windows/Fonts/calibrib.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/Library/Fonts/Arial Bold.ttf',
)

_FONT_CACHE: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """A font at ``size``, cached: every stamp re-asks for the same few sizes."""
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    for path in BOLD_FONT_PATHS if bold else FONT_PATHS:
        if not os.path.exists(path):
            continue
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1 has no sized default font
            font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _width(draw: ImageDraw.ImageDraw, text: str, font) -> float:
    return draw.textlength(text, font=font)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: float) -> list[str]:
    """``text`` broken to ``width``, never losing a word or a character."""
    words = str(text or '').split()
    if not words:
        return ['']
    lines: list[str] = []
    line = words[0]
    for word in words[1:]:
        candidate = f'{line} {word}'
        if _width(draw, candidate, font) <= width:
            line = candidate
        else:
            lines.append(line)
            line = word
    lines.append(line)

    # A single token wider than the column (a long file name, a URL) is cut
    # rather than allowed to run past the panel edge.
    fitted: list[str] = []
    for line in lines:
        while line and _width(draw, line, font) > width and len(line) > 1:
            cut = len(line)
            while cut > 1 and _width(draw, line[:cut], font) > width:
                cut -= 1
            fitted.append(line[:cut])
            line = line[cut:]
        fitted.append(line)
    return fitted


def _base_size(width: int, height: int) -> int:
    size = int(min(width, height) * BASE_FONT_SHARE)
    return max(MIN_FONT_SIZE, min(MAX_FONT_SIZE, size))


def _fit_headline(measure, headline: str, base: int, width: float):
    """The headline at the largest size that keeps it on one line.

    "ACTUAL WIDTH 110 mm" wraps on a 960 px photo at the default size, and a
    wrapped headline reads as two rows rather than one reading.
    """
    font = _font(int(base * 1.42), bold=True)
    for share in (1.42, 1.28, 1.14, 1.02, 0.92):
        font = _font(max(10, int(base * share)), bold=True)
        if len(_wrap(measure, headline, font, width)) == 1:
            break
    return _wrap(measure, headline, font, width), font


def stamp_image(
    image: Image.Image,
    *,
    title: str,
    rows: list[tuple[str, str]],
    headline: str | None = None,
    footer: str | None = None,
    corner: str = 'top-right',
) -> Image.Image:
    """The photo with a stamped panel in ``corner`` (``top``/``bottom`` + ``left``/``right``)."""
    if image.mode != 'RGB':
        image = image.convert('RGB')
    width, height = image.size
    base = _base_size(width, height)

    title_font = _font(max(10, int(base * 0.82)), bold=True)
    label_font = _font(max(10, int(base * 0.90)))
    value_font = _font(base)
    foot_font = _font(max(10, int(base * 0.84)))

    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    pad = int(base * 0.80)
    gap = int(base * 0.62)
    row_gap = int(base * 0.26)
    line_h = int(base * 1.32)
    head_line_h = int(base * 1.60)
    foot_line_h = int(base * 1.28)
    title_h = int(base * 1.15)
    margin = max(8, int(min(width, height) * CORNER_MARGIN_SHARE))

    label_w = int(
        max((_width(measure, str(label), label_font) for label, _ in rows), default=0)
    )
    value_w = int(
        max((_width(measure, str(value), value_font) for _, value in rows), default=0)
    )
    col_gap = int(base * 0.70)
    max_panel = min(int(width * PANEL_MAX_WIDTH_SHARE), width - 2 * margin)
    panel_w = min(max_panel, label_w + col_gap + value_w + 2 * pad)
    panel_w = max(panel_w, min(max_panel, label_w + col_gap + int(base * 8) + 2 * pad))
    value_col = max(int(base * 6), panel_w - 2 * pad - label_w - col_gap)

    if headline:
        head_lines, head_font = _fit_headline(measure, headline, base, panel_w - 2 * pad)
    else:
        head_lines, head_font = [], None
    foot_lines = _wrap(measure, footer, foot_font, panel_w - 2 * pad) if footer else []

    value_x = pad + label_w + col_gap
    blocks: list[dict] = [
        {'text': title.upper(), 'font': title_font, 'fill': TITLE, 'h': title_h,
         'after': gap, 'xoff': pad},
    ]
    for line in head_lines:
        blocks.append({'text': line, 'font': head_font, 'fill': VALUE, 'h': head_line_h,
                       'after': 0, 'xoff': pad})
    if head_lines:
        blocks[-1]['after'] = gap // 2

    for label, value in rows:
        for index, line in enumerate(_wrap(measure, str(value), value_font, value_col)):
            blocks.append(
                {
                    'text': line,
                    'font': value_font,
                    'fill': VALUE,
                    'h': line_h,
                    'after': 0,
                    'xoff': value_x,
                    'label': str(label) if index == 0 else None,
                }
            )
        blocks[-1]['after'] = row_gap

    if foot_lines:
        blocks[-1]['after'] = gap
        for index, line in enumerate(foot_lines):
            blocks.append(
                {
                    'text': line,
                    'font': foot_font,
                    'fill': FOOT,
                    'h': foot_line_h,
                    'after': 0,
                    'xoff': pad,
                }
            )

    panel_h = pad + sum(block['h'] + block['after'] for block in blocks) + pad
    x = margin if 'left' in corner else width - margin - panel_w
    y = margin if 'top' in corner else height - margin - panel_h

    overlay = Image.new('RGBA', image.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(
        [x, y, x + panel_w, y + panel_h],
        radius=int(base * 0.55),
        fill=PANEL + (PANEL_ALPHA,),
        outline=(255, 255, 255, 45),
        width=1,
    )

    cursor = y + pad
    for index, block in enumerate(blocks):
        if index == 0:
            od.text((x + pad, cursor), block['text'], font=block['font'], fill=block['fill'])
            od.line(
                [(x + pad, cursor + block['h'] - 2), (x + panel_w - pad, cursor + block['h'] - 2)],
                fill=(255, 255, 255, 50),
                width=max(1, base // 16),
            )
        else:
            if block.get('label'):
                od.text((x + pad, cursor), block['label'], font=label_font, fill=LABEL)
            od.text(
                (x + block['xoff'], cursor), block['text'], font=block['font'], fill=block['fill']
            )
        cursor += block['h'] + block['after']

    return Image.alpha_composite(image.convert('RGBA'), overlay).convert('RGB')


def stamp_bytes(data: bytes, **kwargs) -> bytes:
    """``stamp_image`` for JPEG/PNG bytes, returned as JPEG bytes."""
    image = Image.open(io.BytesIO(data))
    image.load()
    image = ImageOps.exif_transpose(image)
    stamped = stamp_image(image, **kwargs)
    out = io.BytesIO()
    stamped.save(out, format='JPEG', quality=92, optimize=True)
    return out.getvalue()


def stamp_file(source: str, destination: str, **kwargs) -> dict:
    """Stamp one file onto ``destination``, returning the reading written with it."""
    with open(source, 'rb') as handle:
        data = handle.read()
    stamped = stamp_bytes(data, **kwargs)
    os.makedirs(os.path.dirname(destination) or '.', exist_ok=True)
    with open(destination, 'wb') as handle:
        handle.write(stamped)
    return {'bytes': len(stamped), 'destination': destination}


def _dms(value, ref) -> float | None:
    """Degrees from an EXIF (degrees, minutes, seconds) triple and its hemisphere."""
    try:
        degrees, minutes, seconds = (float(part) for part in value)
    except (TypeError, ValueError):
        return None
    result = degrees + minutes / 60.0 + seconds / 3600.0
    return -result if str(ref or '').upper() in ('S', 'W') else result


def exif_location(data: bytes) -> dict:
    """GPS and capture time from the photo's own EXIF, when it still has them.

    WhatsApp and most chat apps strip EXIF, so this is a bonus rather than the
    only source: the caller's own metadata wins where both exist.
    """
    try:
        image = Image.open(io.BytesIO(data))
        exif = image.getexif()
        gps = exif.get_ifd(0x8825)
    except Exception:
        return {}

    facts: dict = {}
    latitude = _dms(gps.get(2), gps.get(1))
    longitude = _dms(gps.get(4), gps.get(3))
    if latitude is not None and longitude is not None:
        facts['latitude'] = round(latitude, 7)
        facts['longitude'] = round(longitude, 7)
    altitude = gps.get(6)
    if altitude is not None:
        try:
            metres = float(altitude)
            facts['elevation_m'] = round(-metres if gps.get(5) == 1 else metres, 2)
        except (TypeError, ValueError):
            pass

    captured = exif.get(0x9003) or exif.get(0x0132)
    if captured:
        try:
            facts['captured_at'] = str(captured).replace(':', '-', 2)
        except Exception:
            pass
    return facts
