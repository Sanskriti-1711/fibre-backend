"""Upload one test photo to production and check the stamped-photo fields.

Run from fiber-backend with the venv python. Reads the JWT from ../tmp/prod_token.txt.
"""
import json
import pathlib
import sys

import requests

BASE = 'https://fibre-backend-wml3.onrender.com'
PROJECT = '8cce6b1c-2ccb-4399-a864-d53c1e447628'
TOKEN = pathlib.Path('../tmp/prod_token.txt').read_text().strip()
HEAD = {'Authorization': f'Bearer {TOKEN}'}
EDITABLE = {'pending', 'assigned', 'redo'}


def walk(node, out):
    if isinstance(node, dict):
        if node.get('id') and node.get('status') in EDITABLE:
            out.append(node)
        for value in node.values():
            walk(value, out)
    elif isinstance(node, list):
        for value in node:
            walk(value, out)


def main():
    data = requests.get(
        f'{BASE}/api/projects/{PROJECT}/map-data/', headers=HEAD, timeout=180
    )
    print('map-data:', data.status_code)
    found = []
    walk(data.json(), found)
    seen = {}
    for feature in found:
        seen.setdefault(str(feature['id']), feature)
    print('editable features:', len(seen))
    for feature_id, feature in list(seen.items())[:5]:
        print('  ', feature_id, feature.get('status'), feature.get('layer_name'))

    if not seen:
        print('no editable feature; nothing uploaded')
        return 1

    feature_id, feature = next(iter(seen.items()))
    photo = pathlib.Path('../tmp/prod_stamp_test.jpg')
    with photo.open('rb') as handle:
        upload = requests.post(
            f'{BASE}/api/features/{feature_id}/photos/',
            headers=HEAD,
            files={'photos': (photo.name, handle, 'image/jpeg')},
            data={'description': 'Production stamp check (automated test photo)'},
            timeout=240,
        )
    print('upload:', upload.status_code)
    body = upload.json() if upload.content else {}
    if upload.status_code != 201:
        print(json.dumps(body)[:600])
        return 1

    uploaded = body['uploaded'][0]
    print(json.dumps(uploaded, indent=2)[:900])

    gallery = requests.get(
        f'{BASE}/api/features/{feature_id}/photos/', headers=HEAD, timeout=120
    ).json()
    latest = gallery['results'][-1]
    print('--- gallery fields')
    print('url           ', latest['url'])
    print('original_url  ', latest['original_url'])
    print('sha256        ', latest['sha256'])
    print('size_bytes    ', latest['size_bytes'])
    print('storage       ', latest['storage_backend'])

    stamped = requests.get(latest['url'], timeout=120)
    original = requests.get(latest['original_url'], timeout=120)
    print(
        'stamped GET', stamped.status_code, stamped.headers.get('Content-Type'),
        len(stamped.content), 'bytes',
    )
    print(
        'original GET', original.status_code, original.headers.get('Content-Type'),
        len(original.content), 'bytes',
    )
    print('distinct urls:', latest['url'] != latest['original_url'])
    print('annotated name:', latest['url'].split('?')[0].endswith('_annotated.jpg'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
