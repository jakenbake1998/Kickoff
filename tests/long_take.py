#!/usr/bin/env python3
"""Long-take test: one camera rolling for ~20 minutes while the song is played over and over,
with long stretches of no song (talk, resets) between passes, like an action camera left running.

    python3 tests/long_take.py [seed] [passes]

Prints every part the matcher cut, with the true song position of the pass it covers."""
import os
import sys
import time

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import stress_passes as sp    # noqa: E402
import make_synthetic as ms   # noqa: E402
import musicsync as m         # noqa: E402


def run(seed=5, n=12):
    r = np.random.default_rng(seed)
    ms.rng = np.random.default_rng(seed + 1)
    passes, c = [], r.uniform(5, 40)
    for _ in range(n):
        s0 = r.uniform(0, sp.DUR - 15)
        d = r.uniform(12, sp.DUR - s0)
        passes.append((c, s0, d))
        c += d + r.uniform(30, 150)
    length = c
    audio = signal.resample_poly(ms.passes_audio(sp.SONG, passes, length, snr_db=8, live_drums=True),
                                 147, 640).astype(np.float32)
    clip = m.Clip(path="long", rel="long", duration=length, fps=23.976, has_audio=True)
    m.load_channels = lambda path, layout: [("channel 1", audio)]
    t0 = time.time()
    m.sync_clip(clip, sp.MASTER, m.Settings())
    print("%.0f s take, %d passes, matched in %.1f s" % (length, n, time.time() - t0))
    truth = [(c0, c0 + d, s0 - c0) for c0, s0, d in passes]
    bad = 0
    for p in (clip.parts if clip.split else []):
        tr = [o for a, b, o in truth if min(b, p.src_out) - max(a, p.src_in) > 2]
        err = min((abs(p.offset - o) for o in tr), default=None) if p.offset is not None else None
        flag = ""
        if p.status == "placed" and (err is None or err > 0.03) and p.repeat_alt is None:
            flag, bad = "  <-- OFF", bad + 1
        print("  %7.1f-%7.1f %-10s off %8s true %-18s check %-6s drift %-6s %s %s%s" % (
            p.src_in, p.src_out, p.status, "%.2f" % p.offset if p.offset is not None else "-",
            ",".join("%.2f" % o for o in tr), p.check or "-", p.drift_ms, p.reason or "", p.notes, flag))
    placed = sum(1 for p in clip.parts if p.status == "placed")
    print("truth passes:", ", ".join("%.0f-%.0f" % (a, b) for a, b, _ in truth))
    print("parts %d, placed %d of %d passes, off %d" % (len(clip.parts), placed, n, bad))
    return bad


if __name__ == "__main__":
    sys.exit(1 if run(*[int(a) for a in sys.argv[1:]]) else 0)
