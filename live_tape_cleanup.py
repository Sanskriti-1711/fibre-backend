"""Clean up the synthetic tape photos left on the demo feature and time the API."""

import os
import json
import time
import urllib.error
import urllib.request

BASE = os.environ.get('FIBER_API_BASE', 'http://127.0.0.1:8000')
FEATURE = "e2e09ea0-14a6-5fa4-8fec-eecc6cc5ec85"


def call(path, data=None, token=None, method=None, timeout=120):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {}), time.time() - started
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:300], time.time() - started


status, tok, secs = call("/api/users/login/", {"email": os.environ.get("FIBER_EMAIL", ""), "password": os.environ.get("FIBER_PASSWORD", "")})
print("login %s %.1fs" % (status, secs))
token = tok["access"]

status, photos, secs = call("/api/features/%s/photos/" % FEATURE, token=token)
print("photos %s %.1fs count=%s" % (status, secs, photos.get("count") if isinstance(photos, dict) else photos))

for photo in photos.get("results", []):
    url = str(photo.get("url") or "")
    if "tape_test" not in url:
        continue
    status, done, secs = call(
        "/api/features/%s/photos/%s/" % (FEATURE, photo["id"]), token=token, method="DELETE"
    )
    print("delete %s %s %.1fs %s" % (photo["id"], status, secs, done))
