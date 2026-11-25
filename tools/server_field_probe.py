"""Ask a deployed server the same question the app asks it, photo by photo.

    env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe \
        tools/server_field_probe.py --server https://host --token-file tmp/prod_token.txt

Why this exists: the reader runs on the server, so "the app stopped reading the
tape" can be a code regression, a host without the OCR binary, or the photo.
This asks one host about every photo the crew recorded a span for, and prints
the stage each one stops at — which is the difference between those three.

It prints ``/healthz/tape`` first, because a host without tesseract refuses
every blade at the numbers stage and nothing else here will look wrong.

Verdicts are the ones `tools/field_regression.py` uses, so the local run and
the server run are directly comparable: ``OK``, ``WRONG`` (which fails this
run), ``REFUSED``, and ``UNSCORED`` for a photo with no recorded span.

Nothing is stored on the server: ``/api/tape/frame/`` answers a frame without
creating a row, which is why this can be pointed at production.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests  # noqa: E402

from field_regression import (  # noqa: E402
    DEFAULT_FOLDER,
    DEFAULT_TOLERANCE_MM,
    META_NAME,
    classify,
)

MAX_FRAME_BYTES = 4 * 1024 * 1024
IMAGE_SUFFIXES = ('.jpeg', '.jpg')
PROBE_TIMEOUT = 180


def fit_frame(name: str, data: bytes) -> tuple[bytes, bool]:
    """The bytes to send: the photo itself unless it exceeds the endpoint's cap."""
    if len(data) <= MAX_FRAME_BYTES:
        return data, False
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert('RGB')
    image.thumbnail((1600, 1600), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format='JPEG', quality=85)
    return buffer.getvalue(), True


def probe_host(server: str, token: str) -> None:
    url = server.rstrip('/') + '/healthz/tape'
    try:
        response = requests.get(url, timeout=60)
    except requests.RequestException as exc:
        print(f'healthz/tape: unreachable ({type(exc).__name__}: {exc})')
        return
    try:
        body = response.json()
    except ValueError:
        print(f'healthz/tape: HTTP {response.status_code}, non-JSON body')
        return
    print(f'healthz/tape: HTTP {response.status_code} {json.dumps(body)[:200]}')
    if not body.get('ok'):
        print('  ^ this host cannot read any tape; the photos below will all refuse.')


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--server', required=True, help='base URL of the deployed backend')
    parser.add_argument('--token', default=os.environ.get('TAPE_TOKEN', ''))
    parser.add_argument('--token-file', help='file holding a JWT, e.g. tmp/prod_token.txt')
    parser.add_argument(
        'folder', nargs='?', default=os.environ.get('TAPE_FIELD_DIR', DEFAULT_FOLDER)
    )
    parser.add_argument('--element', default='trench')
    parser.add_argument('--dimension', default='depth')
    parser.add_argument('--tolerance', type=float, default=DEFAULT_TOLERANCE_MM)
    parser.add_argument('--limit', type=int, default=0, help='stop after N photos')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)

    token = args.token
    if not token and args.token_file:
        with open(args.token_file, encoding='utf-8') as handle:
            token = handle.read().strip()
    if not token:
        raise SystemExit('a token is required: --token, --token-file or $TAPE_TOKEN')

    probe_host(args.server, token)

    with open(os.path.join(args.folder, META_NAME), encoding='utf-8') as handle:
        meta = json.load(handle)
    names = [
        name
        for name in sorted(os.listdir(args.folder))
        if name.lower().endswith(IMAGE_SUFFIXES)
    ]
    if args.limit:
        names = names[: args.limit]

    endpoint = args.server.rstrip('/') + '/api/tape/frame/'
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {token}'})
    rows: list[dict] = []

    for name in names:
        entry = meta.get(name) or {}
        truth = entry.get('value')
        truth_mm = float(truth) if isinstance(truth, (int, float)) else None
        with open(os.path.join(args.folder, name), 'rb') as handle:
            data, resized = fit_frame(name, handle.read())

        started = time.monotonic()
        try:
            response = session.post(
                endpoint,
                files={'frame': (name, data, 'image/jpeg')},
                data={'element_type': args.element, 'dimension': args.dimension},
                timeout=PROBE_TIMEOUT,
            )
        except requests.RequestException as exc:
            rows.append(
                {
                    'photo': name,
                    'truth_mm': truth_mm,
                    'reading_mm': None,
                    'stage': 'network',
                    'reason': f'{type(exc).__name__}: {exc}',
                    'http': None,
                    'ms': int((time.monotonic() - started) * 1000),
                    'verdict': 'ERROR',
                }
            )
            continue
        wall_ms = int((time.monotonic() - started) * 1000)
        if response.status_code != 200:
            rows.append(
                {
                    'photo': name,
                    'truth_mm': truth_mm,
                    'reading_mm': None,
                    'stage': f'http {response.status_code}',
                    'reason': response.text[:160],
                    'http': response.status_code,
                    'ms': wall_ms,
                    'verdict': 'ERROR',
                }
            )
            continue
        body = response.json()
        clip = body.get('clip') or {}
        rows.append(
            {
                'photo': name,
                'truth_mm': truth_mm,
                'reading_mm': clip.get('reading_mm'),
                'stage': clip.get('stage'),
                'reason': clip.get('reason'),
                'http': 200,
                'ms': wall_ms,
                'server_ms': body.get('elapsed_ms'),
                'resized': resized,
                'verdict': classify(clip, truth_mm, args.tolerance),
            }
        )

    summary = {
        'photos': len(rows),
        'ok': sum(row['verdict'] == 'OK' for row in rows),
        'wrong': sum(row['verdict'] == 'WRONG' for row in rows),
        'refused': sum(row['verdict'] == 'REFUSED' for row in rows),
        'unscored': sum(row['verdict'] == 'UNSCORED' for row in rows),
        'errors': sum(row['verdict'] == 'ERROR' for row in rows),
        'median_ms': sorted(row['ms'] for row in rows)[len(rows) // 2] if rows else 0,
        'slowest_ms': max((row['ms'] for row in rows), default=0),
        'server': args.server,
    }

    if args.json:
        print(json.dumps({'summary': summary, 'photos': rows}, indent=2))
    else:
        print()
        print(f'{"photo":44s} {"truth":>6s} {"read":>6s} {"verdict":9s} {"stage":9s} {"ms":>6s}')
        for row in rows:
            print(
                f'{row["photo"][:44]:44s} {str(row["truth_mm"] or "-"):>6s} '
                f'{str(row["reading_mm"] or "-"):>6s} {row["verdict"]:9s} '
                f'{str(row["stage"] or "-"):9s} {row["ms"]:6d}'
            )
            if row['verdict'] in ('WRONG', 'REFUSED', 'ERROR') and row['reason']:
                print(f'{"":44s}   {row["reason"][:110]}')
        print()
        print(
            'ok {ok}, wrong {wrong}, refused {refused}, errors {errors} '
            '(unscored {unscored})'.format(**summary)
        )
        print(f'timing: median {summary["median_ms"]} ms, slowest {summary["slowest_ms"]} ms')

    return 1 if summary['wrong'] or summary['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
