#!/usr/bin/env python3
"""Compare a musicsync report against make_synthetic.py's expected.json.
    python3 check.py SYNTH_DIR/expected.json OUT_DIR/sync_report.csv"""
import csv, json, sys

exp = json.load(open(sys.argv[1]))["clips"]
rows = {r["file"]: r for r in csv.DictReader(open(sys.argv[2]))}
fps = 24000 / 1001
bad = 0
for f, e in sorted(exp.items()):
    r = rows.get(f)
    if r is None:
        print("MISSING  %s" % f); bad += 1; continue
    if e["expect"] == "placed":
        if r["status"] != "placed":
            ok, detail = False, "not placed: %s" % r["reason"]
        else:
            err = (float(r["offset_seconds"]) - e["offset"]) * 1000
            ok, detail = abs(err) < 1000 / fps / 2 + 25, "error %+.1f ms (%+.2f frames), conf %s, drift %s ms" % (
                err, err / 1000 * fps, r["confidence"], r["drift_ms_head_to_tail"] or "-")
    else:
        ok = r["status"] != "placed" and e["expect"] in r["reason"]
        detail = "%s (%s)" % (r["reason"] or "placed!", r["notes"])
    bad += not ok
    print("%-4s %-45s %-4s %-14s %s" % ("ok" if ok else "FAIL", f, r["camera"], e["expect"], detail))
print("%d failures" % bad)
sys.exit(1 if bad else 0)
