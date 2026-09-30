#!/usr/bin/env python3
"""Check a narrative run against make_narrative.py's expected.json: each clip with the right audio
file, starting within a frame of the right spot; the B-roll left unsynced. Prints and counts WRONG."""
import csv
import json
import sys

exp = json.load(open(sys.argv[1]))
rows = {r["file"]: r for r in csv.DictReader(open(sys.argv[2]))}
wrong = missed = 0
for f, want in exp.items():
    r = rows.get(f)
    if r is None:
        print("MISSING ", f)
        wrong += 1
        continue
    if want is None:
        ok = r["status"] != "placed"
        print("%-8s %-36s %s" % ("ok" if ok else "WRONG", f, r["status"] + " " + r["reason"]))
        wrong += not ok
    elif r["status"] != "placed":
        print("missed   %-36s %s" % (f, r["reason"]))
        missed += 1
    else:
        err = float(r["starts_into_audio_s"]) - want[1]
        ok = r["audio_file"] == want[0] and abs(err) < 1 / 24
        print("%-8s %-36s %s %+.1f ms by %s" % ("ok" if ok else "WRONG", f, r["audio_file"], err * 1000, r["synced_by"]))
        wrong += not ok
print("wrong %d, missed %d" % (wrong, missed))
sys.exit(1 if wrong else 0)
