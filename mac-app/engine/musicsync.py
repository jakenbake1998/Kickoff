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
import copy
import concurrent.futures as cf
import csv
import datetime
import fractions
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import warnings
import zipfile
import zlib
import xml.etree.ElementTree as ET
import dataclasses
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy import ndimage, signal

VERSION = "0.5.56"

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
UNREADABLE_EXT = {".r3d"}   # ffmpeg cannot open RED files (BRAW opens: its sound and timecode read fine)
# RED: kickoff_r3d (built on the Mac against RED's free R3D SDK) writes a clip's sound to a WAV and prints
# its frame rate, size, length and timecode as one JSON line
R3D_HELPER = os.environ.get("KICKOFF_R3D") or os.path.expanduser("~/Library/Application Support/Kickoff/kickoff_r3d")
R3D_SPAN = re.compile(r"^(.*)_(\d{3})\.r3d$", re.I)    # A001_C001_0101AB_001.R3D, _002... one clip
OUT_DIR = "Kickoff Exports"            # what Kickoff writes, inside the shoot folder
OLD_OUT_DIRS = ("Premiere Sync",)      # its name before 0.5.3: earlier projects are still found there
SKIP_DIRS = {"SUB", "THMBNL", "GENERAL", "AVF_INFO", "CACHE", "THMB"}
# not camera originals: proxies, Premiere's preview renders and auto-saves, renders and exports
SKIP_DIR_RE = re.compile(r"prox(y|ies)|previews?\b|auto-?save|\brenders?\b|\bgenerations?\b|\bexports?\b|"
                         r"media cache|^premiere sync$|^kickoff exports$|\.(prproj|fcpbundle|drp)$", re.I)
CAM_FOLDER = re.compile(r"^(?:(?i:cam(?:era)?)(?:[ _-]+([A-Za-z])|([A-Z]))(?![A-Za-z])|"
                        r"([A-Za-z])[ _-]*(?i:cam(?:era)?)(?![A-Za-z]))")


FOLDER_LETTER = re.compile(r"^(?:(?i:media|footage|cam(?:era)?)[ _-]*)?([A-Za-z])$")


def skip_dir(name):
    return name.startswith(".") or name.upper() in SKIP_DIRS or bool(SKIP_DIR_RE.search(name)) or \
        name.lower() in (x.lower() for x in SETTINGS["skip_folders"])

# ---------------------------------------------------------------- settings (the window's Settings page)

# The house bin structure. "role" marks a bin Kickoff fills; it can be renamed or moved but must stay.
DEFAULT_BINS = [
    {"name": "Adjustment Layers", "role": "adjustment"},
    {"name": "Footage", "role": "footage"},
    {"name": "Sequence", "children": [
        {"name": "Breakup", "role": "breakup"},
        {"name": "Sync", "role": "sync", "children": [
            {"name": "Synced", "role": "synced"},
            {"name": "Synced Condensed", "role": "condensed"}]},
        {"name": "Edit", "role": "edit", "children": [{"name": "Working"}, {"name": "Past"}]}]},
    {"name": "Audio", "children": [
        {"name": "Music", "role": "music"},
        {"name": "SFX", "role": "sfx"},
        {"name": "Captured", "role": "captured"}]},
]
REQUIRED_ROLES = ("footage", "breakup", "sync", "synced", "condensed", "edit", "music")
DEFAULT_SETTINGS = {
    "sync_size": "3840x2160",       # Sync, CamsNested and Edit sequences; "first": the camera's first clip
    "start_hour": 1,                # the song starts at 01:00:00:00 (1 to 23: the preroll needs room before it)
    "track_order": "name",          # clips on V1, V2... in file order ("offset": song order)
    "labels": {},                   # camera letter -> Premiere label name (unset: CAMERA_LABELS)
    "bins": DEFAULT_BINS,
    "names": {"breakup": "{cam}_Breakup", "synced": "{cam}_Synced", "condensed": "{cam}_Synced_Condensed",
              "nested": "{project}_CamsNested", "edit": "{project}_Edit", "slowmo": "{project}_Slow Motion",
              "broll": "B Roll_Breakup"},
    "unsynced": True,               # clips that didn't sync go on V1 after the song
    "unsynced_gap_s": 60,
    "place_repeats": True,          # a take that fits two identical choruses: first copy, marked
    "skip_folders": [],             # extra folder names never scanned (on top of proxies, renders...)
    "timecode_narrative": True,     # Narrative: sync by timecode when the clip and the audio file carry it
    "timecode_music": False,        # Music video: sync by timecode (the song file must carry it)
}
SETTINGS = json.loads(json.dumps(DEFAULT_SETTINGS))


def bin_roles(bins, out=None):
    out = {} if out is None else out
    for b in bins:
        if b.get("role"):
            out[b["role"]] = b
        bin_roles(b.get("children") or [], out)
    return out


def load_settings(path):
    """The Settings page's choices (a JSON file the window writes), over the defaults. Anything
    missing or unusable keeps its default, so an old or hand-edited file can't break a run."""
    global SETTINGS
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    if not path or not os.path.isfile(path):
        SETTINGS = s
        return s
    try:
        with open(path, encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, ValueError) as e:
        log("Settings file unreadable (%s): using the defaults" % e)
        SETTINGS = s
        return s
    if not isinstance(user, dict):
        user = {}
    size = str(user.get("sync_size", s["sync_size"]))
    if size == "first" or re.match(r"^\d{2,5}x\d{2,5}$", size):
        s["sync_size"] = size
    if isinstance(user.get("start_hour"), int) and 1 <= user["start_hour"] <= 23:
        s["start_hour"] = user["start_hour"]
    if user.get("track_order") in ("name", "offset"):
        s["track_order"] = user["track_order"]
    for k in ("timecode_narrative", "timecode_music"):
        if isinstance(user.get(k), bool):
            s[k] = user[k]
    if isinstance(user.get("labels"), dict):
        s["labels"] = {k.upper(): v for k, v in user["labels"].items()
                       if isinstance(k, str) and len(k) == 1 and k.isalpha() and v in PREMIERE_LABELS}
    bins = user.get("bins")

    def clean(bl):
        out = []
        for b in bl if isinstance(bl, list) else []:
            if isinstance(b, dict) and str(b.get("name", "")).strip():
                nb = {"name": str(b["name"]).strip()[:120]}
                if b.get("role") in ROLE_NAMES:
                    nb["role"] = b["role"]
                kids = clean(b.get("children"))
                if kids:
                    nb["children"] = kids
                out.append(nb)
        return out
    if bins is not None:
        cb = clean(bins)
        roles = bin_roles(cb)
        if all(r in roles for r in REQUIRED_ROLES) and \
                sum(1 for _ in _walk(cb) if _.get("role")) == len(roles):       # each role once
            s["bins"] = cb
        else:
            log("Settings: the bin list is missing a bin Kickoff fills, so the default bins are used")
    if isinstance(user.get("names"), dict):
        for k, v in user["names"].items():
            if k in s["names"] and isinstance(v, str) and v.strip():
                s["names"][k] = v.strip()[:120]
    if isinstance(user.get("unsynced"), bool):
        s["unsynced"] = user["unsynced"]
    if isinstance(user.get("unsynced_gap_s"), (int, float)) and 0 <= user["unsynced_gap_s"] <= 3600:
        s["unsynced_gap_s"] = user["unsynced_gap_s"]
    if isinstance(user.get("place_repeats"), bool):
        s["place_repeats"] = user["place_repeats"]
    if isinstance(user.get("skip_folders"), list):
        s["skip_folders"] = [str(x).strip() for x in user["skip_folders"] if str(x).strip()][:50]
    SETTINGS = s
    return s


ROLE_NAMES = ("adjustment", "footage", "breakup", "sync", "synced", "condensed", "edit", "music", "sfx",
              "captured")


def _walk(bins):
    for b in bins:
        yield b
        yield from _walk(b.get("children") or [])


def bin_path(role):
    """Where a Kickoff bin sits, as the Settings tree has it: "Sequence > Sync > Synced"."""
    def find(bins, path):
        for b in bins:
            here = path + [b["name"]]
            if b.get("role") == role:
                return here
            got = find(b.get("children") or [], here)
            if got:
                return got
        return None
    return " > ".join(find(SETTINGS["bins"], []) or [role.capitalize()])


def seq_name(kind, letter=None, project=None, suffix=""):
    """A sequence's name from the Settings pattern: {cam} is "A Cam", {project} the project."""
    pat = SETTINGS["names"].get(kind) or DEFAULT_SETTINGS["names"][kind]
    out = pat.replace("{cam}", "%s Cam" % letter if letter else "").replace("{project}", project or "")
    return out.strip() + suffix


def start_frames(fps):
    """Timeline frame the song starts at (01:00:00:00 unless Settings says another hour)."""
    return SETTINGS["start_hour"] * 3600 * rate_xml(fps)[0]


NTSC_RATES = {23.976: 24, 29.97: 30, 47.952: 48, 59.94: 60, 119.88: 120}

REASON_UNREADABLE = "unreadable file"
REASON_NO_AUDIO = "no audio track"
REASON_SILENT = "audio track is silent"
REASON_HFR = "high frame rate (over --max-fps)"
REASON_SQ = "slow motion (S&Q, audio not real time)"
REASON_NO_MATCH = "no match to song"
REASON_LOW_CONF = "confidence below threshold"
REASON_AMBIGUOUS = "ambiguous match (repeated section of song)"
REASON_NO_PICTURE = "no picture (blank or no-signal screen)"
UNSURE_MIN_A = 6           # a clip too weak to place is still "unsure" (not "no match") when its best guess
UNSURE_OVER_CHANCE = 2.0   # has this many landmarks and this many times what chance gives


# ---------------------------------------------------------------- helpers

def log(msg):
    print(msg, file=sys.stderr, flush=True)


EVENTS = False              # --events: progress as JSON lines on stdout, for the Kickoff window


def event(kind, **data):
    if EVENTS:
        print("@@kickoff " + json.dumps(dict(data, event=kind)), flush=True)


# Footage drives (spinning or USB) slow right down when many files are read at once, so only a few
# ffmpeg decodes read at a time; the matching after each read still runs on every core.
DRIVE_READS = threading.BoundedSemaphore(max(1, int(os.environ.get("KICKOFF_READS", "3") or 3)))


def run(cmd):
    if cmd and cmd[0] == "ffmpeg":
        with DRIVE_READS:
            return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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


def fmt_song(seconds):
    """Song time as m:ss."""
    t = int(max(0.0, seconds))
    return "%d:%02d" % (t // 60, t % 60)


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
    audio_src: str = ""                  # where the sound is read from, when not the clip itself (RED: a WAV)
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
    folder_letter: str = ""     # from a folder named "a", "b", "Media_A"... (a weaker hint than "A Cam")
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
    phase: Optional[float] = None        # phase check at the offset (see phase_check): how clear the peak is
    split: bool = False                  # the song restarts / jumps inside this take (see parts)
    parts: list = field(default_factory=list)   # Part per pass of the song; one Part when not split
    seq_start_frame: Optional[int] = None
    guess: Optional[float] = None        # unsure: song time at the best guess (shown in the stringout name)
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
    phase: Optional[float] = None
    track: Optional[int] = None
    notes: list = field(default_factory=list)
    repeat_alt: Optional[float] = None
    guess: Optional[float] = None        # unsure: song time the song first plays in this pass, at the best guess


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
                elif tag == "VideoLayout" and el.get("pixel", "").isdigit() \
                        and el.get("numOfVerticalLine", "").isdigit():
                    out["size"] = (int(el.get("pixel")), int(el.get("numOfVerticalLine")))
                elif tag == "VideoFrame":
                    cap = el.get("captureFps", "")
                    m = re.match(r"([\d.]+)", cap)
                    if m:
                        out["capture_fps"] = float(m.group(1))
            return out
    return {}


def probe_r3d(clip: Clip):
    """A RED clip through kickoff_r3d: its sound to a WAV that everything else reads, the picture's
    facts from the helper's JSON. False when the helper isn't there or fails (the clip is then unreadable)."""
    if not os.path.isfile(R3D_HELPER):
        clip.probe_error = ".R3D needs RED's free R3D SDK: install it and relaunch Kickoff"
        return False
    wav = os.path.join(tempfile.gettempdir(), "kickoff-r3d",
                       hashlib.md5(clip.path.encode()).hexdigest()[:12] + ".wav")
    os.makedirs(os.path.dirname(wav), exist_ok=True)
    r = run([R3D_HELPER, clip.path, wav])
    try:
        info = json.loads((r.stdout or b"").decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        info = None
    if r.returncode != 0 or not info:
        clip.probe_error = "RED reader failed: " + ((r.stderr or b"").decode(errors="replace").strip()[-200:] or "no output")
        return False
    clip.fps = snap_fps(float(info.get("fps") or 0)) or None
    clip.width, clip.height = int(info.get("width") or 0), int(info.get("height") or 0)
    clip.duration = float(info.get("duration") or 0)
    clip.timecode = info.get("timecode") or ""
    clip.vcodec = "r3d"
    clip.model, clip.make = clip.model or "RED", clip.make or "RED"
    ch = int(info.get("channels") or 0)
    if ch:
        clip.audio_src = wav
        clip.has_audio = True
        clip.audio_channels, clip.audio_layout = ch, [ch]
        clip.audio_rate = int(info.get("rate") or 48000)
    return True


def probe(clip: Clip):
    ext = os.path.splitext(clip.path)[1].lower()
    if ext == ".r3d":
        if not probe_r3d(clip):
            clip.readable = False
        return
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
    # Sony XAVC (FX3, FX6...) keeps its timecode on a metadata stream ("rtmd"), not on the video
    clip.timecode = tags.get("timecode") or next(
        ((st.get("tags") or {}).get("timecode") for st in streams if (st.get("tags") or {}).get("timecode")), "")

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
        if not clip.width and side.get("size"):       # X-OCN: ffmpeg can't read the picture, the XML can
            clip.width, clip.height = side["size"]
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
            chans = load_channels(c.audio_src or c.path, c.audio_layout, limit=VOTE_SECONDS)
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


def song_versions(song, audio_files):
    """Other versions of the song (a v1 and a v2 mix, another EQ pass) next to it or in a Music folder:
    audio files within 10% of its length that aren't stems or effects. Up to 4."""
    near = []
    try:
        d = os.path.dirname(os.path.abspath(song))
        near = [os.path.join(d, f) for f in sorted(os.listdir(d))
                if not f.startswith(".") and os.path.splitext(f)[1].lower() in AUDIO_EXT]
    except OSError:
        pass
    music = [p for p in audio_files
             if any(x.lower() in MUSIC_DIRS for x in os.path.dirname(p).replace("\\", "/").split("/"))]
    dur = probe_audio(song)[0]
    out, seen = [], {os.path.abspath(song)}
    for p in near + music:
        a = os.path.abspath(p)
        if a in seen or NOT_SONG.search(os.path.basename(p)):
            continue
        seen.add(a)
        dp = probe_audio(p)[0]
        if dur and dp and abs(dp - dur) <= 0.1 * dur:
            out.append(p)
    return out[:4]


def song_shift(song, other):
    """Seconds to add to a time in `song` to get the same moment in `other`, when the two are the same
    edit (one mix lined up with the other all the way through), else None."""
    try:
        a, mi = load_audio(song), MasterIndex(load_audio(other))
    except RuntimeError:
        return None
    n = len(a)
    found = []
    for lo, hi in ((0, n // 2), (n // 2, n)):      # both halves must agree: same edit, same tempo
        x = a[lo:hi]
        e = evaluate(mi, *landmarks(*find_peaks(x)))
        if e is None or not accepted(e, Settings()):
            return None
        o, _, _, _ = refine_offset(x, mi, e["offset"], 0.0, len(x) / SR)
        found.append(o - lo / SR)
    return found[0] if abs(found[0] - found[1]) < 0.5 / 48 else None


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
            sp = R3D_SPAN.match(f)          # a RED clip spans _001, _002... files: it's one clip, the _001
            if sp and sp.group(2) != "001" and any(R3D_SPAN.match(g) and R3D_SPAN.match(g).group(1) == sp.group(1)
                                                  and R3D_SPAN.match(g).group(2) == "001" for g in files):
                continue
            p = os.path.join(root, f)
            if os.path.abspath(p) == master_abs:
                continue
            rel = os.path.relpath(p, clips_dir)
            parts = rel.replace("\\", "/").split("/")[:-1]
            # the camera's folder: one named like "A Cam (Mini LF)" at any depth (Footage/Day 1/...),
            # else the first folder under the one dropped
            cam = next((d for d in parts if CAM_FOLDER.match(d)), None) or here_cam or (parts[0] if parts else "")
            fl = next((m.group(1).upper() for m in (FOLDER_LETTER.match(d.strip()) for d in reversed(parts)) if m), "")
            out.append(Clip(path=p, rel=rel, top_folder=cam, folder_letter=fl))
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
    if r.returncode == 0 and r.stdout:
        return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    err = r.stderr.decode(errors="replace").strip()[-300:]
    # ffmpeg refuses some recorder poly WAVs (a channel layout that doesn't fit the channel count):
    # read every channel on its own and mix them here, else read the WAV directly
    try:
        chans = _decode_channels(path, [probe_audio(path)[1]])
        if chans:
            return np.mean([c for _, c in chans], axis=0).astype(np.float32)
    except RuntimeError:
        pass
    x = read_wav_mono(path)
    if x is not None:
        return x
    raise RuntimeError(err)


def read_wav_mono(path):
    """A PCM / float WAV (RIFF, RF64, BWF, poly) read without ffmpeg, mixed to mono at SR; None if
    it isn't one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
            if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64", b"BW64") or head[8:12] != b"WAVE":
                return None
            fmt, data_at, data_len, big = None, None, None, None
            while True:
                ck = fh.read(8)
                if len(ck) < 8:
                    break
                cid, size = ck[:4], int.from_bytes(ck[4:], "little")
                if cid == b"ds64":
                    body = fh.read(size)
                    big = int.from_bytes(body[8:16], "little")
                    fh.seek(size % 2, 1)
                    continue
                if cid == b"fmt ":
                    fmt = fh.read(size)
                    fh.seek(size % 2, 1)
                    continue
                if cid == b"data":
                    data_at = fh.tell()
                    data_len = big if (size == 0xFFFFFFFF and big) else size
                    break
                fh.seek(size + size % 2, 1)
        if fmt is None or data_at is None:
            return None
        tag, nch, rate = int.from_bytes(fmt[0:2], "little"), int.from_bytes(fmt[2:4], "little"), \
            int.from_bytes(fmt[4:8], "little")
        bits = int.from_bytes(fmt[14:16], "little")
        if tag == 0xFFFE and len(fmt) >= 26:          # WAVE_FORMAT_EXTENSIBLE: the real format is in the GUID
            tag = int.from_bytes(fmt[24:26], "little")
        width = bits // 8
        if nch < 1 or width not in (2, 3, 4) or tag not in (1, 3):
            return None
        data_len = min(data_len, os.path.getsize(path) - data_at)
        frames = data_len // (width * nch)
        raw = np.memmap(path, dtype=np.uint8, mode="r", offset=data_at, shape=(frames * width * nch,))
        g = math.gcd(int(rate), int(SR))
        up, down = SR // g, rate // g
        step = max(1, (1 << 20) // down) * down          # chunks a whole number of resampling periods
        outs = []
        from scipy.signal import resample_poly
        for f0 in range(0, frames, step):
            n = min(step, frames - f0)
            blk = raw[f0 * width * nch:(f0 + n) * width * nch]
            if tag == 3 and width == 4:
                x = blk.view("<f4").reshape(n, nch).mean(axis=1)
            elif width == 2:
                x = blk.view("<i2").reshape(n, nch).astype(np.float32).mean(axis=1) / 32768.0
            elif width == 4:
                x = blk.view("<i4").reshape(n, nch).astype(np.float32).mean(axis=1) / 2147483648.0
            else:                                     # 24-bit
                b3 = np.asarray(blk).reshape(n, nch, 3).astype(np.int32)
                v = b3[..., 0] | (b3[..., 1] << 8) | (b3[..., 2] << 16)
                v = np.where(v >= 1 << 23, v - (1 << 24), v)
                x = v.astype(np.float32).mean(axis=1) / 8388608.0
            outs.append(resample_poly(x, up, down) if up != down else x)
        x = np.concatenate(outs) if outs else np.zeros(0, np.float32)
        return np.ascontiguousarray(x, dtype=np.float32)
    except (OSError, ValueError):
        return None


MAX_CHANNELS = 16


def _decode_channels(path, layout, limit=None):
    """Every audio channel of the file as its own mono track: [(label, samples)]. Cameras put the
    scratch mic on different channels (an ARRI Mini LF: timecode on 3, mic on 4, 1-2 nearly silent),
    and a downmix buries it, so channels are never mixed before one is chosen. All the streams come
    out of one read of the file (a Mini LF has 5 audio streams, and the file used to be read once
    for each); if that fails, each stream is read on its own so one bad stream doesn't lose the rest."""
    layout = layout or [1]
    need, total = 0, 0
    for ch in layout:                             # streams needed to reach MAX_CHANNELS channels
        need += 1
        total += max(1, ch)
        if total >= MAX_CHANNELS:
            break
    raws = None
    if need > 1:
        with tempfile.TemporaryDirectory(prefix="kickoff-") as tmp:
            outs = [os.path.join(tmp, "a%d.f32" % i) for i in range(need)]
            cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path]
            for i, o in enumerate(outs):          # (-t is an output option: each output needs its own)
                cmd += (["-t", str(limit)] if limit else []) + \
                    ["-map", "0:a:%d" % i, "-vn", "-ar", str(SR), "-f", "f32le", "-acodec", "pcm_f32le", o]
            if run(cmd).returncode == 0:
                raws = [np.fromfile(o, dtype=np.float32) for o in outs]
    out = []
    for i, ch in enumerate(layout):
        if raws is not None:
            if i >= len(raws):
                break
            x = raws[i]
        else:
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


AUDIO_CACHE_GB = 10.0       # decoded camera audio kept for reruns, oldest dropped past this


def audio_cache_dir():
    """Where decoded camera audio is kept between runs (None: caching off). A rerun, an added day
    or Start over then skips the slowest step, reading the audio out of every camera file."""
    if os.environ.get("KICKOFF_NO_CACHE"):
        return None
    home = os.path.expanduser("~")
    base = os.path.join(home, "Library", "Caches", "Kickoff") if sys.platform == "darwin" \
        else os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "kickoff")
    return os.path.join(base, "audio")


def load_channels(path, layout, limit=None):
    """_decode_channels, remembered: the same file (path, size and modification time unchanged)
    read the same way comes back from the cache, sample for sample."""
    d = audio_cache_dir()
    try:
        st_ = os.stat(path)
    except OSError:
        d = None
    if d is None:
        return _decode_channels(path, layout, limit)
    import hashlib
    key = json.dumps([os.path.abspath(path), st_.st_size, st_.st_mtime_ns, SR, list(layout or [1]), limit,
                      MAX_CHANNELS, 1])
    f = os.path.join(d, hashlib.sha1(key.encode()).hexdigest() + ".npz")
    try:
        with np.load(f, allow_pickle=False) as z:
            labels = [str(v) for v in z["labels"]]
            out = [(lb, z["c%d" % i]) for i, lb in enumerate(labels)]
        os.utime(f)                                   # recently used: kept longest
        return out
    except Exception:
        pass
    out = _decode_channels(path, layout, limit)
    try:
        os.makedirs(d, exist_ok=True)
        tmp = f + ".%d.%d.tmp" % (os.getpid(), threading.get_ident())
        with open(tmp, "wb") as fh:
            np.savez(fh, labels=np.array([lb for lb, _ in out]), **{"c%d" % i: x for i, (_, x) in enumerate(out)})
        os.replace(tmp, f)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return out


def trim_audio_cache():
    """Keep the audio cache under AUDIO_CACHE_GB, dropping the least recently used files first."""
    d = audio_cache_dir()
    if not d or not os.path.isdir(d):
        return
    try:
        files = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(d) if e.is_file()]
    except OSError:
        return
    total, cap = sum(f[1] for f in files), AUDIO_CACHE_GB * 1e9
    for _, size, pth in sorted(files):
        if total <= cap:
            break
        try:
            os.remove(pth)
            total -= size
        except OSError:
            pass


def is_timecode(x):
    """LTC timecode recorded as audio: a square wave at a constant level, switching 1900-4000 times
    a second. It never matches a song; skipping it saves the time of trying. Judged second by
    second over the clip, so a silent start (the camera still locking on) or a filtered, rounded
    square wave doesn't hide it."""
    if len(x) < SR:
        return False
    n = len(x) // SR
    starts = [k * SR for k in range(n)] if n <= 40 else [int(k) * SR for k in np.linspace(0, n - 1, 40)]
    f = np.fft.rfftfreq(SR, 1 / SR)
    band = (f > 600) & (f < 2700)
    loud = ltc = 0
    for a in starts:
        seg = x[a:a + SR]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        if rms < 10 ** (-50 / 20):
            continue
        loud += 1
        zc = np.count_nonzero(np.diff(np.signbit(seg)))
        if not (1500 < zc < 5000 and float(np.median(np.abs(seg))) / rms > 0.7):
            continue
        # hiss squashed by a limiter also crosses zero that often at a near-constant level; LTC's
        # energy sits in its two tones (half the bit rate and the bit rate, 960-2400 Hz)
        spec = np.abs(np.fft.rfft(seg)) ** 2
        if float(spec[band].sum() / (spec.sum() + 1e-12)) > 0.55:
            ltc += 1
    return loud > 0 and ltc >= max(1, 0.6 * loud)


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
        self._spec, self._spec_lock = collections.OrderedDict(), threading.Lock()

    def spectrum(self, n):
        """The song's FFT at size n (np.fft.rfft(audio, n)), worked out once and reused: a long take
        is searched against the whole song hundreds of times, and each search used to redo it."""
        with self._spec_lock:
            if n in self._spec:
                self._spec.move_to_end(n)
                return self._spec[n]
        A = np.fft.rfft(self.audio, n)
        with self._spec_lock:
            self._spec[n] = A
            while len(self._spec) > 3:          # a few sizes: each can be a few hundred MB
                self._spec.popitem(last=False)
        return A

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
    deep: bool = True               # waveform searches over the whole song (channel pick, rescue) and
                                    # sped-up playback; off for a quick landmarks-only first look


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
        chans = load_channels(clip.audio_src or clip.path, clip.audio_layout)
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
    tried = []
    for label, xc in loud:
        hc, tc_ = landmarks(*find_peaks(xc))
        ec = evaluate(master, hc, tc_)
        score = (ec["conf"], ec["A"]) if ec is not None else (-1, 0)
        tried.append((score, label, xc, hc, tc_, ec))
    best = max(tried, key=lambda c: c[0])
    if st.deep and len(tried) > 1 and (best[5] is None or not accepted(best[5], st)):
        # the landmarks can't tell the channels apart (a Mini LF's ch3 carries a steady tone or noise
        # that fingerprints as well as the room mic does at chance): the channel whose waveform
        # lines up with the song clearly, all through the clip, is the scratch mic
        ph = [(phase_search(c[2], master, 0.0, len(c[2]) / SR)[1], c) for c in tried]
        r, c = max(ph, key=lambda v: v[0])
        if c is not best and r >= PHASE_AGREE and r > 1.2 * max(v[0] for v in ph if v[1] is best):
            best = c
    # The pick above is only the likeliest channel: when it doesn't place the clip (a Mini LF whose
    # ch3 is blown out while ch4 has clean scratch audio), every other channel that matched the song
    # at all gets the same full search, and the first one that places the clip is kept. Every
    # placement still has to clear the same checks, whichever channel it came from.
    # (only channels whose landmarks really hit the song: a full search on every noisy channel of every
    # clip that doesn't place was most of run 6/7's extra time)
    others = sorted((c for c in tried if c is not best and c[5] is not None and c[5]["A"] >= st.min_hashes),
                    key=lambda c: c[0], reverse=True)[:RETRY_CHANNELS]
    if not others:
        return place_on(clip, master, st, best, len(chans) > 1)
    snap = copy.deepcopy(clip)
    first = None
    for cand in [best] + others:
        work = copy.deepcopy(snap)
        place_on(work, master, st, cand, True, speeds=cand is best)
        if work.status == "placed":
            if cand is not best:
                work.notes.append("placed on %s: %s didn't match" % (cand[1], best[1]))
            clip.__dict__.update(work.__dict__)
            return
        if first is None:
            first = work
    clip.__dict__.update(first.__dict__)


RETRY_CHANNELS = 2        # other channels given the full search when the likeliest one doesn't place the clip


def place_on(clip, master, st, cand, multi, speeds=True):
    """Sync the clip on one audio channel (cand from sync_clip: score, label, samples, landmarks, match)."""
    _, label, x, h, t, ev = cand
    if multi:
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
        for k in (candidate_speeds(clip) if speeds and st.deep else []):   # slow motion: tried on the likeliest channel
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
    if st.deep and (A < st.min_hashes or ev["strength"] < 0.25 or conf < st.threshold) and xs is not None \
            and speed == 1.0:
        # too few landmarks to be sure (a short take, a sparse outro): let the waveform decide
        rescued = waveform_rescue(xs, master, [coarse] + ([ev["runner_up_offset"]] if R else []),
                                  lead=A >= st.min_hashes and ev["strength"] >= 0.25)
    if rescued is not None:
        coarse, n_ok, n, ratio = rescued
        clip.notes.append(("placed by waveform: phase peak %.1fx the next best (landmarks %d vs %d by chance)"
                           % (ratio, A, N)) if ratio else
                          ("placed by waveform: %d of %d windows line up (landmarks %d vs %d by chance)"
                           % (n_ok, n, A, N)))
    elif A < st.min_hashes or ev["strength"] < 0.25:
        # too little to place, but a guess well clear of chance (drums, where only the song's opening
        # reads clearly) is worth the editor's look: unsure, with that guess, rather than no match
        unsure = A >= UNSURE_MIN_A and A >= UNSURE_OVER_CHANCE * N and speed == 1.0
        clip.reasons.append(REASON_LOW_CONF if unsure else REASON_NO_MATCH)
        if unsure:
            clip.guess = max(0.0, coarse - aoff)
        clip.notes.append("best alignment %d landmarks vs %d by chance%s"
                          % (A, N, ", best guess song %.2fs" % (coarse - aoff) if unsure else ""))
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
            clip.guess = max(0.0, coarse - aoff)
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
        clip.phase = phase_check(xs, master, offset_audio, *ov)
    clip.status = "placed"


# A capture box (Video8, HDMI recorders) keeps recording the room sound while its picture is a solid
# "no signal" screen, so a pass can match the song perfectly over nothing but blue. Placed stretches
# are checked against the picture: one frame per second (keyframes of long-GOP files only, so it
# stays cheap), shrunk to 32x18, and a frame that is one flat color is blank.
BLANK_CODECS = {"h264", "hevc", "mpeg4", "mpeg2video", "vp9", "av1"}
BLANK_BLACK = 24           # a pixel darker than this (0-255, every channel) is black
BLANK_NEAR = 20            # a pixel this close to the frame's main color is that color
BLANK_MOSTLY = 0.5         # a part at least this blank is set aside


def blank_frame(f):
    """True for a shrunk frame (pixels x RGB) that is one solid color, with or without black bars
    around it (a Video8 capture box's blue screen is a 4:3 blue box inside black side bars), or
    pure black with nothing in it. A dark stage (most pixels near black, a few dim lights and
    faces) is real picture, so the solid color must be clearly a color (saturated, like a deck's
    blue ~4,0,148) or bright (a grey or white card), and all-black means black without texture."""
    luma = f @ np.array([0.299, 0.587, 0.114], np.float32)
    if luma.mean() < BLANK_PURE_MEAN and luma.std() < BLANK_PURE_STD:
        return True
    black = f.max(axis=1) < BLANK_BLACK
    if black.all():
        return False
    rest = f[~black]
    main = np.median(rest, axis=0)
    if main.max() - main.min() < BLANK_SATURATED and main.max() < BLANK_BRIGHT:
        return False
    solid = np.abs(rest - main).max(axis=1) < BLANK_NEAR
    return bool(solid.sum() >= 0.3 * len(f) and black.sum() + solid.sum() >= 0.97 * len(f))
BLANK_PURE_MEAN = 8.0      # an all-black frame: luma below this on average...
BLANK_PURE_STD = 2.0       # ...and this flat
BLANK_SATURATED = 60       # a flat color this saturated (max-min channel) is a "no signal" screen
BLANK_BRIGHT = 120         # or this bright (a grey or white card)
BLANK_EDGE_S = 3.0         # a blank run this long at a placed part's start or end is cut off it


def blank_seconds(path, duration):
    """Per second of the clip: True where the picture is one flat color (or unknown: None)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-skip_frame", "nokey", "-i", path,
           "-an", "-sn", "-dn", "-vf", "scale=32:18:flags=area,format=rgb24,showinfo", "-vsync", "passthrough",
           "-f", "rawvideo", "-"]
    try:
        with DRIVE_READS:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return None
    ts = [float(v) for v in re.findall(rb"pts_time:\s*([-0-9.]+)", r.stderr)]
    fr = np.frombuffer(r.stdout, np.uint8)
    n = min(len(ts), len(fr) // (32 * 18 * 3))
    if not n:
        return None
    fr = fr[:n * 32 * 18 * 3].reshape(n, 32 * 18, 3).astype(np.float32)
    flat = [blank_frame(f) for f in fr]
    secs = int(math.ceil(duration)) or 1
    out = [None] * secs
    for t, f in zip(ts, flat):                 # each keyframe stands for the time up to the next one
        i = int(t)
        if 0 <= i < secs:
            out[i] = bool(f)
    last = None
    for i in range(secs):
        if out[i] is None:
            out[i] = last
        else:
            last = out[i]
    return out


def join_parts(clip):
    """Neighbouring placed parts that sit at the same song position (within a frame) are one play of
    the song that the pass search cut in two: join them into one part. A restart to the same song
    time from later in the take has a different offset, so it stays its own part."""
    frame = 1.0 / (clip.fps or 24.0)
    out = []
    for p in clip.parts:
        q = out[-1] if out else None
        if q is not None and p.status == q.status == "placed" and p.repeat_alt is None \
                and q.repeat_alt is None and abs(p.src_in - q.src_out) < 0.05 \
                and abs(p.offset - q.offset) < frame:
            keep, other = (q, p) if (q.confidence or 0) >= (p.confidence or 0) else (p, q)
            keep.src_in, keep.src_out = q.src_in, p.src_out
            n = next((int(x.split()[1]) for x in keep.notes if x.startswith("joined ")), 1) + \
                next((int(x.split()[1]) for x in other.notes if x.startswith("joined ")), 1)
            keep.notes = [x for x in keep.notes if not x.startswith("joined ")] + \
                ["joined %d passes at the same song position" % n]
            out[-1] = keep
        else:
            out.append(p)
    clip.parts = out


def drop_blank(clip, blank=None):
    """Set aside placed stretches whose picture is blank (see blank_seconds), and cut blank runs off the
    ends of placed passes. Returns True if anything changed."""
    placed = [p for p in clip.parts if p.status == "placed"] if clip.split else \
        ([clip] if clip.status == "placed" else [])
    if not placed or (clip.vcodec or "").lower() not in BLANK_CODECS or not clip.duration:
        return False
    if blank is None:
        blank = blank_seconds(clip.path, clip.duration)
    if not blank or not any(blank):
        return False

    def frac(a, b):
        v = [blank[i] for i in range(int(a), min(len(blank), int(math.ceil(b))))]
        v = [x for x in v if x is not None]
        return sum(v) / len(v) if v else 0.0

    changed = False
    if not clip.split:
        if frac(0, clip.duration) >= BLANK_MOSTLY:
            clip.status, clip.offset = "not placed", None
            clip.reasons.append(REASON_NO_PICTURE)
            clip.notes.append("%.0f%% of the picture is a flat color" % (100 * frac(0, clip.duration)))
            return True
        return False
    parts = []
    for p in clip.parts:
        if p.status != "placed":
            parts.append(p)
            continue
        # blank runs at the ends come off first; the part goes only if what's left is mostly blank too
        # (a pass that is 59% blue at one end still has real picture in the rest)
        lo, hi = int(p.src_in), min(len(blank), int(math.ceil(p.src_out)))
        a = lo
        while a < hi and blank[a]:
            a += 1
        b = hi
        while b > a and blank[b - 1]:
            b -= 1
        f = frac(p.src_in, p.src_out)
        if f >= BLANK_MOSTLY and (b - a < BLANK_EDGE_S or frac(a, b) >= BLANK_MOSTLY):
            p.notes.append("%.0f%% of the picture is a flat color, placed at song %.1fs by its sound"
                           % (100 * f, p.offset + p.src_in * clip.speed))
            p.status, p.offset, p.reason = "not placed", None, REASON_NO_PICTURE
            parts.append(p)
            changed = True
            continue
        head = Part(p.src_in, float(a), status="not placed", reason=REASON_NO_PICTURE) \
            if a - p.src_in >= BLANK_EDGE_S else None
        tail = Part(float(b), p.src_out, status="not placed", reason=REASON_NO_PICTURE) \
            if p.src_out - b >= BLANK_EDGE_S and b > a else None
        if head:
            p.src_in = head.src_out
            parts.append(head)
        parts.append(p)
        if tail:
            p.src_out = tail.src_in
            parts.append(tail)
        if head or tail:
            p.notes.append("blank picture cut off its %s" % " and ".join(
                w for w, x in (("start", head), ("end", tail)) if x))
            changed = True
    clip.parts = parts
    if changed:
        left = [p for p in parts if p.status == "placed"]
        if left:
            f = left[0]
            clip.offset, clip.confidence, clip.drift_ms = f.offset, f.confidence, f.drift_ms
        else:
            clip.status, clip.offset = "not placed", None
            clip.reasons.append(REASON_NO_PICTURE)
    return changed


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
    """Placed parts the phase check doesn't back up (no stretch of the part peaks clearly at its
    position), or, when it couldn't run, whose waveform lines up in under 70% of its 4 s windows:
    worth a look. (The window count alone flagged nearly every real clip: camera audio of a live
    room rarely lines up in 4 s windows, even at the right spot.)"""
    def weak(p):
        if p.phase is not None:
            return p.phase < PHASE_AGREE
        return bool(p.check) and int(p.check.split("/")[0]) < 0.7 * int(p.check.split("/")[1])
    def found_by_waveform(p):    # a play only the waveform heard: likely real, but worth an eye
        return any(n.startswith("found by waveform") for n in p.notes)
    return [(c, i, p) for c in clips for i, p in enumerate(c.parts, 1)
            if p.status == "placed" and p.repeat_alt is None and (weak(p) or found_by_waveform(p))]


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
    def first_hit(ts):          # only the first matching window from each side counts, so stop there
        return next((t for t in ts if hit(t)), None)
    n = len(xs) / SR
    start = first_hit(np.arange(max(0.0, first - 2.0), min(first + 4.0, n - 1.0), 0.1))
    end = first_hit(np.arange(max(0.0, last - 4.0), min(last + 3.0, n - 1.0), 0.1)[::-1])
    if start is None or end is None:
        # the landmarks' first or last hit was chance (a drummer noodling between passes lines up
        # with the song now and then): walk in from that side to where the waveform really matches
        coarse = np.arange(first, max(first, last - 1.0), 0.5)
        c0 = first_hit(coarse)
        if c0 is None:
            return first, last
        c1 = first_hit(coarse[::-1])
        if start is None:
            start = first_hit(np.arange(max(0.0, c0 - 1.0), c0 + 0.05, 0.1))
        if end is None:
            end = first_hit(np.arange(c1, min(c1 + 1.0, n - 1.0) + 0.05, 0.1)[::-1])
        if start is None or end is None:
            raise IndexError("list index out of range")    # as before: an edge window that never matches
    return start + 0.4, end + 0.5


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
    X = master.spectrum(n) * np.conj(np.fft.rfft(seg, n))
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
    X = master.spectrum(n) * np.conj(np.fft.rfft(seg, n))
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
        wins = []
        for w in np.arange(lo, hi - 1.0 + 1e-6, 0.5):
            q = q1(o, w)                                # (worked out once, not once per known pass)
            if q >= WAVE_MATCH and all(q >= 1.5 * q1(k, w) for k in known):
                wins.append(w)
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
GAP_SHORT_S, GAP_SHORT_MIN_S = 20.0, 12.0


RUN_WINS = 3
PAIR_STRONG = 2.0          # ...or just 2 in a row when both are this clear (Day 2/3 negatives top out at 1.23)


def _phase_run_grid(xs, master, lo, hi, known, best):
    """phase_run on one grid of 10 s windows starting at lo: `best` or a longer run found here."""
    starts = list(np.arange(lo, hi - PHASE_WIN_S + 1e-6, PHASE_HOP_S))
    if len(starts) < 2:
        return best
    res = [(a,) + phase_search(xs, master, a, a + PHASE_WIN_S) for a in starts]
    i = 0
    while i < len(res):
        a, o, r = res[i]
        j = i
        if o is not None and r >= PHASE_AGREE and all(abs(o - k) >= 0.08 for k in known):
            while j + 1 < len(res) and res[j + 1][1] is not None and res[j + 1][2] >= PHASE_AGREE \
                    and abs(res[j + 1][1] - o) < PHASE_FRAME_S:
                j += 1
            n = j - i + 1
            ok = n >= RUN_WINS or (n == 2 and min(res[i][2], res[j][2]) >= PAIR_STRONG)
            if ok and (best is None or n > best[0]):
                best = (n, float(np.median([x[1] for x in res[i:j + 1]])), res[i][0],
                        res[j][0] + PHASE_WIN_S, min(x[2] for x in res[i:j + 1]))
        i = j + 1
    return best


def phase_run(xs, master, st, h, t, lo, hi, known):
    """The longest run of RUN_WINS or more consecutive 10 s windows (hop 5 s) in [lo, hi] whose phase
    peaks against the whole song all land on one position within a frame, each PHASE_AGREE clear, at
    no known pass. Shaped like phase_gap's result, or None. A second grid, shifted half a hop, catches
    a short play the first grid cuts in two (DJI 180238 at 1283: 3.02x and 1.86x on one grid, 3.64x
    and 2.44x on the other)."""
    best = None
    for shift in (0.0, PHASE_HOP_S / 2):
        best = _phase_run_grid(xs, master, lo + shift, hi, known, best)
    if best is None:
        return None
    n, o, first, last, score = best
    sel = (t >= first / FRAME_S) & (t < last / FRAME_S)
    e = evaluate(master, h[sel], t[sel]) if sel.any() else None
    if e is not None and accepted(e, st) and abs(e["offset"] - o) > 0.1:
        return None                     # the landmarks here are sure of somewhere else
    if e is None:
        e = dict(A=0, R=0, N=0.0, offset=o, runner_up_offset=o, conf=0.0, strength=0.0)
    return dict(off=o, first=first, last=last, ev=dict(e, offset=o), good=True, stray=False,
                rescued="found by waveform: %d windows in a row at one spot" % n)


SCATTER_RATIO = 1.15       # a 10 s window counts toward a scattered play when its phase peak is this clear...
SCATTER_MIN = 3            # ...and this many windows that don't overlap peak on one spot (2 when both PAIR_STRONG)
SCATTER_AGREE_S = 0.08     # "one spot": within 2 frames


def scattered_plays(xs, master, lo, hi):
    """Song positions that 10 s windows scattered through clip stretch [lo, hi] keep landing on, each
    window's phase peak against the whole song taken on its own: a faint play under a loud band (B Cam
    through a whole take at ratios 1.16-1.32) never lines windows up in a row, but chance doesn't put
    three separate windows on one spot out of the whole song. [(offset, window starts)], most first."""
    starts = list(np.arange(lo, hi - PHASE_WIN_S + 1e-6, PHASE_HOP_S))
    res = []
    for a in starts:
        o, r = phase_search(xs, master, a, a + PHASE_WIN_S)
        if o is not None and r >= SCATTER_RATIO:
            res.append((o, a, r))
    res.sort()
    out, i = [], 0
    while i < len(res):
        j = i
        while j + 1 < len(res) and res[j + 1][0] - res[i][0] < SCATTER_AGREE_S:
            j += 1
        grp = sorted(res[i:j + 1], key=lambda x: x[1])
        apart, last = [], None                  # windows that don't overlap each other
        for o, a, r in grp:
            if last is None or a - last >= PHASE_WIN_S:
                apart.append((o, a, r))
                last = a
        strong = [x for x in apart if x[2] >= PAIR_STRONG]
        if len(apart) >= SCATTER_MIN or len(strong) >= 2:
            out.append((float(np.median([x[0] for x in grp])), [x[1] for x in grp]))
        i = j + 1
    out.sort(key=lambda c: -len(c[1]))
    return out


def fill_plays(xs, master, st, h, t, passes, xs_len):
    """Two passes at one song position are one play (the song kept time between them): merge them.
    Then search every stretch outside the passes for scattered windows (scattered_plays): windows on
    a neighbouring pass's position grow that pass over them; windows on a new position, which the
    landmarks there don't contradict, become a pass of their own (a play the other searches missed)."""
    passes.sort(key=lambda p: p["first"])
    i = 0
    while i + 1 < len(passes):
        a, b = passes[i], passes[i + 1]
        if abs(a["off"] - b["off"]) < SCATTER_AGREE_S:
            a["last"], a["good"] = max(a["last"], b["last"]), a["good"] or b["good"]
            a.setdefault("rescued", "one play: its passes line up at one song position")
            del passes[i + 1]
        else:
            i += 1
    bounds = [0.0] + [v for p in passes for v in (p["first"], p["last"])] + [xs_len]
    new = []
    for gi, (lo, hi) in enumerate(zip(bounds[::2], bounds[1::2])):
        if hi - lo < 2 * PHASE_WIN_S:
            continue
        before = passes[gi - 1] if gi > 0 else None
        after = passes[gi] if gi < len(passes) else None
        for off, wins in scattered_plays(xs, master, lo, hi):
            first, last = min(wins), min(hi, max(wins) + PHASE_WIN_S)
            if before is not None and abs(off - before["off"]) < SCATTER_AGREE_S:
                before["last"] = max(before["last"], last)
                before["grown"] = True
            elif after is not None and abs(off - after["off"]) < SCATTER_AGREE_S:
                after["first"] = min(after["first"], first)
                after["grown"] = True
            elif all(abs(off - p["off"]) >= SCATTER_AGREE_S for p in passes + new) and \
                    all(n["last"] <= first or n["first"] >= last for n in new):
                sel = (t >= first / FRAME_S) & (t < last / FRAME_S)
                e = evaluate(master, h[sel], t[sel]) if sel.any() else None
                if e is not None and accepted(e, st) and abs(e["offset"] - off) > 0.1:
                    continue                    # the landmarks here are sure of somewhere else
                if e is None:
                    e = dict(A=0, R=0, N=0.0, offset=off, runner_up_offset=off, conf=0.0, strength=0.0)
                new.append(dict(off=off, first=first, last=last, ev=dict(e, offset=off), good=True,
                                stray=False, rescued="found by waveform: %d windows through the take "
                                                     "land on one spot" % len(wins)))
    passes += new
    passes.sort(key=lambda p: p["first"])


def gap_rescue(xs, master, st, h, t, lo, hi, known):
    """A whole performance in clip stretch [lo, hi] that neither the stretch's landmarks nor one phase
    correlation of the whole stretch found (a DJI rolling through a dozen plays with the band louder
    than the playback). The stretch is correlated against the song a minute at a time; a position is
    kept when the waveform lines up in most 4 s windows across 20 s or more, clearly better than at
    any other position found and at every known pass, and the landmarks there don't point elsewhere."""
    if hi - lo < STRAY_LONG_S:
        return None
    chunk = min(GAP_CHUNK_S, master.duration)
    found = phase_gap(xs, master, st, h, t, lo, hi, known, chunk)
    if found is None:
        # a play shorter than a minute is diluted in a minute of talk: 20 s stretches, where one
        # standing PHASE_ALONE clear is enough, over 12 s or more of 10 s windows that agree
        found = phase_gap(xs, master, st, h, t, lo, hi, known, min(GAP_SHORT_S, master.duration),
                          alone_only=True, min_len=GAP_SHORT_MIN_S)
    if found is None:
        # a short play under a loud band: three or more 10 s windows in a row that each peak on the
        # same song position, PHASE_AGREE clear (chance doesn't line three up within a frame)
        found = phase_run(xs, master, st, h, t, lo, hi, known)
    if found is not None:
        return found
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


def local_phase(xs, master, off, a, b, search=2.0):
    """(offset, ratio) of clip stretch [a, b] against the song within +-search s of `off`: the phase
    peak there and how far it stands above the best one 0.25 s or more away from it."""
    seg = xs[int(max(0.0, a) * SR):int(b * SR)]
    m0 = int(round((a + off - search) * SR))
    m1 = m0 + len(seg) + int(2 * search * SR)
    if len(seg) < SR or m0 < 0 or m1 > len(master.audio):
        return None, 0.0
    ref = master.audio[m0:m1]
    n = 1 << int(math.ceil(math.log2(len(ref) + len(seg))))
    X = np.fft.rfft(ref, n) * np.conj(np.fft.rfft(seg, n))
    freqs = np.fft.rfftfreq(n, 1 / SR)
    X[(freqs < 150) | (freqs > 4000)] = 0
    X /= np.maximum(np.abs(X), 1e-12)
    cc = np.fft.irfft(X, n)[:int(2 * search * SR) + 1]
    k = int(np.argmax(cc))
    ex = int(PHASE_EXCL_S * SR)
    rest = np.concatenate([cc[:max(0, k - ex)], cc[k + ex + 1:]])
    second = float(rest.max()) if len(rest) else 0.0
    return off - search + k / SR, (float(cc[k]) / second if second > 0 else 0.0)


PHASE_WIN_S, PHASE_HOP_S = 10.0, 5.0
PHASE_CHECK_S = 30.0


def phase_check(xs, master, off, lo, hi):
    """How clearly the clip stretch [lo, hi] lines up at `off`: of up to three 30 s stretches spread
    across it, the best peak ratio (see local_phase) among those whose peak lands on `off` within a
    frame; 0.0 when none does. At least PHASE_AGREE backs the position up."""
    if xs is None:
        return None
    a0, b0 = max(lo, -off), min(hi, len(xs) / SR, master.duration - off)
    if b0 - a0 < 2.0:
        return None
    L = min(PHASE_CHECK_S, b0 - a0)
    starts = [a0] if b0 - a0 <= L + 1 else list(np.linspace(a0, b0 - L, 3))
    best = 0.0
    for a in starts:
        o, ratio = local_phase(xs, master, off, a, a + L)
        if o is not None and abs(o - off) < PHASE_FRAME_S:
            best = max(best, ratio)
    return round(best, 2)


def phase_extent(xs, master, off, lo, hi, a, b):
    """The run of 10 s windows around [a, b] (inside [lo, hi]) whose own phase peak lands on `off`
    within a frame and stands PHASE_AGREE clear: where one performance at `off` starts and ends."""
    a0, b0 = in_song(xs, master, off, lo, hi)
    wins = []
    w = a0
    while w + PHASE_WIN_S <= b0 + 1e-6:
        o, ratio = local_phase(xs, master, off, w, w + PHASE_WIN_S)
        wins.append((w, o is not None and abs(o - off) < PHASE_FRAME_S and ratio >= PHASE_AGREE))
        w += PHASE_HOP_S
    hit = [i for i, (w, ok) in enumerate(wins) if ok and a - PHASE_WIN_S <= w <= b]
    if not hit:
        return None
    i = j = hit[len(hit) // 2]
    while i > 0 and wins[i - 1][1]:                    # grow while neighbouring windows agree
        i -= 1
    while j + 1 < len(wins) and (wins[j + 1][1] or (j + 2 < len(wins) and wins[j + 2][1])):
        j += 1                                         # (one weak window inside a play is allowed)
    if not wins[j][1]:
        j -= 1
    return wins[i][0], min(b0, wins[j][0] + PHASE_WIN_S)


def phase_gap(xs, master, st, h, t, lo, hi, known, chunk, alone_only=False, min_len=STRAY_LONG_S):
    """gap_rescue by phase correlation: each half-overlapping chunk of the stretch against the whole
    song. A position is kept when one chunk's peak stands PHASE_ALONE clear of anywhere else, or two
    neighbouring chunks land on it within a frame, each PHASE_AGREE clear; it covers those chunks
    (inside the song), runs STRAY_LONG_S or longer, isn't a known pass, and the landmarks in it
    aren't sure of somewhere else."""
    steps = list(np.arange(lo, max(lo, hi - chunk) + 1e-6, chunk / 2))
    res = [(a, min(hi, a + chunk)) + phase_search(xs, master, a, min(hi, a + chunk)) for a in steps]
    best = None
    for i, (a, b, o, ratio) in enumerate(res):
        if o is None or any(abs(o - k) < 0.08 for k in known):
            continue
        near = [r_ for r_ in res[max(0, i - 1):i + 2] if r_[2] is not None and abs(r_[2] - o) < PHASE_FRAME_S]
        strong = ratio >= PHASE_ALONE or (not alone_only and len(near) >= 2 and
                                          all(r_[3] >= PHASE_AGREE for r_ in near))
        if not strong:
            continue
        ext = phase_extent(xs, master, o, lo, hi, min(r_[0] for r_ in near), max(r_[1] for r_ in near))
        if ext is None:
            continue
        first, last = ext
        if last - first < min_len:
            continue
        score = ratio if alone_only or len(near) < 2 else min(r_[3] for r_ in near)
        if best is None or score > best[0]:
            best = (score, o, first, last)
    if best is None:
        return None
    score, o, first, last = best
    sel = (t >= first / FRAME_S) & (t < last / FRAME_S)
    e = evaluate(master, h[sel], t[sel]) if sel.any() else None
    if e is not None and accepted(e, st) and abs(e["offset"] - o) > 0.1:
        return None                     # the landmarks here are sure of somewhere else
    if e is None:
        e = dict(A=0, R=0, N=0.0, offset=o, runner_up_offset=o, conf=0.0, strength=0.0)
    return dict(off=o, first=first, last=last, ev=dict(e, offset=o), good=True, stray=False,
                rescued="found by waveform: phase peak %.1fx the next best" % score)


RESCUE_WIN_S, RESCUE_HOP_S = 20.0, 5.0


def waveform_rescue(xs, master, guesses, lead=False):
    """Where the landmarks are too few to decide (a 20 s take, an outro that fingerprints badly),
    compare the waveform: at each landmark guess, and at the best position of a phase correlation of
    the whole clip against the whole song. Returns (offset, windows that line up, windows) when one
    position lines up through at least half of the clip (3 windows or more) and twice as well as any
    other, else None."""
    n = len(xs) / SR
    if n < WAVE_WIN + 1:
        return None
    # the whole clip's phase correlation against the whole song: its peak standing clear of every
    # other position decides, on its own or where the landmarks' best guess lands on the same spot
    o, ratio = phase_search(xs, master, 0.0, n)
    if o is not None:
        agree = bool(guesses) and abs(o - guesses[0]) < PHASE_FRAME_S
        if ratio >= (PHASE_AGREE if agree else PHASE_ALONE):
            q = [v for _, v in wave_q(xs, master, o, *in_song(xs, master, o, 0.0, n))]
            return o, sum(v >= WAVE_MATCH for v in q), len(q), ratio
    # the song may cover only part of the clip (a short FX3 take that rolls on after the band
    # stops): a 20 s stretch whose own peak lands on the landmarks' guess, PHASE_AGREE clear
    if guesses and n >= RESCUE_WIN_S + RESCUE_HOP_S:
        best = None
        for a in np.arange(0.0, n - RESCUE_WIN_S + 1e-6, RESCUE_HOP_S):
            o, ratio = phase_search(xs, master, a, a + RESCUE_WIN_S)
            if o is not None and abs(o - guesses[0]) < PHASE_FRAME_S and ratio >= PHASE_AGREE \
                    and (best is None or ratio > best[1]):
                best = (o, ratio)
        if best is not None:
            o, ratio = best
            q = [v for _, v in wave_q(xs, master, o, *in_song(xs, master, o, 0.0, n))]
            return o, sum(v >= WAVE_MATCH for v in q), len(q), ratio
    # (only with `lead`: the landmarks found enough to point somewhere, just not surely)
    # a take whose song is too faint to peak over 20 s at once (A Cam's mic under a loud room) can
    # still peak on the landmarks' best guess in 10 s windows: three in a row there, each
    # PHASE_AGREE clear, is more than chance lines up (chance stays near 1.0-1.25 per window)
    if lead and guesses and n >= PHASE_WIN_S + (RUN_WINS - 1) * PHASE_HOP_S:
        run, best = [], None
        for a in np.arange(0.0, n - PHASE_WIN_S + 1e-6, PHASE_HOP_S):
            o, ratio = phase_search(xs, master, a, a + PHASE_WIN_S)
            if o is not None and abs(o - guesses[0]) < PHASE_FRAME_S and ratio >= PHASE_AGREE:
                run.append((o, ratio))
                if len(run) >= RUN_WINS and (best is None or len(run) > len(best)):
                    best = list(run)
            else:
                run = []
        if best is not None:
            o = float(np.median([v[0] for v in best]))
            q = [v for _, v in wave_q(xs, master, o, *in_song(xs, master, o, 0.0, n))]
            return o, sum(v >= WAVE_MATCH for v in q), len(q), min(v[1] for v in best)
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
        return o, k, m, None
    return None


REACH_GAP_WINS = 8          # a pass reaches across this many 10 s windows (hop 5 s) where its song is too quiet


def phase_reach(xs, master, off, lo, hi, way):
    """How far a pass at `off` really reaches before (way -1, from hi down to lo) or after (way 1, from
    lo up to hi) its landmark edge. 10 s windows are walked away from the edge; one whose phase peak
    lands on `off` within a frame, PHASE_AGREE clear, carries the edge along. A play can go quiet
    under the band for a while (up to REACH_GAP_WINS windows), so a run of two or more such windows
    further out still belongs to it; the walk stops at a window clearly at another song position, or
    at a longer silence. Returns the new edge (clip seconds)."""
    a0, b0 = in_song(xs, master, off, lo, hi)
    if b0 - a0 < PHASE_WIN_S:
        return hi if way < 0 else lo
    starts = list(np.arange(b0 - PHASE_WIN_S, a0 - 1e-6, -PHASE_HOP_S)) if way < 0 else \
        list(np.arange(a0, b0 - PHASE_WIN_S + 1e-6, PHASE_HOP_S))
    edge = hi if way < 0 else lo
    run, gap, far = 0, 0, None
    for w in starts:
        o, ratio = local_phase(xs, master, off, w, w + PHASE_WIN_S)
        if o is not None and abs(o - off) < PHASE_FRAME_S and ratio >= PHASE_AGREE:
            run += 1
            far = w if way < 0 else w + PHASE_WIN_S
            if gap == 0 or run >= 2:
                edge, gap = far, 0
            continue
        run = 0
        gap += 1
        if gap > REACH_GAP_WINS:
            break
        o2, r2 = phase_search(xs, master, w, w + PHASE_WIN_S)
        if o2 is not None and abs(o2 - off) > 0.08 and r2 >= PHASE_ALONE:
            break                                          # another play of the song starts here
    return edge


def grow_passes(xs, master, passes, xs_len):
    """A pass's landmarks can miss minutes of its own play (the band louder than the playback at the
    start of a DJI take). Grow each pass into the stretch before and after it, up to its neighbours,
    while its 10 s windows keep peaking on its position PHASE_AGREE clear, then find the exact edge."""
    for i, p in enumerate(passes):
        lo = passes[i - 1]["last"] if i else 0.0
        hi = passes[i + 1]["first"] if i + 1 < len(passes) else xs_len
        if p["first"] - lo < PHASE_WIN_S and hi - p["last"] < PHASE_WIN_S:
            continue
        first = phase_reach(xs, master, p["off"], lo, p["first"], -1) \
            if p["first"] - lo >= PHASE_WIN_S else p["first"]
        last = phase_reach(xs, master, p["off"], p["last"], hi, 1) \
            if hi - p["last"] >= PHASE_WIN_S else p["last"]
        if p["first"] - first < PHASE_WIN_S and last - p["last"] < PHASE_WIN_S:
            continue                                    # nothing to speak of: keep the landmark edges
        rivals = [q["off"] for q in passes if q is not p]
        f0, f1 = pass_edges(xs, master, p["off"], first, last, rivals)
        p["first"], p["last"] = max(lo, min(p["first"], f0)), min(hi, max(p["last"], f1))
        p["grown"] = True


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
        grow_passes(xs, master, passes, xs_len)
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
        fill_plays(xs, master, st, h, t, passes, xs_len)
    if len(passes) < 2 and not (passes and passes[0].get("rescued") and passes[0]["good"]):
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
                part.phase = phase_check(xs, master, o, *ov)
                k, m = (int(v) for v in part.check.split("/")) if part.check else (0, 0)
                if m >= 4 and k < 0.2 * m and not part.phase:
                    # the waveform doesn't back this position up: search the stretch on its own
                    o2 = song_search(xs, master, hlo, hhi)
                    q2 = [v for _, v in wave_q(xs, master, o2, *in_song(xs, master, o2, hlo, hhi))]
                    k2 = sum(v >= WAVE_MATCH for v in q2)
                    if abs(o2 - o) > 0.08 and k2 >= max(3, 0.5 * len(q2)) and k2 >= 2 * k + 2:
                        o, part.drift_ms, part.refine, ov = refine_offset(xs, master, o2, hlo, hhi)
                        part.offset = o - aoff
                        part.check = check_string(wave_q(xs, master, o, *ov, drift=part.drift_ms,
                                                         span=ov[1] - ov[0]))
                        part.phase = phase_check(xs, master, o, *ov)
                        part.notes.append("moved by waveform check (landmarks pointed %.1f s away)" % (p["off"] - o))
                    elif k == 0 and (sc := scattered_plays(xs, master, hlo, hhi)) and \
                            abs(sc[0][0] - o) >= SCATTER_AGREE_S:
                        # the landmarks' position is wrong for this pass (a second play of the same
                        # section splits their vote): its own windows agree on where it belongs
                        o3, wins = sc[0]
                        o, part.drift_ms, part.refine, ov = refine_offset(xs, master, o3, hlo, hhi)
                        part.offset = o - aoff
                        part.check = check_string(wave_q(xs, master, o, *ov, drift=part.drift_ms,
                                                         span=ov[1] - ov[0]))
                        part.phase = phase_check(xs, master, o, *ov)
                        part.notes.append("moved by waveform: %d windows of this pass land on song %.1fs"
                                          % (len(wins), o + p["first"]))
                    if part.status == "placed" and abs(o - p["off"]) > 0.08:
                        # a move must not pull the pass off a clear phase peak (A006C013: the refine
                        # drifted both plays 2-4 frames early while the whole pass peaked 2-3x clear)
                        po, pr = local_phase(xs, master, o, hlo, hhi, search=1.0)
                        if po is not None and pr >= PHASE_AGREE and abs(po - o) >= PHASE_FRAME_S:
                            part.notes.append("held at the phase peak (%.1fx clear), %.0f ms from the "
                                              "waveform's pick" % (pr, (o - po) * 1000))
                            o, part.drift_ms = po, 0.0
                            part.offset = o - aoff
                            part.check = check_string(wave_q(xs, master, o, *ov, span=ov[1] - ov[0]))
                            part.phase = phase_check(xs, master, o, *ov)
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
                part.phase = phase_check(xs, master, o, *ov)
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
                part.guess = p["off"] + p["first"]
                part.notes.append("this pass likely starts at song %.1fs" % (p["off"] + p["first"]))
        clip.parts.append(part)
    join_parts(clip)
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
        clip.guess = next((p.guess for p in heard if p.guess is not None), None)
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
    return m.group(1).upper() if m else clip.folder_letter


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
            # a letter the shoot gives (a folder "b" / "Media_B", else the reel in "B002C001_...")
            # decides next: the same camera often reads differently file to file (a Venice's MXFs
            # say "AXS" or "AXS-R7", its partner FX's MP4s say nothing at all)
            letter = c.folder_letter or c.reel_letter
            if letter:
                c.camera_key = "Camera letter " + letter
                continue
            ident = (("serial " + c.serial) if c.serial else ("folder " + c.top_folder) if c.top_folder else "")
            c.camera_key = model + (" / " + ident if ident else "")
    # a clip with no letter whose camera model is only ever seen under one letter is that camera
    by_model = collections.defaultdict(set)
    for c in clips:
        if c.model and c.camera_key.startswith("Camera letter "):
            by_model[c.model].add(c.camera_key)
    for c in clips:
        if group_by == "auto" and c.model and not c.camera_key.startswith("Camera ") \
                and len(by_model.get(c.model, ())) == 1:
            c.camera_key = next(iter(by_model[c.model]))
    # clips with no readable model (e.g. an unreadable raw file) join the camera they were filed with
    known = [c for c in clips if c.model]
    for c in clips:
        if c.model or c.camera_key.startswith("Camera "):
            continue
        mates = [k for k in known if (c.reel_letter and k.reel_letter == c.reel_letter) or
                 (c.folder_letter and camera_letter_hint(k) == c.folder_letter)] or \
            [k for k in known if c.top_folder and k.top_folder == c.top_folder]
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
    for key in groups:                       # then a folder or reel letter, when still free
        if key.startswith("Camera letter ") and key[-1] not in used and (key not in prior or prior[key][0] == key[-1]):
            labels[key] = key[-1]
            used.add(key[-1])
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
    # a picture ffmpeg can't size (X-OCN with its card XML left behind) takes its camera's size
    for key, cl in groups.items():
        sizes = collections.Counter((c.width, c.height) for c in cl if c.width and c.height)
        if sizes:
            for c in cl:
                if c.fps and not c.width:
                    c.width, c.height = sizes.most_common(1)[0][0]
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
    """A timecode block. With a file's own label (string), the frame count is worked out from it, so an
    app that links media by timecode (DaVinci Resolve) finds the clip."""
    drop = bool(string) and ";" in string
    if string:
        frame = tc_frames(string, fps)
        if frame is None:
            string, frame, drop = None, 0, False
        elif not drop:
            string = None                     # written back as a plain label ("17:31:30:060" -> ":60")
    tc = sub(parent, "timecode")
    add_rate(tc, fps)
    sub(tc, "string", string or fmt_frames(frame, fps))
    sub(tc, "frame", frame)
    sub(tc, "displayformat", "DF" if drop else "NDF")
    return tc


def tc_frames(tc, fps):
    """Frame count of a timecode label at the nominal rate (23.976 counts 24, 119.88 counts 120), drop
    frame (';', 29.97 and 59.94) counted the way a camera drops labels. None when it isn't a label."""
    m = re.match(r"^(\d+)[:;.](\d+)[:;.](\d+)[:;.,](\d+)$", (tc or "").strip())
    if not m or not fps:
        return None
    h, mi, s, f = (int(x) for x in m.groups())
    base = max(1, int(round(fps)))
    n = (h * 3600 + mi * 60 + s) * base + f
    if ";" in tc and base in (30, 60):
        d = base // 15                        # labels skipped each minute but every tenth
        mins = h * 60 + mi
        n -= d * (mins - mins // 10)
    return n


# Premiere label colour names, in the order cameras get them (A Iris, B Mango, C Rose, ...)
# A Iris, B Mango, C Rose (Jake's), then colors that read apart in Premiere's label list: Caribbean
# and Forest are both green there, so D and E are Yellow and Cerulean
CAMERA_LABELS = ["Iris", "Mango", "Rose", "Yellow", "Cerulean", "Caribbean", "Lavender", "Magenta",
                 "Forest", "Tan", "Violet", "Purple", "Blue", "Teal", "Green", "Brown"]


PREMIERE_LABELS = set(CAMERA_LABELS)      # all 16 of Premiere's label colors


def camera_label(letter):
    if letter in SETTINGS["labels"]:
        return SETTINGS["labels"][letter]
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

    def sequence(self, parent, name, fps, width, height, start_tc_frame, entries, label=None, fit="fill",
                 markers=()):
        """entries: dicts with media, start, vtrack (or None), atrack (or None), aenabled.
        markers: dicts with name, comment, start and end frames (a marker spanning that stretch)."""
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
        for mk in markers:                 # sequence markers, after the tracks as Premiere writes them
            m = sub(seq, "marker")
            sub(m, "comment", mk.get("comment", ""))
            sub(m, "name", mk["name"])
            sub(m, "in", mk["start"])
            sub(m, "out", mk["end"])
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
    start_tc = start_frames(seq_fps) - song_frame
    return entries, start_tc


GRIDS = (4, 9, 16)             # Premiere's Multi-Camera monitor: 2x2, 3x3, 4x4, then pages of 16


def condense(entries, fps):
    """The same clips on fewer video tracks (fewer feeds in multicam): the smallest of Premiere's
    multicam grids (4, 9, 16 angles, then pages of 16) that every overlap fits, and then every track of
    that grid used. Clips are dealt out in clip order, clip 1 on V1, clip 2 on V2... and round again
    from V1; a clip moves on to the next free track only when its own is taken at that moment.
    Nothing is cut or moved in time; its scratch audio follows it to the matching audio track."""
    vids = [e for e in entries if e.get("vtrack") and not e.get("tail")]
    spans = {id(e): (e["start"], e["start"] + entry_span(e, fps)[1]) for e in vids}
    edges = sorted([(a, 1) for a, b in spans.values()] + [(b, -1) for a, b in spans.values()],
                   key=lambda x: (x[0], x[1]))          # an end and a start on the same frame don't overlap
    need, cur = 0, 0
    for _, d in edges:
        cur += d
        need = max(need, cur)
    tracks = next((g for g in GRIDS if need <= g), -(-need // 16) * 16)
    # clip n's own track is n (mod the grid): clip 1 on V1, clip 2 on V2... Going through the clips
    # by start time, each takes its own track if that's free by then, else the next free one after
    # it; there's always one, since no more than `tracks` clips ever play at once
    rank = {id(e): n for n, e in enumerate(sorted(vids, key=lambda e: e["vtrack"]))}
    ends, place = [None] * tracks, {}
    for e in sorted(vids, key=lambda e: (spans[id(e)][0], e["vtrack"])):
        a, b = spans[id(e)]
        own = rank[id(e)] % tracks
        k = next(k % tracks for k in range(own, own + tracks) if ends[k % tracks] is None or ends[k % tracks] <= a)
        ends[k] = b
        place[id(e)] = k + 1
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
    return entries, start_frames(fps)


SLOWMO_MIN_FPS = 30.5      # a clip shot faster than this is slow motion (25 and 30 fps play as normal footage)


def is_slowmo(c):
    """Shot for slow motion: a high frame rate file (60p, 120p...) or S&Q (already conformed slow)."""
    sq = c.capture_fps and c.fps and c.capture_fps > c.fps + 1
    return bool(c.readable and c.fps and c.width and c.duration and (c.fps > SLOWMO_MIN_FPS or sq))


def slowmo_entries(cams, fps):
    """The Slow Motion sequence: every slow motion clip of every camera (camera order, then file
    order) back to back on V1, slowed to play at the sequence rate (60 fps at 40%, 120 at 20%; an S&Q
    file already plays slow, at 100%), under a "Slow Motion" marker. A minute after that, the same
    clips in the same order at real speed, under "Copy - Regular Speed". Returns (entries, markers),
    or None when there are no such clips. The clips still sync everywhere else as usual."""
    clips = [(letter, c) for letter, cl in cams for c in cl if is_slowmo(c)]
    if not clips:
        return None
    pos = 0                          # the top of the sequence (it starts at 01:00:00:00)
    entries, markers = [], []
    for title, slow in (("Slow Motion", True), ("Copy - Regular Speed", False)):
        if markers:
            pos += int(round(UNSYNCED_GAP_S * fps))
        first = pos
        for letter, c in clips:
            sq = c.capture_fps and c.capture_fps > c.fps + 1
            # entry speed stretches the timeline: 2.5 is a 60p clip at 40% in a 24p sequence (nominal
            # rates, so 59.94 in 23.976 is exactly 40% too)
            r = lambda v: round(v * 1001 / 1000) if abs(v * 1001 / 1000 - round(v * 1001 / 1000)) < 0.01 else v
            sp = (r(c.fps) / r(fps) if not sq else 1.0) if slow else (r(c.fps) / r(c.capture_fps) if sq else 1.0)
            if abs(sp - 1) < 0.01:
                sp = 1.0
            name = os.path.basename(c.path) + (" (%g%%)" % round(100 / sp) if sp != 1 else "")
            entries.append(dict(media=Media.of_clip(c, fps), start=pos, vtrack=1, atrack=1, all_audio="raw",
                                label=camera_label(letter), speed=sp, name=name))
            pos += max(1, entry_span(entries[-1], fps)[1])
        n = len(clips)
        markers.append(dict(name=title, comment="%d clip%s shot at a high frame rate" % (n, "s"[n == 1:]),
                            start=first, end=pos))
    return entries, markers


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
    """One XMEML that recreates the house bin structure (see README; the Settings page can rename,
    add, remove, reorder and nest bins, so bins are found by what Kickoff puts in them)."""
    xw = Xmeml(args.path_maps)
    root = ET.Element("xmeml", version="4")
    proj = sub(root, "project")
    sub(proj, "name", name)
    top = sub(proj, "children")
    setup_only = getattr(args, "mode", "music") == "setup"          # no song: no Sync sequences
    narr = getattr(args, "narrative", None)          # narrative: one Sync sequence, no Synced/Condensed

    def maybe_empty(parent, path, has_items):
        if not has_items and args.placeholders:
            placeholder(xw, parent, args.out, path, seq_fps)

    # the bins, in the order and nesting of Settings; role -> (children element, path of names)
    where, empty_ok = {}, []

    def make(parent, bins, path):
        for b in bins:
            if setup_only and b.get("role") in ("sync", "synced", "condensed"):
                continue
            if narr and b.get("role") in ("synced", "condensed"):
                continue
            ch = bin_(parent, b["name"])
            here = path + [b["name"]]
            if b.get("role"):
                where[b["role"]] = (ch, here)
            else:
                empty_ok.append((ch, here))
            make(ch, b.get("children") or [], here)
    make(top, SETTINGS["bins"], [])

    footage, fpath = where["footage"]
    for letter, cl in cams:
        label = camera_label(letter)
        b = bin_(footage, cam_bin_name(letter, cl), label)
        usable = [c for c in cl if c.readable and c.fps]
        for c in usable:
            xw.master_clip(b, Media.of_clip(c, seq_fps), label)
        maybe_empty(b, fpath + ["%s Cam" % letter], bool(usable))

    breakup, bpath = where["breakup"]
    syncb = where["sync"][0] if not setup_only else None
    # every take on its own track in Sync > Synced; the same packed onto as few tracks as can hold
    # them in Sync > Synced Condensed, which is what CamsNested and Edit nest (fewer multicam feeds)
    syncedb = where["synced"][0] if syncb is not None and not narr else None
    condb = where["condensed"][0] if syncb is not None and not narr else None
    nests = []
    for letter, cl in cams:
        label = camera_label(letter)
        usable = [c for c in cl if c.readable and c.fps and c.width]
        if usable:
            (w, h), fps = first_format(usable, seq_fps)
            entries, tc = stringout_entries(usable, fps, label)
            xw.sequence(breakup, seq_name("breakup", letter), fps, w, h, tc, entries, label)
        placed = placements_of(cl) if not narr else []
        if placed and syncb is not None:
            (w, h) = args.sync_size or first_format([c for c, _ in placed], seq_fps)[0]
            entries, tc = sync_entries(placed, seq_fps, preroll, master_media, args, label)
            tail, marks = unsynced_entries(cl, entries, seq_fps, preroll, master_media) \
                if SETTINGS["unsynced"] else ([], [])
            for e in tail:
                e["label"] = label
            entries += tail
            # every clip of the camera is on a track or in the stringout: say which ones can't be
            gone = [os.path.basename(c.path) for c in left_out(cl) if not (c.readable and c.duration)]
            if gone and SETTINGS["unsynced"]:
                log("Not in %s (ffmpeg can't read them): %s" % (seq_name("synced", letter), ", ".join(gone)))
            marks = unsure_markers(placed, entries, seq_fps) + marks
            xw.sequence(syncedb, seq_name("synced", letter), seq_fps, w, h, tc, entries, label, markers=marks)
            seq = xw.sequence(condb, seq_name("condensed", letter), seq_fps, w, h, tc,
                              condense(entries, seq_fps), label, markers=marks)
            nests.append((letter, seq, (w, h), tc))
    if not setup_only:
        # B Roll_Breakup: every clip that didn't sync, all cameras (camera order, then file order),
        # laid out like a camera's Breakup, each in its camera's color
        broll = [(letter, c) for letter, cl in cams
                 for c in ([c for c in cl if c.status != "placed"] if narr else left_out(cl))
                 if c.readable and c.fps and c.width and c.duration]
        if broll:
            (w, h), fps = first_format([c for _, c in broll], seq_fps)
            entries, pos = [], 0
            for letter, c in broll:
                entries.append(dict(media=Media.of_clip(c, fps), start=pos, vtrack=1, atrack=1,
                                    label=camera_label(letter), all_audio="raw"))
                pos += int(round(c.duration * fps))
            xw.sequence(breakup, seq_name("broll"), fps, w, h, start_frames(fps), entries)
    maybe_empty(breakup, bpath, bool(len(breakup)) or bool(slowmo_entries(cams, seq_fps)))
    if narr and syncb is not None:
        (w, h) = args.sync_size or (first_format([c for c in clips if c.readable and c.fps and c.width], seq_fps)[0]
                                    if any(c.width for c in clips) else (3840, 2160))
        xw.sequence(syncb, narr["name"], seq_fps, w, h, start_frames(seq_fps), narr["entries"])
    elif syncb is not None:
        maybe_empty(syncedb, where["synced"][1], bool(nests))
        maybe_empty(condb, where["condensed"][1], bool(nests))

    edit = where["edit"][0]
    # the _Edit sequence goes in Edit > Working when that bin is there (else in Edit itself)
    working = next((ch for ch, here in empty_ok if here == where["edit"][1] + ["Working"]), edit)
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
        xw.sequence(condb if condb is not None else syncb, seq_name("nested", project=name), seq_fps, w, h, tc,
                    all_cams())
        xw.sequence(working, seq_name("edit", project=name), seq_fps, w, h, tc, all_cams())
    elif setup_only or narr:
        # an empty sequence to cut in, at the delivery size, starting at 01:00:00:00
        usable = [c for c in clips if c.readable and c.fps and c.width]
        (w, h) = args.sync_size or (first_format(usable, seq_fps)[0] if usable else (3840, 2160))
        xw.sequence(working, seq_name("edit", project=name), seq_fps, w, h, start_frames(seq_fps), [])
    slow = slowmo_entries(cams, seq_fps)
    if slow:
        (w, h) = args.sync_size or (3840, 2160)
        entries, marks = slow
        xw.sequence(breakup, seq_name("slowmo", project=name), seq_fps, w, h, start_frames(seq_fps), entries,
                    markers=marks)
    maybe_empty(edit, where["edit"][1], bool(len(edit)))

    # audio by folder: a bin removed in Settings sends its files on (SFX to Captured, then to Music)
    home = {"Music": "music", "SFX": next(r for r in ("sfx", "captured", "music") if r in where),
            "Captured": next(r for r in ("captured", "music") if r in where)}
    filled = collections.Counter()
    for kind in ("Music", "SFX", "Captured"):
        items = ([master_media] if kind == "Music" and master_media else []) + audio_bins.get(kind, [])
        for m in items:
            xw.master_clip(where[home[kind]][0], m)
        filled[home[kind]] += len(items)
    for role in ("adjustment", "music", "sfx", "captured"):
        if role in where:
            maybe_empty(where[role][0], where[role][1], bool(filled[role]))
    for ch, path in empty_ok:
        maybe_empty(ch, path, bool(len(ch)))
    defined_before_use(root)
    return root


def defined_before_use(root):
    """A nested sequence must be written out before anything references it. Settings can put the
    Edit bin above the Sync bin, so where a reference comes first in the file, the two swap: the
    full sequence goes where it's first used, the bin keeps a reference (as Premiere writes them)."""
    seen = set()
    full = {s.get("id"): s for s in root.iter("sequence") if len(s)}
    for s in list(root.iter("sequence")):
        sid = s.get("id")
        if len(s):
            seen.add(sid)
        elif sid not in seen and sid in full:
            d = full[sid]
            for k in list(d):
                d.remove(k)
                s.append(k)
            for k, v in d.attrib.items():
                s.set(k, v)
            full[sid] = s
            seen.add(sid)


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


UNSURE_REASONS = (REASON_LOW_CONF, REASON_AMBIGUOUS, REASON_SHORT)


def is_unsure(c):
    return bool(c.reasons) and c.reasons[0] in UNSURE_REASONS


def left_out(clips):
    """Clips of a camera with no pass on a Synced track: every one of them goes in the stringout."""
    return [c for c in clips if not (c.status == "placed" and any(p.status == "placed" for p in c.parts))]


def unsynced_entries(clips, entries, seq_fps, preroll_s, master_media):
    """Footage of a camera that isn't on a Synced track, back to back on V1 a minute after the song
    and every synced take have ended: whole clips that didn't line up, and the passes of a restarted
    take that weren't placed (between plays too: no footage is ever dropped). The unsure ones first,
    then the rest, each group in file order, each with all its camera audio on A2, A3... (the song is
    on A1), named with why it wasn't synced. Returns (entries, markers), one marker spanning each group."""
    items = []                          # (clip, part or None for the whole clip, unsure?)
    for c in clips:
        if not (c.readable and c.duration):
            continue
        if left_out([c]):
            items.append((c, None, is_unsure(c)))
        elif c.split:
            items += [(c, p, p.reason in UNSURE_REASONS) for p in c.parts
                      if p.status != "placed" and p.src_out - p.src_in >= 1.0]
    if not items:
        return [], []
    ends = [int(round(preroll_s * seq_fps)) + int(round((master_media.duration if master_media else 0) * seq_fps))]
    ends += [e["start"] + entry_span(e, seq_fps)[1] for e in entries if e.get("media") is not None]
    pos, out, markers = max(ends) + int(round(UNSYNCED_GAP_S * seq_fps)), [], []
    for title, group in (("Unsure", [it for it in items if it[2]]), ("No match", [it for it in items if not it[2]])):
        if not group:
            continue
        if markers:                     # a minute between the Unsure clips and the No match ones too
            pos += int(round(UNSYNCED_GAP_S * seq_fps))
        first = pos
        for c, p, _ in group:
            reason = p.reason if p is not None else (c.reasons or ["not synced"])[0]
            why = re.split(r"\s*[(;]", reason or "not synced")[0].strip()
            if p is None and c.status == "placed":      # placed, but no pass of it made it onto a track
                why = "not synced"
            guess = p.guess if p is not None else c.guess
            if guess is not None and title == "Unsure":
                why += ", song %s?" % fmt_song(guess)
            if p is not None:
                why = "pass %d of %d, %s" % (c.parts.index(p) + 1, len(c.parts), why)
            src = (p.src_in, p.src_out) if p is not None else None
            out.append(dict(media=Media.of_clip(c, seq_fps), start=pos, vtrack=1, atrack=2, all_audio="raw",
                            label=None, tail=True, src=src, name="%s (%s)" % (os.path.basename(c.path), why)))
            pos += max(1, entry_span(out[-1], seq_fps)[1])
        n = len(group)
        markers.append(dict(name=title, comment="%d clip%s or passes that didn't sync" % (n, "s"[n == 1:]),
                            start=first, end=pos))
    return out, markers


def unsure_markers(placed, entries, fps):
    """An "Unsure" marker spanning each placed pass the waveform doesn't back up (worth a look)."""
    weak = {id(p) for c, _, p in doubtful([c for c, _ in placed])}
    out = []
    for (c, p), e in zip(placed, [e for e in entries if e.get("vtrack") and not e.get("tail")]):
        if id(p) in weak:
            out.append(dict(name="Unsure", comment="%s: the waveform only partly lines up here"
                            % (clip_name(c, p) or os.path.basename(c.path)),
                            start=e["start"], end=e["start"] + entry_span(e, fps)[1]))
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
    xw.sequence(root, seq_name("synced", letter), seq_fps, w, h, tc, entries, label)
    return root


def write_xml(root, path, resolve=False):
    """The XML file. resolve=True also writes '<name> - Resolve.xml' beside it for DaVinci Resolve."""
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n')
        fh.write(body)
        fh.write("\n")
    if resolve:
        rpath = os.path.splitext(path)[0] + " - Resolve.xml"
        write_xml(resolve_copy(root), rpath)
        log("Wrote %s (for DaVinci Resolve)" % rpath)


def resolve_copy(root):
    """The same project for DaVinci Resolve, which reads a nested sequence only where its full contents
    are written (a reference by id comes in empty) and ignores a track's on/off switch. So every use of
    a nest gets the whole sequence, and the clips on a switched-off track are switched off themselves."""
    r = copy.deepcopy(root)
    full = {s.get("id"): s for s in r.iter("sequence") if len(s)}
    for _ in range(8):                         # nests inside nests fill on the next round
        refs = [s for s in r.iter("sequence") if not len(s) and s.get("id") in full]
        if not refs:
            break
        for s in refs:
            for k in full[s.get("id")]:
                s.append(copy.deepcopy(k))
    for t in r.iter("track"):
        if t.findtext("enabled") == "FALSE":
            for ci in t.findall("clipitem"):
                en = ci.find("enabled")
                if en is not None:
                    en.text = "FALSE"
    return r


# ---------------------------------------------------------------- reports

COLUMNS = ["file", "status", "reason", "camera", "track", "pass", "clip_range", "playback_speed", "offset_seconds",
           "offset_timecode", "timeline_timecode", "confidence", "waveform_check", "phase_check", "matching_landmarks",
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
                                check=part.check, phase=part.phase, notes=part.notes)
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
        "timeline_timecode": fmt_frames(start_frames(fps) + int(round(c.offset * fps)), fps)
        if c.offset is not None else "",
        "confidence": "%.1f" % c.confidence if c.confidence is not None else "",
        "waveform_check": ("%s windows match" % c.check) if c.check else "",
        "phase_check": "%.2f" % c.phase if c.phase is not None else "",
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
    if getattr(args, "song_note", ""):
        L.append("")
        L.append("**Song version:** %s" % args.song_note)
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
            why = "found by the waveform alone, not the landmarks" \
                if any(n.startswith("found by waveform") for n in p.notes) else "%s windows match" % p.check
            L.append("- %s%s: %s" % (c.rel, " pass %d" % i if c.split else "", why))
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

# ---------------------------------------------------------------- Slop Cut

# A rough first assembly, cut for you: a copy of the Edit sequence with every camera's nest cut on
# the beat, the camera picked for each shot enabled and the rest disabled (never deleted, so every
# angle is still there to switch on). It reads the finished project XML and the song; sync
# placement is never touched.
SLOP_MIN_S, SLOP_MAX_S = 2.0, 8.0
SLOP_SR, SLOP_HOP = 22050, 512             # 23.2 ms analysis frames
SLOP_NAME = "Slop Cut"
# Pace: (shortest shot s, longest shot s, shot lengths x). Genre: (pace notches, rapid cuts on drum bursts)
SLOP_PACES = {"chill": (3.0, 12.0, 2.0), "normal": (SLOP_MIN_S, SLOP_MAX_S, 1.0), "fast": (1.0, 5.0, 0.5),
              "frantic": (0.5, 3.0, 0.25)}
SLOP_PACE_ORDER = ["chill", "normal", "fast", "frantic"]
SLOP_GENRES = {"auto": (0, False), "punk": (1, True), "pop": (0, False), "hiphop": (0, False), "ballad": (-1, False)}


def slop_style(pace="normal", genre="auto", bpm=None):
    """How the Slop Cut cuts: the pace picked, nudged by the genre (punk/rock one notch faster with a
    burst of quick cuts whenever the drums go off, a ballad one notch calmer), and the BPM if typed."""
    pace = pace if pace in SLOP_PACES else "normal"
    genre = genre if genre in SLOP_GENRES else "auto"
    shift, bursts = SLOP_GENRES[genre]
    i = max(0, min(len(SLOP_PACE_ORDER) - 1, SLOP_PACE_ORDER.index(pace) + shift))
    lo, hi, scale = SLOP_PACES[SLOP_PACE_ORDER[i]]
    return dict(pace=SLOP_PACE_ORDER[i], genre=genre, min_s=lo, max_s=hi, scale=scale,
                bursts=bursts or i >= 2, bpm=bpm if bpm and 40 <= bpm <= 300 else None)


def slop_audio(path):
    r = run(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", str(SLOP_SR),
             "-f", "s16le", "-acodec", "pcm_s16le", "-"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace").strip()[-300:])
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def rank01(v):
    """Each value's place among all of them, 0 (quietest) to 1 (loudest)."""
    v = np.asarray(v, float)
    if len(v) < 2:
        return np.zeros_like(v)
    return np.argsort(np.argsort(v, kind="stable"), kind="stable") / (len(v) - 1.0)


def track_beats(onset, fps, want_bpm=None):
    """Beat frames from an onset curve: tempo from its autocorrelation (leaning towards 120 BPM),
    then the beats by dynamic programming (Ellis 2007): each beat sits on a strong onset about one
    beat after the last."""
    o = onset - onset.mean()
    ac = np.correlate(o, o, "full")[len(o) - 1:]
    lags = np.arange(len(ac))
    lo, hi = int(fps * 60 / 200), int(fps * 60 / 70)
    bpm = 60.0 * fps / np.maximum(lags[lo:hi], 1)
    w = ac[lo:hi] * np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)
    k = int(np.argmax(w))
    period = float(lo + k)
    if want_bpm:                                 # the tempo the editor typed in wins
        period, k = 60.0 * fps / want_bpm, -1
    if 0 < k < len(w) - 1:                       # between whole frames: the peak of a parabola through 3
        d = w[k - 1] - 2 * w[k] + w[k + 1]
        if d < 0:
            period += 0.5 * (w[k - 1] - w[k + 1]) / d
    score = (onset / (onset.std() + 1e-9)).astype(float)
    back = np.full(len(onset), -1)
    win = np.arange(-int(round(2 * period)), -int(round(period / 2)) + 1)
    pen = -100.0 * np.log(-win / period) ** 2
    for t in range(len(onset)):
        prev = t + win
        ok = prev >= 0
        if not ok.any():
            continue
        cand = score[prev[ok]] + pen[ok]
        k = int(np.argmax(cand))
        if cand[k] > 0:
            score[t] += cand[k]
            back[t] = prev[ok][k]
    tail = int(round(period * 2))
    t = int(np.argmax(score[-tail:])) + max(0, len(score) - tail)
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    return np.array(beats[::-1]), 60.0 * fps / period


def song_analysis(path, bpm=None):
    """What happens in the song, beat by beat: beat and bar times, how hard each instrument
    group is playing, drum fills, and where sections change. Band splits of a harmonic/percussive
    separated spectrogram, not real stems: good for drums and bass, rougher for guitars vs keys."""
    x = slop_audio(path)
    dur = len(x) / SLOP_SR
    _, _, Z = signal.stft(x, fs=SLOP_SR, window="hann", nperseg=2048, noverlap=2048 - SLOP_HOP,
                          boundary=None, padded=False)
    freqs = np.fft.rfftfreq(2048, 1.0 / SLOP_SR)
    keep = freqs < 6000
    S = np.abs(Z[keep]).astype(np.float32)
    freqs = freqs[keep]
    fps = SLOP_SR / SLOP_HOP
    H = ndimage.median_filter(S, size=(1, 17))           # steady along time: notes
    P = ndimage.median_filter(S, size=(17, 1))           # steady along frequency: hits
    mh = H ** 2 / (H ** 2 + P ** 2 + 1e-12)
    Sh, Sp = S * mh, S * (1 - mh)

    def band(M, a, b):
        return M[(freqs >= a) & (freqs < b)].sum(axis=0)
    lp = np.log1p(100 * Sp)
    onset = np.maximum(0, np.diff(lp, axis=1, prepend=lp[:, :1])).sum(axis=0)
    onset = ndimage.gaussian_filter1d(onset, 1)
    beats, bpm = track_beats(onset, fps, bpm)
    beat_t = beats / fps + 1024.0 / SLOP_SR              # a frame's time is the middle of its window
    if len(beat_t) < 8:                                   # no pulse to speak of: a cut every 2 s
        beat_t = np.arange(0, dur, 0.5)
        bpm = 120.0
    # downbeats: the beat of four with the most kick on it
    kick = band(Sp, 30, 150)
    kick_on = np.maximum(0, np.diff(np.log1p(100 * kick), prepend=0))
    on_beat = np.array([kick_on[max(0, int(t * fps) - 2):int(t * fps) + 3].max(initial=0) for t in beat_t])
    phase = int(np.argmax([on_beat[k::4].mean() if len(on_beat[k::4]) else 0 for k in range(4)]))

    edges = np.append(beat_t, dur)
    feats = {"drums": band(Sp, 30, 6000), "bass": band(Sh, 35, 250), "mid": band(Sh, 250, 3500),
             "high": band(Sh, 1500, 6000), "keys": band(Sh, 250, 2000), "loud": S.sum(axis=0)}
    nf = S.shape[1]

    def beat_mean(v, a, b):
        i = min(int(a * fps), nf - 1)
        return float(v[i:max(i + 1, min(nf, int(b * fps)))].mean())
    per = {k: np.array([beat_mean(v, a, b) for a, b in zip(edges[:-1], edges[1:])]) for k, v in feats.items()}
    busy = np.array([beat_mean(onset, a, b) for a, b in zip(edges[:-1], edges[1:])])
    nb = len(beat_t)
    # vocals: mid-band notes rising above the bed under them (lines come and go; guitars sit there)
    mid = np.log1p(per["mid"])
    bed = ndimage.uniform_filter1d(ndimage.minimum_filter1d(mid, 17), 9)
    vox = rank01(mid - bed)
    act = {"drums": rank01(per["drums"]), "bass": rank01(per["bass"]), "vocals": vox,
           "guitar": rank01(np.log1p(per["mid"]) + 0.5 * np.log1p(per["high"])),
           "keys": rank01(per["keys"]), "loud": rank01(per["loud"])}
    for k in act:
        act[k] = ndimage.uniform_filter1d(act[k], 3)
    # a solo: bright notes and no singing; a bass lead: bass up, the rest down
    solo = (rank01(per["high"]) > 0.75) & (vox < 0.35)
    bass_lead = (act["bass"] > 0.7) & (act["guitar"] < 0.45) & (vox < 0.45)
    # sections: where the sound of 8 bars changes from the 8 before (a checkerboard over band levels)
    F = np.vstack([ndimage.uniform_filter1d(np.log1p(per[k]), 4) for k in ("drums", "bass", "mid", "high", "loud")])
    F = (F - F.mean(axis=1, keepdims=True)) / (F.std(axis=1, keepdims=True) + 1e-9)
    w = 16
    nov = np.zeros(nb)
    for i in range(w, nb - w):
        nov[i] = np.linalg.norm(F[:, i:i + w].mean(axis=1) - F[:, i - w:i].mean(axis=1))
    peaks, _ = signal.find_peaks(nov, distance=16, height=max(0.8, np.percentile(nov, 80)) if nb > 2 * w else 1e9)
    sections = sorted({p - ((p - phase) % 4) for p in peaks if p - ((p - phase) % 4) > 0})
    # fills: the last beats before a new section or 8-bar phrase, busier than the drums usually are
    fills = []
    marks = set(sections) | {b for b in range(phase, nb, 32) if b > 0}
    usual = np.median(busy) + 1e-9
    for b in sorted(marks):
        lead = busy[max(0, b - 2):b]
        if len(lead) and lead.mean() > 1.5 * usual:
            fills.append(b)
    # drum bursts: beats where the drums go off (much busier than usual and up front), the singing
    # and the rest backing off a little: a fill, a blast or a short drum solo
    burst = (busy > 1.8 * usual) & (act["drums"] > 0.6) & (vox < 0.6)
    return dict(dur=dur, bpm=bpm, beats=beat_t, phase=phase, act=act, solo=solo, bass_lead=bass_lead,
                sections=sections, fills=fills, burst=burst)


def shot_grid(an, seed=0, style=None):
    """Where the cuts go, in song seconds: on bar lines (beats when bars are too long), at every new
    section, into each drum fill and where the singing comes in. Shot lengths change with the song:
    1 to 2 bars when it's loud, 2 to 8 when it's calm, always 2 to 8 s (the pace scales all of it).
    With bursts on, every drum burst becomes a run of cuts, one a beat (two in Frantic). Returns
    (times, fill shot starts, section starts, burst shot starts)."""
    style = style or slop_style()
    lo, hi, scale = style["min_s"], style["max_s"], style["scale"]
    beats, nb = an["beats"], len(an["beats"])
    bar_len = 4 * 60.0 / an["bpm"]
    step = 1 if bar_len > hi or scale < 1 else 4
    bars = list(range(an["phase"] % step, nb, step))
    t_of = lambda b: float(beats[b]) if b < nb else an["dur"]
    act = an["act"]
    vox = act["vocals"]
    vox_in = {b for b in bars if b >= 4 and vox[b] > 0.6 and vox[max(0, b - 4):b].mean() < 0.4}
    marks = set(an["sections"]) | set(an["fills"]) | vox_in
    cuts, fill_starts, b, n = [0], set(), 0, 0
    while True:
        n += 1
        lively = float(act["loud"][min(b, nb - 1)])
        choices = (1, 2, 2, 4) if lively > 0.7 else (2, 4, 4, 8) if lively > 0.4 else (4, 8, 8)
        want = choices[zlib.crc32(("%d %d" % (n, seed)).encode()) % len(choices)] * bar_len * scale
        nxt = [k for k in bars if lo - 1e-6 <= t_of(k) - t_of(b) <= hi + 1e-6]
        forced = [k for k in nxt if k in marks and t_of(k) - t_of(b) <= want + 1e-6]
        if forced:
            k = forced[0]
            if k in an["fills"]:      # a drum shot that lasts 2 s or more and ends on the downbeat
                f = max([j for j in range(b + 1, k) if t_of(k) - t_of(j) >= lo - 1e-6
                         and t_of(j) - t_of(b) >= lo - 1e-6], default=None)
                if f is not None:
                    cuts += [f, k]
                    fill_starts.add(t_of(f))
                    b = k
                    continue
        elif nxt:
            k = min(nxt, key=lambda k: abs(t_of(k) - t_of(b) - want))
        else:
            k = min([j for j in range(b + 1, nb + 1) if t_of(j) - t_of(b) >= lo - 1e-6], default=nb)
        if k >= nb or an["dur"] - t_of(k) < lo:
            break
        cuts.append(k)
        b = k
    times = [0.0] + [t_of(k) for k in cuts[1:]] + [an["dur"]]
    bursts = set()
    if style["bursts"] and an.get("burst") is not None:
        on = list(np.flatnonzero(an["burst"]))
        runs, cur = [], []
        for k in on:
            if cur and k != cur[-1] + 1:
                runs.append(cur)
                cur = []
            cur.append(k)
        if cur:
            runs.append(cur)
        half = style["pace"] == "frantic" and an["bpm"] < 150
        for run in (r for r in runs if len(r) >= 2):
            t0, t1 = t_of(run[0]), t_of(run[-1] + 1)
            sub = [t_of(k) for k in run]
            if half:
                sub += [(t_of(k) + t_of(k + 1)) / 2 for k in run]
            times = [t for t in times if not (t0 - 0.3 < t < t1 + 0.3)] + sub + ([t1] if t1 < an["dur"] - 0.3 else [])
            bursts |= set(sub)
        times = sorted(set(times) | {0.0, an["dur"]})
    return times, fill_starts, {t_of(k) for k in an["sections"]} | {0.0}, bursts


# who a frame shows, from Apple Vision's labels (classification identifiers, matched by substring)
SHOT_WORDS = {"drums": ("drum", "cymbal", "percussion"), "guitar": ("guitar", "banjo", "ukulele"),
              "keys": ("piano", "keyboard", "organ", "synthesizer", "accordion"),
              "vocals": ("microphone", "singer", "karaoke"), "wide": ("concert", "stage", "crowd", "band")}


def frame_roles(f):
    """{role: 0..1} for one tagged frame."""
    labels = {k.lower(): v for k, v in (f.get("labels") or {}).items()}
    hit = lambda words: max([v for k, v in labels.items() if any(w in k for w in words)], default=0.0)
    # instruments count from 0.3 up (a sword can read as a guitar at 0.2); Vision's "singer" and
    # "microphone" stay low even with the singer at the mic, so they count from 0.08
    r = {k: min(1.0, max(0.0, (hit(w) - 0.3) / 0.4)) for k, w in SHOT_WORDS.items()}
    r["vocals"] = min(1.0, max(0.0, (hit(SHOT_WORDS["vocals"]) - 0.08) / 0.3))
    r["bass"] = 0.8 * r["guitar"]                     # a bass reads as a guitar to Vision
    people, big, face = f.get("people", 0), f.get("big", 0.0), f.get("face", 0.0)
    instr = max(r["drums"], r["guitar"], r["keys"])
    eyes = f.get("eyes")                              # None: a shot finder too old to say
    if face > 0.16 and instr < 0.3 and eyes != 0:     # a close-up of a face, no instrument: the singer
        r["vocals"] = max(r["vocals"], 0.7)
    if eyes is not None:
        if not eyes:                                  # no eyes to see (back of the head, a profile): not for the singing
            r["vocals"] = min(r["vocals"], 0.05)
        r["eyes"] = float(eyes)
    if people >= 3 or (people >= 2 and big < 0.55):
        r["wide"] = max(r["wide"], 0.8)
    return r


def plan_takes(an, takes, seed=0, style=None):
    """Shots in song seconds [(start, end, take index or None)]. takes: dicts with cam, spans [(a, b)
    in song seconds] and tags(a, b) -> {role: 0..1} or None (not looked at). Each shot goes to the
    take showing the player the music calls for; a take that keeps winning just keeps playing
    (up to 8 s), so the cuts don't fall into a steady pattern."""
    style = style or slop_style()
    times, fill_starts, starts, bursts = shot_grid(an, seed, style)
    beats, nb, act = an["beats"], len(an["beats"]), an["act"]

    def covers(spans, a, b):
        return sum(max(0.0, min(b, y) - max(a, x)) for x, y in spans) / max(1e-6, b - a)

    shots, used = [], collections.Counter()
    for a, b in zip(times[:-1], times[1:]):
        i0 = min(int(np.searchsorted(beats, a)), nb - 1)
        i1 = min(max(i0 + 1, int(np.searchsorted(beats, b))), nb)
        m = lambda k: float(np.mean(act[k][i0:i1]))
        need = {"drums": m("drums") + (2.0 if a in fill_starts or a in bursts else 0.0), "vocals": 1.3 * m("vocals"),
                "guitar": m("guitar") + (0.8 if an["solo"][i0:i1].mean() > 0.5 else 0.0),
                "bass": 0.8 * m("bass") + (0.6 if an["bass_lead"][i0:i1].mean() > 0.5 else 0.0),
                "keys": 0.6 * m("keys"), "wide": 0.5 + (0.8 if a in starts else 0.0)}
        cov = [covers(t["spans"], a, b) for t in takes]
        pool = [i for i, c in enumerate(cov) if c > 0.97] or \
               [i for i, c in enumerate(cov) if c > 0 and c == max(cov, default=0)]
        if not pool:
            shots.append([a, b, None])
            continue
        prev = shots[-1] if shots and shots[-1][2] is not None else None
        total = sum(used.values()) or 1.0

        def score(i):
            tag = takes[i]["tags"](a, b) or {}
            s = sum(need[r] * (0.15 + 0.85 * tag.get(r, 0.0)) for r in need) if tag else sum(need.values()) * 0.3
            if tag and "eyes" in tag and need["vocals"] >= max(need.values()) and tag["eyes"] < 0.5:
                s -= 0.6                         # the singing: only where the singer's eyes are seen
            if prev and i == prev[2]:
                # carrying on in the same take: fine while the shot stays under 8 s, else move on;
                # in a drum burst, every beat is a new angle
                s += -3.0 if a in bursts else 0.15 if b - prev[0] <= style["max_s"] + 1e-6 else -10.0
            elif prev and takes[i]["cam"] == takes[prev[2]]["cam"]:
                s -= 0.25
            s -= 0.8 * used[i] / total
            return s + 0.05 * (zlib.crc32(("%d %.2f %d" % (i, a, seed)).encode()) % 100) / 100.0
        i = max(pool, key=score)
        used[i] += b - a
        if prev and i == prev[2] and shots[-1][1] == a:
            shots[-1][1] = b                     # the same take carries on: no cut here
        else:
            shots.append([a, b, i])
    return [tuple(s) for s in shots]


def xml_fps(el):
    r = el.find("rate")
    base = float(r.findtext("timebase") or 24)
    return base * 1000 / 1001 if (r.findtext("ntsc") or "").upper() == "TRUE" else base


def url_to_path(u):
    p = urllib.parse.urlparse(u)
    path = urllib.parse.unquote(p.path)
    if p.netloc and p.netloc != "localhost":
        path = "//" + p.netloc + path
    return path


# Mac only: kickoff_vision (Apple Vision, built by the updater) looks at frames of a clip and prints
# one JSON line per frame: the labels it sees, how many people and faces, and how big they are
VISION_HELPER = os.environ.get("KICKOFF_VISION") or os.path.expanduser(
    "~/Library/Application Support/Kickoff/kickoff_vision")
VISION_STEP = 1.0


def proxy_of(path):
    """A camera proxy for path (Proxies/<name>_Proxy.mov and the like, next to it or up to 3 folders up),
    for files macOS can't play itself (ARRI MXF); proxies run in step with their originals."""
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    d = os.path.dirname(path)
    for _ in range(4):
        for sub in ("", "Proxies", "Proxy", "proxies", "proxy"):
            folder = os.path.join(d, sub)
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for n in names:
                ns, ext = os.path.splitext(n)
                if ext.lower() in (".mov", ".mp4") and ns.lower() in (stem + "_proxy", stem + "-proxy", stem + " proxy") \
                        or (sub and ns.lower() == stem and ext.lower() in (".mov", ".mp4")):
                    return os.path.join(folder, n)
        d = os.path.dirname(d)
    return None


def _vision_lines(out):
    frames = []
    for line in out.splitlines():
        try:
            f = json.loads(line)
        except ValueError:
            continue
        if "error" not in f:
            frames.append(f)
    return frames


def look_at(path, a, b, step=VISION_STEP):
    """Tagged frames of path from a to b seconds (source time): [{t, labels, people, ...}], cached
    by file, size and time. A file macOS can't play is looked at through its proxy, else through
    stills ffmpeg pulls out. [] when the helper isn't there or nothing can be read."""
    if not os.path.isfile(VISION_HELPER):
        return []
    try:
        st = os.stat(path)
    except OSError:
        return []
    key = hashlib.sha1(("v2|%s|%d|%d|%.2f|%.2f|%.2f" % (path, st.st_size, int(st.st_mtime), a, b, step)).encode()).hexdigest()
    cache = audio_cache_dir()
    cfile = os.path.join(cache, "vision-%s.json" % key) if cache else None
    if cfile and os.path.isfile(cfile):
        try:
            with open(cfile, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    tries = [path] + [p for p in [proxy_of(path)] if p]
    frames = []
    for p in tries:
        r = subprocess.run([VISION_HELPER, p, "%.3f" % a, "%.3f" % b, "%.3f" % step], capture_output=True, text=True)
        frames = _vision_lines(r.stdout)
        if frames:
            break
    if not frames:                       # stills through ffmpeg, which reads what macOS can't
        with tempfile.TemporaryDirectory(prefix="kickoff-stills-") as tmp:
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", "%.3f" % a, "-i", path, "-t", "%.3f" % max(step, b - a),
                            "-vf", "fps=%g,scale=640:-2" % (1.0 / step), "-q:v", "4", os.path.join(tmp, "%05d.jpg")],
                           capture_output=True)
            for n in sorted(os.listdir(tmp)):
                k = int(os.path.splitext(n)[0]) - 1
                os.rename(os.path.join(tmp, n), os.path.join(tmp, "%.3f.jpg" % (a + k * step)))
            r = subprocess.run([VISION_HELPER, "--images", tmp], capture_output=True, text=True)
            frames = _vision_lines(r.stdout)
    if cfile and frames:
        try:
            with open(cfile, "w", encoding="utf-8") as fh:
                json.dump(frames, fh)
        except OSError:
            pass
    return frames


def slop_base(root, project):
    """(sequence to cut from, its song clipitem). A Kickoff project: its Edit sequence and the song on
    its A1. Anyone's XML (a synced sequence exported from Premiere): the sequence with the most video
    clips under it (through nests) that has a song: its longest audio clip, preferring an audio-only file."""
    seqs = [s for s in root.iter("sequence") if s.find("media") is not None]
    by_id = {s.get("id"): s for s in seqs}
    files = {f.get("id"): f for f in root.iter("file") if f.find("pathurl") is not None}
    edit_name = seq_name("edit", project=project)
    edit = next((s for s in seqs if s.findtext("name") == edit_name), None) or \
        next((s for s in seqs if (s.findtext("name") or "").endswith("_Edit")
              and any(ci.find("sequence") is not None for ci in s.iterfind("media/video/track/clipitem"))), None)
    if edit is not None:
        return edit, next(iter(edit.iterfind("media/audio/track/clipitem")), None)

    def count(seq, depth=0):
        n = 0
        for ci in seq.iterfind("media/video/track/clipitem"):
            if ci.find("sequence") is not None and depth < 4:
                inner = by_id.get(ci.find("sequence").get("id"))
                n += count(inner, depth + 1) if inner is not None else 0
            elif ci.find("file") is not None:
                n += 1
        return n

    def song_of(seq):
        best = None
        for ci in seq.iterfind("media/audio/track/clipitem"):
            f = ci.find("file")
            if f is None or f.get("id") not in files or int(ci.findtext("start") or -1) < 0:
                continue
            audio_only = files[f.get("id")].find("media/video") is None
            key = (audio_only, int(ci.findtext("end")) - int(ci.findtext("start")))
            if best is None or key > best[0]:
                best = (key, ci)
        return best[1] if best else None
    nested = {ci.find("sequence").get("id") for s in seqs for ci in s.iter("clipitem") if ci.find("sequence") is not None}
    ranked = sorted(seqs, key=lambda s: (song_of(s) is not None, count(s), s.get("id") not in nested), reverse=True)
    if not ranked or song_of(ranked[0]) is None or not count(ranked[0]):
        return (ranked[0] if ranked else None), None
    return ranked[0], song_of(ranked[0])


def cam_of(path, fallback):
    """Camera letter from a "C Cam ..." folder in the path (folder names are authoritative)."""
    for part in reversed(os.path.dirname(path or "").split("/")):
        m = re.match(r"^([A-Za-z])[ _-]?Cam\b", part, re.I)
        if m:
            return m.group(1).upper()
    return fallback


def slop_cut(xml_path, seed=0, out_path=None, style=None):
    """Add a Slop Cut sequence next to the sequence it's cut from (replacing an earlier one): its song
    with every synced take laid out, cut at the shot lines, only the take picked for each shot
    enabled. A Kickoff project: '<project>_Slop Cut' next to the Edit sequence, each camera's
    condensed tracks laid out (A Cam's first), written back in place. Anyone's synced sequence XML:
    '<sequence>_Slop Cut', its video tracks (and the tracks inside its nests) laid out, written to
    out_path so the original is never touched. Returns (sequence name, shots, takes, analysis, how
    many takes were looked at)."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    project = root.findtext("project/name") or ""
    parent_of = {c: p for p in root.iter() for c in p}
    seqs = [s for s in root.iter("sequence") if s.find("media") is not None]
    by_id = {s.get("id"): s for s in seqs}
    files = {f.get("id"): f for f in root.iter("file") if f.find("pathurl") is not None}
    edit, song_ci = slop_base(root, project)
    if edit is None:
        raise RuntimeError("there's no sequence in %s" % os.path.basename(xml_path))
    kickoff = (edit.findtext("name") or "").endswith("_Edit") and project != ""
    fps = xml_fps(edit)
    if song_ci is None:
        raise RuntimeError("%s has no song on it, so there's nothing to cut to" % (edit.findtext("name") or "The sequence"))
    song_path = url_to_path(files[song_ci.find("file").get("id")].findtext("pathurl"))
    if not os.path.exists(song_path):
        raise RuntimeError("can't find the song at %s" % song_path)
    song_frame = int(song_ci.findtext("start")) - int(song_ci.findtext("in") or 0)   # where the song's 0:00 is
    to_s = lambda f: (f - song_frame) / fps
    path_of = lambda it: url_to_path(files[it.find("file").get("id")].findtext("pathurl")) \
        if it.find("file") is not None and it.find("file").get("id") in files else None

    # every take: from each camera's nested sequence (its tracks), or a clip right on a track.
    # lanes = [(camera, [clipitems])], each one track of the Slop Cut; a nest's clips are shifted to
    # where the nest sits in the sequence
    lanes, takes = [], []

    def shifted(it, off):
        if not off:
            return it
        c = copy.deepcopy(it)
        for k in ("start", "end"):
            c.find(k).text = str(int(c.findtext(k)) + off)
        return c
    for vt, tr in enumerate(edit.findall("media/video/track"), 1):
        direct = []
        for ci in tr.findall("clipitem"):
            if int(ci.findtext("start") or -1) < 0 or (ci.findtext("enabled") or "TRUE") == "FALSE":
                continue
            if ci.find("sequence") is not None:
                nest = by_id.get(ci.find("sequence").get("id"))
                if nest is None:
                    continue
                off = int(ci.findtext("start")) - int(ci.findtext("in") or 0)
                cam = re.sub(r"_.*$", "", ci.findtext("name") or "") if kickoff else None
                for ntr in nest.findall("media/video/track"):
                    items = [shifted(it, off) for it in ntr.findall("clipitem")
                             if it.find("file") is not None and int(it.findtext("start") or -1) >= 0]
                    if items:
                        lanes.append((cam, items))
            elif ci.find("file") is not None:
                direct.append(ci)
        if direct:
            lanes.append((None, direct))
    for k, (cam, items) in enumerate(lanes):
        cam = cam or cam_of(path_of(items[0]), "V%d" % (k + 1))
        lanes[k] = (cam, items)
        for it in items:
            a, b = to_s(int(it.findtext("start"))), to_s(int(it.findtext("end")))
            if b > 0:
                takes.append(dict(cam=cam, item=it, a=a, b=b, spans=[(a, b)]))
    if not takes:
        raise RuntimeError("%s has no synced takes to cut" % (edit.findtext("name") or "The sequence"))
    event("stage", text="Listening to the song")
    style = style or slop_style()
    an = song_analysis(song_path, style["bpm"])
    takes = [t for t in takes if t["a"] < an["dur"]]

    # what each take shows, a frame a second over the part that plays during the song
    looked = 0
    if os.path.isfile(VISION_HELPER):
        for n, t in enumerate(takes, 1):
            event("stage", text="Looking at take %d of %d" % (n, len(takes)))
            it = t["item"]
            s0, s1 = int(it.findtext("start")), int(it.findtext("end"))
            i0, i1 = int(it.findtext("in")), int(it.findtext("out"))
            k = (i1 - i0) / max(1, s1 - s0)                        # source frames per timeline frame
            src = lambda song_t: (i0 + (song_t * fps + song_frame - s0) * k) / fps
            a, b = max(0.0, t["a"]), min(an["dur"], t["b"])
            path = path_of(it)
            frames = look_at(path, src(a), src(b)) if path else []
            if frames:
                looked += 1
            t["frames"] = [((f["t"] * fps - i0) / k + s0 - song_frame) / fps for f in frames]
            t["roles"] = [frame_roles(f) for f in frames]
    for t in takes:
        def tags(a, b, t=t):
            rs = [r for x, r in zip(t.get("frames", []), t.get("roles", [])) if a - 0.5 <= x <= b + 0.5]
            return {k: float(np.mean([r[k] for r in rs])) for k in rs[0]} if rs else None
        t["tags"] = tags
    event("stage", text="Cutting the Slop Cut")
    shots = plan_takes(an, takes, seed, style)

    # the new sequence: the Edit sequence's settings and song, its camera nests swapped for the takes
    base = project if kickoff else (edit.findtext("name") or project)
    name = "%s_%s" % (base, SLOP_NAME) if base else SLOP_NAME
    holder = parent_of[edit]
    for old in [s for s in holder.findall("sequence") if s.findtext("name") == name]:
        holder.remove(old)
    new = copy.deepcopy(edit)
    new.set("id", "sequence-slop")
    new.find("name").text = name
    if new.find("uuid") is not None:
        new.find("uuid").text = "kickoff-slop-%s" % datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    video = new.find("media/video")
    for tr in video.findall("track"):
        video.remove(tr)
    n = 0
    for ci in new.iter("clipitem"):
        n += 1
        ci.set("id", "clipitem-slop-%d" % n)
        for ln in ci.findall("link"):        # links point at clips of the sequence it was copied from
            ci.remove(ln)
    by_ref(new)
    lines = sorted({song_frame + int(round(a * fps)) for a, _, _ in shots} |
                   {song_frame + int(round(shots[-1][1] * fps))})
    on = {}                                   # take index -> [(start, end) frames it's enabled]
    for a, b, i in shots:
        if i is not None:
            on.setdefault(i, []).append((song_frame + int(round(a * fps)), song_frame + int(round(b * fps))))
    index = {id(t["item"]): i for i, t in enumerate(takes)}
    last = 0
    for cam, items in lanes:
        tr = ET.SubElement(video, "track")
        for it in items:
            s0, s1 = int(it.findtext("start")), int(it.findtext("end"))
            i0, i1 = int(it.findtext("in")), int(it.findtext("out"))
            k = (i1 - i0) / max(1, s1 - s0)
            i = index.get(id(it))
            cuts = [s0] + [x for x in lines if s0 < x < s1] + [s1]
            for a, b in zip(cuts[:-1], cuts[1:]):
                seg = copy.deepcopy(it)
                for ln in seg.findall("link"):
                    seg.remove(ln)
                n += 1
                seg.set("id", "clipitem-slop-%d" % n)
                live = i is not None and any(x <= a and b <= y for x, y in on.get(i, []))
                seg.find("enabled").text = "TRUE" if live else "FALSE"
                seg.find("start").text, seg.find("end").text = str(a), str(b)
                seg.find("in").text = str(i0 + int(round((a - s0) * k)))
                seg.find("out").text = str(i0 + int(round((b - s0) * k)))
                by_ref(seg)
                tr.append(seg)
            last = max(last, s1)
        ET.SubElement(tr, "enabled").text = "TRUE"
        ET.SubElement(tr, "locked").text = "FALSE"
    new.find("duration").text = str(max(int(new.findtext("duration")), last))
    holder.insert(list(holder).index(edit) + 1, new)
    write_xml(root, out_path or xml_path)
    return name, shots, takes, an, looked


def by_ref(el):
    """Files and nested sequences inside a copied clip become references (id only): the full ones
    are already written earlier in the XML, where the clips were copied from."""
    for tag in ("file", "sequence"):
        for x in el.iter(tag):
            if x is not el and len(x):
                for k in list(x):
                    x.remove(k)


# ---------------------------------------------------------------- Premiere project files

PR_TICKS = 254016000000                          # Premiere's time unit: ticks per second


def prproj_to_xml(src, out):
    """Read a Premiere project (.prproj, gzipped XML) and write it as an FCP7 XML: its bins, the
    media in them and every sequence's clips (cuts, nests and on/off), which Resolve and Premiere
    both open. Effects, titles, transitions, speed changes and multicam aren't carried; the
    returned notes say what was left out."""
    import gzip
    with open(src, "rb") as fh:
        data = fh.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    try:
        r = ET.fromstring(data)
    except ET.ParseError:
        raise RuntimeError("Kickoff couldn't read that Premiere project. In Premiere, use File > Export > "
                           "Final Cut Pro XML and convert that instead")
    objs = {}
    for e in r:
        k = e.get("ObjectID") or e.get("ObjectUID")
        if k:
            objs[k] = e
    notes = collections.Counter()

    def ref(e, tag=None):
        """The object an element points to (ObjectRef / ObjectURef), or None."""
        if e is None:
            return None
        k = e.get("ObjectRef") or e.get("ObjectURef")
        return objs.get(k) if k else None

    def num(e, path, default=0):
        t = e.findtext(path) if e is not None else None
        try:
            return int(t)
        except (TypeError, ValueError):
            return default

    def rate_el(parent, fps):
        rt = ET.SubElement(parent, "rate")
        ET.SubElement(rt, "timebase").text = str(int(round(fps)))
        ET.SubElement(rt, "ntsc").text = "TRUE" if abs(fps - round(fps)) > 0.001 else "FALSE"

    def clip_of(subclip):
        """The VideoClip/AudioClip a SubClip uses: (clip element, its inner Clip block)."""
        c = ref(subclip.find("Clip")) if subclip is not None else None
        return (c, c.find("Clip")) if c is not None and c.find("Clip") is not None else (None, None)

    def source_of(inner):
        """('media', Media element) or ('sequence', Sequence element) for a clip's Source."""
        s = ref(inner.find("Source"))
        if s is None:
            return None, None
        ms = s.find("MediaSource")
        if ms is not None:
            return "media", ref(ms.find("Media"))
        ss = s.find("SequenceSource")
        if ss is not None:
            return "sequence", ref(ss.find("Sequence"))
        return None, None

    files = {}                                   # media element -> <file> (first full, then by id)
    seq_ids = {}                                 # sequence element -> id

    def media_info(m):
        path = os.path.normpath(m.findtext("ActualMediaFilePath") or m.findtext("FilePath") or "")
        vs, aus = ref(m.find("VideoStream")), [ref(a) for a in m.findall("AudioStream")]
        fticks = num(vs, "FrameRate") if vs is not None else 0
        fps = PR_TICKS / fticks if fticks else 0.0
        nch = 0
        for a in aus:
            if a is not None:
                try:   # a stereo stream lists two channels in its layout
                    nch += max(1, len(json.loads(a.findtext("AudioChannelLayout") or "[]")))
                except ValueError:
                    nch += 1
        return path, fps, vs is not None, nch

    def file_el(parent, m, seq_fps):
        key = id(m)
        if key in files:
            ET.SubElement(parent, "file", id=files[key])
            return
        fid = "file-%d" % (len(files) + 1)
        files[key] = fid
        path, fps, has_v, n_a = media_info(m)
        fps = fps or seq_fps
        f = ET.SubElement(parent, "file", id=fid)
        ET.SubElement(f, "name").text = os.path.basename(path)
        ET.SubElement(f, "pathurl").text = path_to_url(path, [])
        rate_el(f, fps)
        tc = ET.SubElement(f, "timecode")
        rate_el(tc, fps)
        alt = (m.findtext("UseAlternateStart") or "").strip() == "true" and m.find("AlternateStart") is not None
        start = num(m, "AlternateStart" if alt else "Start") * fps / PR_TICKS   # alternate = timecode set in Premiere
        ET.SubElement(tc, "frame").text = str(int(round(start)))
        ET.SubElement(tc, "displayformat").text = "NDF"
        md = ET.SubElement(f, "media")
        if has_v:
            v = ET.SubElement(md, "video")
            sc = ET.SubElement(v, "samplecharacteristics")
            rate_el(sc, fps)
        if n_a or not has_v:
            a = ET.SubElement(md, "audio")
            ET.SubElement(a, "channelcount").text = str(max(1, n_a))

    def seq_id(s):
        if id(s) not in seq_ids:
            seq_ids[id(s)] = "sequence-%d" % (len(seq_ids) + 1)
        return seq_ids[id(s)]

    def build_sequence(s):
        groups = {}
        for tg in s.iter("TrackGroup"):
            g = ref(tg.find("Second"))
            if g is not None:
                groups[g.tag] = g
        vg = groups.get("VideoTrackGroup")
        fticks = num(vg.find("TrackGroup") if vg is not None else None, "FrameRate") or 10594584000
        fps = PR_TICKS / fticks
        rect = (vg.findtext("FrameRect") if vg is not None else "") or "0,0,3840,2160"
        w, h = [int(x) for x in rect.split(",")[2:4]]
        el = ET.Element("sequence", id=seq_id(s))
        ET.SubElement(el, "name").text = s.findtext("Name") or "Sequence"
        rate_el(el, fps)
        media = ET.SubElement(el, "media")
        end_all = 0
        tick = 0
        for kind, gtag in (("video", "VideoTrackGroup"), ("audio", "AudioTrackGroup")):
            g = groups.get(gtag)
            km = ET.SubElement(media, kind)
            if kind == "video":
                fm = ET.SubElement(ET.SubElement(km, "format"), "samplecharacteristics")
                rate_el(fm, fps)
                ET.SubElement(fm, "width").text = str(w)
                ET.SubElement(fm, "height").text = str(h)
            tracks = g.find("TrackGroup/Tracks") if g is not None else None
            for t in (tracks if tracks is not None else []):
                tr = ref(t)
                if tr is None:
                    continue
                ct = tr.find("ClipTrack")
                te = ET.SubElement(km, "track")
                if ct is not None and (ct.findtext("Track/IsMuted") == "true" or ct.findtext("Track/Muted") == "true"):
                    ET.SubElement(te, "enabled").text = "FALSE"
                items = ct.find("ClipItems/TrackItems") if ct is not None else None
                if ct is not None and ct.find("TransitionItems/TrackItems") is not None and \
                        len(ct.find("TransitionItems/TrackItems")):
                    notes["transitions"] += len(ct.find("TransitionItems/TrackItems"))
                for it in (items if items is not None else []):
                    ti = ref(it)
                    cti = ti.find("ClipTrackItem") if ti is not None else None
                    if cti is None:
                        notes["items it couldn't read"] += 1
                        continue
                    st = num(cti, "TrackItem/Start")
                    en = num(cti, "TrackItem/End")
                    clip, inner = clip_of(ref(cti.find("SubClip")))
                    if inner is None:
                        notes["titles or graphics"] += 1
                        continue
                    sk, src_el = source_of(inner)
                    if src_el is None:
                        notes["titles or graphics"] += 1
                        continue
                    speed = inner.findtext("PlaybackSpeed")
                    if speed and abs(float(speed) - 1) > 1e-6:
                        notes["speed changes"] += 1
                    tick += 1
                    s0, s1 = int(round(st / fticks)), int(round(en / fticks))
                    i0 = int(round(num(inner, "InPoint") / fticks))
                    i1 = i0 + (s1 - s0)
                    ci = ET.SubElement(te, "clipitem", id="clipitem-%d" % tick)
                    sub = ref(cti.find("SubClip"))
                    nm = (sub.findtext("Name") if sub is not None else None) or "Clip"
                    ET.SubElement(ci, "name").text = nm
                    off = ti.findtext("ClipTrackItem/TrackItem/Disabled") == "true" or \
                        cti.findtext("TrackItem/IsDisabled") == "true" or (sub is not None and sub.findtext("Disabled") == "true")
                    ET.SubElement(ci, "enabled").text = "FALSE" if off else "TRUE"
                    rate_el(ci, fps)
                    ET.SubElement(ci, "start").text = str(s0)
                    ET.SubElement(ci, "end").text = str(s1)
                    ET.SubElement(ci, "in").text = str(i0)
                    ET.SubElement(ci, "out").text = str(i1)
                    if sk == "media":
                        file_el(ci, src_el, fps)
                        if kind == "audio":
                            stt = ET.SubElement(ci, "sourcetrack")
                            ET.SubElement(stt, "mediatype").text = "audio"
                            ET.SubElement(stt, "trackindex").text = "1"
                    else:
                        ET.SubElement(ci, "sequence", id=seq_id(src_el))
                    end_all = max(end_all, s1)
        ET.SubElement(el, "duration").text = str(end_all)
        tcel = ET.SubElement(el, "timecode")
        rate_el(tcel, fps)
        z = num(s, "Node/Properties/MZ.ZeroPoint")
        ET.SubElement(tcel, "frame").text = str(int(round(z / fticks)))
        ET.SubElement(tcel, "displayformat").text = "NDF"
        return el

    root_item = next((e for e in r if e.tag == "RootProjectItem"), None)
    if root_item is None:
        raise RuntimeError("that Premiere project has no bins in it")
    xm = ET.Element("xmeml", version="4")
    proj = ET.SubElement(xm, "project")
    ET.SubElement(proj, "name").text = os.path.splitext(os.path.basename(src))[0]
    built = []

    def walk(item, parent):
        cont = item.find("ProjectItemContainer/Items")
        for k in (cont if cont is not None else []):
            obj = ref(k)
            if obj is None:
                continue
            name = obj.findtext("ProjectItem/Name") or "Item"
            if obj.tag == "BinProjectItem":
                b = ET.SubElement(parent, "bin")
                ET.SubElement(b, "name").text = name
                walk(obj, ET.SubElement(b, "children"))
            elif obj.tag == "ClipProjectItem":
                mc = ref(obj.find("MasterClip"))
                first = ref(mc.find("Clips/Clip")) if mc is not None else None
                inner = first.find("Clip") if first is not None else None
                sk, src_el = source_of(inner) if inner is not None else (None, None)
                if sk == "sequence":
                    built.append((parent, src_el))
                elif sk == "media":
                    path, fps, has_v, _ = media_info(src_el)
                    c = ET.SubElement(parent, "clip", id="masterclip-%d" % (len(files) + 1))
                    ET.SubElement(c, "name").text = name
                    rate_el(c, fps or 23.976)
                    ci = ET.SubElement(ET.SubElement(ET.SubElement(c, "media"), "video" if has_v else "audio"),
                                       "track")
                    ci = ET.SubElement(ci, "clipitem", id="masterclip-item-%d" % (len(files) + 1))
                    ET.SubElement(ci, "name").text = name
                    file_el(ci, src_el, fps or 23.976)
    walk(root_item, ET.SubElement(proj, "children"))
    for parent, s in built:
        parent.append(build_sequence(s))
    if not built:
        raise RuntimeError("that Premiere project has no sequences in it")
    write_xml(premiere_polish(xm), out)
    return [("Left out %d %s" % (n, k)) for k, n in notes.items()]


def fcp_time(v):
    """An FCPXML time ("1001/24000s", "5s", "0s") in seconds."""
    if not v:
        return 0.0
    v = v.rstrip("s")
    if "/" in v:
        a, b = v.split("/", 1)
        return float(a) / float(b)
    return float(v)


def fcpxml_to_xml(src, out):
    """Read an FCPXML (Final Cut Pro 10 / Resolve) and write its projects as an FCP7 XML that Premiere
    and Resolve open: every clip flattened onto video and audio tracks at its spot, compound clips
    opened up, the active angle of multicam clips. Titles, generators, effects and speed changes stay
    behind; the returned notes say what was left out."""
    r = ET.parse(src).getroot()
    res = r.find("resources")
    formats = {f.get("id"): f for f in (res.findall("format") if res is not None else [])}
    assets = {a.get("id"): a for a in (res.findall("asset") if res is not None else [])}
    medias = {m.get("id"): m for m in (res.findall("media") if res is not None else [])}
    notes = collections.Counter()

    def fps_of(fmt_id, default=23.976):
        f = formats.get(fmt_id)
        fd = fcp_time(f.get("frameDuration")) if f is not None and f.get("frameDuration") else 0
        if not fd:
            return default
        fps = 1.0 / fd
        for std in (24, 30, 60, 120, 48):
            if abs(fps - std * 1000 / 1001) < 0.01:
                return std * 1000 / 1001   # exact NTSC rate, or long source timecodes land a frame early
        return round(fps, 3)

    def asset_path(a):
        mr = a.find("media-rep")
        u = (mr.get("src") if mr is not None else None) or a.get("src") or ""
        return os.path.normpath(url_to_path(u.replace("file://localhost", "file://"))) if u else ""

    def channels(e, a):
        """The file channels a clip plays ("1, 2" = both of a stereo pair), each its own mono clip in
        Premiere, the way it brings in camera audio (left on one track, right on the next)."""
        spec = e.get("srcCh") or next((x.get("srcCh") for x in e.findall("audio-channel-source")
                                       if x.get("srcCh") and x.get("enabled") != "0"), None)
        try:
            chs = [int(c) for c in (spec or "").replace(",", " ").split()]
        except ValueError:
            chs = []
        return chs or list(range(1, max(1, int(a.get("audioChannels") or 2)) + 1))

    def flatten(children, tmap, window, lane0, items, kinds=("video", "audio"), muted=False, off_=False):
        """children: elements whose offset/start are in a local time; tmap(t) gives the sequence time
        of local time t; window = the visible (start, end) in sequence time."""
        for e in children:
            tag = e.tag
            if tag in ("metadata", "adjust-volume", "adjust-transform", "adjust-crop", "filter-video",
                       "filter-audio", "marker", "chapter-marker", "keyword", "rating", "note", "conform-rate",
                       "timeMap", "mute", "audio-channel-source", "audio-role-source", "bookmark"):
                continue
            lane = e.get("lane")
            # inside a connected clip, everything stays on that clip's lane
            ln = lane0 if lane is None or lane0 != 0 else int(lane)
            off, st, du = fcp_time(e.get("offset")), fcp_time(e.get("start")), fcp_time(e.get("duration"))
            t0 = tmap(off)
            w = (max(window[0], t0), min(window[1], t0 + du)) if du else window
            if w[1] - w[0] <= 1e-9 and tag != "spine":
                continue
            emap = (lambda o, s: (lambda t: tmap(o + (t - s))))(off, st)
            eoff = off_ or e.get("enabled") == "0"         # switched off: kept, but off
            if e.find("timeMap") is not None:
                notes["speed changes (they play at normal speed)"] += 1
            vol = e.find("adjust-volume")
            emuted = muted or (vol is not None and (vol.get("amount") or "").startswith("-96"))
            if tag in ("asset-clip", "video", "audio"):
                a = assets.get(e.get("ref"))
                if a is not None:
                    se = e.get("srcEnable") or "all"
                    has_v = a.get("hasVideo") == "1" and tag != "audio" and se in ("all", "video")
                    has_a = a.get("hasAudio") == "1" and tag != "video" and se in ("all", "audio")
                    for kind, ok in (("video", has_v), ("audio", has_a)):
                        if ok and kind in kinds:
                            src_in = st + (w[0] - t0) - fcp_time(a.get("start"))
                            items.append({"kind": kind, "start": w[0], "end": w[1], "in": src_in, "asset": a,
                                          "lane": ln, "on": not eoff and not (kind == "audio" and emuted),
                                          "name": e.get("name") or a.get("name"), "chs": channels(e, a)})
                # connected clips ride on this one, in its own local time
                flatten(list(e), emap, window, ln, items, kinds, muted, off_)
            elif tag in ("clip", "sync-clip", "gap", "spine", "ref-clip"):
                inner = list(e)
                if tag == "ref-clip":
                    m = medias.get(e.get("ref"))
                    sq = m.find("sequence") if m is not None else None
                    inner = (list(sq.find("spine")) if sq is not None and sq.find("spine") is not None else []) + \
                        [c for c in e if c.get("lane")]
                if tag == "spine":            # a storyline: its own items run in the parent's time
                    flatten(inner, emap if e.get("offset") else tmap, w, ln, items, kinds, emuted, eoff)
                else:
                    flatten(inner, emap, w, ln, items, kinds, emuted, eoff)
            elif tag == "mc-clip":
                m = medias.get(e.get("ref"))
                mc = m.find("multicam") if m is not None else None
                for src_el in e.findall("mc-source"):
                    se = src_el.get("srcEnable") or "all"
                    ang = next((x for x in (mc.findall("mc-angle") if mc is not None else [])
                                if x.get("angleID") == src_el.get("angleID")), None)
                    if ang is not None:
                        ks = tuple(k for k in kinds if se == "all" or se == k)
                        flatten(list(ang), emap, w, ln, items, ks, emuted, eoff)
                flatten([c for c in e if c.get("lane")], emap, window, ln, items, kinds, muted, off_)
            elif tag in ("title", "generator"):
                notes["titles and generators"] += 1
            elif tag == "transition":
                notes["transitions"] += 1
            else:
                flatten(list(e), emap, w, ln, items, kinds, emuted, eoff)

    xm = ET.Element("xmeml", version="4")
    proj = ET.SubElement(xm, "project")
    ET.SubElement(proj, "name").text = os.path.splitext(os.path.basename(src))[0]
    kids = ET.SubElement(proj, "children")
    file_ids, nseq = {}, 0

    def rate_el(parent, fps):
        rt = ET.SubElement(parent, "rate")
        ET.SubElement(rt, "timebase").text = str(int(round(fps)))
        ET.SubElement(rt, "ntsc").text = "TRUE" if abs(fps - round(fps)) > 0.001 else "FALSE"

    def place(tr, it, s0, s1, ch, n, kind, fps):
        """Write one clip on a track (an audio clip carries one file channel, ch); returns the count."""
        tr[1].append((s0, s1))
        n += 1
        a = it["asset"]
        p = asset_path(a)
        afps = fps_of(a.get("format"), fps) if a.get("hasVideo") == "1" else fps
        ci = ET.SubElement(tr[2], "clipitem", id="clipitem-%d" % n)
        if kind == "audio":
            ci.set("premiereChannelType", "mono")
        ET.SubElement(ci, "name").text = os.path.basename(p) or it["name"]
        ET.SubElement(ci, "enabled").text = "TRUE" if it["on"] else "FALSE"
        rate_el(ci, fps)
        i0 = int(round(it["in"] * fps))
        for k, v in (("start", s0), ("end", s1), ("in", i0), ("out", i0 + s1 - s0)):
            ET.SubElement(ci, k).text = str(v)
        if p in file_ids:
            ET.SubElement(ci, "file", id=file_ids[p])
        else:
            fid = file_ids[p] = "file-%d" % (len(file_ids) + 1)
            f = ET.SubElement(ci, "file", id=fid)
            ET.SubElement(f, "name").text = os.path.basename(p)
            ET.SubElement(f, "pathurl").text = path_to_url(p, [])
            rate_el(f, afps)
            ET.SubElement(f, "duration").text = str(int(round(fcp_time(a.get("duration")) * afps)))
            ft = ET.SubElement(f, "timecode")
            rate_el(ft, afps)
            ET.SubElement(ft, "frame").text = str(int(round(fcp_time(a.get("start")) * afps)))
            ET.SubElement(ft, "displayformat").text = "NDF"
            fmd = ET.SubElement(f, "media")
            if a.get("hasVideo") == "1":
                ET.SubElement(ET.SubElement(fmd, "video"), "samplecharacteristics")
            if a.get("hasAudio") == "1":
                ET.SubElement(ET.SubElement(fmd, "audio"), "channelcount").text = a.get("audioChannels") or "2"
        if kind == "audio":
            stt = ET.SubElement(ci, "sourcetrack")
            ET.SubElement(stt, "mediatype").text = "audio"
            ET.SubElement(stt, "trackindex").text = str(ch or 1)
        return n

    for pr in r.iter("project"):
        sq = pr.find("sequence")
        if sq is None or sq.find("spine") is None:
            continue
        nseq += 1
        fps = fps_of(sq.get("format"))
        fmt = formats.get(sq.get("format"))
        w, h = (int(fmt.get("width") or 3840), int(fmt.get("height") or 2160)) if fmt is not None else (3840, 2160)
        tc0 = fcp_time(sq.get("tcStart"))
        items = []
        dur = fcp_time(sq.get("duration"))
        flatten(list(sq.find("spine")), lambda t: t, (0.0, dur or 1e9), 0, items)
        # tracks: lanes keep their order (primary storyline on V1, higher lanes above; audio by lane,
        # nearest the picture first), a clip going down a track when it would overlap
        el = ET.SubElement(kids, "sequence", id="sequence-%d" % nseq)
        ET.SubElement(el, "name").text = pr.get("name") or "Sequence %d" % nseq
        ET.SubElement(el, "duration").text = str(int(round(dur * fps)))
        rate_el(el, fps)
        tce = ET.SubElement(el, "timecode")
        rate_el(tce, fps)
        ET.SubElement(tce, "frame").text = str(int(round(tc0 * fps)))
        ET.SubElement(tce, "displayformat").text = "NDF"
        media = ET.SubElement(el, "media")
        n = 0
        for kind in ("video", "audio"):
            km = ET.SubElement(media, kind)
            if kind == "video":
                fm = ET.SubElement(ET.SubElement(km, "format"), "samplecharacteristics")
                rate_el(fm, fps)
                ET.SubElement(fm, "width").text = str(w)
                ET.SubElement(fm, "height").text = str(h)
            mine = [i for i in items if i["kind"] == kind]
            order = sorted(set(i["lane"] for i in mine))          # video: lower lanes lower down
            if kind == "audio":
                order = sorted(set(i["lane"] for i in mine), key=lambda l: (0 if l >= 0 else 1, abs(l)))
            tracks = []                          # [(lane, [(s, e)], element)]
            for lane in order:
                for it in sorted((i for i in mine if i["lane"] == lane), key=lambda i: i["start"]):
                    s0, s1 = int(round(it["start"] * fps)), int(round(it["end"] * fps))
                    if s1 <= s0:
                        continue
                    chs = it["chs"] if kind == "audio" else [None]
                    free = lambda t: t[0] == lane and all(s1 <= a or s0 >= b for a, b in t[1])
                    base = next((k for k, t in enumerate(tracks) if t[0] == lane), len(tracks))
                    j = next((j for j in range(base, len(tracks) - len(chs) + 1, len(chs))
                              if all(free(t) for t in tracks[j:j + len(chs)])), None)
                    if j is None:                # a channel per track, side by side (L above R)
                        j = len(tracks)
                        tracks += [(lane, [], ET.SubElement(km, "track")) for _ in chs]
                    for ch, tr in zip(chs, tracks[j:j + len(chs)]):
                        n = place(tr, it, s0, s1, ch, n, kind, fps)
    if not nseq:
        raise RuntimeError("that FCPXML has no project (timeline) in it")
    write_xml(premiere_polish(xm), out)
    return [("Left out %d %s" % (v, k)) for k, v in notes.items()]


def xml_to_fcpxml(xml, out):
    """Write a Premiere / FCP7 XML's sequences as one FCPXML (1.10) for Final Cut Pro or Resolve: each
    sequence a project, every clip a connected clip on its own lane (video tracks above, audio below)
    so the cut stays exactly as it was; nested sequences become compound clips."""
    root = resolve_copy(ET.parse(xml).getroot())
    if root.find("project") is None:                 # a sequence exported on its own
        pr = ET.Element("project")
        ET.SubElement(pr, "name").text = os.path.splitext(os.path.basename(xml))[0]
        ch = ET.SubElement(pr, "children")
        for sq in root.findall("sequence"):
            root.remove(sq)
            ch.append(sq)
        root.append(pr)
    files = {}
    for f in root.iter("file"):
        if len(f) and f.get("id") not in files:
            files[f.get("id")] = f
    seqs = [s for s in root.iter("sequence") if len(s) and s.find("media") is not None]
    by_id = {s.get("id"): s for s in seqs if s.get("id")}

    def frac(frames, fps):
        """frames at fps as an FCPXML time."""
        num, den = (1001, int(round(fps)) * 1000) if abs(fps - round(fps)) > 0.001 else (1, int(round(fps)))
        return "0s" if not frames else "%d/%ds" % (int(round(frames)) * num, den)

    fx = ET.Element("fcpxml", version="1.10")
    res = ET.SubElement(fx, "resources")
    fmt_ids, asset_ids, media_ids = {}, {}, {}
    nid = [0]

    def new_id():
        nid[0] += 1
        return "r%d" % nid[0]

    def fmt(fps, w, h):
        k = (fps, w, h)
        if k not in fmt_ids:
            fmt_ids[k] = new_id()
            num, den = (1001, int(round(fps)) * 1000) if abs(fps - round(fps)) > 0.001 else (1, int(round(fps)))
            ET.SubElement(res, "format", id=fmt_ids[k], frameDuration="%d/%ds" % (num, den), width=str(w), height=str(h))
        return fmt_ids[k]

    def asset(fid, sfps):
        d = files.get(fid)
        if d is None or not d.findtext("pathurl"):
            return None
        if fid in asset_ids:
            return asset_ids[fid]
        p = os.path.normpath(url_to_path(d.findtext("pathurl")))
        afps = xml_fps(d) if d.find("rate") is not None else sfps
        has_v = d.find("media/video") is not None
        has_a = d.find("media/audio") is not None or not has_v
        tcf = int(d.findtext("timecode/frame") or 0)
        dur = int(d.findtext("duration") or 0)
        aid = asset_ids[fid] = new_id()
        a = ET.Element("asset", id=aid, name=os.path.splitext(os.path.basename(p))[0], start=frac(tcf, afps),
                       duration=frac(dur, afps) if dur else "0s", hasVideo="1" if has_v else "0",
                       hasAudio="1" if has_a else "0")
        if has_v:
            a.set("format", fmt(afps, 3840, 2160))
        if has_a:
            a.set("audioSources", "1")
            # Premiere writes a stereo file as two one-channel blocks: count them all
            a.set("audioChannels", str(sum(int(x.findtext("channelcount") or 1) for x in d.findall("media/audio"))
                                       or 2))
        ET.SubElement(a, "media-rep", kind="original-media", src=path_to_url(p, []).replace("file://localhost", "file://"))
        res.append(a)
        return aid

    def spine_of(s, parent):
        """The sequence's clips as connected clips on a gap the length of the sequence."""
        sfps = xml_fps(s) if s.find("rate") is not None else 23.976
        dur = int(s.findtext("duration") or 0)
        ends = [int(c.findtext("end") or 0) for c in s.iter("clipitem")]
        dur = max([dur] + ends)
        sp = ET.SubElement(parent, "spine")
        gap = ET.SubElement(sp, "gap", name="Gap", offset="0s", start="0s", duration=frac(dur, sfps))
        for kind, sign in (("video", 1), ("audio", -1)):
            for ti, t in enumerate(s.findall("media/%s/track" % kind), 1):
                t_on = (t.findtext("enabled") or "TRUE") != "FALSE"
                for ci in t.findall("clipitem"):
                    st, en = int(ci.findtext("start") or -1), int(ci.findtext("end") or -1)
                    i0 = int(ci.findtext("in") or 0)
                    if st < 0 or en <= st:
                        continue
                    on = t_on and (ci.findtext("enabled") or "TRUE") != "FALSE"
                    nest = ci.find("sequence")
                    attrs = {"lane": str(sign * ti), "offset": frac(st, sfps), "duration": frac(en - st, sfps)}
                    if nest is not None:
                        ns = by_id.get(nest.get("id")) or (nest if len(nest) else None)
                        if ns is None:
                            continue
                        mid = media_for(ns)
                        el = ET.SubElement(gap, "ref-clip", ref=mid, name=ns.findtext("name") or "Nest",
                                           start=frac(i0, sfps), **attrs)
                        if kind == "audio":
                            el.set("srcEnable", "audio")
                        elif ns.find("media/audio/track") is not None:
                            el.set("srcEnable", "video")
                    else:
                        f = ci.find("file")
                        aid = asset(f.get("id"), sfps) if f is not None else None
                        if aid is None:
                            continue
                        d = files[f.get("id")]
                        tcf = int(d.findtext("timecode/frame") or 0)
                        afps = xml_fps(d) if d.find("rate") is not None else sfps
                        el = ET.SubElement(gap, "asset-clip", ref=aid, name=ci.findtext("name") or "",
                                           start=frac(tcf * sfps / afps + i0, sfps), **attrs)
                        el.set("srcEnable", kind)
                        blocks = d.findall("media/audio")
                        ti_ = int(ci.findtext("sourcetrack/trackindex") or 0)
                        if kind == "audio" and len(blocks) > 1 and 0 < ti_ <= len(blocks):
                            # one channel of a file Premiere splits per channel (left and right on own tracks)
                            first = sum(int(x.findtext("channelcount") or 1) for x in blocks[:ti_ - 1])
                            n_ = int(blocks[ti_ - 1].findtext("channelcount") or 1)
                            ET.SubElement(el, "audio-channel-source",
                                          srcCh=", ".join(str(first + k + 1) for k in range(n_)))
                    if not on:
                        el.set("enabled", "0")
        return sfps, dur

    def media_for(ns):
        k = ns.get("id") or id(ns)
        if k in media_ids:
            return media_ids[k]
        mid = media_ids[k] = new_id()
        m = ET.Element("media", id=mid, name=ns.findtext("name") or "Nest")
        sfps = xml_fps(ns) if ns.find("rate") is not None else 23.976
        w = int(ns.findtext("media/video/format/samplecharacteristics/width") or 3840)
        h = int(ns.findtext("media/video/format/samplecharacteristics/height") or 2160)
        sq = ET.SubElement(m, "sequence", format=fmt(sfps, w, h))
        _, dur = spine_of(ns, sq)
        sq.set("duration", frac(dur, sfps))
        sq.set("tcStart", frac(int(ns.findtext("timecode/frame") or 0), sfps))
        res.append(m)
        return mid

    lib = ET.SubElement(fx, "library")
    ev = ET.SubElement(lib, "event", name=os.path.splitext(os.path.basename(xml))[0])
    for s in seqs:
        sfps = xml_fps(s) if s.find("rate") is not None else 23.976
        w = int(s.findtext("media/video/format/samplecharacteristics/width") or 3840)
        h = int(s.findtext("media/video/format/samplecharacteristics/height") or 2160)
        pj = ET.SubElement(ev, "project", name=s.findtext("name") or "Sequence")
        sq = ET.SubElement(pj, "sequence", format=fmt(sfps, w, h))
        _, dur = spine_of(s, sq)
        sq.set("duration", frac(dur, sfps))
        sq.set("tcStart", frac(int(s.findtext("timecode/frame") or 0), sfps))
        sq.set("tcFormat", "NDF")
    # resources must come before they're used: formats, then assets, then compound clips (in order made)
    order = {"format": 0, "asset": 1, "media": 2}
    kids = sorted(list(res), key=lambda e: order.get(e.tag, 3))
    for k in list(res):
        res.remove(k)
    for k in kids:
        res.append(k)
    ET.indent(fx, space="    ")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n')
        fh.write(ET.tostring(fx, encoding="unicode"))
        fh.write("\n")
    speed = sum(1 for ci in root.iter("clipitem") for x in ci.iter("parameter")
                if x.findtext("parameterid") == "speed" and abs(float(x.findtext("value") or 100) - 100) > 0.01)
    return ["%d clips with a speed change play at normal speed" % speed] if speed else []


_FACTS = {}


def media_facts(path):
    """What ffprobe says about a media file: width, height, audio channels, sample rate (None if unreadable)."""
    if path in _FACTS:
        return _FACTS[path]
    facts = None
    if path and os.path.exists(path):
        try:
            out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
                                 capture_output=True, text=True, timeout=30).stdout
            st = json.loads(out or "{}").get("streams", [])
            v = next((s for s in st if s.get("codec_type") == "video" and s.get("width")), None)
            a = [s for s in st if s.get("codec_type") == "audio"]
            facts = {"w": int(v["width"]) if v else 0, "h": int(v["height"]) if v else 0,
                     "ch": sum(int(s.get("channels") or 1) for s in a), "sr": int(a[0].get("sample_rate") or 48000) if a else 48000}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            facts = None
    _FACTS[path] = facts
    return facts


def premiere_polish(xm):
    """Fill an XML written from FCPXML or a .prproj out to the shape Premiere's own export has, which
    Premiere needs to import it (a bare one imports nothing, without a word): sequence uuid and audio
    outputs, timecode strings, clip durations and every file's picture size and sound channels."""
    import uuid as _uuid

    def rate_of(el):
        tb = int(el.findtext("rate/timebase") or 24)
        return tb * 1000 / 1001.0 if (el.findtext("rate/ntsc") or "").upper() == "TRUE" else float(tb)

    def rate_el(fps):
        rt = ET.Element("rate")
        ET.SubElement(rt, "timebase").text = str(int(round(fps)))
        ET.SubElement(rt, "ntsc").text = "TRUE" if abs(fps - round(fps)) > 0.001 else "FALSE"
        return rt

    def order(el, tags):
        kids = list(el)
        for k in kids:
            el.remove(k)
        rank = {t: i for i, t in enumerate(tags)}
        for k in sorted(kids, key=lambda k: rank.get(k.tag, len(tags))):
            el.append(k)

    def fix_tc(tc, fps):
        if tc.find("rate") is None:
            tc.insert(0, rate_el(fps))
        if tc.find("string") is None:
            tc.append(ET.Element("string"))
        tc.find("string").text = fmt_frames(int(tc.findtext("frame") or 0), fps)
        if tc.find("displayformat") is None:
            ET.SubElement(tc, "displayformat").text = "NDF"
        order(tc, ["rate", "string", "frame", "displayformat"])

    files = {}
    for f in xm.iter("file"):
        if not len(f):
            continue
        files[f.get("id")] = f
        p = os.path.normpath(url_to_path(f.findtext("pathurl") or ""))
        fps = rate_of(f) if f.find("rate") is not None else 23.976
        facts = media_facts(p) or {}
        if f.find("timecode") is None:
            ET.SubElement(ET.SubElement(f, "timecode"), "frame").text = "0"
        fix_tc(f.find("timecode"), fps)
        md = f.find("media")
        if md is None:
            md = ET.SubElement(f, "media")
        has_v = md.find("video") is not None
        has_a = md.find("audio") is not None
        md_ch = md.findtext("audio/channelcount") if has_a else None
        if md_ch and len(md.findall("audio")) > 1:
            md_ch = len(md.findall("audio"))
        for x in list(md):
            md.remove(x)
        if has_v:
            sc = ET.SubElement(ET.SubElement(md, "video"), "samplecharacteristics")
            sc.append(rate_el(fps))
            ET.SubElement(sc, "width").text = str(facts.get("w") or 3840)
            ET.SubElement(sc, "height").text = str(facts.get("h") or 2160)
            ET.SubElement(sc, "anamorphic").text = "FALSE"
            ET.SubElement(sc, "pixelaspectratio").text = "square"
            ET.SubElement(sc, "fielddominance").text = "none"
        if has_a or not has_v:
            nch = max(1, facts.get("ch") or int(md_ch or 2))
            for ch in range(1, nch + 1):
                au = ET.SubElement(md, "audio")
                sc = ET.SubElement(au, "samplecharacteristics")
                ET.SubElement(sc, "depth").text = "16"
                ET.SubElement(sc, "samplerate").text = str(facts.get("sr") or 48000)
                ET.SubElement(au, "channelcount").text = "1"
                if nch == 2:   # Premiere's stereo shape: one block per channel, labelled as a pair
                    ET.SubElement(au, "layout").text = "stereo"
                ac = ET.SubElement(au, "audiochannel")
                ET.SubElement(ac, "sourcechannel").text = str(ch)
                if nch == 2:
                    ET.SubElement(ac, "channellabel").text = "left" if ch == 1 else "right"
        order(f, ["name", "pathurl", "rate", "duration", "timecode", "media"])
    n = 0
    for sq in xm.iter("sequence"):
        if sq.find("media") is None:
            continue
        fps = rate_of(sq)
        if sq.find("uuid") is None:
            ET.SubElement(sq, "uuid").text = str(_uuid.uuid4())
        if sq.find("timecode") is None:
            ET.SubElement(ET.SubElement(sq, "timecode"), "frame").text = "0"
        fix_tc(sq.find("timecode"), fps)
        order(sq, ["uuid", "duration", "rate", "name", "media", "timecode"])
        au = sq.find("media/audio")
        if au is None:
            au = ET.SubElement(sq.find("media"), "audio")
        if au.find("numOutputChannels") is None:
            ET.SubElement(au, "numOutputChannels").text = "2"
            fm = ET.SubElement(ET.SubElement(au, "format"), "samplecharacteristics")
            ET.SubElement(fm, "depth").text = "16"
            ET.SubElement(fm, "samplerate").text = "48000"
            outs = ET.SubElement(au, "outputs")
            for i in (1, 2):
                g = ET.SubElement(outs, "group")
                ET.SubElement(g, "index").text = str(i)
                ET.SubElement(g, "numchannels").text = "1"
                ET.SubElement(g, "downmix").text = "0"
                ET.SubElement(ET.SubElement(g, "channel"), "index").text = str(i)
        order(au, ["numOutputChannels", "format", "outputs", "track"])
        vfm = sq.find("media/video/format/samplecharacteristics")
        if vfm is not None:
            for k, v in (("anamorphic", "FALSE"), ("pixelaspectratio", "square"), ("fielddominance", "none")):
                if vfm.find(k) is None:
                    ET.SubElement(vfm, k).text = v
        for kind in ("video", "audio"):
            for t in sq.findall("media/%s/track" % kind):
                if t.find("enabled") is None:
                    ET.SubElement(t, "enabled").text = "TRUE"
                if t.find("locked") is None:
                    ET.SubElement(t, "locked").text = "FALSE"
                if kind == "audio" and not t.get("premiereTrackType"):
                    ti = sq.findall("media/audio/track").index(t)
                    chs = set()
                    for ci in t.findall("clipitem"):
                        f = ci.find("file")
                        full = files.get(f.get("id")) if f is not None else None
                        chs.add(len(full.findall("media/audio")) if full is not None else 0)
                    if chs == {2}:   # a stereo track, so Premiere plays both channels of a stereo file
                        t.attrib.update({"PannerCurrentValue": "0.5", "PannerIsInverted": "true", "PannerName": "Balance",
                                         "currentExplodedTrackIndex": "0", "totalExplodedTrackCount": "1",
                                         "premiereTrackType": "Stereo"})
                        if t.find("outputchannelindex") is None:
                            ET.SubElement(t, "outputchannelindex").text = "1" if ti % 2 == 0 else "2"
                for ci in t.findall("clipitem"):
                    n += 1
                    f = ci.find("file")
                    full = files.get(f.get("id")) if f is not None else None
                    if ci.find("duration") is None:
                        d = int(full.findtext("duration") or 0) if full is not None else 0
                        ffps = rate_of(full) if full is not None and full.find("rate") is not None else fps
                        ET.SubElement(ci, "duration").text = str(int(round(d * fps / ffps)) or
                                                                 int(ci.findtext("out") or 0))
                    if ci.find("masterclipid") is None and f is not None:
                        ET.SubElement(ci, "masterclipid").text = "masterclip-" + f.get("id").split("-")[-1]
                    order(ci, ["masterclipid", "name", "enabled", "duration", "rate", "start", "end", "in", "out",
                               "file", "sequence", "sourcetrack"])
                order(t, ["clipitem", "enabled", "locked"])
        # a shot's picture and sound linked, as Premiere writes them (unlinked they can import apart)
        groups = collections.defaultdict(list)
        for kind in ("video", "audio"):
            for ti, t in enumerate(sq.findall("media/%s/track" % kind), 1):
                for k, ci in enumerate(t.findall("clipitem"), 1):
                    f = ci.find("file")
                    if f is not None:
                        groups[(f.get("id"), ci.findtext("start"), ci.findtext("end"))].append((ci, kind, ti, k))
        for g in groups.values():
            if len(g) > 1 and any(x[1] == "audio" for x in g):
                for ci, _, _, _ in g:
                    for x in ci.findall("link"):
                        ci.remove(x)
                    for other, kind, ti, k in g:
                        ln = ET.SubElement(ci, "link")
                        ET.SubElement(ln, "linkclipref").text = other.get("id")
                        ET.SubElement(ln, "mediatype").text = kind
                        ET.SubElement(ln, "trackindex").text = str(ti)
                        ET.SubElement(ln, "clipindex").text = str(k)
                        if kind == "audio":
                            ET.SubElement(ln, "groupindex").text = "1"
    return xm


RESOLVE_HEAD = r'''
# Kickoff's DaVinci Resolve builder. Run by musicsync.py --mode resolve with a plan file; talks to
# Resolve Studio through its scripting API. Prints one JSON line per step. Exits with os._exit so
# Resolve's library doesn't crash Python on the way out (macOS "Python quit unexpectedly").
import json, os, subprocess, sys, time

def say(**k):
    sys.stdout.write(json.dumps(k) + "\n"); sys.stdout.flush()

def bye(code):
    sys.stdout.flush(); sys.stderr.flush(); os._exit(code)

API = os.environ.get("RESOLVE_SCRIPT_API") or "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
LIB = os.environ.get("RESOLVE_SCRIPT_LIB") or "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
os.environ["RESOLVE_SCRIPT_API"], os.environ["RESOLVE_SCRIPT_LIB"] = API, LIB
sys.path.append(os.path.join(API, "Modules"))
try:
    import DaVinciResolveScript as dvr
except Exception as e:
    say(nolib=str(e)); bye(3)

plan = json.load(open(sys.argv[1]))
resolve = dvr.scriptapp("Resolve")
if not resolve:
    say(stage="Opening DaVinci Resolve")
    subprocess.call(["open", "-a", "DaVinci Resolve"])
    for _ in range(90):
        time.sleep(2)
        resolve = dvr.scriptapp("Resolve")
        if resolve and resolve.GetProjectManager():
            break
if not resolve:
    say(error="Kickoff couldn't talk to DaVinci Resolve. It needs Resolve Studio (the free version blocks this), "
              "with Preferences > System > General > External scripting using set to Local.")
    bye(2)
time.sleep(1)
pm = resolve.GetProjectManager()

def export_timelines(proj, ex):
    """Every timeline as FCP7 XML or FCPXML: ex = {"to", "dir", "base"}. One timeline is <base>.<ext>;
    several go in a "<base> - <FORMAT>" folder, one file each. Returns the files written."""
    keys = ["EXPORT_FCP_7_XML"] if ex["to"] == "xml" else \
        ["EXPORT_FCPXML_1_11", "EXPORT_FCPXML_1_10", "EXPORT_FCPXML_1_9", "EXPORT_FCPXML_1_8"]
    const = next((getattr(resolve, k) for k in keys if getattr(resolve, k, None) is not None), None)
    if const is None:
        return []
    ext = ".xml" if ex["to"] == "xml" else ".fcpxml"
    n = proj.GetTimelineCount() or 0
    out, d = [], ex["dir"]
    if n > 1:
        d = os.path.join(d, "%s - %s" % (ex["base"], "XML" if ex["to"] == "xml" else "FCPXML"))
        os.makedirs(d, exist_ok=True)
    for i in range(1, n + 1):
        t = proj.GetTimelineByIndex(i)
        if not t:
            continue
        say(stage="Writing %s" % t.GetName())
        nm = ex["base"] if n == 1 else "".join(c if c not in '/:' else "-" for c in t.GetName())
        path = os.path.join(d, nm + ext)
        if t.Export(path, const, getattr(resolve, "EXPORT_NONE", 0)):
            out.append(path)
    return out
'''

RESOLVE_DRIVER = RESOLVE_HEAD + r'''name = plan["name"]
drp = plan["drp"]
names = set(pm.GetProjectListInCurrentFolder() or [])
if name in names and os.path.exists(drp) and os.path.getmtime(drp) >= plan["xml_mtime"] and pm.LoadProject(name):
    say(stage="Opening the project in Resolve")
    resolve.OpenPage("edit")
    say(result={"project": name, "drp": drp, "reused": True})
    bye(0)
n, proj = 1, None
while proj is None and n < 50:
    pn = name if n == 1 else "%s %d" % (name, n)
    if pn not in names:
        proj = pm.CreateProject(pn)
        if proj is None:
            break
    n += 1
if proj is None:
    say(error="Resolve has a window open that's waiting on you (often \"Save changes to Untitled Project?\"). "
              "Click Don't Save in Resolve, then click Open in Resolve again."); bye(2)
pname = proj.GetName()
say(stage="Setting up the Resolve project")
for k, v in (("timelineFrameRate", plan["fps"]), ("timelinePlaybackFrameRate", plan["fps"]),
             ("timelineResolutionWidth", str(plan["width"])), ("timelineResolutionHeight", str(plan["height"]))):
    proj.SetSetting(k, v)
mp = proj.GetMediaPool()
root = mp.GetRootFolder()
folders, media_folders, by_path = {(): root}, [], {}

def folder(path):
    path = tuple(path)
    if path not in folders:
        parent = folder(path[:-1])
        f = next((s for s in (parent.GetSubFolderList() or []) if s.GetName() == path[-1]), None)
        folders[path] = f or mp.AddSubFolder(parent, path[-1])
    return folders[path]

say(stage="Bringing the footage into Resolve")
for b in plan["bins"]:
    f = folder(b["path"])
    if b["media"]:
        mp.SetCurrentFolder(f)
        for it in mp.ImportMedia(b["media"]) or []:
            by_path[it.GetClipProperty("File Path")] = it
        media_folders.append(f)
missing_media = [p for b in plan["bins"] for p in b["media"] if p not in by_path]

def item_for(path):
    it = by_path.get(path)
    if it is None:
        mp.SetCurrentFolder(folder(["Kickoff extras"]))
        got = mp.ImportMedia([path]) or []
        if got:
            it = by_path[path] = got[0]
    return it

conformed = {}
def conformed_item(path, fps):           # a second copy of a high frame rate clip, played at the timeline rate
    if path in conformed:
        return conformed[path]
    mp.SetCurrentFolder(folder(plan["slowmo_bin"]))
    got = mp.ImportMedia([path]) or []
    orig = by_path.get(path)
    it = got[0] if got and (orig is None or got[0].GetUniqueId() != orig.GetUniqueId()) else None
    if it is not None and not it.SetClipProperty("FPS", fps):
        it = None
    conformed[path] = it
    return it

report, placed, offline = [], 0, 0
for s in plan["sequences"]:
    say(stage="Adding %s" % s["name"])
    mp.SetCurrentFolder(folder(s["bin"]))
    tl = mp.ImportTimelineFromFile(s["xml"], {"timelineName": s["name"], "importSourceClips": False,
                                              "sourceClipsFolders": media_folders})
    if not tl:
        report.append({"name": s["name"], "ok": False}); continue
    proj.SetCurrentTimeline(tl)
    t0 = tl.GetStartFrame()
    fixed = bad = 0
    for kind, tracks in (("video", s["video"]), ("audio", s["audio"])):
        for ti, tr in enumerate(tracks, 1):
            if not tr["on"]:
                tl.SetTrackEnable(kind, ti, False)
            have = tl.GetItemListInTrack(kind, ti) or [] if ti <= tl.GetTrackCount(kind) else []
            for c in tr["clips"]:
                if c.get("nest") or not c.get("path"):
                    continue
                # Resolve's frame may be one off: of the clips within a frame of the spot, the one from this
                # file counts; another file's clip is only replaced when it sits exactly there (a 1-frame clip's
                # neighbour starts a frame away and stays)
                near = [x for x in have if abs(x.GetStart() - t0 - c["start"]) <= 1]
                def same(x):
                    m = x.GetMediaPoolItem()
                    return m is not None and os.path.basename(m.GetClipProperty("File Path") or "") == os.path.basename(c["path"])
                hit = next((x for x in near if same(x)), None)
                if hit is not None:
                    if not c["on"] and hit.GetClipEnabled():
                        hit.SetClipEnabled(False)
                    continue
                hit = next((x for x in near if x.GetStart() - t0 == c["start"]), None)
                if hit:
                    tl.DeleteClips([hit], False)
                    have = [x for x in have if x is not hit]
                sp = c.get("speed", 100.0)
                if abs(sp - 100.0) < 0.01:
                    it, a, b = item_for(c["path"]), c["in"] * c["ffps"] / s["fps"], c["out"] * c["ffps"] / s["fps"]
                elif abs(s["fps"] / c["ffps"] * 100 - sp) < 0.5:
                    # played a frame a timeline frame; in/out are source time at the timeline rate
                    # (Kickoff) or already the conformed frames (Premiere, where out-in = the length)
                    a = c["in"] if c["out"] - c["in"] == c["end"] - c["start"] else c["in"] * c["ffps"] / s["fps"]
                    it, b = conformed_item(c["path"], plan["fps"]), a + c["end"] - c["start"]
                else:
                    it = None
                if it is None:
                    bad += 1; continue
                got = mp.AppendToTimeline([{"mediaPoolItem": it, "startFrame": int(round(a)), "endFrame": int(round(b)),
                                            "mediaType": 1 if kind == "video" else 2, "trackIndex": ti,
                                            "recordFrame": t0 + c["start"]}]) or []
                if got:
                    fixed += 1
                    if not c["on"]:
                        for g in got:
                            g.SetClipEnabled(False)
                else:
                    bad += 1
    placed += fixed; offline += bad
    report.append({"name": s["name"], "ok": True, "placed": fixed, "offline": bad})

say(stage="Saving the Resolve project")
pm.SaveProject()
ok = pm.ExportProject(pname, drp, False) if drp else False
exported = export_timelines(proj, plan["export"]) if plan.get("export") else []
s_first = next((s for s in plan["sequences"] if s.get("open")), None)
if s_first:
    for i in range(1, proj.GetTimelineCount() + 1):
        t = proj.GetTimelineByIndex(i)
        if t and t.GetName() == s_first["name"]:
            proj.SetCurrentTimeline(t)
resolve.OpenPage("edit")
say(result={"project": pname, "drp": drp if ok else "", "timelines": report, "placed": placed, "offline": offline,
            "missing_media": missing_media, "outputs": exported})
bye(0)
'''


# --mode convert, for an FCPXML or a .drp: Resolve opens it and writes it back out in another format
CONVERT_DRIVER = RESOLVE_HEAD + r"""names = set(pm.GetProjectListInCurrentFolder() or [])
BLOCKED = ("Resolve has a window open that's waiting on you (often \"Save changes to Untitled Project?\"). "
           "Click Don't Save in Resolve, then try again.")
n, pname = 1, plan["name"]
while pname in names:
    n += 1
    pname = "%s %d" % (plan["name"], n)
if True:                                   # a .drp: Resolve opens a copy of it under a new name
    say(stage="Opening the project in Resolve")
    if not pm.ImportProject(plan["src"], pname):
        say(error="Resolve couldn't open that .drp. It may be from a newer Resolve, or damaged."); bye(2)
    proj = pm.LoadProject(pname)
    if proj is None:
        say(error=BLOCKED); bye(2)
outputs, offline = [], 0
for i in range(1, (proj.GetTimelineCount() or 0) + 1):
    t = proj.GetTimelineByIndex(i)
    for kind in ("video", "audio"):
        for ti in range(1, (t.GetTrackCount(kind) or 0) + 1):
            for it in t.GetItemListInTrack(kind, ti) or []:
                mpi = it.GetMediaPoolItem() if hasattr(it, "GetMediaPoolItem") else None
                if mpi is not None and not mpi.GetClipProperty("File Path") and \
                        "compound" not in (mpi.GetClipProperty("Type") or "").lower() and \
                        "timeline" not in (mpi.GetClipProperty("Type") or "").lower():
                    offline += 1
if plan["to"] == "drp":
    say(stage="Saving the Resolve project")
    pm.SaveProject()
    if pm.ExportProject(pname, plan["drp"], False):
        outputs.append(plan["drp"])
else:
    outputs = export_timelines(proj, plan["export"])
n_tl = proj.GetTimelineCount() or 0
if plan["to"] != "drp":                  # the copy was only for reading it out: take it away again
    pm.SaveProject()
    if hasattr(pm, "CloseProject") and pm.CloseProject(proj) and hasattr(pm, "DeleteProject"):
        pm.DeleteProject(pname)
resolve.OpenPage("edit")
say(result={"project": pname, "outputs": outputs, "timelines": n_tl, "offline": offline})
bye(0)
"""


RESOLVE_FPS = {23.976: "23.976", 24: "24", 25: "25", 29.97: "29.97", 30: "30", 47.952: "47.952", 48: "48",
               50: "50", 59.94: "59.94", 60: "60"}


def resolve_prep(xml_path, work):
    """Lay a Premiere XML (Kickoff's, or one exported from Premiere) out for DaVinci Resolve: Resolve
    makes one timeline per XML, so every sequence gets its own file (nests and files written in full),
    plus a plan of the bins, their media and every clip's spot, for the builder to check and patch."""
    root = ET.parse(xml_path).getroot()
    proj = root.find("project")
    if proj is None and root.find("sequence") is not None:
        # a sequence exported on its own (File > Export > Final Cut Pro XML with one sequence picked)
        proj = ET.SubElement(root, "project")
        ET.SubElement(proj, "name").text = os.path.splitext(os.path.basename(xml_path))[0]
        kids = ET.SubElement(proj, "children")
        for sq in root.findall("sequence"):
            root.remove(sq)
            kids.append(sq)
    if proj is None:
        raise RuntimeError("that XML has no project in it")
    pname = proj.findtext("name") or os.path.splitext(os.path.basename(xml_path))[0]
    top = {}                                     # sequence id -> the ids of the sequences it nests
    for s in root.iter("sequence"):
        if len(s) and s.get("id"):
            top.setdefault(s.get("id"), {n.get("id") for ci in s.iter("clipitem")
                                         for n in ci.findall("sequence") if n.get("id")})
    full = resolve_copy(root)                    # every nest in full, clips on switched-off tracks off
    files = {}
    for f in full.iter("file"):
        if len(f) and f.get("id") not in files:
            files[f.get("id")] = f

    def fpath(f):
        d = files.get(f.get("id")) if f is not None else None
        u = d.findtext("pathurl") if d is not None else None
        return os.path.normpath(url_to_path(u)) if u else None

    bins, seqs = [], []
    def walk(el, path):
        ch = el.find("children")
        for c in (ch if ch is not None else []):
            if c.tag == "bin":
                name = c.findtext("name") or "Bin"
                media = []
                for clip in (c.find("children") if c.find("children") is not None else []):
                    if clip.tag == "clip":
                        p = next((fpath(f) for f in clip.iter("file") if fpath(f)), None)
                        if p and "_empty-bin-placeholders" not in p and p not in media:
                            media.append(p)
                bins.append({"path": path + [name], "media": media})
                walk(c, path + [name])
            elif c.tag == "sequence" and len(c):
                seqs.append((path, c))
    walk(full.find("project"), [])

    os.makedirs(work, exist_ok=True)
    ids = {s.get("id") for _, s in seqs}
    order, placed = [], set()
    while len(order) < len(seqs):               # nested sequences before the ones that use them
        left = [x for x in seqs if x not in order]
        ready = [x for x in left if not ((top.get(x[1].get("id"), set()) & ids) - placed - {x[1].get("id")})]
        nxt = (ready or left)[0]
        order.append(nxt)
        placed.add(nxt[1].get("id"))

    rates = collections.Counter()
    plan_seqs = []
    for k, (path, s) in enumerate(order, 1):
        s = copy.deepcopy(s)
        sfps = xml_fps(s)
        fmt = s.find("media/video/format/samplecharacteristics")
        w = int(fmt.findtext("width") or 3840) if fmt is not None else 3840
        h = int(fmt.findtext("height") or 2160) if fmt is not None else 2160
        rates[(snap_fps(sfps) or 23.976, w, h)] += 1
        seen = set()
        for f in s.iter("file"):                 # each file's first use carries its whole definition
            if not len(f) and f.get("id") not in seen and f.get("id") in files:
                for kid in files[f.get("id")]:
                    f.append(copy.deepcopy(kid))
            seen.add(f.get("id"))
            if len(f):
                resolve_tc(f, real_tc(fpath(f)))
        tracks = {}
        for kind in ("video", "audio"):
            tracks[kind] = []
            for t in s.findall("media/%s/track" % kind):
                clips = []
                for ci in list(t.findall("clipitem")):
                    f = ci.find("file")
                    p = fpath(f) if f is not None else None
                    nest = ci.find("sequence") is not None
                    if not p and not nest:       # a generator (Premiere's Black Video slug) stops Resolve's import
                        t.remove(ci)
                        continue
                    sp = next((float(x.findtext("value") or 100) for x in ci.iter("parameter")
                               if x.findtext("parameterid") == "speed"), 100.0)
                    st, en, a, b = (int(ci.findtext(x) or 0) for x in ("start", "end", "in", "out"))
                    d = files.get(f.get("id")) if f is not None else None
                    ffps = xml_fps(d) if d is not None and d.find("rate") is not None else sfps
                    if round(ffps) == 119:               # Premiere writes 119.88 fps as a plain 119
                        ffps = 119.88
                    if d is not None and d.find("media/video") is None:
                        ffps = sfps                      # a sound file counts frames at the timeline's rate
                    if p and abs(sp - 100) < 0.01 and d is not None and d.findtext("duration"):
                        # a clip that runs to the file's very last frame: Resolve may count one frame
                        # less and call the file missing, so it ends a frame early
                        last = int(int(d.findtext("duration")) * sfps / ffps + 1e-6)
                        if b >= last and en - st > 1:
                            b, en = b - 1, en - 1
                            ci.find("out").text, ci.find("end").text = str(b), str(en)
                        elif b >= last and a > 0:        # a 1-frame clip there shows the frame before instead
                            a, b = a - 1, b - 1
                            ci.find("in").text, ci.find("out").text = str(a), str(b)
                    clips.append({"start": st, "end": en, "in": a, "out": b, "path": p, "nest": nest,
                                  "ffps": ffps, "speed": sp, "on": (ci.findtext("enabled") or "TRUE") != "FALSE"})
                tracks[kind].append({"on": (t.findtext("enabled") or "TRUE") != "FALSE", "clips": clips})
        # Resolve links a clip by its name: a renamed one ("... (no match to song)", "(pass 2 of 3)")
        # comes in offline, so every clip, nested ones too, carries its file's own name
        for ci in s.iter("clipitem"):
            p = fpath(ci.find("file")) if ci.find("file") is not None else None
            if p and ci.find("name") is not None:
                ci.find("name").text = os.path.basename(p)
        name = s.findtext("name") or "Sequence %d" % k
        out = os.path.join(work, "%02d %s.xml" % (k, re.sub(r'[/:\\\\]+', "_", name)))
        x = ET.Element("xmeml", version="4")
        x.append(s)
        write_xml(x, out)
        plan_seqs.append({"name": name, "bin": path, "xml": out, "fps": sfps, "video": tracks["video"],
                          "audio": tracks["audio"], "open": bool(re.search(r"(?i)_edit$", name))})
    (fps, w, h), _ = rates.most_common(1)[0] if rates else ((23.976, 3840, 2160), 0)
    if not any(s["open"] for s in plan_seqs) and plan_seqs:
        plan_seqs[-1]["open"] = True
    # files no bin holds (a sequence exported on its own has no bins): Footage, and audio under Music
    held = {m for b in bins for m in b["media"]}
    loose = {}
    for f in files.values():
        p = fpath(f)
        if p and p not in held:
            held.add(p)
            aud = os.path.splitext(p)[1].lower() in (".wav", ".mp3", ".aif", ".aiff", ".m4a", ".flac")
            loose.setdefault("Music" if aud else "Footage", []).append(p)
    for k in sorted(loose):
        bins.append({"path": [k], "media": sorted(loose[k])})
    foot = next((b["path"] for b in bins if b["path"] and b["path"][0].lower() == "footage"), ["Footage"])
    return {"name": pname, "fps": RESOLVE_FPS.get(round(fps, 3) if fps % 1 else int(fps), "%g" % fps),
            "width": w, "height": h, "bins": bins, "sequences": plan_seqs,
            "slowmo_bin": foot[:1] + ["Slow-mo (conformed %s)" % RESOLVE_FPS.get(round(fps, 3) if fps % 1 else int(fps), "%g" % fps)],
            "drp": os.path.splitext(os.path.abspath(xml_path))[0] + ".drp", "xml_mtime": os.path.getmtime(xml_path)}


_REAL_TC = {}


def real_tc(path):
    """The file's own timecode label (any stream: Sony keeps it on a metadata stream), or None.
    Premiere's XML export makes up the timecode of 119.88 fps clips."""
    if not path or not os.path.isfile(path):
        return None
    if path not in _REAL_TC:
        r = run(["ffprobe", "-v", "error", "-show_entries", "format_tags=timecode:stream_tags=timecode", "-of", "json", path])
        try:
            j = json.loads((r.stdout or b"{}").decode(errors="replace"))
        except ValueError:
            j = {}
        tags = [(j.get("format") or {}).get("tags") or {}] + [st.get("tags") or {} for st in j.get("streams") or []]
        _REAL_TC[path] = next((t["timecode"] for t in tags if t.get("timecode")), None)
    return _REAL_TC[path]


def resolve_tc(f, label=None):
    """The file's real timecode (label), and for a 119.88/120 fps file the way Resolve labels it: at
    half the camera's 120 count (17:31:30:60 reads 17:31:30:30), with no frame count, which is how
    Resolve's own XML writes it and the only way it links."""
    tc = f.find("timecode")
    if tc is None or f.find("rate") is None:
        return
    if label and tc_frames(label, xml_fps(f)) is not None and tc.find("string") is not None:
        tc.find("string").text = label
        if tc.find("frame") is not None:
            tc.find("frame").text = str(tc_frames(label, xml_fps(f) if f.findtext("rate/timebase") != "119" else 119.88))
    if f.findtext("rate/timebase") not in ("119", "120"):
        return
    for r in [f.find("rate"), tc.find("rate")] + list(f.iter("samplecharacteristics")):
        r = r.find("rate") if r is not None and r.tag == "samplecharacteristics" else r
        if r is not None and r.findtext("timebase") in ("119", "120"):
            r.find("timebase").text, r.find("ntsc").text = "120", "TRUE"
    m = re.match(r"^(\d+)[:;.](\d+)[:;.](\d+)[:;.,](\d+)$", (tc.findtext("string") or "").strip())
    if m and tc.find("frame") is not None:
        h, mi, s_, fr = (int(v) for v in m.groups())
        tc.find("string").text = "%02d:%02d:%02d:%02d" % (h, mi, s_, fr // 2)
        tc.remove(tc.find("frame"))


def run_driver(driver, plan, work):
    """Run a Resolve script (RESOLVE_DRIVER / CONVERT_DRIVER) on a plan; relays its steps as events.
    Returns its result, or exits with its error."""
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "plan.json"), "w") as fh:
        json.dump(plan, fh)
    with open(os.path.join(work, "build.py"), "w") as fh:
        fh.write(driver)
    # Resolve's scripting library loads in a plain Python 3 (the engine's own, else the Mac's)
    pys = [sys.executable] + [p for p in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3")
                              if os.path.exists(p) and p != sys.executable]
    result = err = None
    for py in pys:
        pr = subprocess.Popen([py, os.path.join(work, "build.py"), os.path.join(work, "plan.json")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        nolib = False
        for line in pr.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "stage" in msg:
                event("stage", text=msg["stage"])
            elif "nolib" in msg:
                nolib = True
            elif "error" in msg:
                err = msg["error"]
            elif "result" in msg:
                result = msg["result"]
        pr.wait()
        if not nolib:
            break
    if result is None:
        sys.exit("error: %s" % (err or "Kickoff couldn't find DaVinci Resolve Studio on this Mac."))
    return result


def resolve_build(xml, drp=None, export=None):
    """Build a Premiere XML in DaVinci Resolve Studio: saves a .drp (next to the XML unless drp is given;
    "" for none) and, with export={"to","dir","base"}, writes every timeline back out as XML or FCPXML."""
    event("stage", text="Getting the project ready for Resolve")
    work = os.path.join(tempfile.gettempdir(), "kickoff-resolve", hashlib.md5(os.path.abspath(xml).encode()).hexdigest()[:10])
    shutil.rmtree(work, ignore_errors=True)
    try:
        plan = resolve_prep(os.path.abspath(xml), work)
    except (RuntimeError, ET.ParseError, OSError) as e:
        sys.exit("error: %s" % e)
    if drp is not None:
        plan["drp"] = drp
    if export:
        plan["export"] = export
    log("%d sequences and %d bins ready for Resolve" % (len(plan["sequences"]), len(plan["bins"])))
    result = run_driver(RESOLVE_DRIVER, plan, work)
    if result.get("reused"):
        log("%s was already in Resolve, so Kickoff opened it" % result["project"])
    else:
        for t in result.get("timelines", []):
            log("  %s: %s" % (t["name"], "in" if t.get("ok") else "Resolve wouldn't take it") +
                (", %d clips placed by hand" % t["placed"] if t.get("placed") else "") +
                (", %d clips still offline" % t["offline"] if t.get("offline") else ""))
        for p in result.get("missing_media", []):
            log("  Resolve couldn't bring in %s" % p)
        log("Made %s in Resolve%s" % (result["project"], " and saved %s" % result["drp"] if result.get("drp") else ""))
    return result


def resolve_main(args):
    """--mode resolve PROJECT.xml: build the project in DaVinci Resolve Studio and save a .drp next to
    the XML. The window's "Open in Resolve" button runs this."""
    xml = next((p for p in args.paths if p.lower().endswith(".xml") and os.path.isfile(p)), None)
    if not xml:
        sys.exit("error: give the project XML to open in Resolve")
    result = resolve_build(xml)
    event("resolve", project=result["project"], drp=result.get("drp", ""), reused=bool(result.get("reused")),
          offline=result.get("offline", 0), timelines=len(result.get("timelines", [])))


CONVERT_EXT = {"xml": ".xml", "fcpxml": ".fcpxml", "drp": ".drp", "prproj": ".prproj"}


def convert_kind(path):
    """What an edit file is: xml (FCP7 / Premiere), fcpxml, drp or prproj; None for anything else."""
    low = path.lower().rstrip("/")
    if low.endswith(".fcpxmld") and (os.path.isdir(path) or zipfile.is_zipfile(path)):
        return "fcpxml"                          # a bundle, or one sent zipped
    ext = os.path.splitext(low)[1]
    if ext == ".xml":
        try:
            with open(path, "rb") as fh:
                head = fh.read(4000).decode("utf-8", "ignore").lower()
        except OSError:
            return None
        return "fcpxml" if "<fcpxml" in head else "xml" if "<xmeml" in head else None
    return {".fcpxml": "fcpxml", ".drp": "drp", ".prproj": "prproj"}.get(ext)


def free_path(d, base, ext):
    """d/base+ext, or "base 2", "base 3"... so nothing already there is overwritten."""
    p, n = os.path.join(d, base + ext), 2
    while os.path.exists(p):
        p, n = os.path.join(d, "%s %d%s" % (base, n, ext)), n + 1
    return p


def fcpxml_prep(src, out, near=None):
    """A copy of an FCPXML for Resolve: media that isn't where the file says (made on another Mac) is
    looked for by name near the FCPXML and on its drive, and a version newer than Resolve reads (1.11)
    is labelled 1.11. Returns notes for the report."""
    with open(src, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    notes = []
    m = re.search(r'<fcpxml version="(\d+)\.(\d+)"', text)
    if m and (int(m.group(1)), int(m.group(2))) > (1, 11):
        text = text.replace(m.group(0), '<fcpxml version="1.11"', 1)
    srcs = sorted(set(re.findall(r'src="(file://[^"]+)"', text)))
    missing = {}
    for u in srcs:
        p = url_to_path(u.replace("file://localhost", "file://"))
        if not os.path.exists(p):           # matched by name without the extension: a camera's .MP4
            missing.setdefault(os.path.splitext(os.path.basename(p))[0].lower(), []).append(u)   # can be FCP's .mov
    if missing:
        here = near or os.path.dirname(os.path.abspath(src.rstrip("/")))
        if here.endswith(".fcpxmld"):
            here = os.path.dirname(here)
        parts = here.split(os.sep)
        roots = [here]
        if len(parts) > 2 and parts[1] == "Volumes":
            roots.append(os.sep.join(parts[:3]))            # the whole drive it's on
        if os.path.isdir("/Volumes"):                      # then every other drive plugged in
            roots += [os.path.join("/Volumes", v) for v in sorted(os.listdir("/Volumes"))
                      if not v.startswith(".") and os.path.join("/Volumes", v) not in roots
                      and not os.path.islink(os.path.join("/Volumes", v))]
        found = {}
        for root in roots:
            for dp, dns, fns in os.walk(root):
                dns[:] = [d for d in dns if not d.startswith(".") and not re.search(r"(?i)prox(y|ies)|render|cache", d)
                          and dp.count(os.sep) - root.count(os.sep) < 7]
                for fn in fns:
                    k, ext = os.path.splitext(fn.lower())
                    if k in missing and k not in found and ext in MEDIA_EXT | AUDIO_EXT:
                        found[k] = os.path.join(dp, fn)
                if len(found) == len(missing):
                    break
            if len(found) == len(missing):
                break
        for k, us in missing.items():
            if k in found:
                for u in us:
                    text = text.replace('src="%s"' % u, 'src="%s"' % path_to_url(found[k], []).replace("file://localhost", "file://"))
        if found:
            notes.append("Found %d media files in new places and linked them" % len(found))
        if len(found) < len(missing):
            notes.append("%d media files aren't on this Mac, so they're offline" % (len(missing) - len(found)))
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    return notes


SYNCED_TAG = "ks"          # prefix of the ids Synced Audio adds, so they never clash with the edit's own


def narrative_reports(paths):
    """{video path (lowercase): (sound file path, seconds into it the clip starts)} from the Narrative
    reports (Kickoff Exports/narrative_report.csv) of the shoots these files are in."""
    found, seen = {}, set()
    for vp in paths:
        d = os.path.dirname(vp)
        while d and d not in seen and os.path.dirname(d) != d:
            seen.add(d)
            for sub_ in ("Kickoff Exports", "Premiere Sync"):
                rp = os.path.join(d, sub_, "narrative_report.csv")
                if not os.path.isfile(rp):
                    continue
                sounds = collections.defaultdict(list)
                for root_, dirs, files in os.walk(d):
                    dirs[:] = sorted(x for x in dirs if not skip_dir(x) and x not in ("Kickoff Exports", "Premiere Sync"))
                    for f in sorted(files):
                        if os.path.splitext(f)[1].lower() in AUDIO_EXT:
                            sounds[f.lower()].append(os.path.join(root_, f))
                with open(rp, newline="") as fh:
                    for row in csv.DictReader(fh):
                        if row.get("status") != "placed" or not row.get("audio_file"):
                            continue
                        want = row.get("audio_path") and os.path.join(d, row["audio_path"])
                        cands = sounds.get(row["audio_file"].lower(), [])
                        ap = want if want and os.path.isfile(want) else (cands[0] if cands else None)
                        if ap:
                            found[os.path.normpath(os.path.join(d, row["file"])).lower()] = \
                                (ap, float(row["starts_into_audio_s"]))
            d = os.path.dirname(d)
    return found


def synced_audio(src, out):
    """A copy of a Premiere sequence XML with, under every shot, its camera's own audio and the sound
    recordist's audio it was synced to (from the shoot's Narrative sync), on new tracks at the bottom,
    linked to the picture. Returns notes for the window."""
    tree = ET.parse(src)
    root = tree.getroot()
    seq = root.find("sequence") if root.find("sequence") is not None else next(root.iter("sequence"), None)
    if seq is None or seq.find("media") is None:
        sys.exit("error: that XML has no sequence in it")
    files = {f.get("id"): f for f in root.iter("file") if len(f)}
    taken = {e.get("id") for e in root.iter() if e.get("id")}
    n_id = [0]

    def new_id(kind):
        while True:
            n_id[0] += 1
            i = "%s-%s-%d" % (kind, SYNCED_TAG, n_id[0])
            if i not in taken:
                taken.add(i)
                return i

    def rate_of(el):
        tb = int(el.findtext("rate/timebase") or 24)
        return tb * 1000 / 1001.0 if (el.findtext("rate/ntsc") or "").upper() == "TRUE" else float(tb)

    fps = rate_of(seq)
    audio = seq.find("media/audio")
    if audio is None:
        audio = ET.SubElement(seq.find("media"), "audio")
    old_tracks = audio.findall("track")
    shots = []                                 # (video clipitem, file element, start, end, source seconds)
    skipped = collections.Counter()
    for vt in seq.findall("media/video/track"):
        for ci in vt.findall("clipitem"):
            fr = ci.find("file")
            f = files.get(fr.get("id")) if fr is not None else None
            if f is None or not f.findtext("pathurl"):
                if ci.find("sequence") is not None:
                    skipped["nested sequences"] += 1
                continue
            a, b = int(ci.findtext("start") or -1), int(ci.findtext("end") or -1)
            i_, o_ = int(ci.findtext("in") or 0), int(ci.findtext("out") or 0)
            if a < 0 and b >= 0:
                a = b - (o_ - i_)
            elif b < 0 and a >= 0:
                b = a + (o_ - i_)
            if a < 0 or b <= a:
                continue
            speed = 100.0
            for eff in ci.iter("effect"):
                if (eff.findtext("effectid") or "").lower() == "timeremap":
                    for prm in eff.iter("parameter"):
                        if (prm.findtext("parameterid") or "") == "speed":
                            speed = float(prm.findtext("value") or 100)
            if abs(speed - 100) > 0.01 or abs(b - a - (o_ - i_)) > 1:
                skipped["shots with a speed change"] += 1
                continue
            ticks = ci.findtext("pproTicksIn")
            s0 = int(ticks) / PR_TICKS if ticks else i_ / rate_of(ci)
            shots.append((ci, f, a, b, s0))
    if not shots:
        sys.exit("error: no camera shots found in that sequence")
    synced = narrative_reports({os.path.normpath(url_to_path(f.findtext("pathurl"))) for _, f, _, _, _ in shots})
    if not synced:
        sys.exit("error: these shots haven't been synced yet. Sync the footage in the Narrative tab "
                 "first, then add the synced audio")

    rec_files, rec_info = {}, {}

    def recorder_file(path):
        if path not in rec_info:
            tc, dur, ch, rate = audio_timecode(path)
            rec_info[path] = (dur, max(1, ch), rate, tc)
        dur, ch, rate, tc = rec_info[path]
        if path in rec_files:
            return ET.Element("file", id=rec_files[path]), ch
        fid = new_id("file")
        rec_files[path] = fid
        f = ET.Element("file", id=fid)
        sub(f, "name", os.path.basename(path))
        sub(f, "pathurl", path_to_url(path, ()))
        add_rate(f, fps)
        sub(f, "duration", int(round(dur * fps)))
        add_timecode(f, fps, int(round((tc or 0) * fps)))
        media = sub(f, "media")
        for k in range(ch):
            a = sub(media, "audio")
            sc = sub(a, "samplecharacteristics")
            sub(sc, "depth", 24)
            sub(sc, "samplerate", rate)
            sub(a, "channelcount", 1)
            sub(a, "layout", "stereo" if ch == 2 else "mono")
            ac = sub(a, "audiochannel")
            sub(ac, "sourcechannel", k + 1)
            sub(ac, "channellabel", ("left", "right")[k] if ch == 2 else "discrete")
        return f, ch

    def camera_channels(f):
        blocks = f.findall("media/audio")
        n = sum(max(1, int(b.findtext("channelcount") or 1)) for b in blocks)
        return n, ("stereo" if n == 2 else "mono")

    # new tracks at the bottom, in lanes so overlapping shots (a cutaway over the A Cam) never share one
    lanes = {"cam": [], "rec": []}             # role -> [[track, ...] per lane], with each lane's busy spans
    busy = {"cam": [], "rec": []}

    def lane_for(role, a, b, nch):
        for k, spans in enumerate(busy[role]):
            if all(b <= x or a >= y for x, y in spans):
                break
        else:
            k = len(busy[role])
            busy[role].append([])
            lanes[role].append([])
        busy[role][k].append((a, b))
        while len(lanes[role][k]) < nch:
            lanes[role][k].append(ET.Element("track"))
        return lanes[role][k]

    def item(track, f, name, a, b, src_in_s, ch, kind, enabled, frames):
        ci = ET.Element("clipitem", id=new_id("clipitem"), premiereChannelType=kind)
        sub(ci, "name", name)
        sub(ci, "enabled", "TRUE" if enabled else "FALSE")
        sub(ci, "duration", frames)
        add_rate(ci, fps)
        sub(ci, "start", a)
        sub(ci, "end", b)
        sub(ci, "in", int(round(src_in_s * fps)))
        sub(ci, "out", int(round(src_in_s * fps)) + (b - a))
        sub(ci, "pproTicksIn", int(round(src_in_s * PR_TICKS)))
        sub(ci, "pproTicksOut", int(round((src_in_s + (b - a) / fps) * PR_TICKS)))
        ci.append(f)
        st = sub(ci, "sourcetrack")
        sub(st, "mediatype", "audio")
        sub(st, "trackindex", ch)
        track.append(ci)
        return ci

    groups, n_cam, n_rec, no_sound = [], 0, 0, set()
    for ci, f, a, b, s0 in sorted(shots, key=lambda x: (x[2], x[3])):
        path = os.path.normpath(url_to_path(f.findtext("pathurl")))
        enabled = (ci.findtext("enabled") or "TRUE").upper() != "FALSE"
        members = []
        nch, kind = camera_channels(f)
        if nch:
            tracks = lane_for("cam", a, b, nch)
            for k in range(nch):
                members.append(item(tracks[k], ET.Element("file", id=f.get("id")), ci.findtext("name") or
                                    f.findtext("name"), a, b, s0, k + 1, kind, enabled,
                                    int(round(int(f.findtext("duration") or 0) * fps / rate_of(f)))))
            n_cam += 1
        hit = synced.get(path.lower())
        if hit:
            rpath, into = hit
            rf, rch = recorder_file(rpath)
            dur = rec_info[rpath][0]
            ra, rb, r0 = a, b, s0 + into
            if r0 < 0:                              # the camera rolled before the recorder: trim the head
                cut = int(math.ceil(-r0 * fps - 1e-6))
                ra, r0 = ra + cut, r0 + cut / fps
            if r0 + (rb - ra) / fps > dur:          # ... or kept rolling after it stopped
                rb = ra + int(math.floor((dur - r0) * fps + 1e-6))
            if rb > ra:
                tracks = lane_for("rec", ra, rb, rch)
                for k in range(rch):
                    members.append(item(tracks[k], rf if k == 0 and len(rf) else ET.Element("file", id=rf.get("id")),
                                        os.path.basename(rpath), ra, rb, r0, k + 1,
                                        "stereo" if rch == 2 else "mono", enabled, int(round(dur * fps))))
                    rf = ET.Element("file", id=rf.get("id"))
                n_rec += 1
        else:
            no_sound.add(os.path.basename(path))
        if members:
            groups.append([ci] + members)

    # tracks: camera audio first, then the recordist's, each in Premiere's stereo-pair shape
    new_tracks = [t for lane in lanes["cam"] for t in lane] + [t for lane in lanes["rec"] for t in lane]
    attrs = dict(old_tracks[-1].attrib) if old_tracks else {}
    first = (int(attrs.get("currentExplodedTrackIndex") or 1) + 1) % 2     # carry on the edit's L/R pairs
    for k, t in enumerate(new_tracks):
        pos = first + k
        t.attrib.update(attrs)
        t.attrib.update({"PannerIsInverted": "true", "PannerCurrentValue": "0.5", "PannerName": "Balance",
                         "currentExplodedTrackIndex": str(pos % 2), "totalExplodedTrackCount": "2",
                         "premiereTrackType": "Stereo"})
        kids = sorted(t.findall("clipitem"), key=lambda c: int(c.findtext("start")))
        for c in list(t):
            t.remove(c)
        for c in kids:
            t.append(c)
        sub(t, "enabled", "TRUE")
        sub(t, "locked", "FALSE")
        sub(t, "outputchannelindex", 1 if pos % 2 == 0 else 2)
        audio.insert(list(audio).index(old_tracks[-1]) + 1 + k if old_tracks else len(audio), t)

    # links: each shot's picture with all its audio, as Premiere writes them
    where = {}
    for kind_, tracks in (("video", seq.findall("media/video/track")), ("audio", audio.findall("track"))):
        for ti, t in enumerate(tracks, 1):
            for cx, c in enumerate(t.findall("clipitem"), 1):
                where[c.get("id")] = (kind_, ti, cx)
    for g in groups:
        ids = [c.get("id") for c in g]
        for c in g:                                 # an earlier link of the picture's stays in the group
            for ln in c.findall("link"):
                r = ln.findtext("linkclipref")
                if r and r not in ids and r in where:
                    ids.append(r)
        byid = {c.get("id"): c for c in seq.iter("clipitem")}
        for i in ids:
            c = byid[i]
            for ln in c.findall("link"):
                c.remove(ln)
            for j in ids:
                kind_, ti, cx = where[j]
                ln = sub(c, "link")
                sub(ln, "linkclipref", j)
                sub(ln, "mediatype", kind_)
                sub(ln, "trackindex", ti)
                sub(ln, "clipindex", cx)
                if kind_ == "audio":
                    sub(ln, "groupindex", 1)
    for nm in seq.findall("name"):
        nm.text = (nm.text or "") + " - Synced Audio"
    write_xml(root, out)
    notes = ["%d shots got their camera audio and %d the sound recordist's" % (n_cam, n_rec)]
    if no_sound:
        notes.append("%d clips have no synced sound (%s)" % (len(no_sound), ", ".join(sorted(no_sound)[:6]) +
                                                          (" ..." if len(no_sound) > 6 else "")))
    for what, n in skipped.items():
        notes.append("%d %s left as they were" % (n, what))
    return notes


def convert_main(args):
    """--mode convert --to FORMAT FILE: an edit (XML, FCPXML, .drp or .prproj) as another of those, saved
    next to it. Kickoff reads and writes XML, FCPXML and .prproj itself; a .drp goes through Resolve
    Studio (made from an XML, or read back out as one); to .prproj means an XML the window opens in a
    new Premiere project."""
    src = os.path.abspath(args.paths[0])
    kind, to = convert_kind(src), args.to
    if not kind:
        sys.exit("error: Kickoff can convert a Premiere or Final Cut XML, an FCPXML, a .drp or a .prproj")
    if not to or to == kind:
        sys.exit("error: pick a different format to convert it to")
    d = os.path.dirname(src)
    base = os.path.splitext(os.path.basename(src.rstrip("/")))[0]
    notes, outputs, offline = [], [], 0
    if to == "syncaudio":                        # not a conversion: the same XML with the synced sound added
        if kind != "xml":
            sys.exit("error: Synced Audio works on a Premiere sequence XML (File > Export > Final Cut Pro XML)")
        event("stage", text="Adding the synced audio")
        out = free_path(d, base + " - Synced Audio", ".xml")
        notes = synced_audio(src, out)
        for n in notes:
            log(n)
        log("Wrote %s" % out)
        event("convert", to=to, outputs=[out], notes=notes, offline=0, premiere="", dir=d)
        return
    work = os.path.join(tempfile.gettempdir(), "kickoff-convert", hashlib.md5(src.encode()).hexdigest()[:10])
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    def xml_out():                               # the XML: next to the file when it's what they asked for
        return free_path(d, base, ".xml") if to in ("xml", "prproj") else os.path.join(work, base + ".xml")

    xml = src if kind == "xml" else None
    try:
        if kind == "fcpxml":
            if os.path.isdir(src):
                src = os.path.join(src, "Info.fcpxml")
            elif zipfile.is_zipfile(src):        # a bundle sent zipped (as a download or attachment)
                with zipfile.ZipFile(src) as z:
                    inner = next((n for n in z.namelist() if n.lower().endswith(".fcpxml")
                                  and "__macosx" not in n.lower()), None)
                    if not inner:
                        sys.exit("error: that zip has no FCPXML in it")
                    z.extract(inner, work)
                    src = os.path.join(work, inner)
            event("stage", text="Finding the media")
            fx = os.path.join(work, base + ".fcpxml")
            notes = fcpxml_prep(src, fx, near=d)
            event("stage", text="Reading the Final Cut project")
            xml = xml_out()
            notes += fcpxml_to_xml(fx, xml)
            if to in ("xml", "prproj"):
                outputs.append(xml)
        elif kind == "prproj":
            event("stage", text="Reading the Premiere project")
            xml = xml_out()
            notes = prproj_to_xml(src, xml)
            if to in ("xml", "prproj"):
                outputs.append(xml)
        elif kind == "drp":
            plan = {"kind": "drp", "src": src, "to": "xml", "name": base,
                    "export": {"to": "xml", "dir": d if to in ("xml", "prproj") else work, "base": base}}
            res = run_driver(CONVERT_DRIVER, plan, work)
            got, offline = res.get("outputs", []), res.get("offline", 0)
            for o in got:                        # Resolve adds " (Resolve)" to every sequence name
                tree = ET.parse(o)
                for sq in tree.getroot().iter("sequence"):
                    nm = sq.find("name")
                    if nm is not None and nm.text:
                        nm.text = re.sub(r"( \(Resolve\))+$", "", nm.text)
                write_xml(tree.getroot(), o)
            if to in ("xml", "prproj"):
                outputs = got
            xml = got[0] if got else None
            if to == "fcpxml" and len(got) > 1:  # several timelines: one XML with them all
                merged = ET.Element("xmeml", version="4")
                pr = ET.SubElement(merged, "project")
                ET.SubElement(pr, "name").text = base
                ch = ET.SubElement(pr, "children")
                for o in got:
                    for sq in ET.parse(o).getroot().iter("sequence"):
                        if sq.find("media") is not None:
                            ch.append(sq)
                xml = os.path.join(work, base + " (all).xml")
                write_xml(merged, xml)
        if to == "fcpxml" and xml:
            event("stage", text="Writing the FCPXML")
            out = free_path(d, base, ".fcpxml")
            notes += xml_to_fcpxml(xml, out)
            outputs = [out]
        elif to == "drp" and xml:
            res = resolve_build(xml, drp=free_path(d, base, ".drp"))
            outputs = [res["drp"]] if res.get("drp") else []
            offline = res.get("offline", 0)
        elif kind == "xml" and to == "prproj":
            outputs = [src]
    except (RuntimeError, OSError, ValueError, ET.ParseError, zipfile.BadZipFile) as e:
        sys.exit("error: %s" % e)
    if not outputs:
        sys.exit("error: Resolve didn't write anything. Check that DaVinci Resolve Studio is open, with no window "
                 "waiting on you")
    for n in notes:
        log(n)
    for o in outputs:
        log("Wrote %s" % o)
    event("convert", to=to, outputs=outputs, notes=notes, offline=offline,
          premiere=(outputs[0] if to == "prproj" else ""), dir=d)


def slop_main(args):
    xml = next((p for p in args.paths if p.lower().endswith(".xml")), None)
    if not xml:                         # a folder was picked: the newest XML in it (not a Slop Cut we made)
        d = next((p for p in args.paths if os.path.isdir(p)), None)
        found = [os.path.join(d, f) for f in os.listdir(d) if f.lower().endswith(".xml")
                 and not f.startswith(".") and not f.lower().endswith((" - slop cut.xml", " - resolve.xml"))] if d else []
        xml = max(found, key=os.path.getmtime) if found else None
        if d and not xml:
            sys.exit("error: there's no XML in %s" % os.path.basename(d.rstrip("/")))
    if not xml or not os.path.isfile(xml):
        sys.exit("error: give the project XML to cut a Slop Cut in")
    event("stage", text="Reading the project")
    # the window's Slop Cut options come as extra words: pace:fast genre:punk bpm:180
    opt = dict(p.split(":", 1) for p in args.paths if re.match(r"^(pace|genre|bpm):", p))
    try:
        bpm = float(opt.get("bpm") or 0) or None
    except ValueError:
        bpm = None
    style = slop_style(opt.get("pace", "normal"), opt.get("genre", "auto"), bpm)
    out = None
    if args.mode == "slopimport":       # someone's own XML: the Slop Cut goes in a copy next to it
        stem = re.sub(r"(?i)( - Slop Cut)?\.xml$", "", os.path.abspath(xml))
        out = stem + " - Slop Cut.xml"
    try:
        name, shots, takes, an, looked = slop_cut(os.path.abspath(xml), out_path=out, style=style)
    except (RuntimeError, ET.ParseError, OSError) as e:
        sys.exit("error: %s" % e)
    used = collections.Counter(takes[i]["cam"] for _, _, i in shots if i is not None)
    log("Added %s to %s: %d shots at %.0f BPM, %d of %d takes looked at." % (
        name, os.path.basename(xml), len(shots), an["bpm"], looked, len(takes)))
    xml = out or xml
    event("slop", xml=os.path.abspath(xml), name=name, pace=style["pace"], genre=style["genre"], shots=len(shots), bpm=round(an["bpm"]),
          cameras=dict(used), takes=len({i for _, _, i in shots if i is not None}), looked=looked,
          seen=bool(looked))
    return 0


def main(argv=None):
    # a long sync runs below everything else, so the Mac stays usable while it works (ffmpeg
    # children inherit it)
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    # the window's Settings page (a JSON file) sets the defaults below; flags still win over it
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--settings", default=os.environ.get("KICKOFF_SETTINGS"))
    load_settings(pre.parse_known_args(argv)[0].settings)
    trim_audio_cache()
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
    ap.add_argument("-j", "--jobs", type=int, default=max(1, min(10, os.cpu_count() or 2)))
    ap.add_argument("--no-version-check", action="store_true",
                    help="sync to the song given even when the clips match another version of it better")
    ap.add_argument("--keep-blank", action="store_true",
                    help="keep placed stretches whose picture is a flat color (see drop_blank)")
    ap.add_argument("--no-cache", action="store_true",
                    help="don't keep decoded camera audio for the next run (see audio_cache_dir)")
    ap.add_argument("--to", choices=["xml", "fcpxml", "drp", "prproj", "syncaudio"],
                    help="--mode convert: the format to turn the file into")
    ap.add_argument("--mode", choices=["auto", "music", "setup", "narrative", "cameras", "slop", "slopimport", "resolve", "convert", "convert-xml", "convert-fcpxml",
                             "convert-drp", "convert-prproj", "convert-syncaudio"], default="auto",
                    help="music: sync to the song (music video); narrative: sync each clip to the sound "
                         "recordist's audio files (timecode, else scratch audio) in one Sync sequence; cameras: "
                         "narrative with no sound files, the cameras synced to each other; setup: "
                         "bins, Breakups and an empty Edit sequence only (commercials); auto (default): music "
                         "when a song is found and clips line up with it; slop: add a Slop Cut (a rough edit) to "
                         "a finished project XML, given as the path")
    ap.add_argument("--sync-by", choices=["auto", "timecode", "audio"], default=None,
                    help="auto (default): timecode when the clip and the audio both carry it, else the "
                         "sound; audio: ignore timecode; timecode: only timecode")
    ap.add_argument("--rebuild", action="store_true",
                    help="build the whole project again even if this folder was run before (by default a "
                         "second run only adds the cards that are new since then)")
    ap.add_argument("--settings", metavar="JSON",
                    help="the Kickoff window's settings file (bins, sequence size and names, label colors...)")
    ap.add_argument("--events", action="store_true", help=argparse.SUPPRESS)   # for the Kickoff window
    ap.set_defaults(sync_size=SETTINGS["sync_size"], track_order=SETTINGS["track_order"],
                    place_repeats=SETTINGS["place_repeats"])
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    if args.no_cache:
        os.environ["KICKOFF_NO_CACHE"] = "1"
    args.cams_together = args.mode == "cameras"
    if args.cams_together:
        args.mode = "narrative"
    if args.sync_by is None:          # the Settings page's choice for this kind of project
        args.sync_by = ("auto" if SETTINGS["timecode_narrative"] else "audio") if args.mode == "narrative" \
            else ("timecode" if SETTINGS["timecode_music"] else "audio")
    global EVENTS
    EVENTS = args.events
    if args.mode in ("slop", "slopimport"):
        return slop_main(args)
    if args.mode == "resolve":
        return resolve_main(args)
    if args.mode.startswith("convert"):          # the window passes convert-<format> (its runner takes a mode only)
        if "-" in args.mode:
            args.to = args.mode.split("-", 1)[1]
        return convert_main(args)
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
    narr_audio = []                                    # narrative: every loose audio file is recorded sound
    for p in args.paths:
        if os.path.isdir(p):
            continue
        if not os.path.isfile(p):
            sys.exit("error: %s is not a folder or a file" % p)
        ext = os.path.splitext(p)[1].lower()
        if ext in AUDIO_EXT and args.mode == "narrative":
            narr_audio.append(os.path.abspath(p))
        elif ext in AUDIO_EXT and not args.master:
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
    state, restrict = (None, None) if args.rebuild or args.mode == "narrative" else load_state(args, out_given)
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
    if args.mode == "narrative":
        # the recorded sound: loose files dropped, and audio in the folders (not Music or SFX)
        def not_music(p):
            return not any(x.lower() in MUSIC_DIRS or x.lower() in ("sfx", "sound effects", "sound fx")
                           for x in os.path.relpath(p, args.clips).replace("\\", "/").split("/")[:-1])
        os.makedirs(args.out, exist_ok=True)
        return narrative(args, project_name, sorted(set(narr_audio + [p for p in audio_files if not_music(p)])))
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
    # A Music folder often holds several mixes of the song, and camera audio only lines up with the
    # one played on set. Let a few clips vote; if they clearly match another version, sync to that
    # one. When the two versions line up with each other, the chosen song still goes on the timeline
    # and every clip is shifted onto it; otherwise the matching version replaces it.
    song_note, sync_song, shift = "", args.master, None
    if args.master and not args.no_version_check:
        # only mixes that line up with the song all the way through count: a same-length file in the
        # Music folder can be another song altogether
        shifts = {p: song_shift(args.master, p) for p in song_versions(args.master, all_audio)}
        for p, sh in shifts.items():
            if sh is None:
                log("Not another version of the song (doesn't line up with it): %s" % os.path.basename(p))
        others = [p for p, sh in shifts.items() if sh is not None]
        if others:
            event("stage", text="Checking which version of the song the cameras match")
            log("Other versions of the song: %s" % ", ".join(os.path.basename(p) for p in others))
            probe_clips = [c for c in find_clips(args.clips, None, [args.out]) if inside(c.path, args.only)]
            won = vote_song([args.master] + others, probe_clips) if probe_clips else None
            if won:
                log("Clips per version: %s" % ", ".join("%s %d" % (os.path.basename(p), k) for p, k in won[1].items()))
            if won and won[0] != args.master and won[1][won[0]] >= 2 and \
                    won[1][won[0]] >= 2 * won[1].get(args.master, 0):
                sync_song = won[0]
                shift = shifts[sync_song]
                a, b = os.path.basename(args.master), os.path.basename(sync_song)
                song_note = ("The cameras were shot to %s, not %s. Synced to it and placed on %s, "
                             "which lines up with it." % (b, a, a))
                log(song_note)
    args.song_note = song_note

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
        log("Indexing master %s" % sync_song)
        try:
            master = MasterIndex(load_audio(sync_song))
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

    if master is not None and args.sync_by in ("timecode", "auto"):
        # the song file's timecode start, and each clip's: a clip whose timecode falls in the song
        # goes there without listening (only when asked: a song's timecode is often meaningless)
        song_tc = audio_timecode(args.master)[0]
        if song_tc is None:
            log("The song file carries no timecode, so every clip syncs by its sound")
        else:
            for c in clips:
                ts = tc_seconds(c.timecode, c.fps)
                if ts is not None and ts + (c.duration or 0) > song_tc and ts < song_tc + master.duration:
                    c.offset, c.status, c.how, c.confidence = ts - song_tc, "placed", "timecode", 100.0
                    c.notes.append("by timecode %s (song starts at %s)" % (c.timecode, "%02d:%02d:%02d" % (song_tc // 3600, song_tc // 60 % 60, song_tc % 60)))
            log("%d clips placed by timecode" % sum(getattr(c, "how", "") == "timecode" for c in clips))
    if master is not None:
        event("start", project=project_name, folder=args.clips, song=os.path.basename(args.master),
              song_duration=round(master.duration, 2), clips=len(clips), version=VERSION, mode="music",
              cams=collections.Counter(clip_cam(c) for c in clips))
        match_all(clips, master, args)
        if shift is not None:
            move_to_song(clips, shift)
            master = MasterIndex(load_audio(args.master))
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
                            check=c.check, phase=c.phase, repeat_alt=c.repeat_alt)]
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
              os.path.join(args.xml_out, proj_file), resolve=True)
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
    event("done", project=project_name, edit_name=seq_name("edit", project=project_name), out=args.out, xml=os.path.join(args.xml_out, proj_file),
          report=report, song_duration=round(master.duration, 2) if master is not None else 0,
          mode=args.mode, clips=len(clips), synced=len(placed), cameras=cam_events,
          set_aside=[dict(reason=r, count=n) for r, n in collections.Counter(aside).most_common()],
          audio=sum(len(v) for v in audio_bins.values()),
          unreadable=sum(1 for c in clips if not c.readable),
          restarted=sum(1 for c in clips if c.split),
          check_chorus=sum(1 for c in clips for p in c.parts if p.repeat_alt is not None),
          worth_a_look=len({id(c) for c, _, _ in doubtful(clips)}),   # clips, not parts
          song_note=song_note,
          lists=clip_lists(clips, master))


def move_to_song(clips, shift):
    """Offsets found on one version of the song, moved onto another that sits `shift` s earlier."""
    for c in clips:
        for obj in [c] + (c.parts if c.split else []):
            for k in ("offset", "repeat_alt", "runner_up_offset"):
                if getattr(obj, k, None) is not None:
                    setattr(obj, k, getattr(obj, k) - shift)


def clip_lists(clips, master):
    """For the window's results: which clips each summary line counts ({file, cam, at}, `at` the song
    second the clip lands at), so Jake can see them and copy their names."""
    def item(c, p=None):
        at = None
        if p is not None and p.status == "placed":
            at = round(p.offset + p.src_in * c.speed, 1)
        elif song_spans(c):
            at = round(min(sp[0] for sp in song_spans(c)), 1)
        d = dict(file=os.path.basename(c.path), cam=clip_cam(c), at=at)
        if c.status != "placed" and c.guess is not None:
            d["guess"] = round(c.guess, 1)
        return d
    if master is None:
        return {}
    look, seen = [], set()
    for c, _, p in doubtful(clips):
        if id(c) not in seen:
            seen.add(id(c))
            look.append(item(c, p))
    aside = collections.defaultdict(list)
    for c in clips:
        if c.status != "placed":
            aside[c.reasons[0] if c.reasons else REASON_NO_MATCH].append(item(c))
    chorus = [item(c, p) for c in clips for p in c.parts if p.repeat_alt is not None]
    def plays(c):                      # passes of the song; the stretches between them aren't plays
        return [p for p in c.parts if p.reason != REASON_BETWEEN]
    return dict(worth_a_look=look, restarted=[dict(item(c), passes=len(plays(c)),
                                                   placed=sum(p.status == "placed" for p in plays(c)))
                                              for c in clips if c.split], check_chorus=chorus,
                aside=dict(aside))


def clip_cam(c):
    """The camera a clip is filed under while syncing, for the window: "A" from an "A Cam (...)"
    folder (or the card's reel letter), else its top folder."""
    return camera_letter_hint(c) or c.top_folder or ""


def song_spans(c):
    """[start, end] in song seconds of each placed stretch of a clip, for the window's song map."""
    if c.status != "placed":
        return []
    if c.split:
        return [[round(p.offset + p.src_in * c.speed, 2), round(p.offset + p.src_out * c.speed, 2)]
                for p in c.parts if p.status == "placed"]
    return [[round(c.offset, 2), round(c.offset + (c.duration or 0) * c.speed, 2)]] if c.offset is not None else []


def match_all(clips, master, args):
    """Sync every clip to the song (in parallel), logging and reporting each as it finishes."""
    st = Settings(threshold=args.threshold, min_hashes=args.min_landmarks, max_fps=args.max_fps,
                  place_repeats=args.place_repeats)
    log("Matching")
    done, placed, tried, give_up = 0, 0, 0, []

    def work(c):
        if give_up:                      # auto mode, and nothing lines up with the "song": stop early
            return c
        if getattr(c, "how", "") == "timecode":      # already placed by timecode
            return c
        try:
            sync_clip(c, master, st)
            if not getattr(args, "keep_blank", False):
                drop_blank(c)
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
                  passes=sum(p.reason != REASON_BETWEEN for p in c.parts) if c.split else 1, reason=c.reasons[0] if c.reasons else "",
                  cam=clip_cam(c), spans=song_spans(c))
            placed += c.status == "placed"
            tried += c.status == "placed" or (c.reasons[:1] in ([REASON_NO_MATCH], [REASON_LOW_CONF]))
            if args.mode == "auto" and not getattr(args, "song_certain", True) and not placed \
                    and tried >= 12 and not give_up:
                log("12 clips with sound and none lines up with the song: not a music video shoot")
                give_up.append(True)


NARR_NO_MATCH = "no match to any audio file"
NARR_DEEP_GROUPS = 2    # sound groups a clip the landmarks couldn't place gets the slow waveform search in
NARR_GROUP_S = 1800.0   # sound files are searched in groups of at most this much audio (memory stays bounded)
MEMORY_LIMIT = 0.4      # stop before the engine holds more than this share of the Mac's memory
SWAP_LIMIT_GB = 6.0     # ... or pushes this much more of the Mac's memory out to disk


class MemoryGuard(Exception):
    pass


_mem = {"t": 0.0, "swap0": None}


def _swap_used_gb():
    if sys.platform != "darwin":
        return None
    r = run(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)([MG])", (r.stdout or b"").decode(errors="replace"))
    return (float(m.group(1)) / (1024 if m.group(2) == "M" else 1)) if m else None


def memory_check():
    """Stop with a clear message before a long run could squeeze the Mac: the engine's own peak
    memory past MEMORY_LIMIT of the machine, or the Mac swapping SWAP_LIMIT_GB more than at the start."""
    now = time.time()
    if now - _mem["t"] < 5:
        return
    _mem["t"] = now
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ImportError, ValueError, OSError, AttributeError):
        peak, total = 0, 0
    swap = _swap_used_gb()
    if _mem["swap0"] is None:
        _mem["swap0"] = swap
    if total and peak > MEMORY_LIMIT * total:
        raise MemoryGuard("Kickoff stopped itself: it needed more memory than is safe on this Mac (%.0f of %.0f GB). "
                          "Try fewer sound files or one scene folder at a time." % (peak / 2**30, total / 2**30))
    if swap is not None and _mem["swap0"] is not None and swap - _mem["swap0"] > SWAP_LIMIT_GB:
        raise MemoryGuard("Kickoff stopped itself: the Mac was running out of memory (%.0f GB pushed to disk). "
                          "Try fewer sound files or one scene folder at a time." % (swap - _mem["swap0"]))
NARR_GAP_S = 2.0        # silence between audio files while listening, and between them on the timeline


def tc_seconds(tc, fps):
    """A timecode label ("01:02:03:04", drop frame ';' too) as seconds of labels: frames count at the
    nominal rate (23.976 counts 24), which is how a recorder's timecode reads too."""
    m = re.match(r"^(\d+)[:;.](\d+)[:;.](\d+)[:;.](\d+)$", (tc or "").strip())
    if not m or not fps:
        return None
    h, mi, s, f = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s + f / max(1, round(fps))


def audio_timecode(path):
    """(start in seconds of timecode labels, duration, channels, rate) of an audio file. A BWF
    recorder file carries its start as time_reference (samples since midnight); some files carry a
    timecode tag instead. None when there's neither."""
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
    info = json.loads(r.stdout or b"{}") if r.returncode == 0 else {}
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {})
    rate = int(a.get("sample_rate") or 48000)
    tags = {k.lower(): v for k, v in ((info.get("format") or {}).get("tags") or {}).items()}
    tags.update({k.lower(): v for k, v in (a.get("tags") or {}).items()})
    start = None
    if str(tags.get("time_reference", "")).strip().isdigit():
        start = int(tags["time_reference"]) / rate
    elif tags.get("timecode"):
        start = tc_seconds(tags["timecode"], 30)
    return start, float((info.get("format") or {}).get("duration") or 0), int(a.get("channels") or 2), rate


def drop_audio_copies(paths, info):
    """The same recording copied to a second folder (a sound dump next to the scene folders) counts
    once, or each clip would match two identical files: same name and size, or (renamed) the same
    recorder timecode, length and channel count. The first by path is kept."""
    seen, out, oinfo, dropped = set(), [], [], 0
    for p, i in sorted(zip(paths, info), key=lambda pi: pi[0]):
        try:
            keys = [("name", os.path.basename(p).lower(), os.path.getsize(p))]
        except OSError:
            keys = [("path", p)]
        if i[0] is not None and i[1] > 0:
            keys.append(("tc", round(i[0], 3), round(i[1], 2), i[2]))
        if any(k in seen for k in keys):
            dropped += 1
            continue
        seen.update(keys)
        out.append(p)
        oinfo.append(i)
    if dropped:
        log("Skipped %d audio files that are copies of others" % dropped)
    return out, oinfo


COPY_LISTEN_S = 30.0      # seconds from the start of two same-length recordings compared to tell a copy


def audio_head(path, secs=COPY_LISTEN_S):
    """The first seconds of a recording, mixed to mono (None if it can't be read)."""
    try:
        chans = _decode_channels(path, [probe_audio(path)[1]], limit=secs)
        if chans:
            return np.mean([c for _, c in chans], axis=0).astype(np.float32)
    except Exception:
        pass
    try:
        return read_wav_mono(path)[:int(secs * SR)]
    except Exception:
        return None


def same_sound(a, b, max_lag_s=1.0):
    """True when two stretches of sound are the same recording (a renamed or remixed copy): their
    best alignment within a second correlates almost perfectly."""
    n = min(len(a), len(b))
    if n < SR * 5:
        return False
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    ea, eb = float(np.sqrt(np.sum(a * a))), float(np.sqrt(np.sum(b * b)))
    if ea < 1e-3 or eb < 1e-3:                    # silence matches anything: never a copy by that
        return False
    m = 1 << int(np.ceil(np.log2(2 * n)))
    xc = np.fft.irfft(np.fft.rfft(a, m) * np.conj(np.fft.rfft(b, m)), m)
    lag = int(max_lag_s * SR)
    peak = max(np.max(xc[:lag + 1]), np.max(xc[-lag:]))
    return peak / (ea * eb) >= 0.95


def drop_sound_copies(paths, info):
    """Copies the names and timecode don't give away (a sound dump renamed and re-written): two
    recordings of the same length whose openings are the same sound. The one with more channels
    (the recorder's own poly file), else the first by path, is kept."""
    keep = [True] * len(paths)
    heads = {}
    order = sorted(range(len(paths)), key=lambda i: (-info[i][2], paths[i]))
    for x, i in enumerate(order):
        if not keep[i] or info[i][1] <= 0:
            continue
        for j in order[x + 1:]:
            if not keep[j] or abs(info[j][1] - info[i][1]) > 1.0:
                continue
            for k in (i, j):
                if k not in heads:
                    heads[k] = audio_head(paths[k])
            if heads[i] is not None and heads[j] is not None and same_sound(heads[i], heads[j]):
                keep[j] = False
    dropped = keep.count(False)
    if dropped:
        log("Skipped %d audio files with the same sound as others" % dropped)
    return [p for p, k in zip(paths, keep) if k], [i for i, k in zip(info, keep) if k]


def narrative(args, project_name, audio_paths):
    """Narrative: every clip synced to the recorder's audio file it was shot with, by timecode when
    both have it, else by the camera's scratch audio. One Sync sequence: the audio files end to end in
    order on A1, each clip over its sound (A Cam on the lowest video tracks, other cameras above), and
    nothing trimmed or moved over another file's sound. Breakups hold the camera clips as shot."""
    cams_only = getattr(args, "cams_together", False) and not audio_paths
    if not audio_paths and not cams_only:
        sys.exit("error: add the sound recordist's audio files (or their folder) to sync the footage to")
    clips = [c for c in find_clips(args.clips, None, [args.out]) if inside(c.path, args.only)]
    if not clips:
        sys.exit("error: no video files found in %s" % ", ".join(args.only or [args.clips]))
    event("stage", text="Reading %d clips and %d audio files" % (len(clips), len(audio_paths)))
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(probe, clips))
    # a video-type file with no picture (an .mp4 exported from an edit: score, ambience) is neither a
    # camera nor the recordist's sound: left out
    sound = [c for c in clips if c.readable and not c.fps and c.audio_layout]
    if sound:
        log("Left out %d sound-only video files (no picture): %s" % (len(sound), ", ".join(c.rel for c in sound[:20])))
        clips = [c for c in clips if c not in sound]
    with cf.ThreadPoolExecutor(args.jobs) as ex:
        info = list(ex.map(audio_timecode, audio_paths))
    audio_paths, info = drop_audio_copies(audio_paths, info)
    audio_paths, info = drop_sound_copies(audio_paths, info)
    files = [dict(path=p, tc=i[0], dur=i[1], ch=i[2], rate=i[3]) for p, i in zip(audio_paths, info) if i[1] > 0]
    if cams_only:
        return narrative_cameras(args, project_name, clips)
    # in the order they were recorded: by timecode when every file has it, else by name
    if files and all(f["tc"] is not None for f in files):
        files.sort(key=lambda f: (f["tc"], f["path"]))
    else:
        files.sort(key=lambda f: os.path.basename(f["path"]).lower())
    log("Audio files, in order: %s" % ", ".join(os.path.basename(f["path"]) for f in files))

    # listening: every file mixed to mono, end to end with a little silence between. The files are
    # matched in groups of at most NARR_GROUP_S (each group indexed, searched, then let go), so a
    # long shoot never holds hours of sound and its index in memory at once
    pos, groups = 0.0, []
    for f in files:
        f["at"] = pos                                  # where it starts in what the clips are matched to
        pos += f["dur"] + NARR_GAP_S
        if not groups or (f["at"] - groups[-1][0]["at"]) + f["dur"] > NARR_GROUP_S:
            groups.append([])
        groups[-1].append(f)
    total = pos

    def group_index(g):
        chunks = []
        for f in g:
            try:
                x = load_audio(f["path"])
            except RuntimeError as e:
                log("  can't read %s: %s" % (os.path.basename(f["path"]), e))
                x = np.zeros(int(f["dur"] * SR), np.float32)
            n = int(round(f["dur"] * SR))              # exactly its length, so later files stay where "at" says
            x = x[:n] if len(x) >= n else np.concatenate([x, np.zeros(n - len(x), np.float32)])
            chunks += [x, np.zeros(int(NARR_GAP_S * SR), np.float32)]
            memory_check()
        return MasterIndex(np.concatenate(chunks))

    def file_at(a, b):                                 # the file a stretch [a, b] overlaps most
        best = max(files, key=lambda f: min(b, f["at"] + f["dur"]) - max(a, f["at"]))
        return best if min(b, best["at"] + best["dur"]) > max(a, best["at"]) else None

    event("start", project=project_name, folder=args.clips, song="%d audio files" % len(files),
          song_duration=round(total, 2), clips=len(clips), version=VERSION, mode="narrative",
          cams=collections.Counter(clip_cam(c) for c in clips))
    st = Settings(threshold=args.threshold, min_hashes=args.min_landmarks, max_fps=args.max_fps,
                  place_repeats=False)

    def work(c):
        c.how = ""
        # timecode first: the clip's start label inside one file's labels
        ts = tc_seconds(c.timecode, c.fps) if args.sync_by != "audio" else None
        if ts is not None:
            # the file whose timecode span the clip's overlaps most
            te = ts + (c.duration or 0) * c.speed
            ov = [(min(te, f["tc"] + f["dur"]) - max(ts, f["tc"]), i) for i, f in enumerate(files) if f["tc"] is not None]
            if ov and max(ov)[0] > 0:
                f = files[max(ov)[1]]
                c.offset, c.status, c.how = f["at"] + ts - f["tc"], "placed", "timecode"
                c.confidence = 100.0
                c.notes.append("by timecode %s in %s" % (c.timecode, os.path.basename(f["path"])))
                return c
        if not c.readable:
            c.reasons.append(REASON_UNREADABLE)
            return c
        if args.sync_by == "timecode":
            c.reasons.append("timecode outside every audio file" if ts is not None else "no timecode")
            return c
        return c                    # by scratch audio: searched group by group below

    # timecode first for every clip; then the rest by scratch audio, one group of sound files at a time
    indexes = {}

    def by_tc(c):
        c.how = ""
        ts = tc_seconds(c.timecode, c.fps) if args.sync_by != "audio" else None
        if ts is not None:
            te = ts + (c.duration or 0) * c.speed
            ov = [(min(te, f["tc"] + f["dur"]) - max(ts, f["tc"]), i) for i, f in enumerate(files) if f["tc"] is not None]
            if ov and max(ov)[0] > 0:
                return True
        return False
    need_audio = [c for c in clips if c.readable and not by_tc(c)] if args.sync_by != "timecode" else []
    if need_audio and groups:
        log("Listening to %d audio files in %d group%s" % (len(files), len(groups), "s" if len(groups) > 1 else ""))
    done = 0

    def results():
        # clips needing no audio search come straight back; the rest once every group was searched
        quick = [c for c in clips if c not in need_audio]
        for c in quick:
            yield work(c)
        if not need_audio:
            return
        # first a quick look (landmarks only) in every group; then the slow waveform search, only
        # for the clips still unplaced and only in the two groups whose landmarks came closest
        # (searching all of them that way was most of an 8-hour run)
        for gi, g in enumerate(groups):
            event("stage", text="Listening to the audio files (%d of %d)" % (gi + 1, len(groups))
                  if len(groups) > 1 else "Listening to the audio files")
            searched[0] = 0
            indexes.clear()
            indexes[gi] = group_index(g)
            with cf.ThreadPoolExecutor(args.jobs) as ex:
                list(ex.map(lambda c: search_group(c, gi, st_quick, need_audio), need_audio))
            indexes.clear()
        closer = collections.defaultdict(list)
        for c in need_audio:
            b = tries.get(id(c))
            if b is not None and b.status == "placed":
                continue
            near = sorted((sc for sc in scores.get(id(c), {}).items() if sc[1][0] > 0), key=lambda sc: sc[1], reverse=True)
            for gi, _ in near[:NARR_DEEP_GROUPS]:
                closer[gi].append(c)
        for k, gi in enumerate(sorted(closer)):
            event("stage", text="Taking a closer listen (%d of %d)" % (k + 1, len(closer)))
            searched[0] = 0
            indexes.clear()
            indexes[gi] = group_index(groups[gi])
            with cf.ThreadPoolExecutor(args.jobs) as ex:
                list(ex.map(lambda c: search_group(c, gi, st, closer[gi]), closer[gi]))
            indexes.clear()
        for c in need_audio:
            yield finish(c)

    tries, scores, searched, lock = {}, {}, [0], threading.Lock()
    st_quick = copy.copy(st)
    st_quick.deep = False

    def search_group(c, gi, how, among):
        memory_check()
        with lock:
            searched[0] += 1
            n = searched[0]
        if n % 5 == 0 or n == len(among):
            event("stage", text="Matching clips to the sound%s: %d of %d" % (
                " (part %d of %d)" % (gi + 1, len(groups)) if len(groups) > 1 else "", n, len(among)))
        t = copy.copy(c)
        t.reasons, t.notes, t.parts = [], [], []
        try:
            sync_clip(t, indexes[gi], how)
        except MemoryError:
            raise
        except Exception as e:
            t.reasons.append(REASON_UNREADABLE)
            t.notes.append("error: %s" % e)
        if t.status == "placed":
            g0 = groups[gi][0]["at"]
            t.offset += g0
            for p in t.parts:
                if getattr(p, "offset", None) is not None:
                    p.offset += g0
        with lock:
            scores.setdefault(id(c), {})[gi] = (t.aligned or 0, t.confidence or 0)
        b = tries.get(id(c))
        if b is None or (t.status == "placed" and (b.status != "placed" or (t.confidence or 0) > (b.confidence or 0))):
            tries[id(c)] = t

    def finish(c):
        best = tries.get(id(c))
        if best is not None:
            notes = c.notes
            c.__dict__.update(best.__dict__)
            c.notes = notes + best.notes
        if c.status == "placed" and c.split:
            p = max((p for p in c.parts if p.status == "placed"), key=lambda p: p.src_out - p.src_in)
            c.offset, c.confidence = p.offset, p.confidence
            c.notes.append("placed whole by its longest matching stretch (%d stretches heard)" % len(c.parts))
            c.split, c.parts = False, []
        if c.status == "placed":
            c.how = "scratch audio"
        return c

    try:
        found = list(results())
    except MemoryGuard as e:
        sys.exit("error: %s" % e)
    for c in found:
        done += 1
        if c.status == "placed" and file_at(c.offset, c.offset + c.duration * c.speed) is None:
            c.status, c.reasons = "", ["lines up with no audio file"]
        if c.status != "placed":
            c.status = "not placed"
            c.reasons = [NARR_NO_MATCH if r in (REASON_NO_MATCH, REASON_LOW_CONF) else r for r in c.reasons] \
                or [NARR_NO_MATCH]
        log("  [%d/%d] %-40s %s" % (done, len(clips), c.rel, ("%.3fs by %s" % (c.offset, c.how))
                                    if c.status == "placed" else "-- " + "; ".join(c.reasons)))
        af = file_at(c.offset, c.offset + c.duration * c.speed) if c.status == "placed" else None
        event("clip", done=done, total=len(clips), file=c.rel, status=c.status, passes=1,
              reason=c.reasons[0] if c.reasons else "", cam=clip_cam(c),
              audio=os.path.basename(af["path"]) if af else "", by=c.how if af else "",
              spans=[[round(c.offset, 2), round(c.offset + c.duration * c.speed, 2)]] if c.status == "placed" else [])

    # the timeline: each file after the one before, with room for clips that roll before it starts
    # or after it ends, so no clip reaches over another file's sound
    placed = [c for c in clips if c.status == "placed"]
    for c in placed:
        c.afile = file_at(c.offset, c.offset + c.duration * c.speed)
    seq_fps = args.fps or (collections.Counter(c.fps for c in placed if c.fps).most_common(1) or
                           collections.Counter(c.fps for c in clips if c.fps).most_common(1) or [(24.0, 0)])[0][0]
    t = 0.0
    for f in files:
        mine = [c for c in placed if c.afile is f]
        head = max([0.0] + [f["at"] - c.offset for c in mine])
        tail = max([0.0] + [c.offset + c.duration * c.speed - (f["at"] + f["dur"]) for c in mine])
        f["pos"] = t + head
        t = f["pos"] + f["dur"] + tail + NARR_GAP_S
    for c in placed:
        c.tl = c.afile["pos"] + (c.offset - c.afile["at"])      # timeline seconds of its first frame
        c.in_file = c.offset - c.afile["at"]                     # ... and into its audio file

    labels = assign_cameras(clips, args.group_by)
    cams = []
    for key, letter in sorted(labels.items(), key=lambda kv: kv[1]):
        cams.append((letter, sorted([c for c in clips if c.camera_key == key], key=lambda c: c.rel)))
    # video tracks: A Cam's clips on the fewest tracks from V1 up (by time), then B Cam's above...
    fr = lambda s: int(round(s * seq_fps))
    base, entries = 0, []
    n_audio = 1
    for f in files:
        f["media"] = Media(f["path"], seq_fps, f["dur"], has_video=False, channels=f["ch"], rate=f["rate"])
        entries.append(dict(media=f["media"], start=fr(f["pos"]), vtrack=None, atrack=1))
    for letter, cl in cams:
        label = camera_label(letter)
        ends = []
        for c in sorted([c for c in cl if c.status == "placed" and c.width], key=lambda c: (c.tl, c.rel)):
            a, b = fr(c.tl), fr(c.tl) + fr(c.duration * c.speed)
            k = next((k for k, e in enumerate(ends) if e <= a), None)
            if k is None:
                k = len(ends)
                ends.append(b)
            ends[k] = b
            c.track = base + k + 1
            entries.append(dict(media=Media.of_clip(c, seq_fps), start=a, vtrack=c.track, atrack=n_audio + c.track,
                                aenabled=True, label=label, speed=c.speed))
        base += len(ends)
    # clips that didn't sync: back to back on V1 a minute after the last file, named with why
    pos = fr(t + UNSYNCED_GAP_S)
    for letter, cl in cams:
        for c in cl:
            if c.status == "placed" or not (c.readable and c.fps and c.width):
                continue
            why = re.split(r"\s*[(;]", (c.reasons or ["not synced"])[0])[0].strip()
            entries.append(dict(media=Media.of_clip(c, seq_fps), start=pos, vtrack=1, atrack=2, all_audio="raw",
                                label=camera_label(letter), tail=True, name="%s (%s)" % (os.path.basename(c.path), why)))
            pos += fr(c.duration)

    audio_bins = collections.defaultdict(list)
    for f in files:
        audio_bins["Captured"].append(f["media"])
    sync_name = "%s_Sync" % project_name
    args.narrative = dict(name=sync_name, entries=entries)
    event("stage", text="Writing the Premiere project")
    proj_file = re.sub(r"[^\w .-]+", "_", project_name) + ".xml"
    write_xml(build_project(project_name, clips, cams, seq_fps, 0.0, None, audio_bins, args),
              os.path.join(args.xml_out, proj_file), resolve=True)
    log("Wrote %s" % os.path.join(args.xml_out, proj_file))

    # the report: which file each clip went with, how, and where
    report = os.path.join(args.out, "narrative_report.csv")
    with open(report, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "camera", "status", "synced_by", "audio_file", "starts_into_audio_s",
                    "timeline_timecode", "reason", "audio_path"])
        for letter, cl in cams:
            for c in cl:
                ok = c.status == "placed"
                w.writerow([c.rel, "%s Cam" % letter, c.status, c.how if ok else "",
                            os.path.basename(c.afile["path"]) if ok else "", "%.3f" % c.in_file if ok else "",
                            fmt_frames(start_frames(seq_fps) + fr(c.tl), seq_fps) if ok else "",
                            "" if ok else "; ".join(c.reasons),
                            os.path.relpath(c.afile["path"], args.clips) if ok else ""])
    log("Wrote %s. Synced %d of %d clips (%d by timecode)." % (
        os.path.basename(report), len(placed), len(clips), sum(c.how == "timecode" for c in placed)))
    aside = collections.Counter(c.reasons[0] if c.reasons else REASON_NO_MATCH for c in clips if c.status != "placed")
    lists = dict(aside={}, worth_a_look=[], restarted=[], check_chorus=[])
    for c in clips:
        if c.status != "placed":
            lists["aside"].setdefault(c.reasons[0] if c.reasons else REASON_NO_MATCH, []).append(
                dict(file=os.path.basename(c.path), cam=clip_cam(c), at=None))
    event("done", project=project_name, edit_name=seq_name("edit", project=project_name), out=args.out,
          xml=os.path.join(args.xml_out, proj_file), report=report, song_duration=round(t, 2), mode="narrative",
          clips=len(clips), synced=len(placed), sync_name=sync_name, audio=len(files),
          by_timecode=sum(c.how == "timecode" for c in placed),
          cameras=[dict(letter=l, name=cam_bin_name(l, cl), label=camera_label(l), clips=len(cl),
                        synced=sum(c.status == "placed" for c in cl),
                        spans=sorted([round(c.tl, 2), round(c.tl + c.duration * c.speed, 2)]
                                     for c in cl if c.status == "placed")) for l, cl in cams],
          set_aside=[dict(reason=r, count=k) for r, k in aside.most_common()],
          unreadable=sum(1 for c in clips if not c.readable), restarted=0, check_chorus=0, worth_a_look=0,
          lists=lists)


def narrative_cameras(args, project_name, clips):
    """Narrative with no sound files: the cameras synced to each other. A Cam's clips are the takes;
    every other camera's clip is lined up with the take it was shot with, by timecode when both carry
    it, else by scratch audio. A clip that lines up with no A Cam take starts a take of its own (B Cam
    leads when A didn't roll), and the next camera's clips are tried against those too. One Sync
    sequence: the takes in shooting order, A Cam on V1, B Cam above, nothing trimmed; clips that can't
    be read go back to back on V1 after everything, in file order."""
    labels = assign_cameras(clips, args.group_by)
    cams = []
    for key, letter in sorted(labels.items(), key=lambda kv: kv[1]):
        cams.append((letter, sorted([c for c in clips if c.camera_key == key], key=lambda c: c.rel)))
    letter_of = {c.path: l for l, cl in cams for c in cl}
    event("start", project=project_name, folder=args.clips, song="the cameras", song_duration=0,
          clips=len(clips), version=VERSION, mode="narrative",
          cams=collections.Counter(clip_cam(c) for c in clips))
    st = Settings(threshold=args.threshold, min_hashes=args.min_landmarks, max_fps=args.max_fps,
                  place_repeats=False)
    usable = [c for c in clips if c.readable and c.width]
    for c in clips:
        c.how, c.afile = "", None
        if c not in usable:
            c.status, c.reasons = "not placed", c.reasons or [REASON_UNREADABLE]
    files, chunks, pos = [], [], 0.0
    master = None
    done = 0

    def report(c):
        nonlocal done
        done += 1
        f = c.afile
        event("clip", done=done, total=len(clips), file=c.rel, status=c.status, passes=1,
              reason=c.reasons[0] if c.reasons else "", cam=clip_cam(c),
              audio=(os.path.basename(f["clip"].path) if f and f["clip"] is not c else ""),
              by=c.how if f and f["clip"] is not c else "", spans=[])

    def file_at(a, b):
        best = max(files, key=lambda f: min(b, f["at"] + f["dur"]) - max(a, f["at"]))
        return best if min(b, best["at"] + best["dur"]) > max(a, best["at"]) else None

    def take_of(c):
        """The take a clip placed by its sound belongs to. A clip can hang over the take before or after
        its own in what it was matched against, so of the takes it overlaps, the one whose sound it
        really matches (normalised correlation over the overlap) wins."""
        a, b = c.offset, c.offset + c.duration * c.speed
        cand = [f for f in files if min(b, f["at"] + f["dur"]) - max(a, f["at"]) > 0.5]
        if len(cand) < 2 or c.how != "scratch audio":
            return file_at(a, b)
        try:
            y = load_audio(c.audio_src or c.path)
        except RuntimeError:
            return file_at(a, b)

        def score(f):
            lo, hi = max(a, f["at"]), min(b, f["at"] + f["dur"])
            u = f["x"][int((lo - f["at"]) * SR):int((hi - f["at"]) * SR)]
            v = y[int((lo - a) / c.speed * SR):][:len(u)]
            n = min(len(u), len(v))
            if n < SR // 2:
                return 0.0
            u, v = u[:n] - u[:n].mean(), v[:n] - v[:n].mean()
            return float(abs(u @ v) / (np.linalg.norm(u) * np.linalg.norm(v) + 1e-9))
        return max(cand, key=score)

    def work(c):
        c.reasons, c.notes = [], [n for n in c.notes if not n.startswith("by timecode")]
        ts = tc_seconds(c.timecode, c.fps) if args.sync_by != "audio" else None
        if ts is not None:
            te = ts + (c.duration or 0) * c.speed
            ov = [(min(te, f["tc"] + f["dur"]) - max(ts, f["tc"]), i) for i, f in enumerate(files) if f["tc"] is not None]
            if ov and max(ov)[0] > 0:
                f = files[max(ov)[1]]
                c.offset, c.status, c.how, c.confidence = f["at"] + ts - f["tc"], "placed", "timecode", 100.0
                c.notes.append("by timecode %s with %s" % (c.timecode, os.path.basename(f["clip"].path)))
                return c
        if args.sync_by == "timecode" or not c.has_audio or master is None:
            c.status = "not placed"
            return c
        c.status, c.split, c.parts = "", False, []
        try:
            sync_clip(c, master, st)
        except Exception as e:
            c.status = "not placed"
            c.notes.append("error: %s" % e)
            return c
        if c.status == "placed" and c.split:
            p = max((p for p in c.parts if p.status == "placed"), key=lambda p: p.src_out - p.src_in)
            c.offset, c.confidence = p.offset, p.confidence
            c.split, c.parts = False, []
        if c.status == "placed":
            c.how = "scratch audio"
        else:
            c.status = "not placed"
        return c

    pending = list(usable)
    leads = []                   # reported last: they take no matching, so counting them first made the
    while pending:               # clips-per-minute (and the time left) look far faster than it is
        # the lowest camera still waiting leads: its clips become takes of their own
        lead = min(letter_of[c.path] for c in pending)
        new = [c for c in pending if letter_of[c.path] == lead]
        pending = [c for c in pending if letter_of[c.path] != lead]
        event("stage", text="Listening to %s Cam" % lead)
        for c in new:
            try:
                x = load_audio(c.audio_src or c.path) if c.has_audio else np.zeros(int(c.duration * SR), np.float32)
            except RuntimeError:
                x = np.zeros(int(c.duration * SR), np.float32)
            files.append(dict(clip=c, tc=tc_seconds(c.timecode, c.fps), dur=len(x) / SR, at=pos, x=x))
            c.status, c.how, c.offset, c.confidence = "placed", "", pos, 100.0
            chunks += [x, np.zeros(int(NARR_GAP_S * SR), np.float32)]
            pos += len(x) / SR + NARR_GAP_S
            c.afile = files[-1]
            leads.append(c)
        if not pending:
            break
        master = MasterIndex(np.concatenate(chunks))
        event("stage", text="Lining up the other cameras with %s Cam" % lead)
        left = []
        with cf.ThreadPoolExecutor(args.jobs) as ex:             # each clip reported as soon as it's done
            for fut in cf.as_completed([ex.submit(work, c) for c in pending]):
                c = fut.result()
                c.afile = take_of(c) if c.status == "placed" else None
                if c.afile is None:
                    c.status = "not placed"
                    left.append(c)
                else:
                    report(c)
                    log("  %-40s with %s by %s" % (c.rel, os.path.basename(c.afile["clip"].path), c.how))
        pending = left
    for c in leads:
        report(c)
    for c in clips:
        if c.status != "placed" and c not in usable:
            report(c)

    # the takes in shooting order: by timecode when every take has it, else by when the file was written
    def when(f):
        try:
            return os.path.getmtime(f["clip"].path)
        except OSError:
            return 0.0
    order = sorted(files, key=(lambda f: (f["tc"], f["clip"].rel)) if files and all(f["tc"] is not None for f in files)
                   else (lambda f: (when(f), f["clip"].rel)))
    placed = [c for c in clips if c.status == "placed"]
    seq_fps = args.fps or (collections.Counter(c.fps for c in placed if c.fps).most_common(1) or
                           collections.Counter(c.fps for c in clips if c.fps).most_common(1) or [(24.0, 0)])[0][0]
    t = 0.0
    for f in order:
        mine = [c for c in placed if c.afile is f]
        head = max([0.0] + [f["at"] - c.offset for c in mine])
        tail = max([0.0] + [c.offset + c.duration * c.speed - (f["at"] + f["dur"]) for c in mine])
        f["pos"] = t + head
        t = f["pos"] + f["dur"] + tail + NARR_GAP_S
    for c in placed:
        c.tl = c.afile["pos"] + (c.offset - c.afile["at"])
        c.in_file = c.offset - c.afile["at"]
    fr = lambda s: int(round(s * seq_fps))
    base, entries = 0, []
    for letter, cl in cams:
        label = camera_label(letter)
        ends = []
        for c in sorted([c for c in cl if c.status == "placed"], key=lambda c: (c.tl, c.rel)):
            a, b = fr(c.tl), fr(c.tl) + fr(c.duration * c.speed)
            k = next((k for k, e in enumerate(ends) if e <= a), None)
            if k is None:
                k = len(ends)
                ends.append(b)
            ends[k] = b
            c.track = base + k + 1
            entries.append(dict(media=Media.of_clip(c, seq_fps), start=a, vtrack=c.track, atrack=c.track,
                                aenabled=True, label=label, speed=c.speed))
        base += len(ends)
    pos_f = fr(t + UNSYNCED_GAP_S)
    for letter, cl in cams:
        for c in cl:
            if c.status == "placed" or not (c.readable and c.fps and c.width):
                continue
            why = re.split(r"\s*[(;]", (c.reasons or ["not synced"])[0])[0].strip()
            entries.append(dict(media=Media.of_clip(c, seq_fps), start=pos_f, vtrack=1, atrack=1, all_audio="raw",
                                label=camera_label(letter), tail=True, name="%s (%s)" % (os.path.basename(c.path), why)))
            pos_f += fr(c.duration)

    sync_name = "%s_Sync" % project_name
    args.narrative = dict(name=sync_name, entries=entries)
    event("stage", text="Writing the Premiere project")
    proj_file = re.sub(r"[^\w .-]+", "_", project_name) + ".xml"
    write_xml(build_project(project_name, clips, cams, seq_fps, 0.0, None, collections.defaultdict(list), args),
              os.path.join(args.xml_out, proj_file), resolve=True)
    log("Wrote %s" % os.path.join(args.xml_out, proj_file))
    report_path = os.path.join(args.out, "narrative_report.csv")
    with open(report_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "camera", "status", "synced_by", "take", "starts_into_take_s", "timeline_timecode", "reason"])
        for letter, cl in cams:
            for c in cl:
                ok = c.status == "placed"
                lead = ok and c.afile["clip"] is c
                w.writerow([c.rel, "%s Cam" % letter, c.status, ("leads its take" if lead else c.how) if ok else "",
                            os.path.basename(c.afile["clip"].path) if ok else "", "%.3f" % c.in_file if ok else "",
                            fmt_frames(start_frames(seq_fps) + fr(c.tl), seq_fps) if ok else "",
                            "" if ok else "; ".join(c.reasons)])
    synced = [c for c in placed if c.afile["clip"] is not c]
    log("Wrote %s. %d takes; %d clips lined up with them (%d by timecode)." % (
        os.path.basename(report_path), len(files), len(synced), sum(c.how == "timecode" for c in synced)))
    aside = collections.Counter(c.reasons[0] if c.reasons else REASON_UNREADABLE for c in clips if c.status != "placed")
    lists = dict(aside={}, worth_a_look=[], restarted=[], check_chorus=[])
    for c in clips:
        if c.status != "placed":
            lists["aside"].setdefault(c.reasons[0] if c.reasons else REASON_UNREADABLE, []).append(
                dict(file=os.path.basename(c.path), cam=clip_cam(c), at=None))
    event("done", project=project_name, edit_name=seq_name("edit", project=project_name), out=args.out,
          xml=os.path.join(args.xml_out, proj_file), report=report_path, song_duration=round(t, 2), mode="narrative",
          clips=len(clips), synced=len(placed), sync_name=sync_name, audio=0, takes=len(files), cams_together=True,
          by_timecode=sum(c.how == "timecode" for c in synced),
          cameras=[dict(letter=l, name=cam_bin_name(l, cl), label=camera_label(l), clips=len(cl),
                        synced=sum(c.status == "placed" for c in cl),
                        spans=sorted([round(c.tl, 2), round(c.tl + c.duration * c.speed, 2)]
                                     for c in cl if c.status == "placed")) for l, cl in cams],
          set_aside=[dict(reason=r, count=k) for r, k in aside.most_common()],
          unreadable=sum(1 for c in clips if not c.readable), restarted=0, check_chorus=0, worth_a_look=0,
          lists=lists)


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
          cams=collections.Counter(clip_cam(c) for c in clips),
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
                            check=c.check, phase=c.phase, repeat_alt=c.repeat_alt)]
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
          worth_a_look=len({id(c) for c, _, _ in doubtful(clips)}), lists=clip_lists(clips, master),
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
        moves.append([bname, bin_path("footage") if new_cam else "%s > %s (as a bin inside it)" % (bin_path("footage"), cam_name)])
        sized = [c for c in usable if c.width]
        if sized:
            (w, h), fps = first_format(sized, seq_fps)
            entries, tc = stringout_entries(sized, fps, label)
            xw.sequence(top, seq_name("breakup", letter, suffix=suffix), fps, w, h, tc, entries, label)
            moves.append([seq_name("breakup", letter, suffix=suffix), bin_path("breakup")])
        placed = placements_of(cl)
        if placed:
            (w, h) = args.sync_size or first_format([c for c, _ in placed], seq_fps)[0]
            entries, tc = sync_entries(placed, seq_fps, preroll, master_media, args, label, song=new_cam)
            xw.sequence(top, seq_name("synced", letter, suffix=suffix), seq_fps, w, h, tc, entries, label)
            moves.append([seq_name("synced", letter, suffix=suffix),
                          "%s, then onto a new top track of %s, at its start"
                          % (bin_path("synced"), seq_name("synced", letter)) if not new_cam else bin_path("synced")])
            xw.sequence(top, seq_name("condensed", letter, suffix=suffix), seq_fps, w, h, tc,
                        condense(entries, seq_fps), label)
            moves.append([seq_name("condensed", letter, suffix=suffix),
                          "%s, then nest it in the Edit sequence on a new track" % bin_path("condensed")
                          if new_cam else "%s, then onto a new top track of %s, at its start"
                          % (bin_path("condensed"), seq_name("condensed", letter))])
    for bname in ("Music", "SFX", "Captured"):
        if audio_bins.get(bname):
            b = bin_(top, "%s (new)" % bname)
            for m_ in audio_bins[bname]:
                xw.master_clip(b, m_)
            role = bname.lower()
            moves.append(["%s (new)" % bname, bin_path(role if role in bin_roles(SETTINGS["bins"]) else "music")])
    return root, moves


if __name__ == "__main__":
    main()
