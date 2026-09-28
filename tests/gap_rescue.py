#!/usr/bin/env python3
"""Long takes where the landmarks miss whole performances (a DJI Action cam with the band louder
than the playback). The landmark gap search is switched off, so every play it can't vouch for is
left to the waveform (stray_song, then gap_rescue). Then a take of the band's OTHER song, same
tempo and kit, must place nothing.

    python3 tests/gap_rescue.py [takes]

Prints placed / missed / WRONG per take; exits 1 on any wrong placement."""
import sys

import numpy as np
from scipy import signal

import long_take as lt        # noqa: E402  (puts tests/ and the repo on sys.path)
import make_synthetic as ms   # noqa: E402
import musicsync as m         # noqa: E402
import stress_passes as sp    # noqa: E402


def other_song():
    parts = [ms.section(4, 11), ms.section(8, 12), ms.section(8, 13), ms.section(8, 14), ms.section(9, 15)]
    s = np.concatenate(parts + [parts[2]])
    return s / (np.abs(s).max() * 1.1)


def take(seed, song, n=6, weak=10):
    r = np.random.default_rng(seed)
    ms.rng = np.random.default_rng(seed + 1)
    passes, c = [], r.uniform(5, 40)
    for i in range(n):
        s0 = r.uniform(0, 2)
        d = sp.DUR - s0 - r.uniform(0, 4)
        passes.append((c, s0, d, 10 ** (-weak / 20) if i % 2 else 1.0))
        c += d + r.uniform(10, 60)
    audio = signal.resample_poly(ms.passes_audio(song, passes, c, snr_db=6, live_drums=True),
                                 147, 640).astype(np.float32)
    return audio, c, [(c0, c0 + d, s0 - c0) for c0, s0, d, _ in passes]


def run(audio, length, truth, landmark_gaps):
    clip = m.Clip(path="long", rel="long", duration=length, fps=23.976, has_audio=True)
    m.load_channels = lambda path, layout, limit=None: [("channel 1", audio)]
    m.gap_passes = real_gap if landmark_gaps else (lambda *a, **k: [])
    m.sync_clip(clip, sp.MASTER, m.Settings())
    placed = [p for p in (clip.parts if clip.split else [clip]) if p.status == "placed"]
    wrong = 0
    for p in placed:
        lo, hi = (p.src_in, p.src_out) if clip.split else (0, length)
        tr = [o for a, b, o in truth if min(b, hi) - max(a, lo) > 2]
        if not tr or min(abs(p.offset - o) for o in tr) > 0.03:
            wrong += 1
    covered = sum(any(p.status == "placed" and min(b, p.src_out) - max(a, p.src_in) > 2
                      for p in (clip.parts if clip.split else [])) for a, b, _ in truth)
    return len(placed), covered, wrong


real_gap = m.gap_passes

if __name__ == "__main__":
    n_takes = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    bad = 0
    for seed in range(n_takes):
        audio, length, truth = take(100 + seed, sp.SONG)
        placed, covered, wrong = run(audio, length, truth, landmark_gaps=False)
        print("take %d: %d plays, %d found, %d parts placed, WRONG %d" % (seed, len(truth), covered, placed, wrong))
        bad += wrong
    other = other_song()
    for seed in range(2):
        audio, length, _ = take(200 + seed, other)
        for gaps in (True, False):
            placed, _, _ = run(audio, length, [], landmark_gaps=gaps)
            print("other song take %d (landmark gap search %s): %d placed (must be 0)"
                  % (seed, "on" if gaps else "off", placed))
            bad += placed
    print("wrong placements:", bad)
    sys.exit(1 if bad else 0)
