"""Locate the crew's clip on the blade, and the two faces that bound the reading.

The clip is not the ruler. Its own size is only a few millimetres along a blade
seen at an unknown angle, so anything measured *across* the blade through it is
foreshortened by an unknown amount: on the field set the same tape's clip came
out anywhere between 3.2 and 14.7 mm wide depending on the photo. What the clip
is good for is bookkeeping — it says where the reading is, which way the blade
runs, and which millimetre is meant.

So this module answers exactly one question: where is the clip, and where are
the two faces of it along the blade. Both faces are returned; which one carries
the reading is a surveyor's convention, not a geometric fact, and picking it
here would bake a guess into every measurement.

Two detections are needed because the clips in the field set are not one colour:

* A coloured clip (cyan, in the phone originals) is its own evidence — its hue
  separates it from a white or yellow blade.
* A clip whose body is white, or black on yellow, has no hue to find, and a
  pure colour test under-measures it: on `20261124_141314` the cyan mask covers
  a 43 px stripe of a clip that is visibly wider in white. What both kinds do
  share is that they *interrupt the blade* — the bright band stops, and the
  blade's own tone stops with it, for a few millimetres.

The band is therefore cut out first, and the clip is found as a short run where
the band's brightness, its colourfulness, or its breadth of blade tone departs
from the band's own baseline. All three are computed along the blade and the
strongest agreeing run wins, so a clip that is merely pale still registers
through the blade tone it displaces.

The blade is localised on a small copy (components, then a principal axis), and
the profile is sampled at working resolution. The blade is the longest bright
elongated thing in a survey photo, and a clip splits it into two components —
so the extent comes from the bright pixels inside the band's own width, not
from one component, or the clip would sit at the end of the search instead of
in the middle of it.

Several candidate bands are graded rather than the first one accepted. The
brightest large patch is not always the blade: on `20261124_140357_1` a paved
edge the same tone as the tape won on area and came back 503 px wide where the
blade is 256, and no clip could then be found on it. The patch that yields a
clip is the blade, so the search keeps the hypotheses and lets the clip decide.

Env: none.
"""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from . import tape_band

# Components are found on a copy this long, so a 3840 px phone original costs
# milliseconds; every position is scaled back up before it is reported.
WORK_EDGE = 700

# A tape is a large, long thing. Smaller or rounder bright patches are gravel,
# a pipe, or the trench wall.
MIN_AREA_SHARE = 0.004
MIN_ELONGATION = 2.5
MIN_WIDTH_PX = 4.0

# Bright patches worth grading as the blade.
MAX_HYPOTHESES = 4

# The baseline a run is measured against, as a share of the blade's length.
# Wide enough to survive the printed numbers, narrow enough to keep the clip
# out of its own baseline.
BASE_WINDOW_SHARE = 0.25

# A clip covers a few millimetres of a blade that is 16-25 mm wide: a sixth to
# half of the blade's width along the tape. The floor is what keeps a long
# millimetre mark out of the result — a real tick is a sixteenth of the blade's
# width, where the narrowest clip in the field set is still a sixth of it.
MIN_RUN_SHARE = 0.12
MAX_RUN_SHARE = 1.3

# A run that touches the sampled ends is the blade leaving the frame, not a clip.
MIN_INTERIOR_PX = 4

# Departure from the baseline that counts, in units of the signal's own spread.
MIN_SIGNAL = 0.45

# How much of the blade's breadth a clip has to displace to count as a clip on
# the breadth signal alone.
MIN_BREADTH_DROP = 0.5

# The signals a run can be seen in, named in the order they are tried.
SIGNALS = ('dark', 'bright', 'colour', 'breadth')


def _level(gray: np.ndarray) -> float:
    """The gray level that splits a frame into ground and blade (Otsu)."""
    histogram = np.bincount(
        np.clip(gray, 0, 255).astype(np.uint8).ravel(), minlength=256
    ).astype(np.float64)
    total = histogram.sum()
    if not total:
        return 255.0
    weights = np.cumsum(histogram)
    sums = np.cumsum(histogram * np.arange(256.0))
    spread = (sums[-1] / total * weights - sums) ** 2 / np.maximum(
        weights * (total - weights), 1e-9
    )
    if not np.isfinite(spread).any() or float(spread.max()) <= 0.0:
        return 255.0
    return float(np.argmax(spread))


def _components(mask: np.ndarray) -> list[np.ndarray]:
    """Bright patches as (x, y) arrays, largest first.

    A flood fill run on the small copy: the only thing wanted from it is an axis
    and a width, and a labelling dependency for that is not one this project
    otherwise carries.
    """
    height, width = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    found: list[np.ndarray] = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        pixels: list[tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            pixels.append((x, y))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if (
                        0 <= ny < height
                        and 0 <= nx < width
                        and not seen[ny, nx]
                        and mask[ny, nx]
                    ):
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        found.append(np.array(pixels, dtype=float))
    found.sort(key=len, reverse=True)
    return found


def _bands(mask: np.ndarray, limit: int = MAX_HYPOTHESES) -> list[dict]:
    """Bright elongated patches, worth testing as the blade, largest first."""
    found: list[dict] = []
    for points in _components(mask)[:8]:
        if len(found) >= limit:
            break
        if len(points) < MIN_AREA_SHARE * mask.size:
            continue
        centre = points.mean(axis=0)
        centred = points - centre
        values, vectors = np.linalg.eigh(np.cov(centred.T))
        axis = vectors[:, int(np.argmax(values))]
        norm = float(np.hypot(axis[0], axis[1]))
        if norm <= 0.0:
            continue
        axis = axis / norm
        # Point the axis rightwards so "the far face" means the same thing in
        # every photo, and a caller can talk about the left and right faces.
        if axis[0] < 0:
            axis = -axis
        across = np.array([-axis[1], axis[0]])
        u = centred @ axis
        v = centred @ across
        length = float(np.percentile(u, 99) - np.percentile(u, 1))
        width = float(np.percentile(v, 98) - np.percentile(v, 2))
        if length < 20.0 or width < MIN_WIDTH_PX or length < MIN_ELONGATION * width:
            continue
        middle = float(np.median(v))
        shifted = centre + across * middle
        found.append(
            {
                'centre': (float(shifted[0]), float(shifted[1])),
                'axis': (float(axis[0]), float(axis[1])),
                'across': (float(across[0]), float(across[1])),
                'length': length,
                'width': width,
            }
        )
    return found


def _blade_extent(band: dict, mask: np.ndarray) -> float:
    """How far the bright band runs along its axis, ignoring the clip's gap.

    Measured from every bright pixel inside the band's own width rather than
    from the largest component: the clip splits the blade in two, and the larger
    half would make the search stop on one side of the thing being looked for.
    """
    ys, xs = np.nonzero(mask)
    points = np.stack([xs, ys], axis=1).astype(float)
    centred = points - np.array(band['centre'])
    u = centred @ np.array(band['axis'])
    v = centred @ np.array(band['across'])
    keep = np.abs(v) <= 0.6 * band['width']
    if int(keep.sum()) < 50:
        return band['length']
    extent = float(np.percentile(u[keep], 98) - np.percentile(u[keep], 2))
    # A band whose bright run is tens of its own widths long is following a
    # kerb or a wall through the frame, not a tape: keep the patch's own length.
    if not 3.0 * band['width'] <= extent <= 30.0 * band['width']:
        return band['length']
    return extent


def _profile(gray: np.ndarray, saturation: np.ndarray, band: dict, length: float) -> dict:
    """Per-column brightness, colourfulness and breadth of blade tone.

    Breadth is the count of pixels that match the blade's own tone. It is what
    catches a white clip: the body is as bright as the blade and no hue test can
    see it, but it stops being *blade* for as long as it covers it, and it
    overhangs both edges, so the count collapses.
    """
    centre = np.array(band['centre'])
    axis = np.array(band['axis'])
    across = np.array(band['across'])
    width = band['width']
    columns = max(200, min(1600, int(round(length))))
    rows = max(9, int(round(width * 1.1)))
    us = np.linspace(-length / 2.0, length / 2.0, columns)
    vs = np.linspace(-0.55 * width, 0.55 * width, rows)
    uu, vv = np.meshgrid(us, vs)
    xs = centre[0] + uu * axis[0] + vv * across[0]
    ys = centre[1] + uu * axis[1] + vv * across[1]
    height, frame_width = gray.shape
    ix = np.clip(np.rint(xs).astype(int), 0, frame_width - 1)
    iy = np.clip(np.rint(ys).astype(int), 0, height - 1)
    tone = gray[iy, ix]
    colours = saturation[iy, ix]
    blade = float(np.median(tone))
    tolerance = max(8.0, 0.18 * float(np.percentile(tone, 95) - np.percentile(tone, 5)))
    return {
        'us': us,
        'brightness': np.median(tone, axis=0),
        'saturation': colours.mean(axis=0),
        'breadth': (np.abs(tone - blade) < tolerance).sum(axis=0).astype(float),
    }


def _baseline(values: np.ndarray, share: float = BASE_WINDOW_SHARE) -> np.ndarray:
    """A moving median: what the blade does when no clip is in the way."""
    count = values.size
    span = max(9, int(round(count * share)))
    span += 1 - span % 2
    if span >= count:
        return np.full_like(values, float(np.median(values)), dtype=float)
    padded = np.pad(values.astype(float), span // 2, mode='edge')
    windows = np.lib.stride_tricks.sliding_window_view(padded, span)
    return np.median(windows, axis=1)[:count]


def _spread(values: np.ndarray) -> float:
    """The signal's own scale, so thresholds mean the same on a dim photo."""
    return max(1e-6, float(np.percentile(values, 90) - np.percentile(values, 10)))


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs as inclusive index pairs."""
    found: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(flags.tolist()):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            found.append((start, index - 1))
            start = None
    if start is not None:
        found.append((start, len(flags) - 1))
    return found


def _departures(profile: dict) -> dict[str, np.ndarray]:
    """Every signal as a positive departure from the blade's own baseline."""
    brightness = profile['brightness']
    saturation = profile['saturation']
    breadth = profile['breadth']
    base_brightness = _baseline(brightness)
    base_saturation = _baseline(saturation)
    base_breadth = _baseline(breadth)
    brightness_scale = _spread(brightness)
    saturation_scale = max(0.05, _spread(saturation))
    return {
        'dark': (base_brightness - brightness) / brightness_scale,
        'bright': (brightness - base_brightness) / brightness_scale,
        'colour': (saturation - base_saturation) / saturation_scale,
        'breadth': (base_breadth - breadth) / np.maximum(1.0, base_breadth),
    }


def _candidates(profile: dict, width: float) -> list[dict]:
    """Runs that look like a clip, strongest first."""
    us = profile['us']
    step = float(us[1] - us[0]) if us.size > 1 else 1.0
    departures = _departures(profile)
    low = max(MIN_INTERIOR_PX, int(round(MIN_RUN_SHARE * width / step)))
    high = max(low + 1, int(round(MAX_RUN_SHARE * width / step)))
    found: list[dict] = []
    for name in SIGNALS:
        values = departures[name]
        for start, stop in _runs(values >= MIN_SIGNAL):
            span = stop - start + 1
            if span < low or span > high:
                continue
            if start < MIN_INTERIOR_PX or stop > values.size - 1 - MIN_INTERIOR_PX:
                continue
            if name == 'breadth':
                base = _baseline(profile['breadth'])[start : stop + 1]
                drop = float(np.median(base)) - float(
                    np.median(profile['breadth'][start : stop + 1])
                )
                if drop < MIN_BREADTH_DROP * max(1.0, float(np.median(base))):
                    continue
            strength = float(np.mean(np.clip(values[start : stop + 1], 0.0, 2.0)))
            # A clip is a few millimetres of blade: a run near the band's own
            # width is a shadow across the whole photo, and is scored down
            # rather than thrown away, because a clip seen end-on is wide.
            sanity = 1.0 if span <= 0.6 * width / step else 0.6
            found.append(
                {
                    'method': name,
                    'start': start,
                    'stop': stop,
                    'us': (float(us[start]), float(us[stop])),
                    'score': strength * sanity,
                }
            )
    found.sort(key=lambda run: run['score'], reverse=True)
    return found


def _grade(
    gray: np.ndarray, saturation: np.ndarray, mask: np.ndarray, band: dict
) -> tuple[dict, list[dict], float] | None:
    """The clip candidates of one candidate band, and the band's own length."""
    length = _blade_extent(band, mask)
    runs = _candidates(_profile(gray, saturation, band, length), band['width'])
    return (band, runs, length) if runs else None


def find_clip(
    rgb: np.ndarray, work_edge: int = WORK_EDGE, band: dict | None = None
) -> dict:
    """The clip on the blade, or a refusal saying why it could not be found.

    `band` is a `tape_band.find_band` result the caller has already decided is
    the blade; passing it in is what lets the reader check the blade before it
    looks for a clip without running the locator twice.
    """
    if rgb is None or getattr(rgb, 'ndim', 3) != 3:
        return {'found': False, 'reason': 'the photo is not a colour image'}
    if rgb.shape[0] < 60 or rgb.shape[1] < 60:
        return {'found': False, 'reason': 'the photo is too small to hold a blade'}

    image = Image.fromarray(rgb).convert('RGB')
    scale = max(image.width, image.height) / float(work_edge)
    if scale > 1.0:
        image.thumbnail((work_edge, work_edge), Image.BILINEAR)
    small = np.asarray(image, dtype=np.float32)
    gray = small @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    high = small.max(axis=2)
    low = small.min(axis=2)
    saturation = (high - low) / np.maximum(1.0, high)

    # The blade comes from `tape_band`, which tracks it. This module used to
    # find its own band from the largest bright patch, and that patch was wrong
    # on the field set often enough that the clip was then looked for on a kerb.
    # One locator, used by both stages, is the point of phase 1.
    blade = band if band is not None else tape_band.find_band(rgb, work_edge=work_edge)
    if not blade.get('found'):
        return {
            'found': False,
            'reason': blade.get('reason') or 'no blade was found to look for a clip on',
            'stage': 'blade',
        }
    # Policy, decided 2026-11-24: a *reading* is only taken on a blade that two
    # working resolutions agree about (`confirmed`). The locator can find a
    # confident-looking band that is not the tape — `20261124_140332` returns a
    # band at -33 degrees where the tape runs at +12 — and a reading taken off
    # that would be a number measured on a trench edge.
    #
    # The flag is passed through here rather than refused, because this stage
    # answers "where is the clip" and the measurement path is what must refuse;
    # that path does not exist yet, and refusing here only made clean frames with
    # one confident band unreadable. The reader built next must check this flag
    # before it reports a millimetre value.
    band = {
        'centre': np.array(blade['centre'], dtype=float) / scale,
        'axis': np.array(blade['axis'], dtype=float),
        'across': np.array(blade['across'], dtype=float),
        'length': float(blade['length_px']) / scale,
        'width': float(blade['width_px']) / scale,
    }
    length = band['length']
    runs = _candidates(_profile(gray, saturation, band, length), band['width'])
    if not runs:
        return {
            'found': False,
            'reason': 'the blade was found but nothing interrupts it',
            'stage': 'clip',
            'band': blade['band'],
        }

    best = runs[0]
    agreeing = [
        run
        for run in runs[1:]
        if run['method'] != best['method']
        and min(run['stop'], best['stop']) >= max(run['start'], best['start'])
    ]

    def at(u: float) -> tuple[float, float]:
        return (
            (band['centre'][0] + u * band['axis'][0]) * scale,
            (band['centre'][1] + u * band['axis'][1]) * scale,
        )

    first_u, last_u = best['us']
    faces = [at(first_u), at(last_u)]
    return {
        'found': True,
        'reason': None,
        'method': best['method'],
        'agreement': [run['method'] for run in agreeing],
        'confidence': round(min(1.0, best['score']), 2),
        'score': round(float(best['score']), 3),
        'faces': [[round(x, 1), round(y, 1)] for x, y in faces],
        'along_tape_px': round(abs(last_u - first_u) * scale, 1),
        'blade_confirmed': bool(blade.get('confirmed')),
        'tape_angle_deg': round(
            float(np.degrees(np.arctan2(band['axis'][1], band['axis'][0]))), 2
        ),
        'tape_width_px': round(band['width'] * scale, 1),
        'band': {
            'centre': [
                round(band['centre'][0] * scale, 1),
                round(band['centre'][1] * scale, 1),
            ],
            'axis': [round(band['axis'][0], 4), round(band['axis'][1], 4)],
            'across': [round(band['across'][0], 4), round(band['across'][1], 4)],
            'length_px': round(length * scale, 1),
            'width_px': round(band['width'] * scale, 1),
        },
        'scale': round(scale, 3),
    }


def find_clip_bytes(data: bytes, work_edge: int = WORK_EDGE) -> dict:
    """The clip in an uploaded photo, with the EXIF rotation applied."""
    import io

    from PIL import ImageOps

    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    except Exception:
        return {'found': False, 'reason': 'the upload could not be read as a photo'}
    return find_clip(np.asarray(image), work_edge=work_edge)


def annotate(rgb: np.ndarray, clip: dict, width: int = 1200) -> Image.Image:
    """The photo with the band, the clip's two faces and the tape axis drawn.

    Both faces are labelled A and B, not "reference": which one carries the
    reading is the crew's convention, and a preview that asserted one would be
    telling the engineer something this module does not know.
    """
    image = Image.fromarray(rgb).convert('RGB')
    scale = min(1.0, width / float(image.width))
    if scale < 1.0:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.BILINEAR,
        )
    draw = ImageDraw.Draw(image)
    if not clip.get('found'):
        draw.text((12, 12), clip.get('reason') or 'no clip found', fill=(255, 40, 40))
        return image
    band = clip['band']
    centre = np.array(band['centre']) * scale
    axis = np.array(band['axis'])
    across = np.array(band['across'])
    half = band['length_px'] * scale / 2.0
    draw.line([tuple(centre - axis * half), tuple(centre + axis * half)],
              fill=(255, 0, 0), width=3)
    reach = band['width_px'] * scale
    for name, face in zip('AB', clip['faces']):
        point = np.array(face) * scale
        draw.line([tuple(point - across * reach), tuple(point + across * reach)],
                  fill=(0, 255, 0), width=3)
        draw.text(tuple(point + across * reach), name, fill=(0, 255, 0))
    return image
