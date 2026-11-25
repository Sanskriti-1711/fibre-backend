"""Measure the tape's mm tick spacing (px/mm) near the clip, then size the clip."""
import glob, os, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import find

SRC = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"
OUT = r"D:/Downloads_D/Q-GIS/Demo_ready/marker/size"


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
    thr = np.percentile(bright, 60)
    mask = bright >= max(thr, 90)
    best = (0, 0, 0)
    run = 0
    for i, flag in enumerate(mask):
        if flag:
            run += 1
            if run > best[0]:
                best = (run, i - run + 1, i + 1)
        else:
            run = 0
    length, lo, hi = best
    return (lo + hi) / 2.0 - (len(vv) / 2.0), float(length), (lo, hi)


def tick_pitch(profile):
    """Median spacing between dark tick lines in a 1-D blade-edge profile."""
    p = profile.astype(np.float64)
    k = np.ones(5) / 5.0
    p = np.convolve(p, k, mode='same')
    lo = np.percentile(p, 15); hi = np.percentile(p, 85)
    dark = p < lo + 0.30 * (hi - lo)
    idx = []
    i = 1
    while i < len(dark) - 1:
        if dark[i] and not dark[i - 1]:
            j = i
            while j < len(dark) - 1 and dark[j]:
                j += 1
            idx.append((i + j - 1) / 2.0)
            i = j + 1
        else:
            i += 1
    if len(idx) < 8:
        return None, [], dark
    diffs = np.diff(idx)
    diffs = diffs[(diffs > 4) & (diffs < 200)]
    if diffs.size < 8:
        return None, diffs, dark
    base = np.percentile(diffs, 15)
    mm = diffs[np.abs(diffs - base) < 0.35 * base]
    return float(np.median(mm)), diffs, dark


for f in sorted(glob.glob(SRC + "/2026*.jpg.jpeg")):
    name = os.path.basename(f).split('.')[0]
    im, info, _m, _k = find(open(f, "rb").read())
    if not info:
        print(name, "NO CLIP", flush=True); continue
    rgb = np.asarray(im)
    c, u_dir, v_dir = info['centre'], info['tape_dir'], info['across_dir']
    clip_along = info['clip_across_tape_px']      # extent along the tape
    clip_across = info['clip_along_tape_px']      # extent across the tape
    reach = int(clip_across * 0.9) + 80
    chosen = None
    for off in (-(clip_along / 2 + 90), (clip_along / 2 + 90), -(clip_along / 2 + 240), (clip_along / 2 + 240)):
        v_off, thick, edges = tape_band(rgb, c, u_dir, v_dir, off, reach)
        if thick < 40:
            continue
        rows = max(4, int(thick * 0.18))
        w = 2400
        patch = window(rgb, (c[0] + v_off * v_dir[0], c[1] + v_off * v_dir[1]), u_dir, v_dir, w, int(thick))
        g = np.asarray(Image.fromarray(patch).convert('L'), float)
        prof = np.concatenate([g[:rows].mean(axis=0), g[-rows:].mean(axis=0)])
        pitch, diffs, dark = tick_pitch(prof)
        if pitch:
            chosen = (off, thick, pitch, prof, g)
            break
    if not chosen:
        print(name, "no pitch", flush=True); continue
    off, thick, pitch, prof, g = chosen
    Image.fromarray(g.astype(np.uint8)).save(os.path.join(OUT, name + "_blade.png"))
    print("%s pitch=%.1fpx/mm blade=%.0fpx(%.1fmm) clip_along=%.0fpx(%.1fmm) clip_across=%.0fpx(%.1fmm)" % (
        name, pitch, thick, thick / pitch, clip_along, clip_along / pitch, clip_across, clip_across / pitch), flush=True)
