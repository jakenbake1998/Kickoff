#!/usr/bin/env python3
"""White Wolf cases: a faint play under a loud band through a whole take (B003C030), two plays of the
same section in one take (A006C013), and takes with no song at all (must place nothing).

    python3 tests/faint_play.py [N]

Prints, per kind, the seconds of song placed right, placed WRONG (must be 0) and missed."""
import os
import sys
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
MASTER = m.MasterIndex(signal.resample_poly(SONG, 147, 640).astype(np.float32))
KINDS = ["faint", "same-section", "none"]


def take(seed):
    r = np.random.default_rng(seed)
    ms.rng = np.random.default_rng(seed + 1000)
    kind = KINDS[seed % len(KINDS)]
    if kind == "faint":            # one play of most of the song, the band far louder than the playback
        s0 = r.uniform(0, 20)
        d = DUR - s0 - r.uniform(0, 10)
        passes, length, snr = [(r.uniform(5, 20), s0, d)], None, r.uniform(-16, -10)
        length = passes[0][0] + d + r.uniform(5, 20)
    elif kind == "same-section":   # the same stretch of song played twice in one take
        s0 = r.uniform(20, DUR - 60)
        d = r.uniform(35, 55)
        gap = r.uniform(3, 8)
        passes = [(2.0, s0, d), (2.0 + d + gap, s0, d)]
        length, snr = 4.0 + 2 * d + gap, r.uniform(0, 8)
    else:                          # no song: the band rehearsing something else
        passes, length, snr = [], r.uniform(60, 200), 0.0
    kw = dict(snr_db=snr, live_drums=True)
    audio = signal.resample_poly(ms.passes_audio(SONG, passes, length, **kw), 147, 640).astype(np.float32)
    clip = m.Clip(path="take%d" % seed, rel="take%d" % seed, duration=length, fps=23.976, has_audio=True)
    m.load_channels = lambda path, layout, **k: [("channel 1", audio)]
    m.sync_clip(clip, MASTER, m.Settings())
    parts = clip.parts if clip.split else ([m.Part(0.0, length, offset=clip.offset, status=clip.status)]
                                           if clip.status == "placed" else [])
    right = wrong = 0.0
    for p in parts:
        if p.status != "placed" or p.offset is None:
            continue
        for t in np.arange(p.src_in, p.src_out, 1.0):          # each placed second: is its song right?
            inside = [c0 + (t - c) for c, c0, dd in passes if c <= t < c + dd]
            if not inside:
                continue                                        # roll outside the play: not scored
            song_t = p.offset + t
            if any(abs(song_t - x) < 0.1 for x in inside):
                right += 1
            else:
                wrong += 1
    total = sum(dd for _, _, dd in passes)
    return kind, right, wrong, total


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    agg = {k: [0.0, 0.0, 0.0] for k in KINDS}
    with ProcessPoolExecutor() as ex:
        for kind, right, wrong, total in ex.map(take, range(n)):
            a = agg[kind]
            a[0] += right; a[1] += wrong; a[2] += total
    for k, (right, wrong, total) in agg.items():
        print("%-13s song s %6.0f  placed right %6.0f  placed WRONG %4.0f  missed %6.0f"
              % (k, total, right, wrong, max(0.0, total - right - wrong)))
