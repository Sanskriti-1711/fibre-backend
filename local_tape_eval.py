"""Evaluate the tape reader on local images.

Usage:
    python local_tape_eval.py <image> [image ...] [--element trench] [--dimension length]
    python local_tape_eval.py <image> ... --engine vision

`--engine ocr` (the default) runs the local tesseract reader, `vision` runs the
model reader, and `both` runs them in the order the endpoint does: OCR first,
then the model when OCR found nothing. Local only — the vision engine uploads
the photo to the configured provider, nothing else leaves the machine.
"""

import json
import sys
import time

from supervision import tape_measure, tape_vision


def main(argv):
    element, dimension, engine = "trench", "depth", "ocr"
    paths = []
    i = 0
    while i < len(argv):
        if argv[i] == "--element":
            element = argv[i + 1]
            i += 2
        elif argv[i] == "--dimension":
            dimension = argv[i + 1]
            i += 2
        elif argv[i] == "--engine":
            engine = argv[i + 1]
            i += 2
        else:
            paths.append(argv[i])
            i += 1

    engines = {
        "ocr": [("ocr", tape_measure.read_tape_bytes)],
        "vision": [("vision", tape_vision.read_tape_bytes)],
        "both": [
            ("ocr", tape_measure.read_tape_bytes),
            ("vision", tape_vision.read_tape_bytes),
        ],
    }[engine]
    print(f"engines: {engine}; vision configured: {tape_vision.configured()}")

    for path in paths:
        with open(path, "rb") as handle:
            data = handle.read()
        print(f"\n=== {path} ({len(data) // 1024} KB) ===")
        read = None
        for label, reader in engines:
            started = time.time()
            read = reader(data)
            elapsed = time.time() - started
            print(f"  [{label}] {elapsed:.2f}s:", end=" ")
            if not read:
                print("no tape read")
                continue
            values = [
                (row["value"], row["unit"], row["x"], row["confidence"])
                for row in read["readings"]
            ]
            print(read["engine"], read["unit"], values)
            break
        if not read:
            continue
        row, note = tape_measure.measure(
            element, dimension, read["readings"], read["unit"]
        )
        print("  measure:", json.dumps(row) if row else None)
        print("  note:", note)


if __name__ == "__main__":
    main(sys.argv[1:])
