"""Throwaway: which pass first reads the whole blade, and what it costs."""

import sys
import time

import pytesseract

import supervision.tape_measure as tm

calls = []
orig = pytesseract.image_to_data


def spy(image, **kwargs):
    started = time.time()
    data = orig(image, **kwargs)
    text = [
        value
        for value in (data.get("text") or [])
        if value and value.strip()
    ]
    calls.append(
        (
            round(time.time() - started, 2),
            "%dx%d" % image.size,
            kwargs.get("config", "").split()[-1],
            text[:12],
        )
    )
    return data


pytesseract.image_to_data = spy

name = sys.argv[1] if len(sys.argv) > 1 else "tape_test4.png"
started = time.time()
read = tm.read_tape_bytes(open(name, "rb").read())
print(name, "read %.2fs" % (time.time() - started), "| passes", len(calls))
for index, call in enumerate(calls, 1):
    print(" %2d" % index, call)
print("readings", [(r["value"], round(r["x"], 2)) for r in (read or {}).get("readings", [])])
print("bounds", (read or {}).get("bounds"))
