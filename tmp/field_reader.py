"""Run the clip reader over the 24 field photos and log one JSON line each.

Read-only: nothing is written but the log. Run from fiber-backend with the venv
python; the photo folder is the field set the earlier probes used.
"""
import glob
import json
import os
import sys
import time

# A script under tmp/ gets tmp/ as sys.path[0], so the project root has to be on
# the path before `config` can be imported.
sys.path.insert(0, os.getcwd())

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.dev_settings')
django.setup()

from supervision import tape_reader  # noqa: E402

FOLDER = 'D:/Downloads_D/Q-GIS/Demo_ready/with_scale_images'
PHONE = sorted(glob.glob(FOLDER + '/2026*.jpg.jpeg'))
WHATSAPP = sorted(glob.glob(FOLDER + '/WhatsApp Image 2026-11-24*'))
LOG = 'tmp/field_reader.log'
SUMMARY = 'tmp/field_reader_summary.json'


def main():
    files = PHONE + WHATSAPP
    print(f'{len(files)} photos ({len(PHONE)} phone, {len(WHATSAPP)} whatsapp)', flush=True)
    rows = []
    with open(LOG, 'w', encoding='utf-8') as log:
        for index, path in enumerate(files, 1):
            name = os.path.basename(path)
            started = time.time()
            try:
                with open(path, 'rb') as handle:
                    data = handle.read()
                result = tape_reader.read_bytes(data)
            except Exception as error:  # one bad file must not stop the sweep
                result = {
                    'usable': False,
                    'reason': f'{type(error).__name__}: {error}',
                    'stage': 'exception',
                }
            elapsed = round(time.time() - started, 1)
            clip = result.get('clip') or {}
            row = {
                'photo': name,
                'set': 'phone' if path in PHONE else 'whatsapp',
                'seconds': elapsed,
                'usable': bool(result.get('usable')),
                'stage': result.get('stage'),
                'reason': result.get('reason'),
                'reading_mm': result.get('reading_mm'),
                'reference_face': result.get('reference_face'),
                'label': result.get('label'),
                'divisions_to_face': result.get('divisions_to_face'),
                'blade_confirmed': clip.get('blade_confirmed'),
                'clip_found': clip.get('found'),
                'tape_angle_deg': clip.get('tape_angle_deg'),
            }
            rows.append(row)
            log.write(json.dumps(row) + '\n')
            log.flush()
            print(
                f'[{index}/{len(files)}] {row["set"][:5]} {name[:34]:34} '
                f'{elapsed:5.1f}s usable={row["usable"]!s:5} '
                f'stage={str(row["stage"]):8} mm={row["reading_mm"]} '
                f'confirmed={row["blade_confirmed"]} {str(row["reason"])[:60]}',
                flush=True,
            )

    counts = {}
    for row in rows:
        key = 'read' if row['usable'] else (row['stage'] or 'unknown')
        counts[key] = counts.get(key, 0) + 1
    with open(SUMMARY, 'w', encoding='utf-8') as handle:
        json.dump({'counts': counts, 'rows': rows}, handle, indent=2)
    print('counts:', json.dumps(counts), flush=True)


if __name__ == '__main__':
    main()
