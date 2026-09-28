#!/usr/bin/env python3
"""Randomized accuracy test for takes where the song restarts, pauses or jumps.

    python3 tests/stress_passes.py [N]

Builds N takes (single pass, restart, straight jump, pause, three passes) from the synthetic song
through the simulated room, runs the matcher on each, and counts passes placed at the right song
position, at the other copy of a pasted chorus (flagged "check chorus": lip sync is the same), a frame or three off (two passes whose song positions differ by under 0.3 s are treated
as one pass that drifted), at a wrong one (should always be 0), and missed. Misses are listed with how many seconds
of the pass lie outside the pasted chorus: a pass that only covers the chorus fits both copies and
is correctly left unplaced."""
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import make_synthetic as ms   # noqa: E402
import musicsync as m         # noqa: E402

FS = ms.FS
SONG, MARKS = ms.make_master()
DUR = len(SONG) / FS
MASTER = m.MasterIndex(signal.resample_poly(SONG, 147, 640).astype(np.float32))   # 48k -> 11025
CHORUS = [(MARKS["chorus1"], MARKS["chorus1"] + 16), (MARKS["chorus2"], MARKS["chorus2"] + 16)]
KINDS = ["single", "restart", "jump", "pause", "three"]


def take(seed):
    r = np.random.default_rng(seed)
    ms.rng = np.random.default_rng(seed + 1000)
    kind = KINDS[seed % len(KINDS)]
    if kind == "single":
        s0 = r.uniform(0, DUR - 10)
        passes = [(0.0, s0, min(r.uniform(8, 70), DUR - s0))]
        length = passes[0][2]
    else:
        passes, c = [], 0.0
        for _ in range(3 if kind == "three" else 2):
            s0 = r.uniform(0, DUR - 12)
            d = r.uniform(8, min(30, DUR - s0))
            passes.append((c, s0, d))
            c += d + {"jump": 0.0, "pause": r.uniform(2, 5)}.get(kind, r.uniform(3, 10))
        length = c + r.uniform(0, 3)
    kw = dict(snr_db=r.uniform(3, 15), live_drums=bool(r.integers(0, 2)))
    audio = signal.resample_poly(ms.passes_audio(SONG, passes, length, **kw), 147, 640).astype(np.float32)
    clip = m.Clip(path="take%d" % seed, rel="take%d" % seed, duration=length, fps=23.976, has_audio=True)
    m.load_audio = lambda path, stream="a:0": audio
    m.sync_clip(clip, MASTER, m.Settings())
    got = [(p.offset, p.status, p.repeat_alt) for p in clip.parts] if clip.split else \
        [(clip.offset, clip.status, clip.repeat_alt)]
    return seed, kind, passes, got


def outside_chorus(s0, d):
    return d - sum(max(0.0, min(s0 + d, b) - max(s0, a)) for a, b in CHORUS)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    t0 = time.time()
    agg = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    gap = CHORUS[1][0] - CHORUS[0][0]
    missed = []
    with ProcessPoolExecutor(os.cpu_count() or 2) as ex:
        for seed, kind, passes, got in ex.map(take, range(n)):
            a = agg[kind]
            a[0] += 1
            for c0, s0, d in passes:
                a[1] += 1
                if any(st == "placed" and alt is None and abs(o - (s0 - c0)) < 0.03 for o, st, alt in got):
                    a[2] += 1
                elif any(st == "placed" and alt is not None and
                         min(abs(o - (s0 - c0) + k) for k in (-gap, 0, gap)) < 0.03 for o, st, alt in got):
                    a[5] += 1          # chorus-only pass placed at a copy of the chorus, flagged
                else:
                    missed.append((seed, kind, round(s0, 1), round(d, 1), round(outside_chorus(s0, d), 1)))
            exp = [s0 - c0 for c0, s0, _ in passes]
            err = [min(abs(o - e + k) for e in exp for k in ((-gap, 0, gap) if alt is not None else (0,)))
                   for o, st, alt in got if st == "placed"]
            a[3] += sum(1 for e in err if e >= 0.15)            # visibly out of sync
            a[4] += sum(1 for e in err if 0.03 <= e < 0.15)     # a frame or three off
    for kind in KINDS:
        t, p, ok, bad, near, rep_ = agg[kind]
        print("%-8s takes %3d  passes %3d  placed right %3d  at a chorus copy (flagged) %3d  within 3 frames %d"
              "  placed WRONG %d" % (kind, t, p, ok, rep_, near, bad))
    print("missed passes (seed, kind, song start, length, seconds outside the chorus):")
    for x in sorted(missed, key=lambda x: -x[4]):
        print("  ", x)
    print("%.0f s" % (time.time() - t0))
    sys.exit(1 if any(a[3] for a in agg.values()) else 0)
