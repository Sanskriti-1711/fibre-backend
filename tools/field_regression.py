"""Score the tape reader against the spans recorded in the field metadata.

    env -u PYTHONPATH -u PYTHONHOME .venv/Scripts/python.exe \
        tools/field_regression.py [FOLDER] [--tolerance MM] [--json]

`FOLDER` holds the crew's photos and the `stamp-meta.json` written beside them,
whose `value` field is the span read off each blade by eye. The photos are not
in the repository, so the folder defaults to `$TAPE_FIELD_DIR` and then to the
demo folder on the build machine; nothing here runs in CI.

Three outcomes, and they are deliberately not equal:

``OK``        the reader produced a number within tolerance of the record.
``WRONG``     it produced a number outside tolerance. This is the one outcome
              that fails the run: a confident wrong span is worse than no
              reading, because it is saved to the record.
``REFUSED``   it produced no number, and said at which stage. Counted and
              printed, but it does not fail the run — the whole field set is
              currently refused, so treating a refusal as failure would make
              the harness red from the first commit and it would be ignored.

A photo the metadata scores only by unit (no `value`) is still run, so its
refusal reason is visible, but it is not scored.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
META_NAME = 'stamp-meta.json'
IMAGE_SUFFIXES = ('.jpeg', '.jpg')

# Printed numbers differ by one division at worst; 2 % or this many millimetres,
# whichever is larger, is a read of the right division.
DEFAULT_TOLERANCE_MM = 10.0
RELATIVE_TOLERANCE = 0.02


def tolerance_for(truth_mm: float, tolerance_mm: float) -> float:
    return max(tolerance_mm, abs(truth_mm) * RELATIVE_TOLERANCE)


def classify(result: dict, truth_mm: float | None, tolerance_mm: float) -> str:
    """`OK`, `WRONG`, `REFUSED`, or `UNSCORED` for a photo with no record."""
    if truth_mm is None:
        return 'UNSCORED'
    reading = result.get('reading_mm')
    if not result.get('usable') or reading is None:
        return 'REFUSED'
    if abs(float(reading) - float(truth_mm)) <= tolerance_for(truth_mm, tolerance_mm):
        return 'OK'
    return 'WRONG'


def read_folder(folder: str, tolerance_mm: float) -> list[dict]:
    """Run every scored photo in `folder`, newest tag first in the report."""
    from supervision import tape_reader

    meta_path = os.path.join(folder, META_NAME)
    if not os.path.isfile(meta_path):
        raise SystemExit(f'no {META_NAME} in {folder}')
    with open(meta_path, encoding='utf-8') as handle:
        meta = json.load(handle)

    names = [name for name in sorted(os.listdir(folder)) if name.lower().endswith(IMAGE_SUFFIXES)]
    rows: list[dict] = []
    for name in names:
        entry = meta.get(name) or {}
        truth = entry.get('value')
        truth_mm = float(truth) if isinstance(truth, (int, float)) else None
        with open(os.path.join(folder, name), 'rb') as handle:
            data = handle.read()
        started = time.monotonic()
        result = tape_reader.read_bytes(data)
        elapsed = int((time.monotonic() - started) * 1000)
        rows.append(
            {
                'photo': name,
                'dimension': entry.get('dimension'),
                'truth_mm': truth_mm,
                'reading_mm': result.get('reading_mm'),
                'stage': result.get('stage'),
                'reason': result.get('reason'),
                'ms': elapsed,
                'verdict': classify(result, truth_mm, tolerance_mm),
            }
        )
    return rows


def report(rows: list[dict], tolerance_mm: float) -> dict:
    scored = [row for row in rows if row['verdict'] != 'UNSCORED']
    summary = {
        'photos': len(rows),
        'scored': len(scored),
        'ok': sum(row['verdict'] == 'OK' for row in rows),
        'wrong': sum(row['verdict'] == 'WRONG' for row in rows),
        'refused': sum(row['verdict'] == 'REFUSED' for row in rows),
        'unscored': sum(row['verdict'] == 'UNSCORED' for row in rows),
        'tolerance_mm': tolerance_mm,
    }
    correct = [row for row in rows if row['verdict'] == 'OK']
    if correct:
        summary['worst_error_mm'] = round(
            max(abs(row['reading_mm'] - row['truth_mm']) for row in correct), 1
        )
    summary['slowest_ms'] = max((row['ms'] for row in rows), default=0)
    summary['median_ms'] = sorted(row['ms'] for row in rows)[len(rows) // 2] if rows else 0
    return summary


def print_report(rows: list[dict], summary: dict) -> None:
    print(f'{"photo":46s} {"truth":>7s} {"read":>7s} {"verdict":9s} {"stage":8s} {"ms":>6s}')
    for row in rows:
        print(
            f'{row["photo"][:46]:46s} '
            f'{str(row["truth_mm"] or "-"):>7s} '
            f'{str(row["reading_mm"] or "-"):>7s} '
            f'{row["verdict"]:9s} '
            f'{str(row["stage"] or "-"):8s} '
            f'{row["ms"]:6d}'
        )
        if row['verdict'] in ('WRONG', 'REFUSED') and row['reason']:
            print(f'{"":46s}   {row["reason"][:110]}')
    print()
    print(
        'scored {scored}/{photos}: ok {ok}, wrong {wrong}, refused {refused} '
        '(unscored {unscored})'.format(**summary)
    )
    if 'worst_error_mm' in summary:
        print(f'worst accepted error: {summary["worst_error_mm"]} mm')
    print(f'timing: median {summary["median_ms"]} ms, slowest {summary["slowest_ms"]} ms')


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        'folder',
        nargs='?',
        default=os.environ.get('TAPE_FIELD_DIR', DEFAULT_FOLDER),
        help='folder holding the photos and stamp-meta.json',
    )
    parser.add_argument(
        '--tolerance',
        type=float,
        default=DEFAULT_TOLERANCE_MM,
        help=f'absolute tolerance in mm (default {DEFAULT_TOLERANCE_MM:g}, '
        f'and never tighter than {int(RELATIVE_TOLERANCE * 100)} %)',
    )
    parser.add_argument('--json', action='store_true', help='print JSON instead of a table')
    args = parser.parse_args(argv)

    rows = read_folder(args.folder, args.tolerance)
    summary = report(rows, args.tolerance)
    if args.json:
        print(json.dumps({'summary': summary, 'photos': rows}, indent=2))
    else:
        print_report(rows, summary)
    # Only a wrong number fails: that is the outcome that reaches a survey record.
    return 1 if summary['wrong'] else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
