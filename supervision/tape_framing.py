"""Is this tape photo framed well enough to measure from?

The reader needs the blade's printed numbers — not just any of them, but a
continuous run at a steady pitch that stays inside the frame. A photo that
crops the blade or sees it too small cannot be measured however good the OCR
is, so this check runs beside the read and tells the engineer to reshoot.

`shortcut:` it reads the blade, not the object. It cannot see where the depth
actually ends — only that the numbers are legible, evenly spaced and inside the
frame. Putting both ends in the brackets stays the engineer's job.
"""
from __future__ import annotations

import io
from collections import deque

import numpy as np
from PIL import Image, ImageFilter

DS = 2           # component search downsample
LINE = 15        # runs longer than this (at DS) are blade edges, not glyphs
MIN_GLYPHS = 5   # a ladder shorter than this cannot confirm a run
MAX_EVENNESS = 0.22   # cv of the gaps; a printed row is nearly uniform
MIN_COVER = 0.30      # the run must cover this much of the frame's long side
EDGE_MARGIN = 0.02    # a glyph this close to the border is cropped
MAX_INK_PIXELS = 120_000


def _gray(data: bytes) -> np.ndarray | None:
    """Greyscale pixels at a size this check can afford to walk."""
    try:
        image = Image.open(io.BytesIO(data))
        image.draft('L', (1600, 1600))   # JPEG: decode at a smaller size
        image = image.convert('L')
    except Exception:
        return None
    if max(image.size) > 1400:
        image.thumbnail((1400, 1400))
    return np.asarray(image)


def _ink(small: np.ndarray) -> np.ndarray:
    """Dark relative to the local background, so white and yellow blades both work."""
    blur = np.asarray(Image.fromarray(small).filter(ImageFilter.BoxBlur(7))).astype(np.int16)
    return small.astype(np.int16) < blur - 26


def _shift(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    out = np.zeros_like(mask)
    h, w = mask.shape
    xs, xd = (slice(dx, w), slice(0, w - dx)) if dx > 0 else (slice(0, w + dx), slice(-dx, w))
    ys, yd = (slice(dy, h), slice(0, h - dy)) if dy > 0 else (slice(0, h + dy), slice(-dy, h))
    if dx == 0:
        xs = xd = slice(0, w)
    if dy == 0:
        ys = yd = slice(0, h)
    out[yd, xd] = mask[ys, xs]
    return out


def _opening(mask: np.ndarray, length: int, horizontal: bool) -> np.ndarray:
    er = mask.copy()
    for i in range(1, length // 2 + 1):
        er &= _shift(mask, i, 0) & _shift(mask, -i, 0) if horizontal else _shift(mask, 0, i) & _shift(mask, 0, -i)
    di = er.copy()
    for i in range(1, length // 2 + 1):
        di |= _shift(er, i, 0) | _shift(er, -i, 0) if horizontal else _shift(er, 0, i) | _shift(er, 0, -i)
    return di


def _components(mask: np.ndarray, min_area: int = 8):
    """Boxes of connected ink, stopping before a busy frame costs the request."""
    h, w = mask.shape
    seen = np.zeros_like(mask)
    visited = 0
    for y in range(h):
        row = mask[y]
        if not row.any():
            continue
        for x in np.flatnonzero(row):
            if seen[y, x]:
                continue
            queue = deque([(y, int(x))])
            seen[y, x] = True
            area = 0
            y0 = y1 = y
            x0 = x1 = int(x)
            while queue:
                cy, cx = queue.popleft()
                area += 1
                visited += 1
                y0, y1 = min(y0, cy), max(y1, cy)
                x0, x1 = min(x0, cx), max(x1, cx)
                for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
            if area >= min_area:
                yield area, x0, y0, x1, y1
            if visited > MAX_INK_PIXELS:
                return


def _glyphs(ink: np.ndarray) -> list[dict]:
    """Boxes that look like printed characters rather than ticks or blade edges."""
    clean = ink & ~_opening(ink, LINE, True) & ~_opening(ink, LINE * 2, False)
    boxes = []
    for area, x0, y0, x1, y1 in _components(clean):
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if bh < 4 or bh > 40 or bw < 2 or bw > bh * 1.6:
            continue
        if area / (bw * bh) > 0.85:   # solid bar: a tick
            continue
        boxes.append({
            'cx': (x0 + x1) / 2.0,
            'cy': (y0 + y1) / 2.0,
            'bw': bw,
            'bh': bh,
        })
    return boxes


def _chain(positions: np.ndarray) -> tuple[np.ndarray, float, float]:
    """Indices on one even pitch, plus that pitch and its evenness."""
    gaps = np.diff(positions)
    if len(gaps) < 2:
        return np.array([], dtype=int), 0.0, 1.0
    pitch = float(np.median(gaps))
    if pitch <= 0:
        return np.array([], dtype=int), 0.0, 1.0
    keep = np.ones(len(positions), dtype=bool)
    for _ in range(2):
        keep = np.ones(len(positions), dtype=bool)
        keep[1:] = np.abs(gaps - pitch) <= pitch * 0.45
        # A gap that fails breaks the run: glyphs after it are a different row.
        for i in range(1, len(keep)):
            if not keep[i]:
                keep[i:] = False
                break
        kept = positions[keep]
        if len(kept) < 2:
            return np.array([], dtype=int), 0.0, 1.0
        pitch = float(np.median(np.diff(kept)))
        if pitch <= 0:
            return np.array([], dtype=int), 0.0, 1.0
    kept_gaps = np.diff(positions[keep])
    evenness = float(np.std(kept_gaps) / max(np.mean(kept_gaps), 1e-9))
    return np.flatnonzero(keep), pitch, evenness


def _ladder(boxes: list[dict]) -> dict | None:
    """The longest evenly spaced run of glyphs on a single line."""
    best = None
    for axis in ('row', 'col'):
        along = 'cx' if axis == 'row' else 'cy'
        across = 'cy' if axis == 'row' else 'cx'
        across_size = 'bh' if axis == 'row' else 'bw'
        med_across = float(np.median([b[across_size] for b in boxes]))
        bins: dict[int, list[dict]] = {}
        for box in boxes:
            bins.setdefault(int(round(box[across] / max(1.0, med_across * 0.6))), []).append(box)
        for group in bins.values():
            if len(group) < MIN_GLYPHS:
                continue
            group.sort(key=lambda b: b[along])
            positions = np.array([b[along] for b in group])
            keep, pitch, evenness = _chain(positions)
            if len(keep) < MIN_GLYPHS:
                continue
            kept = [group[i] for i in np.flatnonzero(keep)]
            span_across = float(np.std([b[across] for b in kept]))
            if span_across > med_across * 0.45:
                continue      # not one row: glyphs smeared down the blade
            # Printed characters repeat at a size: a run whose glyph widths vary
            # wildly, or whose pitch is several glyph widths apart, is ticks and
            # specks that happen to line up, not a printed row.
            widths = np.array([b['bw' if axis == 'row' else 'bh'] for b in kept], dtype=float)
            med_width = float(np.median(widths))
            if med_width <= 0 or float(np.std(widths)) > med_width * 0.35:
                continue
            if not (med_width * 1.2 <= pitch <= med_width * 3.2):
                continue
            score = (positions[keep][-1] - positions[keep][0]) * (1.0 - min(evenness, 1.0))
            if best is None or score > best['score']:
                best = {
                    'axis': axis,
                    'axis_index': 1 if axis == 'row' else 0,
                    'count': int(len(keep)),
                    'pitch': pitch,
                    'evenness': evenness,
                    'first': float(positions[keep][0]),
                    'last': float(positions[keep][-1]),
                    'score': score,
                    'width': med_width,
                }
    return best


def check_gray(gray: np.ndarray, label: str = 'measurement') -> dict:
    """Framing verdict for already-decoded pixels."""
    small = gray[::DS, ::DS]
    height, width = small.shape[:2]
    full_w, full_h = width * DS, height * DS

    boxes = _glyphs(_ink(small))
    ladder = _ladder(boxes) if len(boxes) >= MIN_GLYPHS else None
    detail = {
        'glyphs': len(boxes),
        'ladder': 0,
        'pitch_px': None,
        'evenness': None,
        'covers': None,
    }

    if ladder is None:
        return {
            'ok': False,
            'verdict': 'no_tape',
            'message': (
                f'No even row of printed numbers was found. Lay the blade straight across '
                f'the {label}, printed side up, and keep both ends of it in the frame.'
            ),
            'detail': detail,
        }

    extent = float(full_w if ladder['axis'] == 'row' else full_h)
    first = ladder['first'] * DS
    last = ladder['last'] * DS
    covers = (last - first) / max(extent, 1.0)
    detail.update({
        'ladder': ladder['count'],
        'pitch_px': round(ladder['pitch'] * DS, 1),
        'evenness': round(ladder['evenness'], 2),
        'covers': round(covers, 2),
    })

    # A handful of glyphs covering a sliver of the frame is not the blade's
    # printed row, whatever it looks like; saying "move closer" would send the
    # engineer after the wrong problem.
    if covers < 0.12:
        return {
            'ok': False,
            'verdict': 'no_tape',
            'message': (
                f'No whole row of printed numbers was found. Lay the blade straight across '
                f'the {label}, printed side up, and keep both ends of it in the frame.'
            ),
            'detail': detail,
        }

    crooked = ladder['evenness'] > MAX_EVENNESS
    if crooked:
        return {
            'ok': False,
            'verdict': 'unreadable',
            'message': (
                f'The numbers on the blade are not in a clean row ({ladder["count"]} found, '
                'unevenly spaced). Hold the camera square to the blade and shoot again.'
            ),
            'detail': detail,
        }

    margin = EDGE_MARGIN * extent
    if first < margin or last > extent - margin:
        side = 'both sides' if (first < margin and last > extent - margin) else 'one side'
        return {
            'ok': False,
            'verdict': 'cut_off',
            'message': (
                f'The blade runs out of the frame on {side}, so the rest of the {label} is '
                'not in the photo. Step back until both ends sit inside the brackets, then '
                'shoot again.'
            ),
            'detail': detail,
        }

    if covers < MIN_COVER:
        return {
            'ok': False,
            'verdict': 'too_far',
            'message': (
                f'The tape covers only {covers:.0%} of the frame. Move closer until the '
                'printed numbers fill the view, keeping both ends inside the brackets.'
            ),
            'detail': detail,
        }

    return {
        'ok': True,
        'verdict': 'good',
        'message': (
            f'Blade readable: {ladder["count"]} numbers on one pitch, covering '
            f'{covers:.0%} of the frame.'
        ),
        'detail': detail,
    }


def check_bytes(data: bytes, label: str = 'measurement') -> dict | None:
    """Framing verdict for a photo's bytes, or None if they are not an image."""
    gray = _gray(data)
    if gray is None:
        return None
    try:
        return check_gray(gray, label)
    except Exception:
        # A framing check must never take the read down with it.
        return None


def check_photo(photo, label: str = 'measurement') -> dict | None:
    """Framing verdict for a stored ``FeaturePhoto``, or None."""
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
    return check_bytes(data, label)
