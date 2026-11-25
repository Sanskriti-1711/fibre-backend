"""Prototype: find the cyan tape clip and describe the tape near it."""
import io, math
import numpy as np
from PIL import Image, ImageDraw


def cyan_mask(rgb: np.ndarray) -> np.ndarray:
    r = rgb[..., 0].astype(np.int16)
    g = rgb[..., 1].astype(np.int16)
    b = rgb[..., 2].astype(np.int16)
    return (g - r > 28) & (b - r > 18) & (g > 100) & (b > 95)


def blobs(mask: np.ndarray, min_area: int = 40):
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
    ys, xs = np.nonzero(mask)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        pix = []
        while stack:
            y, x = stack.pop()
            pix.append((x, y))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and not seen[ny, nx] and mask[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if len(pix) >= min_area:
            out.append(pix)
    out.sort(key=len, reverse=True)
    return out


def find(data: bytes, work: int = 1400):
    im = Image.open(io.BytesIO(data)).convert('RGB')
    small = im.copy()
    small.thumbnail((work, work))
    scale = im.width / float(small.width)
    m = cyan_mask(np.asarray(small))
    blobs_found = blobs(m, min_area=max(30, int(m.sum() * 0.02)))
    if not blobs_found:
        return im, None, m, scale
    pts = np.array(blobs_found[0], dtype=float) * scale          # (x, y) full res
    centre = pts.mean(axis=0)
    centred = pts - centre
    vals, vecs = np.linalg.eigh(np.cov(centred.T))
    long_v = vecs[:, int(np.argmax(vals))]     # across the tape
    short_v = vecs[:, int(np.argmin(vals))]    # along the tape
    if short_v[0] < 0:
        short_v = -short_v
    along = centred @ short_v
    across = centred @ long_v
    a0, a1 = np.percentile(along, 1), np.percentile(along, 99)
    info = {
        'area_px': float(len(pts)),
        'centre': (float(centre[0]), float(centre[1])),
        'tape_dir': (float(short_v[0]), float(short_v[1])),
        'across_dir': (float(long_v[0]), float(long_v[1])),
        'tape_angle_deg': math.degrees(math.atan2(short_v[1], short_v[0])),
        'clip_across_tape_px': float(a1 - a0),
        'clip_along_tape_px': float(np.percentile(across, 99) - np.percentile(across, 1)),
        'edges': [
            (float(centre[0] + a0 * short_v[0]), float(centre[1] + a0 * short_v[1])),
            (float(centre[0] + a1 * short_v[0]), float(centre[1] + a1 * short_v[1])),
        ],
        'blobs_px': [float(len(p)) * scale * scale for p in blobs_found[:4]],
    }
    return im, info, m, scale


def overlay(im, info, m, scale, path, zoom: int = 1200):
    """Clip mask in magenta, tape axis in red, the two clip edges in green."""
    k = zoom / float(im.width)
    vis = im.copy()
    vis.thumbnail((zoom, zoom))
    m2 = np.asarray(Image.fromarray((m * 255).astype('uint8')).resize(vis.size))
    base = np.asarray(vis).copy()
    base[m2 > 100] = (255, 0, 255)
    vis = Image.fromarray(base)
    d = ImageDraw.Draw(vis)
    cx, cy = info['centre']
    ux, uy = info['tape_dir']
    px, py = info['across_dir']
    L = 300
    d.line([((cx - L * ux) * k, (cy - L * uy) * k), ((cx + L * ux) * k, (cy + L * uy) * k)],
           fill=(255, 0, 0), width=4)
    for ex, ey in info['edges']:
        d.line([((ex - 45 * px) * k, (ey - 45 * py) * k), ((ex + 45 * px) * k, (ey + 45 * py) * k)],
               fill=(0, 255, 0), width=3)
    vis.save(path, quality=88)
    return path
