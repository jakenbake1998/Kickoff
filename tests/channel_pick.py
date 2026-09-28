#!/usr/bin/env python3
"""Channel test: a camera with a bad channel (blown out, loud noise, a steady tone) next to the
scratch mic, like a Mini LF whose ch3 is unusable while ch4 has clean audio. The clip must sync on
the good channel wherever it sits, and a clip with no song on any channel must never be placed.

    python3 tests/channel_pick.py [takes]"""
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

BADS = ["blown", "noise", "tone", "blown_song"]


def bad_channel(kind, n, r, good):
    t = np.arange(n) / m.SR
    if kind == "blown":        # clipped hard: loud crackle and wind, no usable song
        x = np.clip(r.standard_normal(n) * 3 + np.sin(2 * np.pi * 90 * t) * 4, -1, 1)
    elif kind == "noise":
        x = r.standard_normal(n).astype(np.float32) * 0.5
    elif kind == "tone":       # a rounded ~2.2 kHz wave at -22 dBFS, like the Mini LF's ch3
        x = 0.08 * np.tanh(2 * np.sin(2 * np.pi * 2210 * t))
    else:                      # the song is there but pushed far past clipping
        x = np.clip(good * 60 + r.standard_normal(n) * 0.3, -1, 1)
    return x.astype(np.float32)


def take(seed):
    r = np.random.default_rng(seed)
    ms.rng = np.random.default_rng(seed + 5000)
    kind = BADS[seed % len(BADS)]
    song_here = seed % 5 != 4                 # every fifth take: no song on any channel
    s0 = r.uniform(0, sp.DUR - 15)
    length = min(r.uniform(12, 60), sp.DUR - s0)
    kw = dict(snr_db=r.uniform(-2, 8), live_drums=bool(r.integers(0, 2)))
    if song_here:
        mic = ms.passes_audio(sp.SONG, [(0.0, s0, length)], length, **kw)
    else:
        mic = ms.room(np.zeros(int(length * ms.FS)), **kw) + r.standard_normal(int(length * ms.FS)) * 0.05
    good = signal.resample_poly(mic, 147, 640).astype(np.float32) * r.uniform(0.05, 1.0)
    bad = bad_channel(kind, len(good), r, good)
    order = int(r.integers(0, 2))
    chans = [("channel 1", bad), ("channel 2", good)] if order == 0 else [("channel 1", good), ("channel 2", bad)]
    clip = m.Clip(path="take%d" % seed, rel="take%d" % seed, duration=length, fps=23.976, has_audio=True,
                  audio_layout=[1, 1])
    m.load_channels = lambda path, layout: chans
    m.sync_clip(clip, sp.MASTER, m.Settings())
    got = [(p.offset, p.status, p.repeat_alt) for p in clip.parts] if clip.split else \
        [(clip.offset, clip.status, clip.repeat_alt)]
    return seed, kind, song_here, s0, got


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    t0 = time.time()
    right = wrong = missed = neg = neg_placed = copy = 0
    gap = sp.CHORUS[1][0] - sp.CHORUS[0][0]
    with ProcessPoolExecutor(os.cpu_count() or 2) as ex:
        for seed, kind, song_here, s0, got in ex.map(take, range(n)):
            placed = [(o, alt) for o, st, alt in got if st == "placed"]
            if not song_here:
                neg += 1
                neg_placed += bool(placed)
                continue
            if not placed:
                missed += 1
                print("missed", seed, kind)
            for o, alt in placed:
                if abs(o - s0) < 0.03:
                    right += 1
                elif alt is not None and min(abs(o - s0 + k) for k in (-gap, gap)) < 0.03:
                    copy += 1          # placed at the other copy of the pasted chorus, flagged
                elif abs(o - s0) >= 0.15:
                    wrong += 1
                    print("WRONG", seed, kind, o, s0)
    print("takes with song: placed right %d, at a chorus copy (flagged) %d, missed %d, placed WRONG %d; "
          "takes without song: %d, placed %d (must be 0)" % (right, copy, missed, wrong, neg, neg_placed))
    print("%.0f s" % (time.time() - t0))
    sys.exit(1 if wrong or neg_placed else 0)
