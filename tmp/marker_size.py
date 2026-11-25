"""How big is the clip on the blade, in tape units?

Measures, per photo: the clip's extent along the tape (blade coverage) and
across the tape (jaw span), the blade thickness, and the tick pitch, then
converts px -> mm with the tick pitch.
"""
import glob, io, json, math, os, sys
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import cyan_mask, blobs
from supervision import tape_ticks

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
    return (lo + hi) / 2.0 - (len(vv) / 2.0), float(length)


def period(profile):
    """Strongest repeat in a 1-D intensity profile (tick spacing, in px)."""
    p = profile.astype(np.float64)
    p = p - np.convolve(p, np.ones(31) / 31.0, mode='same')
    p = p[20:-20]
    if p.size < 60:
        return None, None
    n = p.size
    ac = np.correlate(p, p, mode='full')[n - 1:]
    ac = ac / (ac[0] + 1e-9)
    best = (0, 0.0)
    for lag in range(5, min(600, n // 2)):
        if ac[lag] > ac[lag - 1] and ac[lag] >= ac[lag + 1] and ac[lag] > best[1]:
            best = (lag, float(ac[lag]))
    return (best[0] or None), (best[1] or None)


def cloud(data, work=1400):
    im = Image.open(io.BytesIO(data)).convert('RGB')
    small = im.copy(); small.thumbnail((work, work))
    scale = im.width / float(small.width)
    m = cyan_mask(np.asarray(small))
    found = blobs(m, min_area=max(30, int(m.sum() * 0.02)))
    if not found:
        return im, None
    pts = np.array(found[0], dtype=float) * scale
    centre = pts.mean(axis=0)
    centred = pts - centre
    vals, vecs = np.linalg.eigh(np.cov(centred.T))
    long_v = vecs[:, int(np.argmax(vals))]
    short_v = vecs[:, int(np.argmin(vals))]
    if short_v[0] < 0:
        short_v = -short_v
    along = centred @ short_v
    across = centred @ long_v
    info = {
        'centre': (float(centre[0]), float(centre[1])),
        'u_dir': (float(short_v[0]), float(short_v[1])),
        'v_dir': (float(long_v[0]), float(long_v[1])),
        'along_px': float(np.percentile(along, 99) - np.percentile(along, 1)),
        'across_px': float(np.percentile(across, 99) - np.percentile(across, 1)),
        'along_span': (float(np.percentile(along, 1)), float(np.percentile(along, 99))),
        'across_span': (float(np.percentile(across, 1)), float(np.percentile(across, 99))),
        'fill': float(len(pts)) / max(1.0, (np.percentile(along, 99) - np.percentile(along, 1)) *
                                      (np.percentile(across, 99) - np.percentile(across, 1))),
        'area_px': float(len(pts)),
    }
    return im, info


os.makedirs(OUT, exist_ok=True)
rows = []
for f in sorted(glob.glob(SRC + "/2026*.jpg.jpeg")):
    name = os.path.basename(f).split('.')[0]
    im, info = cloud(open(f, "rb").read())
    if not info:
        print(name, "NO CLIP", flush=True); continue
    rgb = np.asarray(im)
    c, u_dir, v_dir = info['centre'], info['u_dir'], info['v_dir']
    reach = int(info['across_px'] * 0.9) + 80
    a_lo, a_hi = info['along_span']
    band = None
    for off in (-(a_lo - 60), -(a_hi + 60), (a_lo - 60), (a_hi + 60)):
        v_off, thick = tape_band(rgb, c, u_dir, v_dir, off, reach)
        if thick > 30:
            band = (off, v_off, thick)
            break
    if band is None:
        print(name, "no blade beside clip", flush=True); continue
    off, v_off, thick = band
    height = int(max(60, round(thick * 1.4)))
    strip_img = Image.fromarray(window(rgb, (c[0] + v_off * v_dir[0], c[1] + v_off * v_dir[1]),
                                      u_dir, v_dir, 2400, height))
    lat = tape_ticks.read_strip(strip_img)
    p_ac, strength = period(np.asarray(strip_img.convert('L'), float).mean(axis=0))
    p_lat = float(lat.get('pitch') or 0) if lat else 0.0
    pitch = p_ac or p_lat or None
    mm = None
    if pitch:
        mm = {'across': info['across_px'] / pitch, 'along': info['along_px'] / pitch,
              'blade': thick / pitch}
    strip_img.save(os.path.join(OUT, name + "_strip.png"))
    vis = im.copy()
    vis.thumbnail((1100, 1100))
    k = vis.width / float(im.width)
    vis = vis.convert('RGB')
    d = ImageDraw.Draw(vis)
    for s in (-1, 1):
        u = info['along_span'][0] if s < 0 else info['along_span'][1]
        d.line([((c[0] + u * u_dir[0] - 40 * v_dir[0]) * k, (c[1] + u * u_dir[1] - 40 * v_dir[1]) * k),
                ((c[0] + u * u_dir[0] + 40 * v_dir[0]) * k, (c[1] + u * u_dir[1] + 40 * v_dir[1]) * k)],
               fill=(0, 255, 0), width=3)
    for s in (-1, 1):
        v = info['across_span'][0] if s < 0 else info['across_span'][1]
        d.line([((c[0] + v * v_dir[0] - 40 * u_dir[0]) * k, (c[1] + v * v_dir[1] - 40 * u_dir[1]) * k),
                ((c[0] + v * v_dir[0] + 40 * u_dir[0]) * k, (c[1] + v * v_dir[1] + 40 * u_dir[1]) * k)],
               fill=(255, 255, 0), width=3)
    vis.save(os.path.join(OUT, name + "_size.jpg"), quality=88)
    rows.append({'name': name, 'along_px': round(info['along_px']), 'across_px': round(info['across_px']),
                 'blade_px': round(thick), 'pitch_px': pitch, 'pitch_ac': p_ac, 'pitch_lat': round(p_lat, 2),
                 'ac': None if strength is None else round(strength, 2), 'mm': None if not mm else
                 {k2: round(v2, 2) for k2, v2 in mm.items()}})
    print("%s along=%.0fpx across=%.0fpx blade=%.0fpx pitch=%s/%s mm=%s" % (
        name, info['along_px'], info['across_px'], thick, p_ac, round(p_lat, 1),
        None if not mm else {k2: round(v2, 2) for k2, v2 in mm.items()}), flush=True)

json.dump(rows, open(os.path.join(OUT, "sizes.json"), "w"), indent=1)
