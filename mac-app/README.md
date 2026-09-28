# Kickoff (Mac)

Drop a shoot folder on Kickoff and it writes, inside that folder, `Premiere Sync/<Folder>.xml`
(your bin structure, synced camera sequences, breakups, the edit sequence) and a sync report.

## Install (once)

1. Unzip, then **right-click `Install Kickoff.command` > Open** (right-click is needed the first
   time because the file came from the internet).
2. If asked, let it install Apple's Command Line Tools, then run the installer again.
3. If ffmpeg isn't on the Mac and Homebrew isn't either, it opens brew.sh: paste the one install
   command there into Terminal, then run the installer again.
4. Kickoff.app lands in your home Applications folder. Drag it to the Dock.

Kickoff updates itself from GitHub: the engine before every run, the window each time you open
the app (the new window shows the next time). Offline, it keeps what's installed. Running the
installer again also updates it.

## Use

Open Kickoff and drop the shoot folder on its window (or on the Dock icon, or click the drop area
to choose one). The window shows each clip as it syncs, then the result: clips synced per camera,
laid out along the song, and what was set aside and why. **Open in Premiere** opens the project
XML in Premiere (and selects it in Finder); if Premiere doesn't pick it up, use File > Import on
it, then drag the contents of the bin it creates up to the top level. **Show report** opens the
sync report. **Details** shows the engine's log.

Before dropping, the switch under the drop area picks the kind of job: **Auto** (a music video when
the folder has a song the clips line up with, otherwise a regular project), **Music video**, or
**Project setup only** (commercials and anything else: bins, Breakups and an empty Edit sequence,
no syncing). Kickoff remembers the choice.

The window is built on your Mac by the installer (it uses Apple's Command Line Tools). If that
build fails, the installer puts in the simple version instead: a folder picker, then a Terminal
window with progress.

Folder conventions it understands:
- the song: in a `Music` folder, or named master/song/mix, or the only audio file
- sound effects: anything under a folder called `SFX`
- everything else audio-only (boom, lav, recorder): Audio > Captured
- camera cards as they come off the card (A_CAM/..., B_CAM/..., DCIM/...)
