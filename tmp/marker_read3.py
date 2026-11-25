"""Which ROI lets the existing framer+ladder read the blade next to the clip?"""
import glob, os, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import find
from supervision import tape_frame, tape_digits
from marker_read2 import sample, tape_band

SRC = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"


def aligned(rgb, c, u_dir, v_dir, width, height, v_off=0.0):
    us = np.arange(width) - width / 2.0
    vs = np.arange(height) - height / 2.0
    uu, vv = np.meshgrid(us, vs + v_off)
    return sample(rgb, c[0], c[1], uu, vv, u_dir, v_dir)


def square(rgb, c, side):
    h, w = rgb.shape[:2]
    x0 = int(max(0, min(w - side, c[0] - side / 2)))
    y0 = int(max(0, min(h - side, c[1] - side / 2)))
    return rgb[y0:y0 + side, x0:x0 + side]


def attempt(gray, label):
    framed = tape_frame.frame_gray(np.ascontiguousarray(gray.astype(np.uint8)))
    if framed is None:
        return label, 'no frame', None, None
    dig = None
    if framed.get('lattice') and framed.get('strip') is not None:
        squash = float(framed.get('thickness_px') or 0) / max(1.0, float(framed.get('height_px') or 1))
        dig = tape_digits.read_labels(framed['strip'], framed['lattice'],
                                      stretch=min(4.0, max(0.5, squash)))
    return (label,
            'ticks=%s pitch=%s thick=%s' % (framed.get('tick_usable'), framed.get('tick_pitch'), framed.get('thickness_px')),
            None if not dig else (dig.get('usable'), dig.get('reason'), dig.get('unit'),
                                  [a['text'] for a in (dig.get('anchors') or [])]),
            framed)


for f in sorted(glob.glob(SRC + "/2026*.jpg.jpeg")):
    name = os.path.basename(f).split('.')[0]
    im, info, _m, _k = find(open(f, "rb").read())
    if not info:
        print(name, "NO CLIP", flush=True); continue
    rgb = np.asarray(im)
    c, u_dir, v_dir = info['centre'], info['tape_dir'], info['across_dir']
    clip_u = info['clip_across_tape_px']
    reach = int(info['clip_along_tape_px'] * 0.9) + 60
    v_off, thick = tape_band(rgb, c, u_dir, v_dir, -(clip_u / 2 + 40), reach)
    if thick < 30:
        v_off, thick = tape_band(rgb, c, u_dir, v_dir, clip_u / 2 + 40, reach)
    h = int(max(70, round(thick * 2.5)))
    print("==", name, "clip=%.0f thick=%.0f" % (clip_u, thick), flush=True)
    for label, patch in [
        ("square1200", square(rgb, c, 1200)),
        ("square1800", square(rgb, c, 1800)),
        ("aligned2600", aligned(rgb, c, u_dir, v_dir, 2600, h, v_off)),
        ("aligned1600", aligned(rgb, c, u_dir, v_dir, 1600, h, v_off)),
    ]:
        gray = np.asarray(Image.fromarray(np.ascontiguousarray(patch)).convert('L'))
        lab, ticks, dig, framed = attempt(gray, label)
        print("   ", lab, ticks, "digits=", dig, flush=True)
