# /// script
# requires-python = ">=3.9,<3.10"
# dependencies = ["numpy==1.23.5", "madmom", "scipy", "setuptools<60", "mido"]
#
# [tool.uv.extra-build-dependencies]
# madmom = ["Cython", "numpy==1.23.5"]
# ///
"""Precompute a real beat + downbeat grid with madmom, as a JSON sidecar.

madmom (0.16.1) is the reference downbeat tracker but predates Python 3.10, so
it lives here in its own py3.9 / numpy-1.23 uv env — deliberately quarantined
from detect_cues.py's torch/transformers stack. This is a precompute step:

    uv run downbeats.py <tracks...> -o grids/

For each track it writes `grids/<stem>.beats.json`:
    {"beats":[t,...], "downbeats":[t,...], "bpm": float, "meter": 4}

where `beats` are every beat time (seconds) and `downbeats` are the bar-one
beats. detect_cues.py reads these with `--downbeats grids/` and snaps to
madmom's grid instead of librosa's onset-heuristic one. Times are seconds.

madmom's RNNDownBeatProcessor is a CPU bidirectional RNN (~several x realtime),
so a large library is a one-time batch; the sidecars are cached and reused.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from madmom.features.downbeats import RNNDownBeatProcessor, DBNDownBeatTrackingProcessor


def grid_for(path: str, meters):
    """Return (beats, downbeats, bpm, meter). beats/downbeats in seconds."""
    act = RNNDownBeatProcessor()(path)
    proc = DBNDownBeatTrackingProcessor(beats_per_bar=meters, fps=100)
    bt = proc(act)  # (N, 2): [time_seconds, beat_index_within_bar (1-based)]
    if bt is None or len(bt) == 0:
        return [], [], 0.0, meters[0]
    beats = bt[:, 0].astype(float)
    downbeats = bt[bt[:, 1] == 1, 0].astype(float)
    # tempo from the median inter-beat interval (robust to the odd dropped beat)
    ibi = np.diff(beats)
    bpm = float(60.0 / np.median(ibi)) if len(ibi) else 0.0
    # infer meter: median beats between consecutive downbeats
    meter = meters[0]
    if len(downbeats) >= 2:
        spans = np.round(np.diff(downbeats) / np.median(ibi)).astype(int) if len(ibi) else []
        if len(spans):
            meter = int(np.median(spans))
    return beats.tolist(), downbeats.tolist(), bpm, meter


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", nargs="+", type=Path)
    ap.add_argument("-o", "--outdir", type=Path, default=Path("grids"))
    ap.add_argument("--meters", default="4", help="candidate beats-per-bar, comma-separated (default 4; e.g. 3,4)")
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    meters = [int(x) for x in args.meters.split(",")]
    args.outdir.mkdir(parents=True, exist_ok=True)
    failed = []
    for n, track in enumerate(args.tracks, 1):
        out = args.outdir / f"{track.stem}.beats.json"
        if args.skip_existing and out.exists():
            print(f"[{n}/{len(args.tracks)}] skip (exists) {track.name}", flush=True)
            continue
        try:
            beats, downbeats, bpm, meter = grid_for(str(track), meters)
        except Exception as e:
            print(f"skip {track.name}: {e}", file=sys.stderr)
            failed.append(track.name)
            continue
        out.write_text(json.dumps({
            "beats": [round(t, 4) for t in beats],
            "downbeats": [round(t, 4) for t in downbeats],
            "bpm": round(bpm, 2),
            "meter": meter,
        }))
        print(f"[{n}/{len(args.tracks)}] {track.name}: {len(beats)} beats, "
              f"{len(downbeats)} bars @{bpm:.0f}bpm ({meter}/4) -> {out}", flush=True)

    if failed:
        print(f"\n{len(failed)} of {len(args.tracks)} failed", file=sys.stderr)


if __name__ == "__main__":
    main()
