-- Kickoff: drop a shoot folder on this app to build a Premiere project XML (bins, sync, breakup,
-- edit sequence) and a sync report inside it. Double-click to pick a folder instead.

property supportDir : ""

on run
	set f to choose folder with prompt "Choose the shoot folder to set up:"
	processFolder(f)
end run

on open theItems
	repeat with f in theItems
		processFolder(f)
	end repeat
end open

on processFolder(f)
	set supportDir to (POSIX path of (path to application support folder from user domain)) & "Kickoff/"
	set p to POSIX path of f
	if p does not end with "/" then
		display dialog "Drop a folder, not a file." buttons {"OK"} default button 1 with icon caution
		return
	end if
	set runner to quoted form of (supportDir & "kickoff-run.sh")
	set cmd to "clear; /bin/bash " & runner & " " & quoted form of p
	tell application "Terminal"
		activate
		do script cmd
	end tell
end processFolder
