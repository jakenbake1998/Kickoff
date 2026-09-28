#!/usr/bin/env python3
"""Compare a musicsync report against make_synthetic.py's expected.json.
    python3 check.py SYNTH_DIR/expected.json OUT_DIR/sync_report.csv"""
import collections, csv, json, sys

exp = json.load(open(sys.argv[1]))["clips"]
rows = collections.defaultdict(list)
for r in csv.DictReader(open(sys.argv[2])):
    rows[r["file"]].append(r)
fps = 24000 / 1001
tol = 1000 / fps / 2 + 25
bad = 0


def secs(clock):
    m, s = clock.split(":")
    return int(m) * 60 + float(s)


def placed_ok(r, offset):
    if r["status"] != "placed":
        return False, "not placed: %s" % r["reason"]
    err = (float(r["offset_seconds"]) - offset) * 1000
    return abs(err) < tol, "error %+.1f ms (%+.2f frames), conf %s, check %s" % (
        err, err / 1000 * fps, r["confidence"], r["waveform_check"] or "-")


for f, e in sorted(exp.items()):
    rs = rows.get(f)
    if not rs:
        print("MISSING  %s" % f); bad += 1; continue
    r = rs[0]
    if e["expect"] == "split":
        ok = len(rs) == len(e["passes"])
        detail = ["%d passes" % len(rs)]
        for k, (r, p) in enumerate(zip(rs, e["passes"])):
            if p["expect"] == "placed":
                good, d = placed_ok(r, p["offset"])
            else:
                good, d = r["status"] != "placed" and p["expect"] in r["reason"], r["reason"] or "placed!"
            a, b = (secs(v) for v in r["clip_range"].split("-"))
            if k:     # the cut must fall between the previous pass's end and this pass's start
                prev = e["passes"][k - 1]
                good = good and prev["end"] - 0.1 <= a <= p["start"] + 0.1
            ok = ok and good
            detail.append("[%s %s %s]" % (r["clip_range"], "ok" if good else "BAD", d))
        detail = " ".join(detail)
    elif e["expect"] == "placed":
        ok, detail = placed_ok(r, e["offset"])
        ok = ok and len(rs) == 1
    else:
        ok = r["status"] != "placed" and e["expect"] in r["reason"]
        detail = "%s (%s)" % (r["reason"] or "placed!", r["notes"])
    bad += not ok
    print("%-4s %-45s %-4s %-14s %s" % ("ok" if ok else "FAIL", f, r["camera"], e["expect"], detail))
print("%d failures" % bad)
sys.exit(1 if bad else 0)
