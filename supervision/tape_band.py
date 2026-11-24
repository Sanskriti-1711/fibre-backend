"""Find the blade in a survey photo by tracking it, not by guessing at patches.

Two attempts at "which bright thing is the tape" failed the same way. Taking the
largest bright elongated component worked on synthetic frames and lost on the
field set: on `20261124_140357_1` a paved edge the same tone as the tape won on
area, and on `20261124_140332` the blade and the sunlit sand merged into a single
59 000 px blob that has no band shape at all, so nothing was proposed. A single
threshold cannot separate a white tape from bright ground.

The tape is still easy to *see*: it is a thin, very long band of the brightest
pixels in the frame. So the seed is found by looking for that band directly —
project the brightest pixels onto a normal at each angle, and the tape shows up
as one narrow spike wherever the normal is perpendicular to it, while ground
spread across the frame contributes a flat background. That gives a direction
and a centre line without needing the blade to be a separate component.

From there the blade is *tracked*: station by station along the seed's direction,
each station measures the bright run across the blade, taking the run nearest to
where the last station found it. A printed number, a tick or a shadow then costs
one station rather than the whole band. The clip costs a dozen stations, so the
band is measured across them rather than required to be continuous — the clip is
the thing the reader is looking for, and refusing a band because the clip is in
the middle of it would be refusing every real photo.

When two bright bands both track cleanly, the band that is most elongated wins:
the tape crosses the frame at one blade's width, while a pipe, a kerb or a
paved edge that happens to be bright is short and fat. The tick ladder breaks a
remaining tie — the blade's along-profile repeats at the tick pitch and a paved
edge is smooth — and that is the same property the reader needs later.

Everything here is numpy and Pillow. No OpenCV, no SciPy, no new dependency.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

# The band is tracked on a copy this long. Tracking is per station, so it does
# not need the original resolution; every reported position is scaled back up.
WORK_EDGE = 900

# Seeds are found on a coarser copy still, from the brightest pixels only.
SEED_EDGE = 450
SEED_PERCENTILE = 88.0
SEED_POINTS = 6000
MAX_SEEDS = 4

# Angle search: coarse, then refined around the best coarse angle.
ANGLE_STEP = 3.0
ANGLE_REFINE = 0.5
ANGLE_WINDOW = 3.0

# A blade is long and thin. Anything narrower than this is a highlight.
MIN_WIDTH_PX = 4.0

# Stations along the blade, and how far past the seed's own ends to look. A seed
# stops where the blade stops being one bright thing — at the clip, usually — so
# the search has to go past its ends to reach the other side of the clip.
STATIONS = 260
SPAN_SHARE = 1.35

# Each station is sampled across this much of the seed's width, so that both
# edges of the blade have darker ground to be measured against.
WINDOW_SHARE = 1.7

# A station's run has to look like the blade it started on: within this factor
# of the seed's width, and not touching the sampled window's edge, because a run
# that fills the window is the window and not a blade.
# A station can only be wrong upwards: a merge with bright ground makes the run
# wider, nothing makes a blade look narrower than it is. Tightening this to 1.6
# was tried to cure the 140332 merges and it dropped so many stations on
# `20261124_141314` that the real tape was refused and a band at -75 degrees was
# returned in its place. Returning a wrong band is worse than refusing, so the
# tolerance stays generous and the width spread gate does the refusing.
WIDTH_LOW = 0.35
WIDTH_HIGH = 2.4

# A millimetre mark or a printed number interrupts a station's bright run
# without touching the blade's edges, so a station's width is taken from every
# bright run that lies this close to the blade's centre line — blade-internal
# marks are then bridged, while ground beyond the blade's edge is not. A clip
# covers the whole blade and leaves only slivers at the edges, which fall
# outside this and are correctly read as a missing station.
CORE_SHARE = 0.6

# The width series is median-filtered over this many stations before its spread
# is taken: one tick, one shadow, one glare spot is not the blade changing
# width, and the spread is what tells a tape from a wall.
WIDTH_SMOOTH = 11

# The track has to hold for this share of the stations between its ends, cover
# this share of the walked span, stay this even in width and this straight, to
# be called a tape. The coverage floor is what the clip costs: on the field set
# a clip is a tenth of the blade, and the sand glare on `20261124_140332` adds
# more, so half the stations is a working band rather than a broken one.
MIN_COVERAGE = 0.50
MIN_SUPPORT = 0.30
MAX_WIDTH_SPREAD = 0.32
MAX_BEND = 0.12

# Ticks run every 1-10 mm, which is a handful of pixels at the tracked scale.
MIN_TICK_LAG = 3
MAX_TICK_LAG = 90


def otsu_level(gray: np.ndarray) -> float:
    """The gray level that splits a frame into ground and bright (Otsu)."""
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


def _brightest(gray: np.ndarray) -> np.ndarray:
    """The brightest pixels of the frame, as an (N, 2) array of (x, y) points.

    The level cannot be a fixed percentile of the frame. A tape is a couple of
    percent of a survey photo but most of a close-up, and on a synthetic frame
    whose blade covered 9% of the pixels the 88th percentile fell in the *ground*
    and the seeds were lines through dirt — which is why the clip tests, which
    render a clean blade, were the ones that broke. The midpoint between the
    ground's tone and the frame's brightest tone is scale-free, and the fixed
    percentile still wins on field photos, where sunlit ground is brighter than
    that midpoint.
    """
    level = max(
        float(np.percentile(gray, SEED_PERCENTILE)),
        0.5 * (float(np.percentile(gray, 20)) + float(np.percentile(gray, 99.5))),
    )
    ys, xs = np.nonzero(gray >= level)
    if xs.size < 200:
        return np.zeros((0, 2), dtype=np.float64)
    points = np.stack([xs, ys], axis=1).astype(np.float64)
    if points.shape[0] > SEED_POINTS:
        index = np.linspace(0, points.shape[0] - 1, SEED_POINTS).astype(int)
        points = points[index]
    return points


def _axes(angle_deg: float) -> tuple[np.ndarray, np.ndarray]:
    radians = np.radians(angle_deg)
    return (
        np.array([np.cos(radians), np.sin(radians)]),
        np.array([-np.sin(radians), np.cos(radians)]),
    )


def _spike(points: np.ndarray, angle_deg: float) -> dict | None:
    """The narrowest band of points seen along this angle, if it is band-like.

    A tape puts all of its pixels within a few pixels of one line, so it fills
    one histogram bin and its neighbours: the score is the share of points in
    that spike. Ground spread across the frame fills the whole histogram and
    scores low, however bright it is.
    """
    axis, across = _axes(angle_deg)
    u = points @ axis
    v = points @ across
    spread = float(v.max() - v.min())
    if spread <= 0.0:
        return None
    # Three-pixel bins to locate the spike, then the spike's own spread to size
    # it. Bin-level full-width-half-maximum was tried and is not stable: one
    # noisy bin at the peak's edge halves or doubles the answer, and the tracker
    # then either sees a window narrower than the tape or reads a patch of
    # sunlit ground as blade.
    bins = int(np.clip(round(spread / 3.0), 24, 300))
    counts, edges = np.histogram(v, bins=bins)
    peak = int(np.argmax(counts))
    total = float(counts.sum())
    if total <= 0.0 or counts[peak] <= 0:
        return None
    step = float(edges[1] - edges[0])
    centre_v = float(0.5 * (edges[peak] + edges[peak + 1]))
    core = np.abs(v - centre_v) <= 3.0 * step
    if int(core.sum()) < 50:
        return None
    inner = v[core]
    width = max(
        1.15 * float(np.percentile(inner, 90) - np.percentile(inner, 10)),
        MIN_WIDTH_PX,
    )
    keep = np.abs(v - centre_v) <= 0.5 * width
    if int(keep.sum()) < 50:
        return None
    low, high = np.percentile(u[keep], [2, 98])
    return {
        'angle': float(angle_deg),
        'share': float(keep.sum()) / total,
        'centre_v': centre_v,
        'width': width,
        'axis': axis,
        'across': across,
        'centre': points[keep].mean(axis=0),
        'length': float(high - low),
    }


def _seeds(gray: np.ndarray, limit: int = MAX_SEEDS) -> list[dict]:
    """Band seeds, strongest first, taking the best band out of the pool each time."""
    pool = _brightest(gray)
    if pool.shape[0] < 200:
        return []
    found: list[dict] = []
    angles = np.arange(0.0, 180.0, ANGLE_STEP)
    while len(found) < limit and pool.shape[0] >= 200:
        best: dict | None = None
        for angle in angles:
            candidate = _spike(pool, float(angle))
            if candidate is None:
                continue
            if best is None or candidate['share'] > best['share']:
                best = candidate
        if best is None:
            break
        refined = best
        for angle in np.arange(best['angle'] - ANGLE_WINDOW,
                               best['angle'] + ANGLE_WINDOW + 0.01, ANGLE_REFINE):
            candidate = _spike(pool, float(angle))
            if candidate is not None and candidate['share'] > refined['share']:
                refined = candidate
        if refined['width'] < MIN_WIDTH_PX or refined['length'] < 10.0 * MIN_WIDTH_PX:
            break
        found.append(refined)
        # Take this band's points out and look again, so a second bright band —
        # a kerb, a paved edge — is graded against the tape instead of hiding
        # behind it.
        projected = (pool @ refined['across']) - refined['centre_v']
        pool = pool[np.abs(projected) > 0.5 * refined['width']]
    return found


def _sample(gray: np.ndarray, band: dict, us: np.ndarray, vs: np.ndarray) -> tuple:
    """Tone and in-frame mask on a grid laid along the band's own axis."""
    centre = band['centre']
    axis = band['axis']
    across = band['across']
    uu, vv = np.meshgrid(us, vs)
    xs = centre[0] + uu * axis[0] + vv * across[0]
    ys = centre[1] + uu * axis[1] + vv * across[1]
    height, width = gray.shape
    inside = (xs >= 0) & (xs < width - 1) & (ys >= 0) & (ys < height - 1)
    ix = np.clip(np.rint(xs).astype(int), 0, width - 1)
    iy = np.clip(np.rint(ys).astype(int), 0, height - 1)
    return gray[iy, ix], inside


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Contiguous True runs of a 1-D flag array, as inclusive index pairs."""
    edges = np.diff(np.concatenate(([0], flags.astype(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    stops = np.flatnonzero(edges == -1) - 1
    return list(zip(starts.tolist(), stops.tolist()))


def _cluster(
    runs: list[tuple[int, int]], vs: np.ndarray, current: float, seed_width: float
) -> tuple[int, int] | None:
    """The runs that belong to the blade at `current`, unioned into one extent."""
    near = [
        (begin, end)
        for begin, end in runs
        if abs(float(vs[(begin + end) // 2]) - current) <= CORE_SHARE * seed_width
    ]
    if not near:
        return None
    return min(run[0] for run in near), max(run[1] for run in near)


def _widen(gray: np.ndarray, band: dict, stations: int = 21) -> tuple[float, float] | None:
    """The blade's full width, measured outward from the seed's centre line.

    The seed is a band of the *brightest* pixels, and the brightest part of a
    tape is the blank margin beside its printed row — the ticks themselves are
    dark. So the seed's own width is a slice of the blade, not the blade: on a
    synthetic frame with a known 103 px blade it came out at 17 px, and the walk
    that followed then measured 15 px slivers and 40 px merges of the same blade
    and called it a band of changing width.

    The seed's *centre line* is right, though, so the edges are found by walking
    out from that line until the tone drops to the midpoint between blade and
    ground, at many stations along it, and taking the median.
    """
    reach = 0.25 * min(gray.shape)
    us = np.linspace(-0.45 * band['length'], 0.45 * band['length'], stations)
    vs = np.arange(-reach, reach, 1.0)
    tone, inside = _sample(gray, band, us, vs)
    widths: list[float] = []
    offsets: list[float] = []
    near = np.abs(vs) <= max(10.0, 0.35 * band['width'])
    for column in range(stations):
        if inside[:, column].mean() < 0.95:
            continue
        values = tone[:, column]
        # Close the millimetre marks before walking. A mark is a pixel or two of
        # dark across the blade, and it is as dark as the ground, so a walk that
        # stops at the first dark pixel stops on the printed row and reports half
        # a blade. A 5-wide maximum fills those marks and nothing else.
        padded = np.pad(values, 2, mode='edge')
        bridged = np.lib.stride_tricks.sliding_window_view(padded, 5).max(axis=1)
        values = bridged
        ground = float(np.percentile(values, 10))
        # Start from the brightest pixel near the centre line: the line may be
        # crossing a tick, and a dark start would walk nowhere.
        start = int(np.flatnonzero(near)[np.argmax(values[near])])
        blade = float(values[start])
        if blade - ground < 15.0:
            continue
        level = 0.5 * (blade + ground)
        low = start
        while low > 0 and values[low - 1] >= level:
            low -= 1
        high = start
        while high < values.size - 1 and values[high + 1] >= level:
            high += 1
        if low == 0 or high == values.size - 1:
            continue
        # The maximum filter widened each edge by two pixels; take them back so
        # the width is the blade's rather than the filter's.
        widths.append(float(vs[min(high, values.size - 1)] - vs[low] - 4.0))
        offsets.append(float(0.5 * (vs[max(high - 2, 0)] + vs[min(low + 2, vs.size - 1)])))
    if len(widths) < 5:
        return None
    return float(np.median(widths)), float(np.median(offsets))


def _smooth(values: np.ndarray, span: int = WIDTH_SMOOTH) -> np.ndarray:
    """A median filter, so one odd station does not set the width spread."""
    if values.size <= span:
        return values
    half = span // 2
    padded = np.pad(values, half, mode='edge')
    windows = np.lib.stride_tricks.sliding_window_view(padded, span)
    return np.median(windows, axis=1)[: values.size]


def _walk(gray: np.ndarray, band: dict) -> dict:
    """Track the bright run across the band, station by station.

    The seed's middle station sets the starting position; each station then
    takes the bright run closest to where the last one found the blade. That is
    what keeps a printed number, or a bright wall beyond the blade, from pulling
    the track off it: choosing the brightest run instead is exactly how the
    paved edge won before.
    """
    us = np.linspace(
        -SPAN_SHARE * band['length'] / 2.0,
        SPAN_SHARE * band['length'] / 2.0,
        STATIONS,
    )
    half_window = WINDOW_SHARE * band['width']
    rows = max(25, int(round(2.0 * half_window)))
    vs = np.linspace(-half_window, half_window, rows)
    tone, inside = _sample(gray, band, us, vs)
    frame = inside.mean(axis=0) >= 0.9
    step = float(vs[1] - vs[0])

    seed_width = band['width']
    centres = np.full(us.size, np.nan)
    widths = np.full(us.size, np.nan)
    start_column = us.size // 2

    # One brightness level for the whole walk, taken from the sampled window as
    # a whole rather than from each station. A per-station threshold moves with
    # whatever is beside the tape — soil at one end, sunlit sand at the other —
    # so a tape that is 18 px wide all the way along came back varying by 40%
    # and was rejected as changing width.
    level = 0.5 * (float(np.percentile(tone, 20)) + float(np.percentile(tone, 80)))

    for direction in (1, -1):
        current = 0.0
        column = start_column
        while 0 <= column < us.size:
            if not frame[column]:
                column += direction
                continue
            values = tone[:, column]
            if float(values.max() - values.min()) >= 10.0:
                core = _cluster(_runs(values >= level), vs, current, seed_width)
                if core is not None:
                    begin, end = core
                    extent = (end - begin + 1) * step
                    inside_window = not (begin == 0 and end == rows - 1)
                    if (inside_window
                            and WIDTH_LOW * seed_width
                            <= extent
                            <= WIDTH_HIGH * seed_width):
                        current = float(vs[begin : end + 1].mean())
                        centres[column] = current
                        widths[column] = extent
            column += direction

    return {'us': us, 'vs': vs, 'tone': tone, 'centres': centres,
            'widths': widths, 'frame': frame}


def _fit(track: dict) -> dict:
    """A straight line through the tracked centres, and how well it holds.

    The band is measured across its whole span rather than as one continuous
    run: the clip is a dozen stations of missing blade in the middle of the
    tape, and requiring continuity would refuse every photo the reader exists
    to measure.
    """
    us = track['us']
    centres = track['centres']
    widths = track['widths']
    valid = np.isfinite(centres) & np.isfinite(widths) & track['frame']
    if int(valid.sum()) < 20:
        return {'ok': False,
                'reason': 'the bright band could not be followed along its length'}

    u = us[valid]
    v = centres[valid]
    w = widths[valid]
    low, high = np.percentile(u, [2, 98])
    span = (us >= low) & (us <= high) & track['frame']
    stations = int(np.count_nonzero(span)) or 1
    coverage = float(np.count_nonzero(valid & span)) / float(stations)
    if high - low < 5.0 * float(np.median(w)):
        return {'ok': False, 'reason': 'the bright band is too short to be a tape'}

    inside = (u >= low) & (u <= high)
    u, v, w = u[inside], v[inside], w[inside]
    # A straight line, trimmed to the stations that agree with it. A tape is a
    # floppy ribbon and does bend a little over a pipe, which is why the bend
    # gate is judged in pixels as well as in blade widths — a parabola was tried
    # here and fitted so freely that a band on the wrong bright edge of the
    # frame passed, so the straight line stays and only the gate is lenient.
    slope, intercept = np.polyfit(u, v, 1)
    for _ in range(2):
        residual = np.abs(v - (slope * u + intercept))
        near = residual < 2.0 * float(np.median(w))
        if near.sum() < 20:
            break
        slope, intercept = np.polyfit(u[near], v[near], 1)
        u, v, w = u[near], v[near], w[near]
    residual = np.abs(v - (slope * u + intercept))
    w = _smooth(w)
    width = float(np.median(w))
    # How much of the blade agrees with that width. A band whose width genuinely
    # wanders has most stations outside it; a good blade with a clip in it and
    # sunlit sand beside one end has a minority of stations outside it, and those
    # are the stations that must not refuse the whole photo.
    agree = float(np.mean(np.abs(w - width) <= 0.3 * width))
    quartiles = np.percentile(w, [25, 75])
    walked = float(np.count_nonzero(track['frame'])) or 1.0
    return {
        'ok': True,
        'slope': float(slope),
        'offset': float(intercept),
        'width': width,
        'spread': float(quartiles[1] - quartiles[0]) / max(1.0, width),
        'bend': float(np.median(residual)) / max(1.0, width),
        'coverage': coverage,
        'support': float(stations) / walked,
        'agree': agree,
        'length': float(u[-1] - u[0]),
        'low': float(u[0]),
        'high': float(u[-1]),
    }


def _periodicity(track: dict, fit: dict) -> float:
    """How strongly the band's length repeats, which is what a ruler does.

    Averaged over the whole blade rather than its middle, because the marks sit
    along the edges and the middle of a tape carries the printed numbers. The
    slow brightness trend is removed first, or the peak is just the shading.
    """
    us = track['us']
    tone = track['tone']
    vs = track['vs']
    rows = np.abs(vs) <= 0.8 * fit['width']
    if rows.sum() < 3:
        return 0.0
    profile = tone[rows].mean(axis=0)
    keep = (us >= fit['low']) & (us <= fit['high'])
    values = profile[keep]
    if values.size < 4 * MIN_TICK_LAG:
        return 0.0
    window = max(3, values.size // 12)
    trend = np.convolve(values, np.ones(window) / window, mode='same')
    signal = values - trend
    energy = float(np.dot(signal, signal))
    if energy <= 1e-6:
        return 0.0
    best = 0.0
    for lag in range(MIN_TICK_LAG, min(MAX_TICK_LAG, signal.size // 3)):
        best = max(best, float(np.dot(signal[:-lag], signal[lag:])) / energy)
    return max(0.0, min(1.0, best))


def _band_from_fit(track: dict, fit: dict, band: dict) -> dict:
    """Turn the fitted line back into a band in the seed's coordinate frame."""
    axis = band['axis'] + band['across'] * fit['slope']
    axis = axis / float(np.hypot(axis[0], axis[1]))
    if axis[0] < 0:
        axis = -axis
    across = np.array([-axis[1], axis[0]])
    middle = 0.5 * (fit['low'] + fit['high'])
    centre = (
        band['centre']
        + band['axis'] * middle
        + band['across'] * (fit['slope'] * middle + fit['offset'])
    )
    return {
        'centre': centre,
        'axis': axis,
        'across': across,
        'width': fit['width'],
        'length': fit['length'],
        'coverage': fit['coverage'],
        'support': fit['support'],
        'width_spread': fit['spread'],
        'width_agree': fit['agree'],
        'bend': fit['bend'],
    }


def _grade(seed: dict, gray: np.ndarray) -> dict:
    """Track one seed to convergence and say whether it is a tape."""
    band = dict(seed)
    widened = _widen(gray, band)
    if widened is not None:
        band['width'], offset = widened
        band['centre'] = band['centre'] + band['across'] * offset
    track = _walk(gray, band)
    fit = _fit(track)
    for _ in range(2):
        if not fit['ok']:
            break
        band = _band_from_fit(track, fit, band)
        track = _walk(gray, band)
        fit = _fit(track)
    if not fit['ok']:
        return {'ok': False, 'reason': fit['reason']}
    graded = _band_from_fit(track, fit, band)
    graded['periodicity'] = _periodicity(track, fit)
    if graded['coverage'] < MIN_COVERAGE:
        return {'ok': False, 'reason': 'the bright band breaks up along its length '
                                       '(held %.0f%% of its stations)'
                                       % (100.0 * graded['coverage'])}
    if graded['support'] < MIN_SUPPORT:
        return {'ok': False, 'reason': 'the bright band is too short to be a tape '
                                       '(%.0f%% of the span looked at)'
                                       % (100.0 * graded['support'])}
    if graded['width_spread'] > MAX_WIDTH_SPREAD:
        return {'ok': False, 'reason': 'the bright band changes width along its '
                                       'length (%.2f)' % graded['width_spread']}
    # Wobble has to be judged against the pixels it is measured in: half a pixel
    # of tracking noise on a 12 px blade reads as a curve in blade-widths, and a
    # synthetic straight blade came back "curving" by 1.5 px that way.
    # A tape is a floppy ribbon: it bends over a pipe or a trench lip, and on the
    # field set every refusal that was left was 3-7 px of drift — a bend, not a
    # lost track. Several pixels of drift is the tape; a frame's worth is not.
    if graded['bend'] > MAX_BEND and graded['bend'] * graded['width'] > 7.0:
        return {'ok': False, 'reason': 'the bright band curves (%.2f of its width, '
                                       '%.1f px)'
                                       % (graded['bend'],
                                          graded['bend'] * graded['width'])}
    # A blade is long and thin, but a close-up of one is only a handful of times
    # longer than it is wide: the synthetic blade is 700 by 110, and the widening
    # measures it about 10% over, so a 5x gate refused the real blade and let a
    # 31 px margin of it win. Three and a half times still rejects the round
    # bright things — a pipe end, a bucket, a paved patch.
    if graded['length'] < 3.5 * graded['width']:
        return {'ok': False, 'reason': 'the bright band is not long enough to be a tape'}
    graded['ok'] = True
    # Among bands that all hold together, the tape is the most elongated one: it
    # crosses the frame while staying one blade wide, where a pipe or a trench
    # edge that happens to be bright is short and fat.
    thin = min(1.0, graded['length'] / (25.0 * graded['width']))
    graded['score'] = (
        graded['coverage']
        * (1.0 - min(0.5, graded['width_spread']))
        * thin
        + 0.2 * graded['periodicity']
    )
    return graded


def _is_fragment(narrow: dict, wide: dict) -> bool:
    """True when a band is a slice of another band on the same blade.

    One tape produces several seeds — a blank margin, the printed row, the
    margin on the other side — and they grade cleanly on their own. On the
    shaded synthetic blade the margin seed scored highest of all and reported a
    37 px band on a 110 px blade. A narrower band running parallel to a wider
    one, with its centre inside it, is that wider band seen partially: it is
    dropped, and the blade itself is what gets returned.
    """
    if wide['width'] <= 1.15 * narrow['width']:
        return False
    if abs(float(narrow['axis'] @ wide['axis'])) < 0.99:
        return False
    offset = abs(float((narrow['centre'] - wide['centre']) @ wide['across']))
    return offset < 0.5 * wide['width'] - 0.5 * narrow['width']


def _find_at(rgb: np.ndarray, work_edge: int) -> dict:
    """One attempt at one working resolution."""
    if rgb is None or getattr(rgb, 'ndim', 3) != 3:
        return {'found': False, 'reason': 'the photo is not a colour image'}
    if rgb.shape[0] < 80 or rgb.shape[1] < 80:
        return {'found': False, 'reason': 'the photo is too small to hold a blade'}

    original_height, original_width = rgb.shape[:2]
    image = Image.fromarray(rgb).convert('RGB')
    if max(image.width, image.height) > work_edge:
        image.thumbnail((work_edge, work_edge), Image.BILINEAR)
    gray = np.asarray(image, dtype=np.float32) @ np.array(
        [0.299, 0.587, 0.114], dtype=np.float32
    )
    to_original = original_width / float(image.width)

    seeds_image = image
    if max(image.width, image.height) > SEED_EDGE:
        seeds_image = image.copy()
        seeds_image.thumbnail((SEED_EDGE, SEED_EDGE), Image.BILINEAR)
    seeds_gray = np.asarray(seeds_image, dtype=np.float32) @ np.array(
        [0.299, 0.587, 0.114], dtype=np.float32
    )

    to_work = seeds_image.width / float(image.width)
    seeds = [
        {
            'centre': seed['centre'] / to_work,
            'axis': seed['axis'],
            'across': seed['across'],
            'length': seed['length'] / to_work,
            'width': seed['width'] / to_work,
            'share': seed['share'],
        }
        for seed in _seeds(seeds_gray)
    ]
    if not seeds:
        return {'found': False,
                'reason': 'no band of bright pixels in the frame could be a blade'}

    graded: list[dict] = []
    refusals: list[str] = []
    for seed in seeds:
        if seed['width'] < MIN_WIDTH_PX or seed['length'] < 20.0:
            continue
        result = _grade(seed, gray)
        if result.get('ok'):
            graded.append(result)
        else:
            refusals.append(result.get('reason') or 'a candidate band was rejected')

    if not graded:
        return {
            'found': False,
            'reason': refusals[0] if refusals else 'no band survived the check',
            'seeds': len(seeds),
        }

    whole = [
        band
        for band in graded
        if not any(
            _is_fragment(band, other) for other in graded if other is not band
        )
    ]
    best = max(whole or graded, key=lambda item: item['score'])
    best['competitors'] = len(whole or graded)
    centre = [
        round(float(best['centre'][0]) * to_original, 1),
        round(float(best['centre'][1]) * to_original, 1),
    ]
    width_px = round(float(best['width']) * to_original, 1)
    length_px = round(float(best['length']) * to_original, 1)
    axis = [round(float(best['axis'][0]), 4), round(float(best['axis'][1]), 4)]
    across = [round(float(best['across'][0]), 4), round(float(best['across'][1]), 4)]
    return {
        'found': True,
        'reason': None,
        'centre': centre,
        'axis': axis,
        'across': across,
        'width_px': width_px,
        'length_px': length_px,
        'angle_deg': round(float(np.degrees(np.arctan2(axis[1], axis[0]))), 2),
        'coverage': round(float(best['coverage']), 3),
        'support': round(float(best['support']), 3),
        'width_spread': round(float(best['width_spread']), 3),
        'width_agree': round(float(best['width_agree']), 3),
        'bend': round(float(best['bend']), 3),        'periodicity': round(float(best['periodicity']), 3),
        'competitors': int(best.get('competitors', 1)),
        'score': round(float(best['score']), 3),
        'scale': round(float(to_original), 3),
        'band': {
            'centre': centre,
            'axis': axis,
            'across': across,
            'length_px': length_px,
            'width_px': width_px,
        },
    }


def find_band(rgb: np.ndarray, work_edge: int = WORK_EDGE) -> dict:
    """The blade in a photo: where it runs, how wide it is, and how sure we are.

    Refuses rather than returning a plausible-looking wall. Keys: `found`,
    `reason`, and on success `centre`, `axis`, `across`, `width_px`, `length_px`,
    `angle_deg`, `coverage`, `support`, `width_spread`, `bend`, `periodicity`.
    All positions are in the original photo's pixels.

    Two working resolutions are tried and the better band kept. The same photo
    grades differently at different scales — `20261124_140332` is refused at 900
    and localised at 700 — because the marks and the clip subtend different
    amounts of a station's width. One scale is a coin toss on marginal frames;
    two is cheap.
    """
    attempts = [
        _find_at(rgb, max(200, int(round(work_edge * factor))))
        for factor in (1.0, 0.78, 1.2)
    ]
    found = [result for result in attempts if result.get('found')]
    if found:
        found.sort(key=lambda result: result.get('score', 0.0), reverse=True)
        best = found[0]
        # Two resolutions agreeing on where the band is and which way it runs is
        # the strongest evidence available without the clip. They can disagree:
        # on `20261124_140332` one resolution refuses and the other returns a
        # band at -33 degrees where the tape runs at +12 — a trench edge. That
        # result is returned marked unconfirmed rather than dropped, so a caller
        # can insist on confirmation before it reads a measurement off it.
        # Confirmed means a *second* resolution independently found the same
        # band at the same angle (within 8 degrees). Three resolutions are tried
        # because two was too easy to fool the other way: on a clean synthetic
        # blade one of the pair could fail outright and a perfectly good band
        # came back unconfirmed.
        agreeing = [
            other
            for other in found[1:]
            if abs(other['angle_deg'] - best['angle_deg']) <= 8.0
        ]
        best['confirmed'] = bool(agreeing)
        if agreeing:
            best['angle_gap_deg'] = round(
                float(max(abs(o['angle_deg'] - best['angle_deg']) for o in agreeing)),
                1,
            )
            # Same direction, different widths: the narrower one is a slice of
            # the blade (a blank margin), so the wider is the tape. This is what
            # a shaded blade needs, where the margin seed wins on score at one
            # resolution and comes back 31 px wide against a 110 px blade.
            best = max([best] + agreeing, key=lambda result: result['width_px'])
        return best
    reasons = [result['reason'] for result in attempts if result.get('reason')]
    return {
        'found': False,
        'reason': reasons[0] if reasons else 'no band was found',
        'attempts': reasons,
    }


def find_band_bytes(data: bytes, work_edge: int = WORK_EDGE) -> dict:
    """The blade in an uploaded photo, with the EXIF rotation applied."""
    import io

    from PIL import ImageOps

    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    except Exception:
        return {'found': False, 'reason': 'the upload could not be read as a photo'}
    return find_band(np.asarray(image), work_edge=work_edge)


def annotate(rgb: np.ndarray, band: dict, width: int = 1200) -> Image.Image:
    """The photo with the tracked band's centre line and its two edges drawn."""
    from PIL import ImageDraw

    image = Image.fromarray(rgb).convert('RGB')
    scale = min(1.0, width / float(image.width))
    if scale < 1.0:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.BILINEAR,
        )
    draw = ImageDraw.Draw(image)
    if not band.get('found'):
        draw.text((12, 12), band.get('reason') or 'no band found', fill=(255, 40, 40))
        return image
    centre = np.array(band['centre']) * scale
    axis = np.array(band['axis'])
    across = np.array(band['across'])
    half = band['length_px'] * scale / 2.0
    edge = band['width_px'] * scale / 2.0
    for offset, colour in ((0.0, (255, 0, 0)), (edge, (0, 255, 0)),
                           (-edge, (0, 255, 0))):
        point = centre + across * offset
        draw.line([tuple(point - axis * half), tuple(point + axis * half)],
                  fill=colour, width=3)
    draw.text(tuple(centre + across * (edge + 8)),
              'W=%.0fpx' % band['width_px'], fill=(0, 255, 0))
    return image
