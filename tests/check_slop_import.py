#!/usr/bin/env python3
"""Slop Cut from someone's own XML: makes two Premiere-style exports out of a Kickoff project (one
sequence per file, files and nests written in full where first used): the Edit sequence with its
camera nests, and a flat A Cam_Synced. Cuts a Slop Cut from each with --mode slopimport and checks
the original is untouched, the copy has the sequence plus '<name>_Slop Cut' with every take whole,
no more than one take on at a time, and every file and nest defined before it's referenced.

    python3 tests/check_slop_import.py PROJECT.xml
"""
import collections
import copy
import hashlib
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))


def export(root, seq_name, out):
    """One sequence alone, the way Premiere's File > Export > Final Cut Pro XML writes it."""
    full = {s.get("id"): s for s in root.iter("sequence") if s.find("media") is not None}
    files = {f.get("id"): f for f in root.iter("file") if f.find("pathurl") is not None}
    seq = copy.deepcopy(next(s for s in full.values() if s.findtext("name") == seq_name))
    seen = set()

    def inline(el):
        for c in list(el):
            if c.tag in ("file", "sequence") and not len(c) and c.get("id") not in seen:
                src = files.get(c.get("id")) if c.tag == "file" else full.get(c.get("id"))
                if src is not None:
                    seen.add(c.get("id"))
                    d = copy.deepcopy(src)
                    i = list(el).index(c)
                    el.remove(c)
                    el.insert(i, d)
                    inline(d)
                    continue
            elif c.tag in ("file", "sequence") and len(c):
                seen.add(c.get("id"))
            inline(c)
    inline(seq)
    x = ET.Element("xmeml", version="4")
    x.append(seq)
    ET.ElementTree(x).write(out, encoding="UTF-8", xml_declaration=True)


def check(path, base):
    root = ET.parse(path).getroot()
    defined = set()
    for el in root.iter():
        if el.tag in ("file", "sequence") and el.get("id"):
            if len(el):
                defined.add(el.get("id"))
            else:
                assert el.get("id") in defined, "%s %s used before it's defined" % (el.tag, el.get("id"))
    seqs = [s for s in root.iter("sequence") if s.find("media") is not None]
    by_id = {s.get("id"): s for s in seqs}
    slop = [s for s in seqs if s.findtext("name") == base + "_Slop Cut"]
    assert len(slop) == 1, "want one %s_Slop Cut, found %s" % (base, [s.findtext("name") for s in seqs])
    src = next(s for s in seqs if s.findtext("name") == base)

    def takes(seq, off=0, out=None):
        out = collections.Counter() if out is None else out
        for ci in seq.iterfind("media/video/track/clipitem"):
            if ci.find("sequence") is not None:
                takes(by_id[ci.find("sequence").get("id")], off + int(ci.findtext("start")), out)
            elif ci.find("file") is not None:
                out[ci.find("file").get("id")] += int(ci.findtext("end")) - int(ci.findtext("start"))
        return out
    want, got = takes(src), takes(slop[0])
    assert want == got, "takes missing or cut short"
    on = sorted((int(ci.findtext("start")), int(ci.findtext("end"))) for ci in slop[0].iterfind("media/video/track/clipitem")
                if ci.findtext("enabled") == "TRUE")
    assert on, "nothing is switched on"
    for (a, b), (c, d) in zip(on, on[1:]):
        assert c >= b, "two takes on at once at frame %d" % c
    assert not slop[0].findall(".//link"), "links left in the Slop Cut"
    print("  %s: OK, %d takes, %d pieces on" % (os.path.basename(path), len(want), len(on)))


def main(project):
    root = ET.parse(project).getroot()
    names = [s.findtext("name") for s in root.iter("sequence") if s.find("media") is not None]
    edit = next(n for n in names if n.endswith("_Edit"))
    synced = next(n for n in names if n.endswith("_Synced"))
    d = os.path.join(os.path.dirname(os.path.abspath(project)), "slop-import-test")
    os.makedirs(d, exist_ok=True)
    for n in (edit, synced):
        x = os.path.join(d, n + ".xml")
        export(root, n, x)
        before = hashlib.sha1(open(x, "rb").read()).hexdigest()
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "musicsync.py"), "--mode", "slopimport", x],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        assert hashlib.sha1(open(x, "rb").read()).hexdigest() == before, "the original XML changed"
        check(os.path.join(d, n + " - Slop Cut.xml"), n)
    print("Slop Cut from your own XML OK")


if __name__ == "__main__":
    main(sys.argv[1])
