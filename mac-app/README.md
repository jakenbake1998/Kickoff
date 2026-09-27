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

Running the installer again updates the engine.

## Use

Drop the shoot folder on Kickoff (or double-click it and pick the folder). A Terminal window shows
progress; the first time, macOS asks to let Kickoff control Terminal, click OK. When it finishes
the `Premiere Sync` folder opens. In Premiere: File > Import the `.xml`, then drag the contents of
the bin it creates up to the top level.

Folder conventions it understands:
- the song: in a `Music` folder, or named master/song/mix, or the only audio file
- sound effects: anything under a folder called `SFX`
- everything else audio-only (boom, lav, recorder): Audio > Captured
- camera cards as they come off the card (A_CAM/..., B_CAM/..., DCIM/...)
