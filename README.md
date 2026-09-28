# musicsync

Batch sync for music video footage without timecode. It listens to each clip's scratch audio,
finds where it sits in the master track with landmark audio fingerprinting, and writes one
Premiere-importable XMEML sequence per camera with every clip on its own video track.

## Install

- **ffmpeg** (includes ffprobe). Mac: `brew install ffmpeg`. Windows: `winget install ffmpeg`.
- **Python 3.9+** with numpy and scipy: `python3 -m pip install -r requirements.txt`

## Run

Point it at the shoot folder:

```
python3 musicsync.py "/Volumes/Shoot/White Wolf"
```

It finds the song itself (the only audio file, or the one in a `Music` folder or named
master/song/mix; otherwise pass `--master "Song.wav"`). Camera card dumps can go in as they are
(`A_CAM/PRIVATE/M4ROOT/CLIP/...`, `B_CAM/A001C003_....mxf`); Sony proxy and thumbnail folders are
skipped. Any other audio files (boom, lav, Zoom recorder) are treated as captured audio.

Everything is written to `Premiere Sync` inside the shoot folder (`-o` to change):

| File | What it is |
|---|---|
| `White Wolf.xml` | The whole project: bins, footage, sequences, labels (below) |
| `sync_report.csv` / `sync_report.md` | Every clip (every pass of a restarted take): placed or not, reason, offset, confidence, waveform check, camera, track, drift |
| `A Cam Sync - ILME-FX3.xml` ... | Only with `--per-camera`: each sync sequence on its own |

## In Premiere

File > Import the project XML. Premiere puts it in a bin named after the file; drag its contents
to the top of the project. The tree is:

```
Adjustment Layers                    (placeholder; Temp Color can't come through XML)
Footage / A Cam (Mini LF), B Cam (FX3), ...   every clip of that camera, bin and clips label colored
Sequence / Breakup   "A Cam_Breakup" ...: every clip of the camera back to back in file order
         / Sync      "A Cam_Sync" ...: every synced clip on its own video track
         / Edit      "<Project>_Edit": each Cam_Sync nested on V1 (A), V2 (B)..., song on A1
                / Working, Past      (placeholders)
Audio / Music        the song (+ anything else in a Music folder)
      / SFX          files from an SFX folder, else placeholder
      / Captured     other audio-only files (boom, lav, recorder)
```

Premiere's XML import drops empty bins, so a bin that would be empty gets a blank
`(empty bin).png` to keep it; delete it once the project is open (`--no-placeholders` to skip).

Label colors: A Cam Iris, B Cam Mango, C Cam Rose, then Caribbean, Forest, Lavender, Cerulean,
Yellow... applied to that camera's bin, its clips and both of its sequences.

Breakup and Sync sequences take the frame size of the camera's first clip (filename order); clips
with a different size are scaled to fill the frame.

Sync sequences: the song's first sample sits at **01:00:00:00** in every one, so they line up
when nested. V1..Vn are the synced clips in filename order (`--track-order offset` sorts by song
position). A1 is the song. A2.. are each clip's scratch audio, imported **disabled** so you can
check sync by enabling one (`--scratch-audio off` leaves them out, `on` enables them).

Edit sequence: the Cam_Sync sequences are plain nests. XML can't switch multicam on, so after
import select the nests in the timeline and right-click > Multi-Camera > Enable.

**Not possible from XML:** the `Temp Color` adjustment layer and the multicam switch.

**Proxies:** in Premiere select everything in Footage, right-click > Proxy > Create Proxies,
QuickTime / ProRes 422 Proxy. Media Encoder makes them with the Mac's hardware ProRes encoder
and Premiere attaches them automatically.

If the XML was written on a different machine than the one editing, rewrite the media paths:

```
--path-map /mnt/footage=/Volumes/Shoot/Footage        # Mac
--path-map /mnt/footage=D:/Shoot/Footage              # Windows
```

Otherwise Premiere asks you to relink; pointing it at one file relinks the rest from the same folder.

## What gets set aside

Nothing is guessed. Every clip that isn't placed gets a reason in the report:

| Reason | Meaning |
|---|---|
| `slow motion (S&Q...)` | Sony S&Q clip (sidecar says captured above its playback rate) that somehow has audio. 60p/120p clips recorded in real time with scratch audio **are synced** |
| `high frame rate (over --max-fps)` | Only if you set `--max-fps` |
| `no audio track` | No audio stream (typical for slow motion) |
| `audio track is silent` | Audio stream present but empty |
| `no match to song` | Best alignment is no better than chance: B-roll, narrative, wrong song |
| `ambiguous match (repeated section of song)` | Fits equally well at two places, and the audio at the two isn't clearly identical (or you passed `--set-aside-repeats`). The report lists both song times. Clips covering only a copy-pasted chorus are placed instead: see "Repeated chorus" below |
| `confidence below threshold` | A likely position exists but not decisively enough (`--threshold`, default 60) |
| `unreadable file` | ffmpeg can't open it. RED `.R3D` and `.braw` are in this group; sync their proxies instead |

## Takes where the song stopped, restarted or jumped

Every clip's audio is followed from head to tail, second by second, against the whole song. If
the song was stopped and started again, paused and resumed, or jumped to another section while the
camera kept rolling, each pass is found on its own:

- The clip is cut where the next pass's song starts (found to about a tenth of a second with a
  sliding waveform comparison). In a gap, the cut sits just before the song comes back in.
- Each pass goes on its own video track in the camera's Sync sequence, named
  `C0007.MP4 (pass 2 of 3)`, at its own place in the song. Nothing is duplicated on disk; the
  passes are the same file with different in and out points.
- The report lists every pass with its clip time, track and song time, in its own section.
- A repeated chorus doesn't count as a jump: when one position explains both stretches, it stays
  one pass. A pass that only covers a copy-pasted chorus is placed at the first copy and flagged
  (see "Repeated chorus" below).
- Nothing outside a pass rides along out of sync: the stretches between and around passes are
  searched again on their own (landmarks plus a phase correlation against the whole song), and a
  false start or other short burst of song becomes its own part, set aside as
  `short burst of song (false start?)` unless it's long and clear enough to place.
- A take where the song stops and never restarts syncs as one clip, and the report's "Worth a look"
  section flags the stretch where the audio no longer matches.

Tested with `tests/stress_passes.py 300`: 300 random single, restarted, paused, jumped and
three-pass takes (600 passes, 3 to 15 dB signal-to-noise, with and without a live drummer). No pass
was placed at a wrong position. 543 were placed exactly and 39 chorus-only passes at a flagged
chorus copy. 17 were left unplaced: passes that are mostly repeated chorus with 3 seconds or less
of anything else, where neither position can be confirmed. One take whose two passes sat only
0.1 s apart in the song was placed as one clip, 1 frame off. The research thread's independent
generator (`sync-research/crosscheck_builder.py 60 11`, different layouts, rooms and scoring) also
finds no wrong placements apart from the flagged chorus copies.

## Repeated chorus

When a clip (or a pass) only covers a section that appears twice in the song with the same audio,
it lip-syncs correctly at either copy, but nothing in the audio says which one was being shot.
Kickoff checks the waveform matches at both copies, places it at the first, adds `(check chorus)`
to its name, and lists it under "Check which chorus" in the report with both song times. Slide it
to the other copy if that's where it belongs. `--set-aside-repeats` sets these aside instead.

## Waveform check

Every placement is checked a second way. Fingerprint landmarks find the position; then the clip's
actual waveform is compared with the song at that position in 4-second windows (phase
correlation, corrected for any drift). The report's `waveform_check` column says how many windows
match, e.g. `14/15`. A correct placement matches in every window where the song is playing. Clips
that match in under 70% of their windows are listed under "Worth a look". The same comparison
settles clips the landmarks alone call borderline (part chorus, part verse): if some stretch
matches only at the best position, the clip is placed.

## Sped-up playback (slow motion)

When a clip doesn't match at normal speed, Kickoff tries the song played faster on set: 1.25x,
1.5x, 2x, 2.5x, 3x, 4x, 5x and the clip's frame rate divided by 23.976/25/29.97. It tries both
ways of speeding a song up: varispeed (pitch goes up) and time-stretch (pitch kept, what VLC or
a phone does; its audio is slowed back down with a phase vocoder before matching). A match found this way is placed slowed down by the same amount (a 2x match
lands at 50% speed), so the lips line up with the normal-speed song. The report's
`playback_speed` column shows the speed and method. Time-stretched matches are accurate to about
one frame; varispeed matches get the same sub-frame refinement as normal ones. A sped-up match has
to stand further above chance than a normal one, because more speeds were tried.

Clips shot at a high frame rate while the song played at normal speed simply match at 1x.

## Confidence

For each clip the matcher counts how many fingerprint landmarks agree on each possible offset.
Confidence (0 to 100) is how decisively the best offset beats the runner-up, scaled by the amount
of evidence: 60 is about 2.7 standard deviations, 90+ is unmistakable. Long clips with a clean
match score near 100. Raise `--threshold` to be stricter.

## Accuracy and drift

After the landmark match, the offset is refined to sub-frame precision with a phase-correlation
pass, then placed on the nearest sequence frame (so at most half a frame of rounding). The clip is
also followed in overlapping 10 second windows from head to tail. If the camera clock or the playback speed
differs from the master (23.976 vs 24 is a 0.1% error, about 3 frames over 2 minutes), the report
shows the head-to-tail drift in ms and frames and lists clips that drift a frame or more. Those
clips are placed so the error is split, in sync at their middle.

## Camera grouping

Clips are grouped by camera model from the file metadata (ffprobe tags, Sony `M01.XML` sidecars,
GoPro firmware string), then split by body using, in order: the serial number, the camera letter in
ARRI/RED style names (`A001C003`, `B002_C004`), or the top-level folder under the clips folder.
Camera letters come from those names or folders (`A_CAM`, `CAM B`, `C001`) when present, otherwise
A, B, C... in order. `--group-by model` or `--group-by folder` forces one rule.

## Options

```
--threshold 60        minimum confidence to place a clip
--max-fps 0           set aside clips above this frame rate even with audio (0: sync them)
--fps 23.976          sequence rate (default: most common among placed clips)
--group-by auto|model|folder
--track-order name|offset
--scratch-audio off|disabled|on
--master FILE         the song, if it isn't found automatically
--name NAME           project name (default: folder name)
--per-camera          also write each sync sequence as its own XML
--no-master-audio     leave the song off A1
--set-aside-repeats   don't place chorus-only clips at the first chorus
--path-map OLD=NEW    rewrite media paths in the XML (repeatable)
-j 8                  parallel files; use -j 2 when reading from a single spinning drive
```

## Self-test

```
python3 tests/make_synthetic.py /tmp/synth
python3 musicsync.py /tmp/synth/clips
python3 tests/check.py /tmp/synth/expected.json "/tmp/synth/clips/Premiere Sync/sync_report.csv"
```

`make_synthetic.py` builds a song with a pasted chorus and 20 clips from an FX3, an Alexa and a
GoPro whose audio is the song played into a reverberant room with a live drummer, noise and a
limiter. It includes slow motion with and without audio, B-roll with different music, a chorus-only
clip, a silent clip, an unreadable R3D, a 0.1% speed mismatch, sped-up playback (varispeed and
time-stretched), and takes where the song was restarted, paused, jumped, played three times, false
started, or stopped for good. The video burns in the clip name and the song time of every frame
(`NO SONG` between passes), so synced clips show the same number across tracks.
