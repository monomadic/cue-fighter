# Auto-cue folder action

Drop a track in a folder, get it back tagged with hot cues, before VirtualDJ
ever sees it. Detection (`detect_cues.py`) then write (`cue-fighter write`),
driven by a macOS Folder Action.

```sh
folder-action/install-folder-action.sh                      # ~/Music/Auto Cue Drop
folder-action/install-folder-action.sh "$HOME/Music/Incoming"
```

Config lands at `~/.config/cue-fighter/env`, log at
`~/Library/Logs/CueFighterFolderAction/cue-fighter-autocue.log`.

## Two chains

**Live / no Mixed In Key.** Install as above and drop files straight in. Nothing
external is required — detection is local.

**After Mixed In Key.** The existing `mik-folder-action` has a hook for exactly
this. Point it at the **in-place** variant — that chain copies the *original*
file on to Lexicon after the hook runs, so cues must go into the file itself or
Lexicon imports an untagged track while the cued copy sits orphaned. In
`~/.config/mik-folder-action/env`:

```sh
MIK_AUTOCUE_SCRIPT="$HOME/Library/Application Scripts/com.nom.cue-fighter/cue-fighter-autocue-inplace.sh"
```

## Behaviour

- **Writes a cued copy to `~/Music/cue-fighter`**, leaving the original
  untouched (`CUE_FIGHTER_OUT_DIR`). Point Lexicon at that folder: VirtualDJ
  only fills *empty* pads from tags, so it has to import the copy, not the
  original. Set the variable to `""` to write in place instead.
- **Backs up first when writing in place** (`<file>.cuebak`, written only if
  absent, so it holds the true original); `cue-fighter undo <file>` reverts.
  Skipped when writing to a copy — there is nothing to protect.
- **Skips files that already carry cues**, so re-drops and re-scans are cheap.
  `CUE_FIGHTER_SKIP_EXISTING=0` to force.
- **Waits for the file to stop changing** before touching it, so a copy still in
  flight is not analysed half-written.
- Only `.flac`, `.mp3`, `.aif`, `.aiff` are processed — there is no Serato
  Markers2 writer for m4a/wav yet, so those are skipped rather than failed.
- The `.cues.json` is kept in `~/.local/state/cue-fighter/cues` for later
  inspection with `report.py`.

Folder Actions run with a bare environment, so the helper sets its own PATH —
including `~/.local/bin`, where `uv` installs itself and which is not on the
default PATH.
