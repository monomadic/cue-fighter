#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WATCH_FOLDER="${1:-$HOME/Music/Auto Cue Drop}"

HELPER_DIR="$HOME/Library/Application Scripts/com.nom.cue-fighter"
HELPER_TARGET="$HELPER_DIR/cue-fighter-autocue.sh"
CONFIG_DIR="$HOME/.config/cue-fighter"
CONFIG_TARGET="$CONFIG_DIR/env"
FOLDER_ACTION_DIR="$HOME/Library/Scripts/Folder Action Scripts"
ACTION_NAME="Auto Cue.scpt"
ACTION_TARGET="$FOLDER_ACTION_DIR/$ACTION_NAME"

mkdir -p "$WATCH_FOLDER" "$HELPER_DIR" "$CONFIG_DIR" "$FOLDER_ACTION_DIR"
WATCH_FOLDER="$(cd "$WATCH_FOLDER" && pwd -P)"

/usr/bin/install -m 755 "$SCRIPT_DIR/cue-fighter-autocue.sh" "$HELPER_TARGET"
/usr/bin/install -m 755 "$SCRIPT_DIR/cue-fighter-autocue-inplace.sh" "$HELPER_DIR/"
if [[ ! -f "$CONFIG_TARGET" ]]; then
  /usr/bin/install -m 644 "$SCRIPT_DIR/default.env" "$CONFIG_TARGET"
fi

/usr/bin/osacompile -o "$ACTION_TARGET" "$SCRIPT_DIR/Auto Cue.applescript"
/usr/bin/osascript "$SCRIPT_DIR/attach-folder-action.applescript" "$WATCH_FOLDER" "$ACTION_TARGET"

printf 'Installed cue-fighter folder action.\n'
printf 'Drop folder:   %s\n' "$WATCH_FOLDER"
printf 'Folder action: %s\n' "$ACTION_TARGET"
printf 'Helper:        %s\n' "$HELPER_TARGET"
printf 'Config:        %s\n' "$CONFIG_TARGET"
printf 'Log:           %s\n' "$HOME/Library/Logs/CueFighterFolderAction/cue-fighter-autocue.log"
printf 'Cued copies:   %s\n' "${CUE_FIGHTER_OUT_DIR:-$HOME/Music/cue-fighter}"
printf '\nTo chain it after Mixed In Key instead, set in ~/.config/mik-folder-action/env:\n'
printf '  MIK_AUTOCUE_SCRIPT="%s/cue-fighter-autocue-inplace.sh"\n' "$HELPER_DIR"
printf '  (the in-place variant: that chain copies the original onward itself)\n'
