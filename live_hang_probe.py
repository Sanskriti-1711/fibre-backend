"""Throwaway: is the live hang the photo read or the OCR? Time one endpoint at a time."""

import os
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get('FIBER_API_BASE', 'http://127.0.0.1:8000')
EMAIL = os.environ.get('FIBER_EMAIL', '')
PASSWORD = os.environ.get('FIBER_PASSWORD', '')


def call(path, data=None, token=None, method=None, timeout=120):
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
            return e.code, raw[:300]


def upload(path, filename, token):
    boundary = "----probe" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    payload = open(filename, "rb").read()
    body = b"".join(
        [
            ("--" + boundary + "\r\n").encode(),
            ('Content-Disposition: form-data; name="photos"; filename="%s"\r\n' % filename).encode(),
            ("Content-Type: %s\r\n\r\n" % ctype).encode(),
            payload,
            ("\r\n--" + boundary + "--\r\n").encode(),
        ]
    )
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.status, json.loads(r.read() or b"{}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "ai"
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 120
    image = sys.argv[3] if len(sys.argv) > 3 else "tiny_tape.png"

    status, tok = call("/api/users/login/", {"email": EMAIL, "password": PASSWORD})
    print("login", status)
    token = tok["access"]

    status, projects = call("/api/projects/", token=token)
    pid = next(p["id"] for p in projects if p["name"].startswith("Demo Named Layers"))
    status, md = call("/api/projects/%s/map-data/" % pid, token=token)
    fid = None
    for feature in md["features"]:
        candidate = feature["properties"]["id"]
        status, photos = call("/api/features/%s/photos/" % candidate, token=token)
        if status == 200:
            fid = candidate
            print("feature", fid, "photos", photos.get("count"))
            break
    if not fid:
        print("no feature")
        return 1

    if mode == "bad":
        # Reaches only the element-type check, which is before any OCR: if this
        # is slow the request path itself is wedged, not tesseract.
        started = time.time()
        status, resp = call(
            "/api/features/%s/tape-measurements/" % fid,
            {
                "photo_id": "00000000-0000-0000-0000-000000000000",
                "element_type": "nonsense",
                "dimension": "length",
            },
            token=token,
            timeout=60,
        )
        print("bad", status, "%.1fs" % (time.time() - started), str(resp)[:200])
        return 0

    status, up = upload("/api/features/%s/photos/" % fid, image, token)
    photo_id = (up.get("uploaded") or [{}])[0].get("id")
    print("upload", status, "photo", photo_id)

    if mode == "ai":
        path, body = "/api/features/%s/ai-measurements/" % fid, {
            "photo_id": photo_id,
            "element_type": "trench",
        }
    else:
        path, body = "/api/features/%s/tape-measurements/" % fid, {
            "photo_id": photo_id,
            "element_type": "trench",
            "dimension": "length",
        }

    started = time.time()
    try:
        status, resp = call(path, body, token=token, timeout=timeout)
        print(mode, status, "%.1fs" % (time.time() - started))
        print(json.dumps(resp, indent=2)[:700])
    except Exception as error:
        print(mode, "no answer after %.1fs:" % (time.time() - started), type(error).__name__, error)

    status, gone = call("/api/features/%s/photos/%s/" % (fid, photo_id), token=token, method="DELETE")
    print("cleanup", status, gone)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
