import glob, os, sys, time
sys.path.insert(0, os.getcwd())
from supervision import tape_framing, tape_measure
src = r"D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images"
for f in sorted(glob.glob(src + "/2026*.jpg.jpeg")):
    raw = open(f, "rb").read()
    t = time.time()
    name = os.path.basename(f)
    try:
        fr = tape_framing.check_bytes(raw)
        print(name, "framing=", (fr or {}).get("ok"), (fr or {}).get("reason"), "%.1fs" % (time.time()-t), flush=True)
        diag = {}
        rd = tape_measure.read_tape_bytes(raw, diag)
        if rd:
            print("   read:", rd.get("unit"), [r["value"] for r in rd.get("readings") or []],
                  "agree=", rd.get("agreement"), "%.1fs" % (time.time()-t), flush=True)
        else:
            print("   read: None", (diag.get("digits") or {}).get("reason"), "%.1fs" % (time.time()-t), flush=True)
    except Exception as e:
        print(name, "ERR", type(e).__name__, e, flush=True)
