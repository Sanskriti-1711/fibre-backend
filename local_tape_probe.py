"""Throwaway local probe: run stored photos through the tape endpoint.

Unlike local_tape_eval.py (which calls the engines directly), this drives the
real HTTP route on a locally running backend, so what it prints is what the app
would receive: login, upload into a feature's gallery, ask for a reading, delete
the photo again.

Usage:
    python local_tape_probe.py <image> [image ...]

Environment:
    PROBE_BASE      default http://127.0.0.1:8000
    PROBE_EMAIL     default admin@admin.com
    PROBE_PASSWORD  default admin123$
    PROBE_ELEMENT   default trench
    PROBE_DIMENSION default actual_depth_mm
"""

import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("PROBE_BASE", "http://127.0.0.1:8000")
EMAIL = os.environ.get("PROBE_EMAIL", "admin@admin.com")
PASSWORD = os.environ.get("PROBE_PASSWORD", "admin123$")
ELEMENT = os.environ.get("PROBE_ELEMENT", "trench")
DIMENSION = os.environ.get("PROBE_DIMENSION", "actual_depth_mm")


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
    body = b"".join(
        [
            ("--" + boundary + "\r\n").encode(),
            (
                'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
                % (field, os.path.basename(filename))
            ).encode(),
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
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw[:500]


def find_feature(token):
    """The first feature in the dev database that this account may add to."""
    status, projects = call("/api/projects/", token=token)
    if status != 200:
        print("projects", status, str(projects)[:200])
        return None
    for project in projects:
        status, md = call("/api/projects/%s/map-data/" % project["id"], token=token)
        if status != 200:
            continue
        for feature in md.get("features", [])[:40]:
            fid = feature["properties"].get("id")
            if not fid:
                continue
            status, _ = call("/api/features/%s/photos/" % fid, token=token)
            if status == 200:
                return fid, project["name"]
    return None


def photo_id_from(response):
    rows = response.get("uploaded") or response.get("results") or response.get("photos") or []
    if isinstance(rows, list) and rows:
        return rows[0].get("id")
    return response.get("id")


def main(argv):
    images = [a for a in argv[1:] if not a.startswith("--")]
    if not images:
        print(__doc__)
        return 2

    status, token_body = call("/api/users/login/", {"email": EMAIL, "password": PASSWORD})
    print("login", status)
    if status != 200:
        return 1
    token = token_body["access"]

    found = find_feature(token)
    if not found:
        print("no editable feature in this database")
        return 1
    feature, project_name = found
    print("feature", feature, "in", project_name, "| element", ELEMENT, DIMENSION)

    refused = 0
    for image in images:
        print("\n===", os.path.basename(image))
        status, up = upload("/api/features/%s/photos/" % feature, "photos", image, token)
        if status not in (200, 201):
            print("upload", status, str(up)[:300])
            continue
        photo = photo_id_from(up)
        if not photo:
            print("upload", status, "returned no photo id:", str(up)[:300])
            continue

        started = time.time()
        status, reading = call(
            "/api/features/%s/tape-measurements/" % feature,
            {"photo_id": photo, "element_type": ELEMENT, "dimension": DIMENSION},
            token=token,
        )
        took = time.time() - started
        if status != 200:
            refused += 1
            print("tape-measurements", status, str(reading)[:300])
        else:
            if reading.get("measurement") is None:
                refused += 1
            print(
                "tape-measurements 200  %.1fs  engine=%s  measurement=%s %s  readings=%s"
                % (
                    took,
                    reading.get("engine"),
                    reading.get("measurement"),
                    reading.get("unit"),
                    reading.get("readings"),
                )
            )
            print("  notes:", reading.get("notes"))
            print("  framing:", reading.get("framing", "<absent>"))

        status, gone = call(
            "/api/features/%s/photos/%s/" % (feature, photo), token=token, method="DELETE"
        )
        print("  cleanup", status, gone)

    print("\n%d of %d photos came back without a reading" % (refused, len(images)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
