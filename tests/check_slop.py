#!/usr/bin/env python3
"""Checks a Slop Cut in a project XML: it sits next to the Edit sequence, which is unchanged; every
synced take in the camera sequences the Edit nests is in it, whole (cut into pieces, nothing
dropped); no more than one take is on at a time, only while the song plays; and shots run 2 s or
more (8 s at most, unless one take keeps playing because nothing else covers it).

    python3 tests/check_slop.py PROJECT.xml [ORIGINAL.xml]
"""
import collections
import sys
import xml.etree.ElementTree as ET


def main(path, original=None):
    root = ET.parse(path).getroot()
    seqs = [s for s in root.iter("sequence") if s.find("media") is not None]
    by_id = {s.get("id"): s for s in seqs}
    slop = [s for s in seqs if (s.findtext("name") or "").endswith("_Slop Cut")]
    edit = [s for s in seqs if (s.findtext("name") or "").endswith("_Edit")]
    assert len(slop) == 1, "want one Slop Cut, found %d" % len(slop)
    assert len(edit) == 1
    slop, edit = slop[0], edit[0]
    r = slop.find("rate")
    fps = float(r.findtext("timebase")) * (1000 / 1001 if r.findtext("ntsc") == "TRUE" else 1)
    if original:
        o = [s for s in ET.parse(original).getroot().iter("sequence")
             if (s.findtext("name") or "").endswith("_Edit") and s.find("media") is not None][0]
        o.tail = edit.tail = None
        assert ET.tostring(o) == ET.tostring(edit), "the Edit sequence changed"
    song = slop.find("media/audio/track/clipitem")
    s0, s1 = int(song.findtext("start")), int(song.findtext("end"))

    # every take of the nested camera sequences, by (file, in): its length on the timeline
    want = collections.Counter()
    for ci in edit.iterfind("media/video/track/clipitem"):
        nest = by_id[ci.find("sequence").get("id")]
        for it in nest.iterfind("media/video/track/clipitem"):
            if it.find("file") is not None:
                want[it.find("file").get("id")] += int(it.findtext("end")) - int(it.findtext("start"))
    got = collections.Counter()
    on = []
    for tr in slop.findall("media/video/track"):
        pos = -1
        for ci in tr.findall("clipitem"):
            a, b = int(ci.findtext("start")), int(ci.findtext("end"))
            assert a >= pos, "overlap on a track at %d" % a
            assert b > a
            pos = b
            got[ci.find("file").get("id")] += b - a
            if ci.findtext("enabled") == "TRUE":
                on.append((a, b, ci.find("file").get("id"), int(ci.findtext("in"))))
            assert not ci.findall("link"), "a piece still links to clips that aren't there"
    assert got == want, "takes missing or cut short: %s" % {k: (want[k], got[k]) for k in want if want[k] != got[k]}
    on.sort()
    shots = []
    for a, b, f, i in on:          # pieces of one take that follow on, in step, are one shot
        if shots and shots[-1][1] == a and shots[-1][2] == f and abs(shots[-1][3] + (a - shots[-1][0]) - i) <= 1:
            shots[-1][1] = b
        else:
            shots.append([a, b, f, i])
    for (a, b, f, _), nxt in zip(shots, shots[1:] + [None]):
        assert s0 <= a and b <= s1 + 1, "a take is on outside the song"
        if nxt:
            assert nxt[0] >= b, "two takes on at once at %.1f s" % ((nxt[0] - s0) / fps)
        L = (b - a) / fps
        assert L >= 2 - 1e-3, "shot of %.2f s at %.1f s" % (L, (a - s0) / fps)
        if L > 8.05:
            print("  note: a %.1f s shot at %.1f s" % (L, (a - s0) / fps))
    lengths = sorted(round((b - a) / fps, 1) for a, b, _, _ in shots)
    covered = sum(b - a for a, b, _, _ in shots)
    print("Slop Cut OK: %d shots, %d takes used of %d, %.0f%% of the song covered, shot lengths %s" % (
        len(shots), len({f for _, _, f, _ in shots}), len(want), 100.0 * covered / max(1, s1 - s0),
        sorted(set(lengths))))


if __name__ == "__main__":
    main(*sys.argv[1:3])
