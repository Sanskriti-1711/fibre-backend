"""Locate span TR-000010 in the demo project and download its photos."""

import os
import json
import urllib.error
import urllib.request

BASE = os.environ.get('FIBER_API_BASE', 'http://127.0.0.1:8000')
DEMO = "8cce6b1c-2ccb-4399-a864-d53c1e447628"


def call(path, data=None, token=None, timeout=90):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:200]


status, tok = call("/api/users/login/", {"email": os.environ.get("FIBER_EMAIL", ""), "password": os.environ.get("FIBER_PASSWORD", "")})
token = tok["access"]
status, md = call("/api/projects/%s/map-data/" % DEMO, token=token)
for feature in md.get("features", []):
    props = feature.get("properties") or {}
    print("feat", props.get("id"), "design", props.get("design_id"), "label", props.get("label"))
    status, photos = call("/api/features/%s/photos/" % props.get("id"), token=token)
    if status == 200 and photos.get("count"):
        print("  photos", photos.get("count"))
        for index, photo in enumerate(photos.get("results", [])):
            try:
                with urllib.request.urlopen(photo["url"], timeout=90) as r:
                    data = r.read()
                name = "builds/real_tape_%s_%d.jpg" % (str(props.get("id"))[:8], index)
                open(name, "wb").write(data)
                print("  saved", name, len(data))
            except Exception as error:
                print("  download failed", error)
