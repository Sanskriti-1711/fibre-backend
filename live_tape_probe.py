"""Throwaway live probe: upload a synthetic tape photo, run OCR, delete the photo."""

import os
import json
import mimetypes
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get('FIBER_API_BASE', 'http://127.0.0.1:8000')
EMAIL = os.environ.get('FIBER_EMAIL', '')
PASSWORD = os.environ.get('FIBER_PASSWORD', '')


def call(path, data=None, token=None, method=None, timeout=300):
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:500]


def upload(path, field, filename, token):
    boundary = "----probe" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(filename, "rb") as fh:
        payload = fh.read()
    parts = []
    parts.append(("--" + boundary + "\r\n").encode())
    parts.append(
        (
            'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (field, filename)
        ).encode()
    )
    parts.append(("Content-Type: %s\r\n\r\n" % ctype).encode())
    parts.append(payload)
    parts.append(("\r\n--" + boundary + "--\r\n").encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:500]


def main():
    image = sys.argv[1] if len(sys.argv) > 1 else "tape_test.png"
    element = sys.argv[2] if len(sys.argv) > 2 else "trench"
    dimension = sys.argv[3] if len(sys.argv) > 3 else "actual_depth_mm"

    status, tok = call("/api/users/login/", {"email": EMAIL, "password": PASSWORD})
    print("login", status)
    token = tok["access"]

    status, projects = call("/api/projects/", token=token)
    pid = next(p["id"] for p in projects if p["name"].startswith("Demo Named Layers"))
    status, md = call("/api/projects/%s/map-data/" % pid, token=token)
    features = [f["properties"]["id"] for f in md["features"]]
    print("project", pid, "features", len(features))

    target = None
    for fid in features:
        status, photos = call("/api/features/%s/photos/" % fid, token=token)
        if status == 200:
            target = fid
            print("using feature", fid, "existing photos", photos.get("count"))
            break
    if target is None:
        print("no editable feature found")
        return 1

    status, up = upload("/api/features/%s/photos/" % target, "photos", image, token)
    print("upload", status, str(up)[:400])
    photo_id = None
    if status in (200, 201):
        results = up.get("uploaded") or up.get("results") or up.get("photos") or []
        if isinstance(results, list) and results:
            photo_id = results[0].get("id")
        photo_id = photo_id or up.get("id")
    if not photo_id:
        print("no photo id, aborting")
        return 1

    import time
    started = time.time()
    try:
        status, reading = call(
            "/api/features/%s/tape-measurements/" % target,
            {"photo_id": photo_id, "element_type": element, "dimension": dimension},
            token=token,
        )
        print("tape-measurements", status, "%.1fs" % (time.time() - started))
        print(json.dumps(reading, indent=2)[:2000])
    except Exception as error:
        print("tape-measurements failed:", type(error).__name__, error)

    for extra in [photo_id] + [p for p in (sys.argv[4:] or [])]:
        status, gone = call(
            "/api/features/%s/photos/%s/" % (target, extra),
            token=token,
            method="DELETE",
        )
        print("cleanup delete", extra, status, gone)
    return 0


if __name__ == "__main__":
    sys.exit(main())
