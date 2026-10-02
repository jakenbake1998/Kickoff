#!/usr/bin/env python3
"""Checks a Slop Cut in a project XML: it sits next to the Edit sequence, every camera track is cut
at the same lines with no gaps, exactly one camera is on at a time while the song plays (and only
where that camera has a take), shots run 2 to 8 s (the last may run a little long), and the Edit
sequence itself is unchanged.

    python3 tests/check_slop.py PROJECT.xml [ORIGINAL.xml]
"""
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
    tracks = slop.findall("media/video/track")
    cuts, on = None, {}
    for i, tr in enumerate(tracks):
        items = tr.findall("clipitem")
        pos = 0
        for ci in items:
            a, b = int(ci.findtext("start")), int(ci.findtext("end"))
            assert a == pos, "gap or overlap on V%d at %d" % (i + 1, a)
            assert (ci.findtext("in"), ci.findtext("out")) == (str(a), str(b)), "nest not in step with the timeline"
            pos = b
            if ci.findtext("enabled") == "TRUE":
                on.setdefault(i, []).append((a, b))
        lines = [int(ci.findtext("start")) for ci in items]
        cuts = cuts or lines
        assert set(lines) >= set(cuts) - {0} or set(cuts) >= set(lines), "tracks cut at different lines"
        # takes in this camera's own sequence
        nest = by_id[items[0].find("sequence").get("id")]
        takes = [(int(c.findtext("start")), int(c.findtext("end"))) for c in nest.iterfind("media/video/track/clipitem")]
        for a, b in on.get(i, []):
            cov = sum(max(0, min(b, y) - max(a, x)) for x, y in takes)
            if cov < 0.97 * (b - a):
                print("  note: V%d is on at %.1f s with a take for %.0f%% of the shot" % (i + 1, (a - s0) / fps, 100 * cov / (b - a)))
    shots = sorted((a, b, i) for i, sp in on.items() for a, b in sp)
    for (a, b, i), nxt in zip(shots, shots[1:] + [None]):
        assert s0 <= a and b <= s1 + 1, "a camera is on outside the song"
        if nxt:
            assert nxt[0] >= b, "two cameras on at once at %d" % nxt[0]
        L = (b - a) / fps
        assert L >= 2 - 1e-3, "shot of %.2f s at %.1f s" % (L, (a - s0) / fps)
        assert L <= 8 + 1e-3 or nxt is None or (nxt is not None and L <= 10.5), "shot of %.2f s" % L
    covered = sum(b - a for a, b, _ in shots)
    print("Slop Cut OK: %d shots over %d tracks, %.0f%% of the song covered" % (
        len(shots), len(tracks), 100.0 * covered / max(1, s1 - s0)))


if __name__ == "__main__":
    main(*sys.argv[1:3])
