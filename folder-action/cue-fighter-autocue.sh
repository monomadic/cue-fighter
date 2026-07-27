#!/usr/bin/env bash
# Detect cue points for one or more audio files and write them into the file's
# Serato Markers2 tag, so VirtualDJ picks them up on first import.
#
# Two ways in:
#   1. As the MIK_AUTOCUE_SCRIPT hook of mik-folder-action (runs after Mixed In
#      Key, before the copy to Lexicon).
#   2. As its own folder action, for the live case where MIK is not in the path.
set -uo pipefail

# Folder Actions run with a bare PATH. ~/.local/bin is where uv installs itself,
# and it is not on the default one.
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

CONFIG_FILE="${CUE_FIGHTER_CONFIG:-$HOME/.config/cue-fighter/env}"
if [[ -f "$CONFIG_FILE" ]]; then
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
fi

REPO_DIR="${CUE_FIGHTER_REPO:-$HOME/src/cue-fighter}"
BIN="${CUE_FIGHTER_BIN:-$REPO_DIR/target/release/cue-fighter}"
DETECT="${CUE_FIGHTER_DETECT:-$REPO_DIR/detect_cues.py}"
SENSITIVITY="${CUE_FIGHTER_SENSITIVITY:-0.7}"
RADIUS="${CUE_FIGHTER_RADIUS:-16}"
MAX_CUES="${CUE_FIGHTER_MAX_CUES:-16}"
SNAP="${CUE_FIGHTER_SNAP:-beat}"
MIN_GAP="${CUE_FIGHTER_MIN_GAP_BEATS:-4}"
# in-place is the point: VDJ only fills EMPTY pads from tags, so the file must be
# tagged before VDJ ever indexes it. Set OUT_DIR to write to a copy instead.
OUT_DIR="${CUE_FIGHTER_OUT_DIR:-}"
BACKUP="${CUE_FIGHTER_BACKUP:-1}"
SKIP_EXISTING="${CUE_FIGHTER_SKIP_EXISTING:-1}"
NOTIFY="${CUE_FIGHTER_NOTIFY:-1}"
KEEP_JSON="${CUE_FIGHTER_KEEP_JSON:-1}"
JSON_DIR="${CUE_FIGHTER_JSON_DIR:-$HOME/.local/state/cue-fighter/cues}"

LOG_DIR="$HOME/Library/Logs/CueFighterFolderAction"
LOG_FILE="$LOG_DIR/cue-fighter-autocue.log"
mkdir -p "$LOG_DIR"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

notify() {
  [[ "$NOTIFY" == "1" ]] || return 0
  /usr/bin/osascript - "$1" "$2" <<'OSA' >/dev/null 2>&1 || true
on run argv
  display notification (item 2 of argv) with title (item 1 of argv)
end run
OSA
}

alert() {
  /usr/bin/osascript - "$1" "$2" <<'OSA' >/dev/null 2>&1 || true
on run argv
  display alert (item 1 of argv) message (item 2 of argv) buttons {"OK"} default button "OK"
end run
OSA
}

is_audio_file() {
  local ext
  ext="$(printf '%s' "${1##*.}" | tr '[:upper:]' '[:lower:]')"
  case "$ext" in
    flac | aif | aiff | mp3) return 0 ;;
    # m4a/wav carry no Serato Markers2 writer yet — skip rather than fail loudly
    *) return 1 ;;
  esac
}

collect_audio_files() {
  local item
  for item in "$@"; do
    if [[ -d "$item" ]]; then
      /usr/bin/find "$item" -type f \( -iname '*.flac' -o -iname '*.aif' \
        -o -iname '*.aiff' -o -iname '*.mp3' \) -print0
    elif [[ -f "$item" ]] && is_audio_file "$item"; then
      printf '%s\0' "$item"
    fi
  done
}

wait_until_file_stable() {
  local file="$1" previous="" current=""
  local deadline=$((SECONDS + 120))
  while ((SECONDS < deadline)); do
    current="$(/usr/bin/stat -f '%z:%m' "$file" 2>/dev/null)" || return 1
    [[ "$current" == "$previous" ]] && return 0
    previous="$current"
    sleep 2
  done
  return 1
}

has_cues() {
  local n
  n="$("$BIN" read "$1" 2>/dev/null | /usr/bin/grep -c '"index"')" || return 1
  ((n > 0))
}

process_file() {
  local file="$1" stem json out n
  stem="$(basename "${file%.*}")"

  wait_until_file_stable "$file" || {
    log "not stable, skipping: $file"
    return 1
  }

  if [[ "$SKIP_EXISTING" == "1" ]] && has_cues "$file"; then
    log "already has cues, skipping: $file"
    return 0
  fi

  mkdir -p "$JSON_DIR"
  json="$JSON_DIR/$stem.cues.json"

  log "detecting: $file"
  if ! uv run "$DETECT" "$file" -o "$JSON_DIR" \
      -s "$SENSITIVITY" -r "$RADIUS" --max-cues "$MAX_CUES" \
      --snap "$SNAP" --min-gap-beats "$MIN_GAP" >>"$LOG_FILE" 2>&1; then
    log "detection FAILED: $file"
    return 1
  fi
  [[ -f "$json" ]] || {
    log "no cues json produced: $file"
    return 1
  }

  local -a args=(write "$file" --json "$json")
  [[ "$BACKUP" == "1" ]] && args+=(--backup)
  [[ -n "$OUT_DIR" ]] && { mkdir -p "$OUT_DIR"; args+=(--out-dir "$OUT_DIR"); }

  if ! "$BIN" "${args[@]}" >>"$LOG_FILE" 2>&1; then
    log "write FAILED: $file"
    return 1
  fi

  out="${OUT_DIR:+$OUT_DIR/}$(basename "$file")"
  n="$(/usr/bin/grep -c '"ms"' "$json" 2>/dev/null || printf 0)"
  log "wrote $n cue(s) -> ${out:-$file}"
  [[ "$KEEP_JSON" == "1" ]] || rm -f "$json"
  notify "Cues written" "$(basename "$file") · $n cues"
  return 0
}

main() {
  local file processed=0 failed=0

  for tool in "$BIN" "$DETECT"; do
    [[ -e "$tool" ]] || {
      alert "cue-fighter" "Not found: $tool
Build it with: cargo build --release  (or set CUE_FIGHTER_BIN in $CONFIG_FILE)"
      log "missing dependency: $tool"
      exit 1
    }
  done
  command -v uv >/dev/null || {
    alert "cue-fighter" "uv is not installed or not on PATH."
    log "missing uv"
    exit 1
  }

  (($# == 0)) && { log "no files passed"; exit 0; }

  while IFS= read -r -d '' file; do
    processed=$((processed + 1))
    process_file "$file" || failed=$((failed + 1))
  done < <(collect_audio_files "$@")

  ((processed == 0)) && log "no supported audio files in input"
  ((failed > 0)) && alert "cue-fighter" "$failed of $processed file(s) failed.
See: $LOG_FILE"
  return 0
}

main "$@"
