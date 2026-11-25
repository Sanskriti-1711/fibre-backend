"""Clip-derived tape band -> strip -> printed labels -> value at the clip edges."""
import glob, json, os, sys
import numpy as np
from PIL import Image, ImageDraw
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import find
from supervision import tape_frame, tape_ticks, tape_digits

SRC = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"
OUT = r"D:/Downloads_D/Q-GIS/Demo_ready/marker"


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
    """Centre offset and thickness of the white blade, seen beside the clip."""
    vv = np.arange(-reach, reach)
    col = sample(rgb, centre[0], centre[1], np.full_like(vv, u_off, dtype=float), vv, u_dir, v_dir)
    bright = col.mean(axis=1)
    text = bright.astype(np.int32)
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


rows = []
for f in sorted(glob.glob(SRC + "/2026*.jpg.jpeg")):
    name = os.path.basename(f)
    im, info, _m, _k = find(open(f, "rb").read())
    if not info:
        print(name, "NO CLIP"); continue
    rgb = np.asarray(im)
    c, u_dir, v_dir = info['centre'], info['tape_dir'], info['across_dir']
    clip_u = info['clip_across_tape_px']
    reach = int(info['clip_along_tape_px'] * 0.9) + 60
    v_off, thick = tape_band(rgb, c, u_dir, v_dir, -(clip_u / 2 + 40), reach)
    if thick < 30:
        v_off, thick = tape_band(rgb, c, u_dir, v_dir, clip_u / 2 + 40, reach)
    height = int(max(60, round(thick * 1.6)))
    strip_img = Image.fromarray(window(rgb, (c[0] + v_off * v_dir[0], c[1] + v_off * v_dir[1]),
                                      u_dir, v_dir, 2600, height))
    strip_img.save(os.path.join(OUT, name.replace('.jpg.jpeg', '') + "_band.png"))
    latt = tape_ticks.read_strip(strip_img)
    dig = tape_digits.read_labels(strip_img, latt) if latt else None
    log = {'name': name, 'clip_px': round(clip_u), 'thickness': round(thick),
           'lattice': None if not latt else {'usable': latt.get('usable'), 'pitch': round(latt.get('pitch', 0), 2),
                                            'count': latt.get('count')},
           'digits': None if not dig else {'usable': dig.get('usable'), 'reason': dig.get('reason'),
                                           'unit': dig.get('unit'), 'mm_per_px': dig.get('mm_per_px'),
                                           'labels': [a['text'] for a in (dig.get('anchors') or [])]}}
    readings = None
    if dig and dig.get('usable'):
        xs = np.array([a['x_px'] for a in dig['anchors']], float)
        vals = np.array([a['value'] for a in dig['anchors']], float)
        slope, intercept = np.polyfit(xs, vals, 1)
        unit_mm = 10.0 if dig['unit'] == 'cm' else 1.0
        px_per_u = 1.0  # window is 1:1 with frame pixels
        edges = {'edge_far': -clip_u / 2, 'edge_near': clip_u / 2}
        readings = {k: round((intercept + slope * (1300.0 + u)) * unit_mm, 1) for k, u in edges.items()}
        log['readings_mm'] = readings
        vis = strip_img.convert('RGB')
        d = ImageDraw.Draw(vis)
        for a in dig['anchors']:
            d.line([(a['x_px'], 0), (a['x_px'], vis.height)], fill=(0, 160, 0), width=2)
            d.text((a['x_px'] + 2, 2), a['text'], fill=(200, 0, 0))
        for k, u in edges.items():
            sx = 1300.0 + u
            d.line([(sx, 0), (sx, vis.height)], fill=(255, 0, 0), width=3)
            d.text((sx + 2, vis.height - 12), k, fill=(255, 0, 0))
        vis.save(os.path.join(OUT, name.replace('.jpg.jpeg', '') + "_strip.jpg"), quality=90)
    rows.append(log)
    print(name, "clip=%.0f thick=%.0f" % (clip_u, thick), "lattice=", log['lattice'],
          "digits=", log['digits'], "edges=", readings, flush=True)

json.dump(rows, open(os.path.join(OUT, "marker_read2.json"), "w"), indent=1)
