"""Throwaway: where does one tape read spend its time?"""

import cProfile
import io
import pstats

import supervision.tape_measure as tm

data = open("tape_test4.png", "rb").read()
profile = cProfile.Profile()
profile.enable()
read = tm.read_tape_bytes(data)
profile.disable()

out = io.StringIO()
pstats.Stats(profile, stream=out).sort_stats("cumulative").print_stats(16)
print(out.getvalue())
print("readings", [(r["value"], round(r["x"], 2)) for r in (read or {}).get("readings", [])])
