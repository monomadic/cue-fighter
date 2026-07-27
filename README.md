# cue-fighter

Auto cue point pipeline: CUE-DETR detection → Serato `Markers2` tags embedded
in the audio file → readable by VirtualDJ, Serato, Mixxx.

Two parts:

- `cue-fighter` — Rust CLI that reads/writes `Serato Markers2` hot cues
  (MP3/AIFF via ID3 GEOB, FLAC via `SERATO_MARKERS_V2` Vorbis comment),
  built on [triseratops]. Preserves existing loops, track color, BPM lock,
  and unknown markers; only touches CUE entries.
- `detect_cues.py` — `uv run` script wrapping [CUE-DETR] (ISMIR'24) inference,
  emitting `{stem}.cues.json` per track.

[triseratops]: https://crates.io/crates/triseratops
[CUE-DETR]: https://github.com/ETH-DISCO/cue-detr

## Build & install

```sh
cargo build --release          # binary at target/release/cue-fighter
cargo install --path .         # install cue-fighter onto your PATH (~/.cargo/bin)
```

## Usage

```sh
# detect (first run downloads the disco-eth/cue-detr checkpoint from HF)
uv run detect_cues.py track.mp3 -o cues/

# write into the file
cue-fighter write track.mp3 --json cues/track.cues.json

# or by hand: IDX:MS[:RRGGBB[:LABEL]]
cue-fighter write track.mp3 --cue "0:15000:CC0000:Intro End" --cue "1:32450::Drop"

# inspect
cue-fighter read track.mp3

# nuke-and-rewrite all CUE entries (loops etc. still preserved)
cue-fighter write track.mp3 --json cues.json --replace

# preview without touching the file
cue-fighter write track.mp3 --json cues.json --dry-run

# take a one-shot undo snapshot before writing, then roll it back
cue-fighter write track.mp3 --json cues.json --backup
cue-fighter undo track.mp3     # restores the pre-write cue set, removes the .cuebak sidecar

# safe mode: never touch the original — write to a COPY (also copies the .vdjstems sidecar)
cue-fighter write track.flac --json cues.json --out-dir "~/Music/Tracks - CueFighter"

# clean slate: strip ALL existing tags from the copy first, so our cues are the
# only metadata in the file (see "competing cue sources" in Notes)
cue-fighter write track.flac --json cues.json --scrub --out-dir "~/Music/Tracks - CueFighter"
```

Batch a whole folder — matches `<audio-dir>/<stem>` to `<json-dir>/<stem>.cues.json`
(the layout `detect_cues.py -o` produces):

```sh
uv run detect_cues.py ~/Music/incoming/*.mp3 -o /tmp/cues
cue-fighter batch ~/Music/incoming --json-dir /tmp/cues --backup

# only cue tracks that don't already have cues; preview first
cue-fighter batch ~/Music/incoming --json-dir /tmp/cues --skip-existing --dry-run

# safe mode across a folder: originals untouched, cues written to copies
cue-fighter batch ~/Music/incoming --json-dir /tmp/cues --out-dir "~/Music/Tracks - CueFighter"
```

### Visual QA report

Renders a self-contained HTML page: waveform with the beat/bar grid baked in,
cue markers overlaid, and an audio player — click the waveform to seek, click a
cue chip to jump to it. Detected cues are shown alongside the track's existing
manual cues, so you can judge placement without involving a DJ app.

```sh
uv run report.py --list tracks.txt --json-dir cues/ -o report.html

# regression check: diff a new run against an older one
uv run report.py --list tracks.txt --json-dir new/ --compare-dir old/ \
    --compare-name "before snapping" -o report.html
```

Audio is *linked*, not embedded (FLACs are large), so keep the tracks in place
and open the report locally. Each `.cues.json` carries a `meta` block recording
the settings that produced it, which the report displays for provenance.

### Tag inspector

`report.py` answers "did detection do a good job?". `inspector.py` answers "what
is actually in these files?" — hot cues from the file's own Serato tag, star
rating, `#hashtags` from the grouping field, cover art, Mixed In Key's beatgrid /
cuepoints / energy, and the complete raw tag dump, as one self-contained page.
It never loads a model (numpy, pillow and ffmpeg are the whole dependency set).

```sh
uv run inspector.py ~/Music/Tracks -o inspect.html      # files, dirs, or --list
uv run inspector.py ~/Music/Tracks --sidecar            # one <track>.flac.html each
uv run inspector.py ~/Music/Tracks --no-wave            # skip waveforms, much faster
```

One row per file: cover, rating, bpm/key, pad count, hashtags, a tag-quality
score, and a waveform whose colour follows the spectral mix per 4-bar phrase
(bass-led warm, mid/vocal violet, air-led cyan). Click the waveform to seek or a
cue marker to play from it; click a row to expand the cue table and full tag
dump. Filter by rating or hashtag, sort by name/score/rating/bpm/cue count.

`--sidecar` writes a page next to each track, expanded, with a *relative* audio
path so a track and its sidecar survive being moved together. As with the report,
audio is linked rather than embedded; graphics are inlined as base64.

### Checkpoint & restore your existing tags

Back up every track's Serato Markers2 tag before letting anything write. Each
track gets a byte-faithful raw `<name>.markers2` (restorable verbatim) plus a
human-readable `<name>.cues.toml`:

```sh
cue-fighter export ~/Music/Tracks --out ~/cue-checkpoint     # a file or a directory
cue-fighter import ~/Music/Tracks --from ~/cue-checkpoint    # restore raw tags verbatim
```

The raw `.markers2` backup works even for tags this tool can't otherwise decode
(see Notes). `write` can also re-apply an edited `.cues.toml`:

```sh
cue-fighter write track.flac --toml ~/cue-checkpoint/track.flac.cues.toml --replace
```

## Notes

- Cue indices 0–15 map to VirtualDJ hot cues 1–16. Default colors follow
  a fixed 16-color palette when unspecified. (Serato DJ itself only displays
  the first 8; VirtualDJ, the primary target, exposes all 16.)
- VDJ caches tags in its database: after writing, right-click → File Info →
  reload tags (or remove/re-add) for existing tracks. Fresh imports pick the
  cues up automatically. Make sure "read Serato markers" is enabled in VDJ
  options. A file at a path VDJ has already seen may show *cached* cues no
  matter what the tags say — test with a fresh filename to be sure.
- **Competing cue sources.** `Serato Markers2` is not the only place cues live.
  Real library files also carry Mixed In Key's `CUEPOINTS` (a JSON blob of
  cue name/time pairs) plus `BEATGRID`, `ENERGY`, `SERATO_BEATGRID` and
  `SERATO_AUTOGAIN`. Players may prefer those over ours, so `--replace` alone
  (which only clears Serato CUE entries) is not enough to guarantee your cues
  are what shows up. Use `--scrub` to strip everything else first. `--scrub`
  requires `--out-dir`, so it can never touch a library original.
- Writes are in-place tag rewrites; audio frames are untouched. Test on copies
  first anyway (or use `--out-dir` to always write to copies).
- Detection knobs: `-s` sensitivity (default 0.9, lower = more cues) and `-r`,
  which is `find_peaks(distance=)` — spacing in *candidate detections*, not time.
  Musical spacing comes from `--min-gap-beats` instead (default 4 = one bar,
  chosen because the reference library's manual cues bottom out at 4.0 beats).
- Cues are snapped to the beat grid by default (`--snap beat`, also `bar` or
  `off`). Pipeline order is detect → snap → drop-too-close → truncate to
  `--max-cues`, so the 16 slots hold 16 *distinct* cues. `--snap bar` measured
  much worse against the reference library (recall 18% vs 46%) — those cues sit
  on many beats, not just downbeats — so beat-snap is the default.
- Grid type: `--grid quantized` (default) builds a rigid constant-tempo grid
  (`phase + k·60/bpm`, like a DJ beatgrid), phase-fitted to the audio; `--grid
  detected` snaps to librosa's raw per-beat estimates, whose interval wobbles.
- Cues are labelled INTRO / BUILD / DROP / BREAK / CUT / OUTRO from position and
  local RMS energy, coloured to match the reference library's convention
  (`--no-labels` restores plain `cue`). CUE-DETR itself is single-class and
  predicts position only — the labels are derived here, not by the model.
- **VirtualDJ tag compatibility.** triseratops 0.0.3 can't decode ~8% of a real
  library (VirtualDJ writes the FLAC Markers2 envelope with `=` padding that
  triseratops mangles). cue-fighter falls back to decoding the envelope itself,
  taking a real test library from 88 unreadable tracks to **0**. A recovered
  tag self-heals to canonical Serato format the next time it's written. The raw
  `.markers2` checkpoint/`import` path is parse-free and byte-faithful either way.

## Possible next steps

- Snap detected cues to the downbeat grid (SongFormer/Beat This!) before
  writing.
- Label cues from SongFormer section types (intro/chorus/...) instead of "cue".
- MP4/M4A support (triseratops has the format; needs an mp4 atom writer crate).
