import glob, os, sys
sys.path.insert(0, os.getcwd()); sys.path.insert(0, os.path.join(os.getcwd(), "tmp"))
from marker import find, overlay
src = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"
out = r"D:/Downloads_D/Q-GIS/Demo_ready/marker"
os.makedirs(out, exist_ok=True)
for f in sorted(glob.glob(src + "/2026*.jpg.jpeg")):
    data = open(f, "rb").read()
    im, info, m, scale = find(data)
    name = os.path.basename(f).replace(" ", "_")[:26]
    if not info:
        print(name, "NO CLIP"); continue
    overlay(im, info, m, scale, os.path.join(out, name + "_marker.jpg"))
    print("%s %s area=%.0f centre=(%.0f,%.0f) tape=%+.1fdeg width=%.0fpx len=%.0fpx edge0=(%.0f,%.0f) edge1=(%.0f,%.0f)" % (
        name, im.size, info['area_px'], info['centre'][0], info['centre'][1],
        info['tape_angle_deg'], info['clip_across_tape_px'], info['clip_along_tape_px'],
        info['edges'][0][0], info['edges'][0][1], info['edges'][1][0], info['edges'][1][1]), flush=True)
