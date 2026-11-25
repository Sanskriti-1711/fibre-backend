"""Measure the clip as the interruption of the blade: gap width in mm ticks."""
import glob, os, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import find

SRC = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"


def sample(rgb, cx, cy, uu, vv, u_dir, v_dir):
    xs = cx + uu * u_dir[0] + vv * v_dir[0]
    ys = cy + uu * u_dir[1] + vv * v_dir[1]
    h, w = rgb.shape[:2]
    xs = np.clip(xs, 0, w - 1.001); ys = np.clip(ys, 0, h - 1.001)
    x0 = np.floor(xs).astype(np.int32); y0 = np.floor(ys).astype(np.int32)
    fx = (xs - x0)[..., None]; fy = (ys - y0)[..., None]
    a = rgb[y0, x0].astype(np.float32); b = rgb[y0, x0 + 1].astype(np.float32)
    c = rgb[y0 + 1, x0].astype(np.float32); d = rgb[y0 + 1, x0 + 1].astype(np.float32)
    return ((a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy).astype(np.uint8)


def window(rgb, centre, u_dir, v_dir, width, height, u_off=0.0):
    us = np.arange(width) - width / 2.0
    vs = np.arange(height) - height / 2.0
    uu, vv = np.meshgrid(us + u_off, vs)
    return sample(rgb, centre[0], centre[1], uu, vv, u_dir, v_dir)


def tape_band(rgb, centre, u_dir, v_dir, u_off, reach):
    vv = np.arange(-reach, reach)
    col = sample(rgb, centre[0], centre[1], np.full_like(vv, u_off, dtype=float), vv, u_dir, v_dir)
    bright = col.mean(axis=1)
    mask = bright >= max(np.percentile(bright, 60), 90)
    best, run = (0, 0, 0), 0
    for i, flag in enumerate(mask):
        if flag:
            run += 1
            if run > best[0]:
                best = (run, i - run + 1, i + 1)
        else:
            run = 0
    length, lo, hi = best
    return (lo + hi) / 2.0 - (len(vv) / 2.0), float(length)


def minima(prof):
    p = np.convolve(prof.astype(float), np.ones(3) / 3.0, mode='same')
    lo, hi = np.percentile(p, 10), np.percentile(p, 90)
    dark = p < lo + 0.35 * (hi - lo)
    out, i = [], 1
    while i < len(dark) - 1:
        if dark[i] and not dark[i - 1]:
            j = i
            while j < len(dark) - 1 and dark[j]:
                j += 1
            out.append((i + j - 1) / 2.0)
            i = j + 1
        else:
            i += 1
    return np.array(out)


for f in sorted(glob.glob(SRC + "/2026*.jpg.jpeg")):
    name = os.path.basename(f).split('.')[0]
    im, info, _m, _k = find(open(f, "rb").read())
    if not info:
        print(name, "NO CLIP", flush=True); continue
    rgb = np.asarray(im)
    c, u_dir, v_dir = info['centre'], info['tape_dir'], info['across_dir']
    along = info['clip_across_tape_px']
    across = info['clip_along_tape_px']
    band = None
    for off in (-(along / 2 + 100), (along / 2 + 100), -(along / 2 + 300), (along / 2 + 300)):
        v_off, thick = tape_band(rgb, c, u_dir, v_dir, off, int(across * 0.9) + 80)
        if thick > 40:
            band = (off, v_off, thick)
            break
    if not band:
        print(name, "no blade", flush=True); continue
    off, v_off, thick = band
    centre2 = (c[0] + v_off * v_dir[0], c[1] + v_off * v_dir[1])
    w = 2600
    hh = int(thick)
    patch = window(rgb, centre2, u_dir, v_dir, w, hh)
    g = np.asarray(Image.fromarray(patch).convert('L'), float)
    rows = max(3, int(hh * 0.16))
    # outer rows: clean ticks; middle rows: the clip body
    edge = np.concatenate([g[:rows].mean(axis=0), g[-rows:].mean(axis=0)])
    mid = np.median(g[int(hh * 0.25):int(hh * 0.75)], axis=0)
    bright = np.percentile(mid, 75)
    gap_mask = mid < 0.62 * bright
    # longest gap run within +-450 px of the window centre
    lo_i, hi_i = w // 2 - 450, w // 2 + 450
    best, run = (0, 0, 0), 0
    for i in range(lo_i, hi_i):
        if gap_mask[i]:
            run += 1
            if run > best[0]:
                best = (run, i - run + 1, i + 1)
        else:
            run = 0
    glen, glo, ghi = best
    ticks = minima(edge)
    near = ticks[(ticks > glo - 700) & (ticks < ghi + 700)]
    diffs = np.diff(near)
    diffs = diffs[(diffs > 4) & (diffs < 120)]
    pitch = float(np.median(diffs)) if diffs.size >= 6 else float('nan')
    print("%s gap=%.0fpx pitch=%.1fpx/web gap_ticks=%.1f gap_mm=%.1f blade=%.0fpx" % (
        name, glen, pitch, glen / pitch, glen / pitch, thick), flush=True)
