# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = [
#   "all-in-one-mlx @ git+https://github.com/ssmall256/all-in-one-mlx",
#   "demucs-mlx[convert]", "mlx", "numpy",
# ]
# ///
"""Precompute a real beat + downbeat + STRUCTURE grid with All-In-One (MLX).

This is the Apple-Silicon-native replacement for downbeats.py: instead of just a
beat/downbeat grid (madmom), All-In-One also predicts *musical segments* with
labels (intro / verse / chorus / inst / bridge / outro ...). Runs entirely on
MLX — no madmom py3.9 quarantine, no NATTEN build. Weights are the repo's
converted harmonix folds under ./mlx-weights (see README).

    uv run allin1.py <tracks...> -o grids/ [--model harmonix-fold0|harmonix-all]

For each track it writes `grids/<stem>.beats.json` — a SUPERSET of downbeats.py's
sidecar, so `detect_cues.py --downbeats grids/` reads the beat grid unchanged:
    {"beats":[t...], "downbeats":[t...], "bpm":float, "meter":4,
     "segments":[{"start":s,"end":s,"label":"chorus"}, ...]}
All times are seconds. Segments give both the structural label and the boundary
(a cue near a boundary is a real cue; one adrift in a section is a likely rogue).

NOTE: works around an upstream packaging bug — helpers call the model with a
`return_embeddings=` kwarg its __call__ doesn't accept; embeddings are always in
the output, so we shim __call__ to accept-and-ignore it.
"""
import argparse
import json
import sys
from pathlib import Path

# --- upstream bug shim (see module docstring) ---
from allin1_mlx.models.allinone_mlx import AllInOneMLX

_orig_call = AllInOneMLX.__call__


def _shim(self, inputs, output_attentions=None, return_embeddings=None, **_):
    return _orig_call(self, inputs, output_attentions=output_attentions)


AllInOneMLX.__call__ = _shim

import allin1_mlx as A  # noqa: E402


def to_sidecar(r) -> dict:
    beats = [round(float(x), 4) for x in (getattr(r, "beats", []) or [])]
    downs = [round(float(x), 4) for x in (getattr(r, "downbeats", []) or [])]
    segs = [{"start": round(float(s.start), 4), "end": round(float(s.end), 4),
             "label": str(s.label)} for s in (getattr(r, "segments", []) or [])]
    bpm = getattr(r, "bpm", None)
    return {"beats": beats, "downbeats": downs, "bpm": round(float(bpm), 2) if bpm else 0.0,
            "meter": 4, "segments": segs}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tracks", nargs="+", type=Path)
    ap.add_argument("-o", "--outdir", type=Path, default=Path("grids"))
    ap.add_argument("--model", default="harmonix-fold0",
                    help="harmonix-fold0 (fast) or harmonix-all (8-fold ensemble, ~8x slower, best)")
    ap.add_argument("--weights-dir", type=Path, default=Path("mlx-weights"))
    ap.add_argument("--work-dir", type=Path, default=Path(".allin1-work"))
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    todo = [t for t in args.tracks
            if not (args.skip_existing and (args.outdir / f"{t.stem}.beats.json").exists())]
    if not todo:
        print("nothing to do (all sidecars exist)")
        return

    failed = []
    for n, track in enumerate(todo, 1):
        try:
            res = A.analyze(str(track), model=args.model, mlx_weights_dir=str(args.weights_dir),
                            demix_dir=str(args.work_dir / "demix"),
                            spec_dir=str(args.work_dir / "spec"), mlx_compile=False)
            r = res[0] if isinstance(res, (list, tuple)) else res
            sc = to_sidecar(r)
        except Exception as e:
            print(f"skip {track.name}: {e}", file=sys.stderr)
            failed.append(track.name)
            continue
        (args.outdir / f"{track.stem}.beats.json").write_text(json.dumps(sc))
        segsum = ", ".join(f"{s['label']}" for s in sc["segments"][:8])
        print(f"[{n}/{len(todo)}] {track.name}: {len(sc['beats'])} beats, "
              f"{len(sc['downbeats'])} bars @{sc['bpm']:.0f}bpm, "
              f"{len(sc['segments'])} segments ({segsum}) -> {args.outdir}", flush=True)

    if failed:
        print(f"\n{len(failed)} of {len(todo)} failed", file=sys.stderr)


if __name__ == "__main__":
    main()
