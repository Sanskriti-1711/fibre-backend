#!/usr/bin/env python
"""Stamp the tape reading, the location and the field notes onto evidence photos.

Run it on a folder of field photos and get a copy of each with a panel in one
corner: what the app read off the blade, where the photo was taken, when, and
the section/chamber notes the crew wrote. The reading comes from the same code
the phone endpoint uses (``supervision.tape_measure``), so a photo that fails
here fails on the phone for the same reason -- which is how the panel is useful
for verification as well as for the record.

    python stamp_evidence.py --dir "D:/photos" --out "D:/photos/annotated"

Per-photo metadata (geolocation, section, chamber, remark, and an
``element_type``/``dimension`` override) comes from a JSON file keyed by file
name; a GPS block still in the photo's EXIF is used when the JSON has none. A
photo with no location is stamped "not recorded": the panel never invents a
value a reviewer might read as fact.

Example JSON:

    {
      "defaults": {"element_type": "trench", "section": "AMB-S01"},
      "IMG_0042.jpg": {
        "dimension": "depth",
        "latitude": 13.126334, "longitude": 80.153331, "accuracy_m": 16.1,
        "elevation_m": 44.11, "elevation_uncertainty_m": 1,
        "captured_at": "05-12-2025 17:53",
        "chamber": "06", "remark": "LTV HH",
        "value": 260, "unit": "mm", "value_source": "blade reading",
        "tape_span": "20 to 46 cm"
      }
    }

"value" is the engineer-confirmed reading for the dimension. It is only drawn
when the app's own read was refused, and the panel then says so: a number that
the tape reader never produced must not look like one it did."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from supervision import photo_stamp, tape_framing, tape_measure, tape_reader  # noqa: E402

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png')
TITLE = 'FIBRE FTTH | TAPE EVIDENCE'
TESSERACT_PATHS = (
    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
    r'C:\Users\HP\anaconda3\envs\ocrtest\Library\bin\tesseract.exe',
)


def ensure_tesseract() -> bool:
    """Whether a tape can be read at all, pointing pytesseract at a known binary.

    The binary is not always on PATH (here it lives under Program Files), and a
    reader that silently reports "nothing read" on an install problem is the
    failure this script must not have.
    """
    try:
        import pytesseract
    except ImportError:
        return False
    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        pass
    for path in TESSERACT_PATHS:
        if os.path.exists(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return tape_measure.ocr_available()
    return False


def read_photo(data: bytes, element_type: str, dimension: str) -> dict:
    """The app's tape read for one photo, in the shape the panel needs."""
    diagnostics: dict = {}
    framing = tape_framing.check_bytes(data, dimension)
    found = tape_measure.read_tape_bytes(data, diagnostics)
    proposal, note = None, ''
    if found:
        proposal, note = tape_measure.measure(
            element_type, dimension, found.get('readings') or [], found.get('unit') or 'cm'
        )
    try:
        clip = tape_reader.read_bytes(data)
    except Exception as error:  # a clip refusal must not cost the whole panel
        clip = {'usable': False, 'stage': 'exception',
                'reason': f'{type(error).__name__}: {error}'}
    return {'found': found, 'framing': framing, 'proposal': proposal, 'note': note,
            'diagnostics': diagnostics, 'clip': clip}


def clip_text(clip: dict) -> str:
    """The clip reading in one panel line: a millimetre, or the stage that refused.

    This is the reading the crew asked the app for -- one printed number and the
    millimetres counted from it to the clip's face -- so a refusal is shown with
    its stage rather than left blank, which would read as "not attempted".
    """
    if clip.get('usable'):
        label = clip.get('label') or {}
        return (
            f"{clip['reading_mm']:g} mm at the clip face, "
            f"{clip['divisions_to_face']:.2f} div past {label.get('value')}"
        )
    stage = clip.get('stage') or 'clip'
    reason = str(clip.get('reason') or 'refused').split('. ')[0].rstrip('.')
    return f'refused at {stage}: {reason}'[:120]


def panel_rows(meta: dict, read: dict) -> list[tuple[str, str]]:
    """Every fact the panel shows, in the order the current field photos print them."""
    rows: list[tuple[str, str]] = []
    found = read['found'] or {}

    readings = found.get('readings') or []
    if meta.get('tape_span'):
        # A confirmed value names the marks it came from; showing the refused
        # run beside it would read as two disagreeing measurements.
        rows.append(('Tape', str(meta['tape_span'])))
    elif readings:
        unit = found.get('unit') or 'cm'
        rows.append(
            (
                'Tape',
                f"{readings[0]['value']:g} to {readings[-1]['value']:g} {unit}, "
                f"{len(readings)} marks",
            )
        )
    else:
        rows.append(('Tape', 'no numbers read'))
    rows.append(('Clip', clip_text(read.get('clip') or {})))

    latitude, longitude = meta.get('latitude'), meta.get('longitude')
    if latitude is not None and longitude is not None:
        hemisphere = (
            f"{abs(latitude):.6f} {'N' if latitude >= 0 else 'S'}, "
            f"{abs(longitude):.6f} {'E' if longitude >= 0 else 'W'}"
        )
        label = f"GPS ({meta['gps_source']})" if meta.get('gps_source') else 'GPS'
        rows.append((label, hemisphere))
    else:
        rows.append(('GPS', 'not recorded on this photo'))

    if meta.get('accuracy_m') is not None:
        rows.append(('Accuracy', f"+/- {float(meta['accuracy_m']):.1f} m"))
    if meta.get('elevation_m') is not None:
        elevation = f"{float(meta['elevation_m']):.2f}"
        if meta.get('elevation_uncertainty_m') is not None:
            elevation += f" +/- {float(meta['elevation_uncertainty_m']):g} m"
        else:
            elevation += ' m'
        rows.append(('Elevation', elevation))
    if meta.get('heading'):
        rows.append(('Heading', str(meta['heading'])))

    rows.append(('Captured', str(meta.get('captured_at') or 'not recorded')))
    for key, label in (
        ('section', 'Section'),
        ('chamber', 'Chamber'),
        ('remark', 'Remark'),
        ('project', 'Project'),
        ('feature', 'Feature'),
    ):
        if meta.get(key):
            rows.append((label, str(meta[key])))

    if read['proposal']:
        rows.append(('Confidence', f"{read['proposal']['confidence']:.2f}"))
    elif meta.get('value') is not None:
        rows.append(('Source', str(meta.get('value_source') or 'entered')))
    return rows


def _value_text(value) -> str:
    try:
        return f'{float(value):g}'
    except (TypeError, ValueError):
        return str(value)


def headline_for(spec: dict | None, dimension: str, read: dict, meta: dict) -> str:
    label = (spec or {}).get('label') or dimension.replace('_', ' ')
    proposal = read['proposal']
    if proposal:
        return f"{label.upper()}  {proposal['value']:g} {proposal['unit']}"
    if meta.get('value') is not None:
        unit = meta.get('unit') or (spec or {}).get('unit') or 'mm'
        return f'{label.upper()}  {_value_text(meta["value"])} {unit}'
    return f'{label.upper()}  NO READING'


def short_reason(read: dict) -> str:
    """The one-line reason the app did not fill this dimension."""
    framing = read['framing'] or {}
    reason = (read['note'] or '').strip()
    if not reason and framing and not framing.get('ok'):
        reason = str(framing.get('message') or '')
    if not reason:
        reason = ', '.join(tape_measure.retake_reasons(read['diagnostics']))
    if not reason:
        reason = 'no tape numbers were read'
    return reason.split('. ')[0].rstrip('.')[:130]


def footer_for(read: dict, meta: dict) -> str:
    if read['proposal']:
        return 'Read in-app from the blade. Confirm against the tape before saving.'
    if meta.get('value') is not None:
        return f'App read refused ({short_reason(read)}). Value entered from the blade.'
    return f'Not filled: {short_reason(read)}'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--dir', required=True, help='folder of photos to stamp')
    parser.add_argument('--out', help='folder for the stamped copies (default: <dir>/annotated)')
    parser.add_argument('--meta', help='JSON file of per-photo facts, keyed by file name')
    parser.add_argument('--element-type', default='trench')
    parser.add_argument('--dimension', default='depth')
    parser.add_argument('--corner', default='top-right',
                        choices=['top-left', 'top-right', 'bottom-left', 'bottom-right'])
    parser.add_argument('--skip-existing', action='store_true',
                        help='leave photos that already have an annotated copy alone')
    args = parser.parse_args(argv)

    source_dir = os.path.abspath(args.dir)
    if not os.path.isdir(source_dir):
        print(f'not a folder: {source_dir}')
        return 2
    out_dir = os.path.abspath(args.out or os.path.join(source_dir, 'annotated'))

    metadata: dict = {}
    defaults: dict = {}
    if args.meta:
        with open(args.meta, encoding='utf-8') as handle:
            document = json.load(handle)
        if isinstance(document, dict):
            defaults = document.get('defaults') or {}
            metadata = {
                key: value
                for key, value in document.items()
                if key != 'defaults' and isinstance(value, dict)
            }

    names = sorted(
        name for name in os.listdir(source_dir)
        if name.lower().endswith(IMAGE_EXTENSIONS)
        and os.path.isfile(os.path.join(source_dir, name))
    )
    if not names:
        print(f'no photos in {source_dir}')
        return 1

    if not ensure_tesseract():
        print('tesseract was not found: no tape can be read. Install it or set its path.')
        return 3

    os.makedirs(out_dir, exist_ok=True)
    print(f'stamping {len(names)} photo(s)')
    print(f'  from {source_dir}')
    print(f'  into {out_dir}')
    print()

    read_count = 0
    failed: list[str] = []
    skipped: list[str] = []
    for index, name in enumerate(names, start=1):
        path = os.path.join(source_dir, name)
        with open(path, 'rb') as handle:
            data = handle.read()

        meta = {**photo_stamp.exif_location(data), **defaults, **metadata.get(name, {})}
        element_type = meta.get('element_type') or args.element_type
        dimension = meta.get('dimension') or args.dimension

        destination = os.path.join(out_dir, os.path.splitext(name)[0] + '_annotated.jpg')
        if args.skip_existing and os.path.exists(destination):
            skipped.append(name)
            print(f'[{index}/{len(names)}] {name}\n      already stamped, left alone')
            continue

        started = time.monotonic()
        try:
            read = read_photo(data, element_type, dimension)
        except Exception as error:  # one unreadable file must not stop the batch
            failed.append(f'{name}: {error}')
            print(f'[{index}/{len(names)}] {name}\n      FAILED: {error}')
            continue

        spec = tape_measure.dimension_spec(element_type, dimension)
        stamped = photo_stamp.stamp_bytes(
            data,
            title=TITLE,
            headline=headline_for(spec, dimension, read, meta),
            rows=panel_rows(meta, read),
            footer=footer_for(read, meta),
            corner=args.corner,
        )
        with open(destination, 'wb') as handle:
            handle.write(stamped)
        elapsed = time.monotonic() - started

        proposal = read['proposal']
        if proposal:
            read_count += 1
            verdict = (
                f"{proposal['value']:g} {proposal['unit']} "
                f"(conf {proposal['confidence']:.2f})"
            )
        elif meta.get('value') is not None:
            verdict = (
                f"app refused; entered {_value_text(meta['value'])} "
                f"{meta.get('unit') or ''} ({short_reason(read)})"
            )
        else:
            verdict = f'no reading ({short_reason(read)})'
        print(f'[{index}/{len(names)}] {name}')
        print(f'      {dimension}: {verdict}  in {elapsed:.1f}s')
        print(f'      saved {destination} ({len(stamped) // 1024} KB)')

    print()
    stamped_count = len(names) - len(failed) - len(skipped)
    print(
        f'{stamped_count}/{len(names)} stamped, {read_count} with a tape reading'
        + (f', {len(skipped)} already had a copy' if skipped else '')
    )
    if failed:
        print('failed:')
        for line in failed:
            print(f'  {line}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
