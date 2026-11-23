"""Fit the tick ladder on a straightened blade and measure its pitch.

The blade is its own ruler: once the ticks are on a known pixel pitch, the
distance between two object edges is millimetres regardless of which numbers the
OCR managed to read. That is what makes sub-number accuracy possible, and what
turns "the tape reads 40 to 50 cm" into a measured depth.

Three decisions came out of measuring the field set:

* The tick band is *found*, not assumed. A fixed bottom slice of the blade put
  the printed numbers inside the profile, and the autocorrelation then reported
  their pitch (60 px, the major-mark spacing) instead of the marks' (6 px).
  Stripes of the strip are scored and the most periodic one wins.
* Pitch comes from the fundamental of that autocorrelation, not its strongest
  peak: a comb correlates perfectly at its own pitch and equally well at every
  multiple, so the harmonic usually wins on raw amplitude.
* Marks are fitted as a lattice (position = phase + k · pitch) and the residual
  is kept. A confident wrong number in a measurement record is worse than none,
  so the caller must be able to refuse a ladder that wanders.

Env: none.
"""

from __future__ import annotations

import numpy as np

# Below this the ticks are not resolvable from the pixel grid; above it they are
# not ticks but stripes of ground.
MIN_PITCH_PX = 2.5
MAX_PITCH_PX = 60.0

# Stripes of the blade searched for the tick row, as a share of its height.
STRIPE_SHARE = 0.14

# Ink is darker than the blade by this many gray levels.
MIN_INK_OFFSET = 18.0

# A column with less ink than this is blade, not a tick.
MIN_COLUMN_INK = 0.08

# A periodic component weaker than this is texture, not printing.
MIN_SCORE = 0.18

# A lattice whose marks wander more than this share of a pitch is not a printed
# ladder.
MAX_RESIDUAL = 0.18

# Fewer marks than this cannot set a scale, however regular they look.
MIN_TICKS = 6

# A tick this much longer than the ordinary one is a major (cm or 5 mm) mark.
MAJOR_RATIO = 1.4

# A candidate fundamental has to carry this share of the strongest periodicity
# to be preferred over a harmonic.
FUNDAMENTAL_SHARE = 0.7


def _ink_profile(band: np.ndarray) -> np.ndarray:
    """Share of dark pixels per column across one stripe."""
    pixels = band.astype(np.float32)
    if not pixels.size:
        return np.zeros(0, dtype=np.float32)
    blade = float(np.median(pixels))
    return (pixels < (blade - MIN_INK_OFFSET)).mean(axis=0)


def _detrended(profile: np.ndarray) -> np.ndarray:
    """The profile with its slow brightness drift removed.

    A blade lit from one end is brighter there, which would otherwise hide the
    marks at the dark end.
    """
    if profile.size < 8:
        return profile
    window = max(9, profile.size // 8)
    trend = np.convolve(profile, np.ones(window) / window, mode='same')
    return np.clip(profile - trend, 0.0, None)


def _autocorrelation(profile: np.ndarray) -> np.ndarray:
    centred = profile - profile.mean()
    size = profile.size
    spectrum = np.fft.rfft(centred, n=2 * size)
    correlation = np.fft.irfft(spectrum * np.conj(spectrum))[:size]
    return correlation / correlation[0] if correlation[0] > 0 else correlation


def _fundamental(correlation: np.ndarray, low: int, high: int) -> float:
    """The shortest lag that still correlates as well as the best one.

    A 6 px comb correlates at 6, 12, 18 … and the strongest peak is often the
    major-mark spacing further out. The mark pitch is the first lag that carries
    most of that strength.
    """
    window = correlation[low : high + 1]
    if not window.size:
        return 0.0
    best = float(window.max())
    if best <= 0:
        return 0.0
    for index in range(1, len(window) - 1):
        if (
            window[index] >= best * FUNDAMENTAL_SHARE
            and window[index] >= window[index - 1]
            and window[index] >= window[index + 1]
        ):
            return float(low + index)
    return float(low + int(np.argmax(window)))


def pitch_from_profile(profile: np.ndarray) -> tuple[float, float]:
    """(pitch in pixels, periodicity score 0–1) of a column ink profile."""
    if profile.size < 32:
        return 0.0, 0.0
    correlation = _autocorrelation(profile)
    if not np.any(correlation):
        return 0.0, 0.0

    low = max(2, int(round(MIN_PITCH_PX)))
    high = min(int(profile.size // 3), int(round(MAX_PITCH_PX)))
    if high <= low + 1:
        return 0.0, 0.0
    lag = _fundamental(correlation, low, high)
    if lag <= 0:
        return 0.0, 0.0
    lag = int(round(lag))
    score = float(correlation[lag])

    # Sub-pixel refinement: a pitch rounded to whole pixels becomes an 8% scale
    # error on a 6 px ladder.
    if 0 < lag < correlation.size - 1:
        left, middle, right = correlation[lag - 1], correlation[lag], correlation[lag + 1]
        curvature = left - 2.0 * middle + right
        if abs(curvature) > 1e-9:
            lag = lag + 0.5 * (left - right) / curvature
    return float(lag), score


def _tick_lengths(pixels: np.ndarray, positions: list[int]) -> list[float]:
    """Length of each mark from whichever blade edge it hangs off.

    Ticks are printed from one edge inwards, so a major (cm or 5 mm) mark is a
    longer run of ink than an ordinary one. Measuring in a stripe hides that: a
    stripe thin enough to isolate the marks is filled by either. The printed
    digits float above the marks, so they never extend a run.
    """
    if not positions:
        return []
    ink = pixels < (float(np.median(pixels)) - MIN_INK_OFFSET)
    height, width = ink.shape
    lengths: list[float] = []
    for position in positions:
        column = ink[:, min(max(position, 0), width - 1)]
        bottom = 0
        for row in range(height - 1, -1, -1):
            if not column[row]:
                break
            bottom += 1
        top = 0
        for row in range(height):
            if not column[row]:
                break
            top += 1
        lengths.append(float(max(bottom, top)))
    return lengths


def _major_every(positions: list[int], heights: list[float], pitch: float) -> int | None:
    """Every how many marks the long (cm or 5 mm) ticks repeat."""
    if len(positions) < 3 or pitch <= 0:
        return None
    marks = np.array(heights, dtype=float)
    if not marks.size or float(marks.max()) <= 0:
        return None
    major = np.flatnonzero(marks >= np.median(marks) * MAJOR_RATIO)
    if major.size < 3:
        return None
    step = float(np.median(np.diff(np.array(positions, dtype=float)[major]))) / pitch
    return int(round(step)) if step >= 1.5 else None


def _fit_lattice(profile: np.ndarray, pitch: float) -> dict:
    """Marks on one pitch, their residual, and how many are real."""
    best_phase, best_weight = 0.0, -1.0
    for phase in np.arange(0.0, pitch, max(0.5, pitch / 16.0)):
        index = np.round(np.arange(phase, profile.size, pitch)).astype(int)
        index = index[index < profile.size]
        if not index.size:
            continue
        weight = float(profile[index].sum())
        if weight > best_weight:
            best_phase, best_weight = float(phase), weight

    half = max(1, int(round(pitch * 0.3)))
    positions: list[int] = []
    heights: list[float] = []
    for predicted in np.arange(best_phase, profile.size, pitch):
        centre = int(round(predicted))
        left, right = max(0, centre - half), min(profile.size, centre + half + 1)
        if right - left < 1:
            continue
        window = profile[left:right]
        peak = int(np.argmax(window))
        if float(window[peak]) < MIN_COLUMN_INK:
            continue
        positions.append(left + peak)
        heights.append(float(window[peak]))

    empty = {
        'pitch': float(pitch),
        'residual': 1.0,
        'count': len(positions),
        'coverage': 0.0,
        'positions': [],
        'major_every': None,
    }
    if len(positions) < 3:
        return empty

    steps = np.arange(len(positions), dtype=float)
    measured = np.array(positions, dtype=float)
    slope, intercept = np.polyfit(steps, measured, 1)
    if slope <= 0:
        return empty
    deviations = measured - (intercept + slope * steps)
    residual = float(np.sqrt(np.mean(deviations**2)) / slope)

    return {
        'pitch': float(slope),
        'residual': residual,
        'count': len(positions),
        'coverage': float((positions[-1] - positions[0]) / max(1, profile.size - 1)),
        'positions': positions,
        # Majors are measured from the whole blade in `read_strip`, where the
        # edge the marks hang off is visible; a stripe cannot tell them apart.
        'major_every': None,
    }


def read_strip(strip) -> dict | None:
    """The tick ladder of a rectified blade strip, or None when there is none.

    Returns the best the stripes could do even when it is not usable, with
    ``usable`` telling the caller so: a response can then say the blade was
    found but its marks were uneven, which is a different instruction to the
    engineer than "no tape here".
    """
    if strip is None:
        return None
    pixels = np.asarray(strip.convert('L'), dtype=np.float32)
    if pixels.ndim != 2 or pixels.shape[1] < 80 or pixels.shape[0] < 24:
        return None

    height, width = pixels.shape
    stripe = max(10, int(height * STRIPE_SHARE))
    starts = range(0, max(1, height - stripe + 1), max(4, height // 10))

    best: dict | None = None
    for start in starts:
        profile = _detrended(_ink_profile(pixels[start : start + stripe, :]))
        pitch, score = pitch_from_profile(profile)
        if not (MIN_PITCH_PX <= pitch <= MAX_PITCH_PX) or score < MIN_SCORE:
            continue
        lattice = _fit_lattice(profile, pitch)
        lattice['score'] = round(score, 3)
        lattice['stripe'] = start
        lattice['usable'] = (
            lattice['count'] >= MIN_TICKS
            and lattice['residual'] <= MAX_RESIDUAL
            and score >= MIN_SCORE
        )
        if best is None or (lattice['usable'], lattice['score']) > (
            best['usable'],
            best['score'],
        ):
            best = lattice

    # Major spacing is measured over the whole blade, not the stripe: a stripe
    # that happens to sit inside the long marks contains only majors, and would
    # then report no major spacing at all.
    if best is not None and best['positions']:
        best['major_every'] = _major_every(
            best['positions'],
            _tick_lengths(pixels, best['positions']),
            best['pitch'],
        )
    return best
