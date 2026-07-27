on run argv
	if (count of argv) is less than 2 then
		error "Usage: attach-folder-action.applescript WATCH_FOLDER ACTION_SCRIPT_PATH"
	end if
	
	set watchPath to item 1 of argv
	set actionScriptPath to item 2 of argv
	set alreadyAttached to false
	
	tell application "System Events"
		set folder actions enabled to true
		
		repeat with folderAction in folder actions
			if (path of folderAction) is watchPath then
				repeat with folderScript in scripts of folderAction
					if (POSIX path of folderScript) is actionScriptPath then
						set alreadyAttached to true
					end if
				end repeat
			end if
		end repeat
		
		tell folder watchPath
			if alreadyAttached is false then
				attach action to it using actionScriptPath
			end if
		end tell
	end tell
end run
