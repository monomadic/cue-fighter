#!/usr/bin/env bash
# In-place variant, for use as mik-folder-action's MIK_AUTOCUE_SCRIPT.
#
# That chain copies the *original* file on to Lexicon after the hook runs, so
# cues have to go into the file itself — a copy written to CUE_FIGHTER_OUT_DIR
# would be orphaned and Lexicon would import the untagged original.
set -uo pipefail
export CUE_FIGHTER_OUT_DIR=""
exec "$(dirname -- "${BASH_SOURCE[0]}")/cue-fighter-autocue.sh" "$@"
