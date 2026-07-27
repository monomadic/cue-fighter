# Jobs

For a job runner that executes `.job` files dropped into a folder and moves them
to `_done` when finished. Nothing here needs installing and nothing scans: a job
finds its own target by stripping `.job` from its own name.

```
my-song.untagged.flac        input
my-song.untagged.flac.job    cue-track.job, copied and renamed
my-song.flac                 output — no .job beside it, so nothing picks it up again
```

Copy the tracks in first and the `.job` last: the job file is the trigger, so
data is always complete before anything runs.

## The jobs

**`bootstrap.job`** — makes a machine able to do the work: `uv`, `ffmpeg`, the
repo, a release build of `cue-fighter`. Every step is a no-op when already
satisfied, so it is safe to drop in ahead of any batch. Writes
`bootstrap.status`; `OK` on the last line means ready.

**`cue-track.job`** — detects cues for one track and writes them into a tagged
copy. Copy it next to a file and rename it after that file. One job per track,
duplicated verbatim; shell scripts are text, so the cost is nothing and the copy
in `_done` becomes an exact record of how that file was produced — including the
detection parameters, which are literals in the script rather than config.

## Conventions

- **In-progress work is named for it.** The tagged copy is staged as
  `<name>.cueing.<ext>` and renamed only on success, so whatever watches the
  output folder never sees a file whose tags are half-written. The partial is
  still playable if you want to look at it mid-flight.
- **The input is never modified.** Cues go into the copy; `my-song.untagged.flac`
  is left exactly as it arrived.
- **A job refuses to clobber an existing output.** Belt and braces — the naming
  scheme already means a finished file has no job pointing at it.
- **Logs sit next to the job** as `<job>.log`, so they travel with it to `_done`.

## Changing parameters

Edit the literals at the top of `cue-track.job` before copying it. Since each
track carries its own copy, tracks cued with different settings keep their own
record, and regressions stay traceable to the exact script that produced them.
