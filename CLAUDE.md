	# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An auto cue-point pipeline for DJ software.

Pipeline: ML detection (Python) → Serato Markers2 tags embedded in audio files (Rust CLI) → readable by VirtualDJ / Serato / Mixxx.

Two independent programs joined by a JSON contract:
1. **`detect_cues.py`** — a `uv run` script (PEP 723 inline deps in its header). Wraps [CUE-DETR](https://github.com/ETH-DISCO/cue-detr) (ISMIR'24) object-detection inference over mel-spectrogram images and emits `<stem>.cues.json` per track.
2. **`cue-fighter`** — a Rust CLI (`src/main.rs`, single file) that reads/writes `Serato Markers2` hot cues into audio files. Output is readable by Serato DJ, VirtualDJ, and Mixxx.

The JSON contract between them:
```json
{"cues": [{"index": 0, "ms": 32450, "color": "CC0000", "label": "Drop"}]}
```

`color` and `label` are optional. `index` is 0–15 (maps to hot cue slots 1–16; VirtualDJ exposes 16, Serato DJ only shows the first 8). The Python side only ever emits `index`/`ms`/`label`; `cue-fighter` fills color from a default palette.

## Commands

```sh
cargo build --release            # binary at target/release/cue-fighter
cargo run -- read <track>        # run CLI in dev

# detection (first run downloads the disco-eth/cue-detr checkpoint from HF)
uv run detect_cues.py <tracks> -o cues/     # -s sensitivity, -r radius, --max-cues

# write / inspect embedded serato tags
cue-fighter write <track> --json cues/track.cues.json
cue-fighter write <track> --cue "0:15000:CC0000:Intro" --cue "1:32450::Drop"
cue-fighter read <track>
cue-fighter write <track> --json cues.json --replace   # drop all existing CUE entries first
cue-fighter write <track> --json cues.json --dry-run   # preview, no file write
cue-fighter write <track> --json cues.json --backup    # snapshot cues to <track>.cuebak first
cue-fighter write <track> --toml cues.toml             # TOML input (as emitted by `export`)
cue-fighter write <track> --json cues.json --out-dir <DIR>   # write to a COPY (also copies .vdjstems)
cue-fighter write <track> --json cues.json --scrub --out-dir <DIR>  # strip ALL other tags from the copy first
cue-fighter undo <track>                               # restore cues from the .cuebak sidecar
cue-fighter batch <audio-dir> --json-dir <cues-dir> [--skip-existing] [--backup] [--dry-run] [--out-dir <DIR>]
cue-fighter export <path> --out <DIR>                  # checkpoint tags: raw .markers2 + .cues.toml
cue-fighter import <path> --from <DIR>                 # restore raw .markers2 tags verbatim

cargo test --release             # integration tests (tests/roundtrip.rs; ffmpeg-gated, else no-op)

# Phase 1 detection-quality tooling
uv run evaluate.py sweep <tracks> -s 0.3,0.5,0.7 -r 8,16      # cue-count table across a grid
uv run evaluate.py tune  --list tracks.txt -s 0.5,0.7,0.9     # recall/precision vs each track's manual cues

# visual QA — waveform + beatgrid + cues, playable in the browser
uv run report.py --list tracks.txt --json-dir cues/ -o report.html
uv run report.py --list tracks.txt --json-dir new/ --compare-dir old/ -o report.html  # regression diff

# tag inspector — what's embedded in the files (cues, rating, hashtags, raw tags)
uv run inspector.py ~/Music/Tracks -o inspect.html
uv run inspector.py ~/Music/Tracks --sidecar     # one <track>.flac.html beside each file
```

Integration tests live in `tests/roundtrip.rs` (drive the built binary via `CARGO_BIN_EXE_cue-fighter`; each test no-ops if ffmpeg is absent). No linters or CI configured; `cargo clippy` applies.

## Non-obvious constraints

- **Rust edition 2021, deps pinned** (`id3 =1.12.0`, `clap =4.4.18`) for
  compatibility with distro cargo (1.75). Don't bump to versions requiring
  edition2024 without checking the toolchain floor.

- **Serato tag preservation (`cue-fighter`).** The core invariant: writing cues must *only* touch `Marker::Cue` entries. Loops, track color, BPM lock, and unknown markers are always preserved — the code reads the existing `Markers2` tag, retains non-cue markers, removes only the cue indices being (re)written (or all cues under `--replace`), then re-serializes. All three write paths (`write`, `batch`, `undo`) funnel through the single `apply_cues()` function — keep the invariant there; don't reintroduce a second mutation path.

- **Backup/undo relies on the cue-only invariant.** `--backup` snapshots just the *cues* (as `{"cues":[...]}`) to `<file>.cuebak`, written only if absent (so repeated writes preserve the true original). `undo` restores them with an internal `replace`. This is a faithful revert *because* writes never touch non-cue markers — if that ever changes, backup must switch to raw tag bytes.

- **Serato tag padding.** Serato/triseratops require the `Markers2` blob to end with trailing null bytes. The code serializes once to a probe buffer to measure length, then sets `m2.size = len + 2` so the writer emits padding. Setting `size` wrong produces tags Serato won't parse. See the probe block in `apply_cues`. **`m2.size` must be set to serialized length + padding before writing.** triseratops only emits trailing nullbytes when `size > bytes_written`, and both its own parser and Serato require at least one trailing null after the base64 blob. Removing the probe-serialize step in `apply_cues` silently produces tags that fail to re-parse.

- **VirtualDJ tag recovery (`parse_markers2` fallback).** triseratops 0.0.3's FLAC envelope decoder assumes Serato's no-padding base64 convention; VirtualDJ writes the envelope with standard `=` padding, which triseratops mangles — ~8% of a real library (88/1108) failed to parse before this fallback. `parse_markers2` now: (1) tries triseratops natively; (2) for FLAC, `deenvelope_flac` decodes the outer envelope tolerantly (`tolerant_b64_decode`) and re-parses the inner via `parse_id3` (the inner base64+marker layer is identical across containers); (3) `reencode_inner` rebuilds a canonical inner for the few tags that also have `=` padding *inside* the inner base64 or a missing trailing nullbyte. This took the library to **0 parse errors**. A recovered tag self-heals to canonical Serato format on the next `write` (apply_cues re-serializes via triseratops). Keep the raw-payload path (`export`/`import` via `write_markers2_payload`) parse-free regardless — it's the byte-faithful backstop. `tolerant_b64_decode` drops a trailing char when `len % 4 == 1` (Serato's 'A' pad-avoidance trick) — don't remove that.

- **Two write paths, by design.** `apply_cues` (parses, edits only cues, re-serializes) is the cue-editing path. `write_markers2_payload` (writes raw bytes verbatim, no parse) is the byte-faithful restore path for `import`; `write_markers2_raw` serializes an `m2` then funnels through it. Keep the cue-only invariant in `apply_cues`; keep `import`/`export` raw.

- **Serato Markers2 is not the only cue source.** Real library files also carry Mixed In Key's `CUEPOINTS` (JSON: `{"cues":[{"name","time"}...],"source":"mixedinkey"}`), plus `BEATGRID`, `ENERGY`, `SERATO_BEATGRID`, `SERATO_AUTOGAIN`. Players (VirtualDJ especially) may read those in preference to ours, so `--replace` — which only clears Serato CUE entries — does NOT guarantee our cues are what the player shows. `scrub_metadata()` strips every tag (FLAC: VorbisComment/Application/Picture/CueSheet blocks; MP3: writes an empty ID3 tag) and is exposed as `--scrub`. **`--scrub` is hard-gated on `--out-dir`** so it can only ever touch a copy — keep that guard. Also note VDJ caches tags in its own database, so a path it has seen before can show stale cues regardless of file contents; test with a fresh filename.

- **Two HTML tools, deliberately separate.** `report.py` judges *detection
  quality* against manual cues, so it imports `detect_cues` — and `detect_cues`
  has `import torch` / `from transformers import ...` at module level, meaning
  every report drags in the whole ML stack. `inspector.py` reports *what is in
  the files* and must stay model-free (numpy + pillow + ffmpeg). Shared pieces
  (the `cue-fighter read` shim, time formatting, the player JS, `file://` URL
  construction) live in `cuehtml.py`, which must never import either of them.
  It's `inspector.py`, not `inspect.py`: a script's directory goes on `sys.path`
  and `inspect` is a stdlib module several dependencies import.

- **`file://` URLs have two silent killers**, both handled in `cuehtml.file_url`.
  (1) Never `Path.resolve()` a track path — `~/Music/Tracks` is commonly a
  symlink into iCloud (`~/Library/Mobile Documents/...`), and browsers cannot
  read TCC-protected `~/Library` at all, so resolving turns every link dead.
  Use `os.path.abspath`. (2) `#` is a fragment delimiter and must be quoted;
  `encodeURI` does *not* escape it, and a large share of a DJ library has a
  sharp in the key (`(126, F#m).flac`).

- **Mixed In Key units are inconsistent.** `CUEPOINTS` times are in
  **milliseconds**; `BEATGRID` beat times are in **seconds**. Cross-check
  against a known Serato cue when in doubt — MIK `54890.37` is the same drop as
  Serato's `54881` ms.

- **Container dispatch.** `detect()` in `src/main.rs` maps extension → `Container`: mp3/aif/aiff use ID3 GEOB frames (`triseratops` + `id3` crate), flac uses the `SERATO_MARKERS_V2` Vorbis comment (`metaflac`). MP4/M4A is unsupported (no atom writer crate). Adding a format means a new `Container` variant plus its read/write arms.

- **CUE-DETR magic numbers.** `OVERLAP`, `W_WIN`, `PADDING`, `SR` in `detect_cues.py` must match the model's training setup — do not change them. `SR=22050` and `n_fft=2048` feed the mel-spectrogram; the sliding-window geometry depends on these constants matching training exactly. Never touch them; they are not tuning knobs. Tuning knobs are `-s` (sensitivity) and `-r` (peak spacing). **`-r` is `find_peaks(distance=)` — spacing in candidate-detection count, NOT time or beats**; it cannot enforce musical spacing, which is why `--min-gap-beats` exists (default 4 = one bar; the library's own manual cues bottom out at exactly 4.0 beats). Pipeline order in `main()` matters: detect → snap to grid → `enforce_min_gap` → truncate to `--max-cues`. Truncating last is what lets 16 slots hold 16 *distinct* cues and restores late-track coverage.

- **Cue labels are derived, not predicted.** CUE-DETR is single-class (`num_labels: 1`) — it only predicts *where*. `label_cues()` assigns INTRO/OUTRO from position and DROP/BREAK/BUILD/CUT from the RMS energy step across the cue, over a two-bar window. `LABEL_COLORS` mirrors the reference library's convention (DROP `00CC00`, CUT `CC0044`, INTRO `CC0000`, BUILD `00CCCC`, BREAK/MIX IN `CCCC00`), each ≥99% consistent there. Beat grid is `librosa.beat.beat_track`; `bar_starts()` picks the strongest-onset phase as a downbeat stand-in — a real downbeat tracker (Beat This!, madmom) would be the upgrade. The model is a DETR (`facebook/detr-resnet-50` processor + `disco-eth/cue detr` weights) that detects cue markers as boxes; box centers are peak-picked (`scipy.find_peaks` with `height=sensitivity`, `distance=radius`) into cue times.

- **Only CUE entries are ours.** Loops, track color, BPMLOCK, and unknown
  markers must be preserved byte-faithfully on rewrite (round-trip through
  triseratops structs, never dropped).

- **Pinned dependencies.** Both sides pin versions deliberately (`id3 =1.12.0`, `clap =4.4.18` in Cargo.toml; `transformers==4.42.3`, `librosa==0.10.2.post1`, `numpy==1.26.4` in the script header). CUE-DETR is sensitive to the transformers/numpy versions; loosen with care.

- **Device selection.** `detect_cues.py` auto-selects cuda → mps → cpu. No GPU is required.

  
## Conventions

- Errors: `Result<_, String>` with context prefix (`"id3 read: ..."`) — fine
  at this size; don't introduce anyhow/thiserror without need.
- JSON schema is the contract between the two halves:
  `{"cues":[{"index":u8,"ms":u32,"color":"RRGGBB"?,"label":str?}]}`.
  Changes must land in `detect_cues.py`, `JsonCue`, and README together.
  `detect_cues.py` also writes a sibling `"meta"` object (detection settings +
  timestamp) for report provenance/regression tracking; serde ignores unknown
  fields so the Rust side is unaffected — don't add `deny_unknown_fields`.
- Deterministic marker order on write: non-cue markers first, then cues by
  index. Keeps diffs/tests stable.
- `write` prints human summary to stderr, machine output (JSON) to stdout.

## Domain notes

- ID3: GEOB frame, desc `Serato Markers2`, mime `application/octet-stream`,
  payload = `\x01\x01` + base64 (newline every 72 chars) + null padding.
- FLAC: Vorbis comment `SERATO_MARKERS_V2`, enveloped
  (`application/octet-stream\0\0<name>\0` + inner payload, base64).
- MP4/M4A would use freeform atom `----:com.serato.dj:markersv2` — not
  implemented (needs an mp4 writer crate; triseratops has the serializer).
- VDJ caches tags in its database: existing tracks need a tag reload
  (File Info) after writing; fresh imports pick cues up automatically.
- Format spec reference: Holzhaus serato-tags repo; triseratops is the
  reference implementation (also used by Mixxx).

## Verification standard

Any change to tag serialization must pass both:
1. round-trip through `cue-fighter read`, and
2. independent decode with mutagen + hand parser against the spec
   (see smoke-test approach; script lives in PLAN.md history / recreate ad hoc).
Never trust triseratops-parses-its-own-output alone.