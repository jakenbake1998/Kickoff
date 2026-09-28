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
import warnings
import xml.etree.ElementTree as ET
import dataclasses
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import ndimage, signal

VERSION = "0.5.13"

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
OUT_DIR = "Kickoff Exports"            # what Kickoff writes, inside the shoot folder
OLD_OUT_DIRS = ("Premiere Sync",)      # its name before 0.5.3: earlier projects are still found there
SKIP_DIRS = {"SUB", "THMBNL", "GENERAL", "AVF_INFO", "CACHE", "THMB"}
# not camera originals: proxies, Premiere's preview renders and auto-saves, renders and exports
SKIP_DIR_RE = re.compile(r"prox(y|ies)|previews?\b|auto-?save|\brenders?\b|\bgenerations?\b|\bexports?\b|"
                         r"media cache|^premiere sync$|^kickoff exports$|\.(prproj|fcpbundle|drp)$", re.I)
CAM_FOLDER = re.compile(r"^(?:(?i:cam(?:era)?)(?:[ _-]+([A-Za-z])|([A-Z]))(?![A-Za-z])|"
                        r"([A-Za-z])[ _-]*(?i:cam(?:era)?)(?![A-Za-z]))")


def skip_dir(name):
    return name.startswith(".") or name.upper() in SKIP_DIRS or bool(SKIP_DIR_RE.search(name))

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


EVENTS = False              # --events: progress as JSON lines on stdout, for the Kickoff window


def event(kind, **data):
    if EVENTS:
        print("@@kickoff " + json.dumps(dict(data, event=kind)), flush=True)


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
    par: float = 1.0                     # pixel aspect (anamorphic 2x: 2.0)
    rotation: int = 0                    # degrees the player turns the picture (phones shot upright: 90)
    vcodec: str = ""
    has_audio: bool = False
    audio_channels: int = 0
    audio_layout: list = field(default_factory=list)    # channels in each audio stream, in order
    audio_pick: str = ""                 # which stream/channel the scratch mic was found on
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
    check: str = ""                      # waveform check: "14/15" windows that match at the offset
    split: bool = False                  # the song restarts / jumps inside this take (see parts)
    parts: list = field(default_factory=list)   # Part per pass of the song; one Part when not split
    seq_start_frame: Optional[int] = None
    notes: list = field(default_factory=list)
    repeat_alt: Optional[float] = None   # placed at the first of two identical sections; the other


@dataclass
class Part:
    """One stretch of a clip that sits at one place in the song. A take where the song was stopped
    and restarted (or jumped) has one Part per pass, each placed on its own track."""
    src_in: float                        # clip seconds (video clock)
    src_out: float
    offset: Optional[float] = None       # song time of the clip's first frame under this pass
    status: str = ""
    reason: str = ""
    confidence: Optional[float] = None
    aligned: int = 0
    runner_up: int = 0
    drift_ms: Optional[float] = None
    refine: str = ""
    check: str = ""
    track: Optional[int] = None
    notes: list = field(default_factory=list)
    repeat_alt: Optional[float] = None


BRANDS = [("gopro", "GoPro"), ("dji", "DJI"), ("arri", "ARRI"), ("alexa", "ARRI"),
          ("red digital", "RED"), ("sony", "Sony"), ("canon", "Canon"),
          ("panasonic", "Panasonic"), ("blackmagic", "Blackmagic"),
          ("fujifilm", "Fujifilm"), ("nikon", "Nikon"),
          ("insta360", "Insta360"), ("z cam", "Z CAM")]
# no "apple": every ProRes/QuickTime file mentions Apple (codec, handler), whatever shot it. iPhones
# name themselves in com.apple.quicktime.model, which is read first.


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
    clip.audio_layout = [int(s.get("channels") or 1) for s in streams if s.get("codec_type") == "audio"]
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
        sar = re.match(r"^(\d+):(\d+)$", v.get("sample_aspect_ratio") or "")
        if sar and int(sar.group(1)) and int(sar.group(2)):
            clip.par = int(sar.group(1)) / int(sar.group(2))
        rot = next((sd.get("rotation") for sd in v.get("side_data_list") or [] if "rotation" in sd),
                   (v.get("tags") or {}).get("rotate"))
        try:
            clip.rotation = int(round(float(rot or 0))) % 360
        except ValueError:
            pass
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
        dirs[:] = sorted(d for d in dirs if not skip_dir(d) and os.path.abspath(os.path.join(root, d)) not in skip)
        out += [os.path.join(root, f) for f in sorted(files)
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in AUDIO_EXT]
    return out


SONG_HINT = re.compile(r"music|master|song|track|mix|playback", re.I)
NOT_SONG = re.compile(r"stem|instrumental|\binst\b|a ?cappella|acapella|vocals? only|click|"
                      r"\bsfx\b|\bvo\b|voice ?over|wild ?track|room ?tone|\bboom\b|\blav\b|zoom\d", re.I)


def pick_master(folder, audio_files, ties=None):
    """Guess the master song in a dropped folder. A file in a Music/Song folder, or named
    master/song/mix, wins; stems, instrumentals, clicks and sound recordings are passed over; of
    several equally likely files, the longest (a full mix, not an edit or a stem) is taken. Those
    equally likely files go in `ties` (longest first), so the clips can vote (vote_song)."""
    if len(audio_files) == 1:
        return audio_files[0]

    def score(p):
        rel = os.path.relpath(p, folder)
        dirs = [d.lower() for d in rel.replace("\\", "/").split("/")[:-1]]
        name = os.path.basename(p)
        sc = 0
        in_music = any(d in ("music", "song", "songs", "master", "playback", "track") for d in dirs)
        if in_music:
            sc += 4
        elif SONG_HINT.search(rel):
            sc += 2
        if SONG_HINT.search(name):
            sc += 1
        if NOT_SONG.search(rel):
            sc -= 5
        # Audio/Music/song.wav is still the song: only a generic audio folder without a music one counts against
        if not in_music and any(d in ("sfx", "sound effects", "sound", "audio", "captured") for d in dirs):
            sc -= 3
        return sc
    scored = sorted(((score(p), p) for p in audio_files), key=lambda sp: -sp[0])
    if not scored or scored[0][0] <= 0:
        return None
    top = [p for sc, p in scored if sc == scored[0][0]]
    if len(top) > 1:
        top.sort(key=lambda p: -probe_audio(p)[0])
        if ties is not None:
            ties[:] = top
            log("Several possible songs: %s. Checking which one the clips were shot to"
                % ", ".join(os.path.basename(p) for p in top))
            return top[0]
        log("Several possible songs: %s. Using the longest, %s (pass --master to pick another)"
            % (", ".join(os.path.basename(p) for p in top), os.path.basename(top[0])))
    return top[0]


MUSIC_DIRS = ("music", "song", "songs", "playback", "master")


VOTE_CLIPS, VOTE_SECONDS = 12, 90


def vote_song(songs, clips):
    """Of several equally likely songs (a Music folder holding the band's other tracks, or a v1 and
    a v2), the one most clips line up with: the first 90 s of up to 12 clips spread through the shoot,
    each voting for the song it matches best. Returns (song, votes) or None when no clip matches any."""
    sample = clips[::max(1, len(clips) // VOTE_CLIPS)][:VOTE_CLIPS]
    idx = []
    for p in songs:
        try:
            idx.append((p, MasterIndex(load_audio(p))))
        except RuntimeError:
            continue
    votes = {p: 0 for p, _ in idx}
    st = Settings()
    for c in sample:
        if not c.duration:
            probe(c)
        if not (c.readable and c.has_audio):
            continue
        try:
            chans = load_channels(c.path, c.audio_layout, limit=VOTE_SECONDS)
        except RuntimeError:
            continue
        best = None
        for _, xc in chans:
            if not len(xc) or float(np.sqrt(np.mean(xc ** 2))) < 10 ** (-60 / 20) or is_timecode(xc):
                continue
            h, t = landmarks(*find_peaks(xc))
            for p, mi in idx:
                e = evaluate(mi, h, t)
                if e is not None and accepted(e, st) and (best is None or e["conf"] > best[0]):
                    best = (e["conf"], p)
        if best:
            votes[best[1]] += 1
    if not votes or max(votes.values()) == 0:
        return None
    top = max(votes.values())
    return next(p for p in songs if votes.get(p) == top), votes


def song_nearby(folder, levels=3, ties=None):
    """The song in a Music folder next to the one dropped, or a level or two up: a card or a day's
    footage dropped on its own (Shoot/Footage/Day 1) with the song in Shoot/Audio/Music."""
    d = os.path.abspath(folder)
    for _ in range(levels):
        up = os.path.dirname(d)
        if up == d or up in ("/", "/Volumes", os.path.expanduser("~")):
            return None
        d = up
        found = []
        for sub in [d] + [os.path.join(d, x) for x in sorted(os.listdir(d)) if os.path.isdir(os.path.join(d, x))]:
            try:
                names = sorted(os.listdir(sub))
            except OSError:
                continue
            for x in names:
                if x.lower() in MUSIC_DIRS and os.path.isdir(os.path.join(sub, x)):
                    found += find_audio(os.path.join(sub, x))
        if found:
            song = pick_master(d, found, ties)
            if song:
                log("Song found next to the folder: %s" % song)
                return song
    return None


def probe_audio(path):
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    info = json.loads(r.stdout or b"{}") if r.returncode == 0 else {}
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    return float((info.get("format") or {}).get("duration") or 0), int(a.get("channels") or 2), \
        int(a.get("sample_rate") or 48000)


def find_clips(clips_dir, master_path, skip_dirs=()):
    master_abs = os.path.abspath(master_path) if master_path else None
    skip = {os.path.abspath(d) for d in skip_dirs}
    out = []
    # dropped a camera folder itself, or clips inside one: the camera is the folder it's in
    here_cam = next((d for d in reversed(os.path.abspath(clips_dir).split(os.sep)) if CAM_FOLDER.match(d)), "")
    for root, dirs, files in os.walk(clips_dir):
        dirs[:] = sorted(d for d in dirs if not skip_dir(d) and os.path.abspath(os.path.join(root, d)) not in skip)
        for f in sorted(files):
            if f.startswith("."):
                continue
            if os.path.splitext(f)[1].lower() not in MEDIA_EXT:
                continue
            p = os.path.join(root, f)
            if os.path.abspath(p) == master_abs:
                continue
            rel = os.path.relpath(p, clips_dir)
            parts = rel.replace("\\", "/").split("/")[:-1]
            # the camera's folder: one named like "A Cam (Mini LF)" at any depth (Footage/Day 1/...),
            # else the first folder under the one dropped
            cam = next((d for d in parts if CAM_FOLDER.match(d)), None) or here_cam or (parts[0] if parts else "")
            out.append(Clip(path=p, rel=rel, top_folder=cam))
    return drop_copies(out)


def drop_copies(clips):
    """The same file copied into two places (a card copied twice, a selects folder) counts once:
    same name and same size. The copy in a camera folder is kept, else the first by path."""
    seen, out, dropped = {}, [], []
    for c in sorted(clips, key=lambda c: (not CAM_FOLDER.match(c.top_folder), c.rel)):
        try:
            key = (os.path.basename(c.path).lower(), os.path.getsize(c.path))
        except OSError:
            key = (c.path,)
        if key in seen:
            dropped.append((c.rel, seen[key]))
            continue
        seen[key] = c.rel
        out.append(c)
    for rel, kept in dropped[:20]:
        log("Skipped %s: a copy of %s" % (rel, kept))
    if len(dropped) > 20:
        log("Skipped %d more copies of files already found" % (len(dropped) - 20))
    return sorted(out, key=lambda c: c.rel)


# ---------------------------------------------------------------- audio + fingerprints

def load_audio(path, stream="a:0"):
    r = run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:" + stream,
             "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-acodec", "pcm_s16le", "-"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:])
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


MAX_CHANNELS = 16


def load_channels(path, layout, limit=None):
    """Every audio channel of the file as its own mono track: [(label, samples)]. Cameras put the
    scratch mic on different channels (an ARRI Mini LF: timecode on 3, mic on 4, 1-2 nearly silent),
    and a downmix buries it, so channels are never mixed before one is chosen."""
    out = []
    for i, ch in enumerate(layout or [1]):
        r = run(["ffmpeg", "-v", "error", "-nostdin", "-i", path] + (["-t", str(limit)] if limit else [])
                + ["-map", "0:a:%d" % i, "-vn",
                 "-ar", str(SR), "-f", "f32le", "-acodec", "pcm_f32le", "-"])
        if r.returncode != 0:
            if out:
                continue
            raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:])
        x = np.frombuffer(r.stdout, dtype=np.float32)
        ch = max(1, ch)
        x = x[:len(x) // ch * ch].reshape(-1, ch)
        for c in range(ch):
            label = ("channel %d" % (c + 1)) if len(layout) <= 1 else ("stream %d channel %d" % (i + 1, c + 1)) \
                if ch > 1 else "channel %d" % (i + 1)
            out.append((label, np.ascontiguousarray(x[:, c])))
            if len(out) >= MAX_CHANNELS:
                return out
    return out


def is_timecode(x):
    """LTC timecode recorded as audio: a square wave at a constant level, switching 1900-4000 times
    a second. It never matches a song; skipping it saves the time of trying."""
    if len(x) < SR:
        return False
    seg = x[:SR * 10]
    rms = float(np.sqrt(np.mean(seg ** 2)))
    if rms <= 0:
        return False
    zc = np.count_nonzero(np.diff(np.signbit(seg))) / (len(seg) / SR)
    if not (1500 < zc < 5000 and float(np.median(np.abs(seg))) / rms > 0.85):
        return False
    # hiss squashed by a limiter also crosses zero that often at a near-constant level; LTC's energy
    # sits in its two tones (half the bit rate and the bit rate, 960-2400 Hz), noise is spread out
    spec = np.abs(np.fft.rfft(seg)) ** 2
    f = np.fft.rfftfreq(len(seg), 1 / SR)
    return float(spec[(f > 600) & (f < 2700)].sum() / (spec.sum() + 1e-12)) > 0.55


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
        res = self.hits(h, t)
        if res is None:
            return None
        offs = res[1]
        base = offs.min()
        return np.bincount(offs - base).astype(np.float64), base

    def hits(self, h, t):
        """Every hash hit as (clip_time, master_time - clip_time), in frames."""
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
        return t[rep], self.t[idx] - t[rep]


def gcc_phat_offset(clip_audio, master, coarse, c0, c1, search=0.12, min_len=2.0):
    """Refine song offset (s) of clip audio start using GCC-PHAT on clip window [c0, c1) s."""
    a0, a1 = int(c0 * SR), int(c1 * SR)
    seg = clip_audio[a0:a1]
    m0 = int(round((c0 + coarse - search) * SR))
    m1 = m0 + len(seg) + int(2 * search * SR)
    if m0 < 0 or m1 > len(master) or len(seg) < SR * min_len:
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
        if off is not None and q > 7:              # chance peaks run 4-6: talk between passes
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
    place_repeats: bool = True      # place clips that fit two copies of a section at the first one


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
        chans = load_channels(clip.path, clip.audio_layout)
    except RuntimeError as e:
        clip.reasons.append(REASON_UNREADABLE)
        clip.notes.append("audio decode failed: %s" % e)
        return
    # the scratch mic: of the channels that carry sound (not silence, not timecode), the one that
    # matches the song best. A wrong channel simply doesn't match.
    loud, levels = [], []
    for label, xc in chans:
        rms = float(np.sqrt(np.mean(xc ** 2))) if len(xc) else 0.0
        levels.append(rms)
        if rms >= 10 ** (-60 / 20) and not is_timecode(xc):
            loud.append((label, xc))
    if not loud:
        rms = max(levels or [0.0])
        clip.reasons.append(REASON_SILENT)
        clip.notes.append("digital silence" if rms < 1e-6 else "audio level %.0f dBFS" % (20 * math.log10(rms))
                          if rms < 10 ** (-60 / 20) else "only timecode on the audio channels")
        return
    best = None
    for label, xc in loud:
        hc, tc_ = landmarks(*find_peaks(xc))
        ec = evaluate(master, hc, tc_)
        score = (ec["conf"], ec["A"]) if ec is not None else (-1, 0)
        if best is None or score > best[0]:
            best = (score, label, xc, hc, tc_, ec)
    _, label, x, h, t, ev = best
    if len(chans) > 1:
        clip.audio_pick = label
        clip.notes.append("scratch audio on %s" % label)
    if ev is None:
        clip.reasons.append(REASON_NO_MATCH)
        return
    # The whole clip is examined in overlapping stretches first: if the song was stopped and
    # restarted, or jumped to another section, each pass is found and placed on its own.
    if split_passes(clip, master, st, h, t, x, 1.0, x):
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
            alt = [("varispeed", xr, landmarks(*find_peaks(xr)))]
            # time-stretched: pitch stays, times scale by k. Undo it with a phase vocoder, and also
            # try peaks picked k times denser in clip time; keep whichever matches better.
            alt.append(("time-stretched", None, landmarks(*find_peaks(unstretch(x, k)))))
            pt, pf = find_peaks(x, max(1, int(round(PEAK_T_NEIGH / k))), int(PEAKS_PER_SEC * k))
            alt.append(("time-stretched", None, landmarks(np.round(pt * k).astype(np.int64), pf)))
            for m_name, xa, (ha, ta) in alt:
                e = evaluate(master, ha, ta)
                if e is not None and (best_alt is None or e["conf"] > best_alt[3]["conf"]):
                    best_alt = (float(frac), m_name, xa, e, ha, ta)
        if best_alt and accepted(best_alt[3], st, extra=st.speed_margin) and \
                best_alt[3]["strength"] >= st.speed_strength and best_alt[3]["conf"] > ev["conf"] + 10:
            speed, mode, xs, ev, h, t = best_alt
            clip.speed, clip.speed_mode = speed, mode
            clip.notes.append("song played at %gx on set (%s); placed at %g%% speed"
                              % (speed, mode, 100 / speed))
            if split_passes(clip, master, st, h, t, x, speed, xs):
                return

    A, R, N, conf = ev["A"], ev["R"], ev["N"], ev["conf"]
    aoff = clip.audio_offset * speed               # audio start offset, in song seconds
    clip.aligned, clip.runner_up = A, R
    coarse = ev["offset"]                           # song time of clip audio sample 0
    clip.runner_up_offset = ev["runner_up_offset"] - aoff if R else None
    clip.confidence = round(conf, 1)
    xs_len = len(xs) / SR if xs is not None else len(x) / SR * speed

    rescued = None
    if (A < st.min_hashes or ev["strength"] < 0.25 or conf < st.threshold) and xs is not None and speed == 1.0:
        # too few landmarks to be sure (a short take, a sparse outro): let the waveform decide
        rescued = waveform_rescue(xs, master, [coarse] + ([ev["runner_up_offset"]] if R else []))
    if rescued is not None:
        coarse, n_ok, n = rescued
        clip.notes.append("placed by waveform: %d of %d windows line up (landmarks %d vs %d by chance)"
                          % (n_ok, n, A, N))
    elif A < st.min_hashes or ev["strength"] < 0.25:
        clip.reasons.append(REASON_NO_MATCH)
        clip.notes.append("best alignment %d landmarks vs %d by chance" % (A, N))
        return
    if conf < st.threshold and rescued is None:
        # Landmarks alone aren't decisive (usually because part of the clip is a repeated chorus).
        # A waveform comparison at both candidate positions settles it when some stretch of the
        # clip matches only at the best one.
        if xs is not None and R and decisive(xs, master, coarse, ev["runner_up_offset"], 0.0, xs_len):
            clip.notes.append("confirmed by waveform check against song %.2fs"
                              % (ev["runner_up_offset"] - aoff))
        elif (R - N) > 0.5 * (A - N):
            first = repeat_pick(xs, master, coarse, ev["runner_up_offset"], 0.0, xs_len) \
                if st.place_repeats else None
            if first is None:
                clip.reasons.append(REASON_AMBIGUOUS)
                clip.notes.append("fits equally at song %.2fs and %.2fs"
                                  % (coarse - aoff, clip.runner_up_offset))
                return
            other = ev["runner_up_offset"] if first == coarse else coarse
            coarse = first
            clip.repeat_alt = other - aoff
            clip.notes.append("%s: placed at the first copy, also fits at song %.2fs"
                              % (REPEAT_NOTE, other - aoff))
        else:
            clip.reasons.append(REASON_LOW_CONF)
            clip.notes.append("best guess song %.2fs" % (coarse - aoff))
            return

    if xs is not None and clip.repeat_alt is None:
        # one pass, but is there song elsewhere in the clip at another position (a false start)?
        res = master.hits(h, t)
        c = int(round(coarse / FRAME_S))
        on = np.sort(res[0][np.abs(res[1] - c) <= 2]) if res is not None else np.zeros(0)
        if len(on) > 2:
            f0, f1 = pass_edges(xs, master, coarse, on[0] * FRAME_S, on[-1] * FRAME_S)
            main = dict(off=coarse, first=f0, last=f1, ev=ev, good=True)
            if emit_parts(clip, master, st, [main], h, t, x, speed, xs):
                return
    offset_audio, clip.drift_ms, clip.refine, ov = refine_offset(xs, master, coarse, 0.0, xs_len)
    if clip.refine.startswith("landmark only"):
        clip.notes.append(clip.refine)
    clip.offset = offset_audio - aoff
    if xs is not None:
        clip.check = check_string(wave_q(xs, master, offset_audio, *ov, drift=clip.drift_ms,
                                         span=ov[1] - ov[0]))
    clip.status = "placed"


REPEAT_NOTE = "repeated section, check which copy"


def repeat_pick(xs, master, off1, off2, lo, hi):
    """For a stretch that fits two places in the song equally: when the audio is really the same at
    both (a pasted chorus), lip sync is right at either, so return the earlier one. None when that
    can't be confirmed from the waveform (then the clip is set aside)."""
    if xs is None:
        return None
    for off in (off1, off2):
        q = wave_q(xs, master, off, *in_song(xs, master, off, lo, hi))
        if len(q) < 2 or sum(v >= WAVE_MATCH for _, v in q) < 0.8 * len(q):
            return None
    return min(off1, off2)


def refine_offset(xs, master, coarse, lo, hi):
    """Sub-frame offset and head-to-tail drift over clip stretch [lo, hi] (xs seconds).
    Returns (song time of xs sample 0 as placed, drift ms or None, how it was refined, the part of
    the stretch inside the song as (start, end), whose middle is where the placement is exact)."""
    if xs is None:          # time-stretched playback: waveforms differ, landmark precision only
        return coarse, None, "landmark only (time-stretched playback, +-1 frame)", (lo, hi)
    ov0 = max(lo, -coarse) + 0.2
    ov1 = min(hi, len(xs) / SR, master.duration - coarse) - 0.2
    track = offset_track(xs, master.audio, coarse, ov0, ov1)
    if track is not None:
        intercept, slope = track
        mid = (ov0 + ov1) / 2                       # centre the drift error across the stretch
        return (intercept + slope * mid, round(slope * (ov1 - ov0) * 1000, 1), "sub-frame refined",
                (ov0, ov1))
    fine, q = gcc_phat_offset(xs, master.audio, coarse, ov0, ov1) if ov1 - ov0 > 3 else (None, 0)
    if fine is not None and q > 6:
        return fine, None, "sub-frame refined", (ov0, ov1)
    return coarse, None, "landmark only (refinement too weak, +-1 frame)", (ov0, ov1)


SONG_TRACK_HEIGHT = 120    # the song's audio track height in Premiere (its default is about 40)
WAVE_WIN, WAVE_HOP = 4.0, 2.0
WAVE_MATCH = 12.0           # GCC-PHAT peak / median: chance ~5, a bar off up to ~9, a real match 15-45


def wave_q(xs, master, off, lo, hi, drift=None, span=None, hop=WAVE_HOP):
    """Waveform match strength of clip audio against the song at song offset `off`, in 4 s
    windows stepping through [lo, hi]. Windows outside the song score 0. Independent of the
    fingerprint landmarks, so it doubles as a check on them. With a measured drift (ms over `span`
    seconds, `off` taken at the middle), the clip is first resampled to the song's speed: even a
    0.1% speed error smears a 4 s window by 4 ms, enough to hide a real match."""
    slope = (drift or 0.0) / 1000.0 / span if drift and span else 0.0
    if abs(slope) > 5e-5:
        mid = (lo + hi) / 2
        fr = fractions.Fraction(1 + slope).limit_denominator(5000)
        xs = signal.resample_poly(xs, fr.numerator, fr.denominator).astype(np.float32)
        off, lo, hi = off - slope * mid, lo * (1 + slope), hi * (1 + slope)
    out = []
    t = lo
    while t + WAVE_WIN <= hi + 1e-6:
        _, q = gcc_phat_offset(xs, master.audio, off, t, t + WAVE_WIN, search=0.06)
        out.append((t, q))
        t += hop
    return out


def in_song(xs, master, off, lo, hi):
    """The part of clip stretch [lo, hi] that lies inside the song at offset `off`."""
    return max(lo, -off) + 0.05, min(hi, len(xs) / SR, master.duration - off) - 0.05


def check_string(q):
    """'14/15': windows inside the song that match (see wave_q)."""
    vals = [v for _, v in q if v > 0]
    return "%d/%d" % (sum(v >= WAVE_MATCH for v in vals), len(vals)) if vals else ""


def doubtful(clips):
    """Placed parts whose waveform lines up in under 70% of its windows: worth a look."""
    return [(c, i, p) for c in clips for i, p in enumerate(c.parts, 1)
            if p.status == "placed" and p.check and p.repeat_alt is None and
            int(p.check.split("/")[0]) < 0.7 * int(p.check.split("/")[1])]


def decisive(xs, master, off, rival, lo, hi):
    """True when some stretch of the clip matches the song only at `off`, and none only at `rival`."""
    qa = dict(wave_q(xs, master, off, lo, hi, hop=1.0))
    qr = dict(wave_q(xs, master, rival, lo, hi, hop=1.0))
    # a window scoring far above chance (15+) counts double: one of those is already decisive
    a_only = sum(1 + (a >= 15) for k, a in qa.items() if a >= WAVE_MATCH and a >= 2 * qr.get(k, 0))
    r_only = sum(1 for k, r in qr.items() if r >= WAVE_MATCH and r >= 2 * qa.get(k, 0))
    return a_only >= 2 and r_only == 0


# ---------------------------------------------------------------- restarted / jumping takes

PASS_BIN = int(round(1.0 / FRAME_S))     # 1 s of fingerprint frames
MIN_PASS_S = 3.0                          # shorter bits of song (a false start) aren't split out
PASS_KAPPA = 1.0                          # landmark hits per second a pass must beat to count
PASS_SWITCH = 6.0                         # cost of changing offset, in hits


def pass_candidates(tc, off):
    """Song offsets that dominate some 5 s stretch of the clip."""
    found = {}
    win = 5 * PASS_BIN
    for s in range(0, int(tc.max()) + 1 if len(tc) else 0, PASS_BIN):
        sel = (tc >= s) & (tc < s + win)
        if sel.sum() < 10:
            continue
        o = off[sel]
        base = o.min()
        hist = np.convolve(np.bincount(o - base), np.ones(3), "same")
        k = int(hist.argmax())
        if hist[k] >= 10:
            found[k + base] = max(found.get(k + base, 0), hist[k])
    out = []
    for c, _ in sorted(found.items(), key=lambda kv: -kv[1]):
        if all(abs(c - o) > 5 for o in out):
            out.append(c)
    return out[:64]        # a long take can hold a dozen passes, each with its chorus copies


def find_passes(master, h, t, single=False):
    """Follow which song offset the clip agrees with, second by second, across the whole clip.

    A Viterbi path over (candidate offsets + 'no song') scores each second by how many landmarks
    agree with that offset, and charges for every change of offset, so a repeated chorus (which
    agrees with two offsets at once) doesn't flip a pass, while a real restart or jump does.
    Returns ([dict(c=offset frames, first, last, r0, r1 frames)] in clip order, hit times, hit offsets).
    With `single`, one candidate offset is enough (a stretch searched on its own)."""
    res = master.hits(h, t)
    if res is None:
        return [], None, None
    tc, off = res
    cands = pass_candidates(tc, off)
    if len(cands) < (1 if single else 2):
        return [], tc, off
    nb = int(tc.max()) // PASS_BIN + 1
    S = np.zeros((len(cands) + 1, nb))            # row 0: no song
    for i, c in enumerate(cands, 1):
        on = np.abs(off - c) <= 2
        S[i] = np.minimum(np.bincount(tc[on] // PASS_BIN, minlength=nb)[:nb], 12) - PASS_KAPPA
    K = len(S)
    V, back = S[:, 0].copy(), np.zeros((K, nb), int)
    for b in range(1, nb):
        best = int(V.argmax())
        jump = V[best] - PASS_SWITCH
        back[:, b] = np.where(V >= jump, np.arange(K), best)
        V = np.maximum(V, jump) + S[:, b]
    path = np.zeros(nb, int)
    path[-1] = int(V.argmax())
    for b in range(nb - 1, 0, -1):
        path[b - 1] = back[path[b], b]
    runs = []
    for b, s in enumerate(path):
        if not s:
            continue
        c = cands[s - 1]
        if runs and abs(runs[-1][0] - c) <= 13:   # same pass (a gap, or a few frames of drift)
            runs[-1][2] = b + 1
        else:
            runs.append([c, b, b + 1])
    out = []
    for c, b0, b1 in runs:
        on = np.sort(tc[(np.abs(off - c) <= 2) & (tc >= b0 * PASS_BIN) & (tc < b1 * PASS_BIN)])
        # where the pass is first / last heard: ignore stray chance hits, want 3 within a second
        dense = np.nonzero(on[2:] - on[:-2] <= PASS_BIN)[0] if len(on) > 2 else []
        if len(dense):
            out.append(dict(c=c, first=int(on[dense[0]]), last=int(on[dense[-1] + 2]),
                            r0=b0 * PASS_BIN, r1=b1 * PASS_BIN))
    return out, tc, off


def pass_edges(xs, master, off, first, last, rivals=()):
    """Where a pass's song really starts and stops (xs seconds). Landmark hits only bracket it to a
    second or so (anchors pair with peaks up to 1.5 s later, and stray chance hits pile up in gaps),
    so this slides a 1 s waveform window in 0.1 s steps across each edge. On test takes the first
    matching window begins 0.4 s before the song does and the last one ends 0.5 s after it stops.
    With `rivals` (the other passes' offsets), a window only counts when it lines up 1.5x better here
    than at any of them: passes a bar apart share the drum pattern."""
    def q_at(o, t):
        return gcc_phat_offset(xs, master.audio, o, t, t + 1.0, search=0.06, min_len=0.9)[1]

    def hit(t):
        q = q_at(off, t)
        return q >= WAVE_MATCH and all(q >= 1.5 * q_at(r, t) for r in rivals)
    n = len(xs) / SR
    starts = [t for t in np.arange(max(0.0, first - 2.0), min(first + 4.0, n - 1.0), 0.1) if hit(t)]
    ends = [t for t in np.arange(max(0.0, last - 4.0), min(last + 3.0, n - 1.0), 0.1) if hit(t)]
    if not starts or not ends:
        # the landmarks' first or last hit was chance (a drummer noodling between passes lines up
        # with the song now and then): walk in from that side to where the waveform really matches
        coarse = [t for t in np.arange(first, max(first, last - 1.0), 0.5) if hit(t)]
        if not coarse:
            return first, last
        if not starts:
            starts = [t for t in np.arange(max(0.0, coarse[0] - 1.0), coarse[0] + 0.05, 0.1) if hit(t)]
        if not ends:
            ends = [t for t in np.arange(coarse[-1], min(coarse[-1] + 1.0, n - 1.0) + 0.05, 0.1) if hit(t)]
    return starts[0] + 0.4, ends[-1] + 0.5


def continues(xs, master, tc, off, a, b):
    """How well pass `a`'s song position also explains pass `b`'s stretch (0-1). High means b is a
    repeated section that only looked like a jump: the same pass carries on through it."""
    if xs is not None:
        lo, hi = in_song(xs, master, a["off"], b["first"], b["last"])
        q = [v for _, v in wave_q(xs, master, a["off"], lo, hi, hop=1.0)]
        if not q and hi - lo >= 2.0:                 # shorter than one window: test it whole
            q = [gcc_phat_offset(xs, master.audio, a["off"], lo, hi, search=0.06)[1]]
        return sum(v >= WAVE_MATCH for v in q) / len(q) if q else 0.0
    f0, f1 = b["first"] / FRAME_S, b["last"] / FRAME_S
    span = (tc >= f0) & (tc <= f1)
    own = (np.abs(off[span] - b["c"]) <= 2).sum()
    return min(1.0, (np.abs(off[span] - a["c"]) <= 2).sum() / own) if own else 0.0


def split_passes(clip, master, st, h, t, x, speed, xs):
    """Split a take in which the song was played more than once (stopped and restarted, paused,
    or jumped to another section) into one Part per pass. Returns False, touching nothing,
    when the clip holds a single pass."""
    raw, tc, off = find_passes(master, h, t)
    if len(raw) < 2:
        return False
    for p in raw:
        p["off"] = p["c"] * FRAME_S
        p["first"], p["last"] = p["first"] * FRAME_S, p["last"] * FRAME_S
        if xs is not None:
            p["first"], p["last"] = pass_edges(xs, master, p["off"], p["first"], p["last"])
    raw = [p for p in raw if p["last"] - p["first"] >= MIN_PASS_S]    # stray bits, false starts
    # a repeated chorus can make one pass look like two: merge when either side just continues
    merged = True
    while merged and len(raw) > 1:
        merged = False
        for i in range(len(raw) - 1):
            a, b = raw[i], raw[i + 1]
            ab, ba = continues(xs, master, tc, off, a, b), continues(xs, master, tc, off, b, a)
            if max(ab, ba) >= 0.6:           # keep the position that explains the other better
                keep = a if (ab, a["last"] - a["first"]) > (ba, b["last"] - b["first"]) else b
                if min(ab, ba) >= 0.6 and xs is not None:   # both do: whichever some stretch fits only
                    lo, hi = min(a["first"], b["first"]), max(a["last"], b["last"])
                    keep = a if decisive(xs, master, a["off"], b["off"], lo, hi) else \
                        b if decisive(xs, master, b["off"], a["off"], lo, hi) else keep
                keep.update(first=min(a["first"], b["first"]), last=max(a["last"], b["last"]),
                            r0=a["r0"], r1=b["r1"])
                raw[i:i + 2] = [keep]
                merged = True
                break

    passes = []
    for p in raw:
        sel = (t >= p["r0"]) & (t < p["r1"])
        e = evaluate(master, h[sel], t[sel])
        if e is None or e["A"] < st.min_hashes or e["strength"] < 0.25:
            continue
        o = p["off"]
        if abs(e["offset"] - o) > 0.1:                # another offset fits this stretch better
            e = dict(e, runner_up_offset=e["offset"], offset=o, conf=0.0)
        lo, hi = p["first"], p["last"]
        q = wave_q(xs, master, o, *in_song(xs, master, o, lo, hi)) if xs is not None else []
        good = accepted(e, st)
        if not good and xs is not None and e["R"]:
            good = decisive(xs, master, o, e["runner_up_offset"], lo, hi)
        if good or (q and sum(v >= WAVE_MATCH for _, v in q) >= max(1, len(q) / 3)):
            passes.append(dict(p, ev=e, good=good))
    if len(passes) < 2:
        return False
    return emit_parts(clip, master, st, passes, h, t, x, speed, xs)


REASON_SHORT = "short burst of song (false start?)"
REASON_BETWEEN = "between plays of the song"
STRAY_LONG_S = 20.0   # song this long outside the found passes is a performance, not a false start
TAIL_KEEP_S = 8.0     # a part carries on this long after its song stops...
GAP_PART_S = 20.0     # ...and a longer stretch than this after that is a part of its own
HEAD_KEEP_S = 12.0    # the first part keeps up to this much roll before its song


def song_search(xs, master, lo, hi):
    """Best song offset for clip stretch [lo, hi] (xs seconds) by phase correlation against the whole
    song: slower than landmarks but it still finds a few seconds of song that are too faint or too
    short for them. Returns the offset (song time of xs sample 0)."""
    seg = xs[int(lo * SR):int(hi * SR)]
    n = 1 << int(math.ceil(math.log2(len(master.audio) + len(seg))))
    X = np.fft.rfft(master.audio, n) * np.conj(np.fft.rfft(seg, n))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    X[(freqs < 120) | (freqs > 4500)] = 0
    X /= np.maximum(np.abs(X), 1e-12)
    cc = np.fft.irfft(X, n)[:len(master.audio)]
    return int(np.argmax(cc)) / SR - lo


PHASE_EXCL_S = 0.25        # the runner-up peak is looked for this far or more from the best one
PHASE_AGREE = 1.35         # best/runner-up ratio enough when the landmarks' best guess lands on the same spot
PHASE_ALONE = 2.0          # ... and without them (the band's other song reaches 1.5 by chance on synthetic takes)
PHASE_FRAME_S = 0.045      # "the same spot": within about a frame


def phase_search(xs, master, lo, hi):
    """(offset, ratio): the best song offset for clip stretch [lo, hi] by phase correlation of the
    whole stretch against the whole song (see song_search), and how far that peak stands above the
    best one anywhere else in the song (runner-up at least PHASE_EXCL_S away). Unlike the 4 s window
    count, the whole stretch is weighed at once, which is what separates real camera audio (room,
    band, crowd over the playback) from chance: chance stays near 1.0-1.25."""
    seg = xs[int(max(0.0, lo) * SR):int(hi * SR)]
    if len(seg) < SR:
        return None, 0.0
    n = 1 << int(math.ceil(math.log2(len(master.audio) + len(seg))))
    X = np.fft.rfft(master.audio, n) * np.conj(np.fft.rfft(seg, n))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    X[(freqs < 150) | (freqs > 4000)] = 0
    X /= np.maximum(np.abs(X), 1e-12)
    cc = np.fft.irfft(X, n)
    # lags cover the clip starting before the song too (negative offsets wrap to the end)
    cc = np.concatenate([cc[n - len(seg):], cc[:len(master.audio)]])
    k = int(np.argmax(cc))
    peak = float(cc[k])
    ex = int(PHASE_EXCL_S * SR)
    rest = np.concatenate([cc[:max(0, k - ex)], cc[k + ex + 1:]])
    second = float(rest.max()) if len(rest) else 0.0
    off = (k - len(seg)) / SR - max(0.0, lo)
    return off, (peak / second if second > 0 else 0.0)


def stray_song(xs, master, st, h, t, lo, hi, known):
    """Song audio in clip stretch [lo, hi] (outside every known pass) at a position of its own, e.g.
    a false start before the real take. Returns a pass dict, or None when the stretch holds no song
    that lines up anywhere else (talk, room noise, the tail of a known pass)."""
    if hi - lo < 2.0:
        return None
    sel = (t >= lo / FRAME_S) & (t < hi / FRAME_S)
    e = evaluate(master, h[sel], t[sel]) if sel.any() else None
    cands = [song_search(xs, master, lo, hi)] + ([e["offset"]] if e is not None and e["A"] >= 6 else [])

    def q1(o, w):
        return gcc_phat_offset(xs, master.audio, o, w, w + 1.0, search=0.06, min_len=0.9)[1]
    best = None
    for o in cands:
        if any(abs(o - k) < 0.08 for k in known):     # beyond that, windows at k miss it (search 0.06)
            continue
        wins = [w for w in np.arange(lo, hi - 1.0 + 1e-6, 0.5)
                if q1(o, w) >= WAVE_MATCH and all(q1(o, w) >= 1.5 * q1(k, w) for k in known)]
        if len(wins) >= 2 and (best is None or len(wins) > len(best[1])):
            best = (o, wins)
    if best is None:
        return None
    o, wins = best
    first, last = wins[0] + 0.4, wins[-1] + 0.5
    sel = (t >= first / FRAME_S) & (t < last / FRAME_S)
    e = evaluate(master, h[sel], t[sel]) if sel.any() else None
    if e is None:
        e = dict(A=0, R=0, N=0.0, offset=o, runner_up_offset=o, conf=0.0, strength=0.0)
    good = abs(e["offset"] - o) < 0.1 and accepted(e, st) and last - first >= MIN_PASS_S
    if not good and last - first >= STRAY_LONG_S:
        # a whole performance the landmarks miss (a worn tape, the band louder than the playback):
        # the waveform lining up here, and better than at every other pass, through a third of it
        # or more is proof enough
        n = len(np.arange(first, last - 1.0 + 1e-6, 0.5))
        good = len(wins) >= max(6, n / 3)
        if good:
            e = dict(e, offset=o)
    return dict(off=o, first=first, last=last, ev=e, good=good, stray=last - first < STRAY_LONG_S)


def gap_passes(xs, master, st, h, t, lo, hi, known):
    """Passes inside clip stretch [lo, hi] (xs seconds, outside every pass found so far), searched
    with only that stretch's landmarks, so a play the louder ones outvoted in the whole-clip search
    (an action camera rolling through a dozen plays) gets a say. Each is kept only when its landmarks
    or its waveform confirm it."""
    if hi - lo < MIN_PASS_S + 1:
        return []
    sel = (t >= lo / FRAME_S) & (t < hi / FRAME_S)
    if sel.sum() < 20:
        return []
    raw, _, _ = find_passes(master, h[sel], t[sel], single=True)
    out = []
    for p in raw:
        o = p["c"] * FRAME_S
        if any(abs(o - k) < 0.08 for k in known):
            continue
        first, last = pass_edges(xs, master, o, p["first"] * FRAME_S, p["last"] * FRAME_S, known)
        first, last = max(first, lo), min(last, hi)
        if last - first < MIN_PASS_S:
            continue
        s2 = (t >= first / FRAME_S) & (t < last / FRAME_S)
        e = evaluate(master, h[s2], t[s2]) if s2.any() else None
        if e is None:
            e = dict(A=0, R=0, N=0.0, offset=o, runner_up_offset=o, conf=0.0, strength=0.0)
        if abs(e["offset"] - o) > 0.1:
            e = dict(e, runner_up_offset=e["offset"], offset=o, conf=0.0)
        q = wave_q(xs, master, o, *in_song(xs, master, o, first, last))
        n_ok = sum(v >= WAVE_MATCH for _, v in q)
        good = accepted(e, st) or (len(q) >= 3 and n_ok >= max(3, len(q) / 3))
        if good or n_ok >= 2:
            out.append(dict(off=o, first=first, last=last, ev=e, good=good, r0=p["r0"], r1=p["r1"]))
    return out


GAP_CHUNK_S = 60.0


def gap_rescue(xs, master, st, h, t, lo, hi, known):
    """A whole performance in clip stretch [lo, hi] that neither the stretch's landmarks nor one phase
    correlation of the whole stretch found (a DJI rolling through a dozen plays with the band louder
    than the playback). The stretch is correlated against the song a minute at a time; a position is
    kept when the waveform lines up in most 4 s windows across 20 s or more, clearly better than at
    any other position found and at every known pass, and the landmarks there don't point elsewhere."""
    if hi - lo < STRAY_LONG_S:
        return None
    chunk = min(GAP_CHUNK_S, master.duration)
    cands = []
    for a in np.arange(lo, max(lo, hi - chunk) + 1e-6, chunk / 2):
        o = song_search(xs, master, a, min(hi, a + chunk))
        if all(abs(o - c) > 0.08 for c in cands) and all(abs(o - k) >= 0.08 for k in known):
            cands.append(o)
    scored = []
    for o in cands:
        a, b = in_song(xs, master, o, lo, hi)
        if b - a < STRAY_LONG_S:
            continue
        q = wave_q(xs, master, o, a, b)
        ok = [w for w, v in q if v >= WAVE_MATCH]
        if len(ok) < 6:
            continue
        first, last = ok[0], ok[-1] + WAVE_WIN
        inside = [v for w, v in q if first <= w <= last - WAVE_WIN]
        k = sum(v >= WAVE_MATCH for v in inside)
        if last - first >= STRAY_LONG_S and k >= 0.5 * len(inside):
            scored.append((k, o, first, last, len(inside)))
    if not scored:
        return None
    scored.sort(reverse=True)
    k, o, first, last, n = scored[0]
    if len(scored) > 1 and scored[1][0] * 1.5 > k:
        return None                     # two positions line up about as well: a repeated section
    for kn in known:                    # a known pass's position must not fit these windows as well
        kk = sum(v >= WAVE_MATCH for _, v in wave_q(xs, master, kn, first, last))
        if kk * 2 > k:
            return None
    sel = (t >= first / FRAME_S) & (t < last / FRAME_S)
    e = evaluate(master, h[sel], t[sel]) if sel.any() else None
    if e is not None and accepted(e, st) and abs(e["offset"] - o) > 0.1:
        return None                     # the landmarks here are sure of somewhere else
    if e is None:
        e = dict(A=0, R=0, N=0.0, offset=o, runner_up_offset=o, conf=0.0, strength=0.0)
    e = dict(e, offset=o)
    return dict(off=o, first=first, last=last, ev=e, good=True, stray=False,
                rescued="found by waveform: %d of %d windows line up" % (k, n))


def waveform_rescue(xs, master, guesses):
    """Where the landmarks are too few to decide (a 20 s take, an outro that fingerprints badly),
    compare the waveform: at each landmark guess, and at the best position of a phase correlation of
    the whole clip against the whole song. Returns (offset, windows that line up, windows) when one
    position lines up through at least half of the clip (3 windows or more) and twice as well as any
    other, else None."""
    n = len(xs) / SR
    if n < WAVE_WIN + 1:
        return None
    cands = []
    whole = song_search(xs, master, 0.0, n)
    for o in list(guesses) + [whole]:
        if all(abs(o - c) > 0.08 for c in cands):
            cands.append(o)
    hop = 1.0 if n < 40 else WAVE_HOP
    scored = []
    for o in cands:
        q = [v for _, v in wave_q(xs, master, o, *in_song(xs, master, o, 0.0, n), hop=hop)]
        scored.append((sum(v >= WAVE_MATCH for v in q), len(q), o))
    scored.sort(reverse=True)
    k, m, o = scored[0]
    rival = max((s[0] for s in scored[1:]), default=0)
    # the landmarks' best guess and a phase correlation of the whole clip, found independently,
    # landing on the same spot: then a quarter of the windows lining up is enough (a short FX3
    # take whose scratch audio is mostly the band). Anywhere else lining up half as well still loses.
    agree = bool(guesses) and abs(o - guesses[0]) < 0.08 and abs(o - whole) < 0.08
    if k >= 3 and k >= (0.25 if agree else 0.5) * m and k >= 2 * rival:
        return o, k, m
    return None


def emit_parts(clip, master, st, passes, h, t, x, speed, xs):
    """Cut the clip into one Part per pass. Before cutting, each pass's edges are tightened against
    its neighbours and the stretches outside every pass are searched for song audio of their own
    (a false start), which becomes its own part, so no part carries song that doesn't line up at its
    offset. Returns False when that leaves a single pass (nothing to split)."""
    xs_len = len(xs) / SR if xs is not None else len(x) / SR * speed
    passes.sort(key=lambda p: p["first"])
    if xs is not None:
        for p in passes:
            rivals = [q["off"] for q in passes if q is not p]
            if rivals:
                p["first"], p["last"] = pass_edges(xs, master, p["off"], p["first"], p["last"], rivals)
        known = [p["off"] for p in passes]
        bounds = [0.0] + [v for p in passes for v in (p["first"], p["last"])] + [xs_len]
        gaps = list(zip(bounds[::2], bounds[1::2]))
        rounds = 0
        while gaps and rounds < 200:     # a long gap can hold several: search what's left around each
            rounds += 1
            lo, hi = gaps.pop()
            found = gap_passes(xs, master, st, h, t, lo, hi, known)
            if not any(p["good"] for p in found):
                # nothing the landmarks can vouch for: search the stretch's waveform against the whole
                # song, and don't let an unconfirmed landmark guess keep a real performance out
                sp = stray_song(xs, master, st, h, t, lo, hi, known)
                if sp and (sp["good"] or not found):
                    found = [sp]
                if not any(p["good"] for p in found):
                    gr = gap_rescue(xs, master, st, h, t, lo, hi, known)
                    if gr:
                        found = [gr]
            found.sort(key=lambda p: p["first"])
            edge = lo
            for sp in found:
                passes.append(sp)
                known.append(sp["off"])
                gaps.append((edge, sp["first"]))
                edge = sp["last"]
            if found:
                gaps.append((edge, hi))
        passes.sort(key=lambda p: p["first"])
    if len(passes) < 2:
        return False

    # cut where the next pass's song starts: in a gap, half a second ahead of it; in a straight
    # jump (no gap), right at it
    cuts = [0.0]
    for a, b in zip(passes, passes[1:]):
        gap = b["first"] - a["last"]
        cuts.append(b["first"] - min(0.5, gap / 2) if gap > 0 else (a["last"] + b["first"]) / 2)
    cuts.append(xs_len)
    # a part runs on past its song only briefly: a long stretch after it becomes a part of its own,
    # left unplaced, so a play the matcher couldn't hear (the band drowning the playback out) never
    # rides along at the previous play's song position
    segs = []
    for i, p in enumerate(passes):
        lo, hi = cuts[i], cuts[i + 1]
        if i == 0 and xs is not None and p["first"] - lo > HEAD_KEEP_S + 8.0:
            segs.append((lo, p["first"] - HEAD_KEEP_S, None))
            lo = p["first"] - HEAD_KEEP_S
        if xs is not None and hi - p["last"] > TAIL_KEEP_S + GAP_PART_S:
            segs += [(lo, p["last"] + TAIL_KEEP_S, p), (p["last"] + TAIL_KEEP_S, hi, None)]
        else:
            segs.append((lo, hi, p))
    aoff = clip.audio_offset * speed
    to_video = lambda s: min(clip.duration or s, max(0.0, s / speed + clip.audio_offset))
    clip.split = True
    for lo, hi, p in segs:
        if p is None:
            part = Part(to_video(lo), to_video(hi), status="not placed", reason=REASON_BETWEEN)
            part.notes.append("no song lines up in this stretch of the take")
            clip.parts.append(part)
            continue
        e = p["ev"]
        part = Part(to_video(lo), to_video(hi), confidence=round(e["conf"], 1), aligned=e["A"],
                    runner_up=e["R"])
        # refine and check over the stretch where this pass's song is heard, not the whole part:
        # a long take can hold minutes of talk between passes, which would swamp the drift fit
        hlo, hhi = (max(lo, p["first"] - 0.5), min(hi, p["last"] + 0.5)) if xs is not None else (lo, hi)
        if p["good"]:
            o, part.drift_ms, part.refine, ov = refine_offset(xs, master, p["off"], hlo, hhi)
            part.offset = o - aoff
            part.status = "placed"
            if xs is not None:
                part.check = check_string(wave_q(xs, master, o, *ov, drift=part.drift_ms,
                                                 span=ov[1] - ov[0]))
                k, m = (int(v) for v in part.check.split("/")) if part.check else (0, 0)
                if m >= 4 and k < 0.2 * m:
                    # the waveform doesn't back this position up: search the stretch on its own
                    o2 = song_search(xs, master, hlo, hhi)
                    q2 = [v for _, v in wave_q(xs, master, o2, *in_song(xs, master, o2, hlo, hhi))]
                    k2 = sum(v >= WAVE_MATCH for v in q2)
                    if abs(o2 - o) > 0.08 and k2 >= max(3, 0.5 * len(q2)) and k2 >= 2 * k + 2:
                        o, part.drift_ms, part.refine, ov = refine_offset(xs, master, o2, hlo, hhi)
                        part.offset = o - aoff
                        part.check = check_string(wave_q(xs, master, o, *ov, drift=part.drift_ms,
                                                         span=ov[1] - ov[0]))
                        part.notes.append("moved by waveform check (landmarks pointed %.1f s away)" % (p["off"] - o))
                    elif k == 0:
                        part.status, part.offset = "not placed", None
                        part.reason = REASON_LOW_CONF
                        part.notes.append("no window of the waveform lines up at song %.1fs" % (o + p["first"]))
            if part.refine.startswith("landmark only"):
                part.notes.append(part.refine)
            if p.get("rescued"):
                part.notes.append(p["rescued"])
        else:
            part.status = "not placed"
            ambiguous = e["R"] and (e["R"] - e["N"]) > 0.5 * (e["A"] - e["N"])
            first = repeat_pick(xs, master, p["off"], e["runner_up_offset"], hlo, hhi) \
                if ambiguous and st.place_repeats else None
            if first is not None:
                other = e["runner_up_offset"] if first == p["off"] else p["off"]
                o, part.drift_ms, part.refine, ov = refine_offset(xs, master, first, hlo, hhi)
                part.offset, part.status, part.repeat_alt = o - aoff, "placed", other - aoff
                part.check = check_string(wave_q(xs, master, o, *ov, drift=part.drift_ms, span=ov[1] - ov[0]))
                part.notes.append("%s: placed at the first copy, also fits at song %.1fs"
                                  % (REPEAT_NOTE, other + p["first"]))
            elif p.get("stray"):
                part.reason = REASON_SHORT
                part.notes.append("%.1f s of song at song %.1fs" % (p["last"] - p["first"], p["off"] + p["first"]))
            elif ambiguous:
                part.reason = REASON_AMBIGUOUS
                part.notes.append("this pass starts at song %.1fs or %.1fs (repeated section)"
                                  % (p["off"] + p["first"], e["runner_up_offset"] + p["first"]))
            else:
                part.reason = REASON_LOW_CONF
                part.notes.append("this pass likely starts at song %.1fs" % (p["off"] + p["first"]))
        clip.parts.append(part)
    placed = [p for p in clip.parts if p.status == "placed"]
    clip.notes.append("song restarts in this take: %d passes" % len(passes))
    if placed:
        first = placed[0]
        clip.status = "placed"
        clip.offset, clip.confidence, clip.drift_ms = first.offset, first.confidence, first.drift_ms
        clip.aligned, clip.runner_up, clip.refine = first.aligned, first.runner_up, first.refine
    else:
        heard = [p for p in clip.parts if p.reason != REASON_BETWEEN] or clip.parts
        clip.reasons.append(heard[0].reason or REASON_LOW_CONF)
        clip.confidence = heard[0].confidence
    return True


SPEEDS = [1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]


def unstretch(x, k, n=1024, hop=256):
    """Slow audio down k times keeping its pitch (phase vocoder): undoes a time-stretched playback
    (VLC, phones, most DAWs) so the song's rhythm lines up with the master again."""
    _, _, Z = signal.stft(x, nperseg=n, noverlap=n - hop, boundary=None, padded=False)
    if Z.shape[1] < 3:
        return x
    steps = np.arange(0, Z.shape[1] - 1, 1.0 / k)
    j = steps.astype(int)
    fr = (steps - j)[None, :]
    mag = np.abs(Z)
    ph = np.angle(Z)
    omega = 2 * np.pi * hop * np.arange(Z.shape[0]) / n
    dp = np.diff(ph, axis=1) - omega[:, None]
    dp -= 2 * np.pi * np.round(dp / (2 * np.pi))
    inc = omega[:, None] + dp[:, j]                     # true phase advance per hop at each step
    acc = ph[:, :1] + np.concatenate([np.zeros((len(omega), 1)), np.cumsum(inc[:, :-1], axis=1)], axis=1)
    out = ((1 - fr) * mag[:, j] + fr * mag[:, j + 1]) * np.exp(1j * acc)
    with warnings.catch_warnings():        # unpadded edges: the first and last half window are faint
        warnings.simplefilter("ignore")
        _, y = signal.istft(out, nperseg=n, noverlap=n - hop, boundary=False)
    return y.astype(np.float32)


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

def folder_camera(name):
    """(letter, camera name) from a camera folder: "C Cam (Action 4.1)" -> ("C", "Action 4.1"),
    "Camera B" -> ("B", ""); None for any other folder."""
    m = CAM_FOLDER.match(name or "")
    if not m:
        return None
    paren = re.search(r"\(([^)]*)\)\s*$", name)
    return (m.group(1) or m.group(2) or m.group(3)).upper(), (paren.group(1).strip() if paren else "")


def camera_letter_hint(clip):
    """The camera letter the shoot's own folders give ("A Cam (Mini LF)", "Camera B"), else the reel."""
    m = CAM_FOLDER.match(clip.top_folder)
    if m:
        return (m.group(1) or m.group(2) or m.group(3)).upper()
    if clip.reel_letter:
        return clip.reel_letter
    m = re.match(r"^([A-Z])\d{3}$", clip.top_folder, re.I)
    return m.group(1).upper() if m else ""


def assign_cameras(clips, group_by, prior=None, prior_folders=None):
    """Camera key and letter per clip. `prior` ({key: [letter, model]}) holds the cameras of earlier
    runs on the same project (cards added later): they keep their letters."""
    prior = prior or {}
    for c in clips:
        model = c.model or "Unknown camera"
        if group_by == "folder":
            c.camera_key = c.top_folder or model
        elif group_by == "model":
            c.camera_key = model
        else:
            # a folder named for the camera decides, whatever the files say: "C Cam (Action 4.1)" is
            # C Cam, in every Day folder, even when some of its files read DJI and others don't
            f = folder_camera(c.top_folder)
            if f:
                c.camera_key = "Camera folder " + f[0]
                continue
            ident = (("serial " + c.serial) if c.serial else ("reel " + c.reel_letter) if c.reel_letter
                     else ("folder " + c.top_folder) if c.top_folder else "")
            c.camera_key = model + (" / " + ident if ident else "")
    # clips with no readable model (e.g. an unreadable raw file) join the camera they were filed with
    known = [c for c in clips if c.model]
    for c in clips:
        if c.model or c.camera_key.startswith("Camera folder "):
            continue
        mates = [k for k in known if (c.reel_letter and k.reel_letter == c.reel_letter) or
                 (c.top_folder and k.top_folder == c.top_folder)]
        if mates:
            c.camera_key = collections.Counter(k.camera_key for k in mates).most_common(1)[0][0]
        elif (prior_folders or {}).get(c.top_folder):         # filed with a camera from an earlier run
            c.camera_key = prior_folders[c.top_folder]
    groups = collections.OrderedDict()
    for c in sorted(clips, key=lambda c: c.rel):
        groups.setdefault(c.camera_key, []).append(c)
    hints = {}
    for key, cl in groups.items():
        letters = collections.Counter(camera_letter_hint(c) for c in cl if camera_letter_hint(c))
        hints[key] = letters.most_common(1)[0][0] if letters else ""
    used, labels = {v[0] for v in prior.values()}, {}
    for key in groups:                       # camera folders keep their own letter
        if key.startswith("Camera folder "):
            labels[key] = key[-1]
    used |= set(labels.values())
    for key in groups:
        if key in labels:
            continue
        if key in prior:
            labels[key] = prior[key][0]
        else:           # same model and letter as a camera already in the project: the same camera
            same = [v[0] for v in prior.values() if v[0] == hints[key] and v[1] == (groups[key][0].model or "")]
            if same:
                labels[key] = same[0]
    hinted = collections.Counter(h for k, h in hints.items() if h and k not in labels)
    for key in sorted(groups, key=lambda k: (hints[k] == "", hints[k], k)):   # unique hints first
        if key not in labels and hints[key] and hinted[hints[key]] == 1 and hints[key] not in used:
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
# A Iris, B Mango, C Rose (Jake's), then colors that read apart in Premiere's label list: Caribbean
# and Forest are both green there, so D and E are Yellow and Cerulean
CAMERA_LABELS = ["Iris", "Mango", "Rose", "Yellow", "Cerulean", "Caribbean", "Lavender", "Magenta",
                 "Forest", "Tan", "Violet", "Purple", "Blue", "Teal", "Green", "Brown"]


def camera_label(letter):
    return CAMERA_LABELS[(ord(letter) - ord("A")) % len(CAMERA_LABELS)]


def add_labels(parent, label):
    if label:
        lb = sub(parent, "labels")
        sub(lb, "label2", label)


def premiere_audio(layout, pick):
    """(audio clips Premiere makes of a file, which one holds the sync channel). Premiere keeps a
    mono or stereo stream as one clip and splits a stream of 3+ channels into mono clips; an ARRI
    Mini LF's 5 mono streams are 5 clips, its scratch mic the 4th. pick: load_channels' label."""
    layout = [max(1, n) for n in (layout or [1])]
    first, n = [], 0
    for ch in layout:
        first.append(n)
        n += 1 if ch <= 2 else ch
    m = re.match(r"^(?:stream (\d+) )?channel (\d+)$", pick or "")
    if not m:
        return n, 0
    if m.group(1):
        i, c = int(m.group(1)) - 1, int(m.group(2)) - 1
    elif len(layout) <= 1:
        i, c = 0, int(m.group(2)) - 1
    else:
        i, c = int(m.group(2)) - 1, 0
    if i >= len(layout):
        return n, 0
    return n, first[i] + 1 + (c if layout[i] > 2 else 0)


def audio_track_channels(layout):
    """Channels in each audio clip Premiere makes of a file, from its streams' channel counts
    (see premiere_audio): [1, 1, 1, 1, 1] for a Mini LF, [2] for most cameras, [1, 1, 1, 1] for
    one 4-channel stream."""
    out = []
    for n in (max(1, n) for n in (layout or [2])):
        out += [n] if n <= 2 else [1] * n
    return out


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
    par: float = 1.0
    rotation: int = 0
    audio_tracks: int = 1        # audio clips Premiere makes of the file (a mono stream each, or a stereo pair)
    audio_pick: int = 0          # which of them holds the channel the sync used (1-based; 0: not known)
    audio_layout: list = field(default_factory=list)    # channels in each audio stream, in order

    @staticmethod
    def of_clip(c, fallback_fps):
        tracks, pick = premiere_audio(c.audio_layout, c.audio_pick)
        return Media(c.path, c.fps or fallback_fps, c.duration, True, c.has_audio,
                     max(1, c.audio_channels), c.audio_rate, c.width or 1920, c.height or 1080, c.timecode,
                     c.par or 1.0, c.rotation, tracks, pick, list(c.audio_layout))

    def shown_size(self):
        """Width and height of the picture as Premiere shows it: anamorphic unsqueezed, phones upright."""
        w, h = self.width * (self.par or 1.0), self.height
        return (h, w) if self.rotation % 180 == 90 else (w, h)


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
            if abs((m.par or 1.0) - 1) < 0.01:      # anything else: Premiere reads it from the file
                sub(sc, "anamorphic", "FALSE")
                sub(sc, "pixelaspectratio", "square")
            sub(sc, "fielddominance", "none")
        if m.has_audio:
            # one <audio> per clip Premiere makes of the file (a mono stream, a stereo pair, or each
            # channel of a 3+ channel stream), numbered by source channel the way Premiere exports
            # them. A single <audio> with the first stream's channel count makes Premiere keep only
            # that stream: on a Mini LF (5 mono streams) that is channel 1, which is empty.
            ch = 0
            for n in audio_track_channels(m.audio_layout or [m.channels]):
                a = sub(media, "audio")
                sc = sub(a, "samplecharacteristics")
                sub(sc, "depth", 16)
                sub(sc, "samplerate", m.rate)
                sub(a, "channelcount", n)
                if n <= 2:
                    sub(a, "layout", "mono" if n == 1 else "stereo")
                for k in range(n):
                    ch += 1
                    ac = sub(a, "audiochannel")
                    sub(ac, "sourcechannel", ch)
                    sub(ac, "channellabel", "discrete" if n == 1 else ("left", "right")[k])
        return f

    def clipitem(self, track, cid, m, mediatype, start, frames, fps, enabled=True, label=None, scale=None,
                 speed=1.0, src_in=0, name=None, strack=1):
        ci = sub(track, "clipitem", id=cid)
        sub(ci, "name", name or os.path.basename(m.path))
        sub(ci, "enabled", "TRUE" if enabled else "FALSE")
        sub(ci, "duration", int(round(m.duration * fps)))
        add_rate(ci, fps)
        sub(ci, "start", start)
        sub(ci, "end", start + frames)
        sub(ci, "in", src_in)                          # source frames; timeline length is frames
        sub(ci, "out", src_in + int(round(frames / speed)))
        self.file(ci, m)
        if abs(speed - 1) > 1e-6:
            add_speed(ci, 100.0 / speed, mediatype)
        if mediatype == "audio":
            st = sub(ci, "sourcetrack")
            sub(st, "mediatype", "audio")
            sub(st, "trackindex", strack)
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
        track_of = {}
        if m.has_audio:
            au = sub(media, "audio")
            for k in range(1, max(1, m.audio_tracks) + 1):
                ci = self.clipitem(sub(au, "track"), self.uid("clipitem"), m, "audio", 0, frames, m.fps, strack=k)
                items.append((ci, "audio"))
                track_of[id(ci)] = k
        self._link(items, {id(ci): track_of.get(id(ci), 1) for ci, _ in items}, {id(ci): 1 for ci, _ in items})
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

    def sequence(self, parent, name, fps, width, height, start_tc_frame, entries, label=None, fit="fill"):
        """entries: dicts with media, start, vtrack (or None), atrack (or None), aenabled."""
        seq = sub(parent, "sequence", id=self.uid("sequence"))
        sub(seq, "uuid", "musicsync-%s-%s" % (datetime.datetime.now().strftime("%Y%m%d%H%M%S"), self.n))
        sub(seq, "name", name)
        total = max([1] + [e["start"] + (e["nest"][1] if e.get("nest") is not None else entry_span(e, fps)[1])
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
        for e in entries:
            if e.get("atrack") and e.get("media") is not None:
                e["asrc"] = audio_sources(e["media"], e.get("aenabled", True), e.get("all_audio"))
        na = max([0] + [(e["atrack"] + len(e.get("asrc") or [1]) - 1) if e["atrack"] else 0 for e in entries])
        vtracks = [sub(video, "track") for _ in range(max(nv, 1))]
        atracks = [sub(audio, "track") for _ in range(na)]
        for e in entries:
            if e.get("tall") and e.get("atrack"):
                # the song's track opens tall, so its waveform is readable (the attributes Premiere
                # writes itself for a track's height)
                for k in range(e["atrack"] - 1, min(na, e["atrack"] - 1 + len(e.get("asrc") or [1]))):
                    atracks[k].set("TL.SQTrackExpanded", "1")
                    atracks[k].set("TL.SQTrackExpandedHeight", str(SONG_TRACK_HEIGHT))
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
            m = e["media"]
            src_in, frames = entry_span(e, fps)
            items = []
            if e["vtrack"]:
                ci = self.clipitem(vtracks[e["vtrack"] - 1], self.uid("clipitem"), m, "video",
                                   e["start"], frames, fps, label=e.get("label"),
                                   scale=fill_scale(m, width, height, fit), speed=sp, src_in=src_in,
                                   name=e.get("name"))
                count["v", e["vtrack"]] += 1
                track_of[id(ci)], index_of[id(ci)] = e["vtrack"], count["v", e["vtrack"]]
                items.append((ci, "video"))
            if e["atrack"] and m.has_audio:
                for k, (strack, on) in enumerate(e["asrc"]):
                    at = e["atrack"] + k
                    ci = self.clipitem(atracks[at - 1], self.uid("clipitem"), m, "audio",
                                       e["start"], frames, fps, enabled=on, label=e.get("label"), speed=sp,
                                       src_in=src_in, name=e.get("name"), strack=strack)
                    count["a", at] += 1
                    track_of[id(ci)], index_of[id(ci)] = at, count["a", at]
                    items.append((ci, "audio"))
            self._link(items, track_of, index_of)
        muted = {e["atrack"] + k for e in entries if e.get("mute") and e.get("atrack")
                 for k in range(len(e.get("asrc") or [1]))}
        for i, t in enumerate(vtracks + atracks):
            # a muted track (the song in a Condensed sequence) is there but silent
            sub(t, "enabled", "FALSE" if i - len(vtracks) + 1 in muted else "TRUE")
            sub(t, "locked", "FALSE")
        add_labels(seq, label)
        return seq


def audio_sources(m, enabled=True, every=False):
    """[(source audio clip, enabled)] a sequence entry puts on consecutive audio tracks. The channel
    the sync heard (the scratch mic) comes first and plays; with every=True the file's other
    channels follow, switched off when the scratch channel is known (on a Mini LF they are near
    silence and timecode), so they are there to switch on. every="raw" (Breakups): every channel in
    the camera's own order, all on, exactly as the file has them."""
    if not m.has_video:
        return [(1, enabled)]
    if every == "raw":
        return [(k, True) for k in range(1, max(1, m.audio_tracks) + 1)]
    pick = m.audio_pick or 1
    out = [(pick, enabled)]
    if every:
        out += [(k, enabled and not m.audio_pick) for k in range(1, max(1, m.audio_tracks) + 1) if k != pick]
    return out


def entry_span(e, fps):
    """(source in frame, timeline length in frames) of a sequence entry; 'src' = (in s, out s)
    picks part of the file (one pass of a restarted take), otherwise the whole file."""
    sp = e.get("speed", 1.0)
    if e.get("src") is None:
        return 0, int(round(e["media"].duration * fps * sp))
    a, b = (int(round(v * fps)) for v in e["src"])
    return a, int(round((b - a) * sp))


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


def fill_scale(m, width, height, fit="fill"):
    """Scale (%) that makes a clip fill the sequence frame edge to edge, cropping the overflow ("fill":
    a 4480x3096 open gate in UHD is 85.71, no black edges), or fit inside it whole ("fit", Premiere's
    Scale to Frame Size, 69.77, which leaves bars)."""
    if not m.has_video or not m.width or not m.height:
        return None
    w, h = m.shown_size()
    return 100.0 * (max if fit == "fill" else min)(width / w, height / h)


def first_format(clips, fallback_fps):
    """Frame size of the first clip in filename order; frame rate most common among them."""
    first = next((c for c in sorted(clips, key=lambda c: c.rel) if c.width), None)
    fps = collections.Counter(c.fps for c in clips if c.fps).most_common(1)
    size = tuple(int(round(v)) for v in Media.of_clip(first, fallback_fps).shown_size()) if first else (1920, 1080)
    return size, (fps[0][0] if fps else fallback_fps)


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


def cam_bin_name(letter, cl):
    """"C Cam (Action 4.1)": the name in the camera folder, else the model the files report."""
    named = [f[1] for f in (folder_camera(c.top_folder) for c in cl) if f and f[1]]
    if named:
        return "%s Cam (%s)" % (letter, collections.Counter(named).most_common(1)[0][0])
    model = next((c.model for c in cl if c.model), "")
    short = short_camera_name(model)
    return "%s Cam (%s)" % (letter, short) if short else "%s Cam" % letter


def sync_entries(placements, seq_fps, preroll_s, master_media, args, label, song=True):
    """Sync layout: song at 01:00:00:00, every clip (every pass of a restarted take) on its own
    video track at its song offset. placements: [(Clip, Part)]."""
    song_frame = int(round(preroll_s * seq_fps))
    entries = []
    with_song = bool(master_media) and song and not args.no_master_audio
    if with_song:
        entries.append(dict(media=master_media, start=song_frame, vtrack=None, atrack=1, tall=True))
    first_a = 2 if with_song else 1
    for c, p in placements:
        # song time of the part's first frame, snapped so the cut sits on a whole source frame
        a = int(round(p.src_in * seq_fps))
        start = song_frame + int(round((p.offset + a / seq_fps * c.speed) * seq_fps))
        if p.src_in == 0:
            c.seq_start_frame = start
        entries.append(dict(media=Media.of_clip(c, seq_fps), start=start, vtrack=p.track,
                            atrack=(first_a + p.track - 1) if args.scratch_audio != "off" else None,
                            aenabled=args.scratch_audio == "on", label=label, speed=c.speed,
                            src=(p.src_in, p.src_out) if c.split or p.src_in > 0 else None,
                            name=clip_name(c, p)))
    start_tc = 3600 * rate_xml(seq_fps)[0] - song_frame
    return entries, start_tc


def condense(entries, fps):
    """The same clips on fewer video tracks (fewer feeds in multicam), within the smallest of
    2, 4, 8, 16... tracks that fits every overlap. Clips go in clip order, each on the highest
    track that's free for its whole length, so clip 1 sits on V1 and the rest follow it down;
    only if that order can't fit the budget are they packed in timeline order instead. Either way
    the tracks are then ordered by their first clip. Nothing is
    cut or moved in time; its scratch audio follows it to the matching audio track."""
    vids = [e for e in entries if e.get("vtrack") and not e.get("tail")]
    spans = {id(e): (e["start"], e["start"] + entry_span(e, fps)[1]) for e in vids}
    edges = sorted([(a, 1) for a, b in spans.values()] + [(b, -1) for a, b in spans.values()],
                   key=lambda x: (x[0], x[1]))          # an end and a start on the same frame don't overlap
    need, cur = 0, 0
    for _, d in edges:
        cur += d
        need = max(need, cur)
    tracks = 2
    while tracks < need:
        tracks *= 2
    def pack(order):                                     # each clip on the highest free track
        busy, place = [], {}
        for e in order:
            a, b = spans[id(e)]
            k = next((k for k, t in enumerate(busy) if all(b <= x or a >= y for x, y in t)), None)
            if k is None:
                k = len(busy)
                busy.append([])
            busy[k].append((a, b))
            place[id(e)] = k + 1
        return place
    # clip order first (clip 1 on V1, later clips below it in order); if that needs more tracks
    # than the budget, timeline order, which always fits in the fewest
    place = pack(sorted(vids, key=lambda e: e["vtrack"]))
    if max(place.values(), default=0) > tracks:
        place = pack(sorted(vids, key=lambda e: (e["start"], e["vtrack"])))
    # tracks top to bottom by their first clip, so V1 always holds clip 1
    first = {}
    for e in vids:
        first[place[id(e)]] = min(first.get(place[id(e)], e["vtrack"]), e["vtrack"])
    renum = {k: i + 1 for i, k in enumerate(sorted(first, key=first.get))}
    place = {i: renum[k] for i, k in place.items()}
    out = []
    for e in entries:
        if e.get("tail"):                                  # clips that didn't sync stay on V1 after the song
            out.append(e)
            continue
        if not e.get("vtrack"):
            # the song is muted here: CamsNested and Edit nest these and play the song on their own A1
            out.append(dict(e, mute=True) if e.get("atrack") else e)
            continue
        k = place[id(e)]
        d = e["atrack"] - e["vtrack"] if e.get("atrack") else None
        out.append(dict(e, vtrack=k, atrack=(k + d) if d is not None else None))
    return out


def stringout_entries(clips, fps, label):
    """Breakup layout: every clip of the camera back to back on V1, in filename order, with all of
    its audio channels as the camera recorded them on A1, A2... (none moved, muted or dropped)."""
    entries, pos = [], 0
    for c in clips:
        m = Media.of_clip(c, fps)
        entries.append(dict(media=m, start=pos, vtrack=1, atrack=1, label=label, all_audio="raw"))
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
        b = bin_(footage, cam_bin_name(letter, cl), label)
        usable = [c for c in cl if c.readable and c.fps]
        for c in usable:
            xw.master_clip(b, Media.of_clip(c, seq_fps), label)
        maybe_empty(b, ["Footage", "%s Cam" % letter], bool(usable))

    seqs = bin_(top, "Sequence")
    breakup = bin_(seqs, "Breakup")
    setup_only = getattr(args, "mode", "music") == "setup"          # no song: no Sync sequences
    syncb = bin_(seqs, "Sync") if not setup_only else None
    # every take on its own track in Sync > Synced; the same packed onto as few tracks as can hold
    # them in Sync > Synced Condensed, which is what CamsNested and Edit nest (fewer multicam feeds)
    syncedb = bin_(syncb, "Synced") if syncb is not None else None
    condb = bin_(syncb, "Synced Condensed") if syncb is not None else None
    nests = []
    for letter, cl in cams:
        label = camera_label(letter)
        usable = [c for c in cl if c.readable and c.fps and c.width]
        if usable:
            (w, h), fps = first_format(usable, seq_fps)
            entries, tc = stringout_entries(usable, fps, label)
            xw.sequence(breakup, "%s Cam_Breakup" % letter, fps, w, h, tc, entries, label)
        placed = placements_of(cl)
        if placed and syncb is not None:
            (w, h) = args.sync_size or first_format([c for c, _ in placed], seq_fps)[0]
            entries, tc = sync_entries(placed, seq_fps, preroll, master_media, args, label)
            tail = unsynced_entries(cl, entries, seq_fps, preroll, master_media)
            for e in tail:
                e["label"] = label
            entries += tail
            xw.sequence(syncedb, "%s Cam_Synced" % letter, seq_fps, w, h, tc, entries, label)
            seq = xw.sequence(condb, "%s Cam_Synced_Condensed" % letter, seq_fps, w, h, tc,
                              condense(entries, seq_fps), label)
            nests.append((letter, seq, (w, h), tc))
    maybe_empty(breakup, ["Sequence", "Breakup"], bool(len(breakup)))
    if syncb is not None:
        maybe_empty(syncedb, ["Sequence", "Sync", "Synced"], bool(nests))
        maybe_empty(condb, ["Sequence", "Sync", "Synced Condensed"], bool(nests))

    edit = bin_(seqs, "Edit")
    for sub_name in ("Working", "Past"):
        maybe_empty(bin_(edit, sub_name), ["Sequence", "Edit", sub_name], False)
    if nests:
        # every camera's condensed sync sequence nested on its own track (A on V1, B on V2...), song on A1:
        # "<name>_CamsNested" in the Sync bin, and the same again as the Edit sequence Jake
        # cuts in. (Multi-Camera on the nests is a switch XML can't carry: select them > Enable.)
        (w, h), tc = nests[0][2], nests[0][3]
        song_frame = int(round(preroll * seq_fps))

        def all_cams():
            entries = [dict(nest=(seq, int(seq.findtext("duration"))), start=0, vtrack=i, atrack=None,
                            label=camera_label(letter))
                       for i, (letter, seq, _, _) in enumerate(nests, 1)]
            if master_media and not args.no_master_audio:
                entries.append(dict(media=master_media, start=song_frame, vtrack=None, atrack=1, tall=True))
            return entries
        # written after the sync sequences it nests: they must be defined before they're referenced
        xw.sequence(syncb, "%s_CamsNested" % name, seq_fps, w, h, tc, all_cams())
        xw.sequence(edit, "%s_Edit" % name, seq_fps, w, h, tc, all_cams())
    elif setup_only:
        # an empty sequence to cut in, at the delivery size, starting at 01:00:00:00
        usable = [c for c in clips if c.readable and c.fps and c.width]
        (w, h) = args.sync_size or (first_format(usable, seq_fps)[0] if usable else (3840, 2160))
        xw.sequence(edit, "%s_Edit" % name, seq_fps, w, h, 3600 * rate_xml(seq_fps)[0], [])

    audio = bin_(top, "Audio")
    for bname in ("Music", "SFX", "Captured"):
        b = bin_(audio, bname)
        items = ([master_media] if bname == "Music" and master_media else []) + audio_bins.get(bname, [])
        for m in items:
            xw.master_clip(b, m)
        maybe_empty(b, ["Audio", bname], bool(items))
    return root


def clip_name(c, p):
    """Timeline name: file name, plus which pass of a restarted take, plus a flag when it was placed
    at the first of two identical sections."""
    name = os.path.basename(c.path)
    if c.split:
        name += " (pass %d of %d)" % (c.parts.index(p) + 1, len(c.parts))
    if p.repeat_alt is not None:
        name += " (check chorus)"
    return name if name != os.path.basename(c.path) else None


UNSYNCED_GAP_S = 60     # clips that didn't sync start this long after the song (or the last take) ends


def unsynced_entries(clips, entries, seq_fps, preroll_s, master_media):
    """Clips of a camera that didn't line up with the song, back to back on V1 in file order, a
    minute after the song and every synced take have ended, each with all its camera audio on
    A2, A3... (the song is on A1), named with why it wasn't synced."""
    left = [c for c in clips if c.readable and c.fps and c.width and c.status != "placed"]
    if not left:
        return []
    ends = [int(round(preroll_s * seq_fps)) + int(round((master_media.duration if master_media else 0) * seq_fps))]
    ends += [e["start"] + entry_span(e, seq_fps)[1] for e in entries if e.get("media") is not None]
    pos, out = max(ends) + int(round(UNSYNCED_GAP_S * seq_fps)), []
    for c in left:
        why = re.split(r"\s*[(;]", (c.reasons or ["not synced"])[0])[0].strip()
        out.append(dict(media=Media.of_clip(c, seq_fps), start=pos, vtrack=1, atrack=2, all_audio="raw",
                        label=None, tail=True, name="%s (%s)" % (os.path.basename(c.path), why)))
        pos += int(round(c.duration * seq_fps))
    return out


def placements_of(clips):
    """[(Clip, Part)] for every placed pass, in track order."""
    return sorted([(c, p) for c in clips if c.status == "placed" for p in c.parts if p.status == "placed"],
                  key=lambda cp: cp[1].track)


def build_camera_xml(letter, clips, seq_fps, preroll, master_media, args):
    """Stand-alone sync sequence for one camera (the --per-camera output)."""
    xw = Xmeml(args.path_maps)
    root = ET.Element("xmeml", version="4")
    pl = placements_of(clips)
    (w, h) = args.sync_size or first_format([c for c, _ in pl], seq_fps)[0]
    label = camera_label(letter)
    entries, tc = sync_entries(pl, seq_fps, preroll, master_media, args, label)
    xw.sequence(root, "%s Cam_Synced" % letter, seq_fps, w, h, tc, entries, label)
    return root


def write_xml(root, path):
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        fh.write(body)
        fh.write("\n")


# ---------------------------------------------------------------- reports

COLUMNS = ["file", "status", "reason", "camera", "track", "pass", "clip_range", "playback_speed", "offset_seconds",
           "offset_timecode", "timeline_timecode", "confidence", "waveform_check", "matching_landmarks",
           "runner_up_landmarks",
           "drift_ms_head_to_tail", "drift_frames", "fps", "duration_s", "resolution", "audio",
           "camera_model", "serial", "notes"]


def fmt_clock(s):
    s = round(s, 1)
    return "%d:%04.1f" % (s // 60, s % 60)


def clip_rows(c, seq_fps, preroll):
    """One report row per clip, or one per pass of the song for a take where it restarted."""
    if not c.split:
        return [clip_row(c, seq_fps, preroll)]
    rows = []
    for i, p in enumerate(c.parts, 1):
        r = clip_row(c, seq_fps, preroll, p)
        r["pass"] = "%d of %d" % (i, len(c.parts))
        r["clip_range"] = "%s-%s" % (fmt_clock(p.src_in), fmt_clock(p.src_out))
        rows.append(r)
    return rows


def clip_row(c, seq_fps, preroll, part=None):
    fps = seq_fps
    if part is not None:                 # one pass of a restarted take: its own placement
        c = dataclasses.replace(c, status=part.status, reasons=[part.reason] if part.reason else [],
                                track=part.track, offset=part.offset, confidence=part.confidence,
                                aligned=part.aligned, runner_up=part.runner_up, drift_ms=part.drift_ms,
                                check=part.check, notes=part.notes)
    drift_frames = round(c.drift_ms / 1000 * fps, 2) if c.drift_ms is not None else ""
    return {
        "file": c.rel,
        "status": c.status,
        "reason": "; ".join(c.reasons),
        "camera": c.camera,
        "track": ("V%d" % c.track) if c.track else "",
        "pass": "",
        "clip_range": "",
        "playback_speed": ("%gx %s" % (c.speed, c.speed_mode)) if c.speed != 1 else "",
        "offset_seconds": "%.3f" % c.offset if c.offset is not None else "",
        "offset_timecode": fmt_tc(c.offset, fps) if c.offset is not None else "",
        "timeline_timecode": fmt_frames(3600 * rate_xml(fps)[0] + int(round(c.offset * fps)), fps)
        if c.offset is not None else "",
        "confidence": "%.1f" % c.confidence if c.confidence is not None else "",
        "waveform_check": ("%s windows match" % c.check) if c.check else "",
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


def write_reports(clips, out_dir, seq_fps, preroll, args, cam_files, labels, captured=(), stem="sync_report"):
    rows = [r for c in clips for r in clip_rows(c, seq_fps, preroll)]
    with open(os.path.join(out_dir, stem + ".csv"), "w", newline="", encoding="utf-8") as fh:
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
        L.append("| %s Cam | %s | %s | %d | %d | %s |" % (letter, camera_label(letter), md_escape(
            cam_bin_name(letter, cl)[len(letter) + 5:].strip("()") or key), p,
                                                          len(cl) - p, cam_files.get(key, "")
                                                          if p else "(nothing placed)"))
    L.append("")
    L.append("Master song: `%s`. Captured audio in Audio > Captured: %s" % (
        os.path.relpath(args.master, args.clips),
        ", ".join("`%s`" % os.path.relpath(p, args.clips) for p in captured) or "none found"))
    L.append("")
    split = [c for c in clips if c.split]
    if split:
        L.append("## Takes where the song restarted")
        L.append("")
        L.append("The song was stopped and restarted, paused, or jumped to another part during these takes. "
                 "Each pass is cut out of the clip and placed on its own track, named `(pass N of M)`.")
        L.append("")
        L.append("| File | Pass | Clip time | Track | Song time at cut | Status | Waveform check |")
        L.append("|---|---|---|---|---|---|---|")
        for c in split:
            for i, p in enumerate(c.parts, 1):
                L.append("| %s | %d of %d | %s-%s | %s | %s | %s | %s |" % (
                    md_escape(c.rel), i, len(c.parts), fmt_clock(p.src_in), fmt_clock(p.src_out),
                    ("V%d" % p.track) if p.track else "",
                    fmt_clock(p.offset + p.src_in * c.speed) if p.offset is not None else "",
                    p.status if p.status == "placed" else md_escape("not placed: %s; %s" % (p.reason, "; ".join(p.notes))),
                    p.check or ""))
        L.append("")
    doubt = doubtful(clips)
    repeats = [(c, i, p) for c in clips for i, p in enumerate(c.parts, 1)
               if p.status == "placed" and p.repeat_alt is not None]
    if repeats:
        L.append("## Check which chorus")
        L.append("")
        L.append("These only cover a section that's in the song twice with the same audio (a pasted "
                 "chorus), so they lip-sync at either copy. They're placed at the first copy and named "
                 "`(check chorus)`; slide one to the other copy if that's where it was shot "
                 "(`--set-aside-repeats` leaves them out instead).")
        L.append("")
        for c, i, p in repeats:
            L.append("- %s%s: placed at song %s, also fits at %s (%+.2f s)" % (
                c.rel, " pass %d" % i if c.split else "", fmt_clock(p.offset + p.src_in * c.speed),
                fmt_clock(p.repeat_alt + p.src_in * c.speed), p.repeat_alt - p.offset))
        L.append("")
    if doubt:
        L.append("## Worth a look")
        L.append("")
        L.append("Placed, but in some stretches the clip's audio doesn't match the song at that position "
                 "(the song may stop, be talked over, or another pass may be too short to split out):")
        L.append("")
        for c, i, p in doubt:
            L.append("- %s%s: %s windows match" % (c.rel, " pass %d" % i if c.split else "", p.check))
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
    cols = ["file", "pass", "status", "reason", "camera", "track", "offset_timecode", "offset_seconds",
            "confidence", "waveform_check", "drift_frames", "fps", "audio"]
    L.append("| " + " | ".join(cols) + " |")
    L.append("|" + "---|" * len(cols))
    for r in rows:
        L.append("| " + " | ".join(md_escape(r[k]) for k in cols) + " |")
    L.append("")
    L.append("Offsets are song time of the clip's first frame (negative means the camera rolled before the "
             "song started). Confidence (0-100) measures how decisively the best position in the song beats the "
             "next-best one: 60 is about 2.7 standard deviations, 90+ is unmistakable. Waveform check is a "
             "second, independent test: the clip's audio is compared with the song at the placed position in "
             "4-second windows, and it counts the windows that match.")
    with open(os.path.join(out_dir, stem + ".md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", metavar="[MASTER] FOLDER",
                    help="a shoot folder (the song is found inside it), or a master song then a clips folder; "
                         "several folders (cards) are taken together, as one project in the folder that holds them")
    ap.add_argument("--master", help="master song, if it can't be found in the folder automatically")
    ap.add_argument("-o", "--out", help="output folder (default: '%s' inside the folder)" % OUT_DIR)
    ap.add_argument("--name", help="project name, which names the XML (default: the folder name)")
    ap.add_argument("--xml-dir", help="where the project XMLs go (default: the output folder); 'drive' puts them "
                                      "at the top of the drive the footage is on. Reports stay in the output folder")
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
    ap.add_argument("--sync-size", default="3840x2160", metavar="WxH",
                    help="frame size of the Sync and Edit sequences, every clip scaled to fill it "
                         "(default 3840x2160; 'first' uses the camera's first clip, like Breakup)")
    ap.add_argument("--set-aside-repeats", dest="place_repeats", action="store_false",
                    help="set aside clips that fit two identical copies of a section (a pasted chorus) "
                         "instead of placing them at the first copy, marked (check chorus)")
    ap.add_argument("--path-map", action="append", default=[], metavar="OLD=NEW",
                    help="rewrite media paths in the XML, e.g. /mnt/footage=/Volumes/SSD/Shoot "
                         "or /mnt/footage=D:/Shoot (repeatable)")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, min(8, os.cpu_count() or 2)))
    ap.add_argument("--mode", choices=["auto", "music", "setup"], default="auto",
                    help="music: sync to the song (music video); setup: bins, Breakups and an empty Edit "
                         "sequence only (commercials); auto (default): music when a song is found and "
                         "clips line up with it")
    ap.add_argument("--rebuild", action="store_true",
                    help="build the whole project again even if this folder was run before (by default a "
                         "second run only adds the cards that are new since then)")
    ap.add_argument("--events", action="store_true", help=argparse.SUPPRESS)   # for the Kickoff window
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    global EVENTS
    EVENTS = args.events
    if args.sync_size == "first":
        args.sync_size = None
    else:
        m = re.match(r"^(\d+)x(\d+)$", args.sync_size)
        if not m:
            ap.error("--sync-size needs WxH, e.g. 3840x2160, or 'first'")
        args.sync_size = (int(m.group(1)), int(m.group(2)))
    # folders, and loose files dropped with them: footage and audio are taken, anything else
    # (XMLs, text, stills, project files) is left out
    folders, files, other = [os.path.abspath(p) for p in args.paths if os.path.isdir(p)], [], []
    for p in args.paths:
        if os.path.isdir(p):
            continue
        if not os.path.isfile(p):
            sys.exit("error: %s is not a folder or a file" % p)
        ext = os.path.splitext(p)[1].lower()
        if ext in AUDIO_EXT and not args.master:
            args.master = p                            # the first audio file is the song
        elif ext in AUDIO_EXT or ext in MEDIA_EXT:
            if os.path.abspath(p) != os.path.abspath(args.master or ""):
                files.append(os.path.abspath(p))
        else:
            other.append(p)
    if other:
        log("Left out %d file%s that aren't footage or audio: %s" % (
            len(other), "" if len(other) == 1 else "s", ", ".join(os.path.basename(p) for p in other[:5])
            + (" ..." if len(other) > 5 else "")))
    if not folders and not files:
        sys.exit("error: give a shoot folder (or the card folders, or the clips) to sync")
    # several folders (cards dropped together): one project in the folder that holds them all
    folders = [f for f in folders if not any(f != g and f.startswith(g.rstrip(os.sep) + os.sep) for g in folders)]
    folders = sorted(set(folders))
    files = sorted(set(f for f in files if not any(f.startswith(g.rstrip(os.sep) + os.sep) for g in folders)))
    homes = sorted(set(folders) | {os.path.dirname(f) for f in files})
    args.only = (folders + files) if len(folders) + len(files) > 1 or files else None
    args.clips = os.path.commonpath(homes) if len(homes) > 1 else homes[0]
    if args.only and args.clips in ("/", "/Volumes", os.path.expanduser("~")):
        sys.exit("error: those folders aren't in one shoot folder. Put the cards in one folder, or run them one at a time.")
    if args.only:
        log("Taking %d %s together in %s: %s" % (len(args.only), "folders" if not files else "items", args.clips,
                                               ", ".join(os.path.relpath(f, args.clips) for f in args.only[:12])
                                               + (" ..." if len(args.only) > 12 else "")))
    out_given = bool(args.out)
    args.out = os.path.abspath(args.out or os.path.join(args.clips, OUT_DIR))
    state, restrict = (None, None) if args.rebuild else load_state(args, out_given)
    args.xml_out = xml_folder(args)
    project_name = args.name or (state or {}).get("project") or os.path.basename(args.clips.rstrip("/\\")) or "Sync"

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit("error: %s not found on PATH" % tool)
    args.path_maps = []
    for pm in args.path_map:
        if "=" not in pm:
            sys.exit("error: --path-map needs OLD=NEW")
        old, new = pm.split("=", 1)
        args.path_maps.append((os.path.abspath(old), new.rstrip("/\\")))

    if state:
        return add_cards(args, state, restrict)

    all_audio = find_audio(args.clips, [args.out])
    audio_files = [p for p in all_audio if inside(p, args.only)]
    if args.mode != "setup" and not args.master:
        # the song can sit outside the cards dropped (Audio/Music next to them): look everywhere
        ties = []
        args.master = pick_master(args.clips, audio_files, ties) or \
            (pick_master(args.clips, all_audio, ties) if args.only else None) or song_nearby(args.clips, ties=ties)
        if args.master and len(ties) > 1:
            # several files could be the song: let a few clips say which one they were shot to
            event("stage", text="Working out which file is the song")
            probe_clips = [c for c in find_clips(args.clips, None, [args.out]) if inside(c.path, args.only)]
            won = vote_song(ties, probe_clips) if probe_clips else None
            if won:
                args.master = won[0]
                log("Song picked by the clips: %s (%s)" % (os.path.basename(won[0]), ", ".join(
                    "%s %d" % (os.path.basename(p), n) for p, n in sorted(won[1].items(), key=lambda kv: -kv[1]))))
        if not args.master and args.mode == "music":
            names = "\n  ".join(os.path.relpath(p, args.clips) for p in audio_files) or "(no audio files)"
            sys.exit("error: can't tell which file is the song. Put it in a 'Music' folder or pass "
                     "--master.\nAudio files found:\n  " + names)
        if args.master:
            log("Master song: %s" % os.path.relpath(args.master, args.clips))
            rel = os.path.relpath(args.master, args.clips)
            # found by its folder or name (not just the only audio file): don't second-guess it
            args.song_certain = bool(SONG_HINT.search(rel))
    if args.mode == "setup":
        args.master = None
    if not args.master:
        log("No song to sync to: setting up the project only (bins, Breakups, Edit sequence)")

    def audio_bin(p):      # files under a folder called SFX / Music go to those bins, the rest is Captured
        parts = [x.lower() for x in os.path.relpath(p, args.clips).replace("\\", "/").split("/")[:-1]]
        if any(x in ("sfx", "sound effects", "sound fx") for x in parts):
            return "SFX"
        if any(x in ("music", "song", "songs") for x in parts):
            return "Music"
        return "Captured"
    os.makedirs(args.out, exist_ok=True)

    master = None
    if args.master:
        event("stage", text="Listening to the song")
        log("Indexing master %s" % args.master)
        try:
            master = MasterIndex(load_audio(args.master))
        except RuntimeError as e:
            sys.exit("error: can't read master: %s" % e)
        log("  %.1f s, %d landmarks" % (master.duration, len(master.h)))

    clips = [c for c in find_clips(args.clips, args.master, [args.out]) if inside(c.path, args.only)]
    if not clips:
        sys.exit("error: no video files found in %s" % ", ".join(args.only or [args.clips]))
    log("Probing %d clips" % len(clips))
    event("stage", text="Reading %d clips" % len(clips))
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(probe, clips))

    if master is not None:
        event("start", project=project_name, folder=args.clips, song=os.path.basename(args.master),
              song_duration=round(master.duration, 2), clips=len(clips), version=VERSION, mode="music")
        match_all(clips, master, args)
        if args.mode == "auto" and not getattr(args, "song_certain", True) \
                and not any(c.status == "placed" for c in clips):
            # the "song" lines up with nothing: it's another recording (a boom track on a commercial)
            log("Nothing lines up with %s, so this isn't a music video shoot: setting up the project "
                "only, with that file under Audio > Captured" % os.path.basename(args.master))
            for c in clips:
                c.status, c.reasons, c.notes = "", [], []
            args.master, master = None, None
    if master is None:
        args.mode = "setup"
        event("start", project=project_name, folder=args.clips, song="", song_duration=0, clips=len(clips),
              version=VERSION, mode="setup")
        for c in clips:
            c.status = "not placed"
    else:
        args.mode = "music"
    master_abs = os.path.abspath(args.master) if args.master else None
    captured = [p for p in audio_files if os.path.abspath(p) != master_abs]

    for c in clips:                     # a take with one pass of the song is one Part covering it all
        if c.status == "placed" and not c.split:
            c.parts = [Part(0.0, c.duration, offset=c.offset, status="placed", confidence=c.confidence,
                            aligned=c.aligned, runner_up=c.runner_up, drift_ms=c.drift_ms, refine=c.refine,
                            check=c.check, repeat_alt=c.repeat_alt)]
    labels = assign_cameras(clips, args.group_by)
    placed = [c for c in clips if c.status == "placed"]
    seq_fps = args.fps or (collections.Counter(c.fps for c in placed if c.fps).most_common(1) or
                           collections.Counter(c.fps for c in clips if c.fps).most_common(1) or [(24.0, 0)])[0][0]
    preroll = max([0.0] + [-(p.offset + p.src_in * c.speed) for c in placed for p in c.parts
                           if p.status == "placed"])
    # whole seconds, identical in every camera sequence; at least 10 s so cards added later that
    # started rolling a little earlier still fit
    preroll = max(PREROLL_MIN, math.ceil(preroll + 0.5))

    cams = []
    for key, letter in sorted(labels.items(), key=lambda kv: kv[1]):
        cl = sorted([c for c in clips if c.camera_key == key], key=lambda c: c.rel)
        # one track per clip, and per pass when the song restarted in a take (passes side by side)
        pl = [(c, p) for c in cl if c.status == "placed" for p in c.parts if p.status == "placed"]
        pl.sort(key=(lambda cp: (cp[0].rel, cp[1].src_in)) if args.track_order == "name"
                else (lambda cp: (cp[1].offset + cp[1].src_in * cp[0].speed, cp[0].rel)))
        for i, (c, p) in enumerate(pl, 1):
            p.track = i
            if c.track is None:
                c.track = i
        cams.append((letter, cl))

    master_media = None
    if master is not None:
        _, mch, mrate = probe_audio(args.master)
        master_media = Media(args.master, seq_fps, master.duration, has_video=False, channels=mch, rate=mrate)
    audio_bins = collections.defaultdict(list)
    for p in captured:
        dur, ch, rate = probe_audio(p)
        audio_bins[audio_bin(p)].append(Media(p, seq_fps, dur, has_video=False, channels=ch, rate=rate))
    captured = [m.path for m in audio_bins["Captured"]]

    event("stage", text="Writing the Premiere project")
    proj_file = re.sub(r"[^\w .-]+", "_", project_name) + ".xml"
    write_xml(build_project(project_name, clips, cams, seq_fps, preroll, master_media, audio_bins, args),
              os.path.join(args.xml_out, proj_file))
    log("Wrote %s" % os.path.join(args.xml_out, proj_file))
    cam_files = {}
    for letter, cl in cams:
        key = cl[0].camera_key
        cam_files[key] = proj_file
        pl = [c for c in cl if c.status == "placed"]
        if args.per_camera and pl:
            model = cam_bin_name(letter, cl)[len(letter) + 5:].strip("()") or key.split(" / ")[0]
            fname = re.sub(r"[^\w .-]+", "_", "%s Cam_Synced - %s.xml" % (letter, model))
            write_xml(build_camera_xml(letter, pl, seq_fps, preroll, master_media, args),
                      os.path.join(args.xml_out, fname))
            cam_files[key] = fname
            log("Wrote %s (%d tracks)" % (fname, len(placements_of(pl))))

    if master is not None:
        write_reports(clips, args.out, seq_fps, preroll, args, cam_files, labels, captured)
        report = os.path.join(args.out, "sync_report.md")
        log("Wrote sync_report.csv and sync_report.md")
        log("Placed %d of %d clips." % (len(placed), len(clips)))
    else:
        report = write_clip_list(clips, cams, args.out, audio_bins)
        log("Wrote clip_list.csv. Set up %d clips from %d cameras." % (len(clips), len(cams)))
    save_state(args, project_name, clips, labels, seq_fps, preroll, audio_files)
    cam_events = []
    for letter, cl in cams:
        pl = [(c, p) for c in cl if c.status == "placed" for p in c.parts if p.status == "placed"]
        spans = sorted([round(p.offset + p.src_in * c.speed, 2),
                        round(p.offset + p.src_out * c.speed, 2)] for c, p in pl)
        cam_events.append(dict(letter=letter, name=cam_bin_name(letter, cl), label=camera_label(letter),
                               clips=len(cl), synced=sum(c.status == "placed" for c in cl), spans=spans))
    aside = collections.Counter(c.reasons[0] if c.reasons else REASON_NO_MATCH
                                for c in clips if c.status != "placed") if master is not None else {}
    event("done", project=project_name, out=args.out, xml=os.path.join(args.xml_out, proj_file),
          report=report, song_duration=round(master.duration, 2) if master is not None else 0,
          mode=args.mode, clips=len(clips), synced=len(placed), cameras=cam_events,
          set_aside=[dict(reason=r, count=n) for r, n in collections.Counter(aside).most_common()],
          audio=sum(len(v) for v in audio_bins.values()),
          unreadable=sum(1 for c in clips if not c.readable),
          restarted=sum(1 for c in clips if c.split),
          check_chorus=sum(1 for c in clips for p in c.parts if p.repeat_alt is not None),
          worth_a_look=len({id(c) for c, _, _ in doubtful(clips)}))   # clips, not parts


def match_all(clips, master, args):
    """Sync every clip to the song (in parallel), logging and reporting each as it finishes."""
    st = Settings(threshold=args.threshold, min_hashes=args.min_landmarks, max_fps=args.max_fps,
                  place_repeats=args.place_repeats)
    log("Matching")
    done, placed, tried, give_up = 0, 0, 0, []

    def work(c):
        if give_up:                      # auto mode, and nothing lines up with the "song": stop early
            return c
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
            if c.split:
                res = "%d passes: " % len(c.parts) + ", ".join(
                    ("%.3fs" % p.offset) if p.status == "placed" else "(%s)" % p.reason for p in c.parts)
            elif c.status == "placed":
                res = "%.3fs  conf %.0f" % (c.offset, c.confidence)
            else:
                res = "-- " + "; ".join(c.reasons)
            log("  [%d/%d] %-40s %s" % (done, len(clips), c.rel, res))
            event("clip", done=done, total=len(clips), file=c.rel, status=c.status,
                  passes=len(c.parts) if c.split else 1, reason=c.reasons[0] if c.reasons else "")
            placed += c.status == "placed"
            tried += c.status == "placed" or (c.reasons[:1] in ([REASON_NO_MATCH], [REASON_LOW_CONF]))
            if args.mode == "auto" and not getattr(args, "song_certain", True) and not placed \
                    and tried >= 12 and not give_up:
                log("12 clips with sound and none lines up with the song: not a music video shoot")
                give_up.append(True)


CLIP_LIST_COLUMNS = ["file", "camera", "model", "resolution", "fps", "duration", "audio", "note"]


def write_clip_list(clips, cams, out_dir, audio_bins, stem="clip_list"):
    """Project-setup runs (no song): a plain list of what went where."""
    path = os.path.join(out_dir, stem + ".csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CLIP_LIST_COLUMNS)
        w.writeheader()
        for letter, cl in cams:
            for c in cl:
                w.writerow(dict(file=c.rel, camera="%s Cam" % letter, model=c.model,
                                resolution="%dx%d" % (c.width, c.height) if c.width else "",
                                fps=("%g" % c.fps) if c.fps else "", duration="%.2f" % c.duration,
                                audio="yes" if c.has_audio else "no",
                                note="" if c.readable else REASON_UNREADABLE))
        for bname, ms in sorted(audio_bins.items()):
            for m in ms:
                w.writerow(dict(file=os.path.relpath(m.path, os.path.dirname(out_dir)), camera="Audio > " + bname,
                                duration="%.2f" % (m.duration or 0), audio="yes"))
    return path


# ---------------------------------------------------------------- adding cards to a project

STATE_FILE = "kickoff-project.json"
PREROLL_MIN = 10


def rel_key(path, root):
    return os.path.relpath(path, root).replace("\\", "/")


def xml_folder(args):
    """Where the project XMLs are written: --xml-dir, or with 'drive' the top of the drive the
    footage is on (/Volumes/<drive>); the output folder when not given, or when the footage is on
    the Mac's own disk."""
    d = args.xml_dir
    if not d:
        return args.out
    if d == "drive":
        parts = os.path.abspath(args.clips).split(os.sep)
        if len(parts) > 2 and parts[1] == "Volumes":
            d = os.sep.join(parts[:3])
        else:
            log("The footage is on this Mac's own disk: the XML goes in %s" % args.out)
            return args.out
    d = os.path.abspath(os.path.expanduser(d))
    try:
        os.makedirs(d, exist_ok=True)
        test = os.path.join(d, ".kickoff-write-test")
        open(test, "w").close()
        os.remove(test)
    except OSError as e:
        log("Can't write the XML to %s (%s): it goes in %s" % (d, e.strerror or e, args.out))
        return args.out
    return d


def inside(p, folders):
    """True when path p is in one of `folders` (None: no limit)."""
    p = os.path.abspath(p)
    return folders is None or any(p == f or p.startswith(f.rstrip(os.sep) + os.sep) for f in folders)


def load_state(args, out_given):
    """What an earlier run on this shoot folder set up (Kickoff Exports/kickoff-project.json), so a
    second run only adds what's new. A folder inside an earlier run's shoot folder (a new card
    dropped on its own) counts too: returns (state, the dropped folder) and points args at the
    shoot folder. (None, None) when this is the first run."""
    def read(out):
        try:
            with open(os.path.join(out, STATE_FILE), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None
    only = getattr(args, "only", None)
    st = read(args.out)
    if st:
        return st, only
    if out_given:
        return None, None
    for old in OLD_OUT_DIRS:                 # a project set up before the folder was renamed
        st = read(os.path.join(args.clips, old))
        if st:
            args.out = os.path.join(args.clips, old)
            return st, only
    d = args.clips
    while os.path.dirname(d) != d:
        d = os.path.dirname(d)
        found = next(((o, st) for o in (OUT_DIR,) + OLD_OUT_DIRS
                      for st in [read(os.path.join(d, o))] if st), None)
        if found:
            st = found[1]
            restrict = only or [args.clips]
            args.clips, args.out = d, os.path.join(d, found[0])
            log("%s %s part of %s, which was set up before: adding %s" % (
                ", ".join(os.path.basename(r) for r in restrict), "is" if len(restrict) == 1 else "are", d,
                "it" if len(restrict) == 1 else "them"))
            return st, restrict
    return None, None


def save_state(args, name, clips, labels, seq_fps, preroll, audio_files, prev=None, cards=None, adds=0):
    st = dict(prev or {})
    master = args.master
    if master:
        master = os.path.abspath(master)
        master = rel_key(master, args.clips) if master.startswith(args.clips + os.sep) else master
    st.update(version=1, project=name, mode=args.mode, master=master, seq_fps=seq_fps, preroll=preroll,
              sync_size=list(args.sync_size) if args.sync_size else None, group_by=args.group_by,
              adds=adds, updated=datetime.datetime.now().isoformat(timespec="seconds"))
    cams = st.setdefault("cameras", {})
    for key, letter in labels.items():
        model = next((c.model for c in clips if c.camera_key == key and c.model), "")
        cams.setdefault(key, [letter, model])
    st["clips"] = sorted(set(st.get("clips", [])) | {rel_key(c.path, args.clips) for c in clips if c.readable})
    # unreadable files are tried again next run only if they've changed (maybe still copying off the card)
    bad = dict(st.get("unreadable", {}))
    for c in clips:
        k = rel_key(c.path, args.clips)
        if c.readable:
            bad.pop(k, None)
        else:
            try:
                bad[k] = os.path.getsize(c.path)
            except OSError:
                pass
    st["unreadable"] = bad
    folders = st.setdefault("folders", {})           # top folder -> camera, for files with no metadata
    for c in clips:
        if c.top_folder and c.model and c.camera_key:
            folders.setdefault(c.top_folder, c.camera_key)
    st["audio"] = sorted(set(st.get("audio", [])) | {rel_key(p, args.clips) for p in audio_files})
    cnt = collections.Counter(labels.values())
    st["cards"] = cards if cards is not None else {letter: 1 for letter in cnt}
    tmp = os.path.join(args.out, STATE_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, os.path.join(args.out, STATE_FILE))


def add_cards(args, state, restrict):
    """A later run on a shoot folder that was set up before (a DIT adding cards as they come in):
    only the clips that are new since then, into one small XML to import into the open project.
    Each camera's new clips get their own bin ('A Cam Card 2'), Breakup and Sync sequence; the
    Sync sequence starts at the same timecode as the camera's main one, so it nests on top of it."""
    name = args.name or state["project"]
    args.mode = state.get("mode") or "music"
    seq_fps, preroll = state["seq_fps"], state["preroll"]
    args.sync_size = tuple(state["sync_size"]) if state.get("sync_size") else None
    n = int(state.get("adds", 0)) + 1
    master_path = state.get("master")
    if master_path and not os.path.isabs(master_path):
        master_path = os.path.join(args.clips, master_path)
    if args.mode == "music" and not (master_path and os.path.exists(master_path)):
        sys.exit("error: can't find the song this project was synced to (%s). Put it back, or run with "
                 "--rebuild to start a new project." % state.get("master"))
    args.master = master_path if args.mode == "music" else None
    os.makedirs(args.out, exist_ok=True)

    bad = state.get("unreadable", {})

    def size(p):
        try:
            return os.path.getsize(p)
        except OSError:
            return -1

    def wanted(p, done):
        k = rel_key(p, args.clips)
        return k not in done and bad.get(k) != size(p) and \
            inside(p, restrict)
    done_clips, done_audio = set(state.get("clips", [])), set(state.get("audio", []))
    clips = [c for c in find_clips(args.clips, args.master, [args.out]) if wanted(c.path, done_clips)]
    master_abs = os.path.abspath(master_path) if master_path else None
    audio_new = [p for p in find_audio(args.clips, [args.out])
                 if wanted(p, done_audio) and os.path.abspath(p) != master_abs]
    log("Adding to %s: %d new clips, %d new audio files" % (name, len(clips), len(audio_new)))
    if not clips and not audio_new:
        log("Nothing new since the last run.")
        event("done", project=name, add=n, nothing_new=True, out=args.out, xml="", report="", mode=args.mode,
              clips=0, synced=0, cameras=[], set_aside=[], audio=0, unreadable=0, restarted=0,
              check_chorus=0, song_duration=0, moves=[])
        return

    master = None
    if args.mode == "music":
        event("stage", text="Listening to the song")
        master = MasterIndex(load_audio(args.master))
    event("stage", text="Reading %d new clips" % len(clips))
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(probe, clips))
    event("start", project=name, folder=args.clips, song=os.path.basename(args.master or ""),
          song_duration=round(master.duration, 2) if master else 0, clips=len(clips), version=VERSION,
          mode=args.mode, add=n)
    if master is not None and clips:
        match_all(clips, master, args)
    else:
        for c in clips:
            c.status = "not placed"
    for c in clips:
        if c.status == "placed" and not c.split:
            c.parts = [Part(0.0, c.duration, offset=c.offset, status="placed", confidence=c.confidence,
                            aligned=c.aligned, runner_up=c.runner_up, drift_ms=c.drift_ms, refine=c.refine,
                            check=c.check, repeat_alt=c.repeat_alt)]
        for p in c.parts:          # rolling longer before the song than the project allows: trim the head
            if p.status == "placed" and p.offset + p.src_in * c.speed < -preroll:
                p.src_in = (-preroll - p.offset) / c.speed + 1.0 / seq_fps
                p.notes.append("head trimmed: rolled more than %d s before the song" % preroll)

    labels = assign_cameras(clips, state.get("group_by", "auto"), prior=state.get("cameras"),
                            prior_folders=state.get("folders"))
    known_letters = {v[0] for v in state.get("cameras", {}).values()}
    cards = dict(state.get("cards", {}))
    cams = []
    for key, letter in sorted(labels.items(), key=lambda kv: kv[1]):
        cl = sorted([c for c in clips if c.camera_key == key], key=lambda c: c.rel)
        if any(x[0] == letter for x in cams):            # a second key on a known letter: same camera
            cams = [(l, x + cl if l == letter else x, k, nc) for l, x, k, nc in cams]
            continue
        new_cam = letter not in known_letters
        card = 1 if new_cam else int(cards.get(letter, 1)) + 1
        cards[letter] = card
        cams.append((letter, cl, card, new_cam))
    for letter, cl, card, new_cam in cams:
        pl = sorted([(c, p) for c in cl if c.status == "placed" for p in c.parts if p.status == "placed"],
                    key=lambda cp: (cp[0].rel, cp[1].src_in))
        for i, (c, p) in enumerate(pl, 1):
            p.track = i
            if c.track is None:
                c.track = i

    master_media = None
    if master is not None:
        _, mch, mrate = probe_audio(args.master)
        master_media = Media(args.master, seq_fps, master.duration, has_video=False, channels=mch, rate=mrate)
    audio_bins = collections.defaultdict(list)
    for p in audio_new:
        dur, ch, rate = probe_audio(p)
        parts = [x.lower() for x in rel_key(p, args.clips).split("/")[:-1]]
        b = "SFX" if any(x in ("sfx", "sound effects", "sound fx") for x in parts) else \
            "Music" if any(x in ("music", "song", "songs") for x in parts) else "Captured"
        audio_bins[b].append(Media(p, seq_fps, dur, has_video=False, channels=ch, rate=rate))

    event("stage", text="Writing the import for the new cards")
    root, moves = build_add_xml(name, cams, seq_fps, preroll, master_media, audio_bins, args)
    what = ", ".join(("%s Cam" % l) if nc else ("%s Cam Card %d" % (l, k)) for l, _, k, nc in cams)
    fname = re.sub(r"[^\w .,()-]+", "_", "%s - Add %d (%s).xml" % (name, n, what or "audio"))[:150]
    if not fname.endswith(".xml"):
        fname = fname[:146] + ".xml"
    xml_path = os.path.join(args.xml_out, fname)
    write_xml(root, xml_path)
    log("Wrote %s" % fname)
    if master is not None:
        write_reports(clips, args.out, seq_fps, preroll, args, {}, labels,
                      [m.path for m in audio_bins["Captured"]], stem="sync_report add %d" % n)
        report = os.path.join(args.out, "sync_report add %d.md" % n)
    else:
        report = write_clip_list(clips, [(l, cl) for l, cl, _, _ in cams], args.out, audio_bins,
                                 stem="clip_list add %d" % n)
    save_state(args, name, clips, labels, seq_fps, preroll, audio_new, prev=state, cards=cards, adds=n)
    for m_ in moves:
        log("  %s  ->  %s" % tuple(m_))
    placed = [c for c in clips if c.status == "placed"]
    aside = collections.Counter(c.reasons[0] if c.reasons else REASON_NO_MATCH
                                for c in clips if c.status != "placed") if master is not None else {}
    event("done", project=name, add=n, out=args.out, xml=xml_path, report=report,
          song_duration=round(master.duration, 2) if master is not None else 0, mode=args.mode,
          clips=len(clips), synced=len(placed),
          cameras=[dict(letter=l, name=("%s Cam" % l) + ("" if nc else " Card %d" % k), label=camera_label(l),
                        clips=len(cl), synced=sum(c.status == "placed" for c in cl),
                        spans=sorted([round(p.offset + p.src_in * c.speed, 2), round(p.offset + p.src_out * c.speed, 2)]
                                     for c in cl if c.status == "placed" for p in c.parts if p.status == "placed"))
                   for l, cl, k, nc in cams],
          set_aside=[dict(reason=r, count=k) for r, k in collections.Counter(aside).most_common()],
          audio=len(audio_new), unreadable=sum(1 for c in clips if not c.readable),
          restarted=sum(1 for c in clips if c.split),
          check_chorus=sum(1 for c in clips for p in c.parts if p.repeat_alt is not None),
          worth_a_look=len(doubtful(clips)),
          moves=moves)


def build_add_xml(name, cams, seq_fps, preroll, master_media, audio_bins, args):
    """The XML for cards added to an existing project. Premiere puts an imported XML in a bin of its
    own, so everything sits at the top of it, ready to drag into place. Returns (root, [(item,
    where it goes)])."""
    xw = Xmeml(args.path_maps)
    root = ET.Element("xmeml", version="4")
    proj = sub(root, "project")
    sub(proj, "name", name)
    top = sub(proj, "children")
    moves = []
    for letter, cl, card, new_cam in cams:
        label = camera_label(letter)
        cam_name = cam_bin_name(letter, cl)
        suffix = "" if new_cam else " Card %d" % card
        bname = cam_name if new_cam else "%s Cam Card %d" % (letter, card)
        b = bin_(top, bname, label)
        usable = [c for c in cl if c.readable and c.fps]
        for c in usable:
            xw.master_clip(b, Media.of_clip(c, seq_fps), label)
        moves.append([bname, "Footage" if new_cam else "Footage > %s (as a bin inside it)" % cam_name])
        sized = [c for c in usable if c.width]
        if sized:
            (w, h), fps = first_format(sized, seq_fps)
            entries, tc = stringout_entries(sized, fps, label)
            xw.sequence(top, "%s Cam_Breakup%s" % (letter, suffix), fps, w, h, tc, entries, label)
            moves.append(["%s Cam_Breakup%s" % (letter, suffix), "Sequence > Breakup"])
        placed = placements_of(cl)
        if placed:
            (w, h) = args.sync_size or first_format([c for c, _ in placed], seq_fps)[0]
            entries, tc = sync_entries(placed, seq_fps, preroll, master_media, args, label, song=new_cam)
            xw.sequence(top, "%s Cam_Synced%s" % (letter, suffix), seq_fps, w, h, tc, entries, label)
            moves.append(["%s Cam_Synced%s" % (letter, suffix),
                          "Sequence > Sync > Synced, then onto a new top track of %s Cam_Synced, at its start"
                          % letter if not new_cam else "Sequence > Sync > Synced"])
            xw.sequence(top, "%s Cam_Synced_Condensed%s" % (letter, suffix), seq_fps, w, h, tc,
                        condense(entries, seq_fps), label)
            moves.append(["%s Cam_Synced_Condensed%s" % (letter, suffix),
                          "Sequence > Sync > Synced Condensed, then nest it in the Edit sequence on a new track"
                          if new_cam else "Sequence > Sync > Synced Condensed, then onto a new top track of "
                          "%s Cam_Synced_Condensed, at its start" % letter])
    for bname in ("Music", "SFX", "Captured"):
        if audio_bins.get(bname):
            b = bin_(top, "%s (new)" % bname)
            for m_ in audio_bins[bname]:
                xw.master_clip(b, m_)
            moves.append(["%s (new)" % bname, "Audio > %s" % bname])
    return root, moves


if __name__ == "__main__":
    main()
