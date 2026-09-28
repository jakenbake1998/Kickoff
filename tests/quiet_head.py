#!/usr/bin/env python3
"""Long takes where a play starts under the band (its first third to half far quieter than the rest), plus a
short quiet play on its own, like the DJI takes on the DragonForce shoot. Measures how much of each
play its placed part covers, and counts wrong placements (must be 0).

    python3 tests/quiet_head.py [takes]"""
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import stress_passes as sp    # noqa: E402
import make_synthetic as ms   # noqa: E402
import musicsync as m         # noqa: E402


QMIN, QMAX = float(os.environ.get("QMIN", 12)), float(os.environ.get("QMAX", 18))   # quiet head, dB down
DIP = int(os.environ.get("DIP", 0))
SNR0, SNR1 = float(os.environ.get("SNR0", 4)), float(os.environ.get("SNR1", 10))


def take(seed):
    r = np.random.default_rng(seed)
    ms.rng = np.random.default_rng(seed + 7000)
    segs, plays, c = [], [], r.uniform(10, 40)
    for k in range(4):
        s0 = r.uniform(0, 12)
        d = r.uniform(0.7, 0.95) * (sp.DUR - s0)
        if k == 2:                                   # a short quiet play on its own
            d = r.uniform(18, 26)
            s0 = r.uniform(0, sp.DUR - d)
            segs.append((c, s0, d, 10 ** (-r.uniform(9, 13) / 20)))
        else:
            q = r.uniform(0.35, 0.6) * d             # quiet head, then the playback comes up
            if DIP:                                  # ...with a stretch in it where it can't be heard at all
                h1 = r.uniform(0.3, 0.5) * q
                segs.append((c, s0, h1, 10 ** (-r.uniform(QMIN, QMAX) / 20)))
                segs.append((c + h1, s0 + h1, q - h1, 10 ** (-40 / 20)))
            else:
                segs.append((c, s0, q, 10 ** (-r.uniform(QMIN, QMAX) / 20)))
            segs.append((c + q, s0 + q, d - q, 1.0))
        plays.append((c, c + d, s0 - c))
        c += d + r.uniform(40, 120)
    audio = signal.resample_poly(ms.passes_audio(sp.SONG, segs, c, snr_db=r.uniform(SNR0, SNR1), live_drums=True),
                                 147, 640).astype(np.float32)
    clip = m.Clip(path="q%d" % seed, rel="q%d" % seed, duration=c, fps=23.976, has_audio=True)
    m.load_channels = lambda path, layout: [("channel 1", audio)]
    m.sync_clip(clip, sp.MASTER, m.Settings())
    parts = [(p.src_in, p.src_out, p.offset, p.repeat_alt) for p in (clip.parts if clip.split else [])
             if p.status == "placed"]
    if not clip.split and clip.status == "placed":
        parts = [(0.0, c, clip.offset, clip.repeat_alt)]
    return seed, plays, parts


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    t0 = time.time()
    gap = sp.CHORUS[1][0] - sp.CHORUS[0][0]
    wrong = 0
    cover = {"long": [0.0, 0.0], "short": [0.0, 0.0]}
    found_short = 0
    with ProcessPoolExecutor(os.cpu_count() or 2) as ex:
        for seed, plays, parts in ex.map(take, range(n)):
            for a, b, o, alt in parts:
                tr = [x for x in plays if min(b, x[1]) - max(a, x[0]) > 2]
                ok = any(abs(o - x[2]) < 0.03 or (alt is not None and
                         min(abs(o - x[2] + k) for k in (-gap, gap)) < 0.03) for x in tr)
                if not ok:
                    wrong += 1
                    print("WRONG seed %d part %.1f-%.1f at %.3f (plays %s)" % (seed, a, b, o, tr and tr[0]))
            for k, (a, b, o) in enumerate(plays):
                kind = "short" if k == 2 else "long"
                got = sum(max(0.0, min(b, pb) - max(a, pa)) for pa, pb, po, alt in parts if abs(po - o) < 0.03)
                cover[kind][0] += got
                cover[kind][1] += b - a
                found_short += kind == "short" and got > 0
    print("long plays covered %.0f%%, short quiet plays found %d of %d (covered %.0f%%), placed WRONG %d" % (
        100 * cover["long"][0] / cover["long"][1], found_short, n,
        100 * cover["short"][0] / max(1, cover["short"][1]), wrong))
    print("%.0f s" % (time.time() - t0))
    sys.exit(1 if wrong else 0)
