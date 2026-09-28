#!/usr/bin/env python3
"""Where along a stretch of a take the song can be heard, and at which offset: 10 s windows every 5 s,
each searched against the whole song by phase correlation (read-only; prints a table).

    python3 tests/diag_parts.py SONG CLIP START END [HAND_OFFSET] [KICKOFF_OFFSET] [...more START END HAND KICK]

START/END are clip seconds, offsets are song time of the clip's first frame (the report's offset_seconds).
A window's ratio >= 1.35 means its peak is clear; 'hand'/'kick' marks a peak within a frame of either."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(HERE)]
import musicsync as m   # noqa: E402

song, path, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
master = m.MasterIndex(m.load_audio(song))
clip = m.Clip(path=path, rel=os.path.basename(path))
m.probe(clip)
chans = [(lab, x) for lab, x in m.load_channels(path, clip.audio_layout)
         if len(x) and not m.is_timecode(x)]
lab, xs = max(chans, key=lambda c: float((c[1] ** 2).mean()))
aoff = clip.audio_offset
fr = 1.0 / (clip.fps or 29.97)
print("%s: %.1f s, fps %s, audio offset %.3f, using %s" % (clip.rel, clip.duration, clip.fps, aoff, lab))
while len(rest) >= 2:
    a, b = float(rest[0]), float(rest[1])
    hand = float(rest[2]) if len(rest) > 2 and rest[2] != "-" else None
    kick = float(rest[3]) if len(rest) > 3 and rest[3] != "-" else None
    rest = rest[4:]
    print("\n== %.1f-%.1f  hand %s  kickoff %s" % (a, b, hand, kick))
    if hand is not None:
        print("   phase_check over the stretch: hand %.2f  kickoff %s" % (
            m.phase_check(xs, master, hand + aoff, a - aoff, b - aoff),
            "%.2f" % m.phase_check(xs, master, kick + aoff, a - aoff, b - aoff) if kick is not None else "-"))
    t = max(0.0, a - 40)
    while t + 10 <= min(b + 40, len(xs) / m.SR + aoff):
        o, r = m.phase_search(xs, master, t - aoff, t + 10 - aoff)
        ov = o - aoff
        tag = []
        if hand is not None and abs(ov - hand) < fr:
            tag.append("hand %+.2ff" % ((ov - hand) / fr))
        if kick is not None and abs(ov - kick) < fr:
            tag.append("kick %+.2ff" % ((ov - kick) / fr))
        if hand is not None and not tag and abs(ov - hand) < 0.5:
            tag.append("near hand %+.1ff" % ((ov - hand) / fr))
        print("  %7.1f-%7.1f  offset %9.3f  song %6.1f  ratio %5.2f %s %s" % (
            t, t + 10, ov, ov + t, r, "*" if r >= m.PHASE_AGREE else " ", " ".join(tag)))
        t += 5
