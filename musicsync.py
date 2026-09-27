#!/usr/bin/env python3
"""
musicsync - batch waveform sync for music video footage.

Finds where each camera clip sits in a master song using landmark audio
fingerprinting of the clip's scratch audio, then writes one legacy Final Cut
Pro XML (XMEML) sequence per camera, one video track per clip, plus a CSV and
Markdown report covering every clip in the folder.

    python3 musicsync.py MASTER.wav CLIPS_DIR -o OUT_DIR

Needs ffmpeg + ffprobe on PATH and Python 3.9+ with numpy and scipy.
"""

import argparse
import collections
import concurrent.futures as cf
import csv
import datetime
import fractions
import json
import math
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import ndimage, signal

VERSION = "0.1.0"

# ---------------------------------------------------------------- constants

SR = 11025                 # analysis sample rate
N_FFT = 512
HOP = 256                  # 23.2 ms per fingerprint frame
FRAME_S = HOP / SR
MAX_BIN = 232              # ~5 kHz; camera mics and small speakers roll off above
PEAK_F_NEIGH = 15          # local-max neighbourhood (bins each side)
PEAK_T_NEIGH = 7           # local-max neighbourhood (frames each side)
PEAKS_PER_SEC = 30
FANOUT = 12
FREQ_Q = 2                 # hash frequencies in 2-bin steps (tolerates EQ / pitch smear)
DT_TOL = 2                 # query also matches anchor->target gaps +-2 frames (reverb smear)
PAIR_DT = (2, 63)          # frames between anchor and target
PAIR_DF = 63               # max bin distance anchor -> target

MEDIA_EXT = {".mov", ".mp4", ".mxf", ".m4v", ".mts", ".m2ts", ".avi", ".mkv",
             ".r3d", ".braw", ".insv", ".360", ".wmv", ".webm"}
AUDIO_EXT = {".wav", ".aif", ".aiff", ".bwf", ".mp3", ".m4a", ".flac", ".aac", ".caf"}
UNREADABLE_EXT = {".r3d", ".braw"}   # ffmpeg cannot decode these containers
SKIP_DIRS = {"SUB", "THMBNL", "GENERAL", "AVF_INFO", "CACHE", "THMB"}

NTSC_RATES = {23.976: 24, 29.97: 30, 47.952: 48, 59.94: 60, 119.88: 120}

REASON_UNREADABLE = "unreadable file"
REASON_NO_AUDIO = "no audio track"
REASON_SILENT = "audio track is silent"
REASON_HFR = "high frame rate (over --max-fps)"
REASON_SQ = "slow motion (S&Q, audio not real time)"
REASON_NO_MATCH = "no match to song"
REASON_LOW_CONF = "confidence below threshold"
REASON_AMBIGUOUS = "ambiguous match (repeated section of song)"


# ---------------------------------------------------------------- helpers

def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def parse_rate(s):
    if not s or s in ("0/0", "0"):
        return None
    if "/" in s:
        n, d = s.split("/")
        n, d = float(n), float(d)
        return n / d if d else None
    try:
        return float(s)
    except ValueError:
        return None


def snap_fps(fps):
    """Snap a measured rate to the nearest standard rate."""
    if not fps:
        return None
    std = [23.976, 24, 25, 29.97, 30, 47.952, 48, 50, 59.94, 60, 72, 90, 96,
           100, 119.88, 120, 150, 180, 200, 240]
    best = min(std, key=lambda s: abs(s - fps))
    return best if abs(best - fps) / fps < 0.004 else round(fps, 3)


def rate_xml(fps):
    """(timebase, ntsc) for an XMEML <rate>."""
    for ntsc, base in NTSC_RATES.items():
        if abs(fps - ntsc) < 0.01:
            return base, True
    return int(round(fps)), False


def fmt_tc(seconds, fps):
    """Signed HH:MM:SS:FF (non-drop) for a duration in seconds."""
    frames = int(round(seconds * fps))
    return fmt_frames(frames, fps)


def fmt_frames(frames, fps):
    """Non-drop timecode label for a frame count (23.976 counts 24 labels per second)."""
    base, _ = rate_xml(fps)
    neg = frames < 0
    frames = abs(frames)
    ff = frames % base
    s = frames // base
    tc = "%02d:%02d:%02d:%02d" % (s // 3600, (s // 60) % 60, s % 60, ff)
    return ("-" if neg else "") + tc


def path_to_url(path, path_maps):
    p = os.path.abspath(path)
    for old, new in path_maps:
        if p.startswith(old):
            p = new + p[len(old):]
            break
    p = p.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):             # Windows drive, Premiere style
        return "file://localhost/" + p[0] + "%3a" + urllib.parse.quote(p[2:])
    return "file://localhost" + urllib.parse.quote(p)


# ---------------------------------------------------------------- probing

@dataclass
class Clip:
    path: str
    rel: str
    top_folder: str = ""
    readable: bool = True
    probe_error: str = ""
    duration: float = 0.0
    fps: Optional[float] = None
    capture_fps: Optional[float] = None
    width: int = 0
    height: int = 0
    vcodec: str = ""
    has_audio: bool = False
    audio_channels: int = 0
    audio_rate: int = 48000
    audio_offset: float = 0.0     # audio stream start minus video stream start
    make: str = ""
    model: str = ""
    serial: str = ""
    reel_letter: str = ""
    timecode: str = ""
    # results
    camera_key: str = ""
    camera: str = ""
    track: Optional[int] = None
    status: str = ""              # "placed" / "not placed"
    reasons: list = field(default_factory=list)
    offset: Optional[float] = None       # song time (s) of the clip's first video frame
    confidence: Optional[float] = None
    aligned: int = 0
    runner_up: int = 0
    runner_up_offset: Optional[float] = None
    drift_ms: Optional[float] = None
    refine: str = ""
    speed: float = 1.0                   # song playback speed on set (2.0 = played at 2x)
    speed_mode: str = ""
    seq_start_frame: Optional[int] = None
    notes: list = field(default_factory=list)


BRANDS = [("gopro", "GoPro"), ("dji", "DJI"), ("arri", "ARRI"), ("alexa", "ARRI"),
          ("red digital", "RED"), ("sony", "Sony"), ("canon", "Canon"),
          ("panasonic", "Panasonic"), ("blackmagic", "Blackmagic"),
          ("fujifilm", "Fujifilm"), ("nikon", "Nikon"), ("apple", "Apple"),
          ("insta360", "Insta360"), ("z cam", "Z CAM")]


def sony_sidecar(path):
    """Sony cameras write C0001M01.XML next to C0001.MP4 with model/serial/capture fps."""
    base, _ = os.path.splitext(path)
    for cand in (base + "M01.XML", base + "M01.xml"):
        if os.path.exists(cand):
            try:
                root = ET.parse(cand).getroot()
            except ET.ParseError:
                return {}
            out = {}
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if tag == "Device":
                    out["make"] = el.get("manufacturer", "")
                    out["model"] = el.get("modelName", "")
                    out["serial"] = el.get("serialNo", "")
                elif tag == "VideoFrame":
                    cap = el.get("captureFps", "")
                    m = re.match(r"([\d.]+)", cap)
                    if m:
                        out["capture_fps"] = float(m.group(1))
            return out
    return {}


def probe(clip: Clip):
    ext = os.path.splitext(clip.path)[1].lower()
    r = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", clip.path])
    if r.returncode != 0:
        clip.readable = False
        clip.probe_error = (r.stderr.decode(errors="replace").strip().splitlines() or ["ffprobe failed"])[-1]
        if ext in UNREADABLE_EXT:
            clip.probe_error = "%s is not readable by ffmpeg; sync its proxy or a transcode" % ext.upper()
        return
    info = json.loads(r.stdout or b"{}")
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    clip.duration = float(fmt.get("duration") or 0)
    tags = {}
    for k, v in (fmt.get("tags") or {}).items():
        tags[k.lower()] = v

    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None and a is None:
        clip.readable = False
        clip.probe_error = "no video or audio streams"
        return
    vstart = 0.0
    if v is not None:
        clip.fps = snap_fps(parse_rate(v.get("avg_frame_rate")) or parse_rate(v.get("r_frame_rate")))
        rf = snap_fps(parse_rate(v.get("r_frame_rate")))
        if rf and (not clip.fps or abs(rf - clip.fps) > 0.5) and rf < 1000:
            clip.fps = rf
        clip.width = int(v.get("width") or 0)
        clip.height = int(v.get("height") or 0)
        clip.vcodec = v.get("codec_name") or "unknown"
        vstart = float(v.get("start_time") or 0)
        if v.get("duration"):
            clip.duration = float(v["duration"])
        for k, val in (v.get("tags") or {}).items():
            tags.setdefault(k.lower(), val)
    if a is not None:
        clip.has_audio = True
        clip.audio_channels = int(a.get("channels") or 1)
        clip.audio_rate = int(a.get("sample_rate") or 48000)
        clip.audio_offset = float(a.get("start_time") or 0) - vstart
        for k, val in (a.get("tags") or {}).items():
            tags.setdefault("audio_" + k.lower(), val)
    clip.timecode = tags.get("timecode", "")

    # camera identity from container metadata (manufacturer agnostic)
    model_keys = ["com.apple.quicktime.model", "model", "com.android.model", "product_name",
                  "camera_model", "com.arri.camera.cameramodel", "com.red.camera.model"]
    make_keys = ["com.apple.quicktime.make", "make", "company_name", "manufacturer",
                 "com.android.manufacturer"]
    serial_keys = ["com.arri.camera.cameraserialnumber", "camera_serial_number", "serial_number",
                   "com.apple.quicktime.camera.identifier", "cameraserialnumber"]
    for k in model_keys:
        if tags.get(k):
            clip.model = tags[k].strip()
            break
    for k in make_keys:
        if tags.get(k):
            clip.make = tags[k].strip()
            break
    for k in serial_keys:
        if tags.get(k):
            clip.serial = tags[k].strip()
            break
    fw = tags.get("firmware", "")
    if not clip.model and re.match(r"^(HD\d+|H\d\d)\.", fw):          # GoPro firmware string
        clip.model = "GoPro " + fw.split(".")[0]
    if not clip.model:
        blob = " ".join(str(x) for x in tags.values()).lower()
        for needle, brand in BRANDS:
            if needle in blob:
                clip.model = brand
                if brand == "GoPro" and tags.get("firmware"):
                    clip.model = "GoPro " + tags["firmware"].split(".")[0]
                break

    side = sony_sidecar(clip.path)
    if side:
        clip.make = side.get("make") or clip.make
        clip.model = side.get("model") or clip.model
        clip.serial = side.get("serial") or clip.serial
        clip.capture_fps = side.get("capture_fps")
    if clip.make and clip.model and not clip.model.lower().startswith(clip.make.lower()):
        if clip.make.lower() not in ("sony",):   # Sony model names are self-explanatory
            clip.model = "%s %s" % (clip.make, clip.model)

    # ARRI / RED / Blackmagic style names: A001C003_..., A001_C003_... -> camera letter A
    m = re.match(r"^([A-Z])\d{3}_?C\d{3}", os.path.basename(clip.path))
    if m:
        clip.reel_letter = m.group(1)


def find_audio(folder, skip_dirs=()):
    skip = {os.path.abspath(d) for d in skip_dirs}
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.upper() not in SKIP_DIRS
                         and os.path.abspath(os.path.join(root, d)) not in skip)
        out += [os.path.join(root, f) for f in sorted(files)
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in AUDIO_EXT]
    return out


def pick_master(folder, audio_files):
    """Guess the master song in a dropped folder: an audio file named or filed as music/master/song."""
    if len(audio_files) == 1:
        return audio_files[0]
    hint = re.compile(r"music|master|song|track|mix|playback", re.I)
    cands = [p for p in audio_files if hint.search(os.path.relpath(p, folder))]
    if len(cands) == 1:
        return cands[0]
    return None


def probe_audio(path):
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    info = json.loads(r.stdout or b"{}") if r.returncode == 0 else {}
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    return float((info.get("format") or {}).get("duration") or 0), int(a.get("channels") or 2), \
        int(a.get("sample_rate") or 48000)


def find_clips(clips_dir, master_path, skip_dirs=()):
    master_abs = os.path.abspath(master_path)
    skip = {os.path.abspath(d) for d in skip_dirs}
    out = []
    for root, dirs, files in os.walk(clips_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d.upper() not in SKIP_DIRS
                         and os.path.abspath(os.path.join(root, d)) not in skip)
        for f in sorted(files):
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() not in MEDIA_EXT:
                continue
            p = os.path.join(root, f)
            if os.path.abspath(p) == master_abs:
                continue
            rel = os.path.relpath(p, clips_dir)
            parts = rel.replace("\\", "/").split("/")
            out.append(Clip(path=p, rel=rel, top_folder=parts[0] if len(parts) > 1 else ""))
    return out


# ---------------------------------------------------------------- audio + fingerprints

def load_audio(path, stream="a:0"):
    r = run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:" + stream,
             "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-acodec", "pcm_s16le", "-"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:])
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def spectrogram(x):
    if len(x) < N_FFT:
        x = np.pad(x, (0, N_FFT - len(x)))
    _, _, Z = signal.stft(x, fs=SR, window="hann", nperseg=N_FFT, noverlap=N_FFT - HOP,
                          boundary=None, padded=False)
    return np.abs(Z[:MAX_BIN])


def find_peaks(x, t_neigh=PEAK_T_NEIGH, per_sec=PEAKS_PER_SEC):
    S = spectrogram(x)
    L = np.log(np.maximum(S, 1e-6))
    L = L - L.mean()
    # high-pass along time: removes steady hum / room tone, keeps musical onsets
    L = signal.lfilter([1, -1], [1, -0.98], L, axis=1)
    mx = ndimage.maximum_filter(L, size=(2 * PEAK_F_NEIGH + 1, 2 * t_neigh + 1), mode="constant",
                                cval=-np.inf)
    f, t = np.nonzero((L == mx) & (L > 0))
    vals = L[f, t]
    # keep the strongest PEAKS_PER_SEC peaks in each second
    sec = (t * FRAME_S).astype(int)
    order = np.lexsort((-vals, sec))
    f, t, sec = f[order], t[order], sec[order]
    rank = np.arange(len(sec)) - np.searchsorted(sec, sec, side="left")
    keep = rank < per_sec
    f, t = f[keep], t[keep]
    order = np.lexsort((f, t))
    return t[order], f[order]


def landmarks(t, f):
    """Pair peaks into (hash, anchor_time)."""
    hashes, times = [], []
    n = len(t)
    for i in range(n):
        j0 = np.searchsorted(t, t[i] + PAIR_DT[0], side="left")
        j1 = np.searchsorted(t, t[i] + PAIR_DT[1], side="right")
        if j0 >= j1:
            continue
        cand = np.arange(j0, j1)
        df = f[cand] - f[i]
        cand = cand[np.abs(df) <= PAIR_DF][:FANOUT]
        if not len(cand):
            continue
        dt = t[cand] - t[i]
        fa, fb = f[i] // FREQ_Q, f[cand] // FREQ_Q
        h = (np.int64(fa) << 13) | ((fb - fa + 64).astype(np.int64) << 6) | dt.astype(np.int64)
        hashes.append(h)
        times.append(np.full(len(cand), t[i], dtype=np.int64))
    if not hashes:
        return np.zeros(0, np.int64), np.zeros(0, np.int64)
    return np.concatenate(hashes), np.concatenate(times)


class MasterIndex:
    def __init__(self, audio):
        self.audio = audio
        self.duration = len(audio) / SR
        h, t = landmarks(*find_peaks(audio))
        order = np.argsort(h, kind="stable")
        self.h, self.t = h[order], t[order]

    def match(self, h, t):
        """Histogram of (master_time - clip_time) over all hash hits."""
        hs, ts = [h], [t]
        for d in range(-DT_TOL, DT_TOL + 1):
            if d:
                dt = (h & 63) + d
                ok = (dt >= PAIR_DT[0]) & (dt <= PAIR_DT[1])
                hs.append((h[ok] & ~np.int64(63)) | dt[ok])
                ts.append(t[ok])
        h, t = np.concatenate(hs), np.concatenate(ts)
        lo = np.searchsorted(self.h, h, side="left")
        hi = np.searchsorted(self.h, h, side="right")
        cnt = hi - lo
        if cnt.sum() == 0:
            return None
        rep = np.repeat(np.arange(len(h)), cnt)
        idx = np.concatenate([np.arange(a, b) for a, b in zip(lo[cnt > 0], hi[cnt > 0])])
        offs = self.t[idx] - t[rep]
        base = offs.min()
        hist = np.bincount(offs - base).astype(np.float64)
        return hist, base


def gcc_phat_offset(clip_audio, master, coarse, c0, c1, search=0.12):
    """Refine song offset (s) of clip audio start using GCC-PHAT on clip window [c0, c1) s."""
    a0, a1 = int(c0 * SR), int(c1 * SR)
    seg = clip_audio[a0:a1]
    m0 = int(round((c0 + coarse - search) * SR))
    m1 = m0 + len(seg) + int(2 * search * SR)
    if m0 < 0 or m1 > len(master) or len(seg) < SR * 2:
        return None, 0.0
    ref = master[m0:m1]
    n = 1 << int(math.ceil(math.log2(len(ref) + len(seg))))
    A = np.fft.rfft(ref, n)
    B = np.fft.rfft(seg, n)
    X = A * np.conj(B)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    X[(freqs < 120) | (freqs > 4500)] = 0
    X /= np.maximum(np.abs(X), 1e-12)
    cc = np.fft.irfft(X, n)
    maxlag = int(2 * search * SR)
    cc = cc[:maxlag + 1]
    k = int(np.argmax(cc))
    peak = cc[k]
    noise = np.median(np.abs(cc)) + 1e-12
    frac = 0.0
    if 0 < k < len(cc) - 1:
        y0, y1, y2 = cc[k - 1], cc[k], cc[k + 1]
        d = (y0 - 2 * y1 + y2)
        frac = 0.5 * (y0 - y2) / d if d else 0.0
    lag = (k + frac) / SR
    return coarse - search + lag, float(peak / noise)


def offset_track(x, master, coarse, ov0, ov1, win=10.0, hop=2.5):
    """Follow the offset through the clip in overlapping windows and fit a line.

    Returns (offset at clip time 0, slope) where the song time of clip time t is
    t + offset + slope * t, or None if fewer than two windows gave a clean reading."""
    if ov1 - ov0 < win + hop:
        return None
    pts = []
    est, slope, search = coarse, 0.0, 0.15
    t = ov0
    while t + win <= ov1 + 1e-6:
        c = t + win / 2
        guess = est + slope * hop if pts else est
        off, q = gcc_phat_offset(x, master, guess, t, t + win, search=search)
        if off is not None and q > 4:
            pts.append((c, off, q))
            if len(pts) >= 2:
                (c0, o0, _), (c1, o1, _) = pts[-2], pts[-1]
                slope = (o1 - o0) / (c1 - c0)
            est, search = off, 0.06
        else:
            est = guess
        t += hop
    for _ in range(2):                                # fit, drop outliers (> 15 ms), refit
        if len(pts) < 2:
            return None
        c, o, q = (np.array(v) for v in zip(*pts))
        k, b = np.polyfit(c, o, 1, w=np.sqrt(q))
        keep = np.abs(o - (k * c + b)) < 0.015
        pts = [p for p, kp in zip(pts, keep) if kp]
    if len(pts) < 2:
        return None
    c, o, q = (np.array(v) for v in zip(*pts))
    k, b = np.polyfit(c, o, 1, w=np.sqrt(q))
    return b, k


# ---------------------------------------------------------------- sync one clip

@dataclass
class Settings:
    threshold: float = 60.0
    min_hashes: int = 12
    max_fps: float = 0.0
    speed_margin: float = 0.0       # extra confidence a sped-up match must clear
    speed_strength: float = 0.5     # and how far above chance it must stand (normal: 0.25)


def sync_clip(clip: Clip, master: MasterIndex, st: Settings):
    if not clip.readable:
        clip.reasons.append(REASON_UNREADABLE)
        clip.notes.append(clip.probe_error)
        return
    # High frame rate alone is not a reason to skip: a 60p/120p clip recorded in real time still
    # has real-time scratch audio and syncs like any other. Only no audio, S&Q (conformed slow
    # motion), or an explicit --max-fps sets a clip aside.
    sq = clip.capture_fps and clip.fps and clip.capture_fps > clip.fps + 1
    if not clip.has_audio:
        clip.reasons.append(REASON_NO_AUDIO)
        if sq:
            clip.notes.append("S&Q slow motion captured at %g fps" % clip.capture_fps)
        elif clip.fps and clip.fps > 31:
            clip.notes.append("%g fps" % clip.fps)
    elif sq:
        clip.reasons.append(REASON_SQ)
        clip.notes.append("captured at %g fps, plays at %g" % (clip.capture_fps, clip.fps))
    if st.max_fps and clip.fps and clip.fps > st.max_fps and not clip.reasons:
        clip.reasons.append(REASON_HFR)
        clip.notes.append("%g fps" % clip.fps)
    if clip.reasons:
        return

    try:
        x = load_audio(clip.path)
    except RuntimeError as e:
        clip.reasons.append(REASON_UNREADABLE)
        clip.notes.append("audio decode failed: %s" % e)
        return
    rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    if rms < 10 ** (-70 / 20):
        clip.reasons.append(REASON_SILENT)
        clip.notes.append("digital silence" if rms < 1e-6 else "audio level %.0f dBFS" % (20 * math.log10(rms)))
        return

    peaks = find_peaks(x)
    ev = evaluate(master, *landmarks(*peaks))
    if ev is None:
        clip.reasons.append(REASON_NO_MATCH)
        return
    speed, mode, xs = 1.0, "", x
    if not accepted(ev, st):
        # Playback may have been sped up on set for slow motion (2x for 48p, 2.5x for 60p...).
        # Try common speeds two ways: varispeed (pitch went up with it) and time-stretched
        # (pitch kept). Keep a speed only if it clears a stricter bar than a normal-speed match.
        best_alt = None
        for k in candidate_speeds(clip):
            frac = fractions.Fraction(k).limit_denominator(20)
            xr = signal.resample_poly(x, frac.numerator, frac.denominator).astype(np.float32)
            alt = [("varispeed", xr, evaluate(master, *landmarks(*find_peaks(xr))))]
            # time-stretched: pitch stays, times scale by k, so pick peaks k times denser in clip time
            pt, pf = find_peaks(x, max(1, int(round(PEAK_T_NEIGH / k))), int(PEAKS_PER_SEC * k))
            ts = np.round(pt * k).astype(np.int64)
            alt.append(("time-stretched", None, evaluate(master, *landmarks(ts, pf))))
            for m_name, xa, e in alt:
                if e is not None and (best_alt is None or e["conf"] > best_alt[3]["conf"]):
                    best_alt = (float(frac), m_name, xa, e)
        if best_alt and accepted(best_alt[3], st, extra=st.speed_margin) and \
                best_alt[3]["strength"] >= st.speed_strength and best_alt[3]["conf"] > ev["conf"] + 10:
            speed, mode, xs, ev = best_alt
            clip.speed, clip.speed_mode = speed, mode
            clip.notes.append("song played at %gx on set (%s); placed at %g%% speed"
                              % (speed, mode, 100 / speed))

    A, R, N, conf = ev["A"], ev["R"], ev["N"], ev["conf"]
    aoff = clip.audio_offset * speed               # audio start offset, in song seconds
    clip.aligned, clip.runner_up = A, R
    coarse = ev["offset"]                           # song time of clip audio sample 0
    clip.runner_up_offset = ev["runner_up_offset"] - aoff if R else None
    clip.confidence = round(conf, 1)

    if A < st.min_hashes or ev["strength"] < 0.25:
        clip.reasons.append(REASON_NO_MATCH)
        clip.notes.append("best alignment %d landmarks vs %d by chance" % (A, N))
        return
    if conf < st.threshold:
        if (R - N) > 0.5 * (A - N):
            clip.reasons.append(REASON_AMBIGUOUS)
            clip.notes.append("fits equally at song %.2fs and %.2fs"
                              % (coarse - aoff, clip.runner_up_offset))
        else:
            clip.reasons.append(REASON_LOW_CONF)
            clip.notes.append("best guess song %.2fs" % (coarse - aoff))
        return

    if xs is None:          # time-stretched playback: waveforms differ, landmark precision only
        clip.offset = coarse - aoff
        clip.refine = "landmark only (time-stretched playback, +-1 frame)"
        clip.notes.append(clip.refine)
        clip.status = "placed"
        return

    # sub-frame refinement + drift measurement on the part of the clip that overlaps the song
    clip_len = len(xs) / SR
    ov0 = max(0.0, -coarse) + 0.2
    ov1 = min(clip_len, master.duration - coarse) - 0.2
    track = offset_track(xs, master.audio, coarse, ov0, ov1)
    if track is not None:
        intercept, slope = track
        mid = (ov0 + ov1) / 2                       # centre the drift error across the clip
        offset_audio = intercept + slope * mid
        clip.drift_ms = round(slope * (ov1 - ov0) * 1000, 1)
        clip.refine = "sub-frame refined"
    else:
        fine, q = gcc_phat_offset(xs, master.audio, coarse, ov0, ov1) if ov1 - ov0 > 3 else (None, 0)
        if fine is not None and q > 6:
            offset_audio = fine
            clip.refine = "sub-frame refined"
        else:
            offset_audio = coarse
            clip.refine = "landmark only (refinement too weak, +-1 frame)"
            clip.notes.append(clip.refine)
    clip.offset = offset_audio - aoff
    clip.status = "placed"


SPEEDS = [1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]


def candidate_speeds(clip):
    ks = list(SPEEDS)
    if clip.fps:
        for base in (23.976, 25.0, 29.97):          # e.g. 59.94p shot for a 23.976 edit -> 2.5x
            k = clip.fps / base
            if k > 1.1:
                ks.append(round(k, 3))
    out = []
    for k in sorted(ks):
        if all(abs(k - o) / o > 0.01 for o in out):
            out.append(k)
    return out


def evaluate(master, h, t):
    """Best alignment of clip landmarks against the song, with runner-up and chance level."""
    res = master.match(h, t) if len(h) else None
    if res is None:
        return None
    hist, base = res
    sm = np.convolve(hist, np.ones(3), mode="same")   # tolerate +-1 frame jitter
    best = int(np.argmax(sm))
    A = int(round(sm[best]))
    excl = int(round(1.0 / FRAME_S))
    masked = sm.copy()
    masked[max(0, best - excl):best + excl + 1] = 0
    ru = int(np.argmax(masked)) if masked.any() else best
    R = int(round(masked[ru])) if masked.any() else 0
    masked[max(0, ru - excl):ru + excl + 1] = 0
    N = float(masked.max()) if masked.any() else 0.0   # noise floor: best chance alignment
    # evidence that the best position beats the runner-up, in standard deviations, mapped to 0-100
    z = (A - R) / math.sqrt(A + R) if A + R else 0.0
    return dict(A=A, R=R, N=N, offset=(best + base) * FRAME_S, runner_up_offset=(ru + base) * FRAME_S,
                conf=100.0 * (1 - math.exp(-max(z, 0.0) / 3)),
                strength=(A - N) / A if A else 0.0)       # how far best stands above chance


def accepted(ev, st, extra=0.0):
    return (ev is not None and ev["A"] >= st.min_hashes and ev["strength"] >= 0.25
            and ev["conf"] >= st.threshold + extra)


# ---------------------------------------------------------------- camera grouping

def camera_letter_hint(clip):
    if clip.reel_letter:
        return clip.reel_letter
    m = re.match(r"^(?:cam(?:era)?[ _-]*([A-Z])|([A-Z])[ _-]*cam(?:era)?|([A-Z])\d{3})$",
                 clip.top_folder, re.I)
    if m:
        return (m.group(1) or m.group(2) or m.group(3)).upper()
    return ""


def assign_cameras(clips, group_by):
    for c in clips:
        model = c.model or "Unknown camera"
        if group_by == "folder":
            c.camera_key = c.top_folder or model
        elif group_by == "model":
            c.camera_key = model
        else:
            ident = (("serial " + c.serial) if c.serial else ("reel " + c.reel_letter) if c.reel_letter
                     else ("folder " + c.top_folder) if c.top_folder else "")
            c.camera_key = model + (" / " + ident if ident else "")
    # clips with no readable model (e.g. an unreadable raw file) join the camera they were filed with
    known = [c for c in clips if c.model]
    for c in clips:
        if c.model:
            continue
        mates = [k for k in known if (c.reel_letter and k.reel_letter == c.reel_letter) or
                 (c.top_folder and k.top_folder == c.top_folder)]
        if mates:
            c.camera_key = collections.Counter(k.camera_key for k in mates).most_common(1)[0][0]
    groups = collections.OrderedDict()
    for c in sorted(clips, key=lambda c: c.rel):
        groups.setdefault(c.camera_key, []).append(c)
    hints = {}
    for key, cl in groups.items():
        letters = collections.Counter(camera_letter_hint(c) for c in cl if camera_letter_hint(c))
        hints[key] = letters.most_common(1)[0][0] if letters else ""
    used, labels = set(), {}
    hinted = collections.Counter(h for h in hints.values() if h)
    for key in sorted(groups, key=lambda k: (hints[k] == "", hints[k], k)):   # unique hints first
        if hints[key] and hinted[hints[key]] == 1:
            labels[key] = hints[key]
            used.add(hints[key])
    for key in sorted(groups, key=lambda k: (hints[k] == "", hints[k], k)):
        if key not in labels:
            letter = next(ch for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if ch not in used)
            labels[key] = letter
            used.add(letter)
    for c in clips:
        c.camera = "%s Cam" % labels[c.camera_key]
    return labels


# ---------------------------------------------------------------- XMEML

def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def add_rate(parent, fps):
    base, ntsc = rate_xml(fps)
    r = sub(parent, "rate")
    sub(r, "timebase", base)
    sub(r, "ntsc", "TRUE" if ntsc else "FALSE")
    return r


def add_timecode(parent, fps, frame, string=None):
    tc = sub(parent, "timecode")
    add_rate(tc, fps)
    sub(tc, "string", string or fmt_frames(frame, fps))
    sub(tc, "frame", frame)
    sub(tc, "displayformat", "NDF")
    return tc


# Premiere label colour names, in the order cameras get them (A Iris, B Mango, C Rose, ...)
CAMERA_LABELS = ["Iris", "Mango", "Rose", "Caribbean", "Forest", "Lavender", "Cerulean", "Yellow",
                 "Magenta", "Tan", "Violet", "Purple", "Blue", "Teal", "Green", "Brown"]


def camera_label(letter):
    return CAMERA_LABELS[(ord(letter) - ord("A")) % len(CAMERA_LABELS)]


def add_labels(parent, label):
    if label:
        lb = sub(parent, "labels")
        sub(lb, "label2", label)


@dataclass
class Media:
    """A source file as it appears in the XML."""
    path: str
    fps: float
    duration: float
    has_video: bool = True
    has_audio: bool = True
    channels: int = 2
    rate: int = 48000
    width: int = 1920
    height: int = 1080
    timecode: str = ""

    @staticmethod
    def of_clip(c, fallback_fps):
        return Media(c.path, c.fps or fallback_fps, c.duration, True, c.has_audio,
                     max(1, c.audio_channels), c.audio_rate, c.width or 1920, c.height or 1080, c.timecode)


class Xmeml:
    """Builds XMEML elements; each source file is fully described once, then referenced by id."""

    def __init__(self, path_maps=()):
        self.path_maps = path_maps
        self.files = {}
        self.n = 0

    def uid(self, prefix):
        self.n += 1
        return "%s-%d" % (prefix, self.n)

    def file(self, parent, m: Media):
        if m.path in self.files:
            return sub(parent, "file", id=self.files[m.path])
        fid = self.uid("file")
        self.files[m.path] = fid
        f = sub(parent, "file", id=fid)
        sub(f, "name", os.path.basename(m.path))
        sub(f, "pathurl", path_to_url(m.path, self.path_maps))
        add_rate(f, m.fps)
        sub(f, "duration", int(round(m.duration * m.fps)))
        add_timecode(f, m.fps, 0, m.timecode or None)
        media = sub(f, "media")
        if m.has_video:
            v = sub(media, "video")
            sc = sub(v, "samplecharacteristics")
            add_rate(sc, m.fps)
            sub(sc, "width", m.width)
            sub(sc, "height", m.height)
            sub(sc, "anamorphic", "FALSE")
            sub(sc, "pixelaspectratio", "square")
            sub(sc, "fielddominance", "none")
        if m.has_audio:
            a = sub(media, "audio")
            sc = sub(a, "samplecharacteristics")
            sub(sc, "depth", 16)
            sub(sc, "samplerate", m.rate)
            sub(a, "channelcount", m.channels)
        return f

    def clipitem(self, track, cid, m, mediatype, start, frames, fps, enabled=True, label=None, scale=None,
                 speed=1.0):
        ci = sub(track, "clipitem", id=cid)
        sub(ci, "name", os.path.basename(m.path))
        sub(ci, "enabled", "TRUE" if enabled else "FALSE")
        sub(ci, "duration", frames)
        add_rate(ci, fps)
        sub(ci, "start", start)
        sub(ci, "end", start + frames)
        sub(ci, "in", 0)
        sub(ci, "out", int(round(frames / speed)))    # source frames; timeline length is frames
        self.file(ci, m)
        if abs(speed - 1) > 1e-6:
            add_speed(ci, 100.0 / speed, mediatype)
        if mediatype == "audio":
            st = sub(ci, "sourcetrack")
            sub(st, "mediatype", "audio")
            sub(st, "trackindex", 1)
        if scale is not None and abs(scale - 100) > 0.01:
            add_scale(ci, scale)
        add_labels(ci, label)
        return ci

    def master_clip(self, parent, m: Media, label=None):
        """A bin item (Premiere master clip) for a source file."""
        frames = int(round(m.duration * m.fps))
        clip = sub(parent, "clip", id=self.uid("masterclip"))
        sub(clip, "name", os.path.basename(m.path))
        sub(clip, "duration", frames)
        add_rate(clip, m.fps)
        sub(clip, "in", 0)
        sub(clip, "out", frames)
        sub(clip, "ismasterclip", "TRUE")
        media = sub(clip, "media")
        items = []
        if m.has_video:
            t = sub(sub(media, "video"), "track")
            items.append((self.clipitem(t, self.uid("clipitem"), m, "video", 0, frames, m.fps), "video"))
        if m.has_audio:
            t = sub(sub(media, "audio"), "track")
            items.append((self.clipitem(t, self.uid("clipitem"), m, "audio", 0, frames, m.fps), "audio"))
        self._link(items, {id(ci): 1 for ci, _ in items}, {id(ci): 1 for ci, _ in items})
        add_labels(clip, label)
        return clip

    @staticmethod
    def _link(items, track_of, index_of):
        if len(items) < 2:
            return
        for ci, _ in items:
            for other, mt in items:
                ln = sub(ci, "link")
                sub(ln, "linkclipref", other.get("id"))
                sub(ln, "mediatype", mt)
                sub(ln, "trackindex", track_of[id(other)])
                sub(ln, "clipindex", index_of[id(other)])

    def sequence(self, parent, name, fps, width, height, start_tc_frame, entries, label=None):
        """entries: dicts with media, start, vtrack (or None), atrack (or None), aenabled."""
        seq = sub(parent, "sequence", id=self.uid("sequence"))
        sub(seq, "uuid", "musicsync-%s-%s" % (datetime.datetime.now().strftime("%Y%m%d%H%M%S"), self.n))
        sub(seq, "name", name)
        total = max([1] + [e["start"] + (e["nest"][1] if e.get("nest") is not None
                                         else int(round(e["media"].duration * fps * e.get("speed", 1))))
                           for e in entries])
        sub(seq, "duration", total)
        add_rate(seq, fps)
        add_timecode(seq, fps, start_tc_frame)
        media = sub(seq, "media")
        video = sub(media, "video")
        sc = sub(sub(video, "format"), "samplecharacteristics")
        add_rate(sc, fps)
        sub(sc, "width", width)
        sub(sc, "height", height)
        sub(sc, "anamorphic", "FALSE")
        sub(sc, "pixelaspectratio", "square")
        sub(sc, "fielddominance", "none")
        audio = sub(media, "audio")
        sub(audio, "numOutputChannels", 2)
        asc = sub(sub(audio, "format"), "samplecharacteristics")
        sub(asc, "depth", 16)
        sub(asc, "samplerate", 48000)

        nv = max([0] + [e["vtrack"] or 0 for e in entries])
        na = max([0] + [e["atrack"] or 0 for e in entries])
        vtracks = [sub(video, "track") for _ in range(max(nv, 1))]
        atracks = [sub(audio, "track") for _ in range(na)]
        count = collections.Counter()
        track_of, index_of = {}, {}
        for e in sorted(entries, key=lambda e: e["start"]):
            if e.get("nest") is not None:           # a sequence used as a clip (nested sequence)
                nseq, nframes = e["nest"]
                ci = sub(vtracks[e["vtrack"] - 1], "clipitem", id=self.uid("clipitem"))
                sub(ci, "name", nseq.findtext("name"))
                sub(ci, "enabled", "TRUE")
                sub(ci, "duration", nframes)
                add_rate(ci, fps)
                sub(ci, "start", e["start"])
                sub(ci, "end", e["start"] + nframes)
                sub(ci, "in", 0)
                sub(ci, "out", nframes)
                sub(ci, "sequence", id=nseq.get("id"))
                add_labels(ci, e.get("label"))
                continue
            sp = e.get("speed", 1.0)
            m, frames = e["media"], int(round(e["media"].duration * fps * sp))
            items = []
            if e["vtrack"]:
                ci = self.clipitem(vtracks[e["vtrack"] - 1], self.uid("clipitem"), m, "video",
                                   e["start"], frames, fps, label=e.get("label"),
                                   scale=fill_scale(m, width, height), speed=sp)
                count["v", e["vtrack"]] += 1
                track_of[id(ci)], index_of[id(ci)] = e["vtrack"], count["v", e["vtrack"]]
                items.append((ci, "video"))
            if e["atrack"] and m.has_audio:
                ci = self.clipitem(atracks[e["atrack"] - 1], self.uid("clipitem"), m, "audio",
                                   e["start"], frames, fps, enabled=e.get("aenabled", True),
                                   label=e.get("label"), speed=sp)
                count["a", e["atrack"]] += 1
                track_of[id(ci)], index_of[id(ci)] = e["atrack"], count["a", e["atrack"]]
                items.append((ci, "audio"))
            self._link(items, track_of, index_of)
        for t in vtracks + atracks:
            sub(t, "enabled", "TRUE")
            sub(t, "locked", "FALSE")
        add_labels(seq, label)
        return seq


def add_scale(clipitem, scale):
    """Motion > Scale, the way Premiere writes it in its own XML exports."""
    eff = sub(sub(clipitem, "filter"), "effect")
    sub(eff, "name", "Basic Motion")
    sub(eff, "effectid", "basic")
    sub(eff, "effectcategory", "motion")
    sub(eff, "effecttype", "motion")
    sub(eff, "mediatype", "video")
    p = sub(eff, "parameter", authoringApp="PremierePro")
    sub(p, "parameterid", "scale")
    sub(p, "name", "Scale")
    sub(p, "valuemin", 0)
    sub(p, "valuemax", 10000)
    sub(p, "value", "%.2f" % scale)


def add_speed(clipitem, percent, mediatype):
    """Constant speed change (Premiere: Speed/Duration), as Premiere writes it in its XML exports."""
    eff = sub(sub(clipitem, "filter"), "effect")
    sub(eff, "name", "Time Remap")
    sub(eff, "effectid", "timeremap")
    sub(eff, "effectcategory", "motion")
    sub(eff, "effecttype", "motion")
    sub(eff, "mediatype", mediatype)
    for pid, val in (("variablespeed", 0), ("speed", "%.4f" % percent), ("reverse", "FALSE"),
                     ("frameblending", "FALSE")):
        p = sub(eff, "parameter", authoringApp="PremierePro")
        sub(p, "parameterid", pid)
        sub(p, "name", pid)
        if pid == "speed":
            sub(p, "valuemin", -100000)
            sub(p, "valuemax", 100000)
        sub(p, "value", val)


def fill_scale(m, width, height):
    """Scale (%) that makes a clip fill the sequence frame, cropping the overflow."""
    if not m.has_video or not m.width or not m.height:
        return None
    return 100.0 * max(width / m.width, height / m.height)


def first_format(clips, fallback_fps):
    """Frame size of the first clip in filename order; frame rate most common among them."""
    first = next((c for c in sorted(clips, key=lambda c: c.rel) if c.width), None)
    fps = collections.Counter(c.fps for c in clips if c.fps).most_common(1)
    return ((first.width, first.height) if first else (1920, 1080)), (fps[0][0] if fps else fallback_fps)


def short_camera_name(model):
    """'ARRI ALEXA Mini LF' -> 'Mini LF', 'ILME-FX3' -> 'FX3', 'GoPro HD9' -> 'GoPro HERO9'."""
    if not model or model == "Unknown camera":
        return ""
    m = model.strip()
    m = re.sub(r"^(ARRI\s+)?ALEXA\s+(?=Mini|35|65)", "", m, flags=re.I)   # ALEXA Mini LF -> Mini LF
    m = re.sub(r"^ARRI\s+", "", m, flags=re.I)
    sony = {"ILCE-7SM3": "A7S III", "ILCE-7SM2": "A7S II", "ILCE-7M4": "A7 IV", "ILCE-1": "A1",
            "ILCE-9M3": "A9 III", "MPC-3610": "Venice", "MPC-3628": "Venice 2", "PXW-FX9": "FX9",
            "ILME-FR7": "FR7", "ILME-FX2": "FX2"}
    if m.upper() in sony:
        return sony[m.upper()]
    m = re.sub(r"^(ILME|PXW|ILCE|DSC)-", "", m)
    g = re.match(r"GoPro\s+(?:HD(\d+)|H(\d\d))", m)
    if g:
        n = int(g.group(1)) if g.group(1) else int(g.group(2)) - 11      # H22 firmware = HERO11
        return "GoPro HERO%d" % n
    return m


def cam_bin_name(letter, model):
    short = short_camera_name(model)
    return "%s Cam (%s)" % (letter, short) if short else "%s Cam" % letter


def sync_entries(clips, seq_fps, preroll_s, master_media, args, label):
    """Sync layout: song at 01:00:00:00, every clip on its own video track at its song offset."""
    song_frame = int(round(preroll_s * seq_fps))
    entries = []
    if master_media and not args.no_master_audio:
        entries.append(dict(media=master_media, start=song_frame, vtrack=None, atrack=1))
    first_a = 1 if args.no_master_audio else 2
    for c in clips:
        c.seq_start_frame = song_frame + int(round(c.offset * seq_fps))
        entries.append(dict(media=Media.of_clip(c, seq_fps), start=c.seq_start_frame, vtrack=c.track,
                            atrack=(first_a + c.track - 1) if args.scratch_audio != "off" else None,
                            aenabled=args.scratch_audio == "on", label=label, speed=c.speed))
    start_tc = 3600 * rate_xml(seq_fps)[0] - song_frame
    return entries, start_tc


def stringout_entries(clips, fps, label):
    """Breakup layout: every clip of the camera back to back on V1/A1, in filename order."""
    entries, pos = [], 0
    for c in clips:
        m = Media.of_clip(c, fps)
        entries.append(dict(media=m, start=pos, vtrack=1, atrack=1, label=label))
        pos += int(round(c.duration * fps))
    return entries, 3600 * rate_xml(fps)[0]


def bin_(parent, name, label=None):
    b = sub(parent, "bin")
    sub(b, "name", name)
    ch = sub(b, "children")
    add_labels(b, label)
    return ch


PNG_1PX = bytes.fromhex(   # 1x1 fully transparent PNG
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def placeholder(xw, parent, out_dir, bin_path, fps):
    """Premiere's XML import drops empty bins, so a bin that should exist empty gets a blank still."""
    d = os.path.join(out_dir, "_empty-bin-placeholders", *bin_path)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "(empty bin).png")
    with open(p, "wb") as fh:
        fh.write(PNG_1PX)
    xw.master_clip(parent, Media(p, fps, 5.0, has_video=True, has_audio=False, width=1, height=1))


def build_project(name, clips, cams, seq_fps, preroll, master_media, audio_bins, args):
    """One XMEML that recreates the house bin structure (see README)."""
    xw = Xmeml(args.path_maps)
    root = ET.Element("xmeml", version="4")
    proj = sub(root, "project")
    sub(proj, "name", name)
    top = sub(proj, "children")

    def maybe_empty(parent, path, has_items):
        if not has_items and args.placeholders:
            placeholder(xw, parent, args.out, path, seq_fps)

    adj = bin_(top, "Adjustment Layers")
    maybe_empty(adj, ["Adjustment Layers"], False)
    footage = bin_(top, "Footage")
    for letter, cl in cams:
        label = camera_label(letter)
        b = bin_(footage, cam_bin_name(letter, cl[0].model if cl else ""), label)
        usable = [c for c in cl if c.readable and c.fps]
        for c in usable:
            xw.master_clip(b, Media.of_clip(c, seq_fps), label)
        maybe_empty(b, ["Footage", "%s Cam" % letter], bool(usable))

    seqs = bin_(top, "Sequence")
    breakup = bin_(seqs, "Breakup")
    syncb = bin_(seqs, "Sync")
    nests = []
    for letter, cl in cams:
        label = camera_label(letter)
        usable = [c for c in cl if c.readable and c.fps and c.width]
        if usable:
            (w, h), fps = first_format(usable, seq_fps)
            entries, tc = stringout_entries(usable, fps, label)
            xw.sequence(breakup, "%s Cam_Breakup" % letter, fps, w, h, tc, entries, label)
        placed = sorted([c for c in cl if c.status == "placed"], key=lambda c: c.track)
        if placed:
            (w, h), _ = first_format(placed, seq_fps)
            entries, tc = sync_entries(placed, seq_fps, preroll, master_media, args, label)
            seq = xw.sequence(syncb, "%s Cam_Sync" % letter, seq_fps, w, h, tc, entries, label)
            nests.append((letter, seq, (w, h), tc))
    maybe_empty(breakup, ["Sequence", "Breakup"], bool(len(breakup)))
    maybe_empty(syncb, ["Sequence", "Sync"], bool(nests))

    edit = bin_(seqs, "Edit")
    for sub_name in ("Working", "Past"):
        maybe_empty(bin_(edit, sub_name), ["Sequence", "Edit", sub_name], False)
    if nests:
        # the sequence Jake cuts in: each camera's sync sequence nested on its own track, song on A1
        (w, h), tc = nests[0][2], nests[0][3]
        song_frame = int(round(preroll * seq_fps))
        entries = [dict(nest=(seq, int(seq.findtext("duration"))), start=0, vtrack=i, atrack=None,
                        label=camera_label(letter))
                   for i, (letter, seq, _, _) in enumerate(nests, 1)]
        if master_media and not args.no_master_audio:
            entries.append(dict(media=master_media, start=song_frame, vtrack=None, atrack=1))
        xw.sequence(edit, "%s_Edit" % name, seq_fps, w, h, tc, entries)

    audio = bin_(top, "Audio")
    for bname in ("Music", "SFX", "Captured"):
        b = bin_(audio, bname)
        items = ([master_media] if bname == "Music" and master_media else []) + audio_bins.get(bname, [])
        for m in items:
            xw.master_clip(b, m)
        maybe_empty(b, ["Audio", bname], bool(items))
    return root


def build_camera_xml(letter, clips, seq_fps, preroll, master_media, args):
    """Stand-alone sync sequence for one camera (the --per-camera output)."""
    xw = Xmeml(args.path_maps)
    root = ET.Element("xmeml", version="4")
    (w, h), _ = first_format(clips, seq_fps)
    label = camera_label(letter)
    entries, tc = sync_entries(clips, seq_fps, preroll, master_media, args, label)
    xw.sequence(root, "%s Cam_Sync" % letter, seq_fps, w, h, tc, entries, label)
    return root


def write_xml(root, path):
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        fh.write(body)
        fh.write("\n")


# ---------------------------------------------------------------- reports

COLUMNS = ["file", "status", "reason", "camera", "track", "playback_speed", "offset_seconds", "offset_timecode",
           "timeline_timecode", "confidence", "matching_landmarks", "runner_up_landmarks",
           "drift_ms_head_to_tail", "drift_frames", "fps", "duration_s", "resolution", "audio",
           "camera_model", "serial", "notes"]


def clip_row(c, seq_fps, preroll):
    fps = seq_fps
    drift_frames = round(c.drift_ms / 1000 * fps, 2) if c.drift_ms is not None else ""
    return {
        "file": c.rel,
        "status": c.status,
        "reason": "; ".join(c.reasons),
        "camera": c.camera,
        "track": ("V%d" % c.track) if c.track else "",
        "playback_speed": ("%gx %s" % (c.speed, c.speed_mode)) if c.speed != 1 else "",
        "offset_seconds": "%.3f" % c.offset if c.offset is not None else "",
        "offset_timecode": fmt_tc(c.offset, fps) if c.offset is not None else "",
        "timeline_timecode": fmt_frames(3600 * rate_xml(fps)[0] + int(round(c.offset * fps)), fps)
        if c.offset is not None else "",
        "confidence": "%.1f" % c.confidence if c.confidence is not None else "",
        "matching_landmarks": c.aligned or "",
        "runner_up_landmarks": c.runner_up if c.aligned else "",
        "drift_ms_head_to_tail": c.drift_ms if c.drift_ms is not None else "",
        "drift_frames": drift_frames,
        "fps": ("%g" % c.fps if c.fps else "") + (" (capture %g)" % c.capture_fps if c.capture_fps and c.fps and abs(c.capture_fps - c.fps) > 1 else ""),
        "duration_s": "%.2f" % c.duration if c.duration else "",
        "resolution": "%dx%d" % (c.width, c.height) if c.width else "",
        "audio": ("%dch" % c.audio_channels) if c.has_audio else "none",
        "camera_model": c.model,
        "serial": c.serial,
        "notes": "; ".join(c.notes),
    }


def md_escape(s):
    return str(s).replace("|", "\\|")


def write_reports(clips, out_dir, seq_fps, preroll, args, cam_files, labels, captured=()):
    rows = [clip_row(c, seq_fps, preroll) for c in clips]
    with open(os.path.join(out_dir, "sync_report.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    placed = [c for c in clips if c.status == "placed"]
    L = []
    L.append("# Sync report")
    L.append("")
    L.append("Master: `%s`  " % os.path.basename(args.master))
    L.append("Clips folder: `%s`  " % args.clips)
    L.append("Generated %s by musicsync %s. Sequence rate %g fps. Song starts at timeline "
             "01:00:00:00 in every camera sequence." % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                                        VERSION, seq_fps))
    L.append("")
    L.append("**%d clips found, %d placed, %d not placed.** Confidence threshold %g. %s" % (
        len(clips), len(placed), len(clips) - len(placed), args.threshold,
        ("Clips above %g fps set aside." % args.max_fps) if args.max_fps
        else "High-frame-rate clips with scratch audio are synced."))
    L.append("")
    L.append("| Camera | Label | Model | Placed | Not placed | Sync sequence in |")
    L.append("|---|---|---|---|---|---|")
    for key, letter in sorted(labels.items(), key=lambda kv: kv[1]):
        cl = [c for c in clips if c.camera_key == key]
        p = sum(1 for c in cl if c.status == "placed")
        L.append("| %s Cam | %s | %s | %d | %d | %s |" % (letter, camera_label(letter), md_escape(key), p,
                                                          len(cl) - p, cam_files.get(key, "")
                                                          if p else "(nothing placed)"))
    L.append("")
    L.append("Master song: `%s`. Captured audio in Audio > Captured: %s" % (
        os.path.relpath(args.master, args.clips),
        ", ".join("`%s`" % os.path.relpath(p, args.clips) for p in captured) or "none found"))
    L.append("")
    un = [c for c in clips if c.status != "placed"]
    if un:
        L.append("## Not placed")
        L.append("")
        by = collections.Counter(c.reasons[0] for c in un)
        L.append(", ".join("%s: %d" % (k, v) for k, v in by.most_common()))
        L.append("")
        L.append("| File | Camera | Reason | Confidence | Detail |")
        L.append("|---|---|---|---|---|")
        for c in un:
            L.append("| %s | %s | %s | %s | %s |" % (md_escape(c.rel), c.camera, "; ".join(c.reasons),
                                                    "%.1f" % c.confidence if c.confidence is not None else "",
                                                    md_escape("; ".join(c.notes))))
        L.append("")
    warn = [c for c in placed if c.drift_ms is not None and abs(c.drift_ms) / 1000 * seq_fps >= 1]
    if warn:
        L.append("## Drift warnings")
        L.append("")
        L.append("These clips are in sync at one point but slide by a frame or more between head and tail "
                 "(clock drift or playback speed / 23.976-vs-24 mismatch). They are placed so the error is "
                 "split across the clip.")
        L.append("")
        for c in warn:
            L.append("- %s: %+.0f ms (%+.1f frames) over the clip" % (c.rel, c.drift_ms,
                                                                    c.drift_ms / 1000 * seq_fps))
        L.append("")
    L.append("## All clips")
    L.append("")
    cols = ["file", "status", "reason", "camera", "track", "offset_timecode", "offset_seconds",
            "confidence", "drift_frames", "fps", "audio"]
    L.append("| " + " | ".join(cols) + " |")
    L.append("|" + "---|" * len(cols))
    for r in rows:
        L.append("| " + " | ".join(md_escape(r[k]) for k in cols) + " |")
    L.append("")
    L.append("Offsets are song time of the clip's first frame (negative means the camera rolled before the "
             "song started). Confidence (0-100) measures how decisively the best position in the song beats the "
             "next-best one: 60 is about 2.7 standard deviations, 90+ is unmistakable.")
    with open(os.path.join(out_dir, "sync_report.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", metavar="[MASTER] FOLDER",
                    help="a shoot folder (the song is found inside it), or a master song then a clips folder")
    ap.add_argument("--master", help="master song, if it can't be found in the folder automatically")
    ap.add_argument("-o", "--out", help="output folder (default: 'Premiere Sync' inside the folder)")
    ap.add_argument("--name", help="project name (default: the folder name)")
    ap.add_argument("--no-placeholders", dest="placeholders", action="store_false",
                    help="don't put a blank '(empty bin)' still in bins that would otherwise be empty "
                         "(Premiere drops empty bins on XML import)")
    ap.add_argument("--per-camera", action="store_true",
                    help="also write a stand-alone sync XML per camera")
    ap.add_argument("--threshold", type=float, default=60.0,
                    help="minimum confidence 0-100 to place a clip (default 60)")
    ap.add_argument("--min-landmarks", type=int, default=12,
                    help="fewer aligned landmarks than this counts as no match (default 12)")
    ap.add_argument("--max-fps", type=float, default=0.0,
                    help="set aside clips above this frame rate even if they have scratch audio "
                         "(default 0: high-frame-rate clips with audio are synced)")
    ap.add_argument("--fps", type=float, help="sync sequence frame rate (default: most common among placed clips)")
    ap.add_argument("--group-by", choices=["auto", "model", "folder"], default="auto",
                    help="auto: model + serial / reel letter / top folder; model: model only; "
                         "folder: top-level folder only")
    ap.add_argument("--track-order", choices=["name", "offset"], default="name",
                    help="order of clips on V1..Vn in the sync sequences (default: filename)")
    ap.add_argument("--scratch-audio", choices=["off", "disabled", "on"], default="disabled",
                    help="put each clip's scratch audio on its own audio track (default: present but disabled)")
    ap.add_argument("--no-master-audio", action="store_true", help="don't put the master song on A1")
    ap.add_argument("--path-map", action="append", default=[], metavar="OLD=NEW",
                    help="rewrite media paths in the XML, e.g. /mnt/footage=/Volumes/SSD/Shoot "
                         "or /mnt/footage=D:/Shoot (repeatable)")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, min(8, os.cpu_count() or 2)))
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    if len(args.paths) > 2:
        ap.error("give a folder, or a master song and a folder")
    if len(args.paths) == 2:
        args.master, args.clips = args.paths
    else:
        args.clips = args.paths[0]
    if not os.path.isdir(args.clips):
        sys.exit("error: %s is not a folder" % args.clips)
    args.clips = os.path.abspath(args.clips)
    args.out = os.path.abspath(args.out or os.path.join(args.clips, "Premiere Sync"))
    project_name = args.name or os.path.basename(args.clips.rstrip("/\\")) or "Sync"

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("error: %s not found on PATH" % tool)
    args.path_maps = []
    for pm in args.path_map:
        if "=" not in pm:
            sys.exit("error: --path-map needs OLD=NEW")
        old, new = pm.split("=", 1)
        args.path_maps.append((os.path.abspath(old), new.rstrip("/\\")))

    audio_files = find_audio(args.clips, [args.out])
    if not args.master:
        args.master = pick_master(args.clips, audio_files)
        if not args.master:
            names = "\n  ".join(os.path.relpath(p, args.clips) for p in audio_files) or "(no audio files)"
            sys.exit("error: can't tell which file is the song. Put it in a 'Music' folder or pass "
                     "--master.\nAudio files found:\n  " + names)
        log("Master song: %s" % os.path.relpath(args.master, args.clips))
    master_abs = os.path.abspath(args.master)
    captured = [p for p in audio_files if os.path.abspath(p) != master_abs]

    def audio_bin(p):      # files under a folder called SFX / Music go to those bins, the rest is Captured
        parts = [x.lower() for x in os.path.relpath(p, args.clips).replace("\\", "/").split("/")[:-1]]
        if any(x in ("sfx", "sound effects", "sound fx") for x in parts):
            return "SFX"
        if any(x in ("music", "song", "songs") for x in parts):
            return "Music"
        return "Captured"
    os.makedirs(args.out, exist_ok=True)

    log("Indexing master %s" % args.master)
    try:
        master = MasterIndex(load_audio(args.master))
    except RuntimeError as e:
        sys.exit("error: can't read master: %s" % e)
    log("  %.1f s, %d landmarks" % (master.duration, len(master.h)))

    clips = find_clips(args.clips, args.master, [args.out])
    if not clips:
        sys.exit("error: no video files found in %s" % args.clips)
    log("Probing %d clips" % len(clips))
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(probe, clips))

    st = Settings(threshold=args.threshold, min_hashes=args.min_landmarks, max_fps=args.max_fps)
    log("Matching")
    done = 0

    def work(c):
        try:
            sync_clip(c, master, st)
        except Exception as e:           # one bad file never kills the batch
            c.reasons.append(REASON_UNREADABLE)
            c.notes.append("error: %s" % e)
        return c

    with cf.ThreadPoolExecutor(args.jobs) as ex:
        for c in ex.map(work, clips):
            done += 1
            if c.status != "placed":
                c.status = "not placed"
                if not c.reasons:
                    c.reasons.append(REASON_NO_MATCH)
            log("  [%d/%d] %-40s %s" % (done, len(clips), c.rel,
                                         ("%.3fs  conf %.0f" % (c.offset, c.confidence)) if c.status == "placed"
                                         else "-- " + "; ".join(c.reasons)))

    labels = assign_cameras(clips, args.group_by)
    placed = [c for c in clips if c.status == "placed"]
    seq_fps = args.fps or (collections.Counter(c.fps for c in placed if c.fps).most_common(1) or
                           collections.Counter(c.fps for c in clips if c.fps).most_common(1) or [(24.0, 0)])[0][0]
    preroll = max([0.0] + [-c.offset for c in placed])
    preroll = math.ceil(preroll + 0.5)            # whole seconds, identical in every camera sequence

    cams = []
    for key, letter in sorted(labels.items(), key=lambda kv: kv[1]):
        cl = sorted([c for c in clips if c.camera_key == key], key=lambda c: c.rel)
        pl = [c for c in cl if c.status == "placed"]
        pl.sort(key=(lambda c: c.rel) if args.track_order == "name" else (lambda c: (c.offset, c.rel)))
        for i, c in enumerate(pl, 1):
            c.track = i
        cams.append((letter, cl))

    _, mch, mrate = probe_audio(args.master)
    master_media = Media(args.master, seq_fps, master.duration, has_video=False, channels=mch, rate=mrate)
    audio_bins = collections.defaultdict(list)
    for p in captured:
        dur, ch, rate = probe_audio(p)
        audio_bins[audio_bin(p)].append(Media(p, seq_fps, dur, has_video=False, channels=ch, rate=rate))
    captured = [m.path for m in audio_bins["Captured"]]

    proj_file = re.sub(r"[^\w .-]+", "_", project_name) + ".xml"
    write_xml(build_project(project_name, clips, cams, seq_fps, preroll, master_media, audio_bins, args),
              os.path.join(args.out, proj_file))
    log("Wrote %s" % proj_file)
    cam_files = {}
    for letter, cl in cams:
        key = cl[0].camera_key
        cam_files[key] = proj_file
        pl = sorted([c for c in cl if c.status == "placed"], key=lambda c: c.track)
        if args.per_camera and pl:
            model = key.split(" / ")[0]
            fname = re.sub(r"[^\w .-]+", "_", "%s Cam_Sync - %s.xml" % (letter, model))
            write_xml(build_camera_xml(letter, pl, seq_fps, preroll, master_media, args),
                      os.path.join(args.out, fname))
            cam_files[key] = fname
            log("Wrote %s (%d tracks)" % (fname, len(pl)))

    write_reports(clips, args.out, seq_fps, preroll, args, cam_files, labels, captured)
    log("Wrote sync_report.csv and sync_report.md")
    log("Placed %d of %d clips." % (len(placed), len(clips)))


if __name__ == "__main__":
    main()
