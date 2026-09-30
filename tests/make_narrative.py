#!/usr/bin/env python3
"""
Build a synthetic narrative shoot: a sound recordist's takes (SOUND/T001.WAV..., BWF with a timecode
start) and two cameras whose scratch audio is the same scene heard from across the room. Some clips
carry timecode (C Cam, jammed to the recorder), the rest have to go by scratch audio; one camera
rolls before and after the recorder; one clip is B-roll with no dialogue.

    python3 make_narrative.py OUT_DIR

Writes OUT_DIR/shoot/ and OUT_DIR/expected.json ({clip: [audio file, seconds into it the clip starts]}).
"""
import json
import os
import subprocess
import sys

import numpy as np
from scipy import signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_synthetic import FS, room, write_wav      # noqa: E402

rng = np.random.default_rng(11)
TC0 = 10 * 3600          # the recorder's first take at 10:00:00:00


def speech(seconds, seed):
    """Dialogue-like sound: syllables of voiced pitch with shifting formants, pauses between phrases."""
    r = np.random.default_rng(seed)
    out = np.zeros(int(seconds * FS))
    t = 0.3
    while t < seconds - 0.5:
        phrase = r.uniform(1.0, 3.5)
        f0 = r.uniform(95, 220)
        end = min(seconds - 0.3, t + phrase)
        while t < end:
            d = r.uniform(0.09, 0.28)
            n = int(d * FS)
            tt = np.arange(n) / FS
            pitch = f0 * (1 + 0.08 * np.sin(2 * np.pi * r.uniform(1, 4) * tt))
            ph = 2 * np.pi * np.cumsum(pitch) / FS
            src = sum(np.sin(k * ph) / k for k in range(1, 12))
            if r.random() < 0.25:                         # a consonant: noise burst
                src = r.standard_normal(n)
            y = np.zeros(n)
            for fc, bw in ((r.uniform(300, 900), 120), (r.uniform(900, 2400), 200), (r.uniform(2400, 3500), 300)):
                b, a = signal.iirpeak(fc, fc / bw, FS)
                y += signal.lfilter(b, a, src)
            y *= np.hanning(n) * r.uniform(0.5, 1.0)
            i = int(t * FS)
            out[i:i + n] += y[:len(out) - i]
            t += d + r.uniform(0.0, 0.06)
        t += r.uniform(0.4, 1.5)
    return out / (np.abs(out).max() + 1e-9) * 0.8


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def clip(path, fps, audio, tc=None, tmp="/tmp/_narr.wav"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_wav(tmp, audio)
    n = len(audio) / FS
    cmd = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=%s:duration=%.3f" % (fps, n), "-i", tmp,
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest"]
    if tc:
        cmd += ["-timecode", tc]
    ff(*cmd, path)


def tc_label(seconds, fps):
    f = int(round(seconds * fps))
    return "%02d:%02d:%02d:%02d" % (f // (3600 * fps), f // (60 * fps) % 60, f // fps % 60, f % fps)


def main(out):
    shoot = os.path.join(out, "shoot")
    sound = os.path.join(shoot, "SOUND")
    os.makedirs(sound, exist_ok=True)
    takes, expected, tc = [], {}, TC0
    for k, dur in enumerate([26, 34, 22, 30], 1):
        x = speech(dur, 100 + k)
        name = "T%03d.WAV" % k
        p = os.path.join(sound, name)
        write_wav("/tmp/_take.wav", x, ch=1)
        ff("-i", "/tmp/_take.wav", "-c:a", "pcm_s24le", "-write_bext", "1",
           "-metadata", "time_reference=%d" % int(tc * FS), p)
        takes.append((name, x, tc))
        tc += dur + 45                                       # time between takes
    # A Cam (no timecode): rolls 1.5-4 s before the recorder, stops 1-3 s after; take 4 missing
    for k, (name, x, t0) in enumerate(takes[:3], 1):
        lead, after = rng.uniform(1.5, 4.0), rng.uniform(1.0, 3.0)
        src = np.concatenate([np.zeros(int(lead * FS)), x, np.zeros(int(after * FS))])
        p = os.path.join(shoot, "A Cam (FX3)", "A001C%03d.MP4" % k)
        clip(p, 24, room(src, snr_db=6, live_drums=False))
        expected[os.path.relpath(p, shoot)] = [name, -lead]
    # B Cam (no timecode): starts inside takes 2 and 4, some seconds in
    for k, ti in enumerate([1, 3], 1):
        name, x, t0 = takes[ti]
        start, length = rng.uniform(3, 8), rng.uniform(12, 18)
        src = x[int(start * FS):int((start + length) * FS)]
        p = os.path.join(shoot, "B Cam (A7S)", "B001C%03d.MP4" % k)
        clip(p, 24, room(src, snr_db=4, live_drums=False))
        expected[os.path.relpath(p, shoot)] = [name, start]
    # C Cam: timecode jammed to the recorder, 25 fps, rolls 2 s before takes 1 and 4
    for k, ti in enumerate([0, 3], 1):
        name, x, t0 = takes[ti]
        src = np.concatenate([np.zeros(2 * FS), x[:int(15 * FS)]])
        p = os.path.join(shoot, "C Cam (Mini LF)", "C001C%03d.mov" % k)
        clip(p, 25, room(src, snr_db=3, live_drums=False), tc=tc_label(t0 - 2.0, 25))
        expected[os.path.relpath(p, shoot)] = [name, -2.0]
    # B-roll: no dialogue at all
    p = os.path.join(shoot, "B Cam (A7S)", "B001C003.MP4")
    clip(p, 24, 0.05 * rng.standard_normal(12 * FS))
    expected[os.path.relpath(p, shoot)] = None
    with open(os.path.join(out, "expected.json"), "w") as fh:
        json.dump(expected, fh, indent=1)
    print("wrote", shoot)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/narr")
