#!/usr/bin/env python3
"""
Build a synthetic shoot to self-test musicsync: a 90 s "song" (with a chorus that is a
sample-exact copy, like a pasted chorus in a real mix) and a card dump of clips from three
cameras whose scratch audio is the song played through a speaker into a room: reverb, EQ,
noise, a live drummer, compression, and in one case a 0.1% playback speed error.

    python3 make_synthetic.py OUT_DIR

Writes a shoot folder OUT_DIR/clips/ (camera cards, Music/Song Master.wav, SOUND/ZOOM0001.WAV from an
external recorder) and OUT_DIR/expected.json.
"""
import json
import os
import subprocess
import sys

import numpy as np
from scipy import signal

FS = 48000
rng = np.random.default_rng(7)


def tone(freq, dur, kind="saw", amp=0.2):
    t = np.arange(int(dur * FS)) / FS
    if kind == "sine":
        y = np.sin(2 * np.pi * freq * t)
    else:
        y = 2 * ((t * freq) % 1) - 1
    env = np.minimum(1, t / 0.01) * np.exp(-t * 3)
    return amp * y * env


def drum(kind):
    t = np.arange(int(0.25 * FS)) / FS
    if kind == "kick":
        f = 50 + 100 * np.exp(-t * 30)
        return 0.8 * np.sin(2 * np.pi * np.cumsum(f) / FS) * np.exp(-t * 12)
    n = rng.standard_normal(len(t))
    if kind == "snare":
        return 0.4 * n * np.exp(-t * 20)
    b, a = signal.butter(2, 6000 / (FS / 2), "high")
    return 0.15 * signal.lfilter(b, a, n) * np.exp(-t * 60)


def section(bars, seed, bpm=120):
    r = np.random.default_rng(seed)
    beat = 60 / bpm
    out = np.zeros(int(bars * 4 * beat * FS) + FS)
    roots = r.choice([110, 123.5, 130.8, 146.8, 164.8, 174.6, 196], size=bars)
    scale = np.array([0, 2, 4, 5, 7, 9, 11, 12, 14, 16])
    for b in range(bars):
        for q in range(4):
            i = int((b * 4 + q) * beat * FS)
            d = drum("kick" if q in (0, 2) else "snare")
            out[i:i + len(d)] += d
            for e in range(2):
                h = drum("hat")
                j = i + int(e * beat / 2 * FS)
                out[j:j + len(h)] += h
            bn = tone(roots[b] / 2, beat, "saw", 0.25)
            out[i:i + len(bn)] += bn
        for n in (0, 4, 7):
            c = tone(roots[b] * 2 ** (n / 12), 4 * beat, "saw", 0.06)
            i = int(b * 4 * beat * FS)
            out[i:i + len(c)] += c
        for s in range(8):
            if r.random() < 0.8:
                f = roots[b] * 4 * 2 ** (r.choice(scale) / 12)
                m = tone(f, beat / 2 * r.choice([1, 2]), "sine", 0.18)
                i = int((b * 4 * beat + s * beat / 2) * FS)
                out[i:i + len(m)] += m
    return out[:int(bars * 4 * beat * FS)]


def make_master():
    intro, verse1, chorus, verse2, outro = (section(4, 1), section(8, 2), section(8, 3),
                                            section(8, 4), section(9, 5))
    song = np.concatenate([intro, verse1, chorus, verse2, chorus, outro])   # chorus pasted twice
    song /= np.abs(song).max() * 1.1
    return song, {"chorus1": len(intro) / FS + len(verse1) / FS,
                  "chorus2": (len(intro) + len(verse1) + len(chorus) + len(verse2)) / FS}


def room(x, snr_db=8, speed=1.0, live_drums=True, drr_db=-3, rt60=0.6, drums_db=-3):
    """Speaker playback heard by an on-camera mic: reverb, band-limiting, a drummer, noise, AGC."""
    if speed != 1.0:     # playback at the wrong speed: resample then treat as FS
        x = signal.resample(x, int(len(x) / speed))
    t = np.arange(int(rt60 * 1.5 * FS)) / FS
    tail = rng.standard_normal(len(t)) * np.exp(-6.9 * t / rt60)
    tail[: int(0.004 * FS)] = 0
    tail *= 10 ** (-drr_db / 20) / np.sqrt(np.sum(tail ** 2))
    ir = tail
    ir[0] += 1.0
    for d, g in ((0.007, 0.5), (0.013, 0.35), (0.021, 0.3)):     # early reflections
        ir[int(d * FS)] += g
    y = signal.fftconvolve(x, ir)[:len(x)]
    b, a = signal.butter(2, [180 / (FS / 2), 6500 / (FS / 2)], "band")
    y = signal.lfilter(b, a, y)
    y /= np.sqrt(np.mean(y ** 2)) + 1e-9
    if live_drums:           # drummer playing along, slightly loose
        kit = np.zeros(len(y))
        beat = 0.5
        for k in range(int(len(y) / FS / beat)):
            i = int((k * beat + rng.normal(0, 0.01)) * FS)
            d = drum("kick" if k % 2 == 0 else "snare")
            if 0 <= i < len(y) - len(d):
                kit[i:i + len(d)] += d
        kit = signal.fftconvolve(kit, ir)[:len(y)]
        y += kit / (np.sqrt(np.mean(kit ** 2)) + 1e-9) * 10 ** (drums_db / 20)
    noise = rng.standard_normal(len(y))
    noise = signal.lfilter([1], [1, -0.97], noise)          # rumble-heavy crowd/room noise
    noise /= np.sqrt(np.mean(noise ** 2))
    y = y + noise * 10 ** (-snr_db / 20)
    y = np.tanh(y * 0.3) / 0.3                               # camera limiter, touches peaks only
    return 0.5 * y / np.abs(y).max()


def camera_audio(song, offset, length, **kw):
    """Scratch audio for a clip that starts at song time `offset` (may be negative)."""
    n = int(length * FS)
    src = np.zeros(n)
    s0 = int(round(offset * FS))
    a, b = max(0, s0), min(len(song), s0 + n)
    if b > a:
        src[a - s0:b - s0] = song[a:b]
    speed = kw.pop("speed", 1.0)
    if speed != 1.0:   # generate from a sped-up song so drift accumulates across the clip
        sped = signal.resample(song, int(len(song) / speed))
        src = np.zeros(n)
        s0 = int(round(offset / speed * FS))
        a, b = max(0, s0), min(len(sped), s0 + n)
        src[a - s0:b - s0] = sped[a:b]
    stretch = kw.pop("stretch", 1.0)
    if stretch != 1.0:  # played k times faster with pitch kept (time-stretch, like VLC / phones)
        seg = np.zeros(int(length * stretch * FS) + FS)
        s0 = int(round(offset * FS))
        a, b = max(0, s0), min(len(song), s0 + len(seg))
        seg[a - s0:b - s0] = song[a:b]
        src = time_stretch(seg, stretch)[:n]
        src = np.pad(src, (0, n - len(src)))
    return room(src, **kw)


def time_stretch(x, rate, n_fft=2048, hop=512):
    """Phase-vocoder time stretch: `rate` times faster, same pitch."""
    win = np.hanning(n_fft)
    frames = np.array([np.fft.rfft(win * x[i:i + n_fft]) for i in range(0, len(x) - n_fft, hop)])
    steps = np.arange(0, len(frames) - 1, rate)
    omega = 2 * np.pi * hop * np.arange(n_fft // 2 + 1) / n_fft
    phase = np.angle(frames[0])
    out = np.zeros(len(steps) * hop + n_fft)
    for j, st in enumerate(steps):
        i = int(st)
        frac = st - i
        mag = (1 - frac) * np.abs(frames[i]) + frac * np.abs(frames[i + 1])
        out[j * hop:j * hop + n_fft] += win * np.fft.irfft(mag * np.exp(1j * phase), n_fft)
        dphi = np.angle(frames[i + 1]) - np.angle(frames[i]) - omega
        dphi -= 2 * np.pi * np.round(dphi / (2 * np.pi))
        phase += omega + dphi
    return out / (np.abs(out).max() + 1e-9) * np.abs(x).max()


def write_wav(path, x, ch=2):
    import wave
    x = np.clip(x, -1, 1)
    data = (x * 32767).astype("<i2")
    if ch == 2:
        data = np.repeat(data[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes(data.tobytes())


def ff(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def burn_in(name, song_offset, speed=1.0):
    """Big on-screen text: clip name and the SONG time of each frame, so synced clips from
    different cameras show the same numbers when they line up."""
    font = "fontsize=40:fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=8"
    txt = "drawtext=text='%s':x=20:y=20:%s" % (name, font)
    if song_offset is None:
        return txt + ",drawtext=text='NOT PLAYBACK':x=20:y=80:%s" % font
    if speed != 1.0:   # song runs `speed` x faster than the clip's own clock
        return txt + (",drawtext=text='SONG %%{eif\\:%s+%s*t\\:d}.%%{eif\\:mod((%s+%s*t)*10\\,10)\\:d}s "
                      "(%gx)':x=20:y=80:%s" % (song_offset, speed, song_offset, speed, speed, font))
    return txt + (",drawtext=text='SONG %%{pts\\:hms\\:%s}':x=20:y=80:%s" % (song_offset, font))


def make_clip(path, fps, length, audio=None, acodec="aac", meta=(), size="640x360", tmp=None,
              song_offset=None, song_speed=1.0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    vf = burn_in(os.path.basename(path), song_offset, song_speed)
    cmd = ["-f", "lavfi", "-i", "testsrc2=size=%s:rate=%s:duration=%s,%s" % (size, fps, length, vf)]
    if audio is not None:
        write_wav(tmp, audio)
        cmd += ["-i", tmp]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p"]
    if audio is not None:
        cmd += ["-c:a", acodec] + (["-b:a", "128k"] if acodec == "aac" else [])
        cmd += ["-shortest"]
    for k, v in meta:
        cmd += ["-metadata", "%s=%s" % (k, v)]
    if meta:
        cmd += ["-movflags", "use_metadata_tags"]
    ff(*cmd, path)


def sony_xml(path, capture="23.98p", fmt="23.98p", serial="5012345"):
    base = os.path.splitext(path)[0]
    with open(base + "M01.XML", "w") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                 '<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.20">\n'
                 '  <VideoFormat>\n'
                 '    <VideoFrame videoCodec="AVC_3840_2160_HP@L51" captureFps="%s" formatFps="%s"/>\n'
                 '  </VideoFormat>\n'
                 '  <Device manufacturer="Sony" modelName="ILME-FX3" serialNo="%s"/>\n'
                 '</NonRealTimeMeta>\n' % (capture, fmt, serial))


def main(out):
    os.makedirs(out, exist_ok=True)
    tmp = os.path.join(out, "_tmp.wav")
    song, marks = make_master()
    clips = os.path.join(out, "clips")
    os.makedirs(os.path.join(clips, "Music"), exist_ok=True)
    write_wav(os.path.join(clips, "Music", "Song Master.wav"), song)
    os.makedirs(os.path.join(clips, "SOUND"), exist_ok=True)
    write_wav(os.path.join(clips, "SOUND", "ZOOM0001.WAV"), camera_audio(song, -1.0, 40, snr_db=15))
    os.makedirs(os.path.join(clips, "SFX"), exist_ok=True)
    write_wav(os.path.join(clips, "SFX", "Whoosh 01.wav"), 0.3 * rng.standard_normal(FS) * np.exp(-np.arange(FS) / FS * 4))
    sony = os.path.join(clips, "A_CAM", "PRIVATE", "M4ROOT", "CLIP")
    arri = os.path.join(clips, "B_CAM")
    gopro = os.path.join(clips, "C_CAM", "DCIM", "100GOPRO")
    arri_meta = [("com.apple.quicktime.make", "ARRI"), ("com.apple.quicktime.model", "ALEXA Mini LF")]
    gopro_meta = [("firmware", "HD9.01.01.60.00"), ("encoder", "GoPro AVC encoder")]
    exp = {}

    def add(path, fps, length, offset=None, expect="placed", sidecar=None, **kw):
        audio = kw.pop("audio", "song")
        meta = kw.pop("meta", ())
        acodec = kw.pop("acodec", "aac")
        size = kw.pop("size", "640x360")
        song_speed = kw.get("stretch", 1.0) if kw.get("stretch", 1.0) != 1.0 else \
            (kw.get("speed", 1.0) if kw.get("speed", 1.0) >= 1.5 else 1.0)
        if audio == "song":
            a = camera_audio(song, offset, length, **kw)
        elif audio == "other":
            a = room(section(int(length / 2) + 1, 99)[:int(length * FS)], snr_db=10)
        elif audio == "silent":
            a = np.zeros(int(length * FS))
        else:
            a = None
        make_clip(path, fps, length, a, acodec=acodec, meta=meta, tmp=tmp, size=size, song_speed=song_speed,
                  song_offset=offset if audio in ("song", None) else None)
        if sidecar is not None:
            sony_xml(path, **sidecar)
        exp[os.path.relpath(path, clips)] = {"expect": expect, "offset": offset}

    # A cam: Sony FX3, identity only in the sidecar XML (as on real cards)
    add(os.path.join(sony, "C0001.MP4"), "24000/1001", 30, 4.0, sidecar={})
    add(os.path.join(sony, "C0002.MP4"), "24000/1001", 25, -3.0, sidecar={})
    add(os.path.join(sony, "C0003.MP4"), "24000/1001", 20, expect="no audio track", audio=None,
        sidecar={"capture": "120p"}, size="1280x720")     # S&Q slow motion: no audio, bigger frame
    add(os.path.join(sony, "C0004.MP4"), "24000/1001", 20, expect="no match to song", audio="other",
        sidecar={})
    add(os.path.join(sony, "C0005.MP4"), "24000/1001", 70, 17.25, sidecar={})
    # B cam: ARRI, PCM audio in MOV, ARRI-style names carry the camera letter
    add(os.path.join(arri, "B001C001_260927_R1AB.mov"), "24000/1001", 32, 4.0, meta=arri_meta,
        acodec="pcm_s16le")
    add(os.path.join(arri, "B001C002_260927_R1AB.mov"), "24000/1001", 45, 41.37, meta=arri_meta,
        acodec="pcm_s16le", speed=1.001)
    add(os.path.join(arri, "B001C003_260927_R1AB.mov"), "24000/1001", 12, marks["chorus2"] + 2.0,
        expect="ambiguous", meta=arri_meta, acodec="pcm_s16le", live_drums=False)
    add(os.path.join(arri, "B001C004_260927_R1AB.mov"), "24000/1001", 15, expect="audio track is silent",
        audio="silent", meta=arri_meta, acodec="pcm_s16le")
    # C cam: GoPro, one normal-speed take and one 60p take that still has scratch audio (must sync)
    add(os.path.join(gopro, "GX010001.MP4"), "30000/1001", 30, 55.5, meta=gopro_meta, snr_db=4)
    add(os.path.join(gopro, "GX010002.MP4"), "60000/1001", 15, 42.0, meta=gopro_meta, size="480x270")
    # Slow-motion takes with the song played sped up on set, so lips sync once slowed down
    add(os.path.join(sony, "C0006.MP4"), "48000/1001", 7, 41.0, sidecar={}, speed=2.0)        # varispeed
    add(os.path.join(gopro, "GX010003.MP4"), "60000/1001", 7, 70.0, meta=gopro_meta,
        stretch=2.5)                                                                          # pitch kept
    with open(os.path.join(arri, "B001C005_260927_R1AB.R3D"), "wb") as fh:   # unreadable raw
        fh.write(os.urandom(4096))
    exp["B_CAM/B001C005_260927_R1AB.R3D"] = {"expect": "unreadable file", "offset": None}
    os.remove(tmp)
    with open(os.path.join(out, "expected.json"), "w") as fh:
        json.dump({"clips": exp, "song_marks": marks}, fh, indent=2)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "synthetic")
