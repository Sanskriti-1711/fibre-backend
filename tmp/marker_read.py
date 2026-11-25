"""Prototype: clip edges -> absolute reading, using the existing framer/digit reader."""
import glob, io, json, math, os, sys
import numpy as np
from PIL import Image, ImageDraw
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import find
from supervision import tape_frame, tape_digits

SRC = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"
OUT = r"D:/Downloads_D/Q-GIS/Demo_ready/marker"


def patch(rgb: np.ndarray, centre, u_dir, v_dir, width=2600, height=760, scale=1.0):
    """Resample a tape-aligned window around ``centre``; clip axis is u=0."""
    cx, cy = centre
    us = (np.arange(width) - width / 2.0) * scale
    vs = (np.arange(height) - height / 2.0) * scale
    uu, vv = np.meshgrid(us, vs)
    xs = cx + uu * u_dir[0] + vv * v_dir[0]
    ys = cy + uu * u_dir[1] + vv * v_dir[1]
    h, w = rgb.shape[:2]
    xs = np.clip(xs, 0, w - 1.001); ys = np.clip(ys, 0, h - 1.001)
    x0 = np.floor(xs).astype(np.int32); y0 = np.floor(ys).astype(np.int32)
    fx = (xs - x0)[..., None]; fy = (ys - y0)[..., None]
    a = rgb[y0, x0].astype(np.float32); b = rgb[y0, x0 + 1].astype(np.float32)
    c = rgb[y0 + 1, x0].astype(np.float32); d = rgb[y0 + 1, x0 + 1].astype(np.float32)
    return ((a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy).astype(np.uint8)


rows = []
for f in sorted(glob.glob(SRC + "/2026*.jpg.jpeg")):
    data = open(f, "rb").read()
    name = os.path.basename(f)
    im, info, mask, k = find(data)
    if not info:
        print(name, "NO CLIP"); continue
    rgb = np.asarray(im)
    u_dir = info['tape_dir']; v_dir = info['across_dir']
    p = patch(rgb, info['centre'], u_dir, v_dir)
    gray = np.asarray(Image.fromarray(p).convert('L'))
    framed = tape_frame.frame_gray(gray)
    log = {'name': name, 'clip_px': round(info['clip_across_tape_px']), 'framed': framed is not None}
    if framed is None:
        print(name, "clip=%.0fpx framing=None" % info['clip_across_tape_px']); rows.append(log); continue
    log.update(tick_usable=framed.get('tick_usable'), pitch=framed.get('tick_pitch'),
               thick=framed.get('thickness_px'), width_px=framed.get('width_px'),
               coverage=framed.get('coverage'))
    digits = None
    if framed.get('lattice') and framed.get('strip') is not None:
        squash = float(framed.get('thickness_px') or 0) / max(1.0, float(framed.get('height_px') or 1))
        digits = tape_digits.read_labels(framed['strip'], framed['lattice'],
                                         stretch=min(4.0, max(0.5, squash)))
    log['digits'] = None if digits is None else {
        'usable': digits.get('usable'), 'reason': digits.get('reason'),
        'unit': digits.get('unit'), 'mm_per_px': digits.get('mm_per_px'),
        'anchors': [(a['text'], a['x_px'], a['confidence']) for a in (digits.get('anchors') or [])],
    }
    W = 2600 / 2.0
    corners = framed.get('corners') or []
    readings = None
    if digits and digits.get('usable') and len(corners) >= 2:
        val = [a['value'] for a in digits['anchors']]
        xs = np.array([a['x_px'] for a in digits['anchors']], float)
        slope, intercept = np.polyfit(xs, np.array(val, float), 1)
        u0 = corners[0][0] - W; u1 = corners[1][0] - W
        unit_mm = 10.0 if digits['unit'] == 'cm' else 1.0
        def at(u):
            sx = (u - u0) / (u1 - u0) * framed['width_px']
            return float(intercept + slope * sx) * unit_mm
        readings = {('edge0' if i == 0 else 'edge1'): round(at(u), 1)
                    for i, u in enumerate((_u for _u in (0 - info['clip_across_tape_px'] / 2,
                                                        info['clip_across_tape_px'] / 2)))}
        log['readings_mm'] = readings
        strip = framed['strip']
        vis = strip.copy().convert('RGB')
        d = ImageDraw.Draw(vis)
        for a in digits['anchors']:
            d.line([(a['x_px'], 0), (a['x_px'], vis.height)], fill=(0, 160, 0), width=2)
            d.text((a['x_px'] + 2, 2), a['text'], fill=(0, 120, 0))
        for i, u in enumerate((0 - info['clip_across_tape_px'] / 2, info['clip_across_tape_px'] / 2)):
            sx = (u - u0) / (u1 - u0) * framed['width_px']
            d.line([(sx, 0), (sx, vis.height)], fill=(255, 0, 0), width=3)
            d.text((sx + 2, vis.height - 14), "%.0fmm" % readings['edge%d' % i], fill=(255, 0, 0))
        vis.save(os.path.join(OUT, name.replace('.jpg.jpeg', '') + "_strip.jpg"), quality=90)
    rows.append(log)
    print(name, "clip=%.0fpx" % info['clip_across_tape_px'], "ticks=%s pitch=%s thick=%s" % (
        framed.get('tick_usable'), framed.get('tick_pitch'), framed.get('thickness_px')),
        "| digits:", None if digits is None else (digits.get('usable'), digits.get('reason'),
        digits.get('unit'), [a['text'] for a in (digits.get('anchors') or [])]), "| edges_mm:", readings, flush=True)

json.dump(rows, open(os.path.join(OUT, "marker_read.json"), "w"), indent=1)
