"""Probe: recover mm-per-pixel from a photographed tape.

Two independent estimates are compared:
  A. printed pitch  - the ladder is periodic; ACF on the rectified ink profile gives
                      the pixel period of the number/tick family (no reading needed)
  B. OCR fit        - digits read inside the blade ROI and fitted value~x

A scale is accepted only when both roughly agree. Saves the rectified blade crop so
the detector can be inspected.

Run: .venv/Scripts/python.exe tape_scale_probe.py [photo ...]
"""
from __future__ import annotations

import glob
import os
import re
import sys
from collections import deque

import numpy as np
from PIL import Image, ImageOps

DS = 4
DEBUG_DIR = "_scale_debug"


# ---------------------------------------------------------------- detection
def candidate_masks(a: np.ndarray):
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    maxc, minc = a.max(axis=2), a.min(axis=2)
    return {
        "yellow": (g > 145) & (g >= b + 30) & (r >= b + 10),
        "white": (maxc > 135) & ((maxc - minc) < 40),
    }


def dilate(m: np.ndarray, k: int = 2) -> np.ndarray:
    out = m.copy()
    for _ in range(k):
        p = np.pad(out, 1)
        out = (p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:] | p[1:-1, 1:-1])
    return out


def components(mask: np.ndarray, min_area: int):
    h, w = mask.shape
    seen = np.zeros_like(mask)
    out = []
    for y in range(h):
        for x in range(w):
            if not mask[y, x] or seen[y, x]:
                continue
            q = deque([(y, x)])
            seen[y, x] = True
            n = 0
            y0 = y1 = y
            x0 = x1 = x
            while q:
                cy, cx = q.popleft()
                n += 1
                y0, y1 = min(y0, cy), max(y1, cy)
                x0, x1 = min(x0, cx), max(x1, cx)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            if n >= min_area:
                out.append((n, y0, y1, x0, x1))
    return out


def pick_blade(a: np.ndarray):
    best = None
    for name, full in candidate_masks(a).items():
        h, w = full.shape
        small = full[: h // DS * DS, : w // DS * DS].reshape(h // DS, DS, w // DS, DS).mean(axis=(1, 3)) > 0.4
        small = dilate(small, 2)
        for n, y0, y1, x0, x1 in components(small, 60):
            bw, bh = x1 - x0 + 1, y1 - y0 + 1
            fill = n / (bw * bh)  # dilated, so inflated
            aspect = max(bw, bh) / max(1, min(bw, bh))
            if aspect < 3.0:
                continue
            score = n * min(aspect, 25)
            if best is None or score > best[0]:
                best = (score, name, (x0 * DS, y0 * DS, min(w, (x1 + 1) * DS), min(h, (y1 + 1) * DS)), aspect)
    return best


def rectify(im: Image.Image, box, mask: np.ndarray):
    sub_img = im.crop(box)
    sub_mask = mask[box[1]:box[3], box[0]:box[2]].astype(np.uint8) * 255
    ys, xs = np.nonzero(sub_mask)
    if len(xs) < 50:
        return None, 0.0
    pts = np.stack([xs, ys], 1).astype(float)
    c = pts - pts.mean(axis=0)
    _, vecs = np.linalg.eigh(np.cov(c.T))
    axis = vecs[:, -1]
    theta = float(np.degrees(np.arctan2(axis[1], axis[0])))
    best = None
    for angle in (theta, theta + 90, theta - 90, theta + 180):
        rot = sub_img.rotate(angle, resample=Image.BICUBIC, expand=True)
        rotm = Image.fromarray(sub_mask).rotate(angle, resample=Image.NEAREST, expand=True)
        mb = rotm.getbbox()
        if not mb:
            continue
        crop = rot.crop(mb)
        ar = crop.width / max(1, crop.height)
        if best is None or ar > best[0]:
            best = (ar, crop, angle)
    if best is None:
        return None, 0.0
    return best[1], best[2]


# ---------------------------------------------------------------- pitch
def acf_peaks(profile: np.ndarray, lo: int = 6, hi: int = 160):
    x = profile.astype(float)
    if len(x) < hi * 3:
        hi = max(lo + 5, len(x) // 3)
    x = x - np.convolve(x, np.ones(31) / 31, mode="same")
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac = ac / (ac[0] + 1e-9)
    peaks = [(lag, float(ac[lag])) for lag in range(lo, min(hi, n // 2) - 1)
             if ac[lag] > ac[lag - 1] and ac[lag] >= ac[lag + 1] and ac[lag] > 0.12]
    return sorted(peaks, key=lambda p: -p[1])


def blob_runs(profile: np.ndarray, min_w: int, max_w: int):
    thr = max(1.0, np.median(profile) * 1.4)
    on = profile > thr
    idx = np.flatnonzero(np.diff(np.concatenate(([0], on.astype(np.int8), [0]))))
    starts, ends = idx[0::2], idx[1::2]
    return np.array([(s + e) / 2 for s, e in zip(starts, ends) if min_w <= e - s <= max_w])


# ---------------------------------------------------------------- OCR
def ocr_scale(crop: Image.Image):
    """Fit digit values to x positions inside the rectified blade. Returns (mm/px, detail)."""
    scale = 4 if crop.width < 1400 else 2
    g = ImageOps.autocontrast(crop.convert("L").resize((crop.width * scale, crop.height * scale), Image.LANCZOS))
    best = None
    for psm in (6, 11, 7):
        d = pytesseract.image_to_data(g, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789",
                                      output_type=pytesseract.Output.DICT)
        toks = []
        for i, t in enumerate(d["text"]):
            t = (t or "").strip()
            if not re.fullmatch(r"\d{1,3}", t):
                continue
            if float(d["conf"][i]) < 40:
                continue
            toks.append(dict(v=int(t), x=(d["left"][i] + d["width"][i] / 2) / scale,
                             y=(d["top"][i] + d["height"][i] / 2) / scale,
                             h=d["height"][i] / scale, c=float(d["conf"][i])))
        if len(toks) < 5:
            continue
        tol = max(6.0, np.median([t["h"] for t in toks]) * 0.7)
        rows = []
        for t in sorted(toks, key=lambda t: t["y"]):
            for r in rows:
                if abs(np.mean([q["y"] for q in r]) - t["y"]) <= tol:
                    r.append(t)
                    break
            else:
                rows.append([t])
        for r in rows:
            if len(r) < 5:
                continue
            r = sorted(r, key=lambda t: t["x"])
            xs = np.array([t["x"] for t in r])
            gaps = np.diff(xs)
            if np.ptp(xs) < 80:
                continue
            for unit in (10, 25.4):
                v = np.array([t["v"] for t in r]) * unit
                s, b = np.polyfit(xs, v, 1)
                rms = float(np.sqrt(np.mean((s * xs + b - v) ** 2)))
                even = float(np.std(gaps) / max(np.mean(gaps), 1e-9))
                if rms < unit * 0.4 and even < 0.4:
                    cand = dict(mm_px=abs(float(s)), rms=rms, even=even, n=len(r), unit=unit,
                                conf=float(np.mean([t["c"] for t in r])), vals=[t["v"] for t in r],
                                psm=psm)
                    if best is None or rms / unit < best["rms"] / best["unit"]:
                        best = cand
    return best


# ---------------------------------------------------------------- pipeline
def analyse(path: str, verbose: bool = True):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    name = os.path.basename(path)
    im = Image.open(path).convert("RGB")
    a = np.asarray(im).astype(int)
    picked = pick_blade(a)
    if not picked:
        print(f"{name}: no blade candidate")
        return None
    score, mask_name, box, aspect = picked
    mask = candidate_masks(a)[mask_name]
    crop, angle = rectify(im, box, mask)
    if crop is None or crop.width < 120 or crop.width / max(1, crop.height) < 2.0:
        print(f"{name}: {mask_name} box={box} -> weak crop {None if crop is None else crop.size}")
        return None
    crop.save(os.path.join(DEBUG_DIR, f"{name}.png"))

    gray = np.asarray(crop.convert("L")).astype(int)
    med = np.median(gray)
    ink = gray < (med - 45)
    h, w = ink.shape
    rows = ink.mean(axis=1)
    print(f"\n{name}: mask={mask_name} box={box} angle={angle:.1f} crop={w}x{h} ink={ink.mean():.3f}")

    pitch_cands = []
    for label, t0, t1 in (("mid", 0.15, 0.85), ("upper", 0.0, 0.30), ("lower", 0.70, 1.0)):
        prof = ink[int(h * t0):max(int(h * t0) + 1, int(h * t1))].sum(axis=0).astype(float)
        peaks = acf_peaks(prof)
        if verbose and peaks:
            print(f"  acf[{label:5s}] " + ", ".join(f"{lag}px:{v:.2f}" for lag, v in peaks[:3]))
        for lag, v in peaks[:2]:
            pitch_cands.append({"band": label, "lag": lag, "strength": v})

    mid = ink[int(h * 0.15):max(int(h * 0.15) + 1, int(h * 0.85))].sum(axis=0).astype(float)
    blobs = blob_runs(mid, 5, max(8, w // 12))
    blob_pitch = None
    if len(blobs) >= 6:
        gaps = np.diff(blobs)
        blob_pitch = float(np.median(gaps))
        if verbose:
            print(f"  blobs {len(blobs)} pitch={blob_pitch:.1f}px spread={np.std(gaps)/blob_pitch:.2f}")

    ocr = ocr_scale(crop)
    if verbose:
        if ocr:
            print(f"  ocr psm={ocr['psm']} unit={ocr['unit']:.0f} n={ocr['n']} conf={ocr['conf']:.0f}"
                  f" -> {ocr['mm_px']:.4f} mm/px rms={ocr['rms']:.1f}mm even={ocr['even']:.2f}")
            print(f"      values {ocr['vals']}")
        else:
            print("  ocr: no usable row")

    # pitch hypotheses: the strong blob pitch is the 10 mm number ladder
    pitch_scale = None
    if blob_pitch:
        pitch_scale = 10.0 / blob_pitch
        if verbose:
            print(f"  pitch(10mm) -> {pitch_scale:.4f} mm/px")

    verdict = "no scale"
    if ocr and pitch_scale:
        delta = abs(ocr["mm_px"] - pitch_scale) / max(ocr["mm_px"], 1e-9)
        verdict = f"agree ({delta*100:.0f}% apart)" if delta < 0.25 else f"DISAGREE ({delta*100:.0f}% apart)"
    elif ocr:
        verdict = "ocr only"
    elif pitch_scale:
        verdict = "pitch only"
    print(f"  => {verdict}")
    return dict(path=path, box=box, angle=angle, crop=crop.size, ocr=ocr, pitch_scale=pitch_scale,
                pitch_cands=pitch_cands, verdict=verdict)


def main(argv):
    paths = argv[1:] or sorted(glob.glob("D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images/*.jpeg"))
    out = [analyse(p) for p in paths]
    print("\n==== summary")
    for r in out:
        if not r:
            continue
        print(f"{os.path.basename(r['path'])[:46]:48s} {r['verdict']:24s}"
              f" ocr={r['ocr']['mm_px'] if r['ocr'] else float('nan'):.4f}"
              f" pitch={r['pitch_scale'] if r['pitch_scale'] else float('nan'):.4f} mm/px"
              f" crop={r['crop']}")
    return out


if __name__ == "__main__":
    import pytesseract  # imported late so --help style runs work without tesseract
    main(sys.argv)
