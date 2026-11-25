"""Throwaway probe against the span the phone is reading: time the tape POST.""" 

import os
import json
import mimetypes
import time
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
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.status, json.loads(r.read() or b"{}")


def login():
    for attempt in range(4):
        status, body = call("/api/users/login/", {"email": EMAIL, "password": PASSWORD})
        if status == 200 and isinstance(body, dict) and body.get("access"):
            return body["access"]
        print("login", status, str(body)[:120], "- waiting")
        time.sleep(60)
    raise SystemExit("login throttled")


def main():
    token = login()
    print("logged in")

    status, projects = call("/api/projects/", token=token)
    print("projects", status, len(projects))
    project = next(
        p for p in projects
        if "edgbaston" in p["name"].lower() and "supervision" in p["name"].lower()
    )
    print("project", project["id"], project["name"])

    status, layers = call("/api/projects/%s/layers/" % project["id"], token=token)
    print("layers", status, [layer.get("name") for layer in layers.get("layers", [])])
    raw_layers = layers.get("layers", [])
    print("layer sample", json.dumps(raw_layers[:2])[:700])

    def layer_id(layer):
        for key in ("id", "layer_id", "layerId", "uuid"):
            if layer.get(key):
                return layer[key]
        for value in layer.values():
            if isinstance(value, str) and len(value) == 36:
                return value
        return None

    fid = None
    for layer in raw_layers:
        lid = layer_id(layer)
        if not lid:
            continue
        status, detail = call(
            "/api/projects/%s/layers/%s/" % (project["id"], lid), token=token
        )
        features = detail.get("features", []) if isinstance(detail, dict) else []
        print(" layer", lid, status, len(features), str(features[:1])[:160])
        for feature in features:
            blob = json.dumps(feature)
            if "TR-000010" in blob:
                fid = feature.get("id")
                print(" match", fid, blob[:200])
                break
        if fid:
            break
    if not fid:
        raise SystemExit("span not found")

    status, photos = call("/api/features/%s/photos/" % fid, token=token)
    print("photos", status, "count", photos.get("count"), "max", photos.get("max_photos"))
    for row in photos.get("results", []):
        print("   ", row.get("id"), row.get("uploaded_at"), row.get("description"))

    started = time.time()
    status, up = upload("/api/features/%s/photos/" % fid, "tape_test4.png", token)
    print("upload", status, "%.1fs" % (time.time() - started))
    photo_id = (up.get("uploaded") or [{}])[0].get("id")
    print("photo", photo_id)

    started = time.time()
    try:
        status, reading = call(
            "/api/features/%s/tape-measurements/" % fid,
            {"photo_id": photo_id, "element_type": "trench", "dimension": "length"},
            token=token,
        )
        print("tape-measurements", status, "%.1fs" % (time.time() - started))
        print(json.dumps(reading, indent=2)[:1200])
    except Exception as error:
        print("tape-measurements failed after %.1fs:" % (time.time() - started), type(error).__name__, error)

    status, gone = call("/api/features/%s/photos/%s/" % (fid, photo_id), token=token, method="DELETE")
    print("cleanup", status, gone)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
