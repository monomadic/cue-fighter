# Tag inspector — design prototype

A self-contained HTML view of what is *actually embedded* in a set of audio files:
hot cues, ratings, hashtags, cover art, analysis blobs and every raw tag. Distinct
from `report.py`, which exists to judge *detection quality* against manual cues.

Not wired into the pipeline yet — this is the design mockup we iterated on, kept so
the decisions and the tricky bits aren't lost.

```sh
python3 prototype/inspector/extract.py TRACK... data.json     # probe -> one JSON blob
python3 prototype/inspector/mockup.py data.json mockup.html    # JSON -> the page
open mockup.html                                               # must be a real browser
```

## What the page does

One row per file: cover, title/artist, star rating, bpm/key, pad-count, hashtags, a
quality score, and a full-width waveform with clickable cue markers. Rows expand to a
cue table (pad, exact ms, bar, colour, label, plus Mixed In Key's own cues) and the
complete tag dump with base64 blobs decoded. Filter by rating, hashtag or text; sort
by name/score/rating/bpm/cue count.

## Non-obvious bits

- **Never `Path.resolve()` a track path.** `~/Music/Tracks` is a symlink into iCloud
  (`~/Library/Mobile Documents/...`), and browsers cannot read TCC-protected
  `~/Library` at all — resolving it silently kills `file://` playback. `os.path.abspath`
  normalises without following symlinks.
- **`encodeURI` does not escape `#`**, so `(126, F#m).flac` truncates at the fragment.
  Escape it explicitly. A large share of the library has a sharp in the key.
- **Audio is linked, never embedded** — a 33 MB FLAC per track defeats the point.
  Graphics *are* inline (cover art JPEG, waveform PNG, both base64).
- **Waveform colour is a hue-angle, not an RGB blend.** Each band sits at its own hue
  (bass 12°, mid/vocal 292°, air 186°) and the mix is their vector sum; hue is held flat
  across each 4-bar phrase off the embedded beatgrid. Blending RGB directly pushes
  balanced phrases to pale pink, and per-band normalisation pushes everything to white.
  Colour is measured *relative to the track's own average spectrum* — raw proportions
  barely vary within a track and come out uniformly muddy.
- **Waveforms are quantised to a 64-colour palette PNG** composited on the panel colour.
  A smooth-gradient RGBA PNG is ~7x larger (107 KB vs ~25 KB per track), which matters
  at 500 tracks.
- `RATING` is a 0/20/40/60/80/100 five-star scale; `GROUPING` holds space-separated
  hashtags on roughly half the library.

## Open questions

- Score weighting. Currently cue coverage 40 / label discipline 25 / tagging 25 /
  analysis data 10. Rating is deliberately *excluded* — a 5-star track with no cues
  should score badly, that's the file worth flagging.
- Multiple hashtags currently narrow (AND) rather than widen (OR).
- Ship as its own `inspect.py`, or as a mode on `report.py`.
- `--sidecar`: emit one `<track>.flac.html` next to each file, expanded by default,
  with a relative audio path so the pair survives being moved.
