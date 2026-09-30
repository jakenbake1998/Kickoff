# Kickoff

Kickoff is Jake's Mac app. It syncs music video footage (with no timecode) to the song and builds a ready Premiere project as XMEML: bins, Breakups, Sync sequences, and an Edit sequence. It also sets up regular (commercial) projects. Jake is a video editor, not a developer. Keep replies to him short and in plain words.

## Pushing to main ships to Jake

The Mac app updates itself from `main` at every launch (`mac-app/kickoff-update.sh`):
- The root `musicsync.py` applies right away, for the next run.
- `mac-app/ui/index.html` and `mac-app/Kickoff.swift` show on the NEXT launch. The Swift file is recompiled on his Mac with `xcrun swiftc`, and the new app is swapped in only if it builds.

So anything on main is live. **Jake sends notes in batches. Log them, investigate, and don't build or push app changes until he says "go".** Bump `VERSION` in `musicsync.py` for every change that ships.

## Layout

- `musicsync.py` is the engine (Python, numpy/scipy, ffmpeg). `mac-app/engine/musicsync.py` is an identical copy used by the installer. Keep them byte-identical (`cmp musicsync.py mac-app/engine/musicsync.py`).
- `mac-app/ui/index.html` is the whole window (HTML/CSS/JS in a WKWebView). It must keep `window.Kickoff`, or the updater refuses it.
- `mac-app/Kickoff.swift` is the AppKit shell. The engine reports progress as `@@kickoff {json}` lines (`--events`).
- `tests/` holds the synthetic footage generator and the accuracy checks.
- Settings (the window's Settings page) are saved by the app to `~/Library/Application Support/Kickoff/settings.json`; the engine reads it through `KICKOFF_SETTINGS` or `--settings`. Bins are found by role (footage, breakup, sync, synced, condensed, edit, music, sfx, captured, adjustment), so they can be renamed, moved and nested. With no settings file the output is exactly the defaults below.

## Testing before any push

Engine changes (ffmpeg must be installed, e.g. `apt-get install -y ffmpeg`):
```
python3 -m py_compile musicsync.py
python3 tests/make_synthetic.py /tmp/synth
python3 musicsync.py /tmp/synth/clips          # add --rebuild when running it again
python3 tests/check.py /tmp/synth/expected.json "/tmp/synth/clips/Kickoff Exports/sync_report.csv"
python3 tests/stress_passes.py                  # takes with restarts: must be 0 wrong
python3 tests/long_take.py                      # 30-minute takes with many plays
```
Judge sync changes by **zero wrong placements**. A clip that's set aside with a reason is better than one placed in the wrong spot.

UI changes: follow the `kickoff-style` skill when it's available (dark window, orange `#f0962e` accent, bundled fonts, big Start button, History apart in the top-right corner). Render the page in Chromium/Playwright at about 900x640, look at the screenshot, and show it to Jake.

Swift changes: they can only compile on a Mac. Keep them small, and when possible check them on Jake's Mac (Remote Control) with `xcrun swiftc -O -o /tmp/Kickoff mac-app/Kickoff.swift -framework Cocoa -framework WebKit`.

## Jake's Premiere rules (don't regress these)

The full list is in the `jake-editing-rules` skill. The ones code changes most often break:
- Camera folder names are authoritative. "C Cam (Action 4.1)" means letter C and exactly that bin name, whatever the metadata says.
- Clips always scale to FILL the frame with no black edges, never "Scale to Frame Size".
- Sync sequences are 3840x2160 and the song starts at 01:00:00:00. "A Cam_Synced" (Sequence > Sync > Synced) has one take per video track; "A Cam_Synced_Condensed" (Sync > Synced Condensed) packs the same clips onto the smallest of 4, 9, 16 tracks (multicam grids) that fits, without cutting any, keeping clip order (clip 1 on V1, the rest following down). EVERY clip that isn't on a track goes back to back on V1 of both, starting a minute after the song (and every synced take) ends: the unsure ones first under an "Unsure" marker (with the guessed song time in the name), then, a minute after those, the rest under a "No match" marker, each group in file order, with all their camera audio on and the reason in the clip name. Only files ffmpeg can't read are left out, and the log names them. CamsNested (in the Synced Condensed bin) and the Edit sequence nest the condensed ones as plain nests (not multicam source sequences) with multicam enabled.
- Label colors: A Iris, B Mango, C Rose, then any other distinct colors.
- Camera audio stays audible: keep every channel, with the synced scratch channel on top. Master clips and Breakups carry every channel of the file in camera order, all on (the XML lists each stream/channel; a Mini LF has 5 mono streams). The song track is muted in the Condensed sequences.
- The project XML goes where Jake picks in the window ("Export XML to"). If he picks nothing, it goes at the top of the drive the footage is on (`/Volumes/<drive>`, the window passes `--xml-dir drive`). Footage on the Mac's own disk falls back to `Kickoff Exports`.
- Reports and the project state file (`kickoff-project.json`) always go in `Kickoff Exports` inside the shoot folder (older runs used `Premiere Sync`, which is still found).
- Proxy, preview, render, Generations and Exports folders are skipped when scanning.
