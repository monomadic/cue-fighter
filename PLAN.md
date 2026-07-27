# PLAN.md — cue-fighter

Goal: fully automatic, taste-matching cue points written as Serato tags,
picked up by VirtualDJ. Detection quality first, plumbing second, models last.

## Status

- [x] Rust CLI `cue-fighter`: read/write Serato Markers2 (MP3/AIFF GEOB, FLAC
      Vorbis comment) via triseratops. Round-trip + independent spec-level
      decode verified. Preserves loops/color/bpmlock.
- [x] `detect_cues.py`: CUE-DETR inference (MPS/CPU), emits per-track
      `{stem}.cues.json` in the shared schema.
- [x] 16 hot cues end-to-end (index 0–15, 16-color palette). VDJ exposes 16;
      Serato DJ shows the first 8. Round-trip verified on a real MP3.
- [ ] Everything below.

## Phase 1 — validate detection quality (gate for all else)

- [x] `evaluate.py score` (no model): scores a finished cue dir vs manual cues
      on position / phase / label. On a 24-track sample it caught that the
      shipped `--snap bar` default was halving recall (18% vs 46% for beat/off)
      by dragging cues to downbeats the user doesn't cue on → **default changed
      to `--snap beat`**. Also showed phase is fine (median offset <0.12 beat,
      so a downbeat tracker is NOT the priority), position tops ~46% recall, and
      labels split: DROP/INTRO/BREAK ~70-77% right, BUILD 8%, and
      MIX IN/ENERGY/CHORUS/VERSE 0% (heuristic can't emit them).


- [x] Harness: `evaluate.py sweep` (cue-count table across a -s/-r grid, model
      runs once per track) and `evaluate.py compare` (recall/precision of
      predicted vs manual cues, 1-beat tolerance from BPM in the filename).
      `detect_cues.detect()` split into `raw_detections()` + `peak_pick()` so
      the sweep re-picks without re-running the model.
- [x] Ran `evaluate.py tune` on an 18-track BPM-spanning sample (82–180),
      scored vs the user's own manual cues (902 labelled tracks available as
      ground truth). Best setting: **-s 0.7 -r 16 → 45% recall, 44% precision**.
      Lower -s just floods 16 cues without lifting recall; higher -s (0.9) is
      precise but sparse (~5 cues).
- [x] Per-track: quality is mediocre **uniformly**, NOT worse outside EDM —
      the house/EDM core (125–140) scored *lower* (27–35%) than several
      non-EDM tracks (82→50%, 86→60%, 175→82%). The "poor outside EDM"
      framing was wrong.
- [x] Off-grid vs missing: widening tolerance 1→2→4 beats moved recall only
      45→47→50%. The missed cues are genuinely absent from predictions, not
      near-misses → **downbeat snapping alone (Phase 3) will NOT close the
      coverage gap.**
- DECISION (resolved): CUE-DETR tops out ~45–50% recall against the user's
  dense, personal cue style (MIX IN / CUT / BUILD / PRE-DROP). Knob-tuning is
  exhausted. Next investment should be structure-aware detection (Phase 4)
  and/or a fine-tune on the user's 902 labelled tracks (Phase 5), NOT more
  CUE-DETR tuning. Phase 3 snapping remains worthwhile for playability but is
  not the coverage fix.

## Phase 1b — the embedded-MIK baseline (measured 2026-07-27)

Found via the tag inspector: most library files already carry Mixed In Key
`CUEPOINTS` and `BEATGRID`. Measured on 194 tracks with both MIK cues and manual
cues (1,733 manual cues), 1-beat tolerance, no model involved:

- **MIK cuepoints alone: 55% recall / 64% precision** — better than CUE-DETR's
  tuned best (45% / 44%). Coverage: MIK cuepoints in 76% of files, beatgrid 75%,
  manual cues 91%. Mean 8.8 manual cues vs 7.6 MIK cuepoints per track.
- **93% of the hits land within 50 ms**, so these manual cues were largely
  *derived from* MIK, not independently placed. Predicting them is partly
  circular — the real target is the residual.
- MIK's beatgrid (tempo + beat list) is a better grid than librosa's tracker and
  is free, which changes the calculus on Phase 3's downbeat-tracker item.

**The residual (781 of 1,733 cues) is mostly geometry around drops**, by label —
total / MIK-covered / residual:

| label | n | MIK hit rate | | label | n | MIK hit rate |
|---|---|---|---|---|---|---|
| DROP | 384 | 73% | | OUTRO | 111 | 14% |
| CUT | 200 | **6%** | | MIX IN | 90 | 61% |
| ENERGY | 196 | 96% | | CHORUS | 79 | 59% |
| INTRO | 181 | 70% | | BREAK | 76 | 68% |
| BUILD | 133 | 41% | | VERSE | 61 | 51% |

- **CUT is the biggest single gap and is near-deterministic**: 197 of 215 CUTs
  are immediately followed by a DROP, at a quantised offset — 4 beats (66%) or
  8 beats (25%) before it. But only 43% of drops have one, so a blanket rule
  over-fires ~2x.
- **BUILD**: 32 beats before the drop (49%) or 16 (18%); present before 26% of
  drops.
- **OUTRO**: median 92% of track position (10th–90th pct: 84–98%).
- MIK's `Energy N` numbers do *not* identify labels — best signal is a +2 energy
  step → DROP only 45% of the time. MIK gives positions, not structure.

**Oracle test of "derive instead of detect" — mostly negative.** Given the
*true* drops (an upper bound no detector can beat), adding geometric satellites
under real pipeline constraints (1-bar min gap, 16-pad cap, 1-beat tolerance):

| source | recall | precision |
|---|---|---|
| MIK alone | 54.4% | 64.4% |
| MIK + CUT@4 | 59.1% | 57.4% |
| MIK + CUT@4 + BUILD@32 | 61.3% | 52.5% |
| MIK + CUT@4,8 + BUILD@32 + OUTRO | 61.2% | 44.9% |
| satellites only | 13.6% | 25.3% |
| oracle drops only | 22.2% | 100% |

So derivation buys ~+5 recall for ~−7 precision *with perfect drops*, and the
fourth row is strictly harmful — extra guesses displace real cues under the
16-pad cap. The earlier "197 of 215 CUTs sit 4–8 beats before a drop" is a fact
about CUTs, not about drops: only **43% of drops carry a CUT**, and that rate is
flat across drop index (45/43/48/39%), track position and inter-drop spacing —
no feature tested predicts *which* drops get one.

**Implication.** Placement of a CUT is solved (it is 4 or 8 beats before the
drop, 91% of the time). The unsolved half is the *decision* to place one — a
small, well-posed binary classification over drops, with ~8k labelled examples
in the library, rather than a general detection problem. Satellite derivation
ships as an opt-in flag, not a default.

## Phase 2 — pipeline hardening

- [x] `cue-fighter batch` subcommand: dir of audio + dir of json, matched by
      stem; `--skip-existing` (skips files already carrying cues), `--backup`,
      `--dry-run`, `--replace`. All write paths share one `apply_cues()`.
- [x] Backup mode: `--backup` snapshots the pre-write cue set to
      `<file>.cuebak` (written once, so it holds the true original); `undo`
      subcommand restores it. Faithful revert verified against a pristine
      library original (byte-identical read).
- [x] Integration tests (`tests/roundtrip.rs`): mp3/flac 16-cue round-trip,
      backup→undo, batch + skip-existing. Drive the built binary via
      `CARGO_BIN_EXE_cue-fighter`; ffmpeg-gated (no-op without it). 4 passing.
      NOTE: independent mutagen/byte-fixture decode still only done ad hoc, not
      wired into `cargo test` (mutagen is Python). See Verification standard.
- [x] `cargo install --path .` documented in README.
- [x] Safe copy-on-write: `--out-dir` on `write`/`batch` writes to a COPY in the
      given dir (originals never touched) and copies the `<name>.vdjstems`
      sidecar alongside. User default target: `~/…/Music/Tracks - CueFighter`.
- [x] Tag checkpoint: `export <path> --out <dir>` writes a byte-faithful raw
      `<name>.markers2` (parse-free) plus a human-readable `<name>.cues.toml`;
      `import <path> --from <dir>` restores raw tags verbatim. TOML is also a
      `write` input (`--toml`). Round-trip byte-identical, verified on real
      library files incl. an unparseable one. Tests in tests/roundtrip.rs (7).
- [x] **RESOLVED — VDJ tag recovery.** Root cause: triseratops 0.0.3's FLAC
      envelope decoder assumes Serato's no-pad base64; VDJ uses standard `=`
      padding, which it mangles. `parse_markers2` now falls back to decoding the
      envelope ourselves (`deenvelope_flac`) and re-parsing the inner via
      `parse_id3`, plus `reencode_inner` for the few with `=` padding inside the
      inner base64 or a missing terminator. **Library went 88 → 0 parse errors**
      (990 tracks now read; the lone holdout is a WAV misnamed `.flac`).
      Non-cue markers preserved through recovery; recovered tags self-heal to
      canonical Serato format on next write. Unit tests guard both paths.

## Phase 3 — musically-aligned cues

- [x] Snap cue times to the grid: `--snap bar|beat|off` (default bar).
      `--grid quantized` (default) builds a rigid constant-tempo grid
      (`phase + k·60/bpm`), phase-fitted to librosa beats via circular mean —
      cues land within ~1ms of a true grid line (vs up to ~230ms for the raw
      `detected` beats). `bar_starts()` picks the downbeat phase by onset energy.
- [x] Minimum spacing `--min-gap-beats` (default 4 = 1 bar), applied after
      snapping. Killed every sub-beat duplicate on an 8-track sample (was 2–5
      per track) and, by truncating last, lifted coverage from ~61% to 96–99%
      of track length on several.
- [ ] Upgrade the grid to a real downbeat tracker (Beat This! / madmom /
      Beat Transformer). librosa's tracker + onset-phase heuristic is the weak
      link now; a proper one also gives the labeller true bar alignment.
- [ ] Alternative grid source: read VDJ's own beatgrid from its database.xml.
- [ ] Optional `Serato BeatGrid` tag writing (triseratops supports it) so the
      grid travels with the file too.

## Phase 4 — structure-aware labels (SongFormer)

- [x] First pass, heuristic (no new model): `label_cues()` assigns
      INTRO/OUTRO by position and DROP/BREAK/BUILD/CUT by the RMS energy step
      across each cue over a two-bar window. Colours mined from the library's
      own convention (each ≥99% consistent). `--no-labels` opts out.
- [ ] Replace the heuristic with real structural segmentation (SongFormer, or
      an all-in-one beat+downbeat+segment model). The heuristic cannot produce
      MIX IN / CHORUS / VERSE, and leans heavily on CUT as a catch-all.
- [ ] Validate labels against the library's 8,273 hand-labelled cues — the
      `evaluate.py tune` harness already matches by time; extend it to score
      label agreement.
- [ ] Fallback cue generation purely from structure boundaries for genres
      where CUE-DETR underperforms (Phase 1 decision).

## Phase 5 — stretch

- [ ] MP4/M4A container support (freeform atom `----:com.serato.dj:markersv2`;
      candidate crates: mp4ameta).
- [ ] EDMFormer-style fine-tune using own VDJ cue history as labels
      (repo 25ohms/EDMFormer has the recipe, no released weights; EDM-CUE
      dataset can supplement).
- [ ] ONNX port of CUE-DETR for a single-binary Rust pipeline
      (symphonia + mel + ort). Only worth it if the pipeline sees daily use.

## Open questions

- Where do generated cues rank vs manual ones? Policy today: never overwrite
  an index that exists unless `--replace`. Revisit once batch mode lands.
- Label taxonomy: freeform strings vs fixed set VDJ displays nicely.
- Should detection confidence be persisted (e.g. in label suffix) for later
  re-filtering without re-running the model?
