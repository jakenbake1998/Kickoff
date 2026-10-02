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
master/song/mix; with several there, it passes over stems, instrumentals and clicks and takes the
longest; otherwise pass `--master "Song.wav"`). When a card or a day's footage is run on its own, a
`Music` folder next to it or a couple of levels up (`Shoot/Audio/Music`) counts too. Camera card
dumps can go in as they are (`A_CAM/PRIVATE/M4ROOT/CLIP/...`, `B_CAM/A001C003_....mxf`). Proxies,
Premiere preview renders and auto-saves, and `Renders`, `Generations` and `Exports` folders are
skipped, as are Sony thumbnail folders. Any other audio files (boom, lav, Zoom recorder) are treated
as captured audio.

Several folders can go in together (cards, or a day's camera folders):

```
python3 musicsync.py "Day 2/A Cam (Mini LF)/A004" "Day 2/B Cam (FX3)/B003"
```

They make one project in the folder that holds them all, or are added to the project that folder
belongs to when it was set up before. A song file given alongside them is the song.

Everything is written to `Kickoff Exports` inside the shoot folder (`-o` to change; projects set up
before 0.5.3 keep using their `Premiere Sync` folder). `--xml-dir FOLDER`
puts the project XMLs somewhere else (reports and the project memory stay in `Kickoff Exports`);
`--xml-dir drive` puts them at the top of the drive the footage is on (`/Volumes/26JL02`), which is
what the Kickoff window does unless you choose a folder:

| File | What it is |
|---|---|
| `White Wolf.xml` | The whole project: bins, footage, sequences, labels (below) |
| `sync_report.csv` / `sync_report.md` | Every clip (every pass of a restarted take): placed or not, reason, offset, confidence, waveform check, camera, track, drift |
| `A Cam Sync - ILME-FX3.xml` ... | Only with `--per-camera`: each sync sequence on its own |

## Music video or regular project

Kickoff works out which kind of job a folder is (`--mode auto`, the default): when it finds a song
and the clips line up with it, it's a music video and everything above applies. When there's no
song, or the audio file it found lines up with none of the clips (say, a boom track on a
commercial; it gives up after 12 clips with sound and no match), it sets up a regular project
instead: the same bins and label colors, a footage bin and a Breakup sequence per camera, an empty
`<Project>_Edit` sequence at 3840x2160 starting at 01:00:00:00, every audio file sorted into
Music, SFX or Captured, and `clip_list.csv` in place of the sync report. There are no Sync
sequences and no Sync bin. `--mode music` or `--mode setup` (the switch in the Kickoff window)
forces one or the other.

## Adding cards as they come in (DIT days)

Kickoff remembers every folder it has set up (`Kickoff Exports/kickoff-project.json`: the song,
cameras and their letters, frame rate, where the sequences start, and every file already in the
project). Run it again on the same shoot folder, or drop just the new card's folder if it sits
inside the shoot folder, and it only picks up what's new since last time. It writes one small XML,
`<Project> - Add 2 (A Cam Card 2, B Cam Card 2).xml`, to import into the project you already have
open. Premiere puts it in a bin of its own, and the window (and the log) lists where each item goes:

- `A Cam Card 2`: drag it into Footage > A Cam as a bin of its own. The clips keep A Cam's label color.
- `A Cam_Breakup Card 2`: Sequence > Breakup.
- `A Cam_Synced Card 2`: the new card's synced clips, starting at the same timecode as `A Cam_Synced`.
  Drag it onto a new top track of `A Cam_Synced` at its start.
- `A Cam_Synced_Condensed Card 2`: the same, packed onto as few tracks as possible. Drag it onto a new
  top track of `A Cam_Synced_Condensed` at its start. Because that one is nested in the Edit sequence,
  the new clips show up there too.
- A camera that wasn't in the project before comes in whole (`C Cam (GoPro HERO9)`, `C Cam_Synced`
  and `C Cam_Synced_Condensed` with the song, `C Cam_Breakup`); nest its condensed sequence in the
  Edit sequence.
- New sound files come in as `Captured (new)` etc.

Cameras keep their letters and colors across runs. Every Sync sequence starts at least 10 s before
the song, so a card that started rolling earlier than the first one still fits; if a clip rolled even
longer before the song, its head is trimmed in the Sync sequence (the report says so). Files that
can't be read (maybe still copying) are tried again on the next run once they've changed. Each add
has its own report (`sync_report add 2.md`). `--rebuild` (the "Start over" box in the window) ignores
earlier runs and builds the whole project again.

## In Premiere

File > Import the project XML. Premiere puts it in a bin named after the file; drag its contents
to the top of the project. The tree is:

```
Adjustment Layers                    (placeholder; Temp Color can't come through XML)
Footage / A Cam (Mini LF), B Cam (FX3), ...   every clip of that camera, bin and clips label colored
Sequence / Breakup   "A Cam_Breakup" ...: every clip of the camera back to back in file order, every camera audio
                      channel on its own track, all on
         / Sync      "<Project>_CamsNested": each condensed sequence nested on V1 (A), V2 (B)..., song on A1
                / Synced              "A Cam_Synced" ...: every synced clip on its own video track
                / Synced Condensed    "A Cam_Synced_Condensed" ...: the same clips on 2, 4, 8 or 16...
                                      tracks (the fewest that fit, none cut or moved), clip 1 on V1 and
                                      the rest in order below it, so multicam has fewer feeds; the song
                                      track is muted so CamsNested/Edit play it once
         / Edit      "<Project>_Edit": a copy of <Project>_CamsNested, to cut in
                / Working, Past      (placeholders)
Audio / Music        the song (+ anything else in a Music folder)
      / SFX          files from an SFX folder, else placeholder
      / Captured     other audio-only files (boom, lav, recorder)
```

Premiere's XML import drops empty bins, so a bin that would be empty gets a blank
`(empty bin).png` to keep it; delete it once the project is open (`--no-placeholders` to skip).

Label colors: A Cam Iris, B Cam Mango, C Cam Rose, D Cam Yellow, E Cam Cerulean, then Caribbean,
Lavender, Magenta, Forest... applied to that camera's bin, its clips and both of its sequences.

Sync sequences (and the Edit sequence) are 3840x2160, with every clip scaled to fill the frame edge
to edge, cropping what overflows, so there are never black edges: 1080p at 200%, 3200x1800 at 120%,
a 4480x3096 open gate at 85.71% (`--sync-size WxH` to change, `--sync-size first` to use the
camera's first clip). Breakup sequences take the frame size of the camera's first clip (filename
order), filled the same way. Anamorphic footage is measured unsqueezed and phone clips shot upright
as upright.

Sync sequences: the song's first sample sits at **01:00:00:00** in every one, so they line up
when nested. V1..Vn are the synced clips in filename order (`--track-order offset` sorts by song
position). A1 is the song. A2.. are each clip's scratch audio, imported **disabled** so you can
check sync by enabling one (`--scratch-audio off` leaves them out, `on` enables them).

Edit sequence: the Cam_Synced_Condensed sequences are plain nests. XML can't switch multicam on, so after
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

## Slop Cut (a rough edit, cut for you)

When a music video finishes, **Make a Slop Cut** on the end screen adds `<project>_Slop Cut` to the Edit bin,
right under the Edit sequence, in the same XML. It has the Edit sequence's song, with every synced take
laid out on its own tracks (each camera's condensed tracks, A Cam's first) and cut at the shot lines. The
take picked for each shot is enabled and every other piece is disabled, never deleted, so all the footage
is there to switch on.

How it picks:
- **The music.** Kickoff finds the beats and bars in the song and splits it into bands (drums, bass, the
  mids where vocals and guitars sit, the highs). Cuts land on bar lines, every 1 to 2 bars when the song is
  loud and 2 to 8 when it's calm, always 2 to 8 s, plus a cut into each drum fill, at each new section and
  where the singing comes in.
- **Who's in each take.** On the Mac, `kickoff_vision` (Apple Vision, built into macOS; the updater
  compiles `mac-app/vision/kickoff_vision.swift`) looks at a frame a second of each take: instrument and
  mic labels, how many people, how big the faces are. Results are cached with the audio cache.
- Each shot goes to the take showing what the music calls for: drums on fills, the singer on vocal lines,
  guitar on solos, a wide at a new section. A take that keeps winning keeps playing (up to 8 s), so the
  cuts don't fall into a pattern. Without the frame tagger it cuts by the music alone.

Sync placement isn't touched. Running it again replaces the earlier Slop Cut. From the command line:
`python3 musicsync.py --mode slop "PROJECT.xml"`, then `python3 tests/check_slop.py "PROJECT.xml"`.

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
| `confidence below threshold` | A likely position exists but not decisively enough (`--threshold`, default 60). Before a clip lands here, the waveform gets a say: a position that lines up through half the clip (3 windows or more) and twice as well as any other is placed, noted `placed by waveform` |
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
  `short burst of song (false start?)` unless it's long and clear enough to place. Twenty seconds
  or more of song whose waveform lines up is a performance, and is placed even when the landmarks
  miss it (a worn tape, the band louder than the playback).
- Every stretch between the plays found is searched again with only its own landmarks, so a quieter
  play the louder ones outvoted is still found.
- A part runs on only 8 s past its song. A longer stretch after it becomes a part of its own, set
  aside as `between plays of the song`, so a play too buried to match never rides along at the
  previous play's position.
- A take where the song stops and never restarts syncs as one clip, and the report's "Worth a look"
  section flags the stretch where the audio no longer matches.

Long takes work the same way: an action camera left rolling for half an hour, with the song played a
dozen times and minutes of talk between plays, is cut into one part per play. Each part is checked
and drift-corrected only over the stretch where its song is heard, so the talk around it can't pull
the placement off.

Tested with `tests/stress_passes.py 300`: 300 random single, restarted, paused, jumped and
three-pass takes (600 passes, 3 to 15 dB signal-to-noise, with and without a live drummer). No pass
was placed at a wrong position and none was missed: 545 were placed exactly and 55 chorus-only
passes at a flagged chorus copy, including a restart that landed only 0.1 s from where the song
had been. `tests/long_take.py` builds 20 to 27 minute takes with 12
plays of the song and 30 to 150 seconds of the drummer and room noise between them: on
ten of them, all 120 plays were placed exactly. The research thread's independent generator
(`sync-research/crosscheck_builder.py 60 11`, different layouts, rooms and scoring) also finds no
wrong placements apart from the flagged chorus copies.

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

## Which audio channel

Cameras put the scratch mic on different channels: an ARRI Mini LF records timecode on channel 3
and the mic on 4, with 1-2 nearly silent. Kickoff never mixes channels down first. It reads every
channel of every audio stream, skips silent ones and timecode (a constant-level square wave),
tries the rest against the song and keeps the one that matches best. The report notes which one
(`scratch audio on channel 4`).

Every channel comes into Premiere. Breakup sequences carry all of a clip's audio on consecutive
tracks, the scratch channel on the top one and playing, the others below and switched off (on a
Mini LF they are near silence and timecode); with one audio track, that's all there is. Sync
sequences carry the scratch channel only. Premiere makes one audio clip per mono or stereo stream
and one per channel of a stream with 3 or more.

## Camera grouping

A folder named for the camera decides first, at any depth (`Footage/Day 1/C Cam (Action 4.1)`):
its letter is the camera letter, the name in brackets is the bin name (`C Cam (Action 4.1)`), and
every file in it is that camera whatever its metadata says, in every Day folder. The same file found
twice (same name and size, e.g. copied into a selects folder) counts once, the copy in the camera
folder.

Footage outside camera folders is grouped by camera model from the file metadata (ffprobe tags, Sony `M01.XML` sidecars,
GoPro firmware string), then split by body using, in order: the serial number, the camera letter in
ARRI/RED style names (`A001C003`, `B002_C004`), or the top-level folder under the clips folder.
Its letters come from reel names (`A001C003`), otherwise the next free letter. `--group-by model` or `--group-by folder` forces one rule.

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
--mode auto|music|setup   music video (sync) or project setup only (default: auto)
--set-aside-repeats   don't place chorus-only clips at the first chorus
--sync-size 3840x2160 frame size of the Sync and Edit sequences (clips scaled to fill)
--rebuild             build the whole project again instead of adding what's new
--path-map OLD=NEW    rewrite media paths in the XML (repeatable)
-j 8                  parallel files; use -j 2 when reading from a single spinning drive
```

## Self-test

```
python3 tests/make_synthetic.py /tmp/synth
python3 musicsync.py /tmp/synth/clips          # add --rebuild when running it again
python3 tests/check.py /tmp/synth/expected.json "/tmp/synth/clips/Kickoff Exports/sync_report.csv"
```

`make_synthetic.py` builds a song with a pasted chorus and 20 clips from an FX3, an Alexa and a
GoPro whose audio is the song played into a reverberant room with a live drummer, noise and a
limiter. It includes slow motion with and without audio, B-roll with different music, a chorus-only
clip, a silent clip, an unreadable R3D, a 0.1% speed mismatch, sped-up playback (varispeed and
time-stretched), and takes where the song was restarted, paused, jumped, played three times, false
started, or stopped for good. The video burns in the clip name and the song time of every frame
(`NO SONG` between passes), so synced clips show the same number across tracks.
