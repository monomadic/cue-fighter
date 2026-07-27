on adding folder items to this_folder after receiving added_items
	set helperPath to (POSIX path of (path to home folder)) & "Library/Application Scripts/com.nom.cue-fighter/cue-fighter-autocue.sh"
	set commandText to "/usr/bin/nohup " & quoted form of helperPath

	repeat with addedItem in added_items
		set commandText to commandText & " " & quoted form of (POSIX path of addedItem)
	end repeat

	do shell script commandText & " >/dev/null 2>&1 &"
end adding folder items to
