"""Cut clean, upscaled crops around the clip so a human can read the tape."""
import glob, os, sys
import numpy as np
from PIL import Image
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import find

SRC = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"
OUT = r"D:/Downloads_D/Q-GIS/Demo_ready/marker/size"
VIEW = os.path.join(os.getcwd(), "tmp", "view")
PICK = {"20261124_140332", "20261124_140444", "20261124_141314", "20261124_140554"}
os.makedirs(OUT, exist_ok=True); os.makedirs(VIEW, exist_ok=True)


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


for f in sorted(glob.glob(SRC + "/2026*.jpg.jpeg")):
    name = os.path.basename(f).split('.')[0]
    im, info, _m, _k = find(open(f, "rb").read())
    if not info:
        print(name, "NO CLIP", flush=True); continue
    rgb = np.asarray(im)
    c, u_dir, v_dir = info['centre'], info['tape_dir'], info['across_dir']
    w, h = 2000, 1300
    uu, vv = np.meshgrid(np.arange(w) - w / 2.0, np.arange(h) - h / 2.0)
    patch = Image.fromarray(sample(rgb, c[0], c[1], uu, vv, u_dir, v_dir))
    patch.save(os.path.join(OUT, name + "_crop.jpg"), quality=90)
    if name in PICK:
        patch.save(os.path.join(VIEW, name + "_crop.jpg"), quality=90)
    print(name, "clip_along_px=%.0f clip_across_px=%.0f" % (info['clip_across_tape_px'], info['clip_along_tape_px']), flush=True)
