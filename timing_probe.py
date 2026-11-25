"""Throwaway: how long one tape read takes here, and how many OCR passes it makes."""

import time

import pytesseract

import supervision.tape_measure as tm

count = {"n": 0}
orig_data = pytesseract.image_to_data
orig_str = pytesseract.image_to_string


def spy_data(*a, **k):
    count["n"] += 1
    return orig_data(*a, **k)


def spy_str(*a, **k):
    count["n"] += 1
    return orig_str(*a, **k)


pytesseract.image_to_data = spy_data
pytesseract.image_to_string = spy_str

for name in ("tape_test4.png", "field_eval/trench_scene.jpg"):
    data = open(name, "rb").read()
    started = time.time()
    read = tm.read_tape_bytes(data)
    elapsed = time.time() - started
    print(
        name,
        "read %.2fs" % elapsed,
        "| ocr passes", count["n"],
        "| readings", [(r["value"], round(r["x"], 2)) for r in (read or {}).get("readings", [])],
        "| bounds", (read or {}).get("bounds"),
    )
    count["n"] = 0
