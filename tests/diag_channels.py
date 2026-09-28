#!/usr/bin/env python3
"""Why a clip didn't sync, channel by channel: for each audio channel, its level, whether it reads as
timecode, the landmark match (aligned / runner-up / chance, confidence, song offset) and the whole-clip
phase peak (offset, ratio). Then the full sync on each channel on its own.

    python3 tests/diag_channels.py SONG.wav CLIP [CLIP ...]"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE)]
import numpy as np            # noqa: E402
import musicsync as m         # noqa: E402

song, clips = sys.argv[1], sys.argv[2:]
master = m.MasterIndex(m.load_audio(song) if hasattr(m, "load_audio") else m._decode_channels(song, [1])[0][1])
for path in clips:
    c = m.Clip(path=path, rel=os.path.basename(path))
    m.probe(c)
    print("\n==", c.rel, "layout", c.audio_layout, "%.1f s" % c.duration)
    chans = m._decode_channels(path, c.audio_layout)
    for label, x in chans:
        rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        clipped = float(np.mean(np.abs(x) > 0.98)) if len(x) else 0.0
        tc = m.is_timecode(x)
        h, t = m.landmarks(*m.find_peaks(x))
        e = m.evaluate(master, h, t)
        o, ratio = m.phase_search(x, master, 0.0, len(x) / m.SR)
        print("  %-22s rms %6.1f dBFS  peak %5.2f  clipped %4.1f%%  timecode %-5s  landmarks %s  phase %s"
              % (label, 20 * np.log10(rms + 1e-12), peak, 100 * clipped, tc,
                 "A%d R%d N%.0f conf %.1f at %.2fs" % (e["A"], e["R"], e["N"], e["conf"], e["offset"]) if e else "none",
                 "%.2fx at %.2fs" % (ratio, o) if o is not None else "-"))
    for i, (label, x) in enumerate(chans):
        cc = m.Clip(path=path, rel=c.rel)
        m.probe(cc)
        orig = m.load_channels
        m.load_channels = lambda p, lay, limit=None, x=x, label=label: [(label, x)]
        try:
            m.sync_clip(cc, master, m.Settings())
        finally:
            m.load_channels = orig
        print("  sync on %-18s -> %s %s %s" % (label, cc.status or "not placed",
              "%.3f" % cc.offset if cc.offset is not None else "", "; ".join(cc.reasons + cc.notes)[:160]))
