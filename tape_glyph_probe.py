"""Probe: find the printed ladder on a tape photo, without knowing where the tape is.

Pipeline
  ink mask -> remove long straight lines (blade edges, tape borders)
           -> glyph components -> group into evenly spaced rows/columns
           -> pitch gives mm/px (numbers are printed every 10 mm)
           -> OCR the glyphs for a second, independent scale and a sanity check

Run: .venv/Scripts/python.exe tape_glyph_probe.py [photo ...]
"""
from __future__ import annotations

import glob
import os
import sys
from collections import deque

import numpy as np
import pytesseract
from PIL import Image, ImageOps

DS = 2          # component search downsample
LINE = 15       # structures longer than this (at DS) are lines, not glyphs


def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Dark relative to the local background, so both white and yellow tapes work."""
    blur = np.asarray(Image.fromarray(gray).filter(__import__("PIL.ImageFilter", fromlist=["x"]).BoxBlur(9)))
    return gray < (blur - 28)


def shift_x(m: np.ndarray, dx: int) -> np.ndarray:
    out = np.zeros_like(m)
    if dx > 0:
        out[:, dx:] = m[:, :-dx]
    elif dx < 0:
        out[:, :dx] = m[:, -dx:]
    else:
        out = m.copy()
    # pad edges as False so lines touching the border still erode
    if dx > 0:
        out[:, :dx] = False
    elif dx < 0:
        out[:, dx:] = False
    return out


def shift_y(m: np.ndarray, dy: int) -> np.ndarray:
    out = np.zeros_like(m)
    if dy > 0:
        out[dy:, :] = m[:-dy, :]
    elif dy < 0:
        out[:dy, :] = m[-dy:, :]
    else:
        out = m.copy()
    if dy > 0:
        out[:dy, :] = False
    elif dy < 0:
        out[dy:, :] = False
    return out


def opening_h(m: np.ndarray, n: int) -> np.ndarray:
    er = m.copy()
    for i in range(1, n // 2 + 1):
        er &= shift_x(m, i)
        er &= shift_x(m, -i)
    di = er.copy()
    for i in range(1, n // 2 + 1):
        di |= shift_x(er, i)
        di |= shift_x(er, -i)
    return di


def opening_v(m: np.ndarray, n: int) -> np.ndarray:
    er = m.copy()
    for i in range(1, n // 2 + 1):
        er &= shift_y(m, i)
        er &= shift_y(m, -i)
    di = er.copy()
    for i in range(1, n // 2 + 1):
        di |= shift_y(er, i)
        di |= shift_y(er, -i)
    return di


def components(mask: np.ndarray, min_area: int = 8):
    h, w = mask.shape
    seen = np.zeros_like(mask)
    out = []
    for y in range(h):
        row = mask[y]
        if not row.any():
            continue
        for x in np.flatnonzero(row):
            if seen[y, x]:
                continue
            q = deque([(y, x)])
            seen[y, x] = True
            n = 0
            y0 = y1 = y
            x0 = x1 = x
            while q:
                cy, cx = q.popleft()
                n += 1
                if cy < y0: y0 = cy
                if cy > y1: y1 = cy
                if cx < x0: x0 = cx
                if cx > x1: x1 = cx
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        q.append((ny, nx))
            if n >= min_area:
                out.append((n, x0, y0, x1, y1))
    return out


def glyph_candidates(ink: np.ndarray):
    """Boxes that look like printed characters, in DS coordinates."""
    clean = ink & ~opening_h(ink, LINE) & ~opening_v(ink, LINE * 2)
    boxes = []
    for n, x0, y0, x1, y1 in components(clean, 8):
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if bh < 4 or bh > 40 or bw < 2 or bw > bh * 1.5:
            continue
        fill = n / (bw * bh)
        if fill > 0.85:      # solid bar: a tick, not a character
            continue
        boxes.append(dict(n=n, x0=x0, y0=y0, x1=x1, y1=y1, bw=bw, bh=bh, fill=fill,
                          cx=(x0 + x1) / 2, cy=(y0 + y1) / 2))
    return boxes


def group_ladder(boxes, axis: str):
    """Largest set of similar glyphs on one line with near-even spacing."""
    if len(boxes) < 6:
        return None
    coord = "cy" if axis == "row" else "cx"
    span = "bh" if axis == "row" else "bw"
    med_span = float(np.median([b[span] for b in boxes]))
    bins: dict[int, list] = {}
    for b in boxes:
        key = int(round(b[coord] / max(1.0, med_span * 0.7)))
        bins.setdefault(key, []).append(b)
    best = None
    for key in list(bins):
        group = bins.get(key, []) + bins.get(key + 1, []) + bins.get(key - 1, [])
        if len(group) < 6:
            continue
        group = sorted(group, key=lambda b: b["cx"] if axis == "row" else b["cy"])
        order = [b["cx"] if axis == "row" else b["cy"] for b in group]
        gaps = np.diff(order)
        if len(gaps) < 5:
            continue
        med = float(np.median(gaps))
        if med <= 0:
            continue
        keep = [group[0]]
        for b, g in zip(group[1:], gaps):
            if abs(g - med) <= med * 0.45:
                keep.append(b)
        if len(keep) < 5:
            continue
        gaps2 = np.diff([b["cx"] if axis == "row" else b["cy"] for b in keep])
        cv = float(np.std(gaps2) / max(np.mean(gaps2), 1e-9))
        score = len(keep) * (1 - min(cv, 1.0))
        if best is None or score > best["score"]:
            best = dict(axis=axis, group=keep, pitch=float(np.median(gaps2)), cv=cv, score=score,
                        coord=float(np.median([b[coord] for b in keep])))
    return best


def ocr_glyph(gray: np.ndarray, box, scale: int):
    x0, y0, x1, y1 = [int(round(v * scale)) for v in (box["x0"], box["y0"], box["x1"], box["y1"])]
    pad = max(2, int((y1 - y0) * 0.3))
    h, w = gray.shape
    patch = Image.fromarray(gray[max(0, y0 - pad):min(h, y1 + pad), max(0, x0 - pad):min(w, x1 + pad)])
    target = 64
    if patch.height < target and patch.height > 0:
        patch = patch.resize((max(1, int(patch.width * target / patch.height)), target), Image.LANCZOS)
    patch = ImageOps.autocontrast(patch)
    best = (None, 0.0)
    for psm in (10, 7):
        d = pytesseract.image_to_data(patch, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789",
                                      output_type=pytesseract.Output.DICT)
        for i, t in enumerate(d["text"]):
            t = (t or "").strip()
            if t.isdigit() and len(t) <= 2:
                c = float(d["conf"][i])
                if c > best[1]:
                    best = (int(t), c)
    return best


def analyse(path, verbose=True):
    name = os.path.basename(path)
    im = Image.open(path).convert("RGB")
    gray = np.asarray(im.convert("L"))
    h, w = gray.shape
    small = gray[::DS, ::DS]
    ink = ink_mask(small)
    boxes = glyph_candidates(ink)
    if verbose:
        print(f"\n{name} {w}x{h} glyph-candidates={len(boxes)}")
    best = None
    for axis in ("row", "col"):
        lad = group_ladder(boxes, axis)
        if lad and (best is None or lad["score"] > best["score"]):
            best = lad
    if not best:
        print("  no ladder found")
        return None
    group = best["group"]
    xs = [b["cx"] for b in group] if best["axis"] == "row" else [b["cy"] for b in group]
    reads = []
    for b, x in zip(group, xs):
        value, conf = ocr_glyph(gray, b, DS)
        reads.append((value, conf, x * DS))
    got = [r for r in reads if r[0] is not None and r[1] >= 45]
    pitch_px = best["pitch"] * DS
    if verbose:
        print(f"  ladder axis={best['axis']} n={len(group)} pitch={pitch_px:.1f}px cv={best['cv']:.2f}"
              f" -> 10mm/{pitch_px:.1f}px = {10/pitch_px:.4f} mm/px")
        print(f"  ocr {len(got)}/{len(reads)}: " + " ".join(f"{v}@{x:.0f}({c:.0f})" for v, c, x in got))
    ocr_scale = None
    if len(got) >= 4:
        got.sort(key=lambda r: r[2])
        gx = np.array([r[2] for r in got])
        for unit in (10, 25.4):
            v = np.array([r[0] for r in got]) * unit
            s, b0 = np.polyfit(gx, v, 1)
            rms = float(np.sqrt(np.mean((s * gx + b0 - v) ** 2)))
            even = float(np.std(np.diff(gx)) / max(np.mean(np.diff(gx)), 1e-9))
            if rms < unit * 0.4 and even < 0.4:
                if ocr_scale is None or rms / unit < ocr_scale[0]:
                    ocr_scale = (rms / unit, abs(float(s)), unit, len(got), rms)
    if ocr_scale and verbose:
        print(f"  ocr fit unit={ocr_scale[2]:.0f}mm n={ocr_scale[3]} -> {ocr_scale[1]:.4f} mm/px rms={ocr_scale[4]:.1f}mm")
    return dict(name=name, pitch_mm_px=10 / pitch_px, ocr_mm_px=ocr_scale[1] if ocr_scale else None,
                n=len(group), axis=best["axis"], cv=best["cv"])


def main(argv):
    paths = argv[1:] or sorted(glob.glob("D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images/*.jpeg"))
    rows = [analyse(p) for p in paths]
    print("\n==== summary")
    for r in rows:
        if not r:
            continue
        ocr = f"{r['ocr_mm_px']:.4f}" if r["ocr_mm_px"] else "  -   "
        print(f"{r['name'][:46]:48s} n={r['n']:3d} axis={r['axis']:3s} cv={r['cv']:.2f}"
              f" pitch={r['pitch_mm_px']:.4f} ocr={ocr}")


if __name__ == "__main__":
    main(sys.argv)
